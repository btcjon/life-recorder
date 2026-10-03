import importlib.util
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from io import StringIO
from contextlib import redirect_stdout
from pathlib import Path


def module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "evaluate-background-trial.py"
    spec = importlib.util.spec_from_file_location("evaluate_background_trial", path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


TOOL = module()
ROOT = Path(__file__).resolve().parents[1]
SENTINEL_DEVICE = "device-sentinel-9f3a2c"
SENTINEL_BUILD = "build-sentinel-9f3a2c"
SENTINEL_PATH = "/private/recordings/sentinel-9f3a2c.m4a"
SENTINEL_TRANSCRIPT = "transcript-sentinel-9f3a2c"
SENTINEL_COORD = "35.7796,-78.6382"


def session(condition, start, **overrides):
    elapsed = int(overrides.pop("elapsed", 4 * 60 * 60))
    end = start + timedelta(seconds=elapsed)
    battery_drop = overrides.pop("battery_drop", 10 if condition == "off" else 13)
    body = {
        "condition": condition,
        "device_pseudonym": SENTINEL_DEVICE,
        "app_build": SENTINEL_BUILD,
        "started_at": start.isoformat(),
        "ended_at": end.isoformat(),
        "charging": False,
        "battery_start_percent": 90,
        "battery_end_percent": 90 - battery_drop,
        "activities": ["stationary", "walking", "vehicle"],
        "recorded_clip_count": 240,
        "recorded_duration_seconds": elapsed,
        "verified_receipt_count": 240,
        "pending_upload_count": 0,
        "failed_upload_count": 0,
        "lost_audio_count": 0,
        "duplicate_clip_count": 0,
        "location": {
            "observation_count": 0 if condition == "off" else 4,
            "fresh_observation_count": 0 if condition == "off" else 2,
            "coverage_percent": None if condition == "off" else 37.5,
            "observation_age_seconds": None if condition == "off" else 125,
        },
    }
    location = overrides.pop("location", None)
    body.update(overrides)
    if location is not None:
        body["location"] = location
    return body


def manifest(*sessions, **extra):
    payload = {"schema": TOOL.SCHEMA, "sessions": list(sessions)}
    payload.update(extra)
    return payload


def paired(off_start=None, on_start=None, **kwargs):
    off_start = off_start or datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
    on_start = on_start or datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
    off_over = kwargs.pop("off", {})
    on_over = kwargs.pop("on", {})
    return manifest(session("off", off_start, **off_over), session("on", on_start, **on_over))


class BackgroundTrialTests(unittest.TestCase):
    def test_valid_measured_manifest_is_not_a_passed_physical_trial(self):
        report = TOOL.evaluate_manifest(paired())
        self.assertEqual(report["status"], "manifest_validated")
        self.assertFalse(report["background_enabled"])
        self.assertFalse(report["physical_trial_passed"])
        self.assertFalse(report["activation_performed"])
        self.assertIsNone(report["accuracy_guarantee"])
        self.assertIsNone(report["location_coverage_threshold_percent"])
        self.assertEqual(report["evidence_label"], "manifest_reported_not_independently_observed")
        self.assertEqual(report["reported_additional_battery_percentage_points"], 3)
        self.assertEqual(report["reported_on_location_coverage_percent"], 37.5)
        self.assertEqual(report["checks"]["completeness"]["status"], "manifest_validated")
        self.assertEqual(report["checks"]["battery"]["status"], "manifest_validated")
        self.assertEqual(report["checks"]["continuity"]["status"], "manifest_validated")
        self.assertEqual(TOOL.evaluate_manifest(TOOL.template_document())["status"], "unavailable")

    def test_timing_non_overlap_and_matching(self):
        start = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
        adjacent = paired(on_start=start + timedelta(hours=4))
        self.assertEqual(TOOL.evaluate_manifest(adjacent)["status"], "manifest_validated")
        overlap = paired(on_start=start + timedelta(hours=4) - timedelta(seconds=1), on={"elapsed": 4 * 60 * 60})
        overlap_report = TOOL.evaluate_manifest(overlap)
        self.assertEqual(overlap_report["status"], "not_passed")
        self.assertIn("sessions_overlap", overlap_report["checks"]["completeness"]["reasons"])
        short = paired(off={"elapsed": 4 * 60 * 60 - 1})
        self.assertIn("duration_below_minimum", TOOL.evaluate_manifest(short)["reasons"])
        within = paired(on={"elapsed": 4 * 60 * 60 + 300})
        self.assertEqual(TOOL.evaluate_manifest(within)["status"], "manifest_validated")
        beyond = paired(on={"elapsed": 4 * 60 * 60 + 301})
        self.assertIn("duration_mismatch", TOOL.evaluate_manifest(beyond)["reasons"])
        naive = paired()
        naive["sessions"][0]["started_at"] = "2026-10-03T12:00:00"
        self.assertIn("timestamp_not_timezone_aware", TOOL.evaluate_manifest(naive)["reasons"])
        unordered = paired()
        unordered["sessions"][1]["ended_at"] = unordered["sessions"][1]["started_at"]
        self.assertEqual(TOOL.evaluate_manifest(unordered)["status"], "unavailable")
        unmatched = paired(on={"activities": ["stationary", "walking"]})
        self.assertIn("activities_not_covered", TOOL.evaluate_manifest(unmatched)["reasons"])
        different = paired(on={"device_pseudonym": "other-device"})
        self.assertIn("device_mismatch", TOOL.evaluate_manifest(different)["reasons"])
        self.assertEqual(TOOL.evaluate_manifest({"schema": TOOL.SCHEMA, "sessions": []})["status"], "unavailable")

    def test_battery_boundary_excess_charging_and_increase(self):
        boundary = TOOL.evaluate_manifest(paired())
        self.assertEqual(boundary["status"], "manifest_validated")
        self.assertEqual(boundary["reported_additional_battery_percentage_points"], 3)
        excess = TOOL.evaluate_manifest(paired(on={"battery_drop": 14}))
        self.assertEqual(excess["status"], "not_passed")
        self.assertIn("battery_delta_exceeds_limit", excess["checks"]["battery"]["reasons"])
        self.assertEqual(excess["checks"]["continuity"]["status"], "manifest_validated")
        self.assertFalse(excess["background_enabled"])
        charging = TOOL.evaluate_manifest(paired(on={"charging": True, "battery_drop": 0}))
        self.assertIn("charging_not_allowed", charging["checks"]["battery"]["reasons"])
        self.assertNotIn("battery_increased", charging["reasons"])
        increased = TOOL.evaluate_manifest(paired(off={"battery_start_percent": 40, "battery_end_percent": 41, "battery_drop": 0}))
        increased = TOOL.evaluate_manifest(paired(off={"battery_start_percent": 40, "battery_end_percent": 55}))
        self.assertIn("battery_increased", increased["checks"]["battery"]["reasons"])
        self.assertEqual(increased["status"], "not_passed")

    def test_missing_nan_and_bool_data_fail_closed(self):
        missing = paired()
        missing["sessions"][1]["battery_end_percent"] = None
        missing_report = TOOL.evaluate_manifest(missing)
        self.assertEqual(missing_report["status"], "unavailable")
        self.assertIn("battery_missing", missing_report["checks"]["battery"]["reasons"])
        self.assertFalse(missing_report["background_enabled"])
        nan = paired()
        nan["sessions"][0]["battery_start_percent"] = float("nan")
        nan_report = TOOL.evaluate_manifest(nan)
        self.assertEqual(nan_report["status"], "unavailable")
        self.assertIn("battery_invalid", nan_report["reasons"])
        self.assertNotIn("NaN", json.dumps(nan_report, allow_nan=False))
        flagged = paired()
        flagged["sessions"][1]["battery_start_percent"] = True
        self.assertIn("battery_invalid", TOOL.evaluate_manifest(flagged)["reasons"])
        counted = paired()
        counted["sessions"][0]["pending_upload_count"] = True
        self.assertEqual(TOOL.evaluate_manifest(counted)["status"], "unavailable")
        self.assertIn("clip_count_invalid", TOOL.evaluate_manifest(counted)["reasons"])

    def test_continuity_and_receipt_failures(self):
        gap = paired(on={"recorded_duration_seconds": 4 * 60 * 60 - 61})
        self.assertIn("recording_coverage_gap", TOOL.evaluate_manifest(gap)["checks"]["continuity"]["reasons"])
        edge = paired(on={"recorded_duration_seconds": 4 * 60 * 60 - 60})
        self.assertEqual(TOOL.evaluate_manifest(edge)["checks"]["continuity"]["status"], "manifest_validated")
        short_receipts = paired(on={"verified_receipt_count": 239})
        self.assertIn("receipts_do_not_cover_clips", TOOL.evaluate_manifest(short_receipts)["reasons"])
        pending = paired(off={"pending_upload_count": 1})
        failed = paired(off={"failed_upload_count": 1})
        lost = paired(on={"lost_audio_count": 2})
        duplicate = paired(on={"duplicate_clip_count": 1})
        zero = paired(on={"recorded_clip_count": 0, "verified_receipt_count": 0})
        self.assertIn("pending_uploads", TOOL.evaluate_manifest(pending)["reasons"])
        self.assertIn("failed_uploads", TOOL.evaluate_manifest(failed)["reasons"])
        self.assertIn("audio_loss", TOOL.evaluate_manifest(lost)["reasons"])
        self.assertIn("duplicate_clip_accounting", TOOL.evaluate_manifest(duplicate)["reasons"])
        self.assertIn("recorded_clips_below_minimum", TOOL.evaluate_manifest(zero)["reasons"])
        self.assertTrue(all(
            TOOL.evaluate_manifest(item)["background_enabled"] is False
            for item in (gap, short_receipts, pending, failed, lost, duplicate, zero)
        ))

    def test_missing_coverage_and_malformed_input(self):
        missing = paired()
        missing["sessions"][1]["location"]["coverage_percent"] = None
        report = TOOL.evaluate_manifest(missing)
        self.assertEqual(report["status"], "unavailable")
        self.assertIn("coverage_missing", report["checks"]["completeness"]["reasons"])
        self.assertIsNone(report["reported_on_location_coverage_percent"])
        self.assertFalse(report["physical_trial_passed"])
        omitted = paired()
        del omitted["sessions"][1]["location"]["coverage_percent"]
        self.assertIn("coverage_missing", TOOL.evaluate_manifest(omitted)["reasons"])
        self.assertEqual(TOOL.evaluate_manifest(["nope"])["status"], "unavailable")
        self.assertEqual(TOOL.evaluate_manifest({"schema": TOOL.SCHEMA, "sessions": {}})["status"], "unavailable")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.json"
            path.write_text("{", encoding="utf-8")
            broken = TOOL.load_manifest(path)
            self.assertEqual(broken["status"], "unavailable")
            self.assertIn("manifest_malformed", broken["reasons"])
            huge = Path(directory) / "huge.json"
            huge.write_bytes(b"{" + b" " * TOOL.MAX_BYTES)
            large = TOOL.load_manifest(huge)
            self.assertEqual(large["reasons"], ["manifest_too_large"])
            self.assertNotIn(SENTINEL_TRANSCRIPT, json.dumps(large))

    def test_extreme_numbers_duplicates_and_deep_json_fail_closed(self):
        payload = paired()
        payload['sessions'][1]['battery_start_percent'] = 10 ** 1000
        self.assertEqual(TOOL.evaluate_manifest(payload)['status'], 'unavailable')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'invalid.json'
            for content in ('{"schema":"wrong","schema":"right"}',
                            '{"n":' + '9' * 5000 + '}',
                            '[' * 2000 + '0' + ']' * 2000):
                path.write_text(content, encoding='utf-8')
                report = TOOL.load_manifest(path)
                self.assertEqual(report['status'], 'unavailable')
                self.assertTrue(set(report['reasons']) <= {'manifest_malformed', 'manifest_not_object'})

    def test_fifo_is_refused_without_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pipe'
            os.mkfifo(path)
            self.assertEqual(TOOL.load_manifest(path)['reasons'], ['manifest_not_file'])

    def test_template_path_safety_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "private"
            parent.mkdir(mode=0o700)
            target = parent / "template.json"
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = TOOL.main(["--write-template", str(target)])
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(stdout.getvalue())["wrote_template"])
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            written = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(written["sessions"][1]["battery_start_percent"], None)
            self.assertIsNone(written["sessions"][0]["location"]["coverage_percent"])
            self.assertIsNone(written["sessions"][0]["recorded_clip_count"])
            target.write_text('{"keep":"original"}', encoding="utf-8")
            os.chmod(target, 0o600)
            with redirect_stdout(StringIO()):
                again = TOOL.main(["--write-template", str(target)])
            self.assertEqual(again, 2)
            self.assertEqual(target.read_text(encoding="utf-8"), '{"keep":"original"}')
            link_target = parent / "linked.json"
            link_target.write_text("safe", encoding="utf-8")
            link = parent / "link.json"
            link.symlink_to(link_target)
            refused = TOOL.write_template(link)
            self.assertFalse(refused["wrote_template"])
            self.assertEqual(link_target.read_text(encoding="utf-8"), "safe")
            open_parent = Path(directory) / "open"
            open_parent.mkdir(mode=0o755)
            self.assertFalse(TOOL.write_template(open_parent / "t.json")["wrote_template"])
            self.assertFalse((open_parent / "t.json").exists())
            missing_parent = parent / "missing" / "t.json"
            self.assertEqual(TOOL.write_template(missing_parent)["reasons"], ["path_parent_missing"])
            self.assertFalse((parent / "missing").exists())
        relative = TOOL.write_template(Path("template.json"))
        self.assertEqual(relative["reasons"], ["path_not_absolute"])
        self.assertFalse((ROOT / "template.json").exists())
        inside = ROOT / "scripts" / "background-trial-template.json"
        refused_inside = TOOL.write_template(inside)
        self.assertEqual(refused_inside["reasons"], ["path_inside_repository"])
        self.assertFalse(inside.exists())

    def test_output_contains_no_identifiers(self):
        payload = paired()
        for item in payload["sessions"]:
            item["device_pseudonym"] = SENTINEL_DEVICE + SENTINEL_PATH
            item["app_build"] = SENTINEL_BUILD + SENTINEL_TRANSCRIPT
        report = TOOL.evaluate_manifest(payload)
        encoded = json.dumps(report, allow_nan=False, sort_keys=True)
        for secret in (SENTINEL_DEVICE, SENTINEL_BUILD, SENTINEL_PATH, SENTINEL_TRANSCRIPT, SENTINEL_COORD):
            self.assertNotIn(secret, encoded)
        self.assertNotIn("device_pseudonym", encoded)
        leaked = paired()
        leaked["note"] = SENTINEL_TRANSCRIPT
        leaked["sessions"][1]["location"]["latitude"] = SENTINEL_COORD
        leaked_report = TOOL.evaluate_manifest(leaked)
        leaked_text = json.dumps(leaked_report, allow_nan=False)
        self.assertEqual(leaked_report["status"], "unavailable")
        self.assertNotIn(SENTINEL_COORD, leaked_text)
        self.assertNotIn(SENTINEL_TRANSCRIPT, leaked_text)
        self.assertNotIn(SENTINEL_DEVICE, leaked_text)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = TOOL.main(["--manifest", str(path)])
            self.assertEqual(code, 0)
            self.assertNotIn(SENTINEL_PATH, stdout.getvalue())
            self.assertNotIn(str(path), stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
