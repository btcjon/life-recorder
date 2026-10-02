"""Offline, synthetic-only hybrid candidate. No production integration or model download.

Run with Python 3 and the existing macOS Swift toolchain. The Apple sentence
model is mandatory; unit tests inject deterministic vectors without substituting
a model in evaluation. Source offsets are Unicode code points, never word times.
"""
import argparse
from datetime import datetime, timedelta
import hashlib
import importlib.util
import itertools
import json
import math
from pathlib import Path
import select
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("offline_lexical", ROOT / "scripts/evaluate-agent-retrieval.py")
lexical = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lexical)
BUDGET = 16384


class ModelUnavailable(RuntimeError):
    pass


class AppleSentenceEmbedding:
    def __init__(self):
        started = time.perf_counter()
        self.process = subprocess.Popen(["/usr/bin/swift", str(ROOT / "scripts/local-sentence-embeddings.swift")],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, bufsize=1)
        self.metadata = self._read(90)
        self.startup_ms = (time.perf_counter() - started) * 1000
        if self.metadata.get("status") != "ready":
            self.close()
            raise ModelUnavailable(json.dumps(self.metadata, sort_keys=True))

    def _read(self, timeout=15):
        if not select.select([self.process.stdout], [], [], timeout)[0]:
            self.close()
            raise ModelUnavailable("Apple sentence bridge timed out; no fallback")
        line = self.process.stdout.readline()
        if not line:
            detail = self.process.stderr.read(2000) if self.process.poll() is not None else "bridge closed"
            self.close()
            raise ModelUnavailable("Apple sentence bridge failed: " + detail)
        return json.loads(line)

    def embed(self, text):
        self.process.stdin.write(json.dumps({"text": text}) + "\n")
        self.process.stdin.flush()
        reply = self._read()
        if reply.get("status") != "ok":
            raise ModelUnavailable("Apple sentence vector unavailable: " + json.dumps(reply))
        return reply["vector"]

    def close(self):
        if getattr(self, "process", None):
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()


def unit_vector(vector):
    if not vector or any(not math.isfinite(v) for v in vector):
        raise ModelUnavailable("Embedding is empty or non-finite")
    length = math.sqrt(sum(v * v for v in vector))
    if not length:
        raise ModelUnavailable("Embedding has zero norm")
    return tuple(v / length for v in vector)


class HybridIndex:
    def __init__(self, db, embedder):
        started = time.perf_counter()
        self.lexical = lexical.AuxiliaryLexicalIndex(db)
        self.embedder = embedder
        self.embedding_ms = 0
        self.vectors = {}
        try:
            for chunk, doc in self.lexical.docs.items():
                before = time.perf_counter()
                self.vectors[chunk] = unit_vector(embedder.embed(doc["text"]))
                self.embedding_ms += (time.perf_counter() - before) * 1000
            dimensions = {len(v) for v in self.vectors.values()}
            if len(dimensions) > 1:
                raise ModelUnavailable("Inconsistent document embedding dimensions")
            self.dimension = next(iter(dimensions), None)
        except Exception:
            self.close()
            raise
        self.build_ms = (time.perf_counter() - started) * 1000

    def close(self):
        self.lexical.close()

    def search(self, body, config, query_vector=None):
        request = lexical.parse_search(body)
        if request["cursor"] or request["place"]:
            raise ValueError("Offline prototype has no cursor/location support")
        terms = {term for term, _, _ in lexical.lexemes(request["query"]) if term not in lexical.STOPWORDS}
        if not terms:
            return {"events": [], "next_cursor": None}
        query_vector = unit_vector(self.embedder.embed(request["query"]) if query_vector is None else query_vector)
        if self.dimension is not None and len(query_vector) != self.dimension:
            raise ModelUnavailable("Query/document embedding dimensions differ")
        count = len(self.lexical.docs)
        weights = {t: 1 + math.log((count + 1) / (self.lexical.frequencies.get(t, 0) + 1)) for t in terms}
        total = sum(weights.values())
        people = self.lexical.all_people if request["include_unconfirmed"] else self.lexical.confirmed
        candidates = []
        for chunk, doc in self.lexical.docs.items():
            stamp = doc["started"]
            if request["start"] and (stamp is None or stamp < request["start"]):
                continue
            if request["end"] and (stamp is None or stamp >= request["end"]):
                continue
            if request["person"] and request["person"].casefold() not in people.get(chunk, set()):
                continue
            matched = terms & doc["terms"]
            coverage = sum(weights[t] for t in matched) / total
            cosine = sum(a * b for a, b in zip(query_vector, self.vectors[chunk]))
            candidates.append((chunk, coverage, cosine, matched))
        lexrank = {r[0]: rank for rank, r in enumerate(sorted(candidates, key=lambda r: (-r[1], r[0])), 1) if r[1] > 0}
        semrank = {r[0]: rank for rank, r in enumerate(sorted(candidates, key=lambda r: (-r[2], r[0]))[:30], 1)}
        scored = []
        for chunk, coverage, cosine, matched in candidates:
            semantic_ok = chunk in semrank and cosine >= config["cosine_min"] and coverage >= config["semantic_lexical_floor"]
            if coverage < config["coverage_min"] and not semantic_ok:
                continue
            alpha = config["semantic_weight"]
            score = ((1 - alpha) / (60 + lexrank[chunk]) if chunk in lexrank else 0) + (alpha / (60 + semrank[chunk]) if chunk in semrank else 0)
            scored.append((-score, -coverage, -cosine, chunk, matched))
        response = {"events": [], "next_cursor": None}
        seen = set()
        for _, _, _, chunk, matched in sorted(scored):
            event = self.lexical.membership.get(chunk, "rec_" + chunk)
            if event in seen:
                continue
            doc = self.lexical.docs[chunk]
            token = max(matched, key=lambda t: (weights[t], t)) if matched else None
            offset = next((s for t, s, _ in doc["positions"] if t == token), 0)
            start = max(0, offset - 160)
            end = min(len(doc["text"]), start + 480)
            match = {"chunk_id": chunk, "text": doc["text"][start:end], "start_offset": start, "end_offset": end,
                     "offset_unit": "unicode_code_points", "attribution": "unknown",
                     "citation": lexical.clip_citation(chunk, doc["text"], doc["started"], start, end)}
            item = {"id": event, "kind": "event" if chunk in self.lexical.membership else "recording", "match": match}
            response["events"].append(item)
            if len(json.dumps(response).encode()) > BUDGET:
                response["events"].pop()
                break
            seen.add(event)
            if len(response["events"]) >= request["limit"]:
                break
        return response


def p95(values):
    return round(sorted(values)[max(0, math.ceil(.95 * len(values)) - 1)], 3) if values else None


def category_metrics(rows):
    positives = [r for r in rows if r["positive"]]
    negatives = [r for r in rows if not r["positive"]]
    return {"cases": len(rows), "positive_cases": len(positives), "negative_cases": len(negatives),
            "recall_at_10": round(sum(r["recall"] for r in positives) / len(positives), 4) if positives else None,
            "precision_at_5": round(sum(r["precision"] for r in positives) / len(positives), 4) if positives else None,
            "negative_accuracy": round(sum(r["negative_correct"] for r in negatives) / len(negatives), 4) if negatives else None,
            "false_positive_negative_cases": sum(not r["negative_correct"] for r in negatives)}


def evaluate():
    raw = lexical.FIXTURE.read_bytes()
    records = json.loads(raw)["cases"]
    assert len(records) == 120 and len({r["id"] for r in records}) == 120
    report = {"fixture_sha256": hashlib.sha256(raw).hexdigest(), "cases": len(records),
              "production_enabled": False, "model": None, "failures": [],
              "limitations": ["Synthetic unique-marker wording limits natural-language generalization.",
                              "Cosine shortlist scans cached synthetic document vectors; no production scale claim.",
                              "Search latency includes fresh query embedding and bridge IPC; build/startup reported separately.",
                              "Clip/person association is never speaker or word attribution.",
                              "Apple model package identity is its API revision/dimension/OS, not a downloadable model hash."]}
    embedder = None
    try:
        embedder = AppleSentenceEmbedding()
        report["model"] = embedder.metadata
        report["model_startup_ms"] = round(embedder.startup_ms, 3)
        with tempfile.TemporaryDirectory() as folder:
            inbox = lexical.Inbox(Path(folder))
            with inbox.connect() as db:
                for row in records:
                    lexical.add_chunk(db, row["started"], row["transcript"], chunk_id=row["chunk_id"])
                    if row["grouped"]:
                        tail = datetime.fromisoformat(row["started"].replace("Z", "+00:00")) + timedelta(minutes=1)
                        lexical.add_chunk(db, tail.isoformat(), "Meeting closed.", chunk_id=row["chunk_id"] + "-tail")
                    if row["person"]:
                        lexical.add_person(db, row["person"], row["chunk_id"], confirmed=row["confirmed"])
            lexical.sync(inbox)
            with inbox.connect() as db:
                index = HybridIndex(db, embedder)
            try:
                tuning = [r for r in records if r["split"] == "tuning"]
                vectors = {r["id"]: embedder.embed(r["request"]["query"]) for r in tuning}
                basetune = lexical.metrics([lexical.score_response(r, lexical.search_events(inbox.db, r["request"])) for r in tuning])
                candidates = []
                for coverage, cosine, floor, weight in itertools.product((.45, .6, .75), (.4, .6, .8), (0, .15, .3), (.25, .5)):
                    config = {"coverage_min": coverage, "cosine_min": cosine, "semantic_lexical_floor": floor, "semantic_weight": weight}
                    score = lexical.metrics([lexical.score_response(r, index.search(r["request"], config, vectors[r["id"]])) for r in tuning])
                    candidates.append({"config": config, "metrics": score})
                eligible = [c for c in candidates if c["metrics"]["precision_at_5"] >= basetune["precision_at_5"] and c["metrics"]["negative_accuracy"] >= basetune["negative_accuracy"]]
                pool = eligible or candidates
                selected = max(pool, key=lambda c: (c["metrics"]["negative_accuracy"], c["metrics"]["recall_at_10"], c["metrics"]["precision_at_5"], c["config"]["semantic_lexical_floor"], c["config"]["cosine_min"]))
                config = dict(selected["config"])  # Frozen before any heldout labels are scored.
                report["selected_config"] = config
                report["selection"] = "Tuning-only precision/negative nonregression, then negative accuracy, recall, precision; conservative threshold tie-break. Grid defined in source before evaluation."
                report["tuning_candidates"] = candidates
                rows = {k: [] for k in ("baseline", "lexical", "hybrid")}
                timing = {k: [] for k in rows}
                # Lexical threshold is independently selected on tuning only.
                lexchoices = [(t, lexical.metrics([lexical.score_response(r, index.lexical.search(r["request"], t)) for r in tuning])) for t in lexical.THRESHOLDS]
                lexeligible = [(t, s) for t, s in lexchoices if s["precision_at_5"] >= basetune["precision_at_5"] and s["negative_accuracy"] >= basetune["negative_accuracy"]]
                lexthreshold = max(lexeligible, key=lambda p: (p[1]["recall_at_10"], p[1]["precision_at_5"], p[0]))[0] if lexeligible else max(lexical.THRESHOLDS)
                report["lexical_threshold"] = lexthreshold
                max_bytes = 0
                evidence_failures = []
                failures = []
                for row in records:
                    for label in rows:
                        started = time.perf_counter()
                        if label == "baseline":
                            response = lexical.search_events(inbox.db, row["request"])
                        elif label == "lexical":
                            response = index.lexical.search(row["request"], lexthreshold)
                        else:
                            response = index.search(row["request"], config)
                        timing[label].append({"split": row["split"], "ms": (time.perf_counter() - started) * 1000})
                        size = len(json.dumps(response).encode())
                        max_bytes = max(size, max_bytes)
                        assert size <= BUDGET
                        for item in response["events"]:
                            match = item["match"]
                            source = index.lexical.docs[match["chunk_id"]]["text"]
                            assert source[match["start_offset"]:match["end_offset"]] == match["text"]
                            assert match["citation"]["transcript_revision"] == hashlib.sha256(source.encode()).hexdigest()
                            assert match["attribution"] == "unknown"
                            for span in row["expected_evidence"]:
                                expected_source = index.lexical.docs[span["chunk_id"]]["text"]
                                assert expected_source[span["start_offset"]:span["end_offset"]] == span["text"]
                                if span["chunk_id"] == match["chunk_id"] and not match["start_offset"] <= span["start_offset"] < span["end_offset"] <= match["end_offset"]:
                                    evidence_failures.append({"case": row["id"], "method": label})
                        result = lexical.score_response(row, response)
                        rows[label].append(result)
                        if label == "hybrid" and ((result["positive"] and result["recall"] < 1) or (not result["positive"] and not result["negative_correct"])):
                            failures.append({"case": row["id"], "category": row["category"], "split": row["split"],
                                             "expected": row["relevant_chunk_ids"], "returned": [e["match"]["chunk_id"] for e in response["events"]]})
                report["metrics"] = {k: {split: lexical.metrics([r for r in data if split == "all" or r["split"] == split]) for split in ("all", "tuning", "heldout")} for k, data in rows.items()}
                report["by_category_heldout"] = {k: {cat: category_metrics([r for r in data if r["split"] == "heldout" and r["category"] == cat]) for cat in sorted({r["category"] for r in records})} for k, data in rows.items()}
                report["p95_ms"] = {k: {split: p95([r["ms"] for r in samples if split == "all" or r["split"] == split]) for split in ("all", "tuning", "heldout")} for k, samples in timing.items()}
                report["build_ms"] = round(index.build_ms, 3)
                report["document_embedding_ms"] = round(index.embedding_ms, 3)
                report["lexical_build_ms"] = round(index.lexical.build_ms, 3)
                report["max_response_bytes"] = max_bytes
                report["evidence_failures"] = evidence_failures
                report["unsupported_attribution_claims"] = 0
                report["splits"] = {split: sum(r["split"] == split for r in records) for split in ("tuning", "heldout")}
                report["multiple_relevance_cases"] = sum(len(r["relevant_chunk_ids"]) > 1 for r in records)
                report["case_failures"] = failures
                h = report["metrics"]["hybrid"]["heldout"]
                b = report["metrics"]["baseline"]["heldout"]
                report["gates"] = {"tuning_nonregression": bool(eligible), "recall_at_10": h["recall_at_10"] >= .9,
                                   "precision_at_5": h["precision_at_5"] >= .7, "precision_nonregression": h["precision_at_5"] >= b["precision_at_5"],
                                   "negative_nonregression": h["negative_accuracy"] >= b["negative_accuracy"],
                                   "p95_under_300ms": report["p95_ms"]["hybrid"]["heldout"] < 300,
                                   "exact_evidence": not evidence_failures, "response_budget": max_bytes <= BUDGET}
                report["synthetic_gate_passed"] = all(report["gates"].values())
                report["status"] = "evaluated"
            finally:
                index.close()
    except ModelUnavailable as error:
        report["status"] = "blocked_model_unavailable"
        report["failures"].append(str(error))
        report["synthetic_gate_passed"] = False
    finally:
        if embedder:
            embedder.close()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write generated synthetic report here")
    args = parser.parse_args()
    result = evaluate()
    rendered = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n")
        print(json.dumps({k: result.get(k) for k in ("status", "model", "selected_config", "metrics", "gates", "failures")}, indent=2))
    else:
        print(rendered)
