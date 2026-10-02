"""Frozen offline evaluation. Never reads the installed runtime.

Precision@5 uses returned results up to five. Empty results on positive cases
score zero; negatives are separate. Recall@10 scores cited relevant clips.
"""
import argparse
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from agent_support import Inbox, add_chunk, add_person, sync
from agent_api.search import search_events

FIXTURE = ROOT / "tests/fixtures/agent-retrieval-v2.json"


def evaluate(baseline_ref=None):
    raw = FIXTURE.read_bytes()
    records = json.loads(raw)["cases"]
    assert len(records) == 120 and len({r["id"] for r in records}) == 120
    timings = []
    outcomes = {"baseline": [], "experimental": []}
    sources = {r["chunk_id"]: r["transcript"] for r in records}
    with tempfile.TemporaryDirectory() as folder:
        inbox = Inbox(Path(folder))
        with inbox.connect() as db:
            for row in records:
                add_chunk(db, row["started"], row["transcript"], chunk_id=row["chunk_id"])
                if row["grouped"]:
                    end = datetime.fromisoformat(row["started"].replace("Z", "+00:00")) + timedelta(minutes=1)
                    tail = row["chunk_id"] + "-tail"
                    sources[tail] = "Meeting closed."
                    add_chunk(db, end.isoformat(), sources[tail], chunk_id=tail)
                if row["person"]:
                    add_person(db, row["person"], row["chunk_id"], confirmed=row["confirmed"])
        sync(inbox)
        for row in records:
            for label in outcomes:
                started = time.perf_counter()
                response = search_events(inbox.db, row["request"], experimental_lexical=label == "experimental")
                if label == "experimental":
                    timings.append((time.perf_counter() - started) * 1000)
                assert len(json.dumps(response).encode()) <= 16384
                got = [item["match"]["chunk_id"] for item in response["events"]]
                expected = set(row["relevant_chunk_ids"])
                for item in response["events"]:
                    match = item["match"]
                    source = sources[match["chunk_id"]]
                    assert source[match["start_offset"]:match["end_offset"]] == match["text"]
                    assert match["citation"]["transcript_revision"] == hashlib.sha256(source.encode()).hexdigest()
                    for expected_span in row["expected_evidence"]:
                        expected_source = sources[expected_span["chunk_id"]]
                        assert expected_source[expected_span["start_offset"]:expected_span["end_offset"]] == expected_span["text"]
                        if expected_span["chunk_id"] == match["chunk_id"]:
                            assert match["start_offset"] <= expected_span["start_offset"] < expected_span["end_offset"] <= match["end_offset"]
                outcomes[label].append({"split": row["split"], "category": row["category"], "positive": bool(expected),
                    "recall": len(expected.intersection(got[:10])) / len(expected) if expected else None,
                    "precision": len(expected.intersection(got[:5])) / max(1, len(got[:5])) if expected else None,
                    "negative_correct": not got if not expected else None})
    def metrics(rows):
        positive = [r for r in rows if r["positive"]]
        negative = [r for r in rows if not r["positive"]]
        return {"cases": len(rows), "recall_at_10": round(sum(r["recall"] for r in positive) / len(positive), 4),
                "precision_at_5": round(sum(r["precision"] for r in positive) / len(positive), 4),
                "negative_accuracy": round(sum(r["negative_correct"] for r in negative) / len(negative), 4) if negative else None}
    report = {label: {"all": metrics(rows), "tuning": metrics([r for r in rows if r["split"] == "tuning"]),
                "heldout": metrics([r for r in rows if r["split"] == "heldout"])} for label, rows in outcomes.items()}
    p95 = sorted(timings)[int(0.95 * (len(timings) - 1))]
    candidate = report["experimental"]["heldout"]
    gates = {"precision_nonregression": candidate["precision_at_5"] >= report["baseline"]["heldout"]["precision_at_5"],
             "recall_at_10": candidate["recall_at_10"] >= 0.90,
             "precision_at_5": candidate["precision_at_5"] >= 0.70, "p95_under_300ms": p95 < 300}
    return {"fixture_sha256": hashlib.sha256(raw).hexdigest(), "cases": 120,
            "splits": {split: sum(r["split"] == split for r in records) for split in ("tuning", "heldout")},
            "categories": {cat: sum(r["category"] == cat for r in records) for cat in sorted({r["category"] for r in records})},
            "baseline": "coverage-complete strict literal search", "historical_baseline_ref": baseline_ref,
            "metrics": report, "experimental_p95_ms": round(p95, 2), "gates": gates,
            "synthetic_gate_passed": all(gates.values()), "production_enabled": False,
            "limitations": "Synthetic unique-marker fixture only; wording variants retain exact marker IDs. Returned-result precision denominator; negatives separate. Candidate available only to offline evaluator, not HTTP. No production latency or word attribution claim. Historical baseline mode unavailable; historical ref is informational, not executed."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-ref", default=None, help="Informational historical reference; current strict literal baseline is used.")
    print(json.dumps(evaluate(parser.parse_args().baseline_ref), indent=2))
