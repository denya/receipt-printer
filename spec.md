# Receipt Printer — Project Spec

A small two-part system that turns Claude Code sessions into physical
paper receipts on a thermal printer at home. One side is a FastAPI
microservice running in Docker on a Raspberry Pi attached to an Epson
TM-T20II thermal printer. The other side is a Claude Code SessionEnd
hook that watches for completed sessions, asks Claude Haiku whether the
session is worth printing, and POSTs a ticket to the service.

The point: leave a tactile artifact at the end of meaningful work. Not
every session — only the ones that actually shipped something.

---

## Architecture

```
┌─────────────────────────┐         ┌──────────────────────────────┐
│ Mac (Claude Code)       │         │ Raspberry Pi (Tailscale)     │
│                         │         │                              │
│ ~/.claude/hooks/        │  HTTPS  │ Docker: receipt-printer      │
│   print-session.sh      │  ────▶  │   FastAPI on :9100           │
│         │               │  POST   │     │                        │
│         │               │  /print │     ▼                        │
│         ▼               │ /session│  python-escpos               │
│   Haiku API filter      │         │     │                        │
│ (api.anthropic.com)     │         │     ▼                        │
└─────────────────────────┘         │  /dev/usb/lp0 (kernel usblp) │
                                    │     │                        │
                                    │     ▼                        │
                                    │  Epson TM-T20II              │
                                    │  80mm thermal · 203 DPI      │
                                    └──────────────────────────────┘
```

Two top-level dirs:

```
receipt-printer/
├── server/              # FastAPI service that runs on the Pi
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── main.py          # FastAPI app + endpoints
│   ├── printer.py       # python-escpos wrapper, async + locked
│   ├── renderer.py      # Pillow → 576px-wide bitmap composer
│   ├── schemas.py       # pydantic request models
│   ├── requirements.txt
│   └── SKILL.md         # Agent-facing contract for callers
│
└── client/
    └── print-session.sh # Claude Code SessionEnd hook
```

---

## Hardware

| Item | Value |
|---|---|
| Printer | Epson TM-T20II, 80 mm thermal, ESC/POS, 203 DPI |
| Print width | 576 dots (= 72 mm) |
| Connection | USB → Raspberry Pi |
| Host | Raspberry Pi (Tailscale IP `100.78.6.79`, also LAN `10.42.x.x`) |
| Device node | `/dev/usb/lp0` (kernel `usblp` driver, mode `660 root:lp`) |

The kernel's `usblp` driver owns the USB endpoint. The service writes
plain bytes to `/dev/usb/lp0` (escpos `File` backend) rather than
fighting libusb for the interface — much more reliable on this device.

---

## Server

### Endpoints

`http://100.78.6.79:9100/`

| Method | Path | Body | Purpose |
|---|---|---|---|
| GET  | `/health`        | —                  | Service + device readiness |
| POST | `/print/test`    | —                  | Text-mode self-test slip (no fonts) |
| POST | `/print/text`    | `TextRequest`      | Free-form text, CP437, no rendering |
| POST | `/print/session` | `SessionTicket`    | Preset "Claude session" receipt |
| POST | `/print/rich`    | `RichRequest`      | Composer: ordered list of typed blocks |

### Request schemas (pydantic)

```python
class SessionTicket(BaseModel):
    brand:     str = "CLAUDE"            # header label, e.g. CLAUDE / CODEX / README
    title:     str                        # 1..200 chars, required
    results:   list[str] = []             # up to 20 bullets, each shown verbatim
    duration:  str | None = None          # e.g. "6m 04s"
    model:     str | None = None          # e.g. "claude-opus-4-7"
    turns:     int | None = None          # 0..99999
    timestamp: str | None = None          # server fills in if omitted

class TextRequest(BaseModel):
    text: str                             # 1..8000 chars, newlines preserved
    cut:  bool = True

class RichRequest(BaseModel):
    blocks: list[dict]                    # 1..80 blocks, see SKILL.md catalog
```

### Response shape

All print endpoints return `{"ok": true, "kind": "...", ...}` on success
and HTTP 500 with `{"detail": "..."}` on failure. `/health` returns
`{"ok": true|false, "device": "...", "device_mode": "0660", "width_chars": 48}`.

### Rendering pipeline

`/print/test` and `/print/text` stay in text-mode (CP437 codepage) — no
fonts required, useful as a renderer-independent smoke test.

`/print/session` and `/print/rich` go through `renderer.py`:

1. Compose a Pillow image at 576px width using Liberation
   (Helvetica/Arial-ish) with DejaVu fallback.
2. Hand the bitmap to `printer.image(impl="bitImageRaster",
   high_density_horizontal=True, high_density_vertical=True)` —
   ESC/POS GS v 0 raster mode, the most reliable mode on this printer
   for tall images.
3. Cut.

Concurrent calls are serialized through an `asyncio.Lock`; sync escpos
calls run in `asyncio.to_thread()` so the FastAPI loop never blocks. The
device file is reopened per job (cheap, avoids stale fd state).

### Block catalog (`/print/rich`)

Blocks are validated permissively as plain dicts; the renderer dispatches
on `type`. Unknown types fall back to body text. Catalogued in `SKILL.md`:
`header`, `title`, `text`, `bullets`, `bar_chart`, `sparkline`, `pie_chart`,
`progress_bar`, `heatmap`, `table`, `qr_code`, etc.

### Container

`docker-compose.yml`:

- Builds from local Dockerfile, image `receipt-printer:latest`.
- `restart: unless-stopped`.
- Bind-mounts `/dev/usb/lp0` from host into the container.
- Publishes `0.0.0.0:9100:9100` so devices on the LAN and Tailnet can hit it.
- Env: `PRINTER_DEVICE=/dev/usb/lp0`, `PRINTER_WIDTH_CHARS=48`,
  `PRINTER_DRY_RUN=0`.
- Healthcheck: `urllib.request.urlopen('http://127.0.0.1:9100/health')`,
  every 30s, 5s timeout, 3 retries, 15s start period.

`Dockerfile`:

- `python:3.12-slim` base.
- apt: `libusb-1.0-0` (kept in case we ever switch to the Usb backend),
  `ca-certificates`, `fonts-liberation`, `fonts-dejavu-core`.
- pip: `requirements.txt` (FastAPI 0.115.6, uvicorn 0.32.1,
  python-escpos 3.1, pydantic 2.10.3, qrcode 8.2).
- Runs as root (needed to open the `660 root:lp` device node).
- `CMD uvicorn main:app --host 0.0.0.0 --port 9100`.

### Deploy on a fresh Pi

```bash
# Prereqs (on the Pi):
sudo apt-get install -y docker.io docker-compose-plugin
sudo usermod -aG docker $USER  # then log out/in

# Drop the printer in:
lsusb                          # confirm Epson detected
ls -l /dev/usb/lp0             # should be 660 root:lp

# Pull this repo to the Pi:
git clone git@github.com:denya/receipt-printer.git
cd receipt-printer/server
docker compose up -d --build

# Smoke test from another machine:
curl http://<pi-ip>:9100/health
curl -X POST http://<pi-ip>:9100/print/test
```

---

## Client (Claude Code hook)

`client/print-session.sh` is wired into Claude Code as a SessionEnd hook
in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionEnd": [
      {
        "hooks": [
          { "type": "command", "command": "/Users/denya/.claude/hooks/print-session.sh" }
        ]
      }
    ]
  }
}
```

### What it does

1. Reads the SessionEnd event JSON from stdin (`transcript_path`,
   `session_id`, `cwd`).
2. Walks the transcript JSONL to extract:
   - First non-trivial user message (skips `Caveat:` / slash-commands)
   - Last 3 assistant text turns
   - Model name, turn count
   - Wall-clock duration (first ts → last ts)
3. **Haiku filter** — calls `claude-haiku-4-5-20251001` with a strict
   system prompt: "PRINT or SKIP. Print only FINAL VALUABLE outcomes."
   Sends a small JSON sample (first user message, last 3 assistant
   turns, turn count, duration, cwd). max_tokens=4. Replies are parsed
   for `SKIP`; anything else means print.
4. If the verdict is PRINT: POST a `SessionTicket` to
   `http://100.78.6.79:9100/print/session`.
5. **Failures are silent.** A missing key, network error, unreachable
   printer, or malformed transcript must never block Claude Code.

### Pre-filter (no API cost)

Before calling Haiku, the hook drops obvious noise:
- `turns < 2` → skip
- empty transcript / no user text and no assistant text → skip

### Env vars (all optional)

| Var | Default | Effect |
|---|---|---|
| `ANTHROPIC_API_KEY`  | — | Enables the Haiku filter. **Without it, every session prints (legacy mode).** |
| `PRINTER_URL`        | `http://100.78.6.79:9100/print/session` | Override endpoint |
| `PRINT_FILTER`       | (filter on) | `off` or `force` → bypass filter, always print |

### Failure modes & guarantees

- API failure (timeout, 4xx/5xx) → fall back to printing.
- Printer unreachable → exception swallowed, exit 0.
- Bad transcript path → exception swallowed, exit 0.
- The hook is wrapped in `python3 - <<'PY' >/dev/null 2>&1 || true`
  followed by `exit 0` — Claude never sees an error.

### Install

```bash
mkdir -p ~/.claude/hooks
cp client/print-session.sh ~/.claude/hooks/print-session.sh
chmod +x ~/.claude/hooks/print-session.sh

# Add the SessionEnd hook entry to ~/.claude/settings.json (see above).
# Set the API key in your shell profile:
echo 'export ANTHROPIC_API_KEY=sk-ant-...' >> ~/.zshrc
```

---

## Recreate from scratch (full build order)

If this repo were lost, the rebuild is:

1. **Pi side**
   - Install Docker on a Raspberry Pi.
   - Plug in the Epson TM-T20II via USB; confirm `/dev/usb/lp0` appears.
   - Build the FastAPI service per `server/`:
     - 4 endpoints (`/health`, `/print/test`, `/print/text`, `/print/session`, `/print/rich`).
     - python-escpos `File` backend, async lock, reopen-per-job.
     - Pillow renderer at 576px wide, Liberation + DejaVu fonts.
   - `docker compose up -d --build`.
   - Verify `curl http://localhost:9100/health` returns `{"ok": true}`.
   - Verify `curl -X POST http://localhost:9100/print/test` produces a slip.

2. **Tailscale**
   - Join the Pi to the Tailnet so `100.78.6.79:9100` is reachable from the laptop.

3. **Mac side**
   - Drop `client/print-session.sh` into `~/.claude/hooks/`, `chmod +x`.
   - Wire it into `~/.claude/settings.json` as a SessionEnd hook.
   - Export `ANTHROPIC_API_KEY` in shell profile.
   - Smoke-test by ending a Claude Code session that *did* something
     real — a receipt should appear at home within a couple of seconds.

---

## Design notes / "why this way"

- **Two endpoints, one rendering path.** `/print/session` is a frozen
  preset for the common case ("Claude finished a task"). `/print/rich`
  is the escape hatch when an agent wants charts or custom layout.
  Same Pillow → bitImageRaster pipeline underneath.

- **escpos `File` backend, not `Usb`.** The `usblp` kernel driver
  already owns the USB interface. Trying to claim it via libusb is
  flaky on this printer; plain file writes are 100% reliable.

- **Async lock + reopen-per-job.** The printer is a serial device:
  concurrent writes corrupt output. The lock serializes; reopening
  per-job avoids stale state and fd leaks.

- **Haiku filter, not Opus/Sonnet.** Cost matters for a hook that
  fires after every session. Haiku at max_tokens=4 is ~free.

- **Filter defaults to PRINT on failure.** If the API key is missing
  or the call errors, we print anyway — losing tickets silently is
  worse than printing the occasional dud.

- **Hook is silent on every failure path.** A broken printer must
  never break the user's terminal session.
