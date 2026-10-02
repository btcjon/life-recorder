"""Synthetic, offline comparison; never reads the installed recorder database."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from agent_support import Inbox, add_chunk, sync
from agent_api.search import search_events


def evaluate(baseline_ref):
    source = subprocess.check_output(["git", "show", baseline_ref + ":receiver/agent_api/search.py"], cwd=ROOT, text=True)
    baseline = types.ModuleType("retrieval_baseline")
    exec(compile(source, "baseline_search.py", "exec"), baseline.__dict__)
    cases = []
    with tempfile.TemporaryDirectory() as folder:
        inbox = Inbox(Path(folder))
        with inbox.connect() as db:
            for i in range(60):
                stamp = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=2*i)
                term = ["café", "budget", "résumé", "東京", "naïve"][i % 5]
                text = ("😀 introductory context " * 100) + f"marker{i} {term} approval " + "closing words " * 15
                chunk = add_chunk(db, stamp.isoformat(), text)
                add_chunk(db, (stamp+timedelta(minutes=1)).isoformat(), "Meeting closed.")
                query = f"marker{i} {term}"
                category = "literal"
                if i >= 45:
                    query = (f"when did we discuss marker{i}" if i < 55 else f"marker{i} authorization")
                    category = "natural_language" if i < 55 else "paraphrase"
                request = {"query": query}
                if i % 3 == 0:
                    request.update({"from":stamp.isoformat(), "to":(stamp+timedelta(hours=1)).isoformat()})
                cases.append((request, chunk, text, category))
        sync(inbox)
        hits = {"before": 0, "after": 0}
        visible = {"before": 0, "after": 0}
        timings = []
        for request, chunk, text, category in cases:
            old = baseline.search_events(inbox.db, request)
            started = time.perf_counter()
            new = search_events(inbox.db, request)
            timings.append((time.perf_counter()-started)*1000)
            assert [e["id"] for e in old["events"]] == [e["id"] for e in new["events"]]
            assert len(json.dumps(new).encode()) <= 16384
            for label, response in [("before",old),("after",new)]:
                hits[label] += bool(response["events"])
                visible[label] += bool(response["events"] and "marker" in response["events"][0]["preview"])
            if new["events"]:
                match = new["events"][0]["match"]
                assert match["chunk_id"] == chunk
                assert text[match["start_offset"]:match["end_offset"]] == match["text"]
                assert match["attribution"] == "unknown"
        return {"baseline_ref":baseline_ref,"cases":60,"categories":{"literal":45,"natural_language":10,"paraphrase":5},
                "events_found":hits,"previews_showing_match":visible,"ranking_and_filters_unchanged":True,
                "exact_offsets_and_response_limit":True,"after_p95_ms":round(sorted(timings)[56],2),
                "limitations":"Synthetic narrow baseline; 15 wording/paraphrase misses remain. No speaker-attribution or production latency claim."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-ref", default="eaf160f")
    print(json.dumps(evaluate(parser.parse_args().baseline_ref), indent=2))
