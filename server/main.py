"""FastAPI app: printer, session status, and Alexa web-service front."""
from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from alexa_skill import build_alexa_webservice_handler
from printer import PrinterService
from schemas import RichRequest, SessionStatusUpdate, SessionTicket, TextRequest
from status_store import StatusStore

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("main")

def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _require_token(request: Request, expected: str | None) -> None:
    if not expected:
        return
    provided = request.headers.get("x-status-token", "")
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid_status_token")


def create_app(
    *,
    device: str | None = None,
    width: int | None = None,
    dry_run: bool | None = None,
    db_path: str | None = None,
    status_token: str | None = None,
    skill_id: str | None = None,
    alexa_verify_signature: bool | None = None,
    alexa_verify_timestamp: bool | None = None,
    alexa_limit: int | None = None,
) -> FastAPI:
    device = device or os.environ.get("PRINTER_DEVICE", "/dev/usb/lp0")
    width = int(width or os.environ.get("PRINTER_WIDTH_CHARS", "48"))
    if dry_run is None:
        dry_run = _env_bool("PRINTER_DRY_RUN", False)
    default_db_path = str(Path(__file__).resolve().parent / "data" / "session-status.db")
    db_path = db_path or os.environ.get("STATUS_DB_PATH", default_db_path)
    status_token = status_token if status_token is not None else os.environ.get(
        "STATUS_API_TOKEN"
    )
    skill_id = skill_id if skill_id is not None else os.environ.get("ALEXA_SKILL_ID")
    if alexa_verify_signature is None:
        alexa_verify_signature = _env_bool("ALEXA_VERIFY_SIGNATURE", True)
    if alexa_verify_timestamp is None:
        alexa_verify_timestamp = _env_bool("ALEXA_VERIFY_TIMESTAMP", True)
    alexa_limit = int(alexa_limit or os.environ.get("ALEXA_STATUS_LIMIT", "3"))

    app = FastAPI(title="Receipt Printer", version="3.0.0")
    printer = PrinterService(device=device, width_chars=width, dry_run=dry_run)
    status_store = StatusStore(
        db_path=db_path,
        active_window_seconds=int(os.environ.get("STATUS_ACTIVE_WINDOW_SECONDS", "1200")),
        recent_window_seconds=int(os.environ.get("STATUS_RECENT_WINDOW_SECONDS", "86400")),
    )
    alexa_handler = build_alexa_webservice_handler(
        status_store,
        skill_id=skill_id,
        limit=alexa_limit,
        verify_signature=alexa_verify_signature,
        verify_timestamp=alexa_verify_timestamp,
    )

    @app.get("/health")
    async def health():
        printer_health = await printer.health()
        printer_health["status_db_path"] = db_path
        return printer_health

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

    @app.post("/status/update")
    async def status_update(req: SessionStatusUpdate, request: Request):
        _require_token(request, status_token)
        try:
            return status_store.ingest(req)
        except Exception as exc:
            log.exception("status/update failed")
            raise HTTPException(status_code=500, detail=str(exc))

    @app.get("/status/latest")
    async def status_latest(request: Request, limit: int = 3):
        _require_token(request, status_token)
        try:
            return status_store.latest(limit=limit)
        except Exception as exc:
            log.exception("status/latest failed")
            raise HTTPException(status_code=500, detail=str(exc))

    @app.get("/status/voice")
    async def status_voice(request: Request, limit: int = 3):
        _require_token(request, status_token)
        try:
            return status_store.voice_payload(limit=limit)
        except Exception as exc:
            log.exception("status/voice failed")
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/alexa")
    async def alexa(request: Request):
        body = (await request.body()).decode("utf-8")
        headers = {key: value for key, value in request.headers.items()}
        try:
            response_body = alexa_handler.verify_request_and_dispatch(headers, body)
        except Exception as exc:
            log.warning(
                "alexa request rejected: %s; header_keys=%s",
                exc,
                sorted(headers.keys()),
            )
            raise HTTPException(status_code=400, detail="invalid_alexa_request")
        if isinstance(response_body, (dict, list)):
            return JSONResponse(content=response_body)
        return Response(content=response_body, media_type="application/json")

    return app


app = create_app()
