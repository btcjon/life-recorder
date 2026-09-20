#!/usr/bin/env python3
"""Prepare private receiver credentials and a certificate-pinned iPhone pairing link."""
import argparse
import hashlib
import html
import json
import os
import plistlib
from pathlib import Path
import shlex
import socket
import ssl
import subprocess
import sys
from urllib.parse import urlencode, urlparse

from receiver import Inbox, atomic_write

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--data-dir", type=Path, required=True)
parser.add_argument("--model", type=Path, required=True)
parser.add_argument("--url", help="Reachable HTTPS URL; defaults to this Mac's .local hostname")
parser.add_argument("--install-agent", action="store_true", help="Start the Mac receiver at login using launchd")
parser.add_argument("--engine", choices=("parakeet", "mlx", "whisper"),
                    help="ASR engine. Omit to keep mlx when both MLX flags exist, otherwise whisper")
parser.add_argument("--mlx-command", help="Optional mlx_whisper executable")
parser.add_argument("--mlx-model", help="Optional local MLX model directory or cached model name")
parser.add_argument("--parakeet-cli", type=Path,
                    default=Path(__file__).resolve().parents[1]
                    / "work/FluidAudio/.build/out/Products/Release/fluidaudiocli")
parser.add_argument("--parakeet-model-dir", type=Path,
                    default=Path.home() / "Library/Application Support/FluidAudio/Models/parakeet-tdt-0.6b-v3")
parser.add_argument("--update-agent", action="store_true", help="Replace this receiver's existing launch-agent command")
args = parser.parse_args()
os.umask(0o077)
root = args.data_dir.resolve()
inbox = Inbox(root)
local_name = subprocess.check_output(["scutil", "--get", "LocalHostName"], text=True).strip() + ".local"
url = args.url or f"https://{local_name}:8765"
parsed = urlparse(url)
if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query:
    parser.error("--url must be an HTTPS address")
cert, key = root / "receiver.crt", root / "receiver.key"
if not cert.exists() or not key.exists():
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:3072", "-nodes", "-sha256", "-days", "365",
                    "-subj", "/CN=Life Recorder", "-keyout", str(key), "-out", str(cert)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
pair = "liferecorder://pair?" + urlencode({"url": url, "token": inbox.token, "pin": pin})
page = f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pair Life Recorder</title><style>
body{{font:18px system-ui;max-width:640px;margin:60px auto;padding:24px;line-height:1.55;background:#faf9f6;color:#202028}}
a{{display:inline-block;padding:14px 24px;background:#5145cd;color:white;border-radius:12px;text-decoration:none}}
code{{overflow-wrap:anywhere;font-size:14px}}dt{{margin-top:20px;color:#666}}dd{{margin:4px 0}}</style>
<h1>Pair your iPhone</h1><p>Install Life Recorder, then open this private page on your iPhone and tap below.</p>
<a href="{html.escape(pair, quote=True)}">Pair Life Recorder</a>
<p>On the same Wi-Fi, this Mac must be awake. For cellular uploads, use a private VPN address and regenerate this page with <code>--url</code>.</p>
<details><summary>Enter settings manually</summary><dl>
<dt>Receiver</dt><dd><code>{html.escape(url)}</code></dd>
<dt>Pairing token</dt><dd><code>{html.escape(inbox.token)}</code></dd>
<dt>Certificate fingerprint</dt><dd><code>{pin}</code></dd></dl></details>
<p>This page contains your private pairing credential. Keep it private.</p>"""
atomic_write(root / "pairing.html", page.encode())
command = [sys.executable, str(Path(__file__).with_name("receiver.py").resolve()),
           "--data-dir", str(root), "--host", "0.0.0.0", "--port", str(parsed.port or 443),
           "--cert", str(cert), "--key", str(key), "--model", str(args.model.resolve()),
           "--whisper", "/opt/homebrew/bin/whisper-cli", "--ffmpeg", "/opt/homebrew/bin/ffmpeg"]
if bool(args.mlx_command) != bool(args.mlx_model):
    parser.error("--mlx-command and --mlx-model must be supplied together")
if args.engine:
    command.extend(["--engine", args.engine])
if args.engine == "parakeet":
    cli = args.parakeet_cli.resolve()
    model_dir = args.parakeet_model_dir.expanduser().resolve()
    if not cli.is_file():
        parser.error("--parakeet-cli must be an existing executable")
    if not model_dir.is_dir():
        parser.error("--parakeet-model-dir must be an existing model directory")
    command.extend(["--parakeet-cli", str(cli), "--parakeet-model-dir", str(model_dir)])
if args.mlx_command:
    mlx_command = Path(args.mlx_command).resolve()
    if not mlx_command.is_file():
        parser.error("--mlx-command must be an existing executable")
    command.extend(["--mlx-command", str(mlx_command), "--mlx-model", args.mlx_model])
atomic_write(root / "start-receiver.command", ("#!/bin/zsh\nexec " + shlex.join(command) + "\n").encode())
os.chmod(root / "start-receiver.command", 0o700)
atomic_write(root / "launch-arguments.json", json.dumps(command).encode())
if args.install_agent:
    agents = Path.home() / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    agent = agents / "com.browseruse.life-recorder.receiver.plist"
    payload = {"Label": "com.browseruse.life-recorder.receiver", "ProgramArguments": command,
               "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
               "WorkingDirectory": str(root), "Umask": 0o077,
               "StandardOutPath": str(root / "receiver.log"), "StandardErrorPath": str(root / "receiver-error.log")}
    agent_updated = False
    if agent.exists():
        existing = plistlib.loads(agent.read_bytes())
        if existing.get("ProgramArguments") != command and not args.update_agent:
            parser.error("A different receiver agent already exists; it was not modified")
        if existing.get("ProgramArguments") != command and args.update_agent:
            atomic_write(agent, plistlib.dumps(payload))
            agent_updated = True
    else:
        atomic_write(agent, plistlib.dumps(payload))
    domain = f"gui/{os.getuid()}"
    running = subprocess.run(["launchctl", "print", domain + "/com.browseruse.life-recorder.receiver"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if agent_updated and running.returncode == 0:
        subprocess.run(["launchctl", "bootout", domain + "/com.browseruse.life-recorder.receiver"], check=True)
        running = subprocess.CompletedProcess(running.args, 1)
    if running.returncode != 0:
        subprocess.run(["launchctl", "bootstrap", domain, str(agent)], check=True)
    subprocess.run(["launchctl", "kickstart", domain + "/com.browseruse.life-recorder.receiver"], check=True)
    print("Mac receiver configured to start at login.")
print(f"Receiver prepared: {url}")
print(f"Private pairing page: {root / 'pairing.html'}")
print(f"Start script: {root / 'start-receiver.command'}")
print("No credentials have been printed. Nothing has been made publicly reachable.")
