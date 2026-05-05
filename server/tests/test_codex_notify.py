import importlib.util
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
CLIENT = ROOT / "client" / "print-codex-notify.py"


def load_notify_module():
    spec = importlib.util.spec_from_file_location("print_codex_notify", CLIENT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class CodexNotifyQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.notify = load_notify_module()

    def test_extract_results_removes_markdown_links_code_and_paths(self) -> None:
        text = """
        **Changed**
        - Updated [`server/renderer.py`](/Users/denya/code/random-vibe-coding/receipt-printer/server/renderer.py:12) to remove blank ticket padding.
        - Verified status voice at https://example.com/down/link without exposing the URL.

        ```bash
        curl http://100.78.6.79:9100/print/session
        ```

        - Alexa now says when input is needed instead of reading raw markdown.
        """

        results = self.notify.extract_results(text)
        joined = " ".join(results)

        self.assertGreaterEqual(len(results), 3)
        self.assertNotIn("```", joined)
        self.assertNotIn("http", joined)
        self.assertNotIn("/Users/denya", joined)
        self.assertNotIn("curl", joined)
        self.assertNotIn(" at without ", f" {joined} ")
        self.assertIn("server/renderer.py", joined)
        self.assertIn("input is needed", joined)

    def test_summary_clip_prefers_complete_sentence(self) -> None:
        text = (
            "Implemented compact status cards that strip markdown links and keep "
            "the whole useful sentence. This extra detail should not be needed "
            "for the compact voice summary."
        )

        summary = self.notify.derive_summary_line(text, "completed")

        self.assertTrue(summary.endswith("."))
        self.assertIn("whole useful sentence", summary)
        self.assertNotIn("This extra detail", summary)

    def test_local_print_filter_skips_running_churn(self) -> None:
        payload = {"type": "agent-turn-complete", "cwd": str(ROOT)}
        text = (
            "I’m inspecting the renderer and status store now. I’ll run tests "
            "after mapping the current ticket layout and voice payload behavior."
        )

        self.assertFalse(self.notify.local_should_print(payload, text))

    def test_completed_with_prior_failure_still_prints_as_completion(self) -> None:
        payload = {"type": "agent-turn-complete", "cwd": str(ROOT)}
        text = (
            "The first test run failed, then I fixed the renderer. Tests passed "
            "and the receipt status summary is verified."
        )

        self.assertEqual(self.notify.classify_status(text), "completed")
        self.assertTrue(self.notify.local_should_print(payload, text))

    def test_semantic_fingerprint_deduplicates_reconfirmed_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self.notify.STATE_PATH = pathlib.Path(tmpdir) / "state.json"
            payload_a = {
                "type": "agent-turn-complete",
                "thread-id": "thread-1",
                "turn-id": "1",
                "cwd": str(ROOT),
                "input-messages": ["Improve receipt printing and Alexa voice status"],
            }
            payload_b = {**payload_a, "turn-id": "2"}
            text = (
                "Completed the receipt quality pass. Tests passed and the voice "
                "status now reports whether user input is needed."
            )

            self.assertFalse(self.notify.seen_print_fingerprint(payload_a, text))
            self.assertTrue(self.notify.seen_print_fingerprint(payload_b, text))

    def test_markdown_table_becomes_rich_table_and_voice_summary(self) -> None:
        text = """
        | Area | Changed | Evidence |
        |---|---|---|
        | Ticket layout | Removed logo and narrowed margins | top 4 px |
        | Alexa voice | Added table summary | no pipes |
        | Codex churn | Skips running updates | print false |
        """
        payload = {
            "type": "agent-turn-complete",
            "cwd": str(ROOT),
            "input-messages": ["Improve receipt tables"],
        }
        calls = []

        def fake_post_json(url, body):
            calls.append((url, body))

        self.notify.post_json = fake_post_json

        summary = self.notify.derive_summary_line(text, "completed")
        self.notify.post_receipt(payload, text)

        self.assertNotIn("|", summary)
        self.assertNotIn("---", summary)
        self.assertNotIn(":", summary)
        self.assertNotIn(";", summary)
        self.assertIn("Table with 3 rows", summary)
        self.assertEqual(len(calls), 1)
        url, body = calls[0]
        self.assertTrue(url.endswith("/print/rich"))
        table_blocks = [b for b in body["blocks"] if b["type"] == "table"]
        self.assertEqual(len(table_blocks), 1)
        self.assertEqual(table_blocks[0]["headers"], ["Area", "Changed", "Evidence"])
        self.assertEqual(len(table_blocks[0]["rows"]), 3)

    def test_extract_results_omits_markdown_table_source(self) -> None:
        text = """
        Completed the reporting pass.

        | Area | Result |
        |---|---|
        | Tickets | Real table rendering |

        - Tests passed.
        """

        results = self.notify.extract_results(text)
        joined = " ".join(results)

        self.assertIn("Tests passed.", joined)
        self.assertNotIn("Area", joined)
        self.assertNotIn("|", joined)


if __name__ == "__main__":
    unittest.main()
