import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
import jwt
from jwt.exceptions import InvalidTokenError
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

import access_auth as access_mod
import viewer as viewer_mod
from receiver import Inbox


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


def rsa_pair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key
    public = key.public_key()
    return private, public


class RangeAndConfigTests(unittest.TestCase):
    def test_parse_byte_range(self):
        parse = viewer_mod.ViewerHandler.parse_byte_range
        handler = viewer_mod.ViewerHandler
        self.assertEqual(parse(handler, "bytes=0-3", 10), (0, 3))
        self.assertEqual(parse(handler, "bytes=8-", 10), (8, 9))
        self.assertEqual(parse(handler, "bytes=-4", 10), (6, 9))
        self.assertEqual(parse(handler, "bytes=-20", 10), (0, 9))
        with self.assertRaises(ValueError):
            parse(handler, "bytes=20-30", 10)
        with self.assertRaises(ValueError):
            parse(handler, "bytes=7-3", 10)
        with self.assertRaises(ValueError):
            parse(handler, "bytes=0-1,2-3", 10)
        with self.assertRaises(ValueError):
            parse(handler, "bytes=-0", 10)

    def test_remote_config_pins_issuer_and_origin(self):
        config = access_mod.RemoteAccessConfig.from_values(
            "lr.genr8ive.ai", "example.cloudflareaccess.com", "aud-1")
        self.assertEqual(config.origin, "https://lr.genr8ive.ai")
        self.assertEqual(config.issuer, "https://example.cloudflareaccess.com")
        self.assertEqual(config.jwks_url, "https://example.cloudflareaccess.com/cdn-cgi/access/certs")
        with self.assertRaises(access_mod.AccessAuthError):
            access_mod.RemoteAccessConfig.from_values("127.0.0.1", "example.cloudflareaccess.com", "aud")
        with self.assertRaises(access_mod.AccessAuthError):
            access_mod.RemoteAccessConfig.from_values("lr.genr8ive.ai", "example.com/team", "aud")


class AccessJwtTests(unittest.TestCase):
    def setUp(self):
        self.private, self.public = rsa_pair()
        self.config = access_mod.RemoteAccessConfig.from_values(
            "lr.genr8ive.ai", "example.cloudflareaccess.com", "aud-1")
        self.verifier = access_mod.AccessVerifier(self.config, client=FakeClient(self.public))

    def token(self, **overrides):
        now = int(time.time())
        payload = {"iss": self.config.issuer, "aud": "aud-1", "exp": now + 60, "nbf": now - 10, "sub": "user"}
        payload.update(overrides)
        return jwt.encode(payload, self.private, algorithm="RS256")

    def test_valid_rs256_jwt(self):
        self.verifier.validate(self.token())

    def test_missing_and_forged_jwt(self):
        with self.assertRaises(access_mod.AccessAuthError):
            self.verifier.validate("")
        with self.assertRaises(access_mod.AccessAuthError):
            self.verifier.validate("not-a-jwt")
        other, _ = rsa_pair()
        token = jwt.encode({"iss": self.config.issuer, "aud": "aud-1", "exp": int(time.time()) + 60},
                           other, algorithm="RS256")
        with self.assertRaises(access_mod.AccessAuthError):
            self.verifier.validate(token)

    def test_wrong_iss_aud_alg_and_exp(self):
        with self.assertRaises(access_mod.AccessAuthError):
            self.verifier.validate(self.token(iss="https://evil.example"))
        with self.assertRaises(access_mod.AccessAuthError):
            self.verifier.validate(self.token(aud="other"))
        with self.assertRaises(access_mod.AccessAuthError):
            self.verifier.validate(self.token(exp=int(time.time()) - 10))
        hs = jwt.encode({"iss": self.config.issuer, "aud": "aud-1", "exp": int(time.time()) + 60},
                        "secret", algorithm="HS256")
        with self.assertRaises(access_mod.AccessAuthError):
            self.verifier.validate(hs)


class ViewerHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.inbox = Inbox(Path(self.temp.name))
        self.chunk_id = str(uuid.uuid4())
        audio = self.inbox.audio / (self.chunk_id + ".m4a")
        audio.write_bytes(b"0123456789")
        with self.inbox.connect() as db:
            db.execute("""INSERT INTO chunks (id,sha256,device,started,duration,path,received,status,audio_state)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (self.chunk_id, "a" * 64, str(uuid.uuid4()), "2026-09-10T12:00:00.000Z",
                 1.0, str(audio), 0, "complete", "present"))
        self.private, self.public = rsa_pair()
        self.config = access_mod.RemoteAccessConfig.from_values(
            "lr.genr8ive.ai", "example.cloudflareaccess.com", "aud-1")
        self.server = viewer_mod.start_viewer(
            self.inbox, port=0, remote=self.config)
        self.server.access_verifier = access_mod.AccessVerifier(self.config, client=FakeClient(self.public))
        self.host, self.port = self.server.server_address
        self.token = (self.inbox.root / "viewer.token").read_text().strip()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, method, path, headers=None, body=None):
        headers = {"Host": "127.0.0.1", **(headers or {})}
        client = http.client.HTTPConnection(self.host, self.port, timeout=5)
        client.request(method, path, body=body, headers=headers)
        response = client.getresponse()
        data = response.read()
        hdr = dict(response.getheaders())
        client.close()
        return response.status, data, hdr

    def access_token(self):
        now = int(time.time())
        return jwt.encode({"iss": self.config.issuer, "aud": "aud-1", "exp": now + 60, "nbf": now - 5},
                          self.private, algorithm="RS256")

    def test_local_bearer_still_required(self):
        status, _, _ = self.request("GET", "/v1/days")
        self.assertEqual(status, 401)
        status, body, _ = self.request("GET", "/v1/days", {"Authorization": "Bearer " + self.token})
        self.assertEqual(status, 200)
        self.assertIn("2026-09-10", json.loads(body)["days"])

    def test_remote_host_without_jwt_is_401(self):
        status, _, _ = self.request("GET", "/v1/days", {"Host": "lr.genr8ive.ai"})
        self.assertEqual(status, 401)

    def test_remote_static_assets_require_valid_jwt(self):
        for path in ("/", "/app.css", "/app.js"):
            status, _, _ = self.request("GET", path, {"Host": "lr.genr8ive.ai"})
            self.assertEqual(status, 401)
            status, _, _ = self.request("GET", path, {
                "Host": "lr.genr8ive.ai",
                "Cf-Access-Jwt-Assertion": "aaaa.bbbb.cccc",
            })
            self.assertEqual(status, 401)
            status, _, _ = self.request("GET", path, {
                "Host": "lr.genr8ive.ai",
                "Cf-Access-Jwt-Assertion": self.access_token(),
            })
            self.assertEqual(status, 200)

    def test_remote_jwt_allows_api_and_rejects_forged(self):
        status, _, _ = self.request("GET", "/v1/days", {
            "Host": "lr.genr8ive.ai",
            "Cf-Access-Jwt-Assertion": self.access_token(),
        })
        self.assertEqual(status, 200)
        status, _, _ = self.request("GET", "/v1/days", {
            "Host": "lr.genr8ive.ai",
            "Cf-Access-Jwt-Assertion": "aaaa.bbbb.cccc",
        })
        self.assertEqual(status, 401)

    def test_remote_origin_must_be_exact_https(self):
        headers = {
            "Host": "lr.genr8ive.ai",
            "Cf-Access-Jwt-Assertion": self.access_token(),
            "Content-Type": "application/json",
            "Origin": "https://evil.example",
        }
        status, _, _ = self.request("POST", "/v1/people", headers, json.dumps({"name": "Ada"}).encode())
        self.assertEqual(status, 403)
        headers["Origin"] = "https://lr.genr8ive.ai"
        status, _, _ = self.request("POST", "/v1/people", headers, json.dumps({"name": "Ada"}).encode())
        self.assertEqual(status, 201)

    def test_remote_mutation_rejects_missing_or_local_origin(self):
        base = {
            "Host": "lr.genr8ive.ai",
            "Cf-Access-Jwt-Assertion": self.access_token(),
            "Content-Type": "application/json",
        }
        for origin in (None, "http://localhost:9999", "https://127.0.0.1:1111"):
            headers = dict(base)
            if origin:
                headers["Origin"] = origin
            status, _, _ = self.request("POST", "/v1/people", headers, b'{"name":"Ada"}')
            self.assertEqual(status, 403)

    def test_mutation_rejects_bad_length_type_and_json_shape(self):
        auth = {"Authorization": "Bearer " + self.token, "Origin": "http://127.0.0.1"}
        status, _, _ = self.request("POST", "/v1/people", {
            **auth, "Content-Type": "application/json", "Content-Length": "-1",
        })
        self.assertEqual(status, 400)
        status, _, _ = self.request("POST", "/v1/people", auth, b'{}')
        self.assertEqual(status, 415)
        status, _, _ = self.request("POST", "/v1/people", {
            **auth, "Content-Type": "application/json",
        }, b'[]')
        self.assertEqual(status, 400)

    def test_unknown_host_forbidden(self):
        status, _, _ = self.request("GET", "/v1/days", {
            "Host": "other.example",
            "Authorization": "Bearer " + self.token,
        })
        self.assertEqual(status, 403)

    def test_range_head_and_416(self):
        auth = {"Authorization": "Bearer " + self.token}
        status, body, headers = self.request("GET", "/v1/audio/" + self.chunk_id, {**auth, "Range": "bytes=2-5"})
        self.assertEqual(status, 206)
        self.assertEqual(body, b"2345")
        self.assertEqual(headers.get("Content-Range"), "bytes 2-5/10")
        status, body, headers = self.request("GET", "/v1/audio/" + self.chunk_id, {**auth, "Range": "bytes=-3"})
        self.assertEqual(status, 206)
        self.assertEqual(body, b"789")
        status, body, _ = self.request("GET", "/v1/audio/" + self.chunk_id, {**auth, "Range": "bytes=90-99"})
        self.assertEqual(status, 416)
        status, body, headers = self.request("HEAD", "/v1/audio/" + self.chunk_id, auth)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(headers.get("Content-Length"), "10")
        self.assertEqual(headers.get("Accept-Ranges"), "bytes")

    def test_remote_js_does_not_use_hash_token(self):
        self.assertIn('const remote = !["127.0.0.1", "localhost"].includes(location.hostname);', viewer_mod.JS)
        self.assertIn("return remote ? {} : { Authorization: \"Bearer \" + token };", viewer_mod.JS)
        self.assertNotIn("Access-Control-Allow-Origin", viewer_mod.ViewerHandler._headers.__code__.co_names)


if __name__ == "__main__":
    unittest.main()
