import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
from receiver import Inbox
import boundary_suggestions as boundaries
import event_edits
import timeline


class ParticipantBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.inbox = Inbox(Path(self.temp.name))
        with self.inbox.connect() as db:
            timeline.ensure_schema(db)
            for index in range(4):
                self.clip(db, "clip" + str(index), f"2026-10-01T12:0{index}:00.000Z")
                self.person(db, "clip" + str(index), "alice" if index < 2 else "bob")

    def tearDown(self):
        self.temp.cleanup()

    def clip(self, db, identifier, started, device="phone", status="complete"):
        db.execute("""INSERT INTO chunks(id,sha256,device,started,duration,path,received,status,transcript)
            VALUES (?,?,?,?,60,'private',1000,?,'private discussion')""", (identifier, "hash", device, started, status))

    def person(self, db, clip, person, source="confirmed"):
        db.execute("""INSERT INTO speaker_turns(id,run_id,chunk_id,speaker_key,started,ended,person_id,label_source)
            VALUES (?,?,?,'s',0,30,?,?)""", (clip + person, "run", clip, person, source))

    def proposed(self, db):
        return db.execute("SELECT * FROM topic_suggestions WHERE state='proposed' ORDER BY start_chunk_id").fetchall()

    def test_participant_change_is_proposal_not_automatic_event(self):
        with self.inbox.connect() as db:
            result = boundaries.refresh(db)
            self.assertEqual(result["suggestion_count"], 2)
            self.assertEqual(event_edits.load(db), [])
            rows = self.proposed(db)
            self.assertEqual([(r["start_chunk_id"], r["end_chunk_id"]) for r in rows], [("clip0", "clip1"), ("clip2", "clip3")])
            self.assertTrue(all(r["model"] == boundaries.MODEL for r in rows))
            self.assertTrue(all(r["title"] == "Participant change candidate" for r in rows))
            self.assertNotIn("private discussion", json.dumps(timeline.list_timeline(db)))

    def test_unknown_unconfirmed_and_overlapping_sets_do_not_split(self):
        with self.inbox.connect() as db:
            db.execute("UPDATE speaker_turns SET label_source='automatic' WHERE chunk_id='clip1'")
            self.assertEqual(boundaries.refresh(db)["suggestion_count"], 0)
            db.execute("UPDATE speaker_turns SET label_source='confirmed'")
            self.person(db, "clip2", "alice")
            self.person(db, "clip3", "alice")
            self.assertEqual(boundaries.refresh(db)["suggestion_count"], 0)

    def test_human_saved_boundary_protects_source_group(self):
        with self.inbox.connect() as db:
            event_edits.save(db, {"title": "Human", "start_chunk_id": "clip0", "end_chunk_id": "clip3", "expected_revision": 0},
                             db.execute("SELECT id,started,duration,transcript FROM chunks").fetchall())
            before = event_edits.load(db)
            self.assertEqual(boundaries.refresh(db)["suggestion_count"], 0)
            self.assertEqual(event_edits.load(db), before)

    def test_late_clips_preserve_full_partition_coverage_and_unchanged_is_noop(self):
        with self.inbox.connect() as db:
            boundaries.refresh(db)
            self.assertEqual(boundaries.refresh(db)["changes"], 0)
            self.clip(db, "late", "2026-10-01T12:01:00.000Z")
            # A non-overlapping late clip at a gap inserts into the same group.
            db.execute("UPDATE chunks SET duration=20 WHERE id='clip1'")
            db.execute("UPDATE chunks SET started='2026-10-01T12:01:20.000Z',duration=40 WHERE id='late'")
            self.person(db, "late", "alice")
            boundaries.refresh(db)
            rows = self.proposed(db)
            expected = ["clip0", "clip1", "late", "clip2", "clip3"]
            self.assertTrue(rows)
            for row in rows:
                self.assertEqual(json.loads(row["source_ids"]), expected)
            positions = {identifier: index for index, identifier in enumerate(expected)}
            covered = []
            for row in rows:
                covered.extend(expected[positions[row["start_chunk_id"]]:positions[row["end_chunk_id"]] + 1])
            self.assertEqual(covered, expected)
            self.assertEqual(boundaries.refresh(db)["changes"], 0)

    def test_device_day_gap_and_pending_clips_break_groups(self):
        with self.inbox.connect() as db:
            db.execute("UPDATE chunks SET device='other' WHERE id IN ('clip2','clip3')")
            self.assertEqual(boundaries.refresh(db)["suggestion_count"], 0)
            db.execute("UPDATE chunks SET device='phone',started='2026-10-02T12:02:00Z' WHERE id='clip2'")
            self.assertEqual(boundaries.refresh(db)["suggestion_count"], 0)
            db.execute("UPDATE chunks SET started='2026-10-01T12:02:00.000Z',status='pending' WHERE id='clip2'")
            self.assertEqual(boundaries.refresh(db)["suggestion_count"], 0)

    def test_removed_confirmation_supersedes_old_local_candidates(self):
        with self.inbox.connect() as db:
            boundaries.refresh(db)
            db.execute("UPDATE speaker_turns SET label_source='automatic' WHERE chunk_id='clip2'")
            boundaries.refresh(db)
            self.assertEqual(self.proposed(db), [])
            self.assertEqual(boundaries.refresh(db)["changes"], 0)
