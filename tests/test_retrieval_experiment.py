import hashlib
import importlib.util
import json
from pathlib import Path
from agent_support import IndexCase, add_chunk, add_person, sync

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("retrieval_experiment", ROOT / "scripts/evaluate-agent-retrieval.py")
experiment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(experiment)


class LexicalExperimentTests(IndexCase):
    def setUp(self):
        super().setUp()
        self.text = "😀 CAFÉ budgets approved with uniqueanchor."
        with self.inbox.connect() as db:
            self.first = add_chunk(db, "2026-10-02T14:00:00Z", self.text)
            add_person(db, "Confirmed Person", self.first)
            self.second = add_chunk(db, "2026-10-02T18:00:00Z", "budgets common notes")
            add_chunk(db, "2026-10-03T18:00:00Z", "common notes unrelated")
        sync(self.inbox)
        with self.inbox.connect() as db:
            self.index = experiment.AuxiliaryLexicalIndex(db)

    def tearDown(self):
        self.index.close()
        super().tearDown()

    def test_normalization_stemming_preserves_unicode_source_offsets(self):
        response = self.index.search({"query": "cafe budget"}, 0.75)
        self.assertEqual(len(response["events"]), 1)
        match = response["events"][0]["match"]
        self.assertEqual(match["chunk_id"], self.first)
        self.assertEqual(match["text"], self.text[match["start_offset"]:match["end_offset"]])
        self.assertEqual(match["citation"]["transcript_revision"], hashlib.sha256(self.text.encode()).hexdigest())
        self.assertEqual(match["attribution"], "unknown")
        self.assertEqual(experiment.stem_word("running"), "run")
        self.assertEqual(experiment.stem_word("policies"), "policy")
        self.assertEqual(experiment.stem_word("marker123"), "marker123")
        self.assertEqual(experiment.stem_word("東京"), "東京")

    def test_partial_coverage_weights_rare_evidence_and_penalizes_unknown_terms(self):
        response = self.index.search({"query": "uniqueanchor budget impossible"}, 0.45)
        self.assertEqual([e["match"]["chunk_id"] for e in response["events"]], [self.first])
        self.assertGreater(response["events"][0]["experimental_weighted_term_coverage"], 0.45)
        self.assertEqual(self.index.search({"query": "uniqueanchor budget impossible"}, 0.90)["events"], [])
        self.assertEqual(self.index.search({"query": "nonexistent budget"}, 0.60)["events"], [])

    def test_negation_is_not_dropped_and_no_semantic_synonyms_are_invented(self):
        self.assertNotIn("not", experiment.STOPWORDS)
        self.assertNotIn("never", experiment.STOPWORDS)
        self.assertNotIn("discuss", experiment.STOPWORDS)
        self.assertEqual(self.index.search({"query": "authorization spending"}, 0.45)["events"], [])
        self.assertEqual(self.index.search({"query": "when did we"}, 0.45)["events"], [])

    def test_person_time_filters_apply_to_same_source_clip(self):
        self.assertEqual(len(self.index.search({"query": "budget", "person": "Confirmed Person"}, 0.45)["events"]), 1)
        self.assertEqual(self.index.search({"query": "budget", "person": "Confirmed Person", "from": "2026-10-02T17:00:00Z"}, 0.45)["events"], [])
        response = self.index.search({"query": "budget", "from": "2026-10-02T17:00:00Z", "to": "2026-10-02T19:00:00Z"}, 0.45)
        self.assertEqual(response["events"][0]["match"]["chunk_id"], self.second)

    def test_auxiliary_search_never_mutates_production_index(self):
        with self.inbox.connect() as db:
            before = [tuple(row) for row in db.execute("SELECT chunk_id,body FROM agent_transcripts ORDER BY chunk_id")]
            generation = db.execute("SELECT search_generation FROM agent_api_state").fetchone()[0]
        self.index.search({"query": "cafe budget"}, 0.45)
        with self.inbox.connect() as db:
            after = [tuple(row) for row in db.execute("SELECT chunk_id,body FROM agent_transcripts ORDER BY chunk_id")]
            self.assertEqual(after, before)
            self.assertEqual(db.execute("SELECT search_generation FROM agent_api_state").fetchone()[0], generation)

    def test_frozen_heldout_gate_reports_actual_miss_and_keeps_unreleased(self):
        before = experiment.FIXTURE.read_bytes()
        report = experiment.evaluate()
        self.assertEqual(experiment.FIXTURE.read_bytes(), before)
        self.assertEqual(report["cases"], 120)
        self.assertEqual(report["fixture_sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(report["selected_coverage_threshold"], 0.45)
        self.assertEqual(report["metrics"]["experimental"]["heldout"]["recall_at_10"], 0.8333)
        self.assertFalse(report["gates"]["recall_at_10"])
        self.assertFalse(report["synthetic_gate_passed"])
        self.assertFalse(report["production_enabled"])
        self.assertEqual(report["unsupported_attribution_claims"], 0)
        self.assertIn("production search unchanged", report["limitations"])
        self.assertLess(len(json.dumps(report)), 16384)
