from datetime import datetime, timezone
import json
import os
import time
from unittest.mock import patch
from agent_support import ViewerCase, add_chunk, sync
import place_context as places


class LocationRouteTests(ViewerCase):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        with self.inbox.connect() as db:
            places.ensure_schema(db)
            self.home = places.save_place(db, {"name": "Home", "latitude": 40, "longitude": -74, "radius_m": 100})
            self.clip = add_chunk(db, datetime.fromtimestamp(self.now, timezone.utc).isoformat(), "budget approval at home")
            self.other = add_chunk(db, datetime.fromtimestamp(self.now+7200, timezone.utc).isoformat(), "budget away")
            places.ingest_observation(db, "phone", {"observation": {"id": "obs-home", "captured_at": datetime.fromtimestamp(self.now-20, timezone.utc).isoformat(),
                "status": "observed", "source": "foreground", "latitude": 40, "longitude": -74, "accuracy_m": 10,
                "activity": {"state": "vehicle", "confidence": "low"}}}, self.now)
            places.bind_clip(db, self.clip, "obs-home")
        sync(self.inbox)

    def machine(self, path, payload, transcript=False, location=False):
        with patch.dict(os.environ, {"LIFE_RECORDER_AGENT_CLIENT_IDS": self.client_id if transcript else "",
             "LIFE_RECORDER_LOCATION_CLIENT_IDS": self.client_id if location else ""}):
            return self.request("POST", path, {"Host": "lr.genr8ive.ai", "Cf-Access-Jwt-Assertion": self.machine_token(),
                "Content-Type": "application/json"}, json.dumps(payload).encode())

    def test_old_transcript_scope_does_not_grant_location(self):
        self.assertEqual(self.machine("/v1/location/last-known", {}, transcript=True)[0], 403)
        self.assertEqual(self.machine("/v1/search", {"query": "budget", "place": "Home"}, transcript=True)[0], 403)
        status, raw, _ = self.machine("/v1/search", {"query": "budget"}, transcript=True)
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(raw)["events"]), 2)
        self.assertTrue(all("location" not in event for event in json.loads(raw)["events"]))

    def test_location_only_cannot_read_transcripts_audio_or_mutate(self):
        for path, body in (("/v1/search", {"query": "budget"}), (f"/v1/clips/{self.clip}/read", {"mode": "transcript"}),
                           ("/v1/events/example/read", {"mode": "overview"}), (f"/audio/{self.clip}", {}),
                           ("/v1/location/observations", {}), ("/v1/location/delete-history", {}), ("/v1/places", {})):
            self.assertEqual(self.machine(path, body, location=True)[0], 403)

    def test_location_projection_is_phone_only_and_has_no_coordinates(self):
        status, raw, _ = self.machine("/v1/location/last-known", {}, location=True)
        self.assertEqual(status, 200)
        value = json.loads(raw)
        self.assertEqual(value["subject"], "phone")
        self.assertEqual(value["observation_id"], "obs-home")
        self.assertEqual(value["source"], "foreground")
        self.assertEqual(value["place"]["id"], self.home["id"])
        self.assertEqual(value["activity"], {"state": "vehicle", "confidence": "low"})
        self.assertNotIn("latitude", value)
        self.assertNotIn("longitude", value)
        self.assertGreaterEqual(value["age_seconds"], 20)
        self.assertEqual(self.machine("/v1/location/last-known", {"coordinates": True}, location=True)[0], 400)

    def test_both_scopes_enable_exact_place_filtered_transcript_search(self):
        for place in ("Home", self.home["id"]):
            status, raw, _ = self.machine("/v1/search", {"query": "budget", "place": place}, transcript=True, location=True)
            self.assertEqual(status, 200)
            result = json.loads(raw)["events"]
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["match"]["chunk_id"], self.clip)
            self.assertEqual(result[0]["location"]["place"]["id"], self.home["id"])
            self.assertNotIn("latitude", result[0]["location"])
        self.assertEqual(self.machine("/v1/search", {"query": "budget", "place": "Hom"}, transcript=True, location=True)[0], 400)

    def test_allowlist_revocation_is_immediate(self):
        self.assertEqual(self.machine("/v1/location/last-known", {}, location=True)[0], 200)
        self.assertEqual(self.machine("/v1/location/last-known", {})[0], 403)
        self.assertEqual(self.machine("/v1/search", {"place": "Home"}, transcript=True, location=True)[0], 200)
        self.assertEqual(self.machine("/v1/search", {"place": "Home"}, transcript=True)[0], 403)

    def test_explicit_history_delete_removes_filter_hits_and_raw_context(self):
        with self.inbox.connect() as db:
            places.clear_history(db, "phone", {"id": "delete-home", "occurred_at": datetime.fromtimestamp(self.now+1, timezone.utc).isoformat()}, self.now+1)
        status, raw, _ = self.machine("/v1/search", {"query": "budget", "place": "Home"}, transcript=True, location=True)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["events"], [])
        status, raw, _ = self.machine("/v1/location/last-known", {}, location=True)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["status"], "unknown")
