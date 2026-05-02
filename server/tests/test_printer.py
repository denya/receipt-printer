import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from printer import PrinterService
from schemas import SessionTicket


class PrinterServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_reports_dry_run_ready(self) -> None:
        service = PrinterService(device="/dev/usb/lp0", width_chars=48, dry_run=True)

        health = await service.health()

        self.assertTrue(health["ok"])
        self.assertTrue(health["dry_run"])
        self.assertEqual(health["device_mode"], "dry_run")

    async def test_print_session_succeeds_in_dry_run(self) -> None:
        service = PrinterService(device="/dev/usb/lp0", width_chars=48, dry_run=True)

        result = await service.print_session(SessionTicket(
            brand="CODEX",
            title="Deploy receipt printer",
            results=["Reviewed repo", "Patched bugs"],
            model="claude-opus-4-7",
            turns=6,
            duration="3m 55s",
        ))

        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], "session")
        self.assertEqual(result["image"]["width"], 576)
