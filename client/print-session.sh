#!/usr/bin/env bash
# SessionEnd hook → POST a session ticket to the home receipt printer.
#
# Behavior:
# - Reads the Claude SessionEnd event JSON from stdin.
# - Parses the transcript file to extract the first user message,
#   last assistant turns, model, turn count, and duration.
# - Filters out sessions that ended mid-conversation (assistant asking
#   a question, presenting a plan, awaiting confirmation) via a local
#   heuristic on the last assistant turn — these never produce a receipt
#   regardless of how much work happened earlier.
# - For sessions that pass the local filter, asks Claude Haiku
#   (claude-haiku-4-5-20251001) whether the session reached a real
#   completion vs trivial / aborted / exploratory work.
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
end_reason="$(printf '%s' "$event" | jq -r '.reason // empty' 2>/dev/null || true)"

python3 - "$transcript_path" "$session_id" "$cwd" "$PRINTER_URL" "$FILTER_MODEL" "$end_reason" <<'PY' >/dev/null 2>&1 || true
import json, os, sys, urllib.request, datetime, re

transcript, session_id, cwd, url, filter_model, end_reason = sys.argv[1:7]

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

# ---- Mid-conversation detection ---------------------------------------
# Sessions that END in the middle of a conversation (assistant asking a
# question, presenting a plan for approval, requesting clarification) are
# NOT completed tasks and should not produce a receipt.
MID_CONVERSATION_PATTERNS = (
    "should i ", "shall i ", "shall we ", "want me to", "would you like",
    "do you want", "let me know if", "let me know whether", "let me know which",
    "confirm whether", "please confirm", "before i proceed", "before i continue",
    "before i do ", "before making", "here's the plan", "here is the plan",
    "here's my plan", "here is my plan", "here's what i propose",
    "here is what i propose", "which would you prefer", "which do you prefer",
    "ready to proceed", "ok to proceed", "okay to proceed", "shall we proceed",
    "i'll wait for", "awaiting your", "your call", "need me to",
    "should i go ahead", "want me to go ahead", "want me to proceed",
    "if you'd like me to", "if you want me to", "do you want me to",
    "would you rather", "any preference",
)

def looks_like_mid_conversation(text: str) -> bool:
    """Heuristic: does the LAST assistant message indicate the session
    ended mid-task — asking the user a question, presenting a plan, or
    awaiting confirmation? If so, no receipt regardless of earlier work.
    """
    if not text:
        return False
    t = text.strip()
    # Any assistant message that ends with a question mark is asking the
    # user something — not a completion. (False positives on rhetorical
    # closing questions are rare and acceptable.)
    if t.endswith("?"):
        return True
    # Check the trailing chunk for explicit asking phrases.
    tail = t[-400:].lower()
    for pat in MID_CONVERSATION_PATTERNS:
        if pat in tail:
            return True
    return False

# ---- Haiku filter: should we print this receipt? ----------------------
def should_print() -> bool:
    """Decide whether this session is a completed task worth printing.

    Returns True if printing should proceed. Defaults to True on any
    error, missing API key, or PRINT_FILTER=off — so missing config never
    causes a silent loss of printouts.
    """
    mode = os.environ.get("PRINT_FILTER", "").lower()
    if mode in ("off", "force"):
        return True

    # Cheap local pre-filter — skip obvious noise without burning API cost.
    if turns < 2:
        return False
    if not first_user_text and not last_assistant_texts:
        return False

    # Hard skip: last assistant turn is clearly a question or plan-for-
    # approval. This means the session ended mid-conversation, which is
    # never a completion regardless of how much work happened earlier.
    last_assistant = last_assistant_texts[-1] if last_assistant_texts else ""
    if looks_like_mid_conversation(last_assistant):
        return False

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return True  # legacy: print everything when no key configured

    sample = {
        "first_user_message": (first_user_text or "")[:600],
        "last_assistant_turn": last_assistant[:1200],
        "previous_assistant_turns": [t[:300] for t in last_assistant_texts[-4:-1]],
        "turns": turns,
        "duration": duration,
        "cwd": cwd,
        "session_end_reason": end_reason or None,
    }

    system = (
        "You decide whether a Claude Code session deserves a paper receipt "
        "printed at home. Sessions end for many reasons: real completion, but "
        "also user quitting mid-task, asking the assistant a question and "
        "leaving, or pausing to think. Your job: distinguish a FINISHED task "
        "from one that ended MID-CONVERSATION.\n\n"
        "Reply PRINT only when the LAST assistant turn shows the assistant "
        "delivered a concrete completed outcome and is NOT awaiting user "
        "input on next steps. Strong PRINT signals in the last turn:\n"
        "- Code committed/pushed with hash, PR/branch/file paths\n"
        "- Bug fix verified (tests pass, behavior confirmed)\n"
        "- Deployment completed (service healthy, container running)\n"
        "- File created/modified with summary of what changed\n"
        "- Investigation/analysis with concrete conclusions delivered\n"
        "- Final wrap-up summary of what was accomplished this session\n\n"
        "Reply SKIP when the LAST assistant turn shows the session ended "
        "mid-conversation. Strong SKIP signals in the last turn:\n"
        "- Asks the user a question (ends with '?', 'Should I…', 'Want me to…')\n"
        "- Presents a plan and waits for approval ('Here's the plan:', 'Ready?')\n"
        "- Requests clarification ('What did you mean…', 'Could you confirm…')\n"
        "- Offers options for the user to choose between\n"
        "- Aborted/partial work, dead-end debugging without a fix\n"
        "- Trivial Q&A or single-step lookup\n"
        "- Pure exploration with no concrete deliverable\n\n"
        "PRIORITY RULE: if the LAST assistant turn is asking, planning, "
        "or awaiting input — reply SKIP, even if earlier turns shipped real "
        "work. The session is not over for the user yet.\n\n"
        "Reply with EXACTLY one token: PRINT or SKIP."
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
