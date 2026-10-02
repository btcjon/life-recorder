"""Deterministic synthetic candidate checks; no Apple assets needed here."""
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest

from agent_support import IndexCase, add_chunk, add_person, sync

spec = importlib.util.spec_from_file_location("hybrid_candidate", Path(__file__).resolve().parents[1] / "scripts/evaluate-hybrid-retrieval.py")
hybrid = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hybrid)


class StubVectors:
    def embed(self, text):
        return [1., 0.] if "budget" in text.casefold() or "spending" in text.casefold() else [0., 1.]


CONFIG = {"coverage_min": .6, "cosine_min": .8, "semantic_lexical_floor": 0., "semantic_weight": .5}


class HybridRetrievalTests(IndexCase):
    def setUp(self):
        super().setUp()
        with self.inbox.connect() as db:
            add_chunk(db, "2026-01-01T00:00:00Z", "😀 Café budget approval.", chunk_id="old")
            add_chunk(db, "2026-01-02T00:00:00Z", "Budget recruitment notes.", chunk_id="new")
            add_chunk(db, "2026-01-03T00:00:00Z", "Completely unrelated music.", chunk_id="other")
            add_person(db, "Confirmed Person", "old", confirmed=True)
            add_person(db, "Suggested Person", "new", confirmed=False)
        sync(self.inbox)
        with self.inbox.connect() as db:
            self.index = hybrid.HybridIndex(db, StubVectors())

    def tearDown(self):
        self.index.close()
        super().tearDown()

    def ids(self, request, config=CONFIG):
        return [e["match"]["chunk_id"] for e in self.index.search(request, config)["events"]]

    def test_semantic_only_recovers_paraphrase(self):
        self.assertEqual(set(self.ids({"query": "spending", "limit": 10})), {"old", "new"})

    def test_time_filters_survive_semantic_shortlist(self):
        self.assertEqual(self.ids({"query": "spending", "from": "2026-01-02T00:00:00Z", "to": "2026-01-03T00:00:00Z"}), ["new"])

    def test_confirmed_person_filters_survive_semantic_shortlist(self):
        self.assertEqual(self.ids({"query": "spending", "person": "Confirmed Person"}), ["old"])
        self.assertEqual(self.ids({"query": "spending", "person": "Suggested Person"}), [])
        self.assertEqual(self.ids({"query": "spending", "person": "Suggested Person", "include_unconfirmed": True}), ["new"])

    def test_source_unicode_excerpt_and_revision(self):
        item = self.index.search({"query": "budget", "person": "Confirmed Person"}, CONFIG)["events"][0]
        match = item["match"]
        source = "😀 Café budget approval."
        self.assertEqual(source[match["start_offset"]:match["end_offset"]], match["text"])
        self.assertEqual(hashlib.sha256(source.encode()).hexdigest(), match["citation"]["transcript_revision"])
        self.assertEqual(match["attribution"], "unknown")
        self.assertEqual(match["offset_unit"], "unicode_code_points")

    def test_negative_abstention_with_lexical_floor(self):
        strict = dict(CONFIG, semantic_lexical_floor=.3)
        self.assertEqual(self.ids({"query": "nonexistent topic"}, strict), [])

    def test_blank_query_and_limit(self):
        self.assertEqual(self.ids({"query": ""}), [])
        self.assertEqual(len(self.ids({"query": "spending", "limit": 1})), 1)
        with self.assertRaises(Exception):
            self.ids({"query": "budget", "limit": 11})

    def test_rejects_unsupported_filters(self):
        with self.assertRaises(ValueError):
            self.ids({"query": "budget", "place": "office"})
        with self.assertRaises(ValueError):
            self.ids({"query": "budget", "cursor": "future"})

    def test_model_failure_never_silently_uses_lexical(self):
        class Broken:
            def embed(self, text):
                raise hybrid.ModelUnavailable("offline asset missing")
        self.index.embedder = Broken()
        with self.assertRaises(hybrid.ModelUnavailable):
            self.ids({"query": "budget"})
        with self.inbox.connect() as db:
            with self.assertRaises(hybrid.ModelUnavailable):
                hybrid.HybridIndex(db, Broken())

    def test_bad_vectors_rejected(self):
        for vector in ([], [0., 0.], [float("nan"), 1.], [float("inf")]):
            with self.assertRaises(hybrid.ModelUnavailable):
                hybrid.unit_vector(vector)
        with self.assertRaises(hybrid.ModelUnavailable):
            self.index.search({"query": "budget"}, CONFIG, [1., 0., 0.])

    def test_response_budget_uses_encoded_bytes(self):
        # Ten exact Unicode excerpts can exceed 16 KiB, even at 480 code points.
        with self.inbox.connect() as db:
            for n in range(10):
                add_chunk(db, f"2026-02-{n+1:02d}T00:00:00Z", "budget " + "😀" * 600, chunk_id=f"large-{n}")
        sync(self.inbox)
        with self.inbox.connect() as db:
            index = hybrid.HybridIndex(db, StubVectors())
        try:
            response = index.search({"query": "budget", "from": "2026-02-01T00:00:00Z", "limit": 10}, CONFIG)
            self.assertLessEqual(len(json.dumps(response).encode()), 16384)
            self.assertGreater(len(response["events"]), 0)
            self.assertLess(len(response["events"]), 10)
            for event in response["events"]:
                self.assertLessEqual(len(event["match"]["text"]), 480)
        finally:
            index.close()


if __name__ == "__main__":
    unittest.main()
