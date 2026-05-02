# Receipt Printer — Agent Skill

A FastAPI microservice that prints to an Epson TM-T20II thermal printer
over the local network. Agents use it to leave a tactile artifact at the
end of a session: a clean, typeset receipt summarizing what just
happened.

This document is the contract for *callers*: what the endpoints accept,
what the rendering pipeline can do, and how to compose a good ticket.

---

## Service location

| Property | Value |
|---|---|
| Host | `100.78.6.79` (Tailscale) — also reachable on the home LAN at the Pi's `10.42.x.x` IP |
| Port | `9100` |
| Base URL | `http://100.78.6.79:9100` |
| Hardware | Epson TM-T20II, 80 mm thermal, ESC/POS, 203 DPI |
| Print width | 576 dots (= 72 mm) |
| Source on the Pi | `/home/denya/receipt-printer-service/` |
| Container | `receipt-printer` (Docker Compose, restart `unless-stopped`) |

`GET /health` returns `{"ok": true, ...}` when the printer device is
reachable. Always health-check before a long-running pipeline.

---

## Endpoints

### `GET /health`
No body. Returns service + device status.

### `POST /print/test`
No body. Prints a small text-mode self-test slip. Useful for verifying
the printer hardware independently of the renderer.

### `POST /print/text`
```json
{ "text": "free-form text\nnewlines preserved", "cut": true }
```
Plain text, CP437 codepage. No fonts needed. Use only for quick debug
notes — the rich renderer is almost always better.

### `POST /print/session`
The "Claude session completed" preset. One template:
```json
{
  "title": "Refactored auth middleware",
  "results": [
    "Extracted JWT validation into its own module",
    "Added 12 unit tests, all passing",
    "Removed 240 lines of dead code"
  ],
  "model": "claude-opus-4-7",
  "turns": 9,
  "duration": "6m 04s",
  "timestamp": "2026-05-02 02:30"   // optional; server fills in if omitted
}
```
Internally renders as a `header` + `title` + `bullets` + `flourish` +
meta line + time line. Use this when the session truly is "task done,
here are the results" and you don't need charts.

### `POST /print/rich`
The composer endpoint. Accepts an ordered list of typed blocks; the
service composites them top-to-bottom into one bitmap, then prints.
```json
{
  "blocks": [
    {"type": "header", "title": "CLAUDE", "subtitle": "ANALYSIS COMPLETE"},
    {"type": "title", "content": "Repo health snapshot"},
    {"type": "text", "content": "Scanned 412 files in 18 directories.", "style": "body"},
    {"type": "bar_chart", "title": "Issues by severity",
     "data": {"high": 3, "med": 14, "low": 27}},
    {"type": "sparkline", "title": "Commits / day (14d)",
     "values": [1,3,2,5,4,7,6,8,4,3,5,9,7,6]},
    {"type": "ornament", "style": "flourish"},
    {"type": "text", "content": "opus-4-7  ·  12 turns  ·  8m", "style": "meta"},
    {"type": "text", "content": "2026-05-02 02:30", "style": "time"}
  ]
}
```
This is the primary surface for an LLM-driven pipeline.

---

## Block catalog

Every block has a `type` field. Unknown types fall back to body text.
Unless noted, blocks are content-width (504 px) and centered in the
576-px print area.

### `header`
```json
{"type": "header", "title": "CLAUDE", "subtitle": "TASK COMPLETED", "logo": true}
```
Logo mark (rosette) above letter-spaced title, dithered fade rule, then
letter-spaced subtitle. `logo` defaults to `true`. Use exactly one
`header` block, at the top.

### `title`
```json
{"type": "title", "content": "What this ticket is about"}
```
Bold, left-aligned, wraps to multiple lines. Use directly after the
header.

### `text`
```json
{"type": "text", "content": "Body paragraph or single line.",
 "style": "body", "align": "left"}
```
`style` ∈ `body | subtitle | meta | time | caption | title`.
Each style picks font + size + line-height + default alignment:

| Style    | Font / size | Default align | Use for |
|----------|-------------|---------------|---------|
| body     | Regular 23  | left          | paragraphs, summaries |
| subtitle | Regular 17  | center        | secondary headers |
| meta     | Regular 21  | center        | compact meta lines |
| time     | Regular 19  | center        | datetime stamps |
| caption  | Regular 18  | center        | tiny labels |
| title    | Bold 27     | left          | when you want a title without a separate `title` block |

`align` overrides the default (`left | center | right`).

### `bullets`
```json
{"type": "bullets", "items": ["First", "Second", "Third"]}
```
Hanging-indent bullet list, regular 23 pt. Continuation lines align
under the text, not the bullet.

### `ornament`
```json
{"type": "ornament", "style": "flourish"}
```
`style` ∈ `flourish | diamonds | wave | fade | hr`.
- `flourish` — center diamond with fading wing dots (dithered).
- `diamonds` — row of evenly spaced diamonds (crisp).
- `wave` — dotted sine wave (crisp).
- `fade` — soft horizontal lozenge that fades in & out (dithered).
- `hr` — single hairline rule.

Use sparingly. One ornament between major sections is plenty; two in a
row is noise.

### `spacer`
```json
{"type": "spacer", "height": 24}
```
Whitespace block, in pixels. Almost always unnecessary because the
compositor already inserts per-type gaps. Reach for this when you need
extra breathing room before/after a chart.

### `bar_chart`
```json
{"type": "bar_chart", "title": "Languages",
 "data": {"React": 45, "Swift": 30, "Python": 25}}
```
Horizontal bars. Label on the left, solid black bar in the middle,
value on the right. Auto-sizes to its longest label. Use for ≤ 8
categories — past that the rows get cramped.

### `sparkline`
```json
{"type": "sparkline", "title": "Commits / day",
 "values": [1,3,2,5,4,7,6,8], "height": 80}
```
Mini line chart with a dithered fill underneath. `min` and `max` are
auto-printed in the top-right corner. 60–120 px tall is the sweet spot.

### `pie_chart`
```json
{"type": "pie_chart", "title": "Time spent",
 "data": {"reading": 40, "editing": 35, "testing": 25}, "height": 200}
```
Dithered pie on the left, legend with swatches + values + percentages
on the right. Best for ≤ 6 slices; smaller slices become illegible at
this resolution.

### `progress_bar`
```json
{"type": "progress_bar", "value": 0.78, "label": "Test coverage"}
```
`value` ∈ `0.0..1.0`. Label on the left, filled bar, percentage on the
right. Stack two or three of these for a mini dashboard.

### `heatmap`
```json
{"type": "heatmap", "title": "Activity",
 "matrix": [[1,2,3],[4,5,6],[7,8,9]],
 "labels_x": ["A","B","C"], "labels_y": ["Mon","Tue","Wed"]}
```
Dithered intensity grid. Higher value → darker cell. Cells auto-size
to fit the print width. Keep matrices ≤ 12 cols for readability.

### `table`
```json
{"type": "table", "title": "Files changed",
 "headers": ["File", "+", "-"],
 "rows": [["app.py", "42", "8"], ["util.py", "12", "0"]]}
```
Bold headers, hairline rules between rows. Cells truncate to one line —
choose short labels.

### `qr_code`
```json
{"type": "qr_code",
 "data": "https://github.com/denya/receipt-printer",
 "label": "github.com/denya/receipt-printer",
 "size": 168}
```
Centered QR code rendered into the bitmap, with an optional caption below.
Use this for repo links, docs, Wi-Fi bootstrap tickets, or any URL you want
to make scannable from paper.

---

## Design guidelines

The aesthetic target is "premium thermal receipt": clean Liberation
Sans typography, generous whitespace, hierarchy through font weight and
size rather than borders.

**Layout**
- Canvas is 576 px wide. Content area is 504 px (36 px margin each side).
- Render at 203 DPI mental model: 8 px ≈ 1 mm.
- Vertical gaps between blocks are auto-computed by type. Don't add
  `spacer` blocks unless you really need extra space.
- One header block, at the top. Always.
- Datetime as the last block (after a `flourish` or `fade` ornament)
  reads as a signature line.

**Typography**
- 6 levels: brand 45 / title 27 / body 23 / meta 21 / time 19 / subtitle 17.
- Caption / chart labels: 18.
- Pillow renders Liberation Sans well at all of these. Don't go below 16.

**Dithering**
- 1-bit output, no grays. The renderer auto-applies Floyd-Steinberg
  dither to gradients (sparkline fill, pie slices, heatmap cells, fade
  ornament). Pure text and line-art use threshold-only (cutoff 200) so
  glyphs stay crisp.
- Don't ask for "pretty gradients" everywhere — dithered regions read
  as texture, and texture next to text fights with the text.

**What to avoid**
- More than one chart per ticket usually = clutter. Pick the most
  informative one.
- Emoji and decorative Unicode glyphs. They mostly render as `?` or
  inconsistent box symbols. Use the `ornament` blocks instead.
- Long URLs, raw stack traces, hashes. The thermal printer eats paper;
  summarize.

---

## Recipes

### Code-review session
A reviewer wraps up: a few high-level findings, severity counts, top
files changed, a commit-frequency sparkline.

```json
{"blocks": [
  {"type": "header", "title": "CLAUDE", "subtitle": "REVIEW COMPLETE"},
  {"type": "title", "content": "auth refactor — review notes"},
  {"type": "text", "content":
     "Generally tight; two correctness concerns flagged. JWT scope check moved earlier in the chain — good. New tests cover the happy path; edge cases for expired tokens still missing.",
   "style": "body"},
  {"type": "bar_chart", "title": "Issues by severity",
   "data": {"high": 1, "med": 4, "low": 9}},
  {"type": "table", "title": "Top files changed",
   "headers": ["File", "+", "-"],
   "rows": [
     ["auth/jwt.py", "82", "31"],
     ["auth/middleware.py", "44", "12"],
     ["tests/test_auth.py", "120", "0"]
   ]},
  {"type": "sparkline", "title": "Commits / day (14d)",
   "values": [2,4,3,1,2,5,7,9,6,4,3,5,4,3]},
  {"type": "ornament", "style": "flourish"},
  {"type": "text", "content": "opus-4-7  ·  14 turns  ·  11m", "style": "meta"},
  {"type": "text", "content": "2026-05-02 02:30", "style": "time"}
]}
```

### Long task with progress dashboard
```json
{"blocks": [
  {"type": "header", "title": "CLAUDE", "subtitle": "BUILD COMPLETE"},
  {"type": "title", "content": "iOS test suite"},
  {"type": "progress_bar", "label": "Coverage", "value": 0.82},
  {"type": "progress_bar", "label": "Pass rate", "value": 0.97},
  {"type": "progress_bar", "label": "Lint clean", "value": 1.0},
  {"type": "ornament", "style": "fade"},
  {"type": "text", "content": "opus-4-7  ·  6 turns  ·  4m", "style": "meta"},
  {"type": "text", "content": "2026-05-02 03:14", "style": "time"}
]}
```

### Quick "task done, no chart"
Use `/print/session` — it's exactly that template.

---

## Intended pipeline

The receipt is the last step of a Claude Code session. The full chain:

1. **Hook**: a Claude Code `Stop` (or session-end) hook fires when the
   user-facing turn ends.
2. **Compaction**: the hook hands the session transcript (or a summary
   of it) to a small fast model — Haiku is the right pick for cost +
   latency.
3. **Block decision**: Haiku reads `SKILL.md` and chooses a block
   sequence that fits *this* session. A doc-only session might be
   `header → title → text(body) → meta → time`; a code-review session
   might be the recipe above; a long debugging session might be a
   `bullets` block of root-cause findings + a `progress_bar` of test
   coverage delta.
4. **POST**: the resulting JSON is sent to `/print/rich`.
5. **Print**: the service composites and rasterizes (~100 ms), then
   pushes the bitmap to the TM-T20II over USB. The printer cuts the
   slip on its own.

Haiku's prompt should include:
- this `SKILL.md` (so it knows the block catalog),
- the session transcript or a compacted summary,
- a brief instruction: "compose a single ticket. ≤ 8 blocks. Pick the
  most informative chart, or skip charts entirely if the session
  doesn't have data worth visualizing."

The service is deliberately stateless so the hook can fire and forget.
If the printer is offline, the request returns a 500 with the error
detail — log it and move on; don't block the user's terminal.
