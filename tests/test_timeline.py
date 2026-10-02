import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
from receiver import Inbox
import event_edits
import timeline


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.inbox = Inbox(Path(self.temp.name))
        with self.inbox.connect() as db:
            timeline.ensure_schema(db)
            for index in range(4):
                db.execute("""INSERT INTO chunks(id,sha256,device,started,duration,path,received,status,transcript)
                    VALUES (?,?,?,?,?,?,?,'complete',?)""",
                    ("clip" + str(index), "hash", "device", f"2026-10-01T12:0{index}:00.000Z", 60,
                     "private audio path", 1000, "private transcript"))

    def tearDown(self):
        self.temp.cleanup()

    def rows(self, db):
        return db.execute("SELECT id,started,duration,status,transcript FROM chunks ORDER BY started,id").fetchall()

    def publish(self, db):
        return timeline.publish_suggestions(db, fingerprint="f", rows=self.rows(db),
            segments=[{"start_clip_id": "clip0", "end_clip_id": "clip3", "title": "Project"}],
            model="grok-4.7", prompt_version="v1", now=1000)[0]

    def test_reads_are_persisted_metadata_only(self):
        with self.inbox.connect() as db:
            self.publish(db)
            changes = db.total_changes
            report = timeline.list_timeline(db, day="2026-10-01")
            self.assertEqual(db.total_changes, changes)
            self.assertEqual(len(report["suggestions"]), 1)
            self.assertNotIn("private transcript", json.dumps(report))
            self.assertEqual(timeline.list_timeline(db, day="2026-10-02")["suggestions"], [])

    def test_accept_split_merge_preserve_lineage_and_human_override(self):
        with self.inbox.connect() as db:
            suggestion = self.publish(db)
            accepted = timeline.decide_suggestion(db, suggestion, 1, True)
            edit = accepted["edit"]
            split = timeline.split_edit(db, edit["id"], edit["revision"], "clip2", ["First", "Second"])
            self.assertEqual(split["source_ids"], [suggestion])
            self.assertTrue(split["human_override"])
            merged = timeline.merge_edits(db, [r["id"] for r in split["edits"]],
                [r["revision"] for r in split["edits"]], "Merged")
            self.assertEqual(merged["source_ids"], [suggestion])
            self.assertEqual(merged["edit"]["start_chunk_id"], "clip0")
            self.assertEqual(merged["edit"]["end_chunk_id"], "clip3")
            report = timeline.list_timeline(db)
            self.assertEqual(report["suggestions"], [])
            self.assertTrue(report["items"][0]["human_override"])
            # A regenerated model proposal never overwrites an accepted human span.
            self.assertEqual(timeline.publish_suggestions(db, fingerprint="new", rows=self.rows(db),
                segments=[{"start_clip_id": "clip0", "end_clip_id": "clip3", "title": "Overwrite"}],
                model="grok-4.7", prompt_version="v1"), [])

    def test_revision_source_and_failed_merge_are_atomic(self):
        with self.inbox.connect() as db:
            suggestion = self.publish(db)
            with self.assertRaises(event_edits.EditError) as caught:
                timeline.decide_suggestion(db, suggestion, 0, True)
            self.assertEqual(caught.exception.status, 409)
            db.execute("UPDATE chunks SET transcript='changed' WHERE id='clip1'")
            with self.assertRaises(event_edits.EditError):
                timeline.decide_suggestion(db, suggestion, 1, True)
            self.assertEqual(event_edits.load(db), [])
            db.execute("UPDATE chunks SET transcript='private transcript' WHERE id='clip1'")
            edit = timeline.decide_suggestion(db, suggestion, 1, True)["edit"]
            split = timeline.split_edit(db, edit["id"], edit["revision"], "clip2")["edits"]
            before = event_edits.load(db)
            with self.assertRaises(event_edits.EditError):
                timeline.merge_edits(db, [r["id"] for r in split], [r["revision"] for r in split], "")
            self.assertEqual(event_edits.load(db), before)

    def test_new_interleaved_upload_invalidates_old_topic_source(self):
        with self.inbox.connect() as db:
            suggestion = self.publish(db)
            db.execute("""INSERT INTO chunks(id,sha256,device,started,duration,path,received,status)
                VALUES ('late','hash','device','2026-10-01T12:01:30.000Z',20,'private',1001,'pending')""")
            with self.assertRaises(event_edits.EditError) as caught:
                timeline.decide_suggestion(db, suggestion, 1, True)
            self.assertEqual(caught.exception.status, 409)

    def test_rejected_suggestion_is_revisioned(self):
        with self.inbox.connect() as db:
            suggestion = self.publish(db)
            result = timeline.decide_suggestion(db, suggestion, 1, False)
            self.assertEqual(result["suggestion"]["state"], "rejected")
            self.assertEqual(result["suggestion"]["revision"], 2)
            self.assertEqual(event_edits.load(db), [])
