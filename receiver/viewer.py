"""Loopback-only authenticated transcript viewer on 127.0.0.1:8767."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from access_auth import AccessAuthError, AccessVerifier, RemoteAccessConfig, VerificationUnavailable
from agent_api import http as agent_http
import event_edits
import speaker_review

VIEWER_PORT = 8767
VIEWER_HOST = "127.0.0.1"
LOCAL_HOSTS = ("127.0.0.1", "localhost")
DEFAULT_REMOTE_HOST = "lr.genr8ive.ai"

GROUP_GAP_SECONDS = 120.0
DISPLAY_ZONE = ZoneInfo("America/New_York")


def _chunk_start(chunk: dict) -> datetime | None:
    raw = chunk.get("started")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return None
    return stamp.astimezone(timezone.utc)


def _duration(chunk: dict) -> float:
    try:
        value = float(chunk.get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0
    if value < 0 or value != value:
        return 0.0
    return value


def _local_date(stamp: datetime):
    return stamp.astimezone(DISPLAY_ZONE).date()


def _local_text(stamp: datetime) -> str:
    return stamp.astimezone(DISPLAY_ZONE).strftime("%Y-%m-%d %H:%M %Z")


def _has_transcript(chunk: dict) -> bool:
    return bool(str(chunk.get("transcript") or "").strip())


def _block_id(ids: list[str]) -> str:
    return hashlib.sha256(",".join(ids).encode()).hexdigest()[:16]


def display_blocks(chunks: list[dict]) -> list[dict]:
    """Group nearby transcribed clips for the sidebar. Display only; nothing is stored."""
    ordered = sorted(
        enumerate(chunks),
        key=lambda item: (
            (0, _chunk_start(item[1]).timestamp(), str(item[1].get("id") or ""))
            if _chunk_start(item[1]) is not None
            else (1, item[0], str(item[1].get("id") or ""))
        ),
    )
    blocks: list[dict] = []
    cluster: list[dict] = []
    pending: list[dict] = []
    transcribed = 0

    def emit_ordinary(items: list[dict]) -> None:
        for item in items:
            blocks.append({"kind": "chunk", "chunk_id": item.get("id")})

    def close_cluster() -> None:
        nonlocal cluster, pending, transcribed
        if transcribed >= 2:
            ids = [str(item.get("id")) for item in cluster]
            start = _chunk_start(cluster[0])
            last = cluster[-1]
            end = _chunk_start(last) + timedelta(seconds=_duration(last))
            preview = next(
                (str(item.get("transcript") or "").strip() for item in cluster if _has_transcript(item)),
                "",
            )
            blocks.append({
                "kind": "event",
                "id": _block_id(ids),
                "chunk_ids": [item.get("id") for item in cluster],
                "started_local": _local_text(start),
                "ended_local": _local_text(end),
                "clip_count": len(cluster),
                "duration": sum(_duration(item) for item in cluster),
                "preview": preview,
            })
        else:
            emit_ordinary(cluster)
        emit_ordinary(pending)
        cluster = []
        pending = []
        transcribed = 0

    for _, chunk in ordered:
        stamp = _chunk_start(chunk)
        if stamp is None:
            close_cluster()
            emit_ordinary([chunk])
            continue
        if not _has_transcript(chunk):
            pending.append(chunk)
            continue
        if not cluster:
            emit_ordinary(pending)
            pending = []
            cluster = [chunk]
            transcribed = 1
            continue
        previous = next(item for item in reversed(cluster) if _has_transcript(item))
        previous_end = _chunk_start(previous) + timedelta(seconds=_duration(previous))
        gap = (stamp - previous_end).total_seconds()
        same_day = _local_date(stamp) == _local_date(_chunk_start(previous))
        if gap <= GROUP_GAP_SECONDS and same_day:
            cluster.extend(pending)
            pending = []
            cluster.append(chunk)
            transcribed += 1
        else:
            close_cluster()
            cluster = [chunk]
            transcribed = 1
    close_cluster()
    return blocks


CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; media-src 'self' blob:; "
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
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<meta name="theme-color" content="#0c1218">
<title>Life Recorder</title>
<link rel="icon" type="image/png" sizes="16x16" href="/favicon-16.png">
<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32.png">
<link rel="icon" type="image/png" sizes="512x512" href="/app-icon.png">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
<link rel="stylesheet" href="/app.css">
<body>
<a class="skip" href="#pane">Skip to recording</a>
<header>
  <div class="header-primary">
    <img class="brand" src="/favicon-32.png" width="32" height="32" alt="Life Recorder">
    <nav aria-label="Library">
      <button id="tab-recordings" type="button" aria-pressed="true">Recordings</button>
      <button id="tab-review" type="button" aria-pressed="false">Review</button>
      <button id="tab-people" type="button" aria-pressed="false">People</button>
    </nav>
    <label class="day-control"><input id="day" type="date" aria-label="Day"></label>
    <button id="refresh" type="button">Refresh</button>
  </div>
  <details id="filters" open>
    <summary>Filters</summary>
    <div class="filter-body">
      <label class="search-field"><input id="search" type="search" placeholder="Search recordings" aria-label="Search recordings"></label>
      <label><input id="show-all" type="checkbox"> Show quiet/pending</label>
      <label><input id="flat-list" type="checkbox"> Flat list</label>
    </div>
  </details>
  <p id="status" aria-live="polite">Loading</p>
</header>
<main>
  <div id="library">
    <aside id="list" aria-label="Recordings"><p class="meta">Loading recordings…</p></aside>
    <section id="pane" tabindex="-1" aria-live="polite"></section>
  </div>
  <section id="people-view" hidden>
    <h2>People</h2>
    <div id="people"></div>
  </section>
  <section id="review-view" hidden aria-labelledby="review-heading">
    <h2 id="review-heading">Review</h2>
    <p id="review-status" class="meta" aria-live="polite">Unconfirmed speaker suggestions.</p>
    <div id="review-list"></div>
  </section>
</main>
<footer id="player-bar">
  <p id="now-playing">No recording selected</p>
  <audio id="player" controls></audio>
  <div id="playback-mode" hidden>
    <button id="play-original" type="button" aria-pressed="true">Original</button>
    <button id="play-enhanced" type="button" aria-pressed="false" disabled>Enhanced</button>
  </div>
  <button id="keep" type="button" hidden>Keep audio</button>
</footer>
<script src="/app.js"></script>
</body>
</html>
"""
CSS = """
:root { color-scheme: dark; --sidebar: 340px; --canvas: #0c1218; --rail: #10161d; --surface: #161e27; --text: #e7eef6; --muted: #8ea0b3; --line: #2a3948; --accent: #8fb8b2; --accent-soft: rgba(143,184,178,.10); --danger: #ff6b6b; }
* { box-sizing: border-box; }
html, body { margin: 0; min-width: 0; overflow-wrap: anywhere; font: 14px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif; background: var(--canvas); color: var(--text); }
body { min-height: 100vh; display: flex; flex-direction: column; }
main { flex: 1 1 auto; min-height: 0; }
.skip { position: absolute; left: -999px; }
.skip:focus { left: 12px; top: 12px; z-index: 10; background: var(--surface); padding: 8px; border-radius: 8px; }
header { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; min-height: 64px; padding: 12px 20px; padding-left: max(20px, env(safe-area-inset-left)); padding-right: max(20px, env(safe-area-inset-right)); background: #0e141b; border-bottom: 1px solid var(--line); }
.header-primary { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; min-width: 0; }
#filters { margin: 0; padding: 0; border: 0; }
#filters .filter-body { display: flex; flex-wrap: nowrap; gap: 10px; align-items: center; min-width: 0; }
#filters .filter-body input[type="search"] { width: 11rem; min-width: 8rem; }

#search { width: 13rem; padding-left: 32px; background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' fill='none' stroke='%238ea0b3' stroke-width='1.8' stroke-linecap='round'%3E%3Ccircle cx='7' cy='7' r='4.5'/%3E%3Cpath d='M10.5 10.5 14 14'/%3E%3C/svg%3E"); background-repeat: no-repeat; background-position: 10px center; }
nav button, #refresh { display: inline-flex; align-items: center; gap: 6px; }
nav button::before, #refresh::before { content: ""; width: 14px; height: 14px; background: currentColor; -webkit-mask: var(--glyph) center / contain no-repeat; mask: var(--glyph) center / contain no-repeat; }
#tab-recordings { --glyph: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M2 3h12v2H2zm0 4h12v2H2zm0 4h8v2H2z'/%3E%3C/svg%3E"); }
#tab-review { --glyph: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M2 3h8v2H2zm0 4h12v2H2zm0 4h6v2H2z'/%3E%3C/svg%3E"); }
#tab-people { --glyph: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M8 8a3 3 0 1 0-3-3 3 3 0 0 0 3 3zm-5 6a5 5 0 0 1 10 0z'/%3E%3C/svg%3E"); }
#review-view { padding: 28px clamp(22px, 5vw, 64px) 100px; overflow: auto; min-height: 0; }
.review-card { max-width: 720px; }
.review-actions, .suggestion-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.suggestion-row { margin: 0 0 8px; }
.review-card .review-choice-label { display: grid; gap: 6px; max-width: 360px; }
.review-choice-label select { width: 100%; }
#review-list > button:first-child { margin-bottom: 12px; }
body.review-mode .day-control, body.review-mode #filters { display: none !important; }
.review-card select, .review-card input[type="checkbox"] { min-height: 34px; }
.review-card label { display: flex; gap: 8px; align-items: center; margin: 8px 0; }
#refresh { --glyph: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M13 8a5 5 0 1 1-1.2-3.2L10 6.5h4V2.5L12.4 4A6.5 6.5 0 1 0 14.5 8z'/%3E%3C/svg%3E"); }
.brand { display: block; width: 32px; height: 32px; margin: 0 4px 0 0; }
h2 { margin: 0; font-size: 28px; letter-spacing: -.03em; font-weight: 680; }
h3 { margin: 0 0 6px; font-size: 14px; }
nav { display: flex; gap: 6px; }
button, select, input { min-height: 34px; padding: 6px 12px; border: 1px solid var(--line); border-radius: 10px; background: var(--surface); color: var(--text); font: inherit; }
#back-recordings { display: none; }
button { cursor: pointer; font-weight: 550; }
button:hover { border-color: #3d5164; background: #1c2733; }
button[aria-pressed="true"] { border-color: rgba(143,184,178,.28); background: var(--accent-soft); color: #c5d9d4; }
button:disabled { cursor: default; opacity: .55; }
button:focus-visible, select:focus-visible, input:focus-visible, .row:focus-visible, summary:focus-visible { outline: 3px solid rgba(143,184,178,.22); outline-offset: 2px; }
header label { display: flex; gap: 6px; align-items: center; color: var(--muted); font-size: 12px; }
#status { margin: 0 0 0 auto; color: var(--muted); font-size: 12px; white-space: nowrap; }
#status.alert { color: #ff8f8f; }
#status.alert::before { content: ""; display: inline-block; width: 7px; height: 7px; margin-right: 6px; border-radius: 50%; background: var(--danger); vertical-align: 1px; }
#library { display: grid; grid-template-columns: var(--sidebar) minmax(0, 1fr); grid-template-rows: minmax(0, 1fr); height: 100%; min-height: 0; }
aside, #pane, #people-view, #review-view { overflow: auto; min-height: 0; }
aside { min-width: 300px; max-width: 360px; width: var(--sidebar); padding: 12px; background: var(--rail); border-right: 1px solid var(--line); }
aside .row { padding: 6px 8px; margin: 0 0 4px; border-radius: 8px; line-height: 1.25; }
aside .preview { margin: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
aside .event { margin: 0 0 8px; padding: 12px; border: 1px solid var(--line); border-radius: 12px; background: var(--surface); }
aside .event:has([aria-current="true"]) { border-color: rgba(143,184,178,.38); box-shadow: none; }
aside .event-toggle { position: relative; width: 100%; min-height: 0; padding: 0 16px 0 0; border: 0; background: transparent; box-shadow: none; text-align: left; font-weight: 450; line-height: 1.25; color: var(--text); }
.event-title { font-weight: 640; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
aside .event-toggle::after { content: "›"; position: absolute; right: 0; top: 0; color: var(--muted); font-weight: 500; }
aside .event-toggle[aria-expanded="true"]::after { content: "⌄"; }
aside .event .row { margin: 4px 0 0; }
aside .event-people { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }
.badge.unconfirmed { background: transparent; border: 1px solid var(--line); color: var(--muted); }
.toolbar button[aria-pressed="true"] { border-color: rgba(143,184,178,.28); background: rgba(143,184,178,.14); color: #d7e6e2; }
.event-summary { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; line-height: 1.35; max-height: 2.7em; margin-top: 6px; font-weight: 400; font-size: 13px; color: #c5d0db; }
.event-tools { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
.event-form { display: grid; gap: 8px; margin-top: 8px; min-width: 0; }
.event-form label { display: grid; gap: 4px; min-width: 0; color: var(--muted); font-size: 12px; }
.event-form input, .event-form select { width: 100%; min-width: 0; }
#pane { padding: 28px clamp(22px, 5vw, 64px) 80px; }
#pane > * { max-width: 860px; }
#people-view { padding: 28px clamp(22px, 5vw, 64px) 100px; }
.row, .item, .person, .group { border: 1px solid var(--line); padding: 12px; margin: 0 0 9px; background: var(--surface); border-radius: 12px; }
.row { width: 100%; text-align: left; cursor: pointer; font-weight: 500; }
.row[aria-current="true"] { border-color: var(--accent); background: var(--accent-soft); box-shadow: none; }
.badge { display: inline-block; padding: 2px 8px; margin-right: 6px; border: 0; border-radius: 999px; font-size: 11px; font-weight: 650; }
.manual, .confirmed { background: rgba(143,184,178,.08); color: #c5d9d4; border: 1px solid rgba(143,184,178,.22); }
.possible, .preserved { background: rgba(255,196,87,.14); color: #ffd98a; }
.unknown { background: #1c2733; color: var(--muted); }
.suggested { background: rgba(120,170,255,.14); color: #c5d8ff; }
.meta { color: var(--muted); font-size: 12px; }
.preview { margin: 4px 0 0; }
.toolbar { display: flex; gap: 8px; flex-wrap: wrap; margin: 8px 0; }
.speakers { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 10px 0 14px; position: relative; }
.brief-turns { flex: 1 1 100%; min-width: 0; max-width: 100%; margin: 0; padding: 0; border: 0; }
.brief-turns > summary { min-width: 0; max-width: 100%; overflow-wrap: anywhere; }
.brief-turns-pills { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; min-width: 0; max-width: 100%; margin-top: 8px; }
.pill { border-radius: 999px; min-height: 30px; padding: 4px 10px; }
.pill[aria-expanded="true"] { box-shadow: 0 0 0 2px rgba(143,184,178,.18); }
.pill.active { box-shadow: 0 0 0 2px rgba(143,184,178,.28); }
.pill[aria-disabled="true"] { cursor: default; opacity: .85; }
.group { position: relative; padding: 14px; }
.group.active { border-color: rgba(143,184,178,.4); box-shadow: none; }
.group-head { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: space-between; }
.group-controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.group-progress { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; width: 100%; margin: 8px 0 4px; }
.group-progress progress { flex: 1 1 160px; min-width: 120px; width: 100%; height: 10px; max-height: 10px; appearance: none; -webkit-appearance: none; border: 0; border-radius: 999px; background: #1c2733; overflow: hidden; }
.group-progress progress::-webkit-progress-bar { background: #1c2733; border-radius: 999px; }
.group-progress progress::-webkit-progress-value, .group-progress progress::-moz-progress-bar { background: var(--accent); border-radius: 999px; }
.group-time { color: var(--muted); font-variant-numeric: tabular-nums; font-size: 12px; }
.group > p { margin: 7px 0; padding-left: 12px; border-left: 3px solid #2a3948; }
.identity-anchor { position: relative; display: inline-flex; min-width: 0; }
.play-toggle[aria-pressed="true"] { border-color: rgba(143,184,178,.28); background: var(--accent-soft); color: #c5d9d4; }
.popover { position: absolute; top: calc(100% + 8px); left: 0; z-index: 20; width: min(300px, calc(100vw - 24px)); padding: 12px; border: 1px solid var(--line); border-radius: 12px; background: #1b2632; box-shadow: 0 12px 32px rgba(0,0,0,.35); }
.person-option { display: flex; width: 100%; margin: 0 0 4px; text-align: left; }
.person-option[aria-pressed="true"] { border-color: rgba(143,184,178,.28); background: var(--accent-soft); }
.popover-footer { display: flex; justify-content: flex-end; gap: 8px; margin-top: 10px; }
.popover details { margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--line); }
#player-bar { position: sticky; bottom: 0; display: grid; grid-template-columns: minmax(160px, .7fr) minmax(280px, 1.4fr) auto auto; gap: 14px; align-items: center; min-height: 74px; padding: 10px 20px; padding-bottom: max(10px, env(safe-area-inset-bottom)); padding-left: max(20px, env(safe-area-inset-left)); padding-right: max(20px, env(safe-area-inset-right)); border-top: 1px solid var(--line); background: #0e141b; }
#now-playing { margin: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
#player { width: min(520px, 100%); }
#playback-mode { display: flex; gap: 6px; }
details { margin-top: 20px; padding-top: 14px; border-top: 1px solid var(--line); }
summary { cursor: pointer; color: var(--muted); }
.hidden { display: none; }
[hidden] { display: none !important; }
.person { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; min-width: 0; }
.person input { min-width: 0; flex: 1 1 160px; }
.popover { overflow-wrap: anywhere; }
@media (max-width: 760px) {
  html, body { overflow-x: hidden; }
  header { gap: 8px; padding: 16px; padding-left: max(16px, env(safe-area-inset-left)); padding-right: max(16px, env(safe-area-inset-right)); }
  .header-primary { width: 100%; }
  button, select, input, summary, .row, .event-toggle, .event-edit, .pill, .person-option, .play-toggle, .replay { min-height: 44px; font-size: 16px; }
  .group-controls { width: 100%; }
  .group-controls button { flex: 1 1 120px; }
  .group-progress { flex-direction: column; align-items: stretch; }
  .group-progress progress { height: 10px; max-height: 10px; }
  #filters { width: 100%; margin: 0; padding: 0; border: 0; }
  #filters > summary { min-height: 44px; font-size: 16px; }
  #filters .filter-body { flex-direction: row; flex-wrap: nowrap; align-items: center; overflow-x: auto; }
  #filters .filter-body input[type="search"] { width: 9rem; min-width: 8rem; }
  #status { width: 100%; margin: 0; }
  #library { display: block; height: auto; min-height: 0; }
  aside, #pane, #people-view, #review-view { overflow: visible; min-width: 0; }
  aside { width: auto; max-width: none; min-width: 0; padding: 16px; padding-left: max(16px, env(safe-area-inset-left)); padding-right: max(16px, env(safe-area-inset-right)); border-right: 0; }
  #pane { padding: 16px; padding-left: max(16px, env(safe-area-inset-left)); padding-right: max(16px, env(safe-area-inset-right)); padding-bottom: 96px; }
  #people-view, #review-view { padding: 16px; padding-left: max(16px, env(safe-area-inset-left)); padding-right: max(16px, env(safe-area-inset-right)); padding-bottom: 96px; }
  .review-card, .review-actions, .suggestion-row { min-width: 0; }
  .review-card button, .review-card select, .review-card label { min-height: 44px; font-size: 16px; }
  .review-actions button, .suggestion-row button { flex: 1 1 140px; }
  body.mobile-list #pane { display: none; }
  body.mobile-detail aside { display: none; }
  body.mobile-list #player-bar, body.mobile-people #player-bar { display: none; }
  #back-recordings { display: inline-flex; align-items: center; }
  .popover { position: static; width: 100%; max-width: none; margin-top: 8px; }
  .popover input, .popover select { min-width: 0; width: 100%; }
  .identity-anchor { display: block; width: 100%; min-width: 0; }
  #player-bar { grid-template-columns: 1fr; }
  #player { width: 100%; }
  .person { flex-direction: column; align-items: stretch; }
  .person input, .person button { width: 100%; }
  .person input { flex: none; }
}
@media (min-width: 761px) {
  html, body { height: 100%; overflow: hidden; }
  header, #player-bar { flex: 0 0 auto; }
  main { min-height: 0; overflow: hidden; display: flex; flex-direction: column; }
  #library, #people-view, #review-view { flex: 1 1 auto; min-height: 0; }
  #filters { display: contents; }
  #filters > summary { display: none; }
  #filters .filter-body { display: flex; flex-wrap: nowrap; }
  #back-recordings { display: none !important; }
}
@media (max-width: 320px) {
  .person { flex-direction: column; align-items: stretch; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { transition: none !important; animation: none !important; }
}
"""
JS = r"""
(() => {
  const remote = !["127.0.0.1", "localhost"].includes(location.hostname);
  let token = "";
  if (!remote) {
    token = location.hash.replace(/^#/, "") || sessionStorage.getItem("life-recorder-token") || "";
    if (location.hash) {
      sessionStorage.setItem("life-recorder-token", token);
      history.replaceState(null, "", location.pathname);
    }
  } else if (location.hash) {
    history.replaceState(null, "", location.pathname);
  }
  const status = document.getElementById("status");
  const day = document.getElementById("day");
  const list = document.getElementById("list");
  const pane = document.getElementById("pane");
  const search = document.getElementById("search");
  const showAll = document.getElementById("show-all");
  const flatList = document.getElementById("flat-list");
  const expandedBlocks = new Set();
  const peoplePane = document.getElementById("people");
  const peopleView = document.getElementById("people-view");
  const library = document.getElementById("library");
  const player = document.getElementById("player");
  const nowPlaying = document.getElementById("now-playing");
  const keepButton = document.getElementById("keep");
  const playbackMode = document.getElementById("playback-mode");
  const playOriginal = document.getElementById("play-original");
  const playEnhanced = document.getElementById("play-enhanced");
  const tabRecordings = document.getElementById("tab-recordings");
  const tabReview = document.getElementById("tab-review");
  const tabPeople = document.getElementById("tab-people");
  const reviewView = document.getElementById("review-view");
  const reviewList = document.getElementById("review-list");
  const reviewStatus = document.getElementById("review-status");
  const filters = document.getElementById("filters");
  let payload = null;
  let audioUrl = null;
  let loadedAudioId = null;
  let audioController = null;
  let audioGeneration = 0;
  let clipStartTime = null;
  let clipStopTime = null;
  let activeGroupKey = null;
  let identityError = "";
  let dayRequest = 0;
  let view = "recordings";
  let selectedId = null;
  let selectedKind = "chunk";
  let playbackKind = "original";
  let detailMode = "transcript";
  let openIdentityKey = null;
  const briefTurnsOpen = new Map();
  let identityDraft = null;
  let identityMode = "list";
  let createdPersonId = null;
  let createdPersonKey = null;
  let createdPersonName = null;
  let identitySample = false;
  let identitySaving = false;
  let reviewTimer = null;
  let identityPointerInside = false;
  let identityNewName = "";
  let mobileDetailOpen = false;
  let listScroll = 0;
  let eventEditor = null;
  let reviewItems = [];
  let reviewCursor = null;
  let reviewLoaded = false;
  let reviewLoading = false;
  let reviewError = "";
  let reviewChoices = {};
  let reviewSamples = {};
  let reviewDiagnostics = null;
  const MOBILE_BP = 760;
  function isMobile() {
    return window.matchMedia("(max-width: " + MOBILE_BP + "px)").matches;
  }
  let mobileLayout = isMobile();
  filters.open = !mobileLayout;
  function authHeaders() {
    return remote ? {} : { Authorization: "Bearer " + token };
  }
  function reasonText(code) {
    return ({
      no_embedding: "No voice sample is attached yet",
      no_enrolled_voiceprints: "No enrolled voices yet",
      no_clean_vector: "This clip has no clean voice sample",
      legacy_centroid: "This voice sample is from an older recording and is not used",
      track_frozen: "This voice track has two names and needs a split",
      track_anchor: "Suggested from this recording",
      needs_confirmation: "Suggested match, not applied",
      "score_below_0.60": "Not a close enough match",
      "score_below_0.85": "Close, but not sure enough to name it",
      "margin_below_0.10": "Too similar to another person",
      need_2_samples: "Needs 2 voice samples",
      need_2_clips: "Needs samples from 2 recordings",
      need_10s: "Needs 10 seconds of confirmed voice",
      need_5s: "Needs 5 seconds of one clean voice",
      confirmed: "Confirmed",
      matched: "Suggested match"
    })[code] || "Needs a closer look";
  }
  function preview(text) {
    const clean = (text || "").replace(/\s+/g, " ").trim();
    return clean ? clean.slice(0, 90) + (clean.length > 90 ? "…" : "") : "No transcript yet";
  }
  function visibleChunks() {
    const query = (search.value || "").toLowerCase();
    return (payload.chunks || []).filter((chunk) => {
      const text = chunk.transcript || "";
      const turns = chunk.speakers || [];
      const turnCount = Number(chunk.speaker_turn_count || 0) || turns.length;
      if (!showAll.checked && chunk.status !== "needs_attention" && !text.trim() && turnCount === 0) return false;
      return !query || text.toLowerCase().includes(query) || (chunk.started_local || "").toLowerCase().includes(query);
    });
  }
  async function loadDays() {
    const response = await fetch("/v1/days", { headers: authHeaders(), cache: "no-store" });
    if (!response.ok) throw new Error("auth");
    const data = await response.json();
    const last = (data.days && data.days.length) ? data.days[data.days.length - 1] : new Date().toISOString().slice(0, 10);
    if (!day.value) day.value = last;
  }
  function listMessage(text) {
    list.replaceChildren();
    const note = document.createElement("p");
    note.className = "meta";
    note.textContent = text;
    list.appendChild(note);
  }
  function scheduleReview(chunkId) {
    clearTimeout(reviewTimer);
    reviewTimer = setTimeout(() => {
      reviewTimer = null;
      if (selectedKind === "chunk" && selectedId === chunkId) loadReview(chunkId);
    }, 2000);
  }
  function ensureReview(chunk) {
    if (!chunk || chunk.reviewState === "loading") return;
    if (Array.isArray(chunk.speakers)) {
      if (chunk.voice_pending && !reviewTimer) scheduleReview(chunk.id);
      return;
    }
    chunk.reviewState = "loading";
    loadReview(chunk.id);
  }
  async function loadReview(chunkId) {
    let response;
    try {
      response = await fetch("/v1/chunks/" + chunkId + "/review", { headers: authHeaders(), cache: "no-store" });
    } catch (error) {
      response = null;
    }
    if (!payload) return;
    const chunk = (payload.chunks || []).find((item) => item.id === chunkId);
    if (!response || !response.ok) {
      if (chunk) {
        chunk.reviewTries = (chunk.reviewTries || 0) + 1;
        chunk.reviewState = "";
      }
      if (chunk && chunk.reviewTries < 3 && selectedKind === "chunk" && selectedId === chunkId) scheduleReview(chunkId);
      return;
    }
    const detail = await response.json();
    if (!payload || selectedKind !== "chunk" || selectedId !== chunkId) return;
    const current = (payload.chunks || []).find((item) => item.id === chunkId);
    if (!current) return;
    const stamp = JSON.stringify(detail.speakers || []);
    const pending = !!detail.voice_pending;
    const changed = current.speakerStamp !== stamp || !Array.isArray(current.speakers);
    current.speakers = detail.speakers || [];
    current.speakerStamp = stamp;
    current.reviewState = "ready";
    current.reviewTries = 0;
    current.voice_pending = pending;
    if (detail.diarization) current.diarization = detail.diarization;
    if (detail.diarization_status) current.diarization_status = detail.diarization_status;
    if (detail.transcript) current.transcript = detail.transcript;
    if (detail.people) payload.people = detail.people;
    if (pending) scheduleReview(chunkId);
    if (changed && !openIdentityKey && !identitySaving) render();
  }
  async function loadDay() {
    const requestId = ++dayRequest;
    clearTimeout(reviewTimer);
    reviewTimer = null;
    status.classList.remove("alert");
    status.textContent = "Loading";
    if (!payload) listMessage("Loading recordings…");
    let response;
    try {
      response = await fetch("/v1/days/" + day.value, { headers: authHeaders(), cache: "no-store" });
    } catch (error) {
      if (requestId !== dayRequest) return;
      status.classList.add("alert");
      status.textContent = "Unavailable";
      if (!payload) listMessage("Couldn't load this day. Refresh to try again.");
      return;
    }
    if (requestId !== dayRequest) return;
    if (!response.ok) {
      status.classList.add("alert");
      status.textContent = "Unavailable";
      if (!payload) listMessage("Couldn't load this day. Refresh to try again.");
      return;
    }
    const nextPayload = await response.json();
    if (requestId !== dayRequest) return;
    payload = nextPayload;
    const stillChunk = selectedKind === "chunk" && (payload.chunks || []).some((chunk) => chunk.id === selectedId);
    if (selectedId && !stillChunk) {
      selectedId = null;
      selectedKind = "chunk";
      mobileDetailOpen = false;
    }
    render();
    const issues = [];
    if (payload.pending) issues.push(payload.pending + " pending");
    if (payload.errors) issues.push(payload.errors + " errors");
    const shadow = payload.activity_shadow || {};
    if (Number(shadow.hold_vad_positive) > 0) issues.push(Number(shadow.hold_vad_positive) + " hold/VAD disagreement");
    status.textContent = issues.length ? issues.join(" · ") : "";
    status.classList.toggle("alert", issues.length > 0);
  }
  function setView(next) {
    view = next;
    tabRecordings.setAttribute("aria-pressed", String(view === "recordings"));
    tabReview.setAttribute("aria-pressed", String(view === "review"));
    tabPeople.setAttribute("aria-pressed", String(view === "people"));
    library.hidden = view !== "recordings";
    peopleView.hidden = view !== "people";
    reviewView.hidden = view !== "review";
    if (view === "review" && !reviewLoaded && !reviewLoading) loadSpeakerReview(true);
    render();
  }
  function identityState(turn) {
    if (turn.preserved) return { label: "Earlier label", cls: "preserved" };
    if (turn.person_id && turn.name) return { label: "Confirmed", cls: "confirmed" };
    if (turn.suggested_name) return { label: "Suggested", cls: "suggested" };
    return { label: "Unknown", cls: "unknown" };
  }
  function groupLabel(group) {
    const turns = group.turns || [];
    const key = group.speaker_key || (turns[0] && turns[0].speaker_key) || "S1";
    if (!turns.length) return { label: "Unknown · No speaker turns available", cls: "unknown", interactive: false };
    const confirmedIds = new Set(turns.filter((turn) => turn.person_id).map((turn) => turn.person_id));
    if (confirmedIds.size > 1) return { label: "Mixed labels", cls: "unknown", interactive: true };
    const named = turns.find((item) => item.name);
    if (confirmedIds.size === 1 && named) {
      const complete = turns.every((turn) => turn.person_id === named.person_id);
      if (turns.every((turn) => turn.preserved)) return { label: named.name + " · Earlier label", cls: "preserved", interactive: true };
      if (complete && turns.every((turn) => turn.label_source === "automatic")) return { label: named.name + " · Auto", cls: "confirmed", interactive: true };
      if (complete) return { label: named.name + " ✓", cls: "confirmed", interactive: true };
      return { label: named.name, cls: "confirmed", interactive: true };
    }
    if (turns.every((turn) => turn.preserved)) return { label: "Earlier label", cls: "preserved", interactive: true };
    const suggested = turns.find((turn) => turn.suggested_name);
    if (suggested && turns.every((turn) => !turn.suggested_name || turn.suggested_name === suggested.suggested_name)) {
      return { label: "Possibly " + suggested.suggested_name, cls: "suggested", interactive: true };
    }
    return { label: "Unknown · " + key, cls: "unknown", interactive: true };
  }
  function enrollmentCopy(person) {
    const samples = Number(person.sample_count || 0);
    const seconds = Number(person.sample_seconds || 0).toFixed(1);
    const clips = Number(person.clip_count || 0);
    const stats = person.name + "'s " + samples + " samples/" + seconds + "s/" + clips + " clips";
    if (person.enrollment_ready) return "Ready to auto-tag matches · " + stats;
    const remaining = Math.max(0, 2 - samples);
    if (remaining === 1) return "One more confirmed voice sample needed for " + stats;
    if (remaining > 1) return remaining + " more confirmed voice samples needed for " + stats;
    return ((person.enrollment_reasons || []).map(reasonText).join(". ") || "Still learning this voice") + " · " + stats;
  }
  function formatTime(value) {
    const seconds = Math.max(0, Number(value) || 0);
    const whole = Math.floor(seconds);
    const minutes = Math.floor(whole / 60);
    const rest = whole % 60;
    return minutes + ":" + String(rest).padStart(2, "0");
  }
  function groupBounds(group) {
    const turns = group.turns || [];
    const starts = turns.map((turn) => Number(turn.started)).filter((value) => Number.isFinite(value));
    const ends = turns.map((turn) => Number(turn.ended)).filter((value) => Number.isFinite(value));
    return {
      start: starts.length ? Math.min.apply(null, starts) : 0,
      end: ends.length ? Math.max.apply(null, ends) : 0,
    };
  }
  function groupTranscript(group) {
    return (group.turns || []).map((turn) => (turn.text || "").trim()).filter(Boolean).join(" ");
  }
  function gapHasOtherSpeaker(turns, left, right) {
    const gapStart = Number(left.ended);
    const gapEnd = Number(right.started);
    if (!Number.isFinite(gapStart) || !Number.isFinite(gapEnd) || gapEnd <= gapStart) return false;
    return turns.some((turn) => {
      if (turn.id === left.id || turn.id === right.id) return false;
      if ((turn.run_id || "") === (left.run_id || "") && (turn.speaker_key || "Unknown") === (left.speaker_key || "Unknown")) return false;
      const start = Number(turn.started);
      const end = Number(turn.ended);
      return Number.isFinite(start) && Number.isFinite(end) && start < gapEnd && end > gapStart;
    });
  }
  function canMergeTurns(currentTurns, next, all) {
    const previous = currentTurns && currentTurns[currentTurns.length - 1];
    if (!previous || !next) return false;
    if ((previous.run_id || "") !== (next.run_id || "")) return false;
    if ((previous.speaker_key || "Unknown") !== (next.speaker_key || "Unknown")) return false;
    const people = new Set();
    for (const turn of currentTurns.concat([next])) {
      if (turn.person_id) people.add(turn.person_id);
    }
    if (people.size > 1) return false;
    return !gapHasOtherSpeaker(all, previous, next);
  }
  function groupTurns(chunk) {
    const turns = (chunk.speakers || []).slice().sort((left, right) => {
      const start = (Number(left.started) || 0) - (Number(right.started) || 0);
      if (start) return start;
      const end = (Number(left.ended) || 0) - (Number(right.ended) || 0);
      if (end) return end;
      return String(left.id || "").localeCompare(String(right.id || ""));
    });
    const groups = [];
    for (const turn of turns) {
      const last = groups[groups.length - 1];
      if (last && canMergeTurns(last.turns, turn, turns)) {
        last.turns.push(turn);
        last.preserved = last.preserved && !!turn.preserved;
        continue;
      }
      groups.push({
        key: chunk.id + ":" + turn.id,
        chunk,
        run_id: turn.run_id,
        speaker_key: turn.speaker_key,
        turns: [turn],
        preserved: !!turn.preserved,
      });
    }
    return groups;
  }
  function briefStretch(group) {
    const bounds = groupBounds(group);
    const duration = bounds.end - bounds.start;
    return Number.isFinite(duration) && duration > 0 && duration < 1;
  }
  function briefDisclosureLabel(count) {
    return count === 1 ? "1 brief turn \u00b7 under 1s" : count + " brief turns \u00b7 under 1s each";
  }
  function placeTranscriptSpeakers(parent, groups) {
    const brief = groups.filter(briefStretch);
    for (const group of groups) {
      if (!briefStretch(group)) parent.appendChild(group.identityAnchor);
    }
    if (brief.length) {
      const details = document.createElement("details");
      details.className = "brief-turns";
      const recordingId = groups[0].chunk.id;
      const wasOpen = briefTurnsOpen.has(recordingId) ? briefTurnsOpen.get(recordingId) : brief.length === groups.length;
      details.open = wasOpen || brief.some((group) => openIdentityKey === group.key);
      details.addEventListener("toggle", () => {
        if (details.isConnected) briefTurnsOpen.set(recordingId, details.open);
      });
      const summary = document.createElement("summary");
      summary.textContent = briefDisclosureLabel(brief.length);
      details.appendChild(summary);
      const body = document.createElement("div");
      body.className = "brief-turns-pills";
      for (const group of brief) body.appendChild(group.identityAnchor);
      details.appendChild(body);
      parent.appendChild(details);
    }
  }

  function updateCardPlayback() {
    const cards = document.querySelectorAll(".group[data-group-key]");
    cards.forEach((card) => {
      const key = card.getAttribute("data-group-key");
      const start = Number(card.getAttribute("data-start"));
      const end = Number(card.getAttribute("data-end"));
      const duration = Math.max(0, end - start);
      const active = activeGroupKey === key && clipStartTime != null;
      const elapsed = active ? Math.max(0, Math.min(duration, player.currentTime - start)) : 0;
      card.classList.toggle("active", active && !player.paused);
      const toggle = card.querySelector(".play-toggle");
      const meter = card.querySelector("progress");
      const clock = card.querySelector(".group-time");
      if (toggle) {
        const playing = active && !player.paused;
        toggle.textContent = playing ? "Pause" : "Play";
        toggle.setAttribute("aria-pressed", String(playing));
        toggle.setAttribute("aria-label", playing ? "Pause this speaker clip" : "Play this speaker clip");
      }
      if (meter) {
        meter.max = duration || 1;
        meter.value = elapsed;
      }
      if (clock) clock.textContent = formatTime(elapsed) + " / " + formatTime(duration);
    });
    document.querySelectorAll(".pill[data-identity-key]").forEach((pill) => {
      const playing = activeGroupKey === pill.getAttribute("data-identity-key") && clipStartTime != null && !player.paused;
      pill.classList.toggle("active", playing);
    });
  }
  player.addEventListener("timeupdate", () => {
    if (clipStartTime != null && player.currentTime < clipStartTime) {
      player.currentTime = clipStartTime;
    }
    if (clipStopTime != null && player.currentTime >= clipStopTime) {
      player.pause();
      if (clipStartTime != null) player.currentTime = clipStopTime;
    }
    updateCardPlayback();
  });
  player.addEventListener("play", () => {
    if (clipStopTime != null && player.currentTime >= clipStopTime && clipStartTime != null) {
      player.currentTime = clipStartTime;
    }
    updateCardPlayback();
  });
  player.addEventListener("pause", updateCardPlayback);
  player.addEventListener("ended", updateCardPlayback);
  function recordedLength(seconds) {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    return total >= 60 ? Math.round(total / 60) + " min" : total + "s";
  }
  function renderList() {
    list.replaceChildren();
    for (const item of payload.intervals || []) {
      const node = document.createElement("div");
      node.className = "item";
      const badge = document.createElement("span");
      badge.className = "badge " + (item.source === "manual" ? "manual" : "possible");
      badge.textContent = item.label || (item.source === "manual" ? "Manual meeting" : "Possible event");
      const when = document.createElement("div");
      when.className = "meta";
      when.textContent = (item.started_at || "") + " -> " + (item.ended_at || "open");
      node.appendChild(badge);
      node.appendChild(when);
      list.appendChild(node);
    }
    for (const session of payload.sessions || []) {
      const node = document.createElement("div");
      node.className = "meta";
      node.textContent = session.title + " · " + session.started;
      list.appendChild(node);
    }
    const chunks = visibleChunks();
    const visible = new Set(chunks.map((chunk) => chunk.id));
    const byId = new Map((payload.chunks || []).map((chunk) => [chunk.id, chunk]));
    const rendered = new Set();
    if (!isMobile() && !selectedId && chunks[0]) { selectedId = chunks[0].id; selectedKind = "chunk"; }
    const flat = !!(flatList && flatList.checked);
    const blocks = flat
      ? chunks.map((chunk) => ({ kind: "chunk", chunk_id: chunk.id }))
      : (payload.display_blocks || chunks.map((chunk) => ({ kind: "chunk", chunk_id: chunk.id })));
    let shown = 0;
    const appendChunk = (chunk, parent) => {
      if (!chunk || rendered.has(chunk.id)) return;
      rendered.add(chunk.id);
      const row = document.createElement("button");
      row.type = "button";
      row.className = "row";
      row.id = "recording-" + chunk.id;
      row.setAttribute("aria-current", selectedKind === "chunk" && chunk.id === selectedId ? "true" : "false");
      const title = document.createElement("div");
      title.textContent = (chunk.started_local || chunk.started || "Recording") + " · " + Number(chunk.duration || 0).toFixed(0) + "s";
      const peek = document.createElement("div");
      peek.className = "preview meta";
      peek.textContent = preview(chunk.transcript);
      row.appendChild(title);
      row.appendChild(peek);
      const audioState = chunk.status === "pending" ? "Processing" : (chunk.status === "needs_attention" ? "Processing needs attention" : (chunk.status !== "complete" ? "Processing failed" : (chunk.audio_playable ? "" : "Audio not kept")));
      if (audioState) {
        const audioNote = document.createElement("div");
        audioNote.className = "meta";
        audioNote.textContent = audioState;
        row.appendChild(audioNote);
      }
      row.addEventListener("click", () => {
        if (selectedKind !== "chunk" || selectedId !== chunk.id) openIdentityKey = null;
        selectedId = chunk.id;
        selectedKind = "chunk";
        if (isMobile()) {
          listScroll = list.scrollTop || window.scrollY || 0;
          mobileDetailOpen = true;
        }
        render();
        if (isMobile()) {
          const back = document.getElementById("back-recordings");
          if (back) back.focus();
        }
      });
      parent.appendChild(row);
      shown += 1;
    };
    for (const block of blocks) {
      if (!flat && block.kind === "event") {
        const members = (block.chunk_ids || []).map((id) => byId.get(id)).filter((chunk) => chunk && visible.has(chunk.id));
        const named = members.filter((chunk) => (chunk.transcript || "").trim());
        const manual = block.source === "manual";
        if ((manual && members.length) || named.length >= 2) {
          const box = document.createElement("div");
          box.className = "event";
          const toggle = document.createElement("button");
          toggle.type = "button";
          toggle.className = "event-toggle";
          const open = expandedBlocks.has(block.id);
          toggle.setAttribute("aria-expanded", open ? "true" : "false");
          if (!open && members.some((chunk) => selectedKind === "chunk" && chunk.id === selectedId)) {
            toggle.setAttribute("aria-current", "true");
          }
          const title = document.createElement("div");
          title.className = "event-title";
          const startLocal = block.started_local || "";
          const endLocal = block.ended_local || "";
          const sameDay = startLocal.slice(0, 10) && startLocal.slice(0, 10) === endLocal.slice(0, 10);
          const range = sameDay && startLocal.length > 16 && endLocal.length > 16
            ? startLocal.slice(11, 16) + "–" + endLocal.slice(11)
            : startLocal + " – " + endLocal;
          title.textContent = manual && block.title ? block.title : ("Event · " + range);
          const meta = document.createElement("div");
          meta.className = "meta";
          const seconds = members.reduce((sum, chunk) => sum + (Number(chunk.duration) || 0), 0);
          meta.textContent = (manual ? "Edited" : "Suggested") + " · " + members.length + " clips · " + recordedLength(seconds);
          toggle.appendChild(title);
          toggle.appendChild(meta);
          const people = document.createElement("div");
          people.className = "event-people";
          const speakers = (block.speakers || []).filter((person) => person && person.confirmed && person.name);
          if (!speakers.length) {
            const unnamed = document.createElement("div");
            unnamed.className = "meta";
            unnamed.textContent = "No named speakers yet.";
            toggle.appendChild(unnamed);
          } else {
            for (const person of speakers) {
              const badge = document.createElement("span");
              badge.className = "badge " + (person.confirmed ? "confirmed" : "unconfirmed");
              badge.textContent = person.name;
              if (!person.confirmed) badge.setAttribute("aria-label", person.name + ", unconfirmed");
              people.appendChild(badge);
            }
            toggle.appendChild(people);
          }
          if (block.summary_state === "ready" && block.summary) {
            if (block.summary_coverage === "sampled") {
              const partial = document.createElement("div");
              partial.className = "meta";
              partial.textContent = "Partial summary.";
              toggle.appendChild(partial);
            }
            const summary = document.createElement("div");
            summary.className = "event-summary";
            summary.textContent = block.summary;
            toggle.appendChild(summary);
          } else {
            const peek = document.createElement("div");
            peek.className = "preview meta";
            const excerpt = block.preview || (named[0] && named[0].transcript) || "";
            peek.textContent = "Excerpt · " + preview(excerpt);
            toggle.appendChild(peek);
            const status = document.createElement("div");
            status.className = "meta";
            status.textContent = block.summary_state === "off" ? "AI summary off"
              : (block.summary_state === "unavailable" ? "Summary unavailable" : "Summary pending");
            toggle.appendChild(status);
          }
          if (manual && block.suggestion && block.suggestion.parts && block.suggestion.parts.length) {
            const suggestion = document.createElement("div");
            suggestion.className = "meta";
            suggestion.textContent = "Suggested: " + block.suggestion.parts.map((part) => {
              if (part.kind === "event") return (part.started_local || "") + " – " + (part.ended_local || "");
              return part.started_local || "Recording";
            }).join("; ");
            toggle.appendChild(suggestion);
          }
          toggle.addEventListener("click", () => {
            if (expandedBlocks.has(block.id)) expandedBlocks.delete(block.id);
            else expandedBlocks.add(block.id);
            renderList();
          });
          box.appendChild(toggle);
          const tools = document.createElement("div");
          tools.className = "event-tools";
          const edit = document.createElement("button");
          edit.type = "button";
          edit.className = "event-edit";
          edit.textContent = eventEditor && eventEditor.blockId === block.id ? "Close" : "Edit";
          edit.setAttribute("aria-label", manual ? "Edit event title and boundaries" : "Edit this suggested event");
          edit.addEventListener("click", () => {
            if (eventEditor && eventEditor.blockId === block.id) eventEditor = null;
            else {
              eventEditor = {
                blockId: block.id,
                manual: manual,
                editId: manual ? block.id : "",
                revision: manual ? Number(block.revision || 0) : 0,
                title: manual ? (block.title || "") : "",
                start: block.start_chunk_id || "",
                end: block.end_chunk_id || "",
                error: "",
                saving: false,
              };
            }
            renderList();
          });
          tools.appendChild(edit);
          box.appendChild(tools);
          if (eventEditor && eventEditor.blockId === block.id) {
            const editor = eventEditor;
            const form = document.createElement("div");
            form.className = "event-form";
            const titleLabel = document.createElement("label");
            titleLabel.textContent = "Title";
            const titleInput = document.createElement("input");
            titleInput.type = "text";
            titleInput.maxLength = 80;
            titleInput.value = editor.title;
            titleInput.setAttribute("aria-label", "Event title");
            titleInput.addEventListener("input", () => { editor.title = titleInput.value; });
            titleLabel.appendChild(titleInput);
            const options = (payload.chunks || []).filter((chunk) => chunk.started_local || chunk.started);
            const boundary = (label, key) => {
              const field = document.createElement("label");
              field.textContent = label;
              const select = document.createElement("select");
              select.setAttribute("aria-label", label);
              for (const chunk of options) {
                const choice = document.createElement("option");
                choice.value = chunk.id;
                choice.textContent = chunk.started_local || chunk.started || "Recording";
                if (chunk.id === editor[key]) choice.selected = true;
                select.appendChild(choice);
              }
              select.addEventListener("change", () => { editor[key] = select.value; });
              field.appendChild(select);
              return field;
            };
            const save = document.createElement("button");
            save.type = "button";
            save.textContent = editor.saving ? "Saving" : "Save";
            save.disabled = !!editor.saving;
            save.addEventListener("click", async () => {
              if (editor.saving) return;
              editor.saving = true;
              editor.error = "";
              renderList();
              const body = {
                title: editor.title,
                start_chunk_id: editor.start,
                end_chunk_id: editor.end,
                expected_revision: editor.revision,
              };
              if (editor.manual && editor.editId) body.id = editor.editId;
              let response;
              try {
                response = await fetch("/v1/event-edits", {
                  method: "POST",
                  headers: { ...authHeaders(), "Content-Type": "application/json" },
                  body: JSON.stringify(body),
                });
              } catch (error) {
                response = null;
              }
              if (!response) {
                editor.saving = false;
                editor.error = "Couldn't save this event.";
                renderList();
                return;
              }
              if (response.status === 409) {
                editor.saving = false;
                editor.error = "This event changed. Refresh before saving again.";
                renderList();
                return;
              }
              if (!response.ok) {
                let message = "Couldn't save this event.";
                try {
                  const data = await response.json();
                  if (data && typeof data.error === "string") message = data.error;
                } catch (error) {
                  message = "Couldn't save this event.";
                }
                editor.saving = false;
                editor.error = message;
                renderList();
                return;
              }
              eventEditor = null;
              await loadDay();
            });
            form.appendChild(titleLabel);
            form.appendChild(boundary("Starts with", "start"));
            form.appendChild(boundary("Ends with", "end"));
            form.appendChild(save);
            if (editor.error) {
              const problem = document.createElement("div");
              problem.className = "meta";
              problem.textContent = editor.error;
              form.appendChild(problem);
            }
            box.appendChild(form);
          }
          if (open) {
            for (const chunk of members) appendChunk(chunk, box);
          }
          list.appendChild(box);
          shown += 1;
          continue;
        }
        for (const chunk of members) appendChunk(chunk, list);
        continue;
      }
      const chunk = byId.get(block.chunk_id);
      if (chunk && visible.has(chunk.id)) appendChunk(chunk, list);
    }
    if (!shown) {
      const empty = document.createElement("p");
      empty.className = "meta";
      empty.textContent = "No recordings match this day or search.";
      list.appendChild(empty);
    }
  }
  function selectedItem() {
    if (selectedKind === "event") return (payload.events || []).find((item) => item.id === selectedId) || null;
    return (payload.chunks || []).find((item) => item.id === selectedId) || null;
  }
  function attachAudio(item) {
    const isEvent = selectedKind === "event";
    const canOriginal = !!(item && item.audio_playable);
    const canEnhanced = !!(isEvent && item && item.enhanced_playable);
    if (isEvent && playbackKind === "enhanced" && !canEnhanced) playbackKind = "original";
    playbackMode.hidden = !isEvent;
    playOriginal.setAttribute("aria-pressed", String(!isEvent || playbackKind !== "enhanced"));
    playEnhanced.setAttribute("aria-pressed", String(isEvent && playbackKind === "enhanced"));
    playEnhanced.disabled = !canEnhanced;
    keepButton.hidden = isEvent || !canOriginal;
    keepButton.disabled = !!(item && item.audio_pinned);
    keepButton.textContent = item && item.audio_pinned ? "Kept" : "Keep audio";
    const label = item ? (item.started_local || (isEvent ? "Speech event" : "Recording")) : "No recording selected";
    nowPlaying.textContent = item ? (label + (canOriginal ? "" : " · audio unavailable")) : "No recording selected";
    keepButton.onclick = (!isEvent && canOriginal) ? async () => {
      const keptId = item.id;
      const response = await fetch("/v1/chunks/" + keptId + "/keep", {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: "{}",
      });
      if (!response.ok) return;
      const current = (payload.chunks || []).find((row) => row.id === keptId);
      if (current) current.audio_pinned = true;
      if (selectedKind === "chunk" && selectedId === keptId) {
        keepButton.textContent = "Kept";
        keepButton.disabled = true;
      }
    } : null;
    const audioPath = !item || !canOriginal ? null : (
      isEvent
        ? "/v1/events/" + item.id + "/audio?kind=" + (playbackKind === "enhanced" && canEnhanced ? "enhanced" : "original")
        : "/v1/audio/" + item.id
    );
    const nextAudioId = audioPath ? ((isEvent ? "event:" : "chunk:") + item.id + ":" + (isEvent ? playbackKind : "original")) : null;
    if (nextAudioId === loadedAudioId) return;
    activeGroupKey = null;
    clipStartTime = null;
    clipStopTime = null;
    loadedAudioId = nextAudioId;
    audioGeneration += 1;
    if (audioController) audioController.abort();
    audioController = null;
    if (audioUrl) { URL.revokeObjectURL(audioUrl); audioUrl = null; }
    player.removeAttribute("src");
    if (!audioPath) return;
    const requestedId = nextAudioId;
    audioController = new AbortController();
    fetch(audioPath, { headers: authHeaders(), signal: audioController.signal }).then((r) => r.ok ? r.blob() : Promise.reject()).then((blob) => {
      if (loadedAudioId !== requestedId) return;
      audioUrl = URL.createObjectURL(blob);
      player.src = audioUrl;
    }).catch((error) => {
      if (error && error.name === "AbortError") return;
      if (loadedAudioId === requestedId) {
        loadedAudioId = null;
        nowPlaying.textContent = "Audio could not be loaded · Refresh to retry";
      }
    });
  }
  function currentPersonId(group) {
    const named = (group.turns || []).find((turn) => turn.person_id);
    return named ? named.person_id : "";
  }
  function fullyNamed(group, personId) {
    const turns = group.turns || [];
    return !!personId && turns.length > 0 && turns.every((turn) => turn.person_id === personId);
  }
  function closePopover(restoreFocus) {
    const key = openIdentityKey;
    openIdentityKey = null;
    identityError = "";
    identityDraft = null;
    identityMode = "list";
    identitySample = false;
    createdPersonId = null;
    createdPersonKey = null;
    createdPersonName = null;
    identityNewName = "";
    render();
    if (restoreFocus && key) {
      const pill = document.querySelector('[data-identity-key="' + key + '"]');
      if (pill) pill.focus();
    }
  }
  function applyIdentity(group, personId, useSample) {
    const turn = group.turns[0];
    if (!turn) return Promise.reject(new Error("turn"));
    if (fullyNamed(group, personId) && !useSample) {
      closePopover(true);
      return Promise.resolve();
    }
    if (identitySaving) return Promise.resolve();
    identitySaving = true;
    identityError = "";
    return fetch("/v1/turns/" + turn.id + "/label", {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ person_id: personId, use_sample: useSample === true })
    }).then((response) => {
      if (!response.ok) throw new Error("label");
      const person = (payload.people || []).find((item) => item.id === personId);
      for (const member of group.turns || []) {
        member.person_id = personId;
        member.name = person ? person.name : member.name;
        member.suggested_name = "";
        member.suggested_person_id = "";
      }
      createdPersonId = null;
      identityDraft = null;
      identityMode = "list";
      identitySample = false;
      identityError = "";
      openIdentityKey = null;
      render();
      return loadDay();
    }).catch((error) => {
      identityError = "Could not save that identity.";
      throw error;
    }).finally(() => {
      identitySaving = false;
    });
  }
  function identityPopover(group) {
    const box = document.createElement("div");
    box.className = "popover";
    box.setAttribute("data-identity-editor", "true");
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-label", "Identify speaker");
    box.addEventListener("click", (event) => event.stopPropagation());
    const note = document.createElement("p");
    note.className = "meta";
    note.setAttribute("aria-live", "polite");
    if (identityError && openIdentityKey === group.key) note.textContent = identityError;
    else note.textContent = "Tap a name to assign this stretch.";
    const cleanSeconds = (group.turns || []).reduce((sum, turn) => sum + (Number(turn.clean_seconds) || 0), 0);
    const sampleReady = cleanSeconds >= 5;
    if (!sampleReady) identitySample = false;
    const sampleNote = document.createElement("p");
    sampleNote.className = "meta";
    sampleNote.textContent = sampleReady
      ? (identitySample
        ? "Tagging this stretch also saves a voice sample."
        : "Leave this unchecked to name the stretch without saving a voice sample.")
      : "This piece is under 5 seconds of one clean voice, so it can be named but not saved as a voice sample.";
    const sampleLabel = document.createElement("label");
    sampleLabel.textContent = "Save a voice sample";
    const sampleBox = document.createElement("input");
    sampleBox.type = "checkbox";
    sampleBox.checked = sampleReady && identitySample;
    sampleBox.disabled = !sampleReady;
    sampleBox.setAttribute("aria-label", "Save a voice sample");
    sampleBox.addEventListener("change", () => {
      identitySample = sampleReady && sampleBox.checked;
      sampleNote.textContent = identitySample
        ? "Tagging this stretch also saves a voice sample."
        : "Leave this unchecked to name the stretch without saving a voice sample.";
    });
    sampleLabel.prepend(sampleBox);
    const footer = document.createElement("div");
    footer.className = "popover-footer";
    function currentId() { return currentPersonId(group); }
    if (identityMode === "new") {
      const name = document.createElement("input");
      name.type = "text";
      name.placeholder = "Name";
      name.value = identityNewName;
      name.setAttribute("aria-label", "New person name");
      name.addEventListener("input", () => { identityNewName = name.value; });
      const back = document.createElement("button");
      back.type = "button";
      back.textContent = "Back";
      back.addEventListener("click", () => { identityMode = "list"; identityNewName = ""; render(); });
      const create = document.createElement("button");
      create.type = "button";
      create.textContent = "Create & assign";
      create.addEventListener("click", async () => {
        const value = (name.value || "").trim();
        if (!value) { note.textContent = "Enter a name first."; return; }
        note.textContent = "Saving…";
        try {
          create.disabled = true;
          let personId = createdPersonKey === group.key && createdPersonName === value ? createdPersonId : null;
          if (!personId) {
            const created = await fetch("/v1/people", { method: "POST", headers: { ...authHeaders(), "Content-Type": "application/json" }, body: JSON.stringify({ name: value }) });
            if (!created.ok) throw new Error("create");
            const person = await created.json();
            personId = person.id;
            createdPersonId = personId;
            createdPersonKey = group.key;
            createdPersonName = value;
            payload.people = (payload.people || []).concat([person]);
          }
          await applyIdentity(group, personId, identitySample);
        } catch (error) {
          note.textContent = "Could not save that identity.";
          create.disabled = false;
        }
      });
      footer.appendChild(back);
      footer.appendChild(create);
      box.appendChild(name);
      if (sampleReady) box.appendChild(sampleLabel);
      box.appendChild(sampleNote);
      box.appendChild(footer);
      box.appendChild(note);
      setTimeout(() => name.focus(), 0);
      return box;
    }
    if (sampleReady) box.appendChild(sampleLabel);
    box.appendChild(sampleNote);
    for (const person of (payload.people || [])) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "person-option";
      row.setAttribute("aria-pressed", String((identityDraft || currentId()) === person.id));
      row.textContent = person.name;
      row.addEventListener("click", (event) => {
        event.stopPropagation();
        if (identitySaving) return;
        note.textContent = "Saving…";
        for (const button of box.querySelectorAll("button")) button.disabled = true;
        applyIdentity(group, person.id, identitySample).catch(() => {
          note.textContent = "Could not save that identity.";
          for (const button of box.querySelectorAll("button")) button.disabled = false;
        });
      });
      box.appendChild(row);
    }
    const create = document.createElement("button");
    create.type = "button";
    create.className = "person-option";
    create.textContent = "New person";
    create.addEventListener("click", () => { identityMode = "new"; identityDraft = null; identityNewName = ""; render(); });
    box.appendChild(create);
    box.appendChild(note);
    return box;
  }
  function renderDetail() {
    pane.replaceChildren();
    if (selectedKind === "event") {
      const item = selectedItem();
      attachAudio(item);
      if (isMobile()) {
        const back = document.createElement("button");
        back.type = "button";
        back.id = "back-recordings";
        back.textContent = "Back to recordings";
        back.addEventListener("click", () => {
          mobileDetailOpen = false;
          render();
        });
        pane.appendChild(back);
      }
      if (!item) {
        const empty = document.createElement("p");
        empty.textContent = "Select a recording.";
        pane.appendChild(empty);
        return;
      }
      const title = document.createElement("h2");
      title.textContent = item.started_local || "Speech event";
      const meta = document.createElement("p");
      meta.className = "meta";
      meta.textContent = Number(item.playable_duration || item.duration || 0).toFixed(1) + " seconds playable";
      pane.appendChild(title);
      pane.appendChild(meta);
      const explanation = document.createElement("p");
      explanation.textContent = "This is a derived speech event. Use the player below to hear it, or choose Original/Enhanced.";
      pane.appendChild(explanation);
      const source = document.createElement("p");
      source.className = "meta";
      source.textContent = (item.chunks || []).length + " source recording" + ((item.chunks || []).length === 1 ? "" : "s") + " · Full transcripts and speaker assignment are available from the source recordings.";
      pane.appendChild(source);
      return;
    }
    const chunk = selectedItem();
    attachAudio(chunk);
    if (isMobile()) {
      const back = document.createElement("button");
      back.type = "button";
      back.id = "back-recordings";
      back.textContent = "Back to recordings";
      back.addEventListener("click", () => {
        mobileDetailOpen = false;
        render();
        const restore = () => {
          const row = selectedId ? document.getElementById("recording-" + selectedId) : null;
          window.scrollTo(0, listScroll);
          if (list) list.scrollTop = listScroll;
          if (row) row.focus();
        };
        requestAnimationFrame(restore);
      });
      pane.appendChild(back);
    }
    if (!chunk) {
      const empty = document.createElement("p");
      empty.textContent = "Select a recording.";
      pane.appendChild(empty);
      return;
    }
    const title = document.createElement("h2");
    title.textContent = chunk.started_local || "Recording";
    const meta = document.createElement("p");
    meta.className = "meta";
    meta.textContent = Number(chunk.duration || 0).toFixed(1) + " seconds";
    const modes = document.createElement("div");
    modes.className = "toolbar";
    const transcriptBtn = document.createElement("button");
    transcriptBtn.type = "button";
    transcriptBtn.textContent = "Transcript";
    transcriptBtn.setAttribute("aria-pressed", String(detailMode === "transcript"));
    const turnsBtn = document.createElement("button");
    turnsBtn.type = "button";
    turnsBtn.textContent = "Speaker turns";
    turnsBtn.setAttribute("aria-pressed", String(detailMode === "turns"));
    transcriptBtn.addEventListener("click", () => { detailMode = "transcript"; render(); });
    turnsBtn.addEventListener("click", () => { detailMode = "turns"; render(); });
    const fullRecordingBtn = document.createElement("button");
    fullRecordingBtn.type = "button";
    fullRecordingBtn.textContent = "Play full recording";
    fullRecordingBtn.disabled = !chunk.audio_playable;
    fullRecordingBtn.addEventListener("click", () => {
      activeGroupKey = null;
      clipStartTime = null;
      clipStopTime = null;
      player.currentTime = 0;
      player.play().catch(() => {});
      updateCardPlayback();
    });
    modes.appendChild(transcriptBtn);
    modes.appendChild(turnsBtn);
    modes.appendChild(fullRecordingBtn);
    pane.appendChild(title);
    pane.appendChild(meta);
    if (chunk.status === "needs_attention") {
      const notice = document.createElement("p");
      notice.className = "meta";
      notice.setAttribute("role", "status");
      notice.textContent = "Mac processing stopped after repeated failures. The original recording is kept if available.";
      const retry = document.createElement("button");
      retry.type = "button";
      retry.textContent = "Retry processing";
      retry.addEventListener("click", async () => {
        retry.disabled = true;
        let response;
        try {
          response = await fetch("/v1/chunks/" + chunk.id + "/retry", {
            method: "POST", headers: { ...authHeaders(), "Content-Type": "application/json" }, body: "{}"
          });
        } catch (error) { response = null; }
        if (response && response.ok) await loadDay();
        else {
          notice.textContent = response && response.status === 409
            ? "Original audio is unavailable; this recording cannot be retried."
            : "Retry did not start. Please try again.";
          retry.disabled = false;
        }
      });
      pane.appendChild(notice);
      pane.appendChild(retry);
    }
    pane.appendChild(modes);
    if (!Array.isArray(chunk.speakers)) {
      if (chunk.status === "complete") ensureReview(chunk);
      const waiting = document.createElement("p");
      waiting.className = "meta";
      waiting.textContent = chunk.status === "complete" ? "Loading speakers…" : "Speaker analysis has not finished.";
      pane.appendChild(waiting);
      const body = document.createElement("p");
      body.textContent = chunk.transcript || "No transcript yet.";
      pane.appendChild(body);
      return;
    }
    const groups = groupTurns(chunk);
    function playSpeakerPiece(group, bounds, fromStart) {
      if (!chunk.audio_playable) return;
      const expectedId = chunk.id;
      const expectedGeneration = audioGeneration;
      const startAt = Number(bounds.start) || 0;
      const stopAt = Number(bounds.end) || 0;
      const jump = () => {
        if (selectedKind !== "chunk" || selectedId !== expectedId || loadedAudioId !== ("chunk:" + expectedId + ":original") || audioGeneration !== expectedGeneration) return;
        activeGroupKey = group.key;
        clipStartTime = startAt;
        clipStopTime = stopAt;
        if (fromStart || player.currentTime < startAt || player.currentTime >= stopAt) player.currentTime = startAt;
        player.play().catch(() => {});
        updateCardPlayback();
      };
      if (player.readyState) jump(); else player.addEventListener("loadedmetadata", jump, { once: true });
    }
    const speakers = document.createElement("div");
    speakers.className = "speakers";
    speakers.setAttribute("aria-label", "Speakers");
    if (!groups.length) {
      const empty = document.createElement("span");
      empty.className = "badge unknown pill";
      empty.textContent = "Unknown · No speaker turns available";
      empty.setAttribute("aria-disabled", "true");
      speakers.appendChild(empty);
    }
    for (const group of groups) {
      const info = groupLabel(group);
      const anchor = document.createElement("div");
      anchor.className = "identity-anchor";
      const pill = document.createElement("button");
      pill.type = "button";
      pill.className = "badge pill " + info.cls;
      const bounds = groupBounds(group);
      const clipStart = Number.isFinite(bounds.start) ? bounds.start.toFixed(1) : "?";
      const clipEnd = Number.isFinite(bounds.end) ? bounds.end.toFixed(1) : "?";
      pill.textContent = info.label + " · " + clipStart + "–" + clipEnd + "s";
      pill.setAttribute("data-identity-key", group.key);
      pill.setAttribute("aria-expanded", String(openIdentityKey === group.key));
      pill.setAttribute("aria-haspopup", "dialog");
      pill.setAttribute("aria-label", "Play " + clipStart + " to " + clipEnd + " seconds");
      pill.addEventListener("click", (event) => {
        event.stopPropagation();
        playSpeakerPiece(group, bounds, true);
        if (openIdentityKey === group.key) return;
        openIdentityKey = group.key;
        identityError = "";
        identityDraft = currentPersonId(group) || null;
        identityMode = "list";
        identitySample = false;
        createdPersonId = null;
        createdPersonKey = null;
        createdPersonName = null;
        identityNewName = "";
        render();
      });
      anchor.appendChild(pill);
      if (openIdentityKey === group.key) {
        const editor = identityPopover(group);
        if (isMobile()) {
          const close = document.createElement("button");
          close.type = "button";
          close.textContent = "Close";
          close.addEventListener("click", () => closePopover(true));
          const footer = editor.querySelector(".popover-footer") || editor.appendChild(document.createElement("div"));
          footer.className = "popover-footer";
          if (![...footer.querySelectorAll("button")].some((button) => button.textContent === "Close" || button.textContent === "Cancel")) {
            footer.insertBefore(close, footer.firstChild);
          }
        }
        anchor.appendChild(editor);
      }
      group.identityAnchor = anchor;
    }
    pane.appendChild(modes);
    if (detailMode === "transcript") {
      placeTranscriptSpeakers(speakers, groups);
      pane.appendChild(speakers);
      const body = document.createElement("p");
      body.textContent = chunk.transcript || "No transcript yet.";
      pane.appendChild(body);
    } else {
      if (!groups.length) {
        const none = document.createElement("p");
        none.textContent = "No speaker turns for this recording.";
        pane.appendChild(none);
      }
      for (const group of groups) {
        const card = document.createElement("div");
        card.className = "group";
        card.setAttribute("data-group-key", group.key);
        const bounds = groupBounds(group);
        card.setAttribute("data-start", String(bounds.start));
        card.setAttribute("data-end", String(bounds.end));
        const head = document.createElement("div");
        head.className = "group-head";
        head.appendChild(group.identityAnchor);
        const controls = document.createElement("div");
        controls.className = "group-controls";
        const play = document.createElement("button");
        play.type = "button";
        play.className = "play-toggle";
        play.textContent = "Play";
        play.setAttribute("aria-pressed", "false");
        play.setAttribute("aria-label", "Play this speaker clip");
        play.disabled = !chunk.audio_playable;
        const replay = document.createElement("button");
        replay.type = "button";
        replay.className = "replay";
        replay.textContent = "Replay";
        replay.setAttribute("aria-label", "Replay this speaker clip");
        replay.disabled = !chunk.audio_playable;
        const playGroup = (fromStart) => playSpeakerPiece(group, bounds, fromStart);
        play.addEventListener("click", () => {
          if (activeGroupKey === group.key && !player.paused) {
            player.pause();
            updateCardPlayback();
            return;
          }
          playGroup(false);
        });
        replay.addEventListener("click", () => playGroup(true));
        controls.appendChild(play);
        controls.appendChild(replay);
        head.appendChild(controls);
        card.appendChild(head);
        const progress = document.createElement("div");
        progress.className = "group-progress";
        const meter = document.createElement("progress");
        meter.max = Math.max(0.001, bounds.end - bounds.start);
        meter.value = 0;
        const clock = document.createElement("span");
        clock.className = "group-time";
        clock.textContent = formatTime(0) + " / " + formatTime(bounds.end - bounds.start);
        progress.appendChild(meter);
        progress.appendChild(clock);
        card.appendChild(progress);
        const body = document.createElement("p");
        body.textContent = groupTranscript(group) || "No transcript for this clip.";
        card.appendChild(body);
        pane.appendChild(card);
      }
      updateCardPlayback();
    }
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "Processing details";
    const diar = chunk.diarization || {};
    const info = document.createElement("p");
    info.className = "meta";
    const bits = [];
    if (diar.outcome || chunk.diarization_status) bits.push(reasonText(diar.outcome || chunk.diarization_status) === "Needs a closer look" ? (diar.outcome || chunk.diarization_status) : (diar.outcome || chunk.diarization_status));
    if (Number.isFinite(Number(diar.speech_seconds))) bits.push(Number(diar.speech_seconds).toFixed(1) + "s speech");
    if (Number.isFinite(Number(diar.coverage))) bits.push(Math.round(Number(diar.coverage) * 100) + "% coverage");
    if (diar.turn_count != null) bits.push(diar.turn_count + " turns");
    if (diar.embedding_count != null) bits.push(diar.embedding_count + " embeddings");
    if (diar.cluster_count != null) bits.push(diar.cluster_count + " clusters");
    info.textContent = bits.join(" · ") || "No processing details yet.";
    details.appendChild(summary);
    details.appendChild(info);
    pane.appendChild(details);
  }
  function renderPeople() {
    peoplePane.replaceChildren();
    const people = payload.people || [];
    if (!people.length) {
      const empty = document.createElement("p");
      empty.textContent = "No people yet. Identify a speaker from a recording to add someone.";
      peoplePane.appendChild(empty);
      return;
    }
    for (const person of people) {
      const row = document.createElement("div");
      row.className = "person";
      const label = document.createElement("div");
      label.textContent = enrollmentCopy(person);
      const rename = document.createElement("input");
      rename.type = "text";
      rename.value = person.name;
      rename.setAttribute("aria-label", "Rename " + person.name);
      const save = document.createElement("button");
      save.type = "button";
      save.textContent = "Rename";
      save.addEventListener("click", async () => {
        const name = (rename.value || "").trim();
        if (!name || name === person.name) return;
        const response = await fetch("/v1/people/" + person.id, { method: "POST", headers: { ...authHeaders(), "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
        if (response.ok) loadDay();
      });
      row.appendChild(label);
      row.appendChild(rename);
      row.appendChild(save);
      peoplePane.appendChild(row);
    }
  }
  function easternDay(iso) {
    try {
      return new Intl.DateTimeFormat("en-CA", {
        timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit"
      }).format(new Date(iso));
    } catch (error) {
      return String(iso || "").slice(0, 10);
    }
  }
  function playReviewSpan(item) {
    if (!item || !item.audio_usable) return;
    const audioPath = "/v1/audio/" + item.chunk_id;
    const nextAudioId = "review:" + item.turn_id;
    const startAt = Number(item.started) || 0;
    const stopAt = Number(item.ended) || 0;
    activeGroupKey = item.turn_id;
    clipStartTime = startAt;
    clipStopTime = stopAt;
    nowPlaying.textContent = "Speaker span · not confirmed";
    const jump = () => {
      if (loadedAudioId !== nextAudioId) return;
      activeGroupKey = item.turn_id;
      clipStartTime = startAt;
      clipStopTime = stopAt;
      player.currentTime = startAt;
      player.play().catch(() => {});
      updateCardPlayback();
    };
    if (nextAudioId === loadedAudioId && player.readyState) {
      jump();
      return;
    }
    loadedAudioId = nextAudioId;
    audioGeneration += 1;
    if (audioController) audioController.abort();
    audioController = new AbortController();
    if (audioUrl) { URL.revokeObjectURL(audioUrl); audioUrl = null; }
    player.removeAttribute("src");
    const requestedId = nextAudioId;
    fetch(audioPath, { headers: authHeaders(), signal: audioController.signal }).then((response) => response.ok ? response.blob() : Promise.reject()).then((blob) => {
      if (loadedAudioId !== requestedId) return;
      audioUrl = URL.createObjectURL(blob);
      player.addEventListener("loadedmetadata", jump, { once: true });
      player.src = audioUrl;
    }).catch((error) => {
      if (error && error.name === "AbortError") return;
      if (loadedAudioId === requestedId) {
        loadedAudioId = null;
        nowPlaying.textContent = "Audio could not be loaded · Refresh to retry";
      }
    });
  }
  async function openReviewClip(item) {
    const localDay = easternDay(item.clip_started);
    selectedKind = "chunk";
    selectedId = item.chunk_id;
    mobileDetailOpen = true;
    view = "recordings";
    if (day.value !== localDay) {
      day.value = localDay;
      await loadDay();
    }
    setView("recordings");
    const row = document.getElementById("recording-" + item.chunk_id);
    if (row) row.focus();
  }
  async function confirmReview(item) {
    const personId = reviewChoices[item.turn_id] || "";
    if (!personId) return;
    const useSample = reviewSamples[item.turn_id] === true;
    reviewError = "";
    const response = await fetch("/v1/turns/" + item.turn_id + "/label", {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ person_id: personId, use_sample: useSample })
    });
    if (!response.ok) {
      reviewError = "Could not confirm that name.";
      renderReview();
      return;
    }
    reviewItems = reviewItems.filter((row) => row.group_id !== item.group_id);
    delete reviewChoices[item.turn_id];
    delete reviewSamples[item.turn_id];
    reviewStatus.textContent = "Confirmed. That name is a human label.";
    renderReview();
  }
  async function rejectReview(item, personId) {
    reviewError = "";
    const response = await fetch("/v1/turns/" + item.turn_id + "/reject", {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ person_id: personId })
    });
    if (!response.ok) {
      reviewError = "Could not reject that suggestion.";
      renderReview();
      return;
    }
    item.suggestions = (item.suggestions || []).filter((suggestion) => suggestion.person_id !== personId);
    if (item.stored_person_id === personId) {
      item.stored_person_id = null;
      item.stored_name = null;
    }
    if (reviewChoices[item.turn_id] === personId) reviewChoices[item.turn_id] = "";
    reviewStatus.textContent = "Suggestion rejected. The turn was not relabeled.";
    renderReview();
  }
  async function loadReviewDiagnostics() {
    const response = await fetch("/v1/speaker-review/diagnostics", { headers: authHeaders(), cache: "no-store" });
    if (!response.ok) {
      reviewError = "Couldn't load voice-check counts.";
      renderReview();
      return;
    }
    reviewDiagnostics = await response.json();
    renderReview();
  }
  async function loadSpeakerReview(reset) {
    reviewLoading = true;
    reviewError = "";
    if (reset) {
      reviewItems = [];
      reviewCursor = null;
      reviewLoaded = false;
    }
    if (view === "review") renderReview();
    const params = new URLSearchParams();
    params.set("limit", "20");
    if (!reset && reviewCursor) params.set("cursor", reviewCursor);
    let response;
    try {
      response = await fetch("/v1/speaker-review?" + params.toString(), { headers: authHeaders(), cache: "no-store" });
    } catch (error) {
      response = null;
    }
    reviewLoading = false;
    if (!response || !response.ok) {
      reviewError = "Couldn't load speaker review.";
      if (view === "review") renderReview();
      return;
    }
    const page = await response.json();
    const incoming = Array.isArray(page.items) ? page.items : [];
    reviewItems = reset ? incoming : reviewItems.concat(incoming);
    reviewCursor = page.next_cursor || null;
    reviewLoaded = true;
    if (view === "review") renderReview();
  }
  function renderReview() {
    reviewView.hidden = false;
    reviewList.replaceChildren();
    reviewStatus.textContent = reviewError || (reviewLoading ? "Loading unconfirmed speaker turns." : (reviewItems.length ? "These names are suggestions until you confirm one." : "No uncertain speaker turns."));
    const counts = document.createElement("button");
    counts.type = "button";
    counts.textContent = "Voice-check counts";
    counts.addEventListener("click", () => loadReviewDiagnostics());
    reviewList.appendChild(counts);
    if (reviewDiagnostics) {
      const summary = document.createElement("p");
      summary.className = "meta";
      const reasons = reviewDiagnostics.rejection_reasons || {};
      summary.textContent = "Held-out groups " + Number(reviewDiagnostics.groups || 0)
        + " · rank 1 " + Number(reviewDiagnostics.rank1_correct_groups || 0)
        + " · auto acceptances " + Number(reasons.accepted || 0);
      reviewList.appendChild(summary);
    }
    if (!reviewItems.length && !reviewLoading) {
      const empty = document.createElement("p");
      empty.textContent = "No uncertain speaker turns.";
      reviewList.appendChild(empty);
    }
    for (const item of reviewItems) {
      const card = document.createElement("article");
      card.className = "group review-card";
      card.setAttribute("data-group-key", item.turn_id);
      card.setAttribute("data-start", String(item.started));
      card.setAttribute("data-end", String(item.ended));
      const title = document.createElement("h3");
      title.textContent = "Unconfirmed speaker";
      const badge = document.createElement("span");
      badge.className = "badge suggested";
      badge.textContent = item.label_source === "automatic" && item.stored_name
        ? "Unconfirmed automatic guess"
        : "Unconfirmed suggestion";
      const when = document.createElement("p");
      when.className = "meta";
      const start = Number(item.started);
      const end = Number(item.ended);
      when.textContent = (Number.isFinite(start) ? start.toFixed(1) : "?") + "–" + (Number.isFinite(end) ? end.toFixed(1) : "?") + "s"
        + (item.audio_usable ? "" : " · audio unavailable");
      card.appendChild(title);
      card.appendChild(badge);
      card.appendChild(when);
      if (item.label_source === "automatic" && item.stored_name) {
        const guess = document.createElement("p");
        guess.textContent = "Automatic guess: " + item.stored_name + " · not confirmed";
        card.appendChild(guess);
      }
      const suggestions = item.suggestions || [];
      for (const suggestion of suggestions) {
        const row = document.createElement("div");
        row.className = "suggestion-row";
        const name = document.createElement("span");
        name.textContent = (suggestion.name || "Unnamed") + " · unconfirmed suggestion";
        const reject = document.createElement("button");
        reject.type = "button";
        reject.textContent = "Reject";
        reject.setAttribute("aria-label", "Reject suggestion " + (suggestion.name || "person"));
        reject.addEventListener("click", () => rejectReview(item, suggestion.person_id));
        row.appendChild(name);
        row.appendChild(reject);
        card.appendChild(row);
      }
      const choiceLabel = document.createElement("label");
      choiceLabel.className = "review-choice-label";
      choiceLabel.textContent = "Identify as";
      const choice = document.createElement("select");
      choice.setAttribute("aria-label", "Person for this speaker turn");
      const blank = document.createElement("option");
      blank.value = "";
      blank.textContent = "Choose a name";
      choice.appendChild(blank);
      for (const suggestion of suggestions) {
        const option = document.createElement("option");
        option.value = suggestion.person_id;
        option.textContent = (suggestion.name || "Unnamed") + " · unconfirmed";
        choice.appendChild(option);
      }
      const suggestedIds = new Set(suggestions.map((suggestion) => suggestion.person_id));
      for (const person of ((payload && payload.people) || [])) {
        if (!person || !person.id || suggestedIds.has(person.id)) continue;
        const option = document.createElement("option");
        option.value = person.id;
        option.textContent = person.name || "Unnamed person";
        choice.appendChild(option);
      }
      choice.value = reviewChoices[item.turn_id] || "";
      if (choice.value && ![...choice.options].some((option) => option.value === choice.value)) choice.value = "";
      choice.addEventListener("change", () => {
        reviewChoices[item.turn_id] = choice.value;
        const confirm = card.querySelector(".review-confirm");
        if (confirm) confirm.disabled = !choice.value;
      });
      choiceLabel.appendChild(choice);
      card.appendChild(choiceLabel);
      const sampleLabel = document.createElement("label");
      const sampleBox = document.createElement("input");
      sampleBox.type = "checkbox";
      const canSample = Number(item.clean_seconds) >= 5 && !!item.audio_usable;
      sampleBox.checked = canSample && reviewSamples[item.turn_id] === true;
      sampleBox.disabled = !canSample;
      sampleBox.setAttribute("aria-label", "Save a voice sample for this confirmation");
      sampleBox.addEventListener("change", () => { reviewSamples[item.turn_id] = sampleBox.checked === true; });
      sampleLabel.appendChild(sampleBox);
      sampleLabel.appendChild(document.createTextNode(canSample ? "Save a voice sample" : "Not enough clean speech to save a voice sample"));
      card.appendChild(sampleLabel);
      const actions = document.createElement("div");
      actions.className = "review-actions";
      const confirm = document.createElement("button");
      confirm.type = "button";
      confirm.className = "review-confirm";
      confirm.textContent = "Confirm";
      confirm.disabled = !choice.value;
      confirm.setAttribute("aria-label", "Confirm selected name");
      confirm.addEventListener("click", () => confirmReview(item));
      const play = document.createElement("button");
      play.type = "button";
      play.className = "play-toggle";
      play.textContent = "Play";
      play.disabled = !item.audio_usable;
      play.setAttribute("aria-label", "Play this speaker span");
      play.addEventListener("click", () => playReviewSpan(item));
      const open = document.createElement("button");
      open.type = "button";
      open.textContent = "Open recording";
      open.setAttribute("aria-label", "Open the source recording");
      open.addEventListener("click", () => openReviewClip(item));
      actions.appendChild(confirm);
      actions.appendChild(play);
      actions.appendChild(open);
      const meter = document.createElement("div");
      meter.className = "group-progress";
      const progress = document.createElement("progress");
      progress.max = Math.max(0, end - start) || 1;
      progress.value = 0;
      const clock = document.createElement("span");
      clock.className = "group-time";
      clock.textContent = "0:00 / " + formatTime(Math.max(0, end - start));
      meter.appendChild(progress);
      meter.appendChild(clock);
      card.appendChild(actions);
      card.appendChild(meter);
      reviewList.appendChild(card);
    }
    if (reviewCursor) {
      const more = document.createElement("button");
      more.type = "button";
      more.textContent = "More";
      more.addEventListener("click", () => loadSpeakerReview(false));
      reviewList.appendChild(more);
    }
  }
  function render() {
    const mobile = isMobile();
    document.body.classList.toggle("review-mode", view === "review");
    document.body.classList.toggle("mobile-review", mobile && view === "review");
    document.body.classList.toggle("mobile-list", mobile && view === "recordings" && !mobileDetailOpen);
    document.body.classList.toggle("mobile-detail", mobile && view === "recordings" && mobileDetailOpen);
    document.body.classList.toggle("mobile-people", mobile && view === "people");
    if (!mobile) mobileDetailOpen = false;
    if (view === "review") {
      document.body.classList.remove("mobile-list", "mobile-detail", "mobile-people");
      library.hidden = true;
      peopleView.hidden = true;
      reviewView.hidden = false;
      renderReview();
      return;
    }
    reviewView.hidden = true;
    if (!payload) return;
    if (view === "people") {
      document.body.classList.remove("mobile-list", "mobile-detail");
      renderPeople();
      return;
    }
    renderList();
    renderDetail();
  }
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && openIdentityKey) {
      event.preventDefault();
      closePopover(true);
    }
  });
  document.addEventListener("pointerdown", (event) => {
    const pop = document.querySelector(".popover");
    const pill = openIdentityKey ? document.querySelector('[data-identity-key="' + openIdentityKey + '"]') : null;
    identityPointerInside = !!((pop && pop.contains(event.target)) || (pill && pill.contains(event.target)));
  }, true);
  document.addEventListener("click", (event) => {
    if (!openIdentityKey || identityPointerInside) return;
    const pop = document.querySelector(".popover");
    const pill = document.querySelector('[data-identity-key="' + openIdentityKey + '"]');
    if (pop && pop.contains(event.target)) return;
    if (pill && pill.contains(event.target)) return;
    closePopover(true);
  });
  tabRecordings.addEventListener("click", () => setView("recordings"));
  tabReview.addEventListener("click", () => setView("review"));
  tabPeople.addEventListener("click", () => setView("people"));
  document.getElementById("refresh").addEventListener("click", () => {
    if (view === "review") loadSpeakerReview(true);
    else loadDay();
  });
  day.addEventListener("change", loadDay);
  search.addEventListener("input", render);
  showAll.addEventListener("change", render);
  if (flatList) flatList.addEventListener("change", renderList);
  playOriginal.addEventListener("click", () => { playbackKind = "original"; render(); });
  playEnhanced.addEventListener("click", () => { playbackKind = "enhanced"; render(); });
  window.addEventListener("resize", () => {
    const nextMobileLayout = isMobile();
    if (nextMobileLayout === mobileLayout) return;
    mobileLayout = nextMobileLayout;
    filters.open = !mobileLayout;
    if (payload) render();
  });
  loadDays().then(loadDay).catch(() => { status.textContent = "Open through the local launcher."; });
})();
"""

class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True



def _access_unavailable(handler) -> None:
    handler._json(503, {"error": "Access verification is unavailable"})


class ViewerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LifeViewer"

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass

    def _inbox(self):
        return self.server.inbox

    def _token(self) -> str:
        return self.server.viewer_token

    def _requested_host(self) -> str:
        return (self.headers.get("Host") or "").split(":")[0].strip().lower()

    def _remote_config(self):
        return getattr(self.server, "remote_access", None)

    def _is_remote_host(self) -> bool:
        config = self._remote_config()
        return bool(config) and self._requested_host() == config.host

    def _host_ok(self) -> bool:
        host = self._requested_host()
        if host in LOCAL_HOSTS:
            return True
        config = self._remote_config()
        return bool(config) and host == config.host

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        host = (parsed.hostname or "").lower()
        if host in LOCAL_HOSTS and parsed.scheme in ("http", "https") and parsed.path in ("", "/"):
            return True
        config = self._remote_config()
        return bool(config) and origin == config.origin

    def _authorized(self) -> bool:
        if self._requested_host() in LOCAL_HOSTS:
            supplied = self.headers.get("Authorization", "")
            return hmac.compare_digest(supplied.encode(), ("Bearer " + self._token()).encode())
        config = self._remote_config()
        if not config or getattr(self.server, "access_verifier", None) is None:
            return False
        kind, _client = agent_http.access_outcome(self)
        if kind == "unavailable":
            raise VerificationUnavailable("Access verification is unavailable")
        return kind == "human"

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

    def _json(self, status: int, payload: dict, extra=None):
        self._send(status, json.dumps(payload).encode(), "application/json", extra)

    def do_OPTIONS(self):
        self.send_response(403)
        self._headers("text/plain", 0)
        self.close_connection = True

    def parse_byte_range(self, header: str, size: int):
        if not header.startswith("bytes=") or "," in header:
            raise ValueError("unsatisfiable")
        left, right = header[6:].split("-", 1)
        if left == "" and right == "":
            raise ValueError("unsatisfiable")
        if left == "":
            suffix = int(right)
            if suffix <= 0 or size == 0:
                raise ValueError("unsatisfiable")
            start = max(0, size - suffix)
            return start, size - 1
        start = int(left)
        end = int(right) if right else size - 1
        if start < 0 or (right and end < start) or start >= size:
            raise ValueError("unsatisfiable")
        return start, min(end, size - 1)

    def _audio_type(self, audio: Path) -> str:
        suffix = audio.suffix.lower()
        if suffix == ".wav":
            return "audio/wav"
        if suffix in (".m4a", ".mp4", ".aac"):
            return "audio/mp4"
        return "application/octet-stream"

    def _event_audio(self, event_id: str, kind: str, include_body: bool):
        if kind not in ("original", "enhanced"):
            return self._json(400, {"error": "Invalid audio kind"})
        audio = self._inbox().event_audio_path(event_id, kind)
        if not audio or not audio.is_file():
            return self._json(404, {"error": "Audio unavailable"})
        return self._send_audio(audio, include_body=include_body)

    def _send_audio(self, audio: Path, include_body: bool):
        size = audio.stat().st_size
        start, end, status = 0, max(0, size - 1), 200
        range_header = self.headers.get("Range")
        extra = {"Accept-Ranges": "bytes"}
        if range_header:
            try:
                start, end = self.parse_byte_range(range_header, size)
                status = 206
                extra["Content-Range"] = f"bytes {start}-{end}/{size}"
            except (ValueError, TypeError):
                return self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
        length = 0 if size == 0 else (end - start + 1)
        extra.pop("Content-Length", None)
        self.send_response(status)
        self._headers(self._audio_type(audio), length, extra)
        if include_body and length:
            with audio.open("rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining:
                    chunk = handle.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        self.close_connection = True

    def _speaker_review(self, parsed) -> bool:
        if parsed.path not in ("/v1/speaker-review", "/v1/speaker-review/diagnostics"):
            return False
        try:
            if len(parsed.query) > 600:
                raise ValueError("query")
            params = parse_qs(parsed.query, keep_blank_values=False, strict_parsing=True) if parsed.query else {}
            with self._inbox().connect() as db:
                if parsed.path.endswith("/diagnostics"):
                    if params:
                        raise ValueError("query")
                    report = speaker_review.diagnose_held_out(db)
                else:
                    if set(params) - {"limit", "cursor"}:
                        raise ValueError("query")
                    limit = speaker_review.PAGE_DEFAULT
                    if "limit" in params:
                        raw = params["limit"]
                        if len(raw) != 1 or not raw[0].isdigit() or len(raw[0]) > 3:
                            raise ValueError("limit")
                        limit = int(raw[0])
                    cursor = None
                    if "cursor" in params:
                        raw = params["cursor"]
                        if len(raw) != 1:
                            raise ValueError("cursor")
                        cursor = raw[0]
                    report = speaker_review.review_queue(db, limit=limit, cursor=cursor)
        except ValueError:
            self._json(400, {"error": "Invalid review request"})
            return True
        self._json(200, report)
        return True

    def _reject_suggestion(self, path: str, body: dict):
        turn_id = path[len("/v1/turns/"):-len("/reject")]
        person_id = body.get("person_id", "")
        if not isinstance(person_id, str) or set(body) - {"person_id"}:
            return self._json(400, {"error": "Invalid rejection"})
        try:
            with self._inbox().connect() as db:
                saved = speaker_review.reject_suggestion(db, turn_id, person_id)
        except ValueError:
            return self._json(400, {"error": "Invalid rejection"})
        if not saved:
            return self._json(404, {"error": "Turn or person unavailable"})
        return self._json(200, {"rejected": True})

    def do_HEAD(self):
        if not self._host_ok() or not self._origin_ok():
            return self._json(403, {"error": "Forbidden"})
        try:
            if agent_http.intercept(self, "HEAD"):
                return
        except VerificationUnavailable:
            return _access_unavailable(self)
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/app.css", "/app.js", "/favicon-16.png", "/favicon-32.png",
                    "/app-icon.png", "/apple-touch-icon.png"):
            if self._is_remote_host() and not self._authorized():
                return self._json(401, {"error": "Unauthorized"})
            assets = {
                "/": (APP.encode(), "text/html; charset=utf-8"),
                "/app.css": (CSS.encode(), "text/css"),
                "/app.js": (JS.encode(), "text/javascript"),
                "/favicon-16.png": ((Path(__file__).with_name("assets") / "favicon-16.png").read_bytes(), "image/png"),
                "/favicon-32.png": ((Path(__file__).with_name("assets") / "favicon-32.png").read_bytes(), "image/png"),
                "/app-icon.png": ((Path(__file__).with_name("assets") / "life-recorder-icon.png").read_bytes(), "image/png"),
                "/apple-touch-icon.png": ((Path(__file__).with_name("assets") / "apple-touch-icon.png").read_bytes(), "image/png"),
            }
            body, ctype = assets[path]
            self.send_response(200)
            self._headers(ctype, len(body))
            self.close_connection = True
            return
        if not self._authorized():
            return self._json(401, {"error": "Unauthorized"})
        if path.startswith("/v1/audio/"):
            chunk_id = path.removeprefix("/v1/audio/")
            row = self._inbox().receipt(chunk_id)
            if not row or row["status"] != "complete" or row["audio_state"] != "present":
                return self._json(404, {"error": "Audio unavailable"})
            audio = Path(row["path"])
            if not audio.is_file():
                return self._json(404, {"error": "Audio unavailable"})
            return self._send_audio(audio, include_body=False)
        if path.startswith("/v1/events/") and path.endswith("/audio"):
            event_id = path[len("/v1/events/"):-len("/audio")]
            kind = (parse_qs(urlparse(self.path).query).get("kind") or ["original"])[0]
            return self._event_audio(event_id, kind, include_body=False)
        return self._json(404, {"error": "Not found"})

    def do_GET(self):
        if not self._host_ok() or not self._origin_ok():
            return self._json(403, {"error": "Forbidden"})
        try:
            if agent_http.intercept(self, "GET"):
                return
        except VerificationUnavailable:
            return _access_unavailable(self)
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/app.css", "/app.js", "/favicon-16.png", "/favicon-32.png",
                    "/app-icon.png", "/apple-touch-icon.png"):
            if self._is_remote_host() and not self._authorized():
                return self._json(401, {"error": "Unauthorized"})
            assets = {
                "/": (APP.encode(), "text/html; charset=utf-8"),
                "/app.css": (CSS.encode(), "text/css"),
                "/app.js": (JS.encode(), "text/javascript"),
                "/favicon-16.png": ((Path(__file__).with_name("assets") / "favicon-16.png").read_bytes(), "image/png"),
                "/favicon-32.png": ((Path(__file__).with_name("assets") / "favicon-32.png").read_bytes(), "image/png"),
                "/app-icon.png": ((Path(__file__).with_name("assets") / "life-recorder-icon.png").read_bytes(), "image/png"),
                "/apple-touch-icon.png": ((Path(__file__).with_name("assets") / "apple-touch-icon.png").read_bytes(), "image/png"),
            }
            body, ctype = assets[path]
            return self._send(200, body, ctype)
        if not self._authorized():
            return self._json(401, {"error": "Unauthorized"})
        if path == "/v1/days":
            return self._json(200, {"days": self._inbox().viewer_days()})
        if path.startswith("/v1/audio/"):
            chunk_id = path.removeprefix("/v1/audio/")
            row = self._inbox().receipt(chunk_id)
            if not row or row["status"] != "complete" or row["audio_state"] != "present":
                return self._json(404, {"error": "Audio unavailable"})
            audio = Path(row["path"])
            if not audio.is_file():
                return self._json(404, {"error": "Audio unavailable"})
            return self._send_audio(audio, include_body=True)
        if path.startswith("/v1/events/") and path.endswith("/audio"):
            event_id = path[len("/v1/events/"):-len("/audio")]
            kind = (parse_qs(parsed.query).get("kind") or ["original"])[0]
            return self._event_audio(event_id, kind, include_body=True)
        if path.startswith("/v1/chunks/") and path.endswith("/review"):
            chunk_id = path[len("/v1/chunks/"):-len("/review")]
            review = self._inbox().chunk_review(chunk_id)
            if not review:
                return self._json(404, {"error": "Recording unavailable"})
            return self._json(200, review)
        if path.startswith("/v1/days/"):
            day = path.removeprefix("/v1/days/")
            try:
                datetime.strptime(day, "%Y-%m-%d")
            except ValueError:
                return self._json(400, {"error": "Invalid day"})
            return self._json(200, self._inbox().viewer_day(day))
        if self._speaker_review(parsed):
            return
        return self._json(404, {"error": "Not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._json(401, {"error": "Unauthorized"})
        try:
            if agent_http.intercept(self, "POST"):
                return
        except VerificationUnavailable:
            return _access_unavailable(self)
        if not self._authorized():
            return self._json(401, {"error": "Unauthorized"})
        origin = self.headers.get("Origin")
        if self._is_remote_host():
            if origin != self._remote_config().origin:
                return self._json(403, {"error": "Forbidden"})
        elif not self._origin_ok():
            return self._json(403, {"error": "Forbidden"})
        path = urlparse(self.path).path
        if self.headers.get("Transfer-Encoding"):
            return self._json(400, {"error": "Transfer encoding unsupported"})
        if self.headers.get_content_type() != "application/json":
            return self._json(415, {"error": "JSON required"})
        try:
            length = int(self.headers.get("Content-Length", ""))
        except (TypeError, ValueError):
            return self._json(400, {"error": "Invalid content length"})
        if length < 0:
            return self._json(400, {"error": "Invalid content length"})
        if length > 4096:
            return self._json(413, {"error": "Request too large"})
        raw = self.rfile.read(length)
        if len(raw) != length:
            return self._json(400, {"error": "Incomplete body"})
        try:
            body = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "Invalid JSON"})
        if not isinstance(body, dict):
            return self._json(400, {"error": "JSON object required"})
        if path == "/v1/event-edits":
            try:
                return self._json(200, self._inbox().save_event_edit(body))
            except event_edits.EditError as error:
                return self._json(error.status, error.payload)
        if path.startswith("/v1/chunks/") and path.endswith("/retry"):
            if body:
                return self._json(400, {"error": "No retry fields expected"})
            chunk_id = path[len("/v1/chunks/"):-len("/retry")]
            try:
                normalized = str(uuid.UUID(chunk_id))
                if normalized != chunk_id.lower():
                    raise ValueError("chunk id")
            except ValueError:
                return self._json(400, {"error": "Invalid recording"})
            record = self._inbox().retry_chunk(normalized)
            if record is None:
                return self._json(404, {"error": "Recording unavailable"})
            if record["status"] == "needs_attention" and not record["retry_eligible"]:
                return self._json(409, {"error": "Original audio unavailable"})
            return self._json(200, record)
        if path == "/v1/people":
            try:
                return self._json(201, self._inbox().create_person(body.get("name", "")))
            except ValueError:
                return self._json(400, {"error": "Invalid name"})
        if path.startswith("/v1/people/"):
            try:
                person = self._inbox().rename_person(path.removeprefix("/v1/people/"), body.get("name", ""))
            except ValueError:
                return self._json(400, {"error": "Invalid name"})
            return self._json(200, person) if person else self._json(404, {"error": "Person unavailable"})
        if path.startswith("/v1/turns/") and path.endswith("/reject"):
            return self._reject_suggestion(path, body)
        if path.startswith("/v1/turns/") and path.endswith("/label"):
            turn_id = path[len("/v1/turns/"):-len("/label")]
            person_id = body.get("person_id", "")
            if not isinstance(person_id, str):
                return self._json(400, {"error": "Invalid person"})
            if self._inbox().label_turn(turn_id, person_id, body.get("use_sample") is True):
                return self._json(200, {"labeled": True})
            return self._json(404, {"error": "Turn or person unavailable"})
        if path.startswith("/v1/chunks/") and path.endswith("/keep"):
            chunk_id = path[len("/v1/chunks/"):-len("/keep")]
            if self._inbox().keep_audio(chunk_id):
                return self._json(200, {"kept": True})
            return self._json(404, {"error": "Audio unavailable"})
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


def remote_access_config(host=None, team_domain=None, audience=None, issuer=None):
    host = host or os.environ.get("LIFE_RECORDER_VIEWER_REMOTE_HOST", "")
    team_domain = team_domain or os.environ.get("LIFE_RECORDER_ACCESS_TEAM_DOMAIN", "")
    audience = audience or os.environ.get("LIFE_RECORDER_ACCESS_AUD", "")
    issuer = issuer or os.environ.get("LIFE_RECORDER_ACCESS_ISSUER") or None
    if not host and not team_domain and not audience:
        return None
    return RemoteAccessConfig.from_values(host or DEFAULT_REMOTE_HOST, team_domain, audience, issuer)


def start_viewer(inbox, host: str = VIEWER_HOST, port: int = VIEWER_PORT, remote=None):
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
    server.remote_access = remote
    server.access_verifier = AccessVerifier(remote) if remote else None
    server.agent_limiter = agent_http.Limiter()
    inbox.viewer_error = None
    inbox.viewer_server = server
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    inbox.viewer_thread = thread
    return server
