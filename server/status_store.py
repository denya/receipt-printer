"""SQLite-backed store for live session status and Alexa voice summaries."""
from __future__ import annotations

import datetime as dt
import json
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
                active_rows = conn.execute(
                    """
                    SELECT *
                    FROM session_latest
                    WHERE updated_at >= ?
                    ORDER BY updated_at DESC
                    """,
                    (active_cutoff,),
                ).fetchall()
                recent_rows = conn.execute(
                    """
                    SELECT *
                    FROM session_latest
                    WHERE updated_at >= ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (recent_cutoff, effective_limit),
                ).fetchall()
            finally:
                conn.close()

        if active_rows:
            visible_rows = active_rows[:effective_limit]
            speech = self._active_speech(visible_rows, len(active_rows))
            stale = False
        elif recent_rows:
            visible_rows = recent_rows[:effective_limit]
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
        count = len(rows)
        noun = "session" if total_active == 1 else "sessions"
        parts = [f"You have {total_active} active {noun}."]
        for row in rows:
            parts.append(self._spoken_session_line(row))
        remaining = total_active - count
        if remaining > 0:
            more = "session" if remaining == 1 else "sessions"
            parts.append(f"And {remaining} more active {more}.")
        return " ".join(parts)

    def _recent_speech(self, rows: List[sqlite3.Row]) -> str:
        latest = rows[0]
        line = self._spoken_session_line(latest)
        return (
            "No active sessions right now. "
            f"Latest recent update: {line}"
        )

    def _spoken_session_line(self, row: sqlite3.Row) -> str:
        title = row["title"].strip().rstrip(".")
        summary = row["summary_line"].strip()
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
