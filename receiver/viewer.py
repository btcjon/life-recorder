"""Loopback-only authenticated transcript viewer on 127.0.0.1:8767."""
from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

VIEWER_PORT = 8767
VIEWER_HOST = "127.0.0.1"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'none'; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
LAUNCHER = """#!/bin/zsh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec /usr/bin/python3 "$ROOT/open-viewer.py"
"""
HELPER = """#!/usr/bin/env python3
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parent
token = (root / "viewer.token").read_text().strip()
url = "http://127.0.0.1:8767/#" + token
subprocess.run(["/usr/bin/open", url], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
"""
APP = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Life Recorder</title>
<link rel="stylesheet" href="/app.css">
<body>
<header>
  <h1>Life Recorder</h1>
  <p id="status">Loading</p>
  <label>Day <input id="day" type="date"></label>
  <button id="refresh" type="button">Refresh</button>
  <label>Search <input id="search" type="search"></label>
</header>
<main>
  <aside id="list"></aside>
  <section id="pane"></section>
</main>
<p id="help">Possible event labels are heuristic. TV or podcasts can still match. Deleted audio cannot be played. Speakers not identified.</p>
<script src="/app.js"></script>
</body>
</html>
"""
CSS = """
:root { color-scheme: light; }
html, body { margin: 0; font: 14px/1.45 ui-sans-serif, system-ui, sans-serif; background: #f4f1ea; color: #1b1b1d; }
header { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; padding: 12px 16px; background: #ece7dc; }
h1 { font-size: 16px; margin: 0; }
main { display: grid; grid-template-columns: minmax(220px, 280px) 1fr; min-height: calc(100vh - 120px); }
aside, section { padding: 12px 16px; overflow: auto; }
.item { border: 1px solid #cfc8b8; padding: 8px; margin: 0 0 8px; background: #fff; }
.badge { display: inline-block; padding: 1px 6px; margin-right: 6px; font-size: 11px; border: 1px solid #888; }
.manual { background: #d9ead3; }
.possible { background: #fff2cc; }
.chunk { margin: 0 0 12px; }
.meta { color: #555; font-size: 12px; }
#help { padding: 8px 16px; color: #444; }
@media (max-width: 720px) { main { grid-template-columns: 1fr; } }
"""
JS = r"""
(() => {
  let token = location.hash.replace(/^#/, "");
  if (token) history.replaceState(null, "", location.pathname);
  const status = document.getElementById("status");
  const day = document.getElementById("day");
  const list = document.getElementById("list");
  const pane = document.getElementById("pane");
  const search = document.getElementById("search");
  let payload = null;
  function authHeaders() {
    return { Authorization: "Bearer " + token };
  }
  async function loadDays() {
    const response = await fetch("/v1/days", { headers: authHeaders(), cache: "no-store" });
    if (!response.ok) throw new Error("auth");
    const data = await response.json();
    const last = (data.days && data.days.length) ? data.days[data.days.length - 1] : new Date().toISOString().slice(0, 10);
    if (!day.value) day.value = last;
  }
  async function loadDay() {
    status.textContent = "Loading";
    const response = await fetch("/v1/days/" + day.value, { headers: authHeaders(), cache: "no-store" });
    if (!response.ok) { status.textContent = "Unavailable"; return; }
    payload = await response.json();
    render();
    status.textContent = "Loaded";
  }
  function render() {
    if (!payload) return;
    const query = (search.value || "").toLowerCase();
    list.replaceChildren();
    pane.replaceChildren();
    const intervals = payload.intervals || [];
    const chunks = payload.chunks || [];
    const sessions = payload.sessions || [];
    for (const item of intervals) {
      const node = document.createElement("div");
      node.className = "item";
      const badge = document.createElement("span");
      badge.className = "badge " + (item.source === "manual" ? "manual" : "possible");
      badge.textContent = item.label || (item.source === "manual" ? "Manual meeting" : "Possible event");
      const title = document.createElement("div");
      title.appendChild(badge);
      const when = document.createElement("div");
      when.className = "meta";
      when.textContent = (item.started_at || "") + " -> " + (item.ended_at || "open");
      node.appendChild(title);
      node.appendChild(when);
      list.appendChild(node);
    }
    for (const session of sessions) {
      const heading = document.createElement("h2");
      heading.textContent = session.title || "Capture session";
      pane.appendChild(heading);
    }
    for (const chunk of chunks) {
      const text = chunk.transcript || "";
      if (query && !text.toLowerCase().includes(query)) continue;
      const node = document.createElement("article");
      node.className = "chunk";
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = (chunk.started_local || chunk.started) + " · audio deleted";
      const body = document.createElement("p");
      body.textContent = text;
      node.appendChild(meta);
      node.appendChild(body);
      pane.appendChild(node);
    }
    const pending = document.createElement("p");
    pending.className = "meta";
    pending.textContent = "Pending " + (payload.pending || 0) + ", errors " + (payload.errors || 0) + ". Speakers not identified.";
    pane.appendChild(pending);
  }
  document.getElementById("refresh").addEventListener("click", loadDay);
  day.addEventListener("change", loadDay);
  search.addEventListener("input", render);
  loadDays().then(loadDay).catch(() => { status.textContent = "Open through the local launcher."; });
})();
"""


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class ViewerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LifeViewer"

    def log_message(self, *args):
        pass

    def _inbox(self):
        return self.server.inbox

    def _token(self) -> str:
        return self.server.viewer_token

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return parsed.hostname in ("127.0.0.1", "localhost") and parsed.scheme in ("http", "https")

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        return hmac.compare_digest(supplied.encode(), ("Bearer " + self._token()).encode())

    def _headers(self, content_type: str, length: int, extra=None):
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()

    def _send(self, status: int, body: bytes, content_type: str, extra=None):
        self.send_response(status)
        self._headers(content_type, len(body), extra)
        self.wfile.write(body)
        self.close_connection = True

    def _json(self, status: int, payload: dict):
        self._send(status, json.dumps(payload).encode(), "application/json")

    def do_OPTIONS(self):
        self.send_response(403)
        self._headers("text/plain", 0)
        self.close_connection = True

    def do_GET(self):
        if not self._host_ok() or not self._origin_ok():
            return self._json(403, {"error": "Forbidden"})
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/app.css", "/app.js"):
            assets = {
                "/": (APP.encode(), "text/html; charset=utf-8"),
                "/app.css": (CSS.encode(), "text/css"),
                "/app.js": (JS.encode(), "text/javascript"),
            }
            body, ctype = assets[path]
            return self._send(200, body, ctype)
        if not self._authorized():
            return self._json(401, {"error": "Unauthorized"})
        if path == "/v1/days":
            return self._json(200, {"days": self._inbox().viewer_days()})
        if path.startswith("/v1/days/"):
            day = path.removeprefix("/v1/days/")
            try:
                datetime.strptime(day, "%Y-%m-%d")
            except ValueError:
                return self._json(400, {"error": "Invalid day"})
            return self._json(200, self._inbox().viewer_day(day))
        return self._json(404, {"error": "Not found"})


def viewer_token_path(root: Path) -> Path:
    return root / "viewer.token"


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def ensure_viewer_token(root: Path) -> str:
    path = viewer_token_path(root)
    if not path.exists():
        _atomic_write(path, secrets.token_urlsafe(32).encode())
    os.chmod(path, 0o600)
    return path.read_text().strip()


def write_launcher(root: Path) -> None:
    helper = root / "open-viewer.py"
    command = root / "open-viewer.command"
    _atomic_write(helper, HELPER.encode())
    os.chmod(helper, 0o700)
    _atomic_write(command, LAUNCHER.encode())
    os.chmod(command, 0o700)


def start_viewer(inbox, host: str = VIEWER_HOST, port: int = VIEWER_PORT):
    write_launcher(inbox.root)
    token = ensure_viewer_token(inbox.root)
    if host != VIEWER_HOST:
        raise ValueError("Viewer may bind only to 127.0.0.1")
    try:
        server = ViewerServer((host, port), ViewerHandler)
    except OSError:
        inbox.viewer_error = f"Viewer disabled: port {port} unavailable"
        inbox.viewer_server = None
        return None
    server.inbox = inbox
    server.viewer_token = token
    inbox.viewer_error = None
    inbox.viewer_server = server
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    inbox.viewer_thread = thread
    return server
