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
STATUS_TOKEN_FILE = Path(
    os.environ.get(
        "STATUS_TOKEN_FILE", str(Path.home() / ".config" / "receipt-printer" / "status-api-token")
    )
).expanduser()
MAX_RECENT_TURNS = 400
RECENT_PURGE_TO = 250


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


def clean_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def derive_title(payload: dict[str, Any]) -> str:
    for item in normalize_input_messages(payload):
        cleaned = clean_text(item)
        if cleaned and not cleaned.startswith("/"):
            return cleaned[:120]
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


def load_status_token() -> str:
    token = safe_string(os.environ.get("STATUS_API_TOKEN")).strip()
    if token:
        return token
    try:
        return STATUS_TOKEN_FILE.read_text().strip()
    except Exception:
        return ""


def extract_results(text: str) -> list[str]:
    bullets: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^[-*]\s+", "", line)
        line = re.sub(r"^\d+\.\s+", "", line)
        line = clean_text(line)
        if not line:
            continue
        bullets.append(line[:120])
        if len(bullets) >= 3:
            return bullets

    paragraphs = [clean_text(p) for p in text.split("\n\n")]
    for paragraph in paragraphs:
        if paragraph:
            bullets.append(paragraph[:120])
        if len(bullets) >= 3:
            break
    return bullets[:3]


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
    if lowered.endswith("?") or any(marker in lowered for marker in waiting_markers):
        return "waiting"
    if any(marker in lowered for marker in blocked_markers):
        return "blocked"
    if any(marker in lowered for marker in completion_markers):
        return "completed"
    if looks_like_interim_update(text):
        return "running"
    return "unknown"


def derive_summary_line(text: str, status: str) -> str:
    sentences = split_sentences(text)
    if status == "waiting":
        return "Waiting for your input."
    if status == "blocked":
        if sentences:
            return sentences[0][:160]
        return "Blocked right now."
    results = extract_results(text)
    if results:
        return results[0][:160]
    if sentences:
        return sentences[0][:160]
    fallback = clean_text(text)
    if not fallback:
        return "Status update available."
    return fallback[:160]


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


def seen_turn(payload: dict[str, Any]) -> bool:
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
    if len(text) < 140:
        return False
    if looks_like_interim_update(text):
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
        if seen_turn(payload):
            return 0

        text = assistant_message(payload)
        if not text:
            return 0
        try:
            post_status_update(payload, text)
        except Exception:
            pass
        if should_print(payload, text):
            try:
                post_receipt(payload, text)
            except Exception:
                pass
        return 0
    finally:
        chain_omx_notify(raw_payload)


if __name__ == "__main__":
    raise SystemExit(main())
