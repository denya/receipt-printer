#!/usr/bin/env python3
"""Codex notify wrapper: print valuable turn results, then chain OMX notify.

Codex does not currently expose a stable session-end hook here, but it does
support a global `notify` command after each completed agent turn. This script
uses that surface to print only final-looking, high-value turns and then
forwards the same payload to the existing oh-my-codex notify hook.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any


PRINTER_URL = os.environ.get(
    "PRINTER_URL", "http://100.78.6.79:9100/print/session"
)
STATUS_URL = os.environ.get("STATUS_URL")
FILTER_MODEL = os.environ.get(
    "CODEX_PRINT_FILTER_MODEL", "claude-haiku-4-5-20251001"
)
OMX_NOTIFY_CMD = [
    "node",
    "/opt/homebrew/lib/node_modules/oh-my-codex/dist/scripts/notify-hook.js",
]
STATE_PATH = Path.home() / ".codex" / "hooks" / "print-codex-state.json"
STATUS_LOG_PATH = Path.home() / ".codex" / "hooks" / "print-codex-status.log"
STATUS_TOKEN_FILE = Path(
    os.environ.get(
        "STATUS_TOKEN_FILE", str(Path.home() / ".config" / "receipt-printer" / "status-api-token")
    )
).expanduser()
MAX_RECENT_TURNS = 400
RECENT_PURGE_TO = 250
MAX_TITLE_CHARS = 72
MAX_RESULT_CHARS = 190
MAX_SUMMARY_CHARS = 220
MAX_RESULTS = 5
MAX_TABLE_ROWS = 12
SECTION_HEADINGS = {
    "changed",
    "changes",
    "completed",
    "done",
    "summary",
    "verified",
    "verification",
    "tests",
    "status",
    "next steps",
    "remaining risks",
    "risks",
}
FOLLOWUP_SUMMARY_MARKERS = (
    "test",
    "verified",
    "deployed",
    "waiting",
    "input",
    "blocked",
    "fixed",
    "completed",
)


def safe_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return ""


def load_payload(argv: list[str]) -> tuple[dict[str, Any] | None, str | None]:
    raw = argv[-1] if argv else None
    if not raw or raw.startswith("-"):
        return None, raw
    try:
        payload = json.loads(raw)
    except Exception:
        return None, raw
    if not isinstance(payload, dict):
        return None, raw
    return payload, raw


def normalize_input_messages(payload: dict[str, Any]) -> list[str]:
    items = payload.get("input-messages") or payload.get("input_messages") or []
    if not isinstance(items, list):
        return []
    return [safe_string(item) for item in items if safe_string(item).strip()]


def assistant_message(payload: dict[str, Any]) -> str:
    return (
        safe_string(payload.get("last-assistant-message"))
        or safe_string(payload.get("last_assistant_message"))
        or safe_string(payload.get("assistant_message"))
    ).strip()


def complete_clip(text: str, limit: int) -> str:
    text = clean_text(text)
    if len(text) <= limit:
        return text
    window = text[: limit + 1]
    floor = max(24, int(limit * 0.55))
    for pattern in (r"[.!?](?=\s|$)", r"[:;](?=\s|$)", r",(?=\s)"):
        matches = list(re.finditer(pattern, window))
        matches = [m for m in matches if m.end() >= floor]
        if matches:
            return window[: matches[-1].end()].strip()
    clipped = window[:limit].rsplit(" ", 1)[0].strip(" ,;:-")
    return clipped + "." if clipped and clipped[-1] not in ".!?" else clipped


def clean_text(text: str) -> str:
    text = safe_string(text)
    text = re.sub(r"```[\s\S]*?```", " ", text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"\[([^\]\n]{1,120})\]\((?:[^)\s]+)(?:\s+\"[^\"]*\")?\)", r"\1", text)
    text = re.sub(r"https?://\S+|www\.\S+", " ", text)
    text = re.sub(r"(?<!\w)/(?:[\w .@+-]+/){2,}([\w .@+-]+\.[A-Za-z0-9]+)(?::\d+)?", r"\1", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s{0,3}>\s?", "", text)
    text = re.sub(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+", "", text)
    text = re.sub(r"[*_~]{1,3}", "", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\b(?:at|from|via)\s+(?=(?:without|with|and|for)\b)", "", text)
    return text


def looks_like_internal_prompt(text: str) -> bool:
    lowered = clean_text(text).lower()
    internal_markers = (
        "you are a helpful assistant.",
        "you will be presented with a user prompt",
        "your job is to provide a short title",
        "read-only final verification",
        "return either:",
        "review commit ",
        "focus only on these files:",
    )
    return any(marker in lowered for marker in internal_markers)


def json_title(text: str) -> str:
    try:
        parsed = json.loads(text)
    except Exception:
        return ""
    if isinstance(parsed, dict):
        title = safe_string(parsed.get("title")).strip()
        if title:
            return complete_clip(title, MAX_TITLE_CHARS)
    return ""


def compact_title(text: str, cwd: str = "") -> str:
    cleaned = clean_text(text)
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
        return f"Codex {Path(cwd).name or cwd}"
    first_sentence = split_sentences(cleaned)[0] if split_sentences(cleaned) else cleaned
    first_sentence = re.sub(r"^(please|can you|could you|i need you to)\s+", "", first_sentence, flags=re.I)
    title = complete_clip(first_sentence, MAX_TITLE_CHARS).strip(" .")
    return title or "Codex session"


def derive_title(payload: dict[str, Any]) -> str:
    for item in normalize_input_messages(payload):
        cleaned = clean_text(item)
        if cleaned and not cleaned.startswith("/") and not looks_like_internal_prompt(cleaned):
            return compact_title(cleaned, safe_string(payload.get("cwd")))
    assistant_title = json_title(assistant_message(payload))
    if assistant_title:
        return assistant_title
    cwd = safe_string(payload.get("cwd"))
    if cwd:
        return f"Codex · {Path(cwd).name or cwd}"
    return "Codex session"


def base_service_url() -> str:
    if "/print/" in PRINTER_URL:
        return PRINTER_URL.split("/print/", 1)[0]
    return PRINTER_URL.rsplit("/", 1)[0]


def resolved_status_url() -> str:
    return STATUS_URL or (base_service_url() + "/status/update")


def rich_print_url() -> str:
    return base_service_url() + "/print/rich"


def load_status_token() -> str:
    token = safe_string(os.environ.get("STATUS_API_TOKEN")).strip()
    if token:
        return token
    try:
        return STATUS_TOKEN_FILE.read_text().strip()
    except Exception:
        return ""


def _split_table_cells(line: str) -> list[str]:
    line = line.strip()
    if not line.startswith("|") or not line.endswith("|"):
        return []
    cells = [clean_text(cell.strip()) for cell in line.strip("|").split("|")]
    return cells if any(cells) else []


def _is_table_separator(line: str) -> bool:
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)


def extract_markdown_tables(text: str) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines) - 1:
        headers = _split_table_cells(lines[i])
        if not headers or not _is_table_separator(lines[i + 1]):
            i += 1
            continue
        rows: list[list[str]] = []
        j = i + 2
        while j < len(lines):
            row = _split_table_cells(lines[j])
            if not row:
                break
            if len(row) < len(headers):
                row.extend([""] * (len(headers) - len(row)))
            rows.append(row[: len(headers)])
            j += 1
        if rows:
            tables.append({"headers": headers, "rows": rows[:MAX_TABLE_ROWS]})
        i = max(j, i + 2)
    return tables


def remove_markdown_tables(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        if i < len(lines) - 1 and _split_table_cells(lines[i]) and _is_table_separator(lines[i + 1]):
            i += 2
            while i < len(lines) and _split_table_cells(lines[i]):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def summarize_table_for_voice(table: dict[str, Any]) -> str:
    headers = [clean_text(h) for h in table.get("headers", []) if clean_text(h)]
    rows = [[clean_text(cell) for cell in row] for row in table.get("rows", [])]
    row_count = len(rows)
    parts: list[str] = []
    if headers:
        parts.append(f"Table with {row_count} rows.")
        parts.append(f"Columns are {' and '.join(headers[:3])}.")
    else:
        parts.append(f"Table with {row_count} rows.")
    for row in rows[:2]:
        pairs: list[str] = []
        for header, cell in zip(headers, row):
            if cell:
                pairs.append(f"{header} {cell}")
        if pairs:
            parts.append(". ".join(pairs[:3]) + ".")
    return complete_clip(" ".join(parts), MAX_SUMMARY_CHARS)


def summarize_tables_for_voice(tables: list[dict[str, Any]]) -> str:
    if not tables:
        return ""
    if len(tables) == 1:
        return summarize_table_for_voice(tables[0])
    total_rows = sum(len(table.get("rows", [])) for table in tables)
    first = summarize_table_for_voice(tables[0])
    return complete_clip(f"{len(tables)} tables with {total_rows} total rows. {first}", MAX_SUMMARY_CHARS)


def extract_results(text: str) -> list[str]:
    bullets: list[str] = []
    text = remove_markdown_tables(text)
    in_fence = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if re.fullmatch(r"[*_`~#>\-\s]+", line):
            continue
        if line.rstrip(":").lower() in SECTION_HEADINGS:
            continue
        is_item = bool(re.match(r"^\s*(?:[-*•]|\d+[.)])\s+", raw_line))
        line = re.sub(r"^[-*•]\s+", "", line)
        line = re.sub(r"^\d+[.)]\s+", "", line)
        line = clean_text(line)
        if not line:
            continue
        if line.rstrip(":").lower() in SECTION_HEADINGS:
            continue
        if line.lower().startswith(("if you want", "if you'd like", "next step: if")):
            continue
        sentence_parts = split_sentences(line)
        if not is_item and len(sentence_parts) > 1:
            continue
        clipped = complete_clip(line, MAX_RESULT_CHARS)
        if clipped and clipped not in bullets:
            bullets.append(clipped)
        if len(bullets) >= MAX_RESULTS:
            return bullets

    if bullets:
        return bullets[:MAX_RESULTS]

    for sentence in split_sentences(text):
        if sentence.lower().startswith(("if you want", "if you'd like")):
            continue
        clipped = complete_clip(sentence, MAX_RESULT_CHARS)
        if clipped and clipped not in bullets:
            bullets.append(clipped)
        if len(bullets) >= MAX_RESULTS:
            break
    return bullets[:MAX_RESULTS]


def split_sentences(text: str) -> list[str]:
    chunks = re.split(r"(?<=[.!?])\s+", clean_text(text))
    return [chunk.strip() for chunk in chunks if chunk.strip()]


def classify_status(text: str) -> str:
    lowered = clean_text(text).lower()
    waiting_markers = (
        "let me know",
        "please confirm",
        "confirm whether",
        "before i proceed",
        "before i continue",
        "would you like",
        "do you want",
        "want me to",
        "should i",
        "if you'd like",
        "if you want",
    )
    blocked_markers = (
        "blocked",
        "failed",
        "failure",
        "error",
        "unable to",
        "could not",
        "can't ",
        "cannot ",
        "missing ",
        "permission denied",
        "timed out",
    )
    completion_markers = (
        "tests passed",
        "completed",
        "done",
        "deployed",
        "fixed",
        "updated",
        "created",
        "implemented",
        "verified",
        "shipped",
    )
    has_blocked = any(marker in lowered for marker in blocked_markers)
    has_completion = any(marker in lowered for marker in completion_markers)
    if lowered.endswith("?") or any(marker in lowered for marker in waiting_markers):
        return "waiting"
    if has_completion:
        return "completed"
    if has_blocked:
        return "blocked"
    if looks_like_interim_update(text):
        return "running"
    return "unknown"


def derive_summary_line(text: str, status: str) -> str:
    embedded_title = json_title(text)
    if embedded_title:
        return embedded_title
    table_summary = summarize_tables_for_voice(extract_markdown_tables(text))
    if table_summary:
        return table_summary
    sentences = split_sentences(text)
    if status == "waiting":
        return "Waiting for your input."
    if status == "blocked":
        if sentences:
            return complete_clip(sentences[0], MAX_SUMMARY_CHARS)
        return "Blocked right now."
    results = extract_results(text)
    if results:
        combined = " ".join(results[:2])
        if (
            status == "completed"
            and len(combined) <= MAX_SUMMARY_CHARS
            and len(results) > 1
            and any(marker in results[1].lower() for marker in FOLLOWUP_SUMMARY_MARKERS)
        ):
            return combined
        return complete_clip(results[0], MAX_SUMMARY_CHARS)
    if sentences:
        return complete_clip(sentences[0], MAX_SUMMARY_CHARS)
    fallback = clean_text(text)
    if not fallback:
        return "Status update available."
    return complete_clip(fallback, MAX_SUMMARY_CHARS)


def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {"recent_turn_keys": []}


def save_state(state: dict[str, Any]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def seen_print_turn(payload: dict[str, Any]) -> bool:
    thread_id = safe_string(payload.get("thread-id") or payload.get("thread_id"))
    turn_id = safe_string(payload.get("turn-id") or payload.get("turn_id"))
    if not thread_id or not turn_id:
        return False

    key = f"{thread_id}|{turn_id}"
    state = load_state()
    recent = state.get("recent_turn_keys")
    if not isinstance(recent, list):
        recent = []
    if key in recent:
        return True

    recent.append(key)
    if len(recent) > MAX_RECENT_TURNS:
        recent = recent[-RECENT_PURGE_TO:]
    state["recent_turn_keys"] = recent
    save_state(state)
    return False


def seen_print_fingerprint(payload: dict[str, Any], text: str) -> bool:
    title = derive_title(payload)
    results = extract_results(text)
    status = classify_status(text)
    fingerprint = "|".join([title.lower(), status, *[r.lower() for r in results[:3]]])
    fingerprint = re.sub(r"\W+", " ", fingerprint).strip()
    if not fingerprint:
        return False
    key = "fp:" + fingerprint[:260]
    state = load_state()
    recent = state.get("recent_print_fingerprints")
    if not isinstance(recent, list):
        recent = []
    if key in recent:
        return True
    recent.append(key)
    if len(recent) > MAX_RECENT_TURNS:
        recent = recent[-RECENT_PURGE_TO:]
    state["recent_print_fingerprints"] = recent
    save_state(state)
    return False


def debug_log(message: str) -> None:
    try:
        STATUS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATUS_LOG_PATH.write_text(
            (STATUS_LOG_PATH.read_text() if STATUS_LOG_PATH.exists() else "") + message + "\n"
        )
    except Exception:
        pass


def looks_like_interim_update(text: str) -> bool:
    lowered = text.lower().strip()
    interim_starts = (
        "i'm ",
        "i am ",
        "i’ll ",
        "i will ",
        "checking ",
        "inspecting ",
        "reviewing ",
        "running ",
        "looking ",
        "mapping ",
        "tracing ",
    )
    completion_markers = (
        "verification",
        "remaining risks",
        "what changed",
        "pi status",
        "tests passed",
        "service is healthy",
        "deployed",
        "updated",
        "fixed",
        "installed",
    )
    if any(marker in lowered for marker in completion_markers):
        return False
    return lowered.startswith(interim_starts)


def local_should_print(payload: dict[str, Any], text: str) -> bool:
    if safe_string(payload.get("type")) not in {"agent-turn-complete", "", "turn-ended"}:
        return False
    if len(clean_text(text)) < 80:
        return False
    if looks_like_interim_update(text):
        return False
    if classify_status(text) in {"running", "waiting", "unknown"}:
        return False
    if not extract_results(text):
        return False
    return True


def remote_should_print(payload: dict[str, Any], text: str) -> bool:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return local_should_print(payload, text)

    sample = {
        "cwd": safe_string(payload.get("cwd")),
        "type": safe_string(payload.get("type")),
        "first_user_message": derive_title(payload),
        "assistant_message": text[:1500],
    }
    system = (
        "You decide whether a Codex agent turn deserves a paper receipt printed at home. "
        "Print ONLY when this turn is a final valuable outcome: shipped code, a bug fixed, "
        "a deployment completed, a meaningful review completed, or a concrete deliverable "
        "handed back. DO NOT print interim progress updates, exploration notes, partial work, "
        "clarifying questions, or trivial answers. Reply with exactly one token: PRINT or SKIP."
    )
    body = json.dumps(
        {
            "model": FILTER_MODEL,
            "max_tokens": 4,
            "system": system,
            "messages": [
                {
                    "role": "user",
                    "content": "Decide PRINT or SKIP:\n\n"
                    + json.dumps(sample, ensure_ascii=False, indent=2),
                }
            ],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return local_should_print(payload, text)

    verdict = ""
    for block in data.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            verdict += safe_string(block.get("text"))
    return "SKIP" not in verdict.strip().upper().split()


def should_print(payload: dict[str, Any], text: str) -> bool:
    mode = os.environ.get("PRINT_FILTER", "").lower()
    if mode in {"off", "force"}:
        return True
    return remote_should_print(payload, text)


def post_json(url: str, body: dict[str, Any]) -> None:
    headers = {"Content-Type": "application/json"}
    token = load_status_token()
    if token:
        headers["x-status-token"] = token
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=4):
        return


def post_status_update(payload: dict[str, Any], text: str) -> None:
    status = classify_status(text)
    thread_id = safe_string(payload.get("thread-id") or payload.get("thread_id"))
    turn_id = safe_string(payload.get("turn-id") or payload.get("turn_id"))
    body = {
        "source": "codex",
        "session_key": thread_id or derive_title(payload),
        "turn_key": f"{thread_id}:{turn_id}" if thread_id and turn_id else None,
        "title": derive_title(payload),
        "summary_line": derive_summary_line(text, status),
        "status": status,
        "cwd": safe_string(payload.get("cwd")) or None,
        "model": safe_string(payload.get("model") or payload.get("assistant_model") or "codex")[:80] or None,
        "updated_at": safe_string(payload.get("timestamp") or payload.get("created_at")) or None,
    }
    body = {key: value for key, value in body.items() if value is not None and value != ""}
    post_json(resolved_status_url(), body)


def post_receipt(payload: dict[str, Any], text: str) -> None:
    tables = extract_markdown_tables(text)
    if tables:
        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "title": "CODEX",
                "subtitle": "STATUS",
                "logo": False,
            },
            {"type": "title", "content": derive_title(payload)},
            {"type": "text", "content": summarize_tables_for_voice(tables), "style": "body"},
        ]
        for table in tables:
            blocks.append({"type": "table", "headers": table["headers"], "rows": table["rows"]})
        results = extract_results(text)
        if results:
            blocks.append({"type": "bullets", "items": results[:3]})
        post_json(rich_print_url(), {"blocks": blocks})
        return

    body = {
        "brand": "CODEX",
        "title": derive_title(payload),
        "results": extract_results(text),
        "model": safe_string(payload.get("model") or payload.get("assistant_model") or "codex")[:40],
    }
    body = {key: value for key, value in body.items() if value}
    post_json(PRINTER_URL, body)


def chain_omx_notify(raw_payload: str | None) -> None:
    if not raw_payload:
        return
    try:
        subprocess.run(
            [*OMX_NOTIFY_CMD, raw_payload],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def main() -> int:
    payload, raw_payload = load_payload(sys.argv[1:])
    try:
        if not payload:
            return 0

        text = assistant_message(payload)
        if not text:
            return 0
        try:
            post_status_update(payload, text)
        except Exception as exc:
            debug_log(f"status_update_failed thread={safe_string(payload.get('thread-id') or payload.get('thread_id'))} error={exc!r}")
        if should_print(payload, text):
            if seen_print_turn(payload):
                return 0
            if seen_print_fingerprint(payload, text):
                return 0
            try:
                post_receipt(payload, text)
            except Exception:
                pass
        return 0
    finally:
        chain_omx_notify(raw_payload)


if __name__ == "__main__":
    raise SystemExit(main())
