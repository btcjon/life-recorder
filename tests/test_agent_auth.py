import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_support import IndexCase, ViewerCase, add_chunk, sync


class AgentAuthTests(ViewerCase):
    def test_unlisted_and_empty_allowlist_are_forbidden(self):
        os.environ["LIFE_RECORDER_AGENT_CLIENT_IDS"] = self.client_id
        status, _, _ = self.request("POST", "/v1/search", {
            "Host": "lr.genr8ive.ai",
            "Cf-Access-Jwt-Assertion": self.machine_token("other.access"),
            "Content-Type": "application/json",
        }, b"{}")
        self.assertEqual(status, 403)
        previous = os.environ.pop("LIFE_RECORDER_AGENT_CLIENT_IDS", None)
        try:
            status, body, _ = self.request("POST", "/v1/search", {
                "Host": "lr.genr8ive.ai",
                "Cf-Access-Jwt-Assertion": self.machine_token(),
                "Content-Type": "application/json",
            }, b"{}")
        finally:
            if previous is not None:
                os.environ["LIFE_RECORDER_AGENT_CLIENT_IDS"] = previous
        self.assertEqual(status, 403)
        self.assertEqual(body, b'{"error": {"code": "forbidden", "message": "This credential cannot use that route."}}')

    def test_machine_cannot_use_human_routes(self):
        os.environ["LIFE_RECORDER_AGENT_CLIENT_IDS"] = self.client_id
        for method, path in (
            ("GET", "/"),
            ("GET", "/v1/days"),
            ("GET", "/v1/days/2026-09-22"),
            ("GET", "/v1/audio/nope"),
            ("HEAD", "/v1/audio/nope"),
            ("POST", "/v1/people"),
            ("POST", "/v1/chunks/nope/keep"),
        ):
            if method == "POST":
                status, _, _ = self.agent(path, {"name": "Ada"})
            else:
                status, _, _ = self.request(method, path, {
                    "Host": "lr.genr8ive.ai",
                    "Cf-Access-Jwt-Assertion": self.machine_token(),
                })
                if status != 403:
                    os.environ["LIFE_RECORDER_AGENT_CLIENT_IDS"] = self.client_id
                    status, _, _ = self.request(method, path, {
                        "Host": "lr.genr8ive.ai",
                        "Cf-Access-Jwt-Assertion": self.machine_token(),
                    })
            self.assertEqual(status, 403, path)

    def test_invalid_body_is_rejected_before_it_can_change_the_status_of_a_bad_credential(self):
        status, _, _ = self.request("POST", "/v1/search", {
            "Host": "lr.genr8ive.ai",
            "Cf-Access-Jwt-Assertion": self.machine_token("missing.access"),
            "Content-Type": "application/json",
        }, b"not-json")
        self.assertEqual(status, 403)

    def test_human_browser_token_still_reads_the_day_list(self):
        status, _, _ = self.request("GET", "/v1/days", {
            "Host": "lr.genr8ive.ai",
            "Cf-Access-Jwt-Assertion": self.human_token(),
        })
        self.assertEqual(status, 200)

    def test_local_bearer_still_reads_the_day_list(self):
        status, _, _ = self.request("GET", "/v1/days", {"Authorization": "Bearer " + self.token})
        self.assertEqual(status, 200)

    def test_missing_jwt_stays_unauthorized(self):
        status, _, _ = self.request("POST", "/v1/search", {
            "Host": "lr.genr8ive.ai",
            "Content-Type": "application/json",
        }, b"{}")
        self.assertEqual(status, 401)


    def test_unknown_kid_validates_once_and_keeps_each_error_shape(self):
        from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError
        os.environ["LIFE_RECORDER_AGENT_CLIENT_IDS"] = self.client_id
        calls = {"count": 0}

        class CountingClient:
            def get_signing_key_from_jwt(self, token):
                calls["count"] += 1
                raise PyJWKClientError('Unable to find a signing key that matches: "missing"')

        self.server.access_verifier = __import__("access_auth").AccessVerifier(self.config, client=CountingClient())
        status, body, _ = self.request("GET", "/v1/days", {
            "Host": "lr.genr8ive.ai",
            "Cf-Access-Jwt-Assertion": self.human_token(),
        })
        self.assertEqual(status, 401)
        self.assertEqual(body, b'{"error": "Unauthorized"}')
        self.assertEqual(calls["count"], 1)
        calls["count"] = 0
        self.server.access_verifier = __import__("access_auth").AccessVerifier(
            self.config, client=type("Down", (), {
                "get_signing_key_from_jwt": lambda *_: (_ for _ in ()).throw(PyJWKClientConnectionError("jwks down"))
            })())
        status, body, _ = self.request("GET", "/v1/days", {
            "Host": "lr.genr8ive.ai",
            "Cf-Access-Jwt-Assertion": self.human_token(),
        })
        self.assertEqual(status, 503)
        self.assertEqual(body, b'{"error": "Access verification is unavailable"}')
        status, body, _ = self.request("POST", "/v1/search", {
            "Host": "lr.genr8ive.ai",
            "Cf-Access-Jwt-Assertion": self.machine_token(),
            "Content-Type": "application/json",
        }, b"{}")
        self.assertEqual(status, 503)
        self.assertEqual(body, b'{"error": {"code": "unavailable", "message": "Access verification is unavailable."}}')

    def test_unknown_kid_is_unauthorized_and_jwks_failure_is_unavailable(self):
        from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError
        os.environ["LIFE_RECORDER_AGENT_CLIENT_IDS"] = self.client_id
        self.server.access_verifier = __import__("access_auth").AccessVerifier(
            self.config, client=__import__("agent_support").FakeClient(
                self.private.public_key(), error=PyJWKClientError('Unable to find a signing key that matches: "missing"')))
        status, body, _ = self.request("POST", "/v1/search", {
            "Host": "lr.genr8ive.ai",
            "Cf-Access-Jwt-Assertion": self.machine_token(),
            "Content-Type": "application/json",
        }, b"{}")
        self.assertEqual(status, 401)
        self.assertIn(b"Unauthorized", body)
        self.assertNotIn(b"unavailable", body)
        status, body, _ = self.request("GET", "/v1/days", {
            "Host": "lr.genr8ive.ai",
            "Cf-Access-Jwt-Assertion": self.human_token(),
        })
        self.assertEqual(status, 401)
        self.server.access_verifier = __import__("access_auth").AccessVerifier(
            self.config, client=__import__("agent_support").FakeClient(
                self.private.public_key(), error=PyJWKClientConnectionError("jwks down")))
        status, body, _ = self.request("POST", "/v1/search", {
            "Host": "lr.genr8ive.ai",
            "Cf-Access-Jwt-Assertion": self.machine_token(),
            "Content-Type": "application/json",
        }, b"{}")
        self.assertEqual(status, 503)
        self.assertIn(b"unavailable", body)
        status, body, _ = self.request("GET", "/v1/days", {
            "Host": "lr.genr8ive.ai",
            "Cf-Access-Jwt-Assertion": self.human_token(),
        })
        self.assertEqual(status, 503)
        self.assertIn(b"unavailable", body)

    def test_key_lookup_failure_is_unavailable(self):
        self.server.access_verifier = __import__("access_auth").AccessVerifier(
            self.config, client=__import__("agent_support").FakeClient(self.private.public_key(), error=RuntimeError("jwks")))
        status, body, _ = self.request("GET", "/v1/days", {
            "Host": "lr.genr8ive.ai",
            "Cf-Access-Jwt-Assertion": self.human_token(),
        })
        self.assertEqual(status, 503)
        self.assertIn(b"unavailable", body)

    def test_empty_jwks_is_unavailable_for_human_and_agent(self):
        from jwt.exceptions import PyJWKClientError
        os.environ["LIFE_RECORDER_AGENT_CLIENT_IDS"] = self.client_id
        calls = {"count": 0}

        class EmptyClient:
            def get_signing_key_from_jwt(self, token):
                calls["count"] += 1
                raise PyJWKClientError("The JWKS endpoint did not contain any signing keys")

        self.server.access_verifier = __import__("access_auth").AccessVerifier(self.config, client=EmptyClient())
        for method, path, token, expected in (
            ("GET", "/v1/days", self.human_token(), b'{"error": "Access verification is unavailable"}'),
            ("POST", "/v1/search", self.machine_token(),
             b'{"error": {"code": "unavailable", "message": "Access verification is unavailable."}}'),
        ):
            status, body, _ = self.request(method, path, {
                "Host": "lr.genr8ive.ai",
                "Cf-Access-Jwt-Assertion": token,
                "Content-Type": "application/json",
            }, b"{}" if method == "POST" else None)
            self.assertEqual((status, body), (503, expected))
        self.assertEqual(calls["count"], 2)


class IdentityTests(IndexCase):
    def test_fts_match_survives_repeated_schema_checks(self):
        with self.inbox.connect() as db:
            add_chunk(db, "2026-09-22T14:00:00Z", "unique hallway words")
            add_chunk(db, "2026-09-22T14:01:00Z", "other words")
        sync(self.inbox)
        with self.inbox.connect() as db:
            expected = db.execute("SELECT id FROM agent_events WHERE tombstoned=0").fetchone()[0]
            state = tuple(db.execute(
                "SELECT search_generation, cursor_key FROM agent_api_state WHERE id=1"
            ).fetchone())
        for _ in range(3):
            sync(self.inbox)
            with self.inbox.connect() as db:
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM agent_transcript_fts WHERE agent_transcript_fts MATCH ?",
                    ("hallway",),
                ).fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM agent_transcript_fts").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT id FROM agent_events WHERE tombstoned=0").fetchone()[0], expected)
                self.assertEqual(tuple(db.execute(
                    "SELECT search_generation, cursor_key FROM agent_api_state WHERE id=1"
                ).fetchone()), state)

    def test_legacy_fts_migration_survives_second_reconcile(self):
        with self.inbox.connect() as db:
            chunk_id = add_chunk(db, "2026-09-22T14:00:00Z", "unique ladder words")
            second_id = add_chunk(db, "2026-09-22T14:01:00Z", "other words")
        sync(self.inbox)
        with self.inbox.connect() as db:
            event_id = db.execute("SELECT id FROM agent_events WHERE tombstoned=0").fetchone()[0]
            cursor_key = db.execute("SELECT cursor_key FROM agent_api_state WHERE id=1").fetchone()[0]
            db.execute("DROP TABLE agent_transcript_fts")
            db.execute("DROP TABLE agent_transcripts")
            db.execute("CREATE VIRTUAL TABLE agent_transcript_fts USING fts5(chunk_id UNINDEXED, body)")
            db.execute("INSERT INTO agent_transcript_fts(chunk_id, body) VALUES (?, ?)",
                       (chunk_id, "unique ladder words"))
            db.execute("INSERT INTO agent_transcript_fts(chunk_id, body) VALUES (?, ?)",
                       (second_id, "other words"))
            db.execute("UPDATE agent_api_state SET schema_version=1 WHERE id=1")
        for _ in range(2):
            sync(self.inbox)
            with self.inbox.connect() as db:
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM agent_transcript_fts WHERE agent_transcript_fts MATCH ?",
                    ("ladder",),
                ).fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT id FROM agent_events WHERE tombstoned=0").fetchone()[0], event_id)
                self.assertEqual(db.execute("SELECT cursor_key FROM agent_api_state WHERE id=1").fetchone()[0], cursor_key)
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_ids_survive_edits_and_second_reconcile_is_quiet(self):
        with self.inbox.connect() as db:
            first = add_chunk(db, "2026-09-22T14:00:00Z", "alpha words")
            add_chunk(db, "2026-09-22T14:01:00Z", "beta words")
        sync(self.inbox)
        with self.inbox.connect() as db:
            event_id = db.execute("SELECT id FROM agent_events WHERE tombstoned=0").fetchone()[0]
            db.execute("UPDATE chunks SET transcript=? WHERE id=?", ("alpha words changed", first))
        sync(self.inbox)
        with self.inbox.connect() as db:
            self.assertEqual(db.execute("SELECT id FROM agent_events WHERE tombstoned=0").fetchone()[0], event_id)
        generation = self.generation()
        sync(self.inbox)
        self.assertEqual(self.generation(), generation)

    def test_split_keeps_one_id_and_merge_aliases_the_other(self):
        with self.inbox.connect() as db:
            add_chunk(db, "2026-09-22T14:00:00Z", "one")
            add_chunk(db, "2026-09-22T14:01:00Z", "two")
            third = add_chunk(db, "2026-09-22T14:02:00Z", "three")
            fourth = add_chunk(db, "2026-09-22T14:03:00Z", "four")
        sync(self.inbox)
        with self.inbox.connect() as db:
            original = db.execute("SELECT id FROM agent_events WHERE tombstoned=0").fetchone()[0]
            db.execute("UPDATE chunks SET started=? WHERE id=?", ("2026-09-22T16:00:00Z", third))
            db.execute("UPDATE chunks SET started=? WHERE id=?", ("2026-09-22T16:01:00Z", fourth))
        sync(self.inbox)
        with self.inbox.connect() as db:
            live = [row[0] for row in db.execute("SELECT id FROM agent_events WHERE tombstoned=0 ORDER BY created_seq")]
            self.assertEqual(len(live), 2)
            self.assertIn(original, live)
            db.execute("UPDATE chunks SET started=? WHERE id=?", ("2026-09-22T14:02:00Z", third))
            db.execute("UPDATE chunks SET started=? WHERE id=?", ("2026-09-22T14:03:00Z", fourth))
        sync(self.inbox)
        with self.inbox.connect() as db:
            live = [row[0] for row in db.execute("SELECT id FROM agent_events WHERE tombstoned=0")]
            self.assertEqual(live, [original])
            alias = db.execute("SELECT canonical_id FROM agent_event_aliases").fetchone()
            self.assertEqual(alias[0], original)

    def test_deleted_event_is_tombstoned_and_reconcile_rolls_back(self):
        with self.inbox.connect() as db:
            add_chunk(db, "2026-09-22T14:00:00Z", "one")
            add_chunk(db, "2026-09-22T14:01:00Z", "two")
        sync(self.inbox)
        before = self.generation()
        try:
            with self.inbox.connect() as db:
                db.execute("DELETE FROM chunks")
                agent_api = __import__("agent_api")
                viewer = __import__("viewer")
                agent_api.reconcile(db, viewer.display_blocks)
                raise RuntimeError("rollback")
        except RuntimeError:
            pass
        self.assertEqual(self.generation(), before)
        with self.inbox.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM agent_events WHERE tombstoned=0").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
