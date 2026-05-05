"""SQLite-backed store for live session status and Alexa voice summaries."""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List

from schemas import SessionStatusUpdate


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def normalize_timestamp(value: str | None) -> str:
    if not value:
        return utc_now_iso()
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


class StatusStore:
    def __init__(
        self,
        db_path: str,
        active_window_seconds: int = 20 * 60,
        recent_window_seconds: int = 24 * 60 * 60,
    ) -> None:
        self.db_path = str(Path(db_path).expanduser())
        self.active_window_seconds = max(60, int(active_window_seconds))
        self.recent_window_seconds = max(self.active_window_seconds, int(recent_window_seconds))
        self._lock = threading.Lock()
        self._init_db()

    def _select_recent_rows(self, conn: sqlite3.Connection, cutoff: str, limit: int) -> List[sqlite3.Row]:
        return conn.execute(
            """
            SELECT *
            FROM session_latest
            WHERE updated_at >= ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()

    def ingest(self, update: SessionStatusUpdate) -> Dict[str, Any]:
        updated_at = normalize_timestamp(update.updated_at)
        payload = update.model_dump(mode="json")
        payload["updated_at"] = updated_at
        event_type = "update"
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO session_events (
                        session_key, turn_key, source, event_type, title,
                        summary_line, status, created_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        update.session_key,
                        update.turn_key,
                        update.source,
                        event_type,
                        update.title,
                        update.summary_line,
                        update.status,
                        updated_at,
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )

                current = conn.execute(
                    """
                    SELECT first_seen_at, updated_at
                    FROM session_latest
                    WHERE session_key = ?
                    """,
                    (update.session_key,),
                ).fetchone()
                if current and current["updated_at"] > updated_at:
                    conn.commit()
                    return {"ok": True, "stored": False, "reason": "stale_update"}

                first_seen_at = current["first_seen_at"] if current else updated_at
                conn.execute(
                    """
                    INSERT INTO session_latest (
                        session_key, source, title, summary_line, status, cwd,
                        model, turns, duration, updated_at, first_seen_at,
                        last_turn_key, raw_payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_key) DO UPDATE SET
                        source = excluded.source,
                        title = excluded.title,
                        summary_line = excluded.summary_line,
                        status = excluded.status,
                        cwd = excluded.cwd,
                        model = excluded.model,
                        turns = excluded.turns,
                        duration = excluded.duration,
                        updated_at = excluded.updated_at,
                        last_turn_key = excluded.last_turn_key,
                        raw_payload_json = excluded.raw_payload_json
                    """,
                    (
                        update.session_key,
                        update.source,
                        update.title,
                        update.summary_line,
                        update.status,
                        update.cwd,
                        update.model,
                        update.turns,
                        update.duration,
                        updated_at,
                        first_seen_at,
                        update.turn_key,
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )
                conn.commit()
                return {"ok": True, "stored": True, "updated_at": updated_at}
            finally:
                conn.close()

    def latest(self, limit: int = 3) -> Dict[str, Any]:
        effective_limit = max(1, min(int(limit), 10))
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM session_latest
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (effective_limit,),
                ).fetchall()
            finally:
                conn.close()
        generated_at = utc_now_iso()
        sessions = [self._row_to_public(row, generated_at) for row in rows]
        return {
            "generated_at": generated_at,
            "sessions": sessions,
        }

    def voice_payload(self, limit: int = 3) -> Dict[str, Any]:
        effective_limit = max(1, min(int(limit), 10))
        generated_at = utc_now_iso()
        active_cutoff = self._age_cutoff(self.active_window_seconds)
        recent_cutoff = self._age_cutoff(self.recent_window_seconds)
        with self._lock:
            conn = self._connect()
            try:
                active_rows = self._select_recent_rows(conn, active_cutoff, 20)
                recent_rows = self._select_recent_rows(conn, recent_cutoff, 20)
            finally:
                conn.close()

        active_voice_rows = [row for row in active_rows if self._is_voiceworthy_row(row)]
        recent_voice_rows = [row for row in recent_rows if self._is_voiceworthy_row(row)]
        visible_rows = self._merge_unique_rows(active_voice_rows, recent_voice_rows)[:effective_limit]

        if active_voice_rows:
            speech = self._active_speech(visible_rows, len(active_voice_rows))
            stale = False
        elif recent_voice_rows:
            speech = self._recent_speech(visible_rows)
            stale = True
        else:
            visible_rows = []
            speech = "No recent Claude or Codex session updates are available right now."
            stale = True

        sessions = [self._row_to_public(row, generated_at) for row in visible_rows]
        return {
            "generated_at": generated_at,
            "stale": stale,
            "speech_text": speech,
            "sessions": sessions,
        }

    def _connect(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS session_latest (
                        session_key TEXT PRIMARY KEY,
                        source TEXT NOT NULL,
                        title TEXT NOT NULL,
                        summary_line TEXT NOT NULL,
                        status TEXT NOT NULL,
                        cwd TEXT,
                        model TEXT,
                        turns INTEGER,
                        duration TEXT,
                        updated_at TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL,
                        last_turn_key TEXT,
                        raw_payload_json TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS session_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_key TEXT NOT NULL,
                        turn_key TEXT,
                        source TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        title TEXT NOT NULL,
                        summary_line TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        payload_json TEXT,
                        UNIQUE(session_key, turn_key, event_type)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_session_latest_updated_at
                    ON session_latest(updated_at DESC)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_session_events_created_at
                    ON session_events(created_at DESC)
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def _row_to_public(self, row: sqlite3.Row, generated_at: str) -> Dict[str, Any]:
        updated_at = row["updated_at"]
        return {
            "session_key": row["session_key"],
            "source": row["source"],
            "title": row["title"],
            "summary_line": row["summary_line"],
            "status": row["status"],
            "cwd": row["cwd"],
            "model": row["model"],
            "turns": row["turns"],
            "duration": row["duration"],
            "updated_at": updated_at,
            "age_seconds": self._age_seconds(updated_at, generated_at),
            "active": updated_at >= self._age_cutoff(self.active_window_seconds, generated_at),
        }

    def _active_speech(self, rows: List[sqlite3.Row], total_active: int) -> str:
        noun = "session" if total_active == 1 else "sessions"
        parts = [f"You have {total_active} active {noun}."]
        for row in rows:
            parts.append(self._spoken_session_line(row))
        return " ".join(parts)

    def _recent_speech(self, rows: List[sqlite3.Row]) -> str:
        if not rows:
            return "No recent Claude or Codex session updates are available right now."
        parts = ["Latest recent sessions."]
        for row in rows:
            parts.append(self._spoken_session_line(row))
        return " ".join(parts)

    def _spoken_session_line(self, row: sqlite3.Row) -> str:
        title = self._clean_title(row["title"], row["cwd"], row["summary_line"]).strip().rstrip(".")
        summary = self._clean_summary(row["summary_line"], row["status"]).strip()
        if summary and summary[-1] not in ".!?":
            summary = summary + "."
        if not summary:
            summary = self._fallback_summary(row["status"])
        return f"{title}. {summary}"

    def _fallback_summary(self, status: str) -> str:
        mapping = {
            "running": "Still running.",
            "waiting": "Waiting for input.",
            "completed": "Recently completed.",
            "blocked": "Blocked right now.",
            "unknown": "Status is unknown.",
        }
        return mapping.get(status, "Status is unknown.")

    def _age_seconds(self, updated_at: str, now_iso: str) -> int:
        now_dt = dt.datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        updated_dt = dt.datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        return max(0, int((now_dt - updated_dt).total_seconds()))

    def _age_cutoff(self, seconds: int, now_iso: str | None = None) -> str:
        now_dt = dt.datetime.fromisoformat((now_iso or utc_now_iso()).replace("Z", "+00:00"))
        cutoff = now_dt - dt.timedelta(seconds=seconds)
        return cutoff.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _merge_unique_rows(
        self, primary: List[sqlite3.Row], secondary: List[sqlite3.Row]
    ) -> List[sqlite3.Row]:
        merged: List[sqlite3.Row] = []
        seen: set[str] = set()
        for row in [*primary, *secondary]:
            key = row["session_key"]
            if key in seen:
                continue
            seen.add(key)
            merged.append(row)
        return merged

    def _is_voiceworthy_row(self, row: sqlite3.Row) -> bool:
        title = (row["title"] or "").strip()
        summary = (row["summary_line"] or "").strip()
        if self._looks_like_internal_prompt(title):
            return False
        if summary.startswith("{") and self._extract_json_title(summary):
            return False
        return True

    def _clean_title(self, title: str | None, cwd: str | None, summary: str | None) -> str:
        cleaned = (title or "").strip()
        if not cleaned:
            cleaned = "Session"
        if self._looks_like_internal_prompt(cleaned):
            embedded = self._extract_json_title(summary or "")
            if embedded:
                return embedded
            if cwd:
                return f"Codex · {Path(cwd).name or cwd}"
            return "Codex session"
        return self._compact_title(cleaned, cwd)

    def _clean_summary(self, summary: str | None, status: str) -> str:
        cleaned = self._sanitize_text(summary or "")
        embedded = self._extract_json_title(cleaned)
        if embedded:
            return embedded
        if not cleaned:
            return self._fallback_summary(status)
        return self._complete_clip(cleaned, 220)

    def _extract_json_title(self, text: str) -> str:
        try:
            parsed = json.loads(text)
        except Exception:
            return ""
        if isinstance(parsed, dict):
            title = str(parsed.get("title") or "").strip()
            if title:
                return self._complete_clip(self._sanitize_text(title), 72)
        return ""

    def _looks_like_internal_prompt(self, text: str) -> bool:
        lowered = re.sub(r"\s+", " ", (text or "").strip()).lower()
        markers = (
            "you are a helpful assistant.",
            "you will be presented with a user prompt",
            "your job is to provide a short title",
            "read-only final verification",
            "focus only on these files:",
            "return either:",
        )
        return any(marker in lowered for marker in markers)

    def _compact_title(self, text: str, cwd: str | None = None) -> str:
        cleaned = self._sanitize_text(text)
        lowered = cleaned.lower()
        keyword_titles = (
            (("receipt", "voice"), "Receipt and voice status quality"),
            (("alexa", "status"), "Alexa status reporting"),
            (("hook", "status"), "Status hook reliability"),
            (("printer", "ticket"), "Receipt ticket quality"),
        )
        for needles, label in keyword_titles:
            if all(needle in lowered for needle in needles):
                return label
        if not cleaned and cwd:
            return f"Codex · {Path(cwd).name or cwd}"
        first = re.split(r"(?<=[.!?])\s+", cleaned)[0] if cleaned else ""
        first = re.sub(
            r"^(please|can you|could you|i need you to)\s+",
            "",
            first,
            flags=re.I,
        )
        return self._complete_clip(first, 72).strip(" .") or "Session"

    def _sanitize_text(self, text: str) -> str:
        text = str(text or "")
        text = re.sub(r"```[\s\S]*?```", " ", text)
        text = re.sub(r"`([^`\n]+)`", r"\1", text)
        text = re.sub(r"\[([^\]\n]{1,120})\]\((?:[^)\s]+)(?:\s+\"[^\"]*\")?\)", r"\1", text)
        text = re.sub(r"https?://\S+|www\.\S+", " ", text)
        text = re.sub(
            r"(?<!\w)/(?:[\w .@+-]+/){2,}([\w .@+-]+\.[A-Za-z0-9]+)(?::\d+)?",
            r"\1",
            text,
        )
        text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
        text = re.sub(r"(?m)^\s{0,3}>\s?", "", text)
        text = re.sub(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+", "", text)
        text = re.sub(r"[*_~]{1,3}", "", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return re.sub(r"\b(?:at|from|via)\s+(?=(?:without|with|and|for)\b)", "", text)

    def _complete_clip(self, text: str, limit: int) -> str:
        cleaned = self._sanitize_text(text)
        if len(cleaned) <= limit:
            return cleaned
        window = cleaned[: limit + 1]
        floor = max(24, int(limit * 0.55))
        for pattern in (r"[.!?](?=\s|$)", r"[:;](?=\s|$)", r",(?=\s)"):
            matches = [m for m in re.finditer(pattern, window) if m.end() >= floor]
            if matches:
                return window[: matches[-1].end()].strip()
        clipped = window[:limit].rsplit(" ", 1)[0].strip(" ,;:-")
        return clipped + "." if clipped and clipped[-1] not in ".!?" else clipped
