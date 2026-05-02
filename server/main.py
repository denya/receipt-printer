"""FastAPI app: thin HTTP front for the receipt printer."""
import logging
import os

from fastapi import FastAPI, HTTPException

from printer import PrinterService
from schemas import RichRequest, SessionTicket, TextRequest

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("main")

DEVICE = os.environ.get("PRINTER_DEVICE", "/dev/usb/lp0")
WIDTH = int(os.environ.get("PRINTER_WIDTH_CHARS", "48"))

app = FastAPI(title="Receipt Printer", version="2.0.0")
printer = PrinterService(device=DEVICE, width_chars=WIDTH)


@app.get("/health")
async def health():
    return await printer.health()


@app.post("/print/test")
async def print_test():
    try:
        return await printer.print_test()
    except Exception as exc:
        log.exception("print/test failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/print/text")
async def print_text(req: TextRequest):
    try:
        return await printer.print_text(req.text, cut=req.cut)
    except Exception as exc:
        log.exception("print/text failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/print/session")
async def print_session(req: SessionTicket):
    try:
        return await printer.print_session(req)
    except Exception as exc:
        log.exception("print/session failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/print/rich")
async def print_rich(req: RichRequest):
    try:
        return await printer.print_rich(req.blocks)
    except Exception as exc:
        log.exception("print/rich failed")
        raise HTTPException(status_code=500, detail=str(exc))
