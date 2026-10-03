"""Validate a private background-trial manifest. This does not observe a phone.

The input is manifest-reported evidence, not independently observed phone truth.
``manifest_validated`` means the reported measurements meet the written protocol.
It does not mean a physical trial passed, and it does not enable background capture.
There is no location-accuracy guarantee and no invented coverage threshold.

Protocol for the reported measurements:
- exactly one ``off`` session and one ``on`` session
- timezone-aware ``started_at`` strictly before ``ended_at``
- sessions do not overlap (a shared endpoint is allowed)
- each elapsed duration is at least 4 hours
- elapsed durations differ by at most 5 minutes
- both sessions report the same non-empty device pseudonym and app build
- charging is false, and battery does not increase
- battery percentages are finite numbers from 0 through 100 (not booleans or NaN)
- on-versus-off extra battery discharge is at most 3 percentage points
- each activity set is exactly stationary, walking, and vehicle
- recorded duration is within 60 seconds of elapsed duration
- recorded clip count is at least 1 and verified receipts cover that count
- pending, failed, lost, and duplicate clip counts are 0
- count fields are nonnegative integers, not booleans
- the on session reports at least one fresh observation, finite coverage
  percent from 0 through 100, and a finite nonnegative observation age

Usage:
  python3 scripts/evaluate-background-trial.py --manifest /private/trial.json
  python3 scripts/evaluate-background-trial.py --write-template /private/trial-template.json

``--manifest`` reads at most 1 MiB and does not write a database or activation state.
``--write-template`` creates an exact new owner-private file. The parent must
already exist, be owner-private, and be outside this repository. It does not
overwrite. Template measurement fields are null, not fabricated results.
Output is aggregate JSON only: no device IDs, coordinates, recording paths,
or transcripts.
"""
import argparse
import json
import math
import os
import stat
import sys
from datetime import datetime
from pathlib import Path

SCHEMA = "life-recorder.background-place-trial.v1"
MAX_BYTES = 1024 * 1024
MIN_SECONDS = 4 * 60 * 60
MATCH_SECONDS = 5 * 60
COVERAGE_TOLERANCE_SECONDS = 60
MAX_EXTRA_BATTERY_POINTS = 3
ACTIVITIES = frozenset({"stationary", "walking", "vehicle"})
REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_LABEL = "manifest_reported_not_independently_observed"
TOP_KEYS = frozenset({"schema", "sessions"})
SESSION_KEYS = frozenset({
    "condition", "device_pseudonym", "app_build", "started_at", "ended_at",
    "charging", "battery_start_percent", "battery_end_percent", "activities",
    "recorded_clip_count", "recorded_duration_seconds", "verified_receipt_count",
    "pending_upload_count", "failed_upload_count", "lost_audio_count",
    "duplicate_clip_count", "location",
})
LOCATION_KEYS = frozenset({
    "observation_count", "fresh_observation_count", "coverage_percent",
    "observation_age_seconds",
})
COUNT_FIELDS = (
    "recorded_clip_count", "verified_receipt_count", "pending_upload_count",
    "failed_upload_count", "lost_audio_count", "duplicate_clip_count",
)
CHECK_NAMES = ("completeness", "battery", "continuity")


def template_document():
    """Return an unfilled private manifest. Nulls are unknown, not measurements."""
    def session(condition):
        return {
            "condition": condition,
            "device_pseudonym": None,
            "app_build": None,
            "started_at": None,
            "ended_at": None,
            "charging": None,
            "battery_start_percent": None,
            "battery_end_percent": None,
            "activities": None,
            "recorded_clip_count": None,
            "recorded_duration_seconds": None,
            "verified_receipt_count": None,
            "pending_upload_count": None,
            "failed_upload_count": None,
            "lost_audio_count": None,
            "duplicate_clip_count": None,
            "location": {
                "observation_count": None,
                "fresh_observation_count": None,
                "coverage_percent": None,
                "observation_age_seconds": None,
            },
        }
    return {"schema": SCHEMA, "sessions": [session("off"), session("on")]}


def _issue(check, code, status):
    return {"check": check, "code": code, "status": status}


def _count(value):
    return type(value) is int and value >= 0


def _finite_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _percent(value):
    return _finite_number(value) and 0 <= value <= 100


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and value.strip() == value


def _stamp(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _blank_report():
    return {
        "accuracy_guarantee": None,
        "activation_performed": False,
        "background_enabled": False,
        "evidence_label": EVIDENCE_LABEL,
        "location_coverage_threshold_percent": None,
        "physical_trial_passed": False,
        "reported_additional_battery_percentage_points": None,
        "reported_on_fresh_observation_count": None,
        "reported_on_location_coverage_percent": None,
        "reported_on_observation_age_seconds": None,
        "reported_on_observation_count": None,
    }


def _finish(issues, report=None):
    result = _blank_report()
    if report:
        result.update(report)
    by_check = {name: [] for name in CHECK_NAMES}
    for item in issues:
        by_check[item["check"]].append(item)
    checks = {}
    statuses = []
    reasons = []
    for name in CHECK_NAMES:
        found = by_check[name]
        if any(item["status"] == "unavailable" for item in found):
            status = "unavailable"
        elif found:
            status = "not_passed"
        elif any(item["status"] == "unavailable" for item in issues):
            status = "unavailable"
        else:
            status = "manifest_validated"
        codes = sorted({item["code"] for item in found})
        if status == "unavailable" and not codes:
            codes = ["evidence_unavailable"]
        checks[name] = {"reasons": codes, "status": status}
        statuses.append(status)
        reasons.extend(item["code"] for item in found)
    if "unavailable" in statuses:
        overall = "unavailable"
    elif "not_passed" in statuses:
        overall = "not_passed"
    else:
        overall = "manifest_validated"
    result["checks"] = checks
    result["reasons"] = sorted(set(reasons))
    result["status"] = overall
    return result


def _parse_location(location, condition, issues):
    parsed = {"count": None, "fresh": None, "coverage": None, "age": None}
    if not isinstance(location, dict):
        issues.append(_issue("completeness", "location_missing" if location is None else "location_invalid", "unavailable"))
        return parsed
    if set(location) - LOCATION_KEYS:
        issues.append(_issue("completeness", "unexpected_field", "unavailable"))
    count = location.get("observation_count")
    fresh = location.get("fresh_observation_count")
    if not _count(count):
        issues.append(_issue("completeness", "observation_count_invalid", "unavailable"))
    else:
        parsed["count"] = count
    if not _count(fresh):
        issues.append(_issue("completeness", "fresh_observation_count_invalid", "unavailable"))
    else:
        parsed["fresh"] = fresh
    if parsed["count"] is not None and parsed["fresh"] is not None and parsed["fresh"] > parsed["count"]:
        issues.append(_issue("completeness", "fresh_observation_count_invalid", "unavailable"))
        parsed["fresh"] = None
    coverage = location.get("coverage_percent", None)
    age = location.get("observation_age_seconds", None)
    needs_measurement = condition == "on" or (parsed["count"] or 0) > 0
    if coverage is None:
        if needs_measurement:
            issues.append(_issue("completeness", "coverage_missing", "unavailable"))
    elif not _percent(coverage):
        issues.append(_issue("completeness", "coverage_invalid", "unavailable"))
    else:
        parsed["coverage"] = coverage
    if age is None:
        if needs_measurement:
            issues.append(_issue("completeness", "observation_age_missing", "unavailable"))
    elif not _finite_number(age) or age < 0:
        issues.append(_issue("completeness", "observation_age_invalid", "unavailable"))
    else:
        parsed["age"] = age
    if condition == "on" and parsed["fresh"] == 0:
        issues.append(_issue("completeness", "fresh_observation_below_minimum", "not_passed"))
    return parsed


def _parse_session(session, issues):
    parsed = {"ok": False}
    if not isinstance(session, dict):
        issues.append(_issue("completeness", "session_not_object", "unavailable"))
        return parsed
    if set(session) - SESSION_KEYS:
        issues.append(_issue("completeness", "unexpected_field", "unavailable"))
    condition = session.get("condition")
    if condition not in ("off", "on"):
        issues.append(_issue("completeness", "condition_invalid", "unavailable"))
        return parsed
    parsed["condition"] = condition
    for key, missing, invalid in (
        ("device_pseudonym", "device_missing", "device_invalid"),
        ("app_build", "build_missing", "build_invalid"),
    ):
        value = session.get(key)
        if value is None:
            issues.append(_issue("completeness", missing, "unavailable"))
        elif not _text(value):
            issues.append(_issue("completeness", invalid, "unavailable"))
        else:
            parsed[key] = value
    started = _stamp(session.get("started_at")) if isinstance(session.get("started_at"), str) else None
    ended = _stamp(session.get("ended_at")) if isinstance(session.get("ended_at"), str) else None
    if session.get("started_at") is None or session.get("ended_at") is None:
        issues.append(_issue("completeness", "timestamp_missing", "unavailable"))
    elif not isinstance(session.get("started_at"), str) or not isinstance(session.get("ended_at"), str):
        issues.append(_issue("completeness", "timestamp_malformed", "unavailable"))
    elif started is None or ended is None:
        raw_start = session.get("started_at")
        raw_end = session.get("ended_at")
        naive = False
        for raw in (raw_start, raw_end):
            try:
                candidate = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
            except ValueError:
                candidate = None
            if candidate is not None and candidate.tzinfo is None:
                naive = True
        issues.append(_issue("completeness", "timestamp_not_timezone_aware" if naive else "timestamp_malformed", "unavailable"))
    elif started >= ended:
        issues.append(_issue("completeness", "timestamp_not_ordered", "unavailable"))
    else:
        parsed["started"] = started
        parsed["ended"] = ended
        parsed["elapsed"] = (ended - started).total_seconds()
    charging = session.get("charging")
    if charging is None:
        issues.append(_issue("battery", "charging_missing", "unavailable"))
    elif type(charging) is not bool:
        issues.append(_issue("battery", "charging_invalid", "unavailable"))
    else:
        parsed["charging"] = charging
        if charging:
            issues.append(_issue("battery", "charging_not_allowed", "not_passed"))
    batteries = []
    battery_ok = True
    for key in ("battery_start_percent", "battery_end_percent"):
        value = session.get(key)
        if value is None:
            issues.append(_issue("battery", "battery_missing", "unavailable"))
            battery_ok = False
        elif not _percent(value):
            issues.append(_issue("battery", "battery_invalid", "unavailable"))
            battery_ok = False
        else:
            batteries.append(value)
    if battery_ok and len(batteries) == 2:
        parsed["battery_start"], parsed["battery_end"] = batteries
        if not parsed.get("charging") and batteries[1] > batteries[0]:
            issues.append(_issue("battery", "battery_increased", "not_passed"))
    activities = session.get("activities")
    if activities is None:
        issues.append(_issue("completeness", "activities_missing", "unavailable"))
    elif not isinstance(activities, list) or any(not isinstance(item, str) for item in activities):
        issues.append(_issue("completeness", "activities_invalid", "unavailable"))
    elif any(item not in ACTIVITIES for item in activities) or len(set(activities)) != len(activities):
        issues.append(_issue("completeness", "activities_invalid", "unavailable"))
    else:
        parsed["activities"] = frozenset(activities)
        if parsed["activities"] != ACTIVITIES:
            issues.append(_issue("completeness", "activities_not_covered", "not_passed"))
    duration = session.get("recorded_duration_seconds")
    if duration is None:
        issues.append(_issue("continuity", "recorded_duration_missing", "unavailable"))
    elif not _count(duration):
        issues.append(_issue("continuity", "recorded_duration_invalid", "unavailable"))
    else:
        parsed["recorded_duration"] = duration
    counts = {}
    for key in COUNT_FIELDS:
        value = session.get(key)
        if value is None:
            issues.append(_issue("continuity", "clip_count_missing", "unavailable"))
        elif not _count(value):
            issues.append(_issue("continuity", "clip_count_invalid", "unavailable"))
        else:
            counts[key] = value
    parsed["counts"] = counts
    if "recorded_clip_count" in counts and counts["recorded_clip_count"] < 1:
        issues.append(_issue("continuity", "recorded_clips_below_minimum", "not_passed"))
    if "verified_receipt_count" in counts and "recorded_clip_count" in counts:
        if counts["verified_receipt_count"] < counts["recorded_clip_count"]:
            issues.append(_issue("continuity", "receipts_do_not_cover_clips", "not_passed"))
    for key, code in (
        ("pending_upload_count", "pending_uploads"),
        ("failed_upload_count", "failed_uploads"),
        ("lost_audio_count", "audio_loss"),
        ("duplicate_clip_count", "duplicate_clip_accounting"),
    ):
        if counts.get(key, 0) > 0 and key in counts:
            issues.append(_issue("continuity", code, "not_passed"))
    if "elapsed" in parsed and "recorded_duration" in parsed:
        if abs(parsed["recorded_duration"] - parsed["elapsed"]) > COVERAGE_TOLERANCE_SECONDS:
            issues.append(_issue("continuity", "recording_coverage_gap", "not_passed"))
    parsed["location"] = _parse_location(session.get("location"), condition, issues)
    parsed["ok"] = True
    return parsed


def evaluate_manifest(payload):
    """Return an aggregate report. Never echoes identifiers or raw evidence."""
    if not isinstance(payload, dict):
        return _finish([_issue("completeness", "manifest_not_object", "unavailable")])
    issues = []
    if set(payload) - TOP_KEYS:
        issues.append(_issue("completeness", "unexpected_field", "unavailable"))
    if "schema" not in payload or payload.get("schema") is None:
        issues.append(_issue("completeness", "schema_missing", "unavailable"))
    elif payload.get("schema") != SCHEMA:
        issues.append(_issue("completeness", "schema_unsupported", "unavailable"))
    sessions = payload.get("sessions")
    if sessions is None:
        issues.append(_issue("completeness", "sessions_missing", "unavailable"))
        return _finish(issues)
    if not isinstance(sessions, list):
        issues.append(_issue("completeness", "sessions_malformed", "unavailable"))
        return _finish(issues)
    if not sessions:
        issues.append(_issue("completeness", "sessions_empty", "unavailable"))
        return _finish(issues)
    if len(sessions) != 2:
        issues.append(_issue("completeness", "session_count_invalid", "unavailable"))
    parsed = [_parse_session(session, issues) for session in sessions]
    usable = [item for item in parsed if item.get("condition") in ("off", "on")]
    conditions = [item["condition"] for item in usable]
    if sorted(conditions) != ["off", "on"] or len(usable) != 2:
        issues.append(_issue("completeness", "condition_unmatched", "unavailable"))
        return _finish(issues)
    paired = {item["condition"]: item for item in usable}
    off, on = paired["off"], paired["on"]
    if "device_pseudonym" in off and "device_pseudonym" in on and off["device_pseudonym"] != on["device_pseudonym"]:
        issues.append(_issue("completeness", "device_mismatch", "not_passed"))
    if "app_build" in off and "app_build" in on and off["app_build"] != on["app_build"]:
        issues.append(_issue("completeness", "build_mismatch", "not_passed"))
    if "elapsed" in off and "elapsed" in on:
        if off["elapsed"] < MIN_SECONDS or on["elapsed"] < MIN_SECONDS:
            issues.append(_issue("completeness", "duration_below_minimum", "not_passed"))
        if abs(off["elapsed"] - on["elapsed"]) > MATCH_SECONDS:
            issues.append(_issue("completeness", "duration_mismatch", "not_passed"))
    if "started" in off and "ended" in off and "started" in on and "ended" in on:
        if off["started"] < on["ended"] and on["started"] < off["ended"]:
            issues.append(_issue("completeness", "sessions_overlap", "not_passed"))
    if "activities" in off and "activities" in on and off["activities"] != on["activities"]:
        issues.append(_issue("completeness", "activities_unmatched", "not_passed"))
    report = {}
    if all(key in item for item in (off, on) for key in ("battery_start", "battery_end")):
        if off["battery_end"] <= off["battery_start"] and on["battery_end"] <= on["battery_start"]:
            extra = (on["battery_start"] - on["battery_end"]) - (off["battery_start"] - off["battery_end"])
            report["reported_additional_battery_percentage_points"] = extra
            if extra > MAX_EXTRA_BATTERY_POINTS and not math.isclose(extra, MAX_EXTRA_BATTERY_POINTS, abs_tol=1e-9):
                issues.append(_issue("battery", "battery_delta_exceeds_limit", "not_passed"))
    on_location = on.get("location") or {}
    if on_location.get("coverage") is not None:
        report["reported_on_location_coverage_percent"] = on_location["coverage"]
    if on_location.get("age") is not None:
        report["reported_on_observation_age_seconds"] = on_location["age"]
    if on_location.get("count") is not None:
        report["reported_on_observation_count"] = on_location["count"]
    if on_location.get("fresh") is not None:
        report["reported_on_fresh_observation_count"] = on_location["fresh"]
    return _finish(issues, report)


def load_manifest(path):
    """Read a bounded private manifest and return an aggregate report."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return _finish([_issue("completeness", "manifest_unreadable", "unavailable")])
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return _finish([_issue("completeness", "manifest_not_file", "unavailable")])
        if info.st_size > MAX_BYTES:
            return _finish([_issue("completeness", "manifest_too_large", "unavailable")])
        payload = os.read(fd, MAX_BYTES + 1)
    except OSError:
        return _finish([_issue("completeness", "manifest_unreadable", "unavailable")])
    finally:
        os.close(fd)
    if len(payload) > MAX_BYTES:
        return _finish([_issue("completeness", "manifest_too_large", "unavailable")])
    try:
        text = payload.decode("utf-8")
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate_field')
                result[key] = value
            return result
        parsed = json.loads(text, object_pairs_hook=unique_object)
    except (UnicodeError, ValueError, RecursionError):
        return _finish([_issue("completeness", "manifest_malformed", "unavailable")])
    return evaluate_manifest(parsed)


def _inside_repo(path):
    try:
        path.resolve().relative_to(REPO_ROOT)
    except ValueError:
        return False
    return True


def _template_refusal(path):
    if not isinstance(path, Path) or not path.is_absolute():
        return "path_not_absolute"
    parent = path.parent
    if parent == path or not parent.exists() or not parent.is_dir():
        return "path_parent_missing"
    if parent.is_symlink():
        return "path_parent_not_private"
    if _inside_repo(parent) or _inside_repo(path):
        return "path_inside_repository"
    try:
        info = parent.lstat()
    except OSError:
        return "path_parent_missing"
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        return "path_parent_not_private"
    if path.is_symlink() or path.exists():
        return "path_exists"
    return None


def write_template(path):
    """Create an exact new owner-private template. Never overwrites."""
    reason = _template_refusal(Path(path))
    if reason:
        return {
            "activation_performed": False,
            "background_enabled": False,
            "reasons": [reason],
            "status": "unavailable",
            "wrote_template": False,
        }
    data = (json.dumps(template_document(), indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError:
        return {
            "activation_performed": False,
            "background_enabled": False,
            "reasons": ["template_write_failed"],
            "status": "unavailable",
            "wrote_template": False,
        }
    try:
        os.write(fd, data)
        os.fchmod(fd, 0o600)
    except OSError:
        os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass
        return {
            "activation_performed": False,
            "background_enabled": False,
            "reasons": ["template_write_failed"],
            "status": "unavailable",
            "wrote_template": False,
        }
    os.close(fd)
    return {"activation_performed": False, "background_enabled": False, "wrote_template": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, help="Private JSON manifest, at most 1 MiB")
    parser.add_argument("--write-template", type=Path, help="Exact new owner-private template path outside the repository")
    args = parser.parse_args(argv)
    if (args.manifest is None) == (args.write_template is None):
        result = {
            "activation_performed": False,
            "background_enabled": False,
            "reasons": ["arguments_invalid"],
            "status": "unavailable",
        }
        print(json.dumps(result, allow_nan=False, sort_keys=True))
        return 2
    if args.write_template is not None:
        result = write_template(args.write_template)
        print(json.dumps(result, allow_nan=False, sort_keys=True))
        return 0 if result.get("wrote_template") else 2
    result = load_manifest(args.manifest)
    print(json.dumps(result, allow_nan=False, sort_keys=True))
    if result["status"] == "manifest_validated":
        return 0
    if result["status"] == "not_passed":
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
