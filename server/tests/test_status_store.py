import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from schemas import SessionStatusUpdate
from status_store import StatusStore


class StatusStoreTests(unittest.TestCase):
    def test_latest_prefers_newer_update_and_ignores_stale_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StatusStore(db_path=str(pathlib.Path(tmpdir) / "status.db"))
            fresh = SessionStatusUpdate(
                source="codex",
                session_key="thread-1",
                turn_key="thread-1:2",
                title="Relocant CRM",
                summary_line="Saved views refined.",
                status="completed",
                updated_at="2026-05-02T20:11:03Z",
            )
            stale = SessionStatusUpdate(
                source="codex",
                session_key="thread-1",
                turn_key="thread-1:1",
                title="Relocant CRM",
                summary_line="Reading files.",
                status="running",
                updated_at="2026-05-02T20:01:03Z",
            )

            self.assertTrue(store.ingest(fresh)["stored"])
            self.assertFalse(store.ingest(stale)["stored"])

            latest = store.latest(limit=3)
            self.assertEqual(latest["sessions"][0]["summary_line"], "Saved views refined.")
            self.assertEqual(latest["sessions"][0]["status"], "completed")

    def test_voice_payload_prefers_active_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StatusStore(
                db_path=str(pathlib.Path(tmpdir) / "status.db"),
                active_window_seconds=1800,
                recent_window_seconds=86400,
            )
            store.ingest(SessionStatusUpdate(
                source="claude",
                session_key="session-1",
                turn_key="session-1:abc",
                title="Telegram triage",
                summary_line="Processing recent messages.",
                status="running",
            ))

            payload = store.voice_payload(limit=3)

            self.assertFalse(payload["stale"])
            self.assertIn("active session", payload["speech_text"])
            self.assertIn("Telegram triage", payload["speech_text"])
