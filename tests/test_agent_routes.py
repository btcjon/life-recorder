import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_support import ViewerCase, add_chunk, sync
import viewer as viewer_mod


class RouteTests(ViewerCase):
    def test_rate_limit_counts_invalid_bodies(self):
        for _ in range(30):
            status, _, _ = self.agent("/v1/search", "not-a-dict")
            self.assertEqual(status, 400)
        status, body, headers = self.agent("/v1/search", {})
        self.assertEqual(status, 503)
        self.assertIn("retry-after", headers)
        self.assertIn(b"rate_limited", body)

    def test_query_string_is_rejected_and_day_payload_is_not_a_fallback(self):
        status, body, _ = self.request("POST", "/v1/search?query=secret", {
            "Host": "lr.genr8ive.ai",
            "Cf-Access-Jwt-Assertion": self.machine_token(),
            "Content-Type": "application/json",
        }, b"{}")
        # Allowlist is unset here, so this is 403. Set it and retry.
        import os
        os.environ["LIFE_RECORDER_AGENT_CLIENT_IDS"] = self.client_id
        status, body, _ = self.request("POST", "/v1/search?query=secret", {
            "Host": "lr.genr8ive.ai",
            "Cf-Access-Jwt-Assertion": self.machine_token(),
            "Content-Type": "application/json",
        }, b"{}")
        self.assertEqual(status, 400)
        self.assertNotIn(b"days", body)

    def test_viewer_stays_on_loopback(self):
        self.assertEqual(self.host, "127.0.0.1")
        self.assertEqual(viewer_mod.VIEWER_HOST, "127.0.0.1")
        with self.inbox.connect() as db:
            add_chunk(db, "2026-09-22T14:00:00Z", "alpha")
            add_chunk(db, "2026-09-22T14:01:00Z", "beta")
        sync(self.inbox)
        blocks = viewer_mod.display_blocks([
            {"id": "a", "started": "2026-09-22T14:00:00Z", "duration": 60, "transcript": "alpha"},
            {"id": "b", "started": "2026-09-22T14:01:00Z", "duration": 60, "transcript": "beta"},
        ])
        self.assertEqual(len(blocks[0]["id"]), 16)


if __name__ == "__main__":
    unittest.main()
