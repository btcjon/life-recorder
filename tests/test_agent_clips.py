import json
from agent_support import IndexCase, ViewerCase, add_chunk, add_person, sync
from agent_api.read import read_clip, read_event
from agent_api.search import search_events
from agent_api.errors import AgentError


class ClipTests(IndexCase):
    def seed(self, text="😀 café budget approval " * 300):
        with self.inbox.connect() as db:
            chunk = add_chunk(db, "2026-10-02T14:00:00Z", text)
        sync(self.inbox)
        return chunk, text

    def test_singleton_search_and_filter_coverage(self):
        chunk, _ = self.seed()
        with self.inbox.connect() as db:
            add_person(db, "Confirmed Person", chunk)
        for body in ({"query": "budget"}, {"person": "Confirmed Person"},
                     {"from": "2026-10-02T14:00:00Z", "to": "2026-10-02T14:01:00Z"}):
            result = search_events(self.inbox.db, body)["events"]
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["kind"], "recording")
            self.assertEqual(result[0]["match"]["chunk_id"], chunk)
        self.assertEqual(search_events(self.inbox.db, {"query": "budget", "person": "Other"})["events"], [])

    def test_stable_citation_survives_grouping_and_identity_changes(self):
        chunk, text = self.seed()
        result = search_events(self.inbox.db, {"query": "budget"})["events"][0]
        citation = result["match"]["citation"]
        with self.inbox.connect() as db:
            add_chunk(db, "2026-10-02T14:01:00Z", "additional recording")
            add_person(db, "A Person", chunk)
        sync(self.inbox)
        grouped = search_events(self.inbox.db, {"query": "budget"})["events"][0]
        self.assertEqual(grouped["kind"], "event")
        self.assertEqual(grouped["match"]["citation"], citation)
        page = read_clip(self.inbox.db, chunk, {"mode": "transcript", "citation": citation})
        self.assertEqual(page["excerpts"][0]["text"], text[:2000])
        event_page = read_event(self.inbox.db, grouped["id"], {"mode": "transcript"})
        self.assertEqual(event_page["excerpts"][0]["citation"]["transcript_revision"], citation["transcript_revision"])

    def test_singleton_legacy_event_route_reads_and_detects_changed_anchor(self):
        chunk, text = self.seed()
        result = search_events(self.inbox.db, {"query": "budget"})["events"][0]
        body = {"mode": "transcript", "anchor": result["match"]["anchor"], "max_chars": 20, "context_before": 0}
        page = read_event(self.inbox.db, result["id"], body)
        self.assertEqual(page["id"], result["id"])
        self.assertEqual(page["kind"], "recording")
        self.assertEqual(page["excerpts"][0]["chunk_id"], chunk)
        self.assertTrue(page["next_cursor"])
        follow = read_event(self.inbox.db, result["id"], {**body, "cursor": page["next_cursor"]})
        self.assertEqual(follow["excerpts"][0]["start_offset"], page["excerpts"][0]["end_offset"])
        with self.inbox.connect() as db:
            db.execute("UPDATE chunks SET transcript=? WHERE id=?", ("changed budget text", chunk))
        with self.assertRaises(AgentError) as error:
            read_event(self.inbox.db, result["id"], body)
        self.assertEqual(error.exception.status, 409)

    def test_unicode_pages_lossless_and_bounded(self):
        chunk, text = self.seed("😀界 café budget " * 700)
        request = {"mode": "transcript", "max_chars": 4000}
        joined = ""
        while True:
            page = read_clip(self.inbox.db, chunk, request)
            self.assertLessEqual(len(json.dumps(page).encode()), 16384)
            excerpt = page["excerpts"][0]
            self.assertEqual(excerpt["text"], text[excerpt["start_offset"]:excerpt["end_offset"]])
            self.assertEqual(excerpt["citation"]["end_offset"], excerpt["end_offset"])
            joined += excerpt["text"]
            if not page["next_cursor"]:
                break
            request["cursor"] = page["next_cursor"]
        self.assertEqual(joined, text)

    def test_revision_deletion_invalid_and_cursor_scope(self):
        chunk, _ = self.seed()
        citation = search_events(self.inbox.db, {"query": "budget"})["events"][0]["match"]["citation"]
        request = {"mode": "transcript", "citation": citation, "context_before": 0, "max_chars": 20}
        page = read_clip(self.inbox.db, chunk, request)
        for bad in ({**citation, "end_offset": True}, {**citation, "chunk_id": "foreign"},
                    {**citation, "end_offset": 999999}, {**citation, "start_offset": -1}):
            with self.assertRaises(AgentError) as error:
                read_clip(self.inbox.db, chunk, {**request, "citation": bad})
            self.assertEqual(error.exception.status, 400)
        with self.assertRaises(AgentError) as error:
            read_clip(self.inbox.db, chunk, {**request, "max_chars": 21, "cursor": page["next_cursor"]})
        self.assertEqual(error.exception.status, 400)
        with self.inbox.connect() as db:
            db.execute("UPDATE chunks SET transcript='updated budget' WHERE id=?", (chunk,))
        # Reads check the live source even before index reconciliation.
        for read_request in (request, {**request, "cursor": page["next_cursor"]}):
            with self.assertRaises(AgentError) as error:
                read_clip(self.inbox.db, chunk, read_request)
            self.assertEqual(error.exception.status, 409)
        with self.inbox.connect() as db:
            db.execute("DELETE FROM chunks WHERE id=?", (chunk,))
        with self.assertRaises(AgentError) as error:
            read_clip(self.inbox.db, chunk, request)
        self.assertEqual(error.exception.status, 404)

    def test_turn_states_and_no_word_attribution(self):
        chunk, _ = self.seed()
        with self.inbox.connect() as db:
            add_person(db, "Confirmed", chunk)
            add_person(db, "Suggested", chunk, confirmed=False)
            db.execute("INSERT INTO speaker_turns(id,run_id,chunk_id,speaker_key,started,ended) VALUES('unknown','run',?,'S3',2,3)", (chunk,))
        page = read_clip(self.inbox.db, chunk, {"mode": "overview"})
        self.assertEqual({t["status"] for t in page["speaker_turns"]}, {"confirmed", "unconfirmed", "unknown"})
        self.assertIsNone(next(t for t in page["speaker_turns"] if t["status"] == "unconfirmed")["name"])
        self.assertTrue(all(t["attribution"] == "unknown" for t in page["speaker_turns"]))
        self.assertTrue(all(t["start_seconds"] <= t["end_seconds"] for t in page["speaker_turns"]))

    def test_large_identity_metadata_remains_bounded_and_explicit(self):
        chunk, _ = self.seed()
        with self.inbox.connect() as db:
            for i in range(40):
                add_person(db, "😀" * 200 + str(i), chunk)
        page = read_clip(self.inbox.db, chunk, {"mode": "transcript", "max_chars": 4000})
        self.assertLessEqual(len(json.dumps(page).encode()), 16384)
        self.assertLessEqual(len(page["speaker_turns"]), 32)
        self.assertTrue(page["speaker_turns_truncated"])
        self.assertGreater(len(page["excerpts"][0]["text"]), 0)

    def test_blank_transcript_and_unknown_fields_fail_closed(self):
        chunk, _ = self.seed()
        for request in ({"mode": "transcript", "audio": True}, {"mode": "transcript", "max_chars": 4001}):
            with self.assertRaises(AgentError) as error:
                read_clip(self.inbox.db, chunk, request)
            self.assertEqual(error.exception.status, 400)
        with self.inbox.connect() as db:
            db.execute("UPDATE chunks SET transcript=' ' WHERE id=?", (chunk,))
        with self.assertRaises(AgentError) as error:
            read_clip(self.inbox.db, chunk, {"mode": "transcript"})
        self.assertEqual(error.exception.status, 404)

    def test_singleton_speaker_change_invalidates_search_cursor(self):
        from agent_api.identity import note_speaker_change
        chunk, _ = self.seed()
        with self.inbox.connect() as db:
            add_chunk(db, "2026-10-02T18:00:00Z", "budget later")
        sync(self.inbox)
        cursor = search_events(self.inbox.db, {"query": "budget", "limit": 1})["next_cursor"]
        with self.inbox.connect() as db:
            add_person(db, "Person", chunk)
            note_speaker_change(db, [chunk])
        with self.assertRaises(AgentError) as error:
            search_events(self.inbox.db, {"query": "budget", "limit": 1, "cursor": cursor})
        self.assertEqual(error.exception.status, 409)


class ClipRouteTests(ViewerCase):
    def test_machine_clip_route_and_human_audio_remains_forbidden(self):
        with self.inbox.connect() as db:
            chunk = add_chunk(db, "2026-10-02T14:00:00Z", "single café budget")
        sync(self.inbox)
        status, raw, _ = self.agent("/v1/search", {"query": "budget"})
        self.assertEqual(status, 200)
        citation = json.loads(raw)["events"][0]["match"]["citation"]
        status, raw, _ = self.agent(f"/v1/clips/{chunk}/read", {"mode": "transcript", "citation": citation})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["excerpts"][0]["text"], "single café budget")
        status, _, _ = self.agent(f"/audio/{chunk}", {})
        self.assertEqual(status, 403)
