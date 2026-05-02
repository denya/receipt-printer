# Receipt printer microservice — FastAPI + python-escpos
# Targets Epson TM-T20II via the kernel usblp device at /dev/usb/lp0,
# which is bind-mounted by docker-compose (see compose file).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# libusb-1.0  : kept in case we ever switch to the Usb backend.
# fonts-*     : Liberation (Helvetica/Arial-ish) + DejaVu fallback for
#               the rasterized ticket renderer (renderer.py).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         libusb-1.0-0 ca-certificates \
         fonts-liberation fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/
RUN pip install -r requirements.txt

COPY *.py /app/

EXPOSE 9100

# Runs as root so we can open /dev/usb/lp0 (root:lp 660). The usblp
# kernel driver owns the bus; we only do plain file writes — no libusb
# fighting for the interface.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9100", "--log-level", "info"]
