import pathlib
import tempfile
import unittest
import datetime as dt


ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from schemas import SessionStatusUpdate
from status_store import StatusStore, utc_now_iso


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

    def test_voice_payload_filters_internal_prompt_titles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StatusStore(
                db_path=str(pathlib.Path(tmpdir) / "status.db"),
                active_window_seconds=1800,
                recent_window_seconds=86400,
            )
            store.ingest(SessionStatusUpdate(
                source="codex",
                session_key="bad-1",
                turn_key="bad-1:1",
                title="You are a helpful assistant. You will be presented with a user prompt.",
                summary_line='{"title":"Review b6e364d"}',
                status="unknown",
            ))
            store.ingest(SessionStatusUpdate(
                source="codex",
                session_key="good-1",
                turn_key="good-1:1",
                title="Alexa skill setup",
                summary_line="Skill created and ready for testing.",
                status="completed",
            ))

            payload = store.voice_payload(limit=3)

            self.assertIn("Alexa skill setup", payload["speech_text"])
            self.assertNotIn("You are a helpful assistant", payload["speech_text"])

    def test_recent_payload_reads_multiple_recent_sessions_when_no_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StatusStore(
                db_path=str(pathlib.Path(tmpdir) / "status.db"),
                active_window_seconds=1,
                recent_window_seconds=86400,
            )
            base = dt.datetime.fromisoformat(utc_now_iso().replace("Z", "+00:00"))
            store.ingest(SessionStatusUpdate(
                source="codex",
                session_key="recent-1",
                turn_key="recent-1:1",
                title="First recent session",
                summary_line="First recent summary.",
                status="completed",
                updated_at=(base - dt.timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
            ))
            store.ingest(SessionStatusUpdate(
                source="codex",
                session_key="recent-2",
                turn_key="recent-2:1",
                title="Second recent session",
                summary_line="Second recent summary.",
                status="completed",
                updated_at=(base - dt.timedelta(minutes=4)).isoformat().replace("+00:00", "Z"),
            ))

            payload = store.voice_payload(limit=3)

            self.assertTrue(payload["stale"])
            self.assertIn("Latest recent sessions", payload["speech_text"])
            self.assertNotIn("No active sessions right now", payload["speech_text"])
            self.assertIn("Second recent session", payload["speech_text"])
            self.assertIn("First recent session", payload["speech_text"])
