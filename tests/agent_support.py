import json
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

import access_auth as access_mod
import agent_api
import viewer as viewer_mod
from receiver import Inbox


def rsa_pair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


class FakeKey:
    def __init__(self, key):
        self.key = key


class FakeClient:
    def __init__(self, key, error=None):
        self.key = key
        self.error = error

    def get_signing_key_from_jwt(self, token):
        if self.error:
            raise self.error
        return FakeKey(self.key)


def add_chunk(db, started, transcript, duration=60.0, chunk_id=None):
    chunk_id = chunk_id or str(uuid.uuid4())
    db.execute(
        """INSERT INTO chunks (id,sha256,device,started,duration,path,received,status,transcript)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (chunk_id, "a" * 64, "phone", started, duration, "/tmp/none.m4a", 0, "complete", transcript),
    )
    return chunk_id


def add_person(db, name, chunk_id, confirmed=True):
    person_id = str(uuid.uuid4())
    now = time.time()
    db.execute(
        "INSERT INTO people (id, name, created_at, updated_at) VALUES (?,?,?,?)",
        (person_id, name, now, now),
    )
    db.execute(
        """INSERT INTO speaker_turns
           (id, run_id, chunk_id, speaker_key, started, ended, person_id, label_source)
           VALUES (?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), str(uuid.uuid4()), chunk_id, "S1", 0.0, 1.0, person_id,
         "confirmed" if confirmed else None),
    )
    return person_id


def sync(inbox):
    with inbox.connect() as db:
        agent_api.reconcile(db, viewer_mod.display_blocks)


class IndexCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.inbox = Inbox(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def generation(self):
        with self.inbox.connect() as db:
            return db.execute("SELECT search_generation FROM agent_api_state").fetchone()[0]


class ViewerCase(IndexCase):
    def setUp(self):
        super().setUp()
        self.private, public = rsa_pair()
        self.config = access_mod.RemoteAccessConfig.from_values(
            "lr.genr8ive.ai", "example.cloudflareaccess.com", "aud-1")
        self.server = viewer_mod.start_viewer(self.inbox, port=0, remote=self.config)
        self.server.access_verifier = access_mod.AccessVerifier(self.config, client=FakeClient(public))
        self.host, self.port = self.server.server_address
        self.token = (self.inbox.root / "viewer.token").read_text().strip()
        self.client_id = "agent123.access"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def machine_token(self, client_id=None, **extra):
        now = int(time.time())
        payload = {
            "iss": self.config.issuer,
            "aud": "aud-1",
            "exp": now + 60,
            "nbf": now - 5,
            "sub": "",
            "common_name": client_id or self.client_id,
        }
        payload.update(extra)
        return jwt.encode(payload, self.private, algorithm="RS256")

    def human_token(self):
        now = int(time.time())
        return jwt.encode({
            "iss": self.config.issuer, "aud": "aud-1", "exp": now + 60, "nbf": now - 5, "sub": "user",
            "email": "jon@example.com",
        }, self.private, algorithm="RS256")

    def request(self, method, path, headers=None, body=None):
        import http.client
        client = http.client.HTTPConnection(self.host, self.port, timeout=5)
        client.request(method, path, body=body, headers=headers or {})
        response = client.getresponse()
        raw = response.read()
        header_map = {key.lower(): value for key, value in response.getheaders()}
        client.close()
        return response.status, raw, header_map

    def agent(self, path, payload, client_id=None):
        import os
        previous = os.environ.get("LIFE_RECORDER_AGENT_CLIENT_IDS")
        os.environ["LIFE_RECORDER_AGENT_CLIENT_IDS"] = client_id or self.client_id
        try:
            return self.request("POST", path, {
                "Host": "lr.genr8ive.ai",
                "Cf-Access-Jwt-Assertion": self.machine_token(client_id),
                "Content-Type": "application/json",
            }, json.dumps(payload).encode())
        finally:
            if previous is None:
                os.environ.pop("LIFE_RECORDER_AGENT_CLIENT_IDS", None)
            else:
                os.environ["LIFE_RECORDER_AGENT_CLIENT_IDS"] = previous
