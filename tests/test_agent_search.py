import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_support import IndexCase, ViewerCase, add_chunk, add_person, sync


class SearchTests(ViewerCase):
    def seed(self):
        with self.inbox.connect() as db:
            morning = add_chunk(db, "2026-09-15T14:00:00Z", "paint the hallway before noon")
            add_chunk(db, "2026-09-15T14:01:00Z", "bring the ladder")
            add_person(db, "Jon Bennett", morning, confirmed=True)
            later = add_chunk(db, "2026-09-15T18:00:00Z", "the server deploy failed")
            add_chunk(db, "2026-09-15T18:01:00Z", "restart it after lunch")
            add_person(db, "Maybe Person", later, confirmed=False)
            add_chunk(db, "2026-11-01T05:30:00Z", "before the clock change")
            add_chunk(db, "2026-11-01T05:31:00Z", "still eastern daylight")
        sync(self.inbox)

    def test_query_person_and_time_apply_to_the_same_clip(self):
        self.seed()
        status, body, _ = self.agent("/v1/search", {
            "query": "hallway",
            "from": "2026-09-15T09:00:00-04:00",
            "to": "2026-09-15T12:00:00-04:00",
            "person": "jon bennett",
        })
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(payload["events"][0]["people"][0]["name"], "Jon Bennett")
        self.assertIn("hallway", payload["events"][0]["preview"])
        status, body, _ = self.agent("/v1/search", {"query": "hallway", "person": "Maybe Person"})
        self.assertEqual(json.loads(body)["events"], [])
        status, body, _ = self.agent("/v1/search", {"query": "server", "include_unconfirmed": True, "person": "Maybe Person"})
        self.assertEqual(len(json.loads(body)["events"]), 1)

    def test_query_text_is_literal(self):
        self.seed()
        status, body, _ = self.agent("/v1/search", {"query": "OR 1=1; DROP TABLE chunks"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["events"], [])
        status, body, _ = self.agent("/v1/search", {"query": "???"})
        self.assertEqual(status, 400)

    def test_dst_offset_boundary_and_newest_order(self):
        self.seed()
        status, body, _ = self.agent("/v1/search", {
            "from": "2026-11-01T01:00:00-04:00",
            "to": "2026-11-01T01:45:00-04:00",
        })
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["events"]), 1)
        self.assertIn("clock", payload["events"][0]["preview"])
        status, body, _ = self.agent("/v1/search", {"from": "2026-09-15T00:00:00-04:00", "to": "2026-09-16T00:00:00-04:00"})
        starts = [event["start"] for event in json.loads(body)["events"]]
        self.assertEqual(starts, sorted(starts, reverse=True))

    def test_pagination_and_naive_timestamp(self):
        self.seed()
        status, body, _ = self.agent("/v1/search", {"limit": 1})
        page = json.loads(body)
        self.assertEqual(len(page["events"]), 1)
        self.assertIsNotNone(page["next_cursor"])
        status, body, _ = self.agent("/v1/search", {"limit": 1, "cursor": page["next_cursor"]})
        second = json.loads(body)
        self.assertNotEqual(page["events"][0]["id"], second["events"][0]["id"])
        status, _, _ = self.agent("/v1/search", {"from": "2026-09-15T10:00:00"})
        self.assertEqual(status, 400)

    def test_search_does_not_write_the_index(self):
        self.seed()
        before = self.generation()
        self.agent("/v1/search", {"query": "hallway"})
        self.assertEqual(self.generation(), before)
        with self.inbox.connect() as db:
            self.inbox.viewer_day("2026-09-15")
        self.assertEqual(self.generation(), before)



    def test_speaker_label_and_rename_invalidate_the_search_cursor(self):
        self.seed()
        status, body, _ = self.agent("/v1/search", {"limit": 1})
        cursor = json.loads(body)["next_cursor"]
        self.assertIsNotNone(cursor)
        before = self.generation()
        with self.inbox.connect() as db:
            turn = db.execute("SELECT id FROM speaker_turns WHERE label_source='confirmed'").fetchone()
            person = db.execute("SELECT id FROM people WHERE name='Jon Bennett'").fetchone()
        self.assertTrue(self.inbox.label_turn(turn[0], person[0]))
        self.assertEqual(self.generation(), before)
        with self.inbox.connect() as db:
            other = str(__import__('uuid').uuid4())
            db.execute("INSERT INTO people (id, name, created_at, updated_at) VALUES (?,?,?,?)", (other, "Ada", 0, 0))
        self.assertTrue(self.inbox.label_turn(turn[0], other))
        self.assertGreater(self.generation(), before)
        status, _, _ = self.agent("/v1/search", {"limit": 1, "cursor": cursor})
        self.assertEqual(status, 409)
        renamed = self.generation()
        self.inbox.rename_person(other, "Ada Lovelace")
        self.assertGreater(self.generation(), renamed)

    def test_legacy_unindexed_fts_migrates_without_dropping_hits(self):
        with self.inbox.connect() as db:
            add_chunk(db, "2026-09-15T14:00:00Z", "paint the hallway before noon")
            add_chunk(db, "2026-09-15T14:01:00Z", "bring the ladder")
        sync(self.inbox)
        with self.inbox.connect() as db:
            event_id = db.execute("SELECT id FROM agent_events WHERE tombstoned=0").fetchone()[0]
            rows = list(db.execute("SELECT chunk_id, body FROM agent_transcripts"))
            db.execute("DROP TABLE agent_transcript_fts")
            db.execute("DROP TABLE agent_transcripts")
            db.execute("""CREATE VIRTUAL TABLE agent_transcript_fts USING fts5(
                chunk_id UNINDEXED, body, tokenize='unicode61')""")
            db.executemany("INSERT INTO agent_transcript_fts (chunk_id, body) VALUES (?, ?)", rows)
            db.execute("UPDATE agent_api_state SET schema_version=1")
        sync(self.inbox)
        with self.inbox.connect() as db:
            plan = " ".join(
                row[3] for row in db.execute(
                    "EXPLAIN QUERY PLAN SELECT body FROM agent_transcripts WHERE chunk_id=?",
                    (rows[0][0],),
                )
            )
            self.assertIn("SEARCH", plan)
            self.assertNotIn("SCAN", plan)
            self.assertEqual(db.execute("SELECT id FROM agent_events WHERE tombstoned=0").fetchone()[0], event_id)
            self.assertEqual(db.execute("SELECT body FROM agent_transcripts WHERE chunk_id=?", (rows[0][0],)).fetchone()[0], rows[0][1])
        status, body, _ = self.agent("/v1/search", {"query": "hallway"})
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["events"]), 1)

    def test_reconcile_chunk_lookup_uses_the_keyed_table(self):
        with self.inbox.connect() as db:
            chunk_id = add_chunk(db, "2026-09-15T14:00:00Z", "paint the hallway before noon")
            add_chunk(db, "2026-09-15T14:01:00Z", "bring the ladder")
        sync(self.inbox)
        with self.inbox.connect() as db:
            plan = " ".join(row[3] for row in db.execute(
                "EXPLAIN QUERY PLAN SELECT body FROM agent_transcripts WHERE chunk_id IN (?,?)",
                (chunk_id, "missing"),
            ))
            self.assertIn("SEARCH", plan)
            self.assertNotIn("agent_transcript_fts", plan)
            fts_plan = " ".join(row[3] for row in db.execute(
                "EXPLAIN QUERY PLAN SELECT rowid FROM agent_transcript_fts WHERE agent_transcript_fts MATCH ?",
                ('"hallway"',),
            ))
            self.assertIn("INDEX 0:M", fts_plan)

if __name__ == "__main__":
    unittest.main()
