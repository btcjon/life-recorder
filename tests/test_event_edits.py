import json
import os
import sqlite3
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_api
import event_edits
import event_summaries
import meetings
import viewer
from agent_support import ViewerCase
from receiver import Inbox

SECRET = "zephyr-quartz-transcript"
AUDIO = "/private/life-recorder-secret/clip.m4a"


def add_chunk(db, chunk_id, started, transcript=SECRET, duration=60.0):
    db.execute(
        """INSERT INTO chunks
           (id, sha256, device, started, duration, path, received, status, transcript)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (chunk_id, "b" * 64, "phone", started, duration, AUDIO, 0, "complete", transcript),
    )


class EditBehaviorTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.inbox = Inbox(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def _ids(self, *stamps):
        ids = []
        with self.inbox.connect() as db:
            for index, stamp in enumerate(stamps):
                chunk_id = "clip" + "abcdef"[index]
                add_chunk(db, chunk_id, stamp)
                ids.append(chunk_id)
        return ids

    def _events(self, day="2026-09-22"):
        payload = self.inbox.viewer_day(day)
        return [block for block in payload["display_blocks"] if block["kind"] == "event"]

    def _members(self):
        with self.inbox.connect() as db:
            rows = db.execute(
                """SELECT m.event_id, m.chunk_id FROM agent_event_members m
                   JOIN agent_events e ON e.id = m.event_id AND e.tombstoned = 0
                   ORDER BY m.chunk_id"""
            ).fetchall()
        grouped = {}
        for row in rows:
            grouped.setdefault(row["event_id"], set()).add(row["chunk_id"])
        return grouped

    def test_edited_id_stays_stable_when_title_and_boundaries_change(self):
        clips = self._ids(
            "2026-09-22T13:00:00Z",
            "2026-09-22T13:01:00Z",
            "2026-09-22T13:02:00Z",
            "2026-09-22T13:03:00Z",
        )
        created = self.inbox.save_event_edit({
            "title": "Planning",
            "start_chunk_id": clips[0],
            "end_chunk_id": clips[3],
            "expected_revision": 0,
        })
        updated = self.inbox.save_event_edit({
            "id": created["id"],
            "title": "Planning room",
            "start_chunk_id": clips[1],
            "end_chunk_id": clips[2],
            "expected_revision": created["revision"],
        })
        self.assertEqual(updated["id"], created["id"])
        self.assertTrue(updated["id"].startswith("edt_"))
        self.assertEqual(updated["revision"], 2)
        manual = next(event for event in self._events() if event["source"] == "manual")
        self.assertEqual(manual["id"], created["id"])
        self.assertEqual(manual["chunk_ids"], clips[1:3])
        self.assertEqual(manual["title"], "Planning room")

    def test_manual_boundaries_merge_and_split_ahead_of_automatic_groups(self):
        separate = self._ids(
            "2026-09-22T13:00:00Z",
            "2026-09-22T13:01:00Z",
            "2026-09-22T13:10:00Z",
            "2026-09-22T13:11:00Z",
        )
        auto = viewer.display_blocks([
            {"id": separate[0], "started": "2026-09-22T13:00:00Z", "duration": 60, "transcript": SECRET},
            {"id": separate[1], "started": "2026-09-22T13:01:00Z", "duration": 60, "transcript": SECRET},
            {"id": separate[2], "started": "2026-09-22T13:10:00Z", "duration": 60, "transcript": SECRET},
            {"id": separate[3], "started": "2026-09-22T13:11:00Z", "duration": 60, "transcript": SECRET},
        ])
        self.assertEqual(sum(block["kind"] == "event" for block in auto), 2)
        merged = self.inbox.save_event_edit({
            "title": "Both",
            "start_chunk_id": separate[0],
            "end_chunk_id": separate[3],
            "expected_revision": 0,
        })
        shown = self._events()
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0]["source"], "manual")
        self.assertEqual(shown[0]["chunk_ids"], separate)
        self.assertEqual(shown[0]["id"], merged["id"])
        self.assertGreaterEqual(len(shown[0]["suggestion"]["parts"]), 2)
        self.assertNotIn(SECRET, json.dumps(shown[0]["suggestion"]))
        self.assertEqual({frozenset(ids) for ids in self._members().values()}, {frozenset(separate)})

        self.inbox.save_event_edit({
            "id": merged["id"],
            "title": "Middle",
            "start_chunk_id": separate[1],
            "end_chunk_id": separate[2],
            "expected_revision": merged["revision"],
        })
        kinds = [
            (block["kind"], block.get("chunk_ids") or [block.get("chunk_id")])
            for block in self.inbox.viewer_day("2026-09-22")["display_blocks"]
        ]
        self.assertEqual(kinds[0], ("chunk", [separate[0]]))
        self.assertEqual(kinds[1][0], "event")
        self.assertEqual(kinds[1][1], separate[1:3])
        self.assertEqual(kinds[2], ("chunk", [separate[3]]))

    def test_new_clip_between_anchors_stays_inside_the_saved_event(self):
        first, last = self._ids("2026-09-22T13:00:00Z", "2026-09-22T13:02:00Z")
        created = self.inbox.save_event_edit({
            "title": "Hold",
            "start_chunk_id": first,
            "end_chunk_id": last,
            "expected_revision": 0,
        })
        with self.inbox.connect() as db:
            add_chunk(db, "clipmiddle", "2026-09-22T13:01:00Z")
            before = db.execute("SELECT id, revision, title, start_chunk_id FROM event_edits").fetchone()
            agent_api.reconcile(db, self.inbox._blocks_for(db))
        event = self._events()[0]
        self.assertEqual(event["id"], created["id"])
        self.assertEqual(event["chunk_ids"], [first, "clipmiddle", last])
        self.assertEqual(before["title"], "Hold")
        self.assertEqual(before["revision"], 1)
        self.assertEqual(before["id"], created["id"])

    def test_automatic_groups_do_not_bridge_a_manual_event(self):
        clips = self._ids(*(
            f"2026-09-22T13:0{index}:00Z" for index in range(6)
        ))
        self.inbox.save_event_edit({
            "title": "Middle",
            "start_chunk_id": clips[2],
            "end_chunk_id": clips[3],
            "expected_revision": 0,
        })
        blocks = self.inbox.viewer_day("2026-09-22")["display_blocks"]
        self.assertEqual(
            [(block["kind"], block.get("chunk_ids") or [block.get("chunk_id")]) for block in blocks],
            [("event", clips[:2]), ("event", clips[2:4]), ("event", clips[4:])],
        )
        self.assertEqual([block["source"] for block in blocks], ["suggestion", "manual", "suggestion"])

    def test_same_day_order_and_overlap_are_rejected(self):
        morning = self._ids("2026-09-22T13:00:00Z", "2026-09-22T13:01:00Z")
        with self.inbox.connect() as db:
            add_chunk(db, "nextday", "2026-09-23T04:30:00Z")
        saved = self.inbox.save_event_edit({
            "title": "Kept",
            "start_chunk_id": morning[0],
            "end_chunk_id": morning[1],
            "expected_revision": 0,
        })
        with self.assertRaises(event_edits.EditError) as crossed:
            self.inbox.save_event_edit({
                "title": "Overnight",
                "start_chunk_id": morning[0],
                "end_chunk_id": "nextday",
                "expected_revision": 0,
            })
        self.assertEqual(crossed.exception.payload["error"], "Events must stay on one day")
        with self.assertRaises(event_edits.EditError) as backwards:
            self.inbox.save_event_edit({
                "id": saved["id"],
                "title": "Backwards",
                "start_chunk_id": morning[1],
                "end_chunk_id": morning[0],
                "expected_revision": saved["revision"],
            })
        self.assertEqual(backwards.exception.payload["error"], "Boundaries are out of order")
        with self.inbox.connect() as db:
            add_chunk(db, "cliplater", "2026-09-22T13:02:00Z")
        with self.assertRaises(event_edits.EditError) as overlap:
            self.inbox.save_event_edit({
                "title": "Overlap",
                "start_chunk_id": morning[1],
                "end_chunk_id": "cliplater",
                "expected_revision": 0,
            })
        self.assertEqual(overlap.exception.payload["error"], "Boundaries overlap another edit")
        with self.inbox.connect() as db:
            row = db.execute("SELECT title, revision, end_chunk_id FROM event_edits").fetchone()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM event_edits").fetchone()[0], 1)
        self.assertEqual(row["title"], "Kept")
        self.assertEqual(row["revision"], 1)
        self.assertEqual(row["end_chunk_id"], morning[1])

    def test_stale_revision_does_not_write(self):
        clips = self._ids("2026-09-22T13:00:00Z", "2026-09-22T13:01:00Z", "2026-09-22T13:02:00Z")
        created = self.inbox.save_event_edit({
            "title": "First",
            "start_chunk_id": clips[0],
            "end_chunk_id": clips[1],
            "expected_revision": 0,
        })
        self.inbox.save_event_edit({
            "id": created["id"],
            "title": "Second",
            "start_chunk_id": clips[0],
            "end_chunk_id": clips[2],
            "expected_revision": 1,
        })
        with self.assertRaises(event_edits.EditError) as conflict:
            self.inbox.save_event_edit({
                "id": created["id"],
                "title": "Third",
                "start_chunk_id": clips[0],
                "end_chunk_id": clips[1],
                "expected_revision": 1,
            })
        self.assertEqual(conflict.exception.status, 409)
        self.assertEqual(conflict.exception.payload["revision"], 2)
        self.assertNotIn(SECRET, json.dumps(conflict.exception.payload))
        self.assertNotIn("clip", json.dumps(conflict.exception.payload))
        with self.inbox.connect() as db:
            row = db.execute("SELECT title, revision, end_chunk_id FROM event_edits").fetchone()
        self.assertEqual(row["title"], "Second")
        self.assertEqual(row["revision"], 2)
        self.assertEqual(row["end_chunk_id"], clips[2])

    def test_untouched_automatic_events_keep_their_identity(self):
        clips = self._ids("2026-09-22T13:00:00Z", "2026-09-22T13:01:00Z")
        auto = viewer.display_blocks([
            {"id": clips[0], "started": "2026-09-22T13:00:00Z", "duration": 60, "transcript": SECRET},
            {"id": clips[1], "started": "2026-09-22T13:01:00Z", "duration": 60, "transcript": SECRET},
        ])
        payload = self.inbox.viewer_day("2026-09-22")
        event = next(block for block in payload["display_blocks"] if block["kind"] == "event")
        self.assertEqual(event["id"], auto[0]["id"])
        self.assertEqual(event["chunk_ids"], auto[0]["chunk_ids"])
        self.assertEqual(event["source"], "suggestion")
        self.assertEqual(event["title"], "")
        self.assertEqual(event["revision"], None)
        self.assertTrue(payload["sessions"][0]["title"].startswith("Capture session"))
        self.assertEqual(payload["events"], [])
        with self.inbox.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM speech_events").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM event_edits").fetchone()[0], 0)
            agent_api.reconcile(db, self.inbox._blocks_for(db))
        self.assertEqual({frozenset(ids) for ids in self._members().values()}, {frozenset(clips)})

    def test_read_does_not_call_summary_or_replace_a_title(self):
        clips = self._ids("2026-09-22T13:00:00Z", "2026-09-22T13:01:00Z")
        created = self.inbox.save_event_edit({
            "title": "Planning",
            "start_chunk_id": clips[0],
            "end_chunk_id": clips[1],
            "expected_revision": 0,
        })
        key = event_summaries.fingerprint([(clips[0], SECRET), (clips[1], SECRET)])
        with self.inbox.connect() as db:
            db.execute(
                """INSERT INTO event_summaries
                   (cache_key, summary, coverage, status, generated_at, retry_at, error_code)
                   VALUES (?, ?, 'full', 'ready', 1, NULL, NULL)""",
                (key, "They compared notes."),
            )
        with mock.patch.object(event_summaries, "run_command", side_effect=AssertionError("model")):
            first = self._events()[0]
            with self.inbox.connect() as db:
                db.execute("UPDATE event_summaries SET summary=? WHERE cache_key=?", ("A regenerated line.", key))
            second = self._events()[0]
        self.assertEqual(first["title"], "Planning")
        self.assertEqual(first["summary"], "They compared notes.")
        self.assertEqual(second["title"], "Planning")
        self.assertEqual(second["summary"], "A regenerated line.")
        with self.inbox.connect() as db:
            stored = db.execute("SELECT title, revision FROM event_edits WHERE id=?", (created["id"],)).fetchone()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM event_summaries").fetchone()[0], 1)
        self.assertEqual(stored["title"], "Planning")
        self.assertEqual(stored["revision"], 1)
        narrowed = self.inbox.save_event_edit({
            "id": created["id"],
            "title": "Planning",
            "start_chunk_id": clips[0],
            "end_chunk_id": clips[1],
            "expected_revision": 1,
        })
        self.assertEqual(narrowed["title"], "Planning")

    def test_boundary_change_does_not_keep_the_previous_summary(self):
        clips = self._ids(
            "2026-09-22T13:00:00Z",
            "2026-09-22T13:01:00Z",
            "2026-09-22T13:02:00Z",
        )
        created = self.inbox.save_event_edit({
            "title": "Planning",
            "start_chunk_id": clips[0],
            "end_chunk_id": clips[2],
            "expected_revision": 0,
        })
        key = event_summaries.fingerprint([(clip, SECRET) for clip in clips])
        with self.inbox.connect() as db:
            db.execute(
                """INSERT INTO event_summaries
                   (cache_key, summary, coverage, status, generated_at)
                   VALUES (?, 'Wide summary.', 'full', 'ready', 1)""",
                (key,),
            )
        self.inbox.save_event_edit({
            "id": created["id"],
            "title": "Planning",
            "start_chunk_id": clips[0],
            "end_chunk_id": clips[1],
            "expected_revision": 1,
        })
        event = self._events()[0]
        self.assertEqual(event["title"], "Planning")
        self.assertNotEqual(event.get("summary"), "Wide summary.")
        with self.inbox.connect() as db:
            self.assertEqual(
                db.execute("SELECT summary FROM event_summaries").fetchone()["summary"],
                "Wide summary.",
            )

    def test_only_confirmed_names_are_shown(self):
        clips = self._ids("2026-09-22T13:00:00Z", "2026-09-22T13:01:00Z")
        with self.inbox.connect() as db:
            now = 1
            db.execute(
                "INSERT INTO people (id, name, created_at, updated_at) VALUES (?,?,?,?)",
                ("person-bea", "Bea", now, now),
            )
            db.execute(
                "INSERT INTO people (id, name, created_at, updated_at) VALUES (?,?,?,?)",
                ("person-auto", "Zephyr Quint", now, now),
            )
            for person_id, chunk_id, source in (
                ("person-bea", clips[0], "confirmed"),
                ("person-auto", clips[1], "automatic"),
            ):
                db.execute(
                    """INSERT INTO speaker_turns
                       (id, run_id, chunk_id, speaker_key, started, ended, person_id, label_source)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), str(uuid.uuid4()), chunk_id, "S", 0, 1, person_id, source),
                )
        event = self._events()[0]
        self.assertEqual([person["name"] for person in event["speakers"]], ["Bea"])
        self.assertTrue(event["speakers"][0]["confirmed"])
        self.assertNotIn("Zephyr", json.dumps(event["speakers"]))

    def test_migration_on_a_private_copy_is_idempotent(self):
        clips = self._ids("2026-09-22T13:00:00Z", "2026-09-22T13:01:00Z")
        created = self.inbox.save_event_edit({
            "title": "Planning",
            "start_chunk_id": clips[0],
            "end_chunk_id": clips[1],
            "expected_revision": 0,
        })
        with self.inbox.connect() as db:
            before_events = db.execute("SELECT COUNT(*) FROM agent_events").fetchone()[0]
            before_version = db.execute("PRAGMA user_version").fetchone()[0]
        copy_path = Path(self.temp.name) / "rehearsal.sqlite3"
        source = sqlite3.connect(self.inbox.db)
        copied = sqlite3.connect(copy_path)
        source.backup(copied)
        source.close()
        copied.close()
        for _ in range(2):
            db = sqlite3.connect(copy_path)
            db.row_factory = sqlite3.Row
            meetings.migrate_schema(db)
            db.commit()
            db.close()
        db = sqlite3.connect(copy_path)
        db.row_factory = sqlite3.Row
        self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], before_version)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM agent_events").fetchone()[0], before_events)
        row = db.execute("SELECT id, title, revision FROM event_edits").fetchone()
        self.assertEqual(row["id"], created["id"])
        self.assertEqual(row["title"], "Planning")
        self.assertEqual(row["revision"], 1)
        db.close()

    def test_viewer_script_edits_without_transcript_markup(self):
        self.assertIn("/v1/event-edits", viewer.JS)
        self.assertIn("expected_revision", viewer.JS)
        self.assertIn("Edit event title and boundaries", viewer.JS)
        self.assertIn("Suggested:", viewer.JS)
        self.assertNotIn("innerHTML", viewer.JS)
        self.assertIn("person.confirmed", viewer.JS)
        self.assertIn(".event-edit", viewer.CSS)


class EditRouteTests(ViewerCase):
    def _seed(self):
        with self.inbox.connect() as db:
            add_chunk(db, "clipa", "2026-09-22T13:00:00Z")
            add_chunk(db, "clipb", "2026-09-22T13:01:00Z")

    def test_machine_credential_cannot_mutate_and_nothing_leaks(self):
        self._seed()
        with self.inbox.connect() as db:
            before = db.execute("SELECT search_generation FROM agent_api_state").fetchone()[0]
        status, body, _ = self.agent("/v1/event-edits", {
            "title": "Nope",
            "start_chunk_id": "clipa",
            "end_chunk_id": "clipb",
            "expected_revision": 0,
            "transcript": SECRET,
        })
        self.assertEqual(status, 403)
        self.assertNotIn(SECRET.encode(), body)
        self.assertNotIn(b"clip.m4a", body)
        self.assertNotIn(AUDIO.encode(), body)
        with self.inbox.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM event_edits").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT search_generation FROM agent_api_state").fetchone()[0], before)

    def test_human_route_rejects_conflict_without_leaking_recording_text(self):
        self._seed()
        payload = {
            "title": "Planning",
            "start_chunk_id": "clipa",
            "end_chunk_id": "clipb",
            "expected_revision": 0,
        }
        status, body, _ = self.request("POST", "/v1/event-edits", {
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
            "Host": "127.0.0.1",
        }, json.dumps(payload).encode())
        self.assertEqual(status, 200)
        saved = json.loads(body)
        self.assertEqual(set(saved), {"id", "revision", "title", "start_chunk_id", "end_chunk_id"})
        self.assertNotIn(SECRET, body.decode())
        self.assertNotIn("m4a", body.decode())
        status, denied, _ = self.request("POST", "/v1/event-edits", {
            "Content-Type": "application/json",
            "Host": "127.0.0.1",
        }, json.dumps(payload).encode())
        self.assertEqual(status, 401)
        self.assertNotIn(SECRET.encode(), denied)
        stale = dict(payload, id=saved["id"], title="Third", expected_revision=0)
        status, conflict, _ = self.request("POST", "/v1/event-edits", {
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
            "Host": "127.0.0.1",
        }, json.dumps(stale).encode())
        self.assertEqual(status, 409)
        self.assertNotIn(SECRET.encode(), conflict)
        self.assertNotIn(b"m4a", conflict)
        with self.inbox.connect() as db:
            self.assertEqual(db.execute("SELECT title FROM event_edits").fetchone()["title"], "Planning")


if __name__ == "__main__":
    unittest.main()
