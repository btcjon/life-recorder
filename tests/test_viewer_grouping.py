import copy
import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
viewer = importlib.import_module("viewer")


def clip(chunk_id, started, duration=60, transcript="hello"):
    return {
        "id": chunk_id,
        "started": started,
        "duration": duration,
        "transcript": transcript,
        "status": "complete",
    }


class DisplayBlockTests(unittest.TestCase):
    def assert_chunk(self, block, chunk_id):
        self.assertEqual(block, {"kind": "chunk", "chunk_id": chunk_id})

    def test_empty_day(self):
        self.assertEqual(viewer.display_blocks([]), [])

    def test_singleton_stays_a_row(self):
        blocks = viewer.display_blocks([clip("a", "2026-09-22T13:14:00Z")])
        self.assert_chunk(blocks[0], "a")
        self.assertEqual(len(blocks), 1)

    def test_adjacent_minutes_become_one_event(self):
        chunks = [
            clip("a", "2026-09-22T13:14:00Z", transcript="first words"),
            clip("b", "2026-09-22T13:15:00Z", transcript="second words"),
        ]
        blocks = viewer.display_blocks(chunks)
        self.assertEqual(len(blocks), 1)
        block = blocks[0]
        self.assertEqual(block["kind"], "event")
        self.assertEqual(block["chunk_ids"], ["a", "b"])
        self.assertEqual(block["clip_count"], 2)
        self.assertEqual(block["duration"], 120)
        self.assertEqual(block["preview"], "first words")
        self.assertIn("09:14", block["started_local"])
        self.assertIn("09:16", block["ended_local"])
        self.assertNotIn("Meeting", block["preview"])

    def test_gap_of_120_seconds_continues_and_121_splits(self):
        joined = viewer.display_blocks([
            clip("a", "2026-09-22T13:00:00Z", duration=60),
            clip("b", "2026-09-22T13:03:00Z"),
        ])
        self.assertEqual(joined[0]["chunk_ids"], ["a", "b"])
        split = viewer.display_blocks([
            clip("a", "2026-09-22T13:00:00Z", duration=60),
            clip("b", "2026-09-22T13:03:01Z"),
        ])
        self.assertEqual([block["chunk_id"] for block in split], ["a", "b"])

    def test_missing_transcripts_are_included_only_when_bracketed(self):
        leading = viewer.display_blocks([
            clip("gap", "2026-09-22T12:59:00Z", transcript=""),
            clip("a", "2026-09-22T13:00:00Z"),
            clip("b", "2026-09-22T13:01:00Z"),
        ])
        self.assert_chunk(leading[0], "gap")
        self.assertEqual(leading[1]["chunk_ids"], ["a", "b"])

        trailing = viewer.display_blocks([
            clip("a", "2026-09-22T13:00:00Z"),
            clip("b", "2026-09-22T13:01:00Z"),
            clip("gap", "2026-09-22T13:02:30Z", transcript="  "),
        ])
        self.assertEqual(trailing[0]["chunk_ids"], ["a", "b"])
        self.assert_chunk(trailing[1], "gap")

        bracketed = viewer.display_blocks([
            clip("a", "2026-09-22T13:00:00Z", duration=60),
            clip("gap", "2026-09-22T13:01:00Z", duration=30, transcript=""),
            clip("b", "2026-09-22T13:01:30Z"),
        ])
        self.assertEqual(bracketed[0]["chunk_ids"], ["a", "gap", "b"])
        self.assertEqual(bracketed[0]["clip_count"], 3)

        stretched = viewer.display_blocks([
            clip("a", "2026-09-22T13:00:00Z", duration=60),
            clip("g1", "2026-09-22T13:01:10Z", transcript=""),
            clip("g2", "2026-09-22T13:02:10Z", transcript=""),
            clip("b", "2026-09-22T13:04:30Z"),
        ])
        self.assertEqual([block["chunk_id"] for block in stretched], ["a", "g1", "g2", "b"])

    def test_quiet_hours_do_not_split_a_short_gap(self):
        blocks = viewer.display_blocks([
            clip("a", "2026-09-22T01:59:30Z", duration=60),
            clip("b", "2026-09-22T02:00:40Z"),
        ])
        self.assertEqual(blocks[0]["chunk_ids"], ["a", "b"])

    def test_local_day_boundary_splits_even_a_short_gap(self):
        blocks = viewer.display_blocks([
            clip("a", "2026-09-22T03:59:40Z", duration=15),
            clip("b", "2026-09-22T04:00:10Z", duration=15),
        ])
        self.assertEqual([block["chunk_id"] for block in blocks], ["a", "b"])

    def test_dst_uses_elapsed_time(self):
        apart = viewer.display_blocks([
            clip("a", "2026-11-01T05:58:00Z", duration=60),
            clip("b", "2026-11-01T06:30:00Z"),
        ])
        self.assertEqual([block["kind"] for block in apart], ["chunk", "chunk"])
        across = viewer.display_blocks([
            clip("a", "2026-11-01T05:58:00Z", duration=60, transcript="before the fold"),
            clip("b", "2026-11-01T06:00:30Z", transcript="after the fold"),
        ])
        self.assertEqual(across[0]["chunk_ids"], ["a", "b"])

    def test_invalid_timestamp_is_its_own_row_and_input_is_unchanged(self):
        chunks = [
            clip("b", "2026-09-22T13:01:00Z"),
            {"id": "bad", "started": "nope", "duration": 60, "transcript": "lost"},
            clip("a", "2026-09-22T13:00:00Z"),
        ]
        original = copy.deepcopy(chunks)
        blocks = viewer.display_blocks(chunks)
        self.assertEqual(chunks, original)
        self.assertEqual(blocks[0]["chunk_ids"], ["a", "b"])
        self.assert_chunk(blocks[1], "bad")
        again = viewer.display_blocks(chunks)
        self.assertEqual(blocks[0]["id"], again[0]["id"])

    def test_every_chunk_appears_once(self):
        chunks = [
            clip("a", "2026-09-22T13:00:00Z"),
            clip("b", "2026-09-22T13:01:00Z"),
            clip("c", "2026-09-22T16:00:00Z"),
            clip("d", "2026-09-22T13:00:30Z", transcript=""),
        ]
        blocks = viewer.display_blocks(chunks)
        seen = []
        for block in blocks:
            if block["kind"] == "event":
                seen.extend(block["chunk_ids"])
            else:
                seen.append(block["chunk_id"])
        self.assertEqual(sorted(seen), ["a", "b", "c", "d"])
        self.assertEqual(len(seen), len(set(seen)))

    def test_sidebar_script_expands_without_loading_reviews(self):
        self.assertIn('id="flat-list"', viewer.APP)
        self.assertIn("display_blocks", viewer.JS)
        self.assertIn("aria-expanded", viewer.JS)
        self.assertIn("Event · ", viewer.JS)
        self.assertNotIn("innerHTML", viewer.JS)
        start = viewer.JS.find('toggle.addEventListener("click"')
        self.assertGreater(start, 0)
        handler = viewer.JS[start:start + 280]
        self.assertIn("renderList();", handler)
        self.assertNotIn("loadReview", handler)
        self.assertNotIn("/review", handler)


if __name__ == "__main__":
    unittest.main()
