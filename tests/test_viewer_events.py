import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
import viewer


class ViewerEventMarkupTests(unittest.TestCase):
    def test_event_header_clamps_the_summary_and_uses_text(self):
        css = viewer.CSS
        self.assertIn("-webkit-line-clamp: 2", css)
        self.assertIn("max-height: 2.7em", css)
        self.assertIn("font-weight: 400", css)
        self.assertIn(".badge.unconfirmed", css)
        script = viewer.JS
        for phrase in (
            "No named speakers yet.",
            "Summary pending",
            "AI summary off",
            "Summary unavailable",
            "Partial summary.",
            "Excerpt · ",
        ):
            self.assertIn(phrase, script)
        self.assertIn("summary.textContent = block.summary", script)
        self.assertNotIn("innerHTML", script)


if __name__ == "__main__":
    unittest.main()
