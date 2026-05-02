import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

import renderer


class RendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_regular = renderer.FONT_REGULAR
        self._orig_bold = renderer.FONT_BOLD
        self._orig_mono = renderer.FONT_MONO
        renderer._FONT_CACHE.clear()

    def tearDown(self) -> None:
        renderer.FONT_REGULAR = self._orig_regular
        renderer.FONT_BOLD = self._orig_bold
        renderer.FONT_MONO = self._orig_mono
        renderer._FONT_CACHE.clear()

    def test_render_session_survives_missing_system_fonts(self) -> None:
        renderer.FONT_REGULAR = ("definitely-missing-regular.ttf",)
        renderer.FONT_BOLD = ("definitely-missing-bold.ttf",)
        renderer.FONT_MONO = ("definitely-missing-mono.ttf",)

        img = renderer.render_session(
            brand="CLAUDE",
            title="Deploy receipt printer",
            results=["Reviewed code", "Fixed renderer fallback"],
            model="claude-opus-4-7",
            turns=8,
            duration="4m 21s",
            timestamp="2026-05-02 13:30",
        )

        self.assertEqual(img.mode, "1")
        self.assertEqual(img.width, renderer.CANVAS_WIDTH)
        self.assertGreater(img.height, 1)

    def test_render_session_brand_changes_output(self) -> None:
        claude = renderer.render_session(
            brand="CLAUDE",
            title="Deploy receipt printer",
            results=["Reviewed code"],
            model="claude-opus-4-7",
            turns=8,
            duration="4m 21s",
            timestamp="2026-05-02 13:30",
        )
        codex = renderer.render_session(
            brand="CODEX",
            title="Deploy receipt printer",
            results=["Reviewed code"],
            model="gpt-5.4",
            turns=8,
            duration="4m 21s",
            timestamp="2026-05-02 13:30",
        )

        self.assertNotEqual(claude.tobytes(), codex.tobytes())

    def test_render_blocks_error_tag_works_without_custom_fonts(self) -> None:
        renderer.FONT_REGULAR = ("definitely-missing-regular.ttf",)
        renderer.FONT_BOLD = ("definitely-missing-bold.ttf",)

        img = renderer.render_blocks([
            {"type": "heatmap", "matrix": [["not-a-number"]]},
        ])

        self.assertEqual(img.mode, "1")
        self.assertEqual(img.width, renderer.CANVAS_WIDTH)
        self.assertGreater(img.height, 1)

    def test_render_table_expands_for_wrapped_cells(self) -> None:
        short = renderer.render_table(
            headers=["Name", "Notes"],
            rows=[["Printer", "Ready"]],
        )
        long = renderer.render_table(
            headers=["Name", "Notes"],
            rows=[[
                "Printer",
                "This row should wrap across multiple lines instead of "
                "silently truncating after the first one.",
            ]],
        )

        self.assertGreater(long.height, short.height)

    def test_inline_parser_handles_common_markdown(self) -> None:
        runs = renderer._parse_inline(
            "mix **bold** and *italic* and `code` and ~~strike~~ and ***both***"
        )
        styles = {text: set(s) for text, s in runs}
        self.assertEqual(styles["bold"], {"b"})
        self.assertEqual(styles["italic"], {"i"})
        self.assertEqual(styles["code"], {"code"})
        self.assertEqual(styles["strike"], {"s"})
        self.assertEqual(styles["both"], {"b", "i"})

    def test_inline_parser_skips_intra_word_underscores(self) -> None:
        # snake_case identifiers and arithmetic should stay unstyled.
        for text in ("snake_case_var", "5 * 4 * 3 = 60"):
            runs = renderer._parse_inline(text)
            self.assertEqual(runs, [(text, frozenset())], text)

    def test_inline_parser_locks_code_span_content(self) -> None:
        runs = renderer._parse_inline("`literal **stars**`")
        self.assertEqual(runs, [("literal **stars**", frozenset({"code"}))])

    def test_render_blocks_strips_markdown_delimiters(self) -> None:
        # Plain ASCII rendering of styled content must not contain the
        # raw "**" / "*" delimiters that the parser is meant to consume.
        plain = renderer.render_blocks([
            {"type": "title", "content": "Refined formatting"},
            {"type": "bullets", "items": ["Added bold rendering"]},
        ])
        styled = renderer.render_blocks([
            {"type": "title", "content": "Refined **formatting**"},
            {"type": "bullets", "items": ["Added **bold** rendering"]},
        ])
        # Visually different (bold weight changes pixel count)…
        self.assertNotEqual(plain.tobytes(), styled.tobytes())
        # …but neither image should contain runaway height from the
        # delimiters being treated as literal text.
        self.assertLess(abs(plain.height - styled.height), 8)

    def test_render_qr_code_returns_printable_image(self) -> None:
        img = renderer.render_qr_code(
            "https://github.com/denya/receipt-printer",
            label="github.com/denya/receipt-printer",
            size=144,
        )

        self.assertEqual(img.mode, "1")
        self.assertEqual(img.width, renderer.CONTENT_W)
        self.assertGreater(img.height, 144)


if __name__ == "__main__":
    unittest.main()
