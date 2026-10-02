import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_support import IndexCase, add_chunk, sync
from agent_api.search import search_events
from agent_api.read import read_event
from agent_api.errors import AgentError


class EvidenceTests(IndexCase):
    def test_preview_word_budget_retains_late_hit(self):
        self.seed("a " * 100 + "budget approval " + "b " * 200)
        event = search_events(self.inbox.db, {"query": "budget"})["events"][0]
        self.assertIn("budget", event["preview"])
        self.assertLessEqual(len(event["preview"].split()), 60)

    def seed(self, text):
        with self.inbox.connect() as db:
            first = add_chunk(db, "2026-10-02T14:00:00Z", text)
            add_chunk(db, "2026-10-02T14:01:00Z", "Next recording.")
        sync(self.inbox)
        return first

    def test_late_unicode_match_anchor_and_lossless_continuation(self):
        text = "😀 introductory words " * 100 + "café budget approval " + "tail " * 200
        chunk = self.seed(text)
        result = search_events(self.inbox.db, {"query": "cafe budget"})["events"][0]
        match = result["match"]
        self.assertEqual(match["chunk_id"], chunk)
        self.assertIn("café budget", match["text"])
        self.assertEqual(match["text"], text[match["start_offset"]:match["end_offset"]])
        self.assertEqual(match["anchor"]["offset"], text.index("café"))
        self.assertEqual(match["attribution"], "unknown")
        request = {"mode": "transcript", "anchor": match["anchor"], "context_before": 15, "max_chars": 31}
        collected = []
        while True:
            page = read_event(self.inbox.db, result["id"], request)
            self.assertLessEqual(len(json.dumps(page).encode()), 16384)
            for item in page["excerpts"]:
                source = text if item["chunk_id"] == chunk else "Next recording."
                self.assertEqual(item["text"], source[item["start_offset"]:item["end_offset"]])
                self.assertEqual(item["attribution"], "unknown")
                collected.append(item["text"])
            if not page["next_cursor"]:
                break
            request["cursor"] = page["next_cursor"]
        self.assertEqual("".join(collected), text[text.index("café")-15:] + "Next recording.")

    def test_invalid_stale_and_changed_cursor_anchor(self):
        chunk = self.seed("prefix budget " + "tail " * 100)
        event = search_events(self.inbox.db, {"query": "budget"})["events"][0]
        anchor = event["match"]["anchor"]
        for bad in [{**anchor, "offset": True}, {**anchor, "offset": -1},
                    {**anchor, "offset": 999999}, {**anchor, "chunk_id": "foreign"}]:
            with self.assertRaises(AgentError) as error:
                read_event(self.inbox.db, event["id"], {"mode": "transcript", "anchor": bad})
            self.assertEqual(error.exception.status, 400)
        request = {"mode": "transcript", "anchor": anchor, "max_chars": 20}
        page = read_event(self.inbox.db, event["id"], request)
        with self.assertRaises(AgentError) as error:
            read_event(self.inbox.db, event["id"], {**request, "anchor": {**anchor,"offset": 0}, "cursor":page["next_cursor"]})
        self.assertEqual(error.exception.status, 400)
        with self.inbox.connect() as db:
            db.execute("UPDATE chunks SET transcript=? WHERE id=?", ("changed budget", chunk))
        sync(self.inbox)
        with self.assertRaises(AgentError) as error:
            read_event(self.inbox.db, event["id"], request)
        self.assertEqual(error.exception.status, 409)

    def test_multibyte_response_trimming_keeps_offsets(self):
        text = "budget " + "😀" * 10000
        chunk = self.seed(text)
        event = search_events(self.inbox.db, {"query": "budget"})["events"][0]
        page = read_event(self.inbox.db, event["id"], {"mode": "transcript", "max_chars": 4000})
        self.assertLessEqual(len(json.dumps(page).encode()), 16384)
        excerpt = page["excerpts"][0]
        self.assertEqual(excerpt["text"], text[excerpt["start_offset"]:excerpt["end_offset"]])
        resumed = read_event(self.inbox.db, event["id"], {"mode": "transcript", "max_chars": 4000,"cursor":page["next_cursor"]})
        self.assertEqual(resumed["excerpts"][0]["start_offset"], excerpt["end_offset"])

    def test_search_shrinking_and_anchored_unicode_continuation(self):
        from datetime import datetime, timedelta, timezone
        sources = {}
        with self.inbox.connect() as db:
            for index in range(10):
                stamp = datetime(2026, 10, 2, tzinfo=timezone.utc) + timedelta(hours=2*index)
                text = "😀" * 2500 + " budget " + "界" * 4000
                sources[add_chunk(db, stamp.isoformat(), text)] = text
                sources[add_chunk(db, (stamp+timedelta(minutes=1)).isoformat(), "suffix recording")] = "suffix recording"
        sync(self.inbox)
        result = search_events(self.inbox.db, {"query": "budget", "limit": 10})
        self.assertEqual(len(result["events"]), 10)
        self.assertLessEqual(len(json.dumps(result).encode()), 16384)
        for event in result["events"]:
            match = event["match"]
            self.assertLess(len(match["text"]), 480)
            self.assertEqual(match["text"], sources[match["chunk_id"]][match["start_offset"]:match["end_offset"]])
            self.assertIn("budget", event["preview"])
        event = result["events"][0]
        request = {"mode":"transcript", "anchor":event["match"]["anchor"], "context_before":1000, "max_chars":4000}
        page = read_event(self.inbox.db, event["id"], request)
        self.assertLess(sum(len(e["text"]) for e in page["excerpts"]), 4000)
        for changed in [{"context_before":999}, {"max_chars":3999}]:
            with self.assertRaises(AgentError) as error:
                read_event(self.inbox.db, event["id"], {**request,**changed,"cursor":page["next_cursor"]})
            self.assertEqual(error.exception.status,400)
        collected = []
        while True:
            self.assertLessEqual(len(json.dumps(page).encode()),16384)
            for excerpt in page["excerpts"]:
                self.assertEqual(excerpt["text"],sources[excerpt["chunk_id"]][excerpt["start_offset"]:excerpt["end_offset"]])
                collected.append(excerpt["text"])
            if not page["next_cursor"]:
                break
            page = read_event(self.inbox.db,event["id"],{**request,"cursor":page["next_cursor"]})
        anchor=request["anchor"]
        self.assertEqual("".join(collected),sources[anchor["chunk_id"]][anchor["offset"]-1000:] + "suffix recording")
