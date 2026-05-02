# receipt-printer

Print Claude Code session receipts on a thermal printer at home.

This repo has two parts:

- `server/` — a FastAPI service that runs on a Raspberry Pi and talks to an Epson TM-T20II over `/dev/usb/lp0`
- `client/print-session.sh` — a Claude Code `SessionEnd` hook that decides whether a session is worth printing, then POSTs a receipt to the server

The current live target is the Raspberry Pi at `100.78.6.79`, where the standalone deployment lives in `/home/denya/receipt-printer-service/`.

## Repo layout

```text
receipt-printer/
├── README.md
├── spec.md
├── client/
│   └── print-session.sh
└── server/
    ├── .env.example
    ├── Dockerfile
    ├── docker-compose.yml
    ├── main.py
    ├── printer.py
    ├── renderer.py
    ├── requirements.txt
    ├── schemas.py
    └── tests/
```

## What the server exposes

- `GET /health` — reports whether the service is ready
- `POST /print/test` — text-only printer smoke test
- `POST /print/text` — print plain text
- `POST /print/session` — print a Claude session receipt
- `POST /print/rich` — print arbitrary rendered blocks

Example:

```bash
curl http://100.78.6.79:9100/health
curl -X POST http://100.78.6.79:9100/print/test
curl -X POST http://100.78.6.79:9100/print/session \
  -H 'Content-Type: application/json' \
  -d '{"brand":"CLAUDE","title":"Shipped fix","results":["Reviewed code","Patched bug"]}'
```

## Local development

Create a virtualenv and install the server dependencies:

```bash
cd server
python3 -m venv ../.venv
. ../.venv/bin/activate
pip install -r requirements.txt
```

Run the tests:

```bash
. ../.venv/bin/activate
python -m unittest discover -s tests -v
```

### Run the API without a real printer

Use dry-run mode. It exercises the full render path but skips writing to `/dev/usb/lp0`.

```bash
cd server
cp .env.example .env
sed -i.bak 's/PRINTER_DRY_RUN=0/PRINTER_DRY_RUN=1/' .env
docker compose up --build
```

Or without Docker:

```bash
cd server
PRINTER_DRY_RUN=1 uvicorn main:app --host 127.0.0.1 --port 9100
```

Then:

```bash
curl http://127.0.0.1:9100/health
curl -X POST http://127.0.0.1:9100/print/session \
  -H 'Content-Type: application/json' \
  -d '{"title":"Dry run","results":["No printer needed"]}'
```

## Raspberry Pi deployment

The standalone service on the Pi is not a git checkout. The deployable files in `server/` are copied into:

```text
/home/denya/receipt-printer-service/
```

### First-time setup on the Pi

```bash
ssh denya@100.78.6.79
mkdir -p /home/denya/receipt-printer-service
cd /home/denya/receipt-printer-service
cp .env.example .env
docker compose up -d --build
```

Recommended `.env`:

```dotenv
PRINTER_DEVICE=/dev/usb/lp0
PRINTER_WIDTH_CHARS=48
PRINTER_DRY_RUN=0
```

### Update the Pi deployment

Copy the standalone server files from this repo to the Pi, preserving `.env`:

```bash
rsync -av \
  --exclude '__pycache__/' \
  --exclude '.env' \
  ./server/ denya@100.78.6.79:/home/denya/receipt-printer-service/
```

Then rebuild and verify:

```bash
ssh denya@100.78.6.79 '
  cd /home/denya/receipt-printer-service &&
  docker compose up -d --build &&
  curl -sf http://127.0.0.1:9100/health
'
```

## Claude Code hook

Install the hook locally:

```bash
mkdir -p ~/.claude/hooks
cp client/print-session.sh ~/.claude/hooks/print-session.sh
chmod +x ~/.claude/hooks/print-session.sh
```

Example `~/.claude/settings.json` snippet:

```json
{
  "hooks": {
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/Users/denya/.claude/hooks/print-session.sh"
          }
        ]
      }
    ]
  }
}
```

Useful env vars for the hook:

- `PRINTER_URL` — defaults to `http://100.78.6.79:9100/print/session`
- `ANTHROPIC_API_KEY` — enables the Haiku print-worthiness filter
- `PRINT_FILTER=off` — bypasses the filter and prints every session

## Codex hook

Codex does not have the same session-end hook surface here. The practical hook point is the global `notify` command that fires after each completed agent turn.

This repo includes [client/print-codex-notify.py](/Users/denya/code/random-vibe-coding/receipt-printer/client/print-codex-notify.py:1), which:

- receives the Codex notify payload
- prints only high-value final-looking turns
- then chains to the existing oh-my-codex notify hook so current notifications keep working

Install it:

```bash
mkdir -p ~/.codex/hooks
cp client/print-codex-notify.py ~/.codex/hooks/print-codex-notify.py
chmod +x ~/.codex/hooks/print-codex-notify.py
cp ~/.codex/config.toml ~/.codex/config.toml.bak-receipt-hook
```

Then change the `notify = [...]` entry in `~/.codex/config.toml` so `--previous-notify` points at:

```json
["/Users/denya/.codex/hooks/print-codex-notify.py"]
```

The wrapper itself forwards to the existing OMX notify hook, so the old behavior is preserved.

## Configurable header brand

`/print/session` now accepts a `brand` field, so the large header can be `CLAUDE`, `CODEX`, `README`, or any other short label.

Example:

```bash
curl -X POST http://100.78.6.79:9100/print/session \
  -H 'Content-Type: application/json' \
  -d '{"brand":"CODEX","title":"Repo shipped","results":["Hook installed","Pi updated"]}'
```

## Current improvements in this repo

- Font loading now falls back cleanly outside the container instead of crashing on hardcoded Linux font paths.
- Rich-block rendering now survives renderer failures and prints an inline error tag instead of failing the whole request.
- Table blocks now expand for wrapped cell content instead of silently truncating after the first line.
- Dry-run mode makes local verification possible without a physical printer.
- Focused unit tests protect the renderer and dry-run service behavior.

See [`spec.md`](./spec.md) for the full system spec and design notes.
