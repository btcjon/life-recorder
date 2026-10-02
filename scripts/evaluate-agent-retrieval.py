"""Frozen offline evaluation. Never reads the installed runtime.

Precision@5 uses returned results up to five. Empty results on positive cases
score zero; negatives are separate. Recall@10 scores cited relevant clips.
"""
import argparse
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from agent_support import Inbox, add_chunk, add_person, sync
from agent_api.search import search_events, _stamp, _show, _chunk_people, _live_members, clip_citation
from agent_api.schemas import parse_search

FIXTURE = ROOT / "tests/fixtures/agent-retrieval-v2.json"

# Fixed before heldout evaluation. Negation and content-bearing verbs remain.
STOPWORDS = frozenset({"a", "an", "the", "when", "where", "what", "did", "do", "does", "we", "i", "you", "please", "find", "about"})
WORD = re.compile(r"[^\W_]+", re.UNICODE)
THRESHOLDS = (0.45, 0.60, 0.75)


def normalize_word(word):
    return "".join(c for c in unicodedata.normalize("NFKD", word.casefold()) if not unicodedata.combining(c))


def stem_word(word):
    """Conservative English inflection stems, not a semantic synonym system.

    Leave identifiers/non-ASCII words intact. Never truncate a stem below three
    characters. Only strip past/progressive endings when the stem has a vowel.
    """
    if not word.isascii() or not word.isalpha() or len(word) < 5:
        return word
    if word.endswith("ies") and len(word) > 5:
        word = word[:-3] + "y"
    elif word.endswith("sses"):
        word = word[:-2]
    elif word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    for suffix in ("ing", "ed"):
        root = word[:-len(suffix)] if word.endswith(suffix) else ""
        if len(root) >= 3 and any(c in "aeiouy" for c in root):
            word = root
            if len(word) > 3 and word[-1] == word[-2] and word[-1] in "bdgmnprt":
                word = word[:-1]
            break
    return word


def lexemes(text):
    return [(stem_word(normalize_word(m.group())), m.start(), m.end()) for m in WORD.finditer(text)]


class AuxiliaryLexicalIndex:
    """Private in-memory FTS5 index; production tables/search are untouched."""
    def __init__(self, db):
        started = time.perf_counter()
        self.index = sqlite3.connect(":memory:")
        self.index.row_factory = sqlite3.Row
        self.index.execute("CREATE VIRTUAL TABLE auxiliary_fts USING fts5(chunk_id UNINDEXED, body, tokenize='unicode61')")
        self.docs = {}
        self.frequencies = {}
        for row in db.execute("SELECT id,started,COALESCE(transcript,'') AS transcript FROM chunks"):
            if not row["transcript"].strip():
                continue
            terms = lexemes(row["transcript"])
            unique = {term for term, _, _ in terms}
            self.docs[row["id"]] = {"text": row["transcript"], "started": _stamp(row["started"]), "terms": unique, "positions": terms}
            for term in unique:
                self.frequencies[term] = self.frequencies.get(term, 0) + 1
            self.index.execute("INSERT INTO auxiliary_fts(chunk_id,body) VALUES(?,?)", (row["id"], " ".join(t[0] for t in terms)))
        self.membership = _live_members(db)
        self.confirmed = _chunk_people(db, False)
        self.all_people = _chunk_people(db, True)
        self.build_ms = (time.perf_counter() - started) * 1000

    def close(self):
        self.index.close()

    def search(self, body, coverage_threshold):
        request = parse_search(body)
        if request["cursor"] or request["place"]:
            raise ValueError("Offline experiment does not implement cursors or location filters.")
        terms = set(term for term, _, _ in lexemes(request["query"]) if term not in STOPWORDS)
        if not terms:
            return {"events": [], "next_cursor": None}
        count = len(self.docs)
        weights = {term: 1 + math.log((count + 1) / (self.frequencies.get(term, 0) + 1)) for term in terms}
        total = sum(weights.values())
        query = " OR ".join('"' + t.replace('"', '""') + '"' for t in sorted(terms))
        scored = []
        people = self.all_people if request["include_unconfirmed"] else self.confirmed
        for hit in self.index.execute("SELECT chunk_id,bm25(auxiliary_fts) AS rank FROM auxiliary_fts WHERE auxiliary_fts MATCH ?", (query,)):
            chunk = hit["chunk_id"]
            doc = self.docs[chunk]
            stamp = doc["started"]
            if request["start"] and (stamp is None or stamp < request["start"]):
                continue
            if request["end"] and (stamp is None or stamp >= request["end"]):
                continue
            if request["person"] and request["person"].casefold() not in people.get(chunk, set()):
                continue
            matched = terms.intersection(doc["terms"])
            coverage = sum(weights[t] for t in matched) / total
            if coverage < coverage_threshold:
                continue
            scored.append(((-coverage, hit["rank"], -(stamp.timestamp() if stamp else 0), chunk), chunk, matched, coverage))
        events, seen = [], set()
        for sort, chunk, matched, coverage in sorted(scored):
            event = self.membership.get(chunk, "rec_" + chunk)
            if event in seen:
                continue
            seen.add(event)
            doc = self.docs[chunk]
            # Citation points to source Unicode positions, never normalized text.
            term = max(matched, key=lambda term: (weights[term], term))
            offset = next(start for token, start, end in doc["positions"] if token == term)
            start, end = max(0, offset-160), min(len(doc["text"]), max(0, offset-160)+480)
            match = {"chunk_id": chunk, "text": doc["text"][start:end], "start_offset": start, "end_offset": end,
                     "offset_unit": "unicode_code_points", "attribution": "unknown",
                     "citation": clip_citation(chunk, doc["text"], doc["started"], start, end)}
            events.append({"id": event, "kind": "event" if chunk in self.membership else "recording", "match": match,
                           "experimental_weighted_term_coverage": round(coverage, 4)})
            if len(events) >= request["limit"]:
                break
        return {"events": events, "next_cursor": None}


def metrics(rows):
    positive = [r for r in rows if r["positive"]]
    negative = [r for r in rows if not r["positive"]]
    return {"cases": len(rows), "recall_at_10": round(sum(r["recall"] for r in positive) / len(positive), 4),
            "precision_at_5": round(sum(r["precision"] for r in positive) / len(positive), 4),
            "negative_accuracy": round(sum(r["negative_correct"] for r in negative) / len(negative), 4) if negative else None}


def score_response(row, response):
    got = [item["match"]["chunk_id"] for item in response["events"]]
    expected = set(row["relevant_chunk_ids"])
    return {"split": row["split"], "category": row["category"], "positive": bool(expected),
            "recall": len(expected.intersection(got[:10])) / len(expected) if expected else None,
            "precision": len(expected.intersection(got[:5])) / max(1, len(got[:5])) if expected else None,
            "negative_correct": not got if not expected else None}


def evaluate(baseline_ref=None):
    raw = FIXTURE.read_bytes()
    records = json.loads(raw)["cases"]
    assert len(records) == 120 and len({r["id"] for r in records}) == 120
    timings = {"baseline": [], "experimental": []}
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
        with inbox.connect() as db:
            index = AuxiliaryLexicalIndex(db)
        # Select only on tuning labels. Heldout labels cannot affect threshold.
        tuning_rows = [r for r in records if r["split"] == "tuning"]
        tuning_baseline = metrics([score_response(r, search_events(inbox.db, r["request"])) for r in tuning_rows])
        tuning_candidates = {threshold: metrics([score_response(r, index.search(r["request"], threshold)) for r in tuning_rows]) for threshold in THRESHOLDS}
        eligible = [threshold for threshold, score in tuning_candidates.items()
                    if score["precision_at_5"] >= tuning_baseline["precision_at_5"] and score["negative_accuracy"] >= tuning_baseline["negative_accuracy"]]
        selected = max(eligible, key=lambda threshold: (tuning_candidates[threshold]["recall_at_10"], tuning_candidates[threshold]["precision_at_5"], threshold)) if eligible else max(THRESHOLDS)
        for row in records:
            for label in outcomes:
                started = time.perf_counter()
                response = search_events(inbox.db, row["request"]) if label == "baseline" else index.search(row["request"], selected)
                timings[label].append({"split": row["split"], "ms": (time.perf_counter() - started) * 1000})
                assert len(json.dumps(response).encode()) <= 16384
                got = [item["match"]["chunk_id"] for item in response["events"]]
                expected = set(row["relevant_chunk_ids"])
                for item in response["events"]:
                    match = item["match"]
                    source = sources[match["chunk_id"]]
                    assert source[match["start_offset"]:match["end_offset"]] == match["text"]
                    assert match["citation"]["transcript_revision"] == hashlib.sha256(source.encode()).hexdigest()
                    assert match["attribution"] == "unknown"
                    for expected_span in row["expected_evidence"]:
                        expected_source = sources[expected_span["chunk_id"]]
                        assert expected_source[expected_span["start_offset"]:expected_span["end_offset"]] == expected_span["text"]
                        if expected_span["chunk_id"] == match["chunk_id"]:
                            assert match["start_offset"] <= expected_span["start_offset"] < expected_span["end_offset"] <= match["end_offset"]
                outcomes[label].append(score_response(row, response))
        index_build_ms = index.build_ms
        index.close()
    report = {label: {"all": metrics(rows), "tuning": metrics([r for r in rows if r["split"] == "tuning"]),
                "heldout": metrics([r for r in rows if r["split"] == "heldout"])} for label, rows in outcomes.items()}
    latency = {}
    for label, samples in timings.items():
        latency[label] = {}
        for split in ("all", "tuning", "heldout"):
            values = sorted(s["ms"] for s in samples if split == "all" or s["split"] == split)
            latency[label][split] = round(values[int(0.95 * (len(values)-1))], 3)
    p95 = latency["experimental"]["heldout"]
    candidate = report["experimental"]["heldout"]
    gates = {"tuning_nonregression": bool(eligible), "precision_nonregression": candidate["precision_at_5"] >= report["baseline"]["heldout"]["precision_at_5"],
             "recall_at_10": candidate["recall_at_10"] >= 0.90,
             "precision_at_5": candidate["precision_at_5"] >= 0.70, "p95_under_300ms": p95 < 300}
    return {"fixture_sha256": hashlib.sha256(raw).hexdigest(), "cases": 120,
            "splits": {split: sum(r["split"] == split for r in records) for split in ("tuning", "heldout")},
            "categories": {cat: sum(r["category"] == cat for r in records) for cat in sorted({r["category"] for r in records})},
            "baseline": "coverage-complete strict literal search", "historical_baseline_ref": baseline_ref,
            "experiment": "private normalized/inflection-stemmed FTS5 index; IDF-weighted partial-term coverage; no synonym map or model",
            "selected_coverage_threshold": selected,
            "tuning_candidates": {str(threshold): score for threshold, score in tuning_candidates.items()},
            "metrics": report, "p95_ms": latency, "auxiliary_index_build_ms": round(index_build_ms, 3), "experimental_p95_ms": p95, "gates": gates,
            "synthetic_gate_passed": all(gates.values()), "production_enabled": False,
            "unsupported_attribution_claims": 0,
            "limitations": "Synthetic unique-marker fixture only; wording variants retain exact marker IDs. Returned-result precision denominator; negatives separate. Auxiliary index and threshold experiment exist only in this script; production search unchanged. Candidate p95 uses cached in-memory metadata and excludes separately reported index build; baseline opens the private database per request. Normalization/stemming cannot establish paraphrase equivalence. No production latency or word attribution claim. Historical baseline mode unavailable; historical ref is informational, not executed."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-ref", default=None, help="Informational historical reference; current strict literal baseline is used.")
    print(json.dumps(evaluate(parser.parse_args().baseline_ref), indent=2))
