# receipt-printer

Print Claude Code session receipts on a thermal printer at home.

- **`server/`** — FastAPI microservice (Docker, runs on a Raspberry Pi)
  that drives an Epson TM-T20II thermal printer.
- **`client/print-session.sh`** — Claude Code SessionEnd hook that
  asks Claude Haiku whether the session is worth printing, then POSTs
  a ticket to the server.

See [`spec.md`](./spec.md) for the full system spec, recreate-from-scratch
build order, and design notes.
