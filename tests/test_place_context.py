from datetime import datetime, timezone
import json
import math
from agent_support import IndexCase, add_chunk, sync
import place_context as places
from agent_api.errors import AgentError


NOW = datetime(2026, 10, 2, 14, tzinfo=timezone.utc).timestamp()


def observation(ident="obs-1", age=0, **updates):
    item = {"id": ident, "captured_at": datetime.fromtimestamp(NOW-age, timezone.utc).isoformat(),
            "source": "foreground", "status": "observed", "latitude": 40, "longitude": -74, "accuracy_m": 10}
    item.update(updates)
    if item.get("status") in ("denied", "unavailable", "revoked"):
        for key in ("latitude", "longitude", "accuracy_m"):
            item.pop(key, None)
    return {"observation": item}


class PlaceContextTests(IndexCase):
    def setUp(self):
        super().setUp()
        with self.inbox.connect() as db:
            places.ensure_schema(db)
            self.home = places.save_place(db, {"name": "Home", "latitude": 40, "longitude": -74, "radius_m": 100})
            self.clip = add_chunk(db, datetime.fromtimestamp(NOW, timezone.utc).isoformat(), "budget at home")
        sync(self.inbox)

    def test_schema_noop_and_place_revision_crud(self):
        with self.inbox.connect() as db:
            places.ensure_schema(db)
            places.ensure_schema(db)
            before = places.list_places(db)
            self.assertEqual(len(before), 1)
            body = {k: self.home[k] for k in ("name", "latitude", "longitude", "radius_m", "revision")}
            body["name"] = "My Home"
            updated = places.save_place(db, body, self.home["id"])
            self.assertEqual(updated["revision"], 2)
            with self.assertRaises(AgentError) as error:
                places.save_place(db, body, self.home["id"])
            self.assertEqual(error.exception.status, 409)
            with self.assertRaises(AgentError):
                places.delete_place(db, self.home["id"], 1)
            places.delete_place(db, self.home["id"], 2)
            self.assertEqual(places.list_places(db), [])

    def test_place_validation_and_exact_names(self):
        with self.inbox.connect() as db:
            self.assertEqual(places.place_filter(db, "Home"), self.home["id"])
            self.assertEqual(places.place_filter(db, self.home["id"]), self.home["id"])
            for query in ("Hom", "unknown", ""):
                with self.assertRaises(AgentError):
                    places.place_filter(db, query)
            for field, value in (("latitude", math.nan), ("longitude", 181), ("radius_m", True), ("radius_m", 1)):
                body = {"name": "Other", "latitude": 41, "longitude": -74, "radius_m": 50, field: value}
                with self.assertRaises(AgentError):
                    places.save_place(db, body)

    def test_explicit_pointer_required_and_arrival_after_clip_resolves_it(self):
        with self.inbox.connect() as db:
            self.assertEqual(places.clip_context(db, self.clip)["status"], "unknown")
            self.assertEqual(places.bind_clip(db, self.clip, "late")["status"], "pending")
            places.ingest_observation(db, "phone", observation("unrelated"), NOW)
            self.assertEqual(places.clip_context(db, self.clip)["status"], "pending")
            places.ingest_observation(db, "phone", observation("late"), NOW+10)
            self.assertEqual(places.clip_context(db, self.clip)["place"]["id"], self.home["id"])
            with self.assertRaises(AgentError) as error:
                places.bind_clip(db, self.clip, "unrelated")
            self.assertEqual(error.exception.status, 409)

    def test_duplicate_is_idempotent_and_conflicting_id_rejected(self):
        with self.inbox.connect() as db:
            original = observation()
            self.assertFalse(places.ingest_observation(db, "phone", original, NOW)["duplicate"])
            self.assertTrue(places.ingest_observation(db, "phone", original, NOW+60)["duplicate"])
            self.assertEqual(db.execute("SELECT received_at FROM location_observations").fetchone()[0], NOW)
            with self.assertRaises(AgentError) as error:
                places.ingest_observation(db, "phone", observation(accuracy_m=11), NOW+60)
            self.assertEqual(error.exception.status, 409)
            with self.assertRaises(AgentError):
                places.ingest_observation(db, "other-device", original, NOW+60)

    def test_stale_future_and_wrong_device_cannot_label_clip(self):
        for ident, payload, device, now, state in (
            ("stale", observation("stale", 301), "phone", NOW, "stale"),
            ("future", observation("future", -1), "phone", NOW+1, "future_observation"),
            ("other", observation("other"), "other-phone", NOW, "device_mismatch")):
            with self.inbox.connect() as db:
                clip = add_chunk(db, datetime.fromtimestamp(NOW, timezone.utc).isoformat(), "words")
                places.ingest_observation(db, device, payload, now)
                result = places.bind_clip(db, clip, ident)
                self.assertEqual(result["status"], state)
                self.assertIsNone(result["place"])

    def test_uncertainty_must_fit_wholly_exactly_one_place(self):
        with self.inbox.connect() as db:
            places.ingest_observation(db, "phone", observation("wide", accuracy_m=101), NOW)
            self.assertEqual(places.last_known(db, NOW)["status"], "unknown")
            places.save_place(db, {"name": "Neighbor", "latitude": 40, "longitude": -74, "radius_m": 80})
            places.ingest_observation(db, "phone", observation("ambiguous", age=-1), NOW+1)
            self.assertEqual(places.last_known(db, NOW+1)["status"], "ambiguous")

    def test_delayed_does_not_replace_newest_travel_context(self):
        with self.inbox.connect() as db:
            places.ingest_observation(db, "phone", observation("away", latitude=41), NOW)
            places.ingest_observation(db, "phone", observation("home-delayed", age=60), NOW+60)
            context = places.last_known(db, NOW+60)
            self.assertEqual(context["status"], "unknown")
            self.assertEqual(context["captured_at"], datetime.fromtimestamp(NOW, timezone.utc).isoformat(timespec="seconds"))
            self.assertNotIn("latitude", json.dumps(context))
            places.ingest_observation(db, "phone", observation("same-time-delayed"), NOW+120)
            self.assertEqual(places.last_known(db, NOW+120)["observation_id"], "away")

    def test_phone_denial_and_offline_status_replace_previous_knowledge(self):
        with self.inbox.connect() as db:
            places.ingest_observation(db, "phone", observation(), NOW)
            places.ingest_observation(db, "phone", observation("denied", age=-10, status="denied"), NOW+10)
            self.assertEqual(places.last_known(db, NOW+10)["status"], "denied")
            self.assertIsNone(places.last_known(db, NOW+10)["place"])
            places.ingest_observation(db, "phone", observation("offline", age=-20, status="unavailable"), NOW+20)
            self.assertEqual(places.last_known(db, NOW+20)["status"], "unavailable")

    def test_collection_source_activity_and_revoke_are_preserved(self):
        with self.inbox.connect() as db:
            activity = {"state": "vehicle", "confidence": "low"}
            places.ingest_observation(db, "phone", observation(source="background", activity=activity), NOW)
            context = places.last_known(db, NOW)
            self.assertEqual(context["subject"], "phone")
            self.assertEqual(context["source"], "background")
            self.assertEqual(context["activity"], activity)
            places.ingest_observation(db, "phone", observation("revoke", age=-10, status="revoked"), NOW+10)
            self.assertEqual(places.last_known(db, NOW+10)["status"], "revoked")
            self.assertIsNone(places.last_known(db, NOW+10)["place"])

    def test_raw_coordinates_expire_but_labels_survive_and_explicit_delete_removes_them(self):
        with self.inbox.connect() as db:
            places.ingest_observation(db, "phone", observation(), NOW)
            places.bind_clip(db, self.clip, "obs-1")
            self.assertEqual(places.cleanup(db, NOW+86400)["raw_coordinates_expired"], 1)
            row = db.execute("SELECT latitude,longitude FROM location_observations").fetchone()
            self.assertEqual(tuple(row), (None, None))
            self.assertEqual(places.clip_context(db, self.clip)["place"]["id"], self.home["id"])
            self.assertEqual(places.last_known(db, NOW+86400)["status"], "unknown")
            self.assertIsNone(places.last_known(db, NOW+86400)['observation_id'])
            places.delete_observation(db, "obs-1")
            self.assertEqual(places.clip_context(db, self.clip)["status"], "unknown")
            self.assertEqual(places.ingest_observation(db, "phone", observation(), NOW+86401)["ignored"], "history_deleted")

    def test_duplicate_after_raw_expiry_is_unchanged_and_conflict_still_rejected(self):
        with self.inbox.connect() as db:
            places.ingest_observation(db, "phone", observation(), NOW)
            self.assertTrue(places.ingest_observation(db, "phone", observation(), NOW+86401)["duplicate"])
            self.assertIsNone(db.execute("SELECT latitude FROM location_observations WHERE id='obs-1'").fetchone()[0])
            with self.assertRaises(AgentError) as error:
                places.ingest_observation(db, "phone", observation(accuracy_m=11), NOW+86401)
            self.assertEqual(error.exception.status, 409)

    def test_old_delayed_coordinates_not_stored_and_place_delete_clears_labels(self):
        with self.inbox.connect() as db:
            places.ingest_observation(db, "phone", observation("old", age=86401), NOW)
            self.assertIsNone(db.execute("SELECT latitude FROM location_observations WHERE id='old'").fetchone())
            self.assertEqual(db.execute("SELECT disposition FROM location_observation_receipts WHERE id='old'").fetchone()[0], "expired")
            places.ingest_observation(db, "phone", observation(), NOW)
            places.bind_clip(db, self.clip, "obs-1")
            places.delete_place(db, self.home["id"], 1)
            self.assertEqual(places.clip_context(db, self.clip)["status"], "place_deleted")
            self.assertIsNone(places.last_known(db, NOW)["place"])

    def test_invalid_observation_envelopes(self):
        invalid = ({}, {"observation": []}, observation(latitude=math.inf), observation(accuracy_m=-1),
                   observation(source="computer"), observation(extra="unsupported"), observation(id="x"*5000),
                   observation("future", age=-10), observation(activity="teleporting"))
        for payload in invalid:
            with self.assertRaises(AgentError):
                places.parse_observation(payload, "phone", NOW)

    def test_bulk_device_delete_is_idempotent_and_blocks_late_recreation(self):
        with self.inbox.connect() as db:
            places.ingest_observation(db, "phone", observation(), NOW)
            places.ingest_observation(db, "other-phone", observation("other-device"), NOW)
            places.bind_clip(db, self.clip, "obs-1")
            request = {"id": "delete-request", "occurred_at": datetime.fromtimestamp(NOW, timezone.utc).isoformat()}
            self.assertFalse(places.clear_history(db, "phone", request, NOW)["duplicate"])
            self.assertTrue(places.clear_history(db, "phone", request, NOW)["duplicate"])
            self.assertEqual(places.clip_context(db, self.clip)["status"], "unknown")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM location_observations WHERE device='other-phone'").fetchone()[0], 1)
            self.assertEqual(places.ingest_observation(db, "phone", observation(), NOW+1)["ignored"], "history_deleted")
            self.assertEqual(places.ingest_observation(db, "phone", observation("late", age=1), NOW+1)["ignored"], "history_deleted")
            self.assertEqual(places.bind_clip(db, self.clip, "late")["status"], "history_deleted")
            places.ingest_observation(db, "phone", observation("new", age=-2), NOW+2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM location_observations WHERE device='phone'").fetchone()[0], 1)
            with self.assertRaises(AgentError):
                places.clear_history(db, "other-phone", request, NOW)

    def test_bulk_delete_clears_pending_pointer_at_fractional_cutoff(self):
        with self.inbox.connect() as db:
            started = NOW + 0.125
            clip = add_chunk(db, datetime.fromtimestamp(started, timezone.utc).isoformat(), "pending context")
            places.bind_clip(db, clip, "not-yet-uploaded")
            places.clear_history(db, "phone", {"id": "fractional-delete", "occurred_at": datetime.fromtimestamp(started+0.25, timezone.utc).isoformat()}, started+0.25)
            self.assertIsNone(db.execute("SELECT chunk_id FROM clip_place_context WHERE chunk_id=?", (clip,)).fetchone())

    def test_manual_event_tag_is_separate_from_captured_context(self):
        with self.inbox.connect() as db:
            add_chunk(db, "2026-10-02T14:01:00Z", "end event")
        sync(self.inbox)
        with self.inbox.connect() as db:
            event = db.execute("SELECT id FROM agent_events WHERE tombstoned=0").fetchone()[0]
            tag = places.set_event_place(db, event, self.home["id"], 0)
            self.assertEqual(tag["revision"], 1)
            self.assertEqual(places.event_place(db, event)["place_id"], self.home["id"])
            self.assertEqual(places.clip_context(db, self.clip)["status"], "unknown")
            with self.assertRaises(AgentError):
                places.set_event_place(db, event, None, 0)
