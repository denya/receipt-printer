#!/usr/bin/env bash
# Stop hook → POST a session ticket to the home receipt printer
# whenever the assistant has just delivered a real task completion.
#
# Why Stop, not SessionEnd:
# - SessionEnd only fires when the user explicitly ends the session
#   (clear/exit/logout). Closing-the-laptop-mid-task triggers a print;
#   ordinary "I finished a task, let's keep working" does not.
# - Stop fires after every assistant turn, so we can decide PER TURN
#   whether THIS turn is a real completion. The cheap Haiku verifier is
#   the source of truth.
#
# Behavior:
# - Reads the Claude Stop event JSON from stdin.
# - Parses the transcript and pulls the LATEST assistant turn.
# - Local pre-filter: skip without burning API tokens if the latest
#   turn ends with "?" or contains explicit asking/planning phrases
#   ("Should I…", "Want me to…", "Here's the plan", etc.).
# - Dedup by per-session message-hash in a JSON state file — the same
#   completion turn is never reprinted (the Stop hook will fire again
#   if the user types another short ack like "thanks", but Haiku will
#   say SKIP for that turn).
# - Calls Claude Haiku via `claude -p --model claude-haiku-4-5-20251001`
#   for the per-turn decision. Uses the user's existing Claude Code auth
#   (Pro/Max subscription or Console) — no separate ANTHROPIC_API_KEY
#   needed. The verifier subprocess sets RECEIPT_PRINTER_VERIFIER=1 so
#   the recursive Stop hook fires-and-exits without re-running the work.
# - On PRINT: POSTs the ticket and records the dedup state.
# - All failures are silent: a missing/unreachable printer or claude CLI
#   must never block Claude Code (Stop hooks must exit 0).
#
# Optional env vars (graceful fallback when absent):
#   PRINTER_URL                 Override the printer endpoint. Default below.
#   PRINT_FILTER                "off" / "force" — bypass Haiku, print every
#                                turn that passes the local filter + dedup.
#                                (default) — Haiku decides.
#   RECEIPT_PRINTER_VERIFIER    Internal recursion guard. Set to "1" by the
#                                verifier subprocess so the recursive Stop
#                                exits immediately. Do NOT set manually.
#   CLAUDE_BIN                  Override the path to the claude CLI.
#                                Default: auto-detect via PATH then known dirs.

set -e

# Recursion guard: when this hook calls `claude -p` to verify, the
# subprocess will itself emit Stop events that re-trigger this hook.
# The env-var sentinel breaks the loop with a fast exit.
if [ -n "${RECEIPT_PRINTER_VERIFIER:-}" ]; then
  exit 0
fi

PRINTER_URL="${PRINTER_URL:-http://100.78.6.79:9100/print/session}"
STATUS_URL="${STATUS_URL:-${PRINTER_URL%/print/session}/status/update}"
FILTER_MODEL="claude-haiku-4-5-20251001"
STATE_PATH="${HOME}/.claude/hooks/print-stop-state.json"
STATUS_TOKEN_FILE="${STATUS_TOKEN_FILE:-$HOME/.config/receipt-printer/status-api-token}"
STATUS_LOG_PATH="${STATUS_LOG_PATH:-$HOME/.claude/hooks/print-status.log}"

# Find claude CLI — Claude Code may launch hooks with a minimal PATH.
if [ -z "${CLAUDE_BIN:-}" ]; then
  if command -v claude >/dev/null 2>&1; then
    CLAUDE_BIN="$(command -v claude)"
  elif [ -x "$HOME/.local/bin/claude" ]; then
    CLAUDE_BIN="$HOME/.local/bin/claude"
  elif [ -x "/usr/local/bin/claude" ]; then
    CLAUDE_BIN="/usr/local/bin/claude"
  elif [ -x "/opt/homebrew/bin/claude" ]; then
    CLAUDE_BIN="/opt/homebrew/bin/claude"
  fi
fi

event="$(cat || true)"

transcript_path="$(printf '%s' "$event" | jq -r '.transcript_path // empty' 2>/dev/null || true)"
session_id="$(printf '%s' "$event" | jq -r '.session_id // empty' 2>/dev/null || true)"
cwd="$(printf '%s' "$event" | jq -r '.cwd // empty' 2>/dev/null || true)"
stop_hook_active="$(printf '%s' "$event" | jq -r '.stop_hook_active // empty' 2>/dev/null || true)"

python3 - "$transcript_path" "$session_id" "$cwd" "$PRINTER_URL" "$STATUS_URL" "$FILTER_MODEL" "$STATE_PATH" "$stop_hook_active" "${CLAUDE_BIN:-}" "$STATUS_TOKEN_FILE" "$STATUS_LOG_PATH" <<'PY' >/dev/null 2>&1 || true
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request

(
    transcript,
    session_id,
    cwd,
    url,
    status_url,
    filter_model,
    state_path,
    stop_hook_active,
    claude_bin,
    status_token_file,
    status_log_path,
) = sys.argv[1:12]

title = "Claude session"
results = []
turns = 0
model = None
first_ts = last_ts = None
first_user_text = None
last_assistant_texts = []

def clean_report_text(text: str) -> str:
    text = re.sub(r"```[\s\S]*?```", " ", text or "")
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"\[([^\]\n]{1,120})\]\((?:[^)\s]+)(?:\s+\"[^\"]*\")?\)", r"\1", text)
    text = re.sub(r"https?://\S+|www\.\S+", " ", text)
    text = re.sub(r"(?<!\w)/(?:[\w .@+-]+/){2,}([\w .@+-]+\.[A-Za-z0-9]+)(?::\d+)?", r"\1", text)
    text = re.sub(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+", "", text)
    text = re.sub(r"[*_~#>`]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\b(?:at|from|via)\s+(?=(?:without|with|and|for)\b)", "", text)

def complete_clip(text: str, limit: int) -> str:
    text = clean_report_text(text)
    if len(text) <= limit:
        return text
    window = text[:limit + 1]
    floor = max(24, int(limit * 0.55))
    for pattern in (r"[.!?](?=\s|$)", r"[:;](?=\s|$)", r",(?=\s)"):
        matches = [m for m in re.finditer(pattern, window) if m.end() >= floor]
        if matches:
            return window[:matches[-1].end()].strip()
    clipped = window[:limit].rsplit(" ", 1)[0].strip(" ,;:-")
    return clipped + "." if clipped and clipped[-1] not in ".!?" else clipped

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
                first_user_text = complete_clip(text, 72)
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
        title = first_user_text
    results = [complete_clip(t, 190) for t in last_assistant_texts[-5:] if complete_clip(t, 190)]

# Need at least one assistant turn to have something to evaluate.
if not last_assistant_texts:
    sys.exit(0)
last_assistant = last_assistant_texts[-1]

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


def clean_text(text: str) -> str:
    text = clean_report_text(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", (text or "")).strip()


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
    interim_starts = (
        "i'm ", "i am ", "i’ll ", "i will ", "checking ", "inspecting ",
        "reviewing ", "running ", "looking ", "mapping ", "tracing ",
    )
    if lowered.startswith(interim_starts):
        return "running"
    return "unknown"


def derive_summary_line(text: str, status: str) -> str:
    if status == "waiting":
        return "Waiting for your input."
    sentences = split_sentences(text)
    if status == "completed" and len(sentences) >= 2 and re.search(r"test|verified|deployed|waiting|input|blocked|fixed|completed", sentences[1], re.I):
        combined = complete_clip(sentences[0], 110) + " " + complete_clip(sentences[1], 110)
        if len(combined) <= 220:
            return combined
    if sentences:
        return complete_clip(sentences[0], 220)
    return complete_clip(clean_text(text), 220) or "Status update available."


def load_status_token() -> str:
    token = os.environ.get("STATUS_API_TOKEN", "").strip()
    if token:
        return token
    try:
        with open(os.path.expanduser(status_token_file), "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def debug_log(message: str) -> None:
    try:
        os.makedirs(os.path.dirname(os.path.expanduser(status_log_path)), exist_ok=True)
        with open(os.path.expanduser(status_log_path), "a", encoding="utf-8") as f:
            ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
            f.write(f"{ts} {message}\n")
    except Exception:
        pass


def post_status_update() -> None:
    status = classify_status(last_assistant)
    body = {
        "source": "claude",
        "session_key": session_key,
        "turn_key": f"{session_key}:{current_hash}",
        "title": title,
        "summary_line": derive_summary_line(last_assistant, status),
        "status": status,
        "cwd": cwd or None,
        "model": (model or "")[:80] or None,
        "turns": turns or None,
        "duration": duration,
        "updated_at": last_ts,
    }
    body = {k: v for k, v in body.items() if v is not None and v != ""}
    headers = {"Content-Type": "application/json"}
    token = load_status_token()
    if token:
        headers["x-status-token"] = token
    req = urllib.request.Request(
        status_url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    urllib.request.urlopen(req, timeout=4).read()

# ---- Mid-conversation pre-filter (cheap, no API call) -----------------
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
    """The latest assistant turn is asking a question, presenting a plan,
    or otherwise awaiting input. Not a completion."""
    if not text:
        return False
    t = text.strip()
    if t.endswith("?"):
        return True
    tail = t[-400:].lower()
    for pat in MID_CONVERSATION_PATTERNS:
        if pat in tail:
            return True
    return False

mid_conversation = looks_like_mid_conversation(last_assistant)

# ---- Dedup state -------------------------------------------------------
# Stop fires after every assistant turn. If the user keeps the session
# open after a completion, the next short ack ("thanks", "what next?")
# would also Stop — but Haiku will SKIP those, so this is mainly defense
# in depth. The hash key prevents reprinting the IDENTICAL turn (e.g.
# if a hook retry runs).
def msg_hash(text: str) -> str:
    return hashlib.sha256(text[-2000:].encode("utf-8", errors="replace")).hexdigest()

def load_state() -> dict:
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("sessions"), dict):
                return data
    except Exception:
        pass
    return {"sessions": {}}

def save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        sessions = state.get("sessions", {})
        # LRU bound at 100 sessions to keep the file tiny.
        if len(sessions) > 100:
            ordered = sorted(sessions.items(), key=lambda kv: kv[1].get("updated", ""))
            state["sessions"] = dict(ordered[-100:])
        tmp = state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, state_path)
    except Exception:
        pass

current_hash = msg_hash(last_assistant)
state = load_state()
session_key = session_id or "_unknown"
sess_state = state.get("sessions", {}).get(session_key, {})

try:
    post_status_update()
except Exception as exc:
    debug_log(f"status_update_failed session={session_key} error={exc!r}")

if sess_state.get("last_printed_hash") == current_hash:
    # Already printed THIS exact turn — nothing new for the print path.
    sys.exit(0)

# ---- Haiku decision (the reliable verdict) ----------------------------
def haiku_says_print() -> bool:
    if mid_conversation:
        return False
    mode = os.environ.get("PRINT_FILTER", "").lower()
    if mode in ("off", "force"):
        return True

    if not claude_bin or not os.path.exists(claude_bin):
        # No claude CLI found → can't verify → don't print spurious receipts.
        return False

    sample = {
        "first_user_message": (first_user_text or "")[:600],
        "latest_assistant_turn": last_assistant[:1500],
        "previous_assistant_turns": [t[:300] for t in last_assistant_texts[-4:-1]],
        "turns_so_far": turns,
        "duration": duration,
        "cwd": cwd,
    }

    # System + user prompt baked into a single -p argument. claude -p
    # treats it as the user message; we phrase the system rules inline.
    prompt = (
        "You decide whether the assistant's MOST RECENT turn in a Claude "
        "Code session is a real task completion worth printing on a paper "
        "receipt at home. This hook fires after every assistant turn, so "
        "the vast majority of turns are interim and must be SKIPPED.\n\n"
        "Reply PRINT only when the LATEST assistant turn shows the "
        "assistant has DELIVERED a concrete completed outcome and is NOT "
        "awaiting user input on next steps. Strong PRINT signals:\n"
        "- Code committed/pushed (commit hash, PR/branch, file paths)\n"
        "- Bug fix verified (tests pass, behavior confirmed)\n"
        "- Deployment completed (service healthy, container running)\n"
        "- File created/modified with summary of what changed\n"
        "- Investigation/analysis with concrete conclusions delivered\n"
        "- Final wrap-up summary of what was accomplished\n\n"
        "Reply SKIP for everything else, especially:\n"
        "- Asks the user a question (ends with '?', 'Should I…')\n"
        "- Presents a plan and waits for approval ('Here's the plan:')\n"
        "- Requests clarification or offers options to choose between\n"
        "- Interim progress: 'Checking…', 'I'll now…', 'Reading file…'\n"
        "- Partial work, dead-end debugging without a fix\n"
        "- Trivial Q&A or single-step lookup with no real deliverable\n"
        "- Pure exploration / research without a concrete outcome\n\n"
        "PRIORITY RULE: when in doubt, SKIP. A missed receipt is fine; "
        "a spurious receipt is annoying. Only PRINT when the turn clearly "
        "marks a logical task endpoint.\n\n"
        "Reply with EXACTLY one token: PRINT or SKIP. No other text.\n\n"
        "Decide PRINT or SKIP for this turn:\n\n"
        + json.dumps(sample, ensure_ascii=False, indent=2)
    )

    # Spawn `claude -p` with the recursion guard. The subprocess will emit
    # its own Stop event that re-runs THIS hook; the env var makes that
    # recursion exit instantly at the bash guard above.
    env = {**os.environ, "RECEIPT_PRINTER_VERIFIER": "1"}
    try:
        result = subprocess.run(
            [claude_bin, "-p", "--model", filter_model, prompt],
            env=env,
            capture_output=True,
            text=True,
            timeout=18,
        )
    except Exception:
        return False
    if result.returncode != 0:
        return False
    verdict = (result.stdout or "").strip().upper()
    tokens = verdict.split()
    # Strict: explicit PRINT and no SKIP token anywhere in the response.
    return "PRINT" in tokens and "SKIP" not in tokens

if not haiku_says_print():
    sys.exit(0)

# ---- POST receipt + record dedup state -------------------------------
payload = {"title": title}
if results: payload["results"] = results
if duration: payload["duration"] = duration
if model: payload["model"] = model[:40]
if turns: payload["turns"] = turns

data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
try:
    urllib.request.urlopen(req, timeout=4).read()
except Exception:
    # Print failed; don't record dedup so the next Stop can retry.
    sys.exit(0)

# Record dedup AFTER successful POST so retries can recover.
state.setdefault("sessions", {})[session_key] = {
    "last_printed_hash": current_hash,
    "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
save_state(state)
PY

exit 0
