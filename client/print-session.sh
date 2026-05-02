#!/usr/bin/env bash
# SessionEnd hook → POST a session ticket to the home receipt printer.
#
# Behavior:
# - Reads the Claude SessionEnd event JSON from stdin.
# - Parses the transcript file to extract the first user message,
#   last assistant turns, model, turn count, and duration.
# - Asks Claude Haiku (claude-haiku-4-5-20251001) whether this session
#   is a "FINAL VALUABLE" task worth a paper receipt — trivial, aborted,
#   or chat-only sessions are filtered out.
# - On "PRINT", POSTs the ticket to the home receipt printer.
# - All failures are silent: a missing/unreachable printer or API must
#   never block Claude Code.
#
# Optional env vars (graceful fallback when absent):
#   ANTHROPIC_API_KEY   API key for the Haiku filter call.
#                       If unset, the hook prints every session (legacy mode).
#   PRINTER_URL         Override the printer endpoint. Defaults below.
#   PRINT_FILTER        "off"   — bypass filter, print everything.
#                       "force" — same as "off".
#                       (default) — Haiku decides.

set -e

PRINTER_URL="${PRINTER_URL:-http://100.78.6.79:9100/print/session}"
FILTER_MODEL="claude-haiku-4-5-20251001"

event="$(cat || true)"

transcript_path="$(printf '%s' "$event" | jq -r '.transcript_path // empty' 2>/dev/null || true)"
session_id="$(printf '%s' "$event" | jq -r '.session_id // empty' 2>/dev/null || true)"
cwd="$(printf '%s' "$event" | jq -r '.cwd // empty' 2>/dev/null || true)"

python3 - "$transcript_path" "$session_id" "$cwd" "$PRINTER_URL" "$FILTER_MODEL" <<'PY' >/dev/null 2>&1 || true
import json, os, sys, urllib.request, datetime, re

transcript, session_id, cwd, url, filter_model = sys.argv[1:6]

title = "Claude session"
results = []
turns = 0
model = None
first_ts = last_ts = None
first_user_text = None
last_assistant_texts = []

if cwd:
    title = f"Claude · {os.path.basename(cwd.rstrip('/')) or cwd}"

if transcript and os.path.exists(transcript):
    for line in open(transcript, encoding="utf-8", errors="replace"):
        try:
            o = json.loads(line)
        except Exception:
            continue
        msg = o.get("message") or {}
        role = msg.get("role")
        ts = o.get("timestamp") or msg.get("created_at")
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        if role == "user" and first_user_text is None:
            content = msg.get("content")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        text += p.get("text", "")
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            if text and not text.startswith("Caveat:") and not text.startswith("/"):
                first_user_text = text
        if role == "assistant":
            turns += 1
            m = msg.get("model")
            if m:
                model = m
            content = msg.get("content")
            if isinstance(content, list):
                txt = "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
            else:
                txt = content if isinstance(content, str) else ""
            txt = re.sub(r"\s+", " ", (txt or "")).strip()
            if txt:
                last_assistant_texts.append(txt)

    if first_user_text:
        title = first_user_text[:120]
    results = [t[:120] for t in last_assistant_texts[-3:]]

duration = None
if first_ts and last_ts:
    try:
        a = datetime.datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
        b = datetime.datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        secs = int((b - a).total_seconds())
        if secs >= 60:
            duration = f"{secs // 60}m {secs % 60}s"
        else:
            duration = f"{secs}s"
    except Exception:
        pass

# ---- Haiku filter: should we print this receipt? ----------------------
def should_print() -> bool:
    """Ask Haiku whether this session is final & valuable enough to print.

    Returns True if printing should proceed. Defaults to True on any
    error, missing API key, or PRINT_FILTER=off — so missing config never
    causes a silent loss of printouts.
    """
    mode = os.environ.get("PRINT_FILTER", "").lower()
    if mode in ("off", "force"):
        return True

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return True  # legacy: print everything when no key configured

    # Cheap local pre-filter — skip obvious noise without burning API cost.
    if turns < 2:
        return False
    if not first_user_text and not last_assistant_texts:
        return False

    sample = {
        "first_user_message": (first_user_text or "")[:600],
        "last_assistant_turns": [t[:400] for t in last_assistant_texts[-3:]],
        "turns": turns,
        "duration": duration,
        "cwd": cwd,
    }

    system = (
        "You are a strict filter that decides whether a Claude Code session "
        "deserves a paper receipt printed at home. Print ONLY when the session "
        "represents a FINAL VALUABLE outcome: real code shipped, a bug "
        "diagnosed and fixed, a meaningful artifact produced, a non-trivial "
        "investigation completed, or a clear deliverable handed back to the user. "
        "DO NOT print: trivial Q&A, aborted/half-done work, exploratory chat, "
        "single-step lookups, configuration tweaks, debugging dead-ends without "
        "resolution, or sessions where the assistant mostly asked clarifying "
        "questions. Reply with EXACTLY one token: PRINT or SKIP. No explanation."
    )

    user = (
        "Decide PRINT or SKIP for this session:\n\n"
        + json.dumps(sample, ensure_ascii=False, indent=2)
    )

    body = json.dumps({
        "model": filter_model,
        "max_tokens": 4,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")

    req = urllib.request.Request(
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
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return True  # network/API error → fall back to printing

    text = ""
    for blk in data.get("content", []):
        if isinstance(blk, dict) and blk.get("type") == "text":
            text += blk.get("text", "")
    verdict = text.strip().upper()
    # Be permissive: only an explicit SKIP suppresses the print.
    return "SKIP" not in verdict.split()

if not should_print():
    sys.exit(0)

payload = {"brand": "CLAUDE", "title": title}
if results: payload["results"] = results
if duration: payload["duration"] = duration
if model: payload["model"] = model[:40]
if turns: payload["turns"] = turns

data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
urllib.request.urlopen(req, timeout=4).read()
PY

exit 0
