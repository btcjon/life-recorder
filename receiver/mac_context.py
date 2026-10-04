"""Mac output-IO observer. Public CoreAudio helper, compiled once per source hash.

The helper reports process output IO, not audible sound. Browser metadata stays
unavailable by default; this module does not inspect or grant browser permissions.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import threading
import time
import uuid

import context_observations

SAMPLE_SECONDS = 2
HEARTBEAT_SECONDS = 15
EXPIRY_SECONDS = 30
PROBE_TIMEOUT_SECONDS = 1.5
COMPILE_TIMEOUT_SECONDS = 120
PROVENANCE = "mac-coreaudio-public-output-io"
HELPER_SOURCE = Path(__file__).with_name("mac_context_probe.swift")


def browser_adapter_status() -> dict:
    """Capability gate. Do not prompt, launch a browser, or infer a foreground source."""
    return dict(context_observations.BROWSER_ADAPTER)


def producer_id(root: Path) -> str:
    path = root / "mac-context-device-id"
    if path.exists():
        return context_observations.valid_uuid(path.read_text().strip())
    value = str(uuid.uuid4())
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(value + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return value


def helper_binary(cache_dir: Path, source: Path = HELPER_SOURCE, compiler=None, compile_fn=None) -> Path:
    """Compile the attested source once. Later samples execute the cached binary."""
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    binary = cache_dir / f"mac-context-probe-{digest}"
    stamp = cache_dir / f"mac-context-probe-{digest}.sha256"
    if not cache_dir.is_dir() or cache_dir.is_symlink() or cache_dir.stat().st_uid != os.getuid() or cache_dir.stat().st_mode & 0o077:
        raise ValueError("Helper cache must be owner-private")
    if binary.is_file() and not binary.is_symlink() and os.access(binary, os.X_OK) and stamp.is_file() and not stamp.is_symlink():
        try:
            evidence = json.loads(stamp.read_text())
            if (evidence.get("source_sha256") == digest
                    and evidence.get("binary_sha256") == hashlib.sha256(binary.read_bytes()).hexdigest()):
                return binary
        except (ValueError, OSError, AttributeError):
            pass
    tmp = cache_dir / f".mac-context-probe-{digest}.tmp"
    tmp.unlink(missing_ok=True)
    if compile_fn is not None:
        compile_fn(source, tmp)
    else:
        command = list(compiler or ["xcrun", "swiftc", "-O", "-module-cache-path", str(cache_dir / "swift-module-cache")])
        subprocess.run([*command, "-o", str(tmp), str(source)], check=True, timeout=COMPILE_TIMEOUT_SECONDS,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if not tmp.is_file() or tmp.stat().st_size == 0:
        raise RuntimeError("helper compile produced no binary")
    os.chmod(tmp, 0o700)
    os.replace(tmp, binary)
    stamp.write_text(json.dumps({"source_sha256": digest, "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}) + "\n")
    os.chmod(stamp, 0o600)
    return binary


def parse_probe(raw: bytes) -> dict:
    if not raw or len(raw) > 65536:
        raise ValueError("Invalid probe")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload.get("version") != 1 or not isinstance(payload.get("ok"), bool):
        raise ValueError("Invalid probe")
    if set(payload) - {"version", "ok", "processes", "reason"}:
        raise ValueError("Invalid probe")
    if not payload["ok"]:
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason or len(reason) > 80:
            reason = "probe_failed"
        return {"ok": False, "reason": reason, "processes": []}
    processes = payload.get("processes")
    if not isinstance(processes, list) or len(processes) > 64:
        raise ValueError("Invalid probe")
    clean = []
    for item in processes:
        if not isinstance(item, dict) or set(item) - {"bundle_id", "output_io"}:
            raise ValueError("Incomplete probe")
        bundle = item.get("bundle_id")
        if item.get("output_io") is not True or not isinstance(bundle, str):
            raise ValueError("Incomplete probe")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", bundle):
            raise ValueError("Incomplete probe")
        clean.append(bundle)
    return {"ok": True, "reason": None, "processes": sorted(set(clean))}


def run_helper(binary: Path, timeout: float = PROBE_TIMEOUT_SECONDS, stop=None) -> dict:
    proc = subprocess.Popen([str(binary)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + timeout
    try:
        while proc.poll() is None:
            if stop is not None and stop.is_set():
                proc.kill()
                proc.wait(timeout=2)
                return {"ok": False, "reason": "probe_failed", "processes": []}
            if time.monotonic() >= deadline:
                proc.kill()
                proc.wait(timeout=2)
                return {"ok": False, "reason": "probe_failed", "processes": []}
            time.sleep(0.02)
        raw = proc.stdout.read() if proc.stdout is not None else b""
        if proc.returncode != 0:
            return {"ok": False, "reason": "probe_failed", "processes": []}
        return parse_probe(raw)
    except (OSError, ValueError, json.JSONDecodeError, UnicodeError):
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
        return {"ok": False, "reason": "probe_failed", "processes": []}


class MacContextObserver:
    def __init__(self, inbox, stop, *, device_id=None, session_id=None, probe=None,
                 source=None, cache_dir=None, clock=None, compile_fn=None):
        self.inbox = inbox
        self.stop = stop
        self.device_id = device_id or producer_id(inbox.root)
        self.session_id = session_id or str(uuid.uuid4())
        self.sequence = 0
        self.probe = probe
        self.source = source or HELPER_SOURCE
        self.cache_dir = cache_dir or (inbox.root / "cache" / "mac-context")
        self.clock = clock or time.time
        self.compile_fn = compile_fn
        self.active: set[str] = set()
        self.last_state: dict[str, str] = {}
        self.last_emit: dict[str, float] = {}
        self.failure_emit: dict[str, float] = {}
        self.uncertain_apps: set[str] = set()
        self.binary = None
        self.helper_error = None
        self.thread = None
        self._status(helper="starting")

    def _status(self, helper: str, last_sample_at=None) -> None:
        self.inbox.mac_context_status = {
            "running": not self.stop.is_set(),
            "helper": helper,
            "last_sample_at": last_sample_at,
            "session_id": self.session_id,
            "browser_adapter": browser_adapter_status(),
            "measures_audible_sound": False,
            "sample_seconds": SAMPLE_SECONDS,
            "heartbeat_seconds": HEARTBEAT_SECONDS,
            "expiry_seconds": EXPIRY_SECONDS,
        }

    def run(self) -> None:
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self.tick()
            except Exception:
                self._status(helper="probe_failed")
            remaining = SAMPLE_SECONDS - (time.monotonic() - started)
            if self.stop.wait(max(0.0, remaining)):
                break
        self._status(helper=self.inbox.mac_context_status.get("helper", "stopped"))

    def sample(self) -> dict:
        if self.probe is not None:
            return self.probe()
        if self.helper_error and self.binary is None:
            return {"ok": False, "reason": self.helper_error, "processes": []}
        try:
            self.binary = helper_binary(self.cache_dir, self.source, compile_fn=self.compile_fn)
        except Exception:
            self.helper_error = "helper_unavailable"
            self.binary = None
            return {"ok": False, "reason": "helper_unavailable", "processes": []}
        return run_helper(self.binary, stop=self.stop)

    def tick(self, now=None) -> None:
        sample = self.sample()
        now = self.clock() if now is None else float(now)
        self._status(helper="ready" if sample["ok"] else sample["reason"], last_sample_at=now)
        if not sample["ok"]:
            self._emit_failure(sample["reason"] or "probe_failed", now)
            return
        self.failure_emit.clear()
        self.uncertain_apps.clear()
        current = set(sample["processes"])
        for app in sorted(current):
            if self.last_state.get(app) != "active":
                self._emit(app, "active", "output_io_running", now)
            elif now - self.last_emit.get(app, 0) >= HEARTBEAT_SECONDS:
                self._emit(app, "active", "output_io_heartbeat", now)
        for app in sorted(set(self.last_state) - current):
            if self.last_state.get(app) == "inactive":
                continue
            self._emit(app, "inactive", "output_io_stopped", now)

    def _emit_failure(self, reason: str, now: float) -> None:
        apps = sorted(self.active or self.uncertain_apps) or [None]
        for app in apps:
            key = app or ""
            if app:
                self.uncertain_apps.add(app)
                self.active.discard(app)
            previous = self.failure_emit.get(key)
            if previous is not None and now - previous < HEARTBEAT_SECONDS:
                continue
            metadata = {"reason": reason}
            if app:
                metadata["app"] = app
            self._store("unknown", metadata, now)
            self.failure_emit[key] = now
            if app:
                self.last_state[app] = "unknown"
                self.last_emit[app] = now

    def _emit(self, app: str, state: str, reason: str, now: float) -> None:
        self._store(state, {"app": app, "reason": reason}, now)
        self.last_emit[app] = now
        self.last_state[app] = state
        if state == "active":
            self.active.add(app)
        else:
            self.active.discard(app)

    def _store(self, state: str, metadata: dict, now: float) -> None:
        observed = context_observations.format_utc(datetime.fromtimestamp(now, timezone.utc))
        body = {
            "device_id": self.device_id,
            "observation": {
                "version": 1,
                "id": str(uuid.uuid4()),
                "producer_id": self.device_id,
                "session_id": self.session_id,
                "sequence": self.sequence,
                "observed_at": observed,
                "signal": "mac_output",
                "state": state,
                "provenance": PROVENANCE,
                "metadata": metadata,
            },
        }
        result = self.inbox.store_context_observation(self.device_id, body, now=now)
        if not result["duplicate"]:
            self.sequence += 1


def start(inbox, stop) -> MacContextObserver | None:
    if os.environ.get("LIFE_RECORDER_MAC_CONTEXT") == "0":
        inbox.mac_context_status = {
            "running": False, "helper": "disabled", "browser_adapter": browser_adapter_status(),
            "measures_audible_sound": False,
        }
        return None
    observer = MacContextObserver(inbox, stop)
    observer.thread = threading.Thread(target=observer.run, name="mac-context", daemon=True)
    observer.thread.start()
    return observer


def join(observer, timeout: float = 5) -> None:
    if observer is None:
        return
    thread = getattr(observer, "thread", None)
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
