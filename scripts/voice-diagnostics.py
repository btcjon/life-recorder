#!/usr/bin/env python3
"""Read-only aggregate speaker/queue health; never emit transcript or voice data."""
import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
import voice_id


def diagnose(db, now=None):
    now = time.time() if now is None else now
    profiles = voice_id.manual_profiles(db)
    errors = {}
    for row in db.execute("SELECT last_error,COUNT(*) AS n FROM voice_jobs WHERE last_error IS NOT NULL GROUP BY last_error"):
        key = row["last_error"] if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", row["last_error"] or "") else "redacted"
        errors[key] = errors.get(key, 0) + row["n"]
    return {
        "profiles": [{"samples": p["samples"], "recordings": len(p["clips"]),
                      "clean_seconds": round(p["seconds"], 1), "ready": p["ready"]}
                     for p in profiles.values()],
        "labels": [dict(r) for r in db.execute("SELECT COALESCE(label_source,'unknown') AS source,COUNT(*) AS turns FROM speaker_turns GROUP BY label_source")],
        "queue": [{"reason": r["reason"] if r["reason"] in {"sample", "recover", "startup", "result"} else "other",
                   "count": r["n"], "oldest_seconds": round(max(0, now-r["oldest"]), 1),
                   "max_attempts": r["attempts"]}
                  for r in db.execute("SELECT reason,COUNT(*) AS n,MIN(enqueued_at) AS oldest,MAX(attempts) AS attempts FROM voice_jobs GROUP BY reason")],
        "error_classes": errors,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    args = parser.parse_args()
    with sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        print(json.dumps(diagnose(db), indent=2))


if __name__ == "__main__":
    main()
