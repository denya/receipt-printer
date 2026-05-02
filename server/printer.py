"""Thin async wrapper around python-escpos for the Epson TM-T20II.

We use the File backend (writes to /dev/usb/lp0 directly) rather than the
Usb backend, because the kernel's usblp driver already owns the device and
fighting it with libusb is fragile. Sync escpos calls run inside
asyncio.to_thread() so the FastAPI event loop never blocks on a slow
write, and an asyncio.Lock serializes prints (the printer is a serial
device — concurrent writes corrupt output).

Layout strategy:
- /print/text and /print/test stay text-mode (codepage CP437) — fast,
  no font dependencies needed for plain output.
- /print/session and /print/rich go through renderer.py → Pillow →
  printer.image(), giving real typography and pixel-precise layout.
"""
import asyncio
import datetime
import logging
import os
from typing import Any, Callable, Dict, List

from escpos.printer import File
from PIL import Image

import renderer

log = logging.getLogger("printer")


class PrinterService:
    def __init__(self, device: str, width_chars: int):
        self.device = device
        self.width = max(20, int(width_chars))
        self._lock = asyncio.Lock()

    # ---------- public API ----------

    async def health(self) -> Dict[str, Any]:
        try:
            st = os.stat(self.device)
        except FileNotFoundError:
            return {"ok": False, "device": self.device,
                    "error": "device_not_found"}
        try:
            await self._run(lambda p: None)
        except Exception as exc:
            return {"ok": False, "device": self.device, "error": str(exc)}
        return {
            "ok": True,
            "device": self.device,
            "device_mode": oct(st.st_mode & 0o777),
            "width_chars": self.width,
        }

    async def print_test(self) -> Dict[str, Any]:
        await self._run(self._render_test)
        return {"ok": True, "kind": "test"}

    async def print_text(self, text: str, cut: bool) -> Dict[str, Any]:
        def job(p):
            p.text(text)
            if not text.endswith("\n"):
                p.text("\n")
            if cut:
                p.text("\n\n")
                p.cut()
        await self._run(job)
        return {"ok": True, "kind": "text", "chars": len(text)}

    async def print_session(self, ticket) -> Dict[str, Any]:
        img = renderer.render_session(
            title=ticket.title,
            results=list(ticket.results or []),
            model=ticket.model,
            turns=ticket.turns,
            duration=ticket.duration,
            timestamp=ticket.timestamp,
        )
        await self._print_image(img)
        return {
            "ok": True, "kind": "session",
            "image": {"width": img.width, "height": img.height},
        }

    async def print_rich(self, blocks: List[Dict[str, Any]]) -> Dict[str, Any]:
        img = renderer.render_blocks(blocks)
        await self._print_image(img)
        return {
            "ok": True, "kind": "rich",
            "blocks": len(blocks),
            "image": {"width": img.width, "height": img.height},
        }

    # ---------- internals ----------

    async def _print_image(self, img: Image.Image) -> None:
        def job(p):
            # impl="bitImageRaster" → ESC/POS GS v 0 raster command,
            # the most reliable mode on the TM-T20II for tall images.
            p.image(img, impl="bitImageRaster",
                    high_density_horizontal=True,
                    high_density_vertical=True)
            p.text("\n\n\n")
            p.cut()
        await self._run(job)

    async def _run(self, job: Callable[[Any], None]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._open_and_run, job)

    def _open_and_run(self, job: Callable[[Any], None]) -> None:
        # Re-open per job — cheap, avoids stale state and fd leaks.
        printer = File(self.device, auto_flush=True)
        try:
            job(printer)
        finally:
            try:
                printer.close()
            except Exception:
                pass

    # ---------- text-mode test slip ----------
    # Kept text-mode (CP437) so it works even if font files are missing
    # — useful as a renderer-independent smoke test.

    def _render_test(self, p) -> None:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        p.set(align="center", bold=True)
        p.text(("=" * self.width) + "\n")
        p.text("PRINTER TEST OK\n")
        p.text(("=" * self.width) + "\n")
        p.set(align="left", bold=False)
        p.text("\n")
        p.text("  Device   " + self.device + "\n")
        p.text("  Model    EPSON TM-T20II\n")
        p.text("  Width    " + str(self.width) + " chars\n")
        p.text("  Time     " + ts + "\n")
        p.text("\n")
        p.text(("-" * self.width) + "\n")
        p.text("\n\n\n")
        p.cut()
