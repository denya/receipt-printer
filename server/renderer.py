"""Rasterized ticket renderer using Pillow.

Two entry points:
- render_session(...)  — preset for "task completed" tickets.
- render_blocks([...]) — freeform composer; mix headers, text,
                          ornaments, charts, tables.

Visualization primitives are also exposed individually:
  render_bar_chart, render_sparkline, render_pie_chart,
  render_progress_bar, render_heatmap, render_table.

Output is a 1-bit PIL.Image at the TM-T20II's print width
(576 dots = 72 mm at 203 DPI).

Dithering strategy:
- Pure text/line content     → threshold without dither (cutoff 200).
                                 Crisp glyph edges.
- Gradients, intensity grids → Floyd-Steinberg dither in "L" → "1".
                                 Soft visual transitions on a 1-bit
                                 device.
"""
from __future__ import annotations

import datetime
import math
import os
import re
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont
import qrcode


# ---------- canvas geometry ----------

CANVAS_WIDTH = 576
PAD_X = 12
CONTENT_W = CANVAS_WIDTH - 2 * PAD_X

FONT_REGULAR = (
    os.environ.get("RECEIPT_FONT_REGULAR"),
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "LiberationSans-Regular.ttf",
    "DejaVuSans.ttf",
    "Arial.ttf",
)
FONT_BOLD = (
    os.environ.get("RECEIPT_FONT_BOLD"),
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "LiberationSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
    "Arial Bold.ttf",
)
FONT_ITALIC = (
    os.environ.get("RECEIPT_FONT_ITALIC"),
    "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
    "/Library/Fonts/Arial Italic.ttf",
    "/System/Library/Fonts/Supplemental/Arial Italic.ttf",
    "LiberationSans-Italic.ttf",
    "DejaVuSans-Oblique.ttf",
    "Arial Italic.ttf",
)
FONT_BOLD_ITALIC = (
    os.environ.get("RECEIPT_FONT_BOLD_ITALIC"),
    "/usr/share/fonts/truetype/liberation/LiberationSans-BoldItalic.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf",
    "/Library/Fonts/Arial Bold Italic.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf",
    "LiberationSans-BoldItalic.ttf",
    "DejaVuSans-BoldOblique.ttf",
    "Arial Bold Italic.ttf",
)
FONT_MONO = (
    os.environ.get("RECEIPT_FONT_MONO"),
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/System/Library/Fonts/Supplemental/Courier New.ttf",
    "LiberationMono-Regular.ttf",
    "DejaVuSansMono.ttf",
    "Courier New.ttf",
)

# Session receipts are intentionally compact: no logo, no ornamental footer,
# and only enough margin to keep cutter noise away from text.
FS_BRAND    = 30
FS_SUBTITLE = 17
FS_TITLE    = 25
FS_BODY     = 23
FS_META     = 21
FS_TIME     = 19
FS_CAPTION  = 17


# ---------- helpers ----------

_FONT_CACHE: Dict[Tuple[Tuple[str, ...], int], ImageFont.ImageFont] = {}


def _font(candidates: Sequence[Optional[str]], size: int) -> ImageFont.ImageFont:
    key = (tuple(c for c in candidates if c), size)
    if key not in _FONT_CACHE:
        for candidate in key[0]:
            try:
                _FONT_CACHE[key] = ImageFont.truetype(candidate, size=size)
                break
            except OSError:
                continue
        else:
            _FONT_CACHE[key] = ImageFont.load_default()
    return _FONT_CACHE[key]


def _measure(draw: ImageDraw.ImageDraw, text: str,
             font: ImageFont.ImageFont) -> Tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _wrap(draw: ImageDraw.ImageDraw, text: str,
          font: ImageFont.ImageFont, max_width: int) -> List[str]:
    out: List[str] = []
    for paragraph in (text or "").split("\n"):
        if not paragraph:
            out.append("")
            continue
        line = ""
        for word in paragraph.split(" "):
            cand = (line + " " + word) if line else word
            if _measure(draw, cand, font)[0] <= max_width:
                line = cand
                continue
            if line:
                out.append(line)
            if _measure(draw, word, font)[0] > max_width:
                cur = ""
                for ch in word:
                    if _measure(draw, cur + ch, font)[0] > max_width and cur:
                        out.append(cur)
                        cur = ch
                    else:
                        cur += ch
                line = cur
            else:
                line = word
        if line:
            out.append(line)
    return out


def _to_bw_text(img_l: Image.Image) -> Image.Image:
    """Crisp threshold — for pure text and line-art."""
    return img_l.point(lambda v: 0 if v < 200 else 255).convert(
        "1", dither=Image.Dither.NONE)


def _to_bw_fs(img_l: Image.Image) -> Image.Image:
    """Floyd-Steinberg — for gradients and patterned fills."""
    return img_l.convert("1", dither=Image.Dither.FLOYDSTEINBERG)


# ---------- inline markdown ----------
#
# Lightweight CommonMark-ish parser that recognises:
#   ***bi***  ___bi___    bold + italic
#   **b**     __b__       bold
#   *i*       _i_         italic   (`_` requires word-boundary so
#                                    snake_case stays plain text)
#   `code`                inline mono
#   ~~s~~                 strike-through
#
# Output: list of (text, style_set) runs. Styles are a frozenset of
# {"b","i","code","s"}.  Used by every text-emitting block so that
# "**done**" prints as bold "done", not literal asterisks.

_StyleSet = FrozenSet[str]
_Run = Tuple[str, _StyleSet]

# Patterns ordered longest-delim-first; the recursive splitter picks
# the earliest match across all of them.  Each pattern requires the
# inner content to start and end with a non-space char so stray "*"
# in arithmetic ("5 * 4 * 3") doesn't accidentally italicise.
_INLINE_PATTERNS: Tuple[Tuple[re.Pattern[str], _StyleSet], ...] = (
    (re.compile(r"\*\*\*(?=\S)([\s\S]+?)(?<=\S)\*\*\*"), frozenset({"b", "i"})),
    (re.compile(r"___(?=\S)([\s\S]+?)(?<=\S)___"),       frozenset({"b", "i"})),
    (re.compile(r"\*\*(?=\S)([\s\S]+?)(?<=\S)\*\*"),     frozenset({"b"})),
    (re.compile(r"(?<![A-Za-z0-9])__(?=\S)([^\n]+?)(?<=\S)__(?![A-Za-z0-9])"),
     frozenset({"b"})),
    (re.compile(r"(?<![A-Za-z0-9])_(?=\S)([^\n_]+?)(?<=\S)_(?![A-Za-z0-9])"),
     frozenset({"i"})),
    (re.compile(r"\*(?=\S)([^\n*]+?)(?<=\S)\*"),         frozenset({"i"})),
    (re.compile(r"`([^`\n]+)`"),                          frozenset({"code"})),
    (re.compile(r"~~(?=\S)([\s\S]+?)(?<=\S)~~"),         frozenset({"s"})),
)


def _parse_inline(text: str) -> List[_Run]:
    """Split ``text`` into a flat list of (substring, styles) runs."""
    if not text:
        return []

    def split(s: str, current: _StyleSet) -> List[_Run]:
        earliest: Optional[Tuple[re.Match[str], _StyleSet]] = None
        for pattern, styles in _INLINE_PATTERNS:
            m = pattern.search(s)
            if m and (earliest is None or m.start() < earliest[0].start()):
                earliest = (m, styles)
        if earliest is None:
            return [(s, current)] if s else []
        m, styles = earliest
        out: List[_Run] = []
        if m.start() > 0:
            out.append((s[: m.start()], current))
        # Inside a code span we don't recurse — backticks lock content
        # to its literal form so `**foo**` inside code stays as text.
        if "code" in styles:
            out.append((m.group(1), current | styles))
        else:
            out.extend(split(m.group(1), current | styles))
        if m.end() < len(s):
            out.extend(split(s[m.end():], current))
        return out

    runs = split(text, frozenset())
    # Coalesce neighbours that share a style — keeps draw calls tidy.
    merged: List[_Run] = []
    for run in runs:
        if merged and merged[-1][1] == run[1]:
            merged[-1] = (merged[-1][0] + run[0], run[1])
        else:
            merged.append(run)
    return merged


# ---------- styled-line layout ----------

# (kind, text, styles); kind ∈ {"word","space","newline"}.
_Token = Tuple[str, str, _StyleSet]


def _tokenize_runs(runs: Iterable[_Run]) -> List[_Token]:
    out: List[_Token] = []
    for text, styles in runs:
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == "\n":
                out.append(("newline", "\n", styles))
                i += 1
                continue
            if ch.isspace():
                j = i
                while j < len(text) and text[j].isspace() and text[j] != "\n":
                    j += 1
                out.append(("space", text[i:j], styles))
                i = j
                continue
            j = i
            while j < len(text) and not text[j].isspace():
                j += 1
            out.append(("word", text[i:j], styles))
            i = j
    return out


_FontFor = Any  # callable: styles -> ImageFont


def _font_for_inline(base_regular: Sequence[Optional[str]],
                     base_bold: Sequence[Optional[str]],
                     size: int) -> _FontFor:
    """Return a function mapping a style-set to a loaded font."""

    def lookup(styles: _StyleSet) -> ImageFont.ImageFont:
        if "code" in styles:
            return _font(FONT_MONO, size)
        if "b" in styles and "i" in styles:
            # Fall back to plain bold if a bold-italic face is missing.
            try:
                return _font(FONT_BOLD_ITALIC, size)
            except Exception:  # noqa: BLE001
                return _font(base_bold, size)
        if "b" in styles:
            return _font(base_bold, size)
        if "i" in styles:
            try:
                return _font(FONT_ITALIC, size)
            except Exception:  # noqa: BLE001
                return _font(base_regular, size)
        return _font(base_regular, size)

    return lookup


def _wrap_styled(draw: ImageDraw.ImageDraw,
                 tokens: Sequence[_Token],
                 font_for: _FontFor,
                 max_width: int) -> List[List[_Run]]:
    """Greedy line-wrap a stream of styled tokens.

    Returns a list of lines; each line is a list of (text, styles)
    runs ready for ``_draw_styled_line`` to paint.
    """
    lines: List[List[_Run]] = [[]]
    cur_w = 0
    pending_space: Optional[Tuple[str, _StyleSet, int]] = None

    def push_run(text: str, styles: _StyleSet) -> None:
        line = lines[-1]
        if line and line[-1][1] == styles:
            line[-1] = (line[-1][0] + text, styles)
        else:
            line.append((text, styles))

    for kind, text, styles in tokens:
        if kind == "newline":
            lines.append([])
            cur_w = 0
            pending_space = None
            continue
        if kind == "space":
            if cur_w > 0:
                pending_space = (text, styles,
                                 _measure(draw, text, font_for(styles))[0])
            continue
        # word
        font = font_for(styles)
        ww = _measure(draw, text, font)[0]
        sp_w = pending_space[2] if pending_space else 0
        if cur_w > 0 and cur_w + sp_w + ww > max_width:
            lines.append([])
            cur_w = 0
            pending_space = None
        elif pending_space is not None:
            sp_text, sp_styles, _sp_w = pending_space
            push_run(sp_text, sp_styles)
            cur_w += sp_w
            pending_space = None
        if ww <= max_width:
            push_run(text, styles)
            cur_w += ww
            continue
        # Single word longer than the line — break by character.
        cur_chars = ""
        for ch in text:
            test = cur_chars + ch
            tw = _measure(draw, test, font)[0]
            if tw > max_width and cur_chars:
                push_run(cur_chars, styles)
                lines.append([])
                cur_chars = ch
                cur_w = 0
            else:
                cur_chars = test
        if cur_chars:
            push_run(cur_chars, styles)
            cur_w = _measure(draw, cur_chars, font)[0]
    # Strip trailing empty line if the source ended with "\n".
    if len(lines) > 1 and not lines[-1]:
        lines.pop()
    return lines


def _line_metrics(draw: ImageDraw.ImageDraw,
                  font_for: _FontFor,
                  size_hint: int) -> int:
    """Pick a uniform line height so mixed-weight runs sit on the
    same baseline."""
    sample = font_for(frozenset({"b"}))
    return int(_measure(draw, "Mg", sample)[1] * 1.30) if sample else size_hint


def _line_width(draw: ImageDraw.ImageDraw,
                line: Sequence[_Run],
                font_for: _FontFor) -> int:
    return sum(_measure(draw, t, font_for(s))[0] for t, s in line)


def _draw_styled_line(draw: ImageDraw.ImageDraw,
                      x: int, y: int,
                      line: Sequence[_Run],
                      font_for: _FontFor) -> int:
    """Paint a wrapped line of styled runs. Returns total width drawn."""
    cursor = x
    for text, styles in line:
        font = font_for(styles)
        draw.text((cursor, y), text, font=font, fill=0)
        w, h = _measure(draw, text, font)
        if "s" in styles and w > 0:
            mid = y + h // 2
            draw.line((cursor, mid, cursor + w - 1, mid), fill=0, width=1)
        cursor += w
    return cursor - x


def _format_num(v: Any) -> str:
    try:
        if isinstance(v, bool):
            return str(v)
        f = float(v)
        return str(int(f)) if f.is_integer() else f"{f:.1f}"
    except (TypeError, ValueError):
        return str(v)


# ============================================================
# ORNAMENTS
# ============================================================

def render_ornament(style: str = "flourish",
                    width: int = CONTENT_W) -> Image.Image:
    """Public: return a 1-bit ornament strip of the requested style."""
    return _ORN.get((style or "flourish").lower(), _orn_flourish)(width)


def _orn_hr(width: int) -> Image.Image:
    img = Image.new("L", (width, 5), 255)
    ImageDraw.Draw(img).rectangle((0, 2, width - 1, 2), fill=0)
    return _to_bw_text(img)


def _orn_fade(width: int) -> Image.Image:
    """Soft horizontal lozenge — fades both axes; FS-dithered."""
    height = 12
    img = Image.new("L", (width, height), 255)
    cx = (width - 1) / 2
    cy = (height - 1) / 2
    for x in range(width):
        h = max(0.0, 1.0 - (abs(x - cx) / cx) ** 0.85) if cx > 0 else 0
        for y in range(height):
            v = max(0.0, 1.0 - (abs(y - cy) / cy) ** 1.3) if cy > 0 else 0
            d = h * v
            img.putpixel((x, y), int(255 - d * 230))
    return _to_bw_fs(img)


def _orn_flourish(width: int) -> Image.Image:
    """Center diamond + fading wing dots on each side."""
    height = 18
    img = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(img)
    cy = height // 2
    cx = width // 2
    s = 6
    draw.polygon(
        [(cx, cy - s), (cx + s, cy), (cx, cy + s), (cx - s, cy)],
        fill=0,
    )
    gap = s + 8
    max_off = cx - gap
    if max_off > 0:
        for off in range(gap, cx, 4):
            d = (off - gap) / max_off
            density = max(0.0, 1.0 - d ** 0.7)
            v = int(255 - density * 230)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    for sx in (cx - off, cx + off):
                        x = sx + dx
                        y = cy + dy
                        if 0 <= x < width and 0 <= y < height:
                            img.putpixel((x, y), min(img.getpixel((x, y)), v))
    return _to_bw_fs(img)


def _orn_diamonds(width: int, count: int = 7) -> Image.Image:
    s = 5
    height = s * 2 + 6
    img = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(img)
    spacing = width / (count + 1)
    cy = height // 2
    for i in range(1, count + 1):
        cx = int(i * spacing)
        draw.polygon(
            [(cx, cy - s), (cx + s, cy), (cx, cy + s), (cx - s, cy)],
            fill=0,
        )
    return _to_bw_text(img)


def _orn_wave(width: int) -> Image.Image:
    height = 14
    img = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(img)
    amp = (height - 4) / 2
    cy = (height - 1) / 2
    for x in range(0, width, 4):
        y = cy + amp * math.sin(x / width * 6 * math.pi)
        draw.ellipse((x - 1, y - 1, x + 2, y + 2), fill=0)
    return _to_bw_text(img)


_ORN = {
    "flourish": _orn_flourish,
    "diamonds": _orn_diamonds,
    "wave": _orn_wave,
    "fade": _orn_fade,
    "hr": _orn_hr,
}


def _logo_mark(size: int = 56) -> Image.Image:
    """Abstract geometric mark: ringed asterisk-rosette."""
    img = Image.new("L", (size, size), 255)
    draw = ImageDraw.Draw(img)
    cx = cy = size / 2
    r_outer = size * 0.46
    r_petal = size * 0.30
    draw.ellipse((cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer),
                 outline=0, width=2)
    petal_s = max(3, int(size * 0.085))
    for angle_deg in range(0, 360, 60):
        a = math.radians(angle_deg)
        x = cx + r_petal * math.cos(a)
        y = cy + r_petal * math.sin(a)
        draw.polygon(
            [(x, y - petal_s), (x + petal_s, y),
             (x, y + petal_s), (x - petal_s, y)],
            fill=0,
        )
    cd = max(2, int(size * 0.06))
    draw.ellipse((cx - cd, cy - cd, cx + cd, cy + cd), fill=0)
    return _to_bw_text(img)


# ============================================================
# VISUALIZATIONS
# ============================================================

def render_bar_chart(data: Dict[str, float],
                     width: int = CONTENT_W,
                     title: Optional[str] = None) -> Image.Image:
    if not data:
        return Image.new("1", (width, 1), 1)
    f_lab = _font(FONT_REGULAR, FS_CAPTION)
    f_title = _font(FONT_BOLD, FS_BODY)
    items = list(data.items())
    row_h = 32
    title_h = (FS_BODY + 10) if title else 0
    height = title_h + len(items) * row_h + 8

    img = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(img)
    y = 0
    if title:
        draw.text((0, 0), title, font=f_title, fill=0)
        y = title_h

    label_w = max(_measure(draw, str(k), f_lab)[0] for k, _ in items)
    val_strs = [_format_num(v) for _, v in items]
    val_w = max(_measure(draw, s, f_lab)[0] for s in val_strs)
    bar_x = label_w + 16
    bar_x_end = width - val_w - 12
    bar_w_max = max(1, bar_x_end - bar_x)
    max_v = max(abs(float(v)) for _, v in items) or 1.0

    for (label, value), val_str in zip(items, val_strs):
        _, lh = _measure(draw, str(label), f_lab)
        draw.text((0, y + (row_h - lh) // 2 - 2),
                  str(label), font=f_lab, fill=0)
        bw = int(bar_w_max * (abs(float(value)) / max_v))
        bar_y = y + 8
        bar_h = row_h - 16
        # Outline so 0-value rows still show a track.
        draw.rectangle((bar_x, bar_y, bar_x_end, bar_y + bar_h),
                       outline=0, width=1)
        if bw > 0:
            draw.rectangle((bar_x, bar_y, bar_x + bw, bar_y + bar_h), fill=0)
        vw, vh = _measure(draw, val_str, f_lab)
        draw.text((width - vw, y + (row_h - vh) // 2 - 2),
                  val_str, font=f_lab, fill=0)
        y += row_h
    return _to_bw_text(img)


def render_sparkline(values: Sequence[float],
                     width: int = CONTENT_W,
                     height: int = 80,
                     title: Optional[str] = None) -> Image.Image:
    if not values:
        return Image.new("1", (width, 1), 1)
    f_title = _font(FONT_BOLD, FS_BODY)
    f_cap = _font(FONT_REGULAR, FS_CAPTION)
    title_h = (FS_BODY + 8) if title else 0
    plot_y0 = title_h + 4
    plot_h = max(20, height - title_h - 8)
    pad = 6

    img = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(img)
    if title:
        draw.text((0, 0), title, font=f_title, fill=0)

    vmin, vmax = min(values), max(values)
    span = (vmax - vmin) or 1.0
    n = len(values)
    pts: List[Tuple[float, float]] = []
    for i, v in enumerate(values):
        x = pad + (width - 2 * pad - 1) * (i / (n - 1) if n > 1 else 0.5)
        y = plot_y0 + (plot_h - 1) * (1 - (v - vmin) / span)
        pts.append((x, y))

    # Filled area under the line — light gray, dithers cleanly.
    poly = list(pts) + [(pts[-1][0], plot_y0 + plot_h - 1),
                        (pts[0][0], plot_y0 + plot_h - 1)]
    draw.polygon(poly, fill=180)

    # Line + point dots on top of fill.
    if len(pts) >= 2:
        draw.line(pts, fill=0, width=2)
    for px, py in pts:
        draw.ellipse((px - 2, py - 2, px + 2, py + 2), fill=0)

    cap = f"min {_format_num(vmin)}  max {_format_num(vmax)}"
    cw, _ = _measure(draw, cap, f_cap)
    draw.text((width - cw, 1), cap, font=f_cap, fill=0)

    return _to_bw_fs(img)


def render_pie_chart(data: Dict[str, float],
                     width: int = CONTENT_W,
                     height: int = 200,
                     title: Optional[str] = None) -> Image.Image:
    if not data:
        return Image.new("1", (width, 1), 1)
    f_title = _font(FONT_BOLD, FS_BODY)
    f_lab = _font(FONT_REGULAR, FS_CAPTION)
    items = list(data.items())
    title_h = (FS_BODY + 10) if title else 0
    img = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(img)
    if title:
        draw.text((0, 0), title, font=f_title, fill=0)

    pie_top = title_h + 4
    pie_h = height - pie_top - 4
    r = pie_h // 2 - 2
    cx = r + 4
    cy = pie_top + r
    total = sum(float(v) for _, v in items) or 1.0
    grays = [40, 100, 165, 70, 200, 130, 30, 180, 90]

    angle = -90.0  # start at 12 o'clock
    for i, (lab, value) in enumerate(items):
        sweep = 360 * float(value) / total
        gray = grays[i % len(grays)]
        draw.pieslice((cx - r, cy - r, cx + r, cy + r),
                      angle, angle + sweep, fill=gray, outline=0, width=1)
        angle += sweep
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=0, width=2)

    # Legend on the right.
    legend_x = cx + r + 24
    legend_y = pie_top + 6
    swatch = 16
    for i, (lab, value) in enumerate(items):
        gray = grays[i % len(grays)]
        draw.rectangle(
            (legend_x, legend_y, legend_x + swatch, legend_y + swatch),
            fill=gray, outline=0, width=1)
        pct = 100 * float(value) / total
        text = f"{lab}  {_format_num(value)}  ({pct:.0f}%)"
        avail = width - (legend_x + swatch + 8)
        wrapped = _wrap(draw, text, f_lab, avail)
        ty = legend_y - 2
        for line in wrapped[:2]:
            draw.text((legend_x + swatch + 8, ty),
                      line, font=f_lab, fill=0)
            ty += _measure(draw, line, f_lab)[1] + 2
        legend_y = max(ty + 4, legend_y + swatch + 8)
    return _to_bw_fs(img)


def render_progress_bar(value: float,
                        label: str = "",
                        width: int = CONTENT_W) -> Image.Image:
    f = _font(FONT_REGULAR, FS_BODY)
    pct = max(0.0, min(1.0, float(value)))
    height = 36
    img = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(img)
    pct_str = f"{int(pct * 100)}%"
    pct_w, pct_h = _measure(draw, pct_str, f)
    label_w, label_h = _measure(draw, label, f) if label else (0, 0)
    if label:
        draw.text((0, (height - label_h) // 2 - 2),
                  label, font=f, fill=0)
    bar_x = label_w + 14 if label else 0
    bar_x_end = width - pct_w - 12
    bar_y = (height - 18) // 2
    bar_h = 18
    if bar_x_end > bar_x:
        draw.rectangle((bar_x, bar_y, bar_x_end, bar_y + bar_h),
                       outline=0, width=2)
        fill_w = int((bar_x_end - bar_x - 4) * pct)
        if fill_w > 0:
            draw.rectangle(
                (bar_x + 2, bar_y + 2, bar_x + 2 + fill_w, bar_y + bar_h - 2),
                fill=0)
    draw.text((width - pct_w, (height - pct_h) // 2 - 2),
              pct_str, font=f, fill=0)
    return _to_bw_text(img)


def render_heatmap(matrix: List[List[float]],
                   labels_x: Optional[List[str]] = None,
                   labels_y: Optional[List[str]] = None,
                   width: int = CONTENT_W,
                   height: Optional[int] = None,
                   title: Optional[str] = None) -> Image.Image:
    if not matrix or not matrix[0]:
        return Image.new("1", (width, 1), 1)
    rows = len(matrix)
    cols = len(matrix[0])
    f_title = _font(FONT_BOLD, FS_BODY)
    f_lab = _font(FONT_REGULAR, FS_CAPTION)

    tmp = ImageDraw.Draw(Image.new("L", (1, 1), 255))
    label_x_h = (FS_CAPTION + 6) if labels_x else 0
    label_y_w = (max((_measure(tmp, str(l), f_lab)[0] for l in labels_y),
                     default=0) + 8) if labels_y else 0
    title_h = (FS_BODY + 10) if title else 0

    cell = max(20, (width - label_y_w) // cols)
    grid_w = cell * cols
    grid_h = cell * rows
    h = title_h + label_x_h + grid_h + 6
    if height is not None:
        h = max(h, height)

    img = Image.new("L", (width, h), 255)
    draw = ImageDraw.Draw(img)
    if title:
        draw.text((0, 0), title, font=f_title, fill=0)

    flat = [float(v) for row in matrix for v in row]
    vmin, vmax = min(flat), max(flat)
    span = (vmax - vmin) or 1.0
    grid_x0 = label_y_w
    grid_y0 = title_h + label_x_h

    for ri, row in enumerate(matrix):
        for ci, v in enumerate(row):
            x = grid_x0 + ci * cell
            y = grid_y0 + ri * cell
            t = (float(v) - vmin) / span
            gray = int(255 - t * 235)
            draw.rectangle((x, y, x + cell - 1, y + cell - 1), fill=gray)

    # Grid lines on top of cells.
    for ri in range(rows + 1):
        y = grid_y0 + ri * cell
        draw.line((grid_x0, y, grid_x0 + grid_w, y), fill=0, width=1)
    for ci in range(cols + 1):
        x = grid_x0 + ci * cell
        draw.line((x, grid_y0, x, grid_y0 + grid_h), fill=0, width=1)

    if labels_y:
        for ri, lab in enumerate(labels_y[:rows]):
            lab = str(lab)
            _, lh = _measure(draw, lab, f_lab)
            y = grid_y0 + ri * cell + (cell - lh) // 2 - 2
            draw.text((0, y), lab, font=f_lab, fill=0)
    if labels_x:
        for ci, lab in enumerate(labels_x[:cols]):
            lab = str(lab)
            lw, _ = _measure(draw, lab, f_lab)
            x = grid_x0 + ci * cell + (cell - lw) // 2
            draw.text((x, title_h), lab, font=f_lab, fill=0)
    return _to_bw_fs(img)


def render_table(headers: List[str],
                 rows: List[List[str]],
                 width: int = CONTENT_W,
                 title: Optional[str] = None) -> Image.Image:
    f_h = _font(FONT_BOLD, FS_CAPTION)
    f_r = _font(FONT_REGULAR, FS_CAPTION)
    f_t = _font(FONT_BOLD, FS_BODY)
    n_cols = len(headers)
    if n_cols == 0:
        return Image.new("1", (width, 1), 1)
    col_w = width // n_cols
    pad = 5
    min_row_h = FS_CAPTION + 10
    body_line_h = int(_measure(ImageDraw.Draw(Image.new("L", (1, 1), 255)),
                               "Mg", f_r)[1] * 1.12)
    header_h = max(min_row_h, int(_measure(
        ImageDraw.Draw(Image.new("L", (1, 1), 255)), "Mg", f_h)[1] * 1.12) + 8)
    title_h = (FS_BODY + 6) if title else 0

    wrapped_rows: List[Tuple[List[List[str]], int]] = []
    for row in rows:
        wrapped_cells: List[List[str]] = []
        max_lines = 1
        for ci in range(n_cols):
            cell = str(row[ci]) if ci < len(row) else ""
            lines = _wrap(ImageDraw.Draw(Image.new("L", (1, 1), 255)),
                          cell, f_r, col_w - 2 * pad) or [""]
            wrapped_cells.append(lines)
            max_lines = max(max_lines, len(lines))
        row_h = max(min_row_h, max_lines * body_line_h + 8)
        wrapped_rows.append((wrapped_cells, row_h))

    height = title_h + header_h + 4 + sum(row_h for _, row_h in wrapped_rows)

    img = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(img)
    if title:
        draw.text((0, 0), title, font=f_t, fill=0)

    y = title_h
    for ci, h in enumerate(headers):
        draw.text((ci * col_w + pad, y + 4), str(h), font=f_h, fill=0)
    y += header_h
    draw.line((0, y - 1, width - 1, y - 1), fill=0, width=2)

    for wrapped_cells, row_h in wrapped_rows:
        for ci in range(n_cols):
            cell_y = y + 4
            for line in wrapped_cells[ci]:
                draw.text((ci * col_w + pad, cell_y), line, font=f_r, fill=0)
                cell_y += body_line_h
        y += row_h
        draw.line((0, y - 1, width - 1, y - 1), fill=0, width=1)
    return _to_bw_text(img)


def render_qr_code(data: str,
                   label: Optional[str] = None,
                   size: int = 168,
                   width: int = CONTENT_W) -> Image.Image:
    if not data:
        return Image.new("1", (width, 1), 1)

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("1")
    qr_img = qr_img.resize((size, size), Image.Resampling.NEAREST)

    label_lines: List[str] = []
    line_h = 0
    f_label = _font(FONT_REGULAR, FS_CAPTION)
    if label:
        tmp = ImageDraw.Draw(Image.new("L", (1, 1), 255))
        label_lines = _wrap(tmp, str(label), f_label, width)
        line_h = int(_measure(tmp, "Mg", f_label)[1] * 1.20)

    label_height = line_h * len(label_lines) if label_lines else 0
    gap = 10 if label_lines else 0
    total_h = size + gap + label_height
    img = Image.new("L", (width, total_h), 255)
    qr_x = (width - size) // 2
    img.paste(qr_img.convert("L"), (qr_x, 0))

    if label_lines:
        draw = ImageDraw.Draw(img)
        y = size + gap
        for line in label_lines:
            line_w, _ = _measure(draw, line, f_label)
            draw.text(((width - line_w) // 2, y), line, font=f_label, fill=0)
            y += line_h

    return _to_bw_text(img)


# ============================================================
# BLOCK RENDERERS  (used by /print/rich and render_session)
# ============================================================

def _block_header(b: Dict[str, Any]) -> Image.Image:
    title = str(b.get("title", "CLAUDE"))
    subtitle = b.get("subtitle")
    show_logo = bool(b.get("logo", False))

    f_brand = _font(FONT_BOLD, FS_BRAND)
    f_sub = _font(FONT_REGULAR, FS_SUBTITLE)
    spaced = " ".join(title)
    sub_spaced = str(subtitle).upper() if subtitle else None

    tmp = ImageDraw.Draw(Image.new("L", (1, 1), 255))
    bw, bh = _measure(tmp, spaced, f_brand)
    sw, sh = _measure(tmp, sub_spaced, f_sub) if sub_spaced else (0, 0)

    logo_size = 56 if show_logo else 0
    y_logo = 0
    y_brand = (logo_size + 14) if show_logo else 0
    y_sub = y_brand + bh + 3 if sub_spaced else 0
    height = (y_sub + sh + 1) if sub_spaced else (y_brand + bh + 1)

    text_l = Image.new("L", (CONTENT_W, height), 255)
    draw = ImageDraw.Draw(text_l)

    if show_logo:
        logo_bw = _logo_mark(logo_size)
        text_l.paste(logo_bw.convert("L"),
                      ((CONTENT_W - logo_size) // 2, y_logo))

    draw.text(((CONTENT_W - bw) // 2, y_brand),
              spaced, font=f_brand, fill=0)
    if sub_spaced:
        draw.text(((CONTENT_W - sw) // 2, y_sub),
                  sub_spaced, font=f_sub, fill=0)

    return _to_bw_text(text_l)


def _block_title(b: Dict[str, Any]) -> Image.Image:
    content = str(b.get("content", ""))
    # Title is already bold; inline ** inside flips to bold-italic so
    # emphasis still reads on the heaviest line of the ticket.
    font_for = _font_for_inline(FONT_BOLD, FONT_BOLD, FS_TITLE)
    runs = _parse_inline(content)
    tmp = ImageDraw.Draw(Image.new("L", (1, 1), 255))
    tokens = _tokenize_runs(runs)
    lines = _wrap_styled(tmp, tokens, font_for, CONTENT_W)
    line_h = _line_metrics(tmp, font_for, FS_TITLE)
    height = max(line_h, line_h * len(lines))

    img = Image.new("L", (CONTENT_W, height), 255)
    draw = ImageDraw.Draw(img)
    y = 0
    for line in lines:
        _draw_styled_line(draw, 0, y, line, font_for)
        y += line_h
    return _to_bw_text(img)


_STYLE_FONTS = {
    "title":    (FONT_BOLD,    FS_TITLE,    1.30),
    "body":     (FONT_REGULAR, FS_BODY,     1.30),
    "subtitle": (FONT_REGULAR, FS_SUBTITLE, 1.40),
    "meta":     (FONT_REGULAR, FS_META,     1.20),
    "time":     (FONT_REGULAR, FS_TIME,     1.20),
    "caption":  (FONT_REGULAR, FS_CAPTION,  1.20),
}

_STYLE_ALIGN = {
    "title":    "left",
    "body":     "left",
    "subtitle": "center",
    "meta":     "center",
    "time":     "center",
    "caption":  "center",
}


def _block_text(b: Dict[str, Any]) -> Image.Image:
    content = str(b.get("content", ""))
    style = str(b.get("style", "body"))
    align = b.get("align") or _STYLE_ALIGN.get(style, "left")
    font_path, fs, lh_mul = _STYLE_FONTS.get(style, _STYLE_FONTS["body"])
    base_bold = FONT_BOLD if font_path is FONT_REGULAR else font_path
    font_for = _font_for_inline(font_path, base_bold, fs)
    sample = font_for(frozenset())
    line_h = int(_measure(ImageDraw.Draw(Image.new("L", (1, 1), 255)),
                          "Mg", sample)[1] * lh_mul)

    runs = _parse_inline(content)
    tmp = ImageDraw.Draw(Image.new("L", (1, 1), 255))
    tokens = _tokenize_runs(runs)
    lines = _wrap_styled(tmp, tokens, font_for, CONTENT_W)
    height = max(line_h, line_h * len(lines))

    img = Image.new("L", (CONTENT_W, height), 255)
    draw = ImageDraw.Draw(img)
    y = 0
    for line in lines:
        if align == "center":
            x = (CONTENT_W - _line_width(draw, line, font_for)) // 2
        elif align == "right":
            x = CONTENT_W - _line_width(draw, line, font_for)
        else:
            x = 0
        _draw_styled_line(draw, x, y, line, font_for)
        y += line_h
    return _to_bw_text(img)


def _block_bullets(b: Dict[str, Any]) -> Image.Image:
    items = list(b.get("items") or [])
    if not items:
        return Image.new("1", (CONTENT_W, 1), 1)
    font_for = _font_for_inline(FONT_REGULAR, FONT_BOLD, FS_BODY)
    f_plain = _font(FONT_REGULAR, FS_BODY)
    bullet_x = 4
    text_x = 24
    text_w = CONTENT_W - text_x

    tmp = ImageDraw.Draw(Image.new("L", (1, 1), 255))
    line_h = int(_measure(tmp, "Mg", f_plain)[1] * 1.40)

    wrapped_all: List[List[List[_Run]]] = []
    height = 0
    for raw in items:
        runs = _parse_inline(str(raw))
        tokens = _tokenize_runs(runs)
        lines = _wrap_styled(tmp, tokens, font_for, text_w)
        wrapped_all.append(lines)
        height += line_h * max(1, len(lines)) + 4
    height = max(height, line_h)

    img = Image.new("L", (CONTENT_W, height), 255)
    draw = ImageDraw.Draw(img)
    y = 0
    for lines in wrapped_all:
        for i, line in enumerate(lines):
            if i == 0:
                draw.text((bullet_x, y), "•", font=f_plain, fill=0)
            _draw_styled_line(draw, text_x, y, line, font_for)
            y += line_h
        y += 4
    return _to_bw_text(img)


def _block_ornament(b: Dict[str, Any]) -> Image.Image:
    return render_ornament(b.get("style", "flourish"), CONTENT_W)


def _block_spacer(b: Dict[str, Any]) -> Image.Image:
    h = max(1, int(b.get("height", 12)))
    return Image.new("1", (CONTENT_W, h), 1)


def _block_bar_chart(b: Dict[str, Any]) -> Image.Image:
    return render_bar_chart(dict(b.get("data") or {}),
                            width=CONTENT_W, title=b.get("title"))


def _block_sparkline(b: Dict[str, Any]) -> Image.Image:
    return render_sparkline(list(b.get("values") or []),
                            width=CONTENT_W,
                            height=int(b.get("height", 80)),
                            title=b.get("title"))


def _block_pie_chart(b: Dict[str, Any]) -> Image.Image:
    return render_pie_chart(dict(b.get("data") or {}),
                            width=CONTENT_W,
                            height=int(b.get("height", 200)),
                            title=b.get("title"))


def _block_progress_bar(b: Dict[str, Any]) -> Image.Image:
    return render_progress_bar(float(b.get("value", 0)),
                               label=str(b.get("label", "")),
                               width=CONTENT_W)


def _block_heatmap(b: Dict[str, Any]) -> Image.Image:
    return render_heatmap(list(b.get("matrix") or []),
                          labels_x=list(b.get("labels_x") or []) or None,
                          labels_y=list(b.get("labels_y") or []) or None,
                          width=CONTENT_W,
                          height=b.get("height"),
                          title=b.get("title"))


def _block_table(b: Dict[str, Any]) -> Image.Image:
    return render_table(list(b.get("headers") or []),
                        list(b.get("rows") or []),
                        width=CONTENT_W,
                        title=b.get("title"))


def _block_qr_code(b: Dict[str, Any]) -> Image.Image:
    return render_qr_code(
        str(b.get("data", "")),
        label=b.get("label"),
        size=int(b.get("size", 168)),
        width=CONTENT_W,
    )


_BLOCK_RENDERERS = {
    "header":       _block_header,
    "title":        _block_title,
    "text":         _block_text,
    "bullets":      _block_bullets,
    "ornament":     _block_ornament,
    "spacer":       _block_spacer,
    "bar_chart":    _block_bar_chart,
    "sparkline":    _block_sparkline,
    "pie_chart":    _block_pie_chart,
    "progress_bar": _block_progress_bar,
    "heatmap":      _block_heatmap,
    "table":        _block_table,
    "qr_code":      _block_qr_code,
}

# Per-block-type vertical gap below the block. Tuned for visual rhythm.
_GAP_AFTER = {
    "header":       10,
    "title":        10,
    "text":         8,
    "bullets":      14,
    "ornament":     14,
    "spacer":       0,
    "bar_chart":    16,
    "sparkline":    16,
    "pie_chart":    16,
    "progress_bar": 12,
    "heatmap":      16,
    "table":        16,
    "qr_code":      16,
}


def render_blocks(blocks: List[Dict[str, Any]]) -> Image.Image:
    """Composite a list of blocks into a single 1-bit image."""
    if not blocks:
        return Image.new("1", (CANVAS_WIDTH, 1), 1)

    rendered: List[Tuple[Dict[str, Any], Image.Image]] = []
    for b in blocks:
        t = str(b.get("type", "text"))
        fn = _BLOCK_RENDERERS.get(t, _block_text)
        try:
            img = fn(b)
        except Exception as exc:  # noqa: BLE001
            # Render a small error tag so the caller can see what failed.
            err_l = Image.new("L", (CONTENT_W, 28), 255)
            ImageDraw.Draw(err_l).text(
                (0, 4), f"[block:{t}] error: {exc}",
                font=_font(FONT_REGULAR, FS_CAPTION), fill=0)
            img = _to_bw_text(err_l)
        rendered.append((b, img))

    # Compute total height with per-block gaps. Keep edge padding minimal so
    # tickets do not waste paper before the first line or after the last fact.
    pad_top = 2
    pad_bottom = 2
    total_h = pad_top + pad_bottom
    for i, (b, img) in enumerate(rendered):
        total_h += img.height
        if i < len(rendered) - 1:
            total_h += _GAP_AFTER.get(str(b.get("type", "")), 12)

    canvas = Image.new("1", (CANVAS_WIDTH, total_h), 1)
    y = pad_top
    for i, (b, img) in enumerate(rendered):
        if img.width == CANVAS_WIDTH:
            canvas.paste(img, (0, y))
        else:
            x = (CANVAS_WIDTH - img.width) // 2
            canvas.paste(img, (max(0, x), y))
        y += img.height
        if i < len(rendered) - 1:
            y += _GAP_AFTER.get(str(b.get("type", "")), 12)
    return canvas


# ============================================================
# SESSION PRESET (legacy /print/session shim)
# ============================================================

def render_session(brand: str,
                   title: str,
                   results: List[str],
                   model: Optional[str],
                   turns: Optional[int],
                   duration: Optional[str],
                   timestamp: Optional[str]) -> Image.Image:
    ts = timestamp or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    header_title = (brand or "CLAUDE").strip().upper()[:20]
    blocks: List[Dict[str, Any]] = [
        {"type": "header", "title": header_title,
         "subtitle": "STATUS", "logo": False},
        {"type": "title", "content": title},
    ]
    if results:
        blocks.append({"type": "bullets", "items": list(results)})

    parts: List[str] = []
    if model:
        parts.append(model.replace("claude-", "").replace("gpt-", "").strip())
    if turns is not None:
        parts.append(f"{turns} turn{'s' if turns != 1 else ''}")
    if duration:
        parts.append(duration)
    if parts:
        blocks.append({"type": "text",
                        "content": "   ·   ".join(parts),
                        "style": "meta"})
    blocks.append({"type": "text", "content": ts, "style": "time"})
    return render_blocks(blocks)
