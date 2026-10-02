import json
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_support import ViewerCase


class HealthViewerTests(ViewerCase):
    def test_human_health_route_is_aggregate_and_authenticated(self):
        status, raw, _ = self.request("GET", "/v1/health", {"Authorization": "Bearer " + self.token})
        self.assertEqual(status, 200)
        report = json.loads(raw)
        self.assertIn("processing", report)
        self.assertIn("runtime", report)
        self.assertNotIn("cursor_key", raw.decode())
        status, _, _ = self.request("GET", "/v1/health", {})
        self.assertEqual(status, 401)

    def test_machine_credentials_cannot_read_health(self):
        with mock.patch.dict("os.environ", {"LIFE_RECORDER_AGENT_CLIENT_IDS": self.client_id}):
            status, _, _ = self.request("GET", "/v1/health", {
                "Host": "lr.genr8ive.ai", "Cf-Access-Jwt-Assertion": self.machine_token()})
        self.assertEqual(status, 403)
