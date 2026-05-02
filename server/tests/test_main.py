import pathlib
import tempfile
import unittest

from fastapi.testclient import TestClient


ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from main import create_app


def alexa_intent_request(intent_name: str) -> dict:
    return {
        "version": "1.0",
        "session": {
            "new": True,
            "sessionId": "SessionId.test",
            "application": {"applicationId": "amzn1.ask.skill.test"},
            "user": {"userId": "amzn1.ask.account.test"},
        },
        "context": {
            "System": {
                "application": {"applicationId": "amzn1.ask.skill.test"},
                "user": {"userId": "amzn1.ask.account.test"},
                "device": {"deviceId": "device", "supportedInterfaces": {}},
            }
        },
        "request": {
            "type": "IntentRequest",
            "requestId": "EdwRequestId.test",
            "locale": "en-US",
            "timestamp": "2026-05-02T19:00:00Z",
            "intent": {"name": intent_name, "confirmationStatus": "NONE"},
        },
    }


class MainAppTests(unittest.TestCase):
    def test_status_endpoints_and_alexa_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = create_app(
                device="/dev/usb/lp0",
                width=48,
                dry_run=True,
                db_path=str(pathlib.Path(tmpdir) / "status.db"),
                status_token="secret",
                skill_id="amzn1.ask.skill.test",
                alexa_verify_signature=False,
                alexa_verify_timestamp=False,
            )
            client = TestClient(app)

            response = client.post(
                "/status/update",
                headers={"x-status-token": "secret"},
                json={
                    "source": "codex",
                    "session_key": "thread-1",
                    "turn_key": "thread-1:1",
                    "title": "Relocant CRM",
                    "summary_line": "Waiting on final verification.",
                    "status": "waiting",
                },
            )
            self.assertEqual(response.status_code, 200)

            voice = client.get("/status/voice?limit=3", headers={"x-status-token": "secret"})
            self.assertEqual(voice.status_code, 200)
            self.assertIn("Relocant CRM", voice.json()["speech_text"])

            alexa = client.post("/alexa", json=alexa_intent_request("GetSessionStatusIntent"))
            self.assertEqual(alexa.status_code, 200)
            self.assertIn("Relocant CRM", alexa.text)
