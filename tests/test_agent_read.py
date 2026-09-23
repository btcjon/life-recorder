import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_support import ViewerCase, add_chunk, add_person, sync


class ReadTests(ViewerCase):
    def event_id(self):
        with self.inbox.connect() as db:
            return db.execute("SELECT id FROM agent_events WHERE tombstoned=0").fetchone()[0]

    def test_overview_limits_and_transcript_pages_rebuild_the_source(self):
        text = ("héllo 😀 " * 800).strip()
        with self.inbox.connect() as db:
            first = add_chunk(db, "2026-09-22T14:00:00Z", text)
            add_chunk(db, "2026-09-22T14:01:00Z", "second clip words")
            add_chunk(db, "2026-09-22T14:02:00Z", "third clip words")
            add_chunk(db, "2026-09-22T14:03:00Z", "fourth clip words")
            add_person(db, "Jon Bennett", first, confirmed=True)
        sync(self.inbox)
        status, body, _ = self.agent("/v1/events/" + self.event_id() + "/read", {"mode": "overview"})
        overview = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(overview["truncated"])
        self.assertLessEqual(len(overview["excerpts"]), 3)
        self.assertLessEqual(sum(len(item["text"]) for item in overview["excerpts"]), 2000)
        self.assertIsNone(overview["next_cursor"])
        self.assertEqual(overview["excerpts"][0]["speaker"], "Jon Bennett")
        self.assertNotIn("audio", overview)
        collected = []
        cursor = None
        while True:
            payload = {"mode": "transcript", "max_chars": 500}
            if cursor:
                payload["cursor"] = cursor
            status, body, _ = self.agent("/v1/events/" + self.event_id() + "/read", payload)
            self.assertEqual(status, 200)
            page = json.loads(body)
            self.assertLessEqual(len(body), 16 * 1024)
            collected.extend(item["text"] for item in page["excerpts"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        self.assertEqual("".join(collected), text + "second clip words" + "third clip words" + "fourth clip words")

    def test_alias_resolves_and_stale_cursor_conflicts(self):
        with self.inbox.connect() as db:
            add_chunk(db, "2026-09-22T14:00:00Z", "one")
            add_chunk(db, "2026-09-22T14:01:00Z", "two")
            third = add_chunk(db, "2026-09-22T16:00:00Z", "three")
            fourth = add_chunk(db, "2026-09-22T16:01:00Z", "four")
        sync(self.inbox)
        with self.inbox.connect() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM agent_events WHERE tombstoned=0 ORDER BY created_seq")]
            db.execute("UPDATE chunks SET started=? WHERE id=?", ("2026-09-22T14:02:00Z", third))
            db.execute("UPDATE chunks SET started=? WHERE id=?", ("2026-09-22T14:03:00Z", fourth))
        sync(self.inbox)
        alias = ids[1]
        status, body, _ = self.agent("/v1/events/" + alias + "/read", {"mode": "overview"})
        payload = json.loads(body)
        self.assertEqual(payload["requested_id"], alias)
        self.assertEqual(payload["id"], ids[0])
        status, first, _ = self.agent("/v1/events/" + ids[0] + "/read", {"mode": "transcript", "max_chars": 2})
        cursor = json.loads(first)["next_cursor"]
        with self.inbox.connect() as db:
            db.execute("UPDATE chunks SET transcript=? WHERE id=?", ("changed", db.execute("SELECT id FROM chunks LIMIT 1").fetchone()[0]))
        sync(self.inbox)
        status, _, _ = self.agent("/v1/events/" + ids[0] + "/read", {"mode": "transcript", "max_chars": 2, "cursor": cursor})
        self.assertEqual(status, 409)
        status, _, _ = self.agent("/v1/events/evt_missing/read", {"mode": "overview"})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
