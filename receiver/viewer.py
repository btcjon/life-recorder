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
from urllib.parse import parse_qs, urlparse

from access_auth import AccessAuthError, AccessVerifier, RemoteAccessConfig

VIEWER_PORT = 8767
VIEWER_HOST = "127.0.0.1"
LOCAL_HOSTS = ("127.0.0.1", "localhost")
DEFAULT_REMOTE_HOST = "lr.genr8ive.ai"
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
<meta name="theme-color" content="#245fcc">
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
    <h1>Life Recorder</h1>
    <nav aria-label="Library">
      <button id="tab-recordings" type="button" aria-pressed="true">Recordings</button>
      <button id="tab-people" type="button" aria-pressed="false">People</button>
    </nav>
    <label class="day-control">Day <input id="day" type="date"></label>
    <button id="refresh" type="button">Refresh</button>
  </div>
  <details id="filters" open>
    <summary>Filters</summary>
    <div class="filter-body">
      <label>Search <input id="search" type="search" placeholder="Search recordings"></label>
      <label><input id="show-all" type="checkbox"> Show quiet/pending</label>
    </div>
  </details>
  <p id="status" aria-live="polite">Loading</p>
</header>
<main>
  <div id="library">
    <aside id="list" aria-label="Recordings"></aside>
    <section id="pane" tabindex="-1" aria-live="polite"></section>
  </div>
  <section id="people-view" hidden>
    <h2>People</h2>
    <div id="people"></div>
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
<p id="help">Possible event labels are heuristic. TV or podcasts can still match. Audio is retained for seven days by default; Keep protects a clip. Enhanced playback is optional and never used for transcription.</p>
<script src="/app.js"></script>
</body>
</html>
"""
CSS = """
:root { color-scheme: light; --sidebar: 304px; --canvas: #f5f6f8; --rail: #eceff3; --surface: #fff; --text: #18212f; --muted: #596577; --line: #dce1e8; --accent: #245fcc; --accent-soft: #eaf1ff; }
* { box-sizing: border-box; }
html, body { margin: 0; min-width: 0; overflow-wrap: anywhere; font: 14px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif; background: var(--canvas); color: var(--text); }
body { min-height: 100vh; display: flex; flex-direction: column; }
main { flex: 1 1 auto; min-height: 0; }
.skip { position: absolute; left: -999px; }
.skip:focus { left: 12px; top: 12px; z-index: 10; background: #fff; padding: 8px; border-radius: 8px; }
header { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; min-height: 64px; padding: 12px 20px; padding-left: max(20px, env(safe-area-inset-left)); padding-right: max(20px, env(safe-area-inset-right)); background: rgba(255,255,255,.94); border-bottom: 1px solid var(--line); }
.header-primary { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; min-width: 0; }
#filters { margin: 0; padding: 0; border: 0; }
#filters .filter-body { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
h1 { margin: 0 12px 0 0; font-size: 18px; letter-spacing: -.01em; }
h2 { margin: 0; font-size: 22px; letter-spacing: -.02em; }
h3 { margin: 0 0 6px; font-size: 14px; }
nav { display: flex; gap: 6px; }
button, select, input { min-height: 34px; padding: 6px 10px; border: 1px solid #c9d0da; border-radius: 8px; background: var(--surface); color: var(--text); font: inherit; }
#back-recordings { display: none; }
button { cursor: pointer; font-weight: 550; }
button:hover { border-color: #9eabbc; background: #f8fafc; }
button[aria-pressed="true"] { border-color: #abc3f3; background: var(--accent-soft); color: #184b9f; }
button:disabled { cursor: default; opacity: .55; }
button:focus-visible, select:focus-visible, input:focus-visible, .row:focus-visible, summary:focus-visible { outline: 3px solid rgba(36,95,204,.3); outline-offset: 2px; }
header label { display: flex; gap: 6px; align-items: center; color: var(--muted); font-size: 12px; }
#status { margin: 0 0 0 auto; color: var(--muted); font-size: 12px; }
#library { display: grid; grid-template-columns: var(--sidebar) minmax(0, 1fr); grid-template-rows: minmax(0, 1fr); height: 100%; min-height: 0; }
aside, #pane, #people-view { overflow: auto; min-height: 0; }
aside { min-width: 280px; max-width: 320px; width: var(--sidebar); padding: 14px 12px; background: var(--rail); border-right: 1px solid var(--line); }
#pane { padding: 28px clamp(22px, 5vw, 64px) 80px; }
#pane > * { max-width: 860px; }
#people-view { padding: 28px clamp(22px, 5vw, 64px) 100px; }
.row, .item, .person, .group { border: 1px solid var(--line); padding: 12px; margin: 0 0 9px; background: var(--surface); border-radius: 11px; box-shadow: 0 1px 2px rgba(24,33,47,.035); }
.row { width: 100%; text-align: left; cursor: pointer; font-weight: 500; }
.row[aria-current="true"] { border-color: #9fbbef; background: var(--accent-soft); box-shadow: 0 0 0 1px rgba(36,95,204,.08); }
.badge { display: inline-block; padding: 2px 8px; margin-right: 6px; border: 0; border-radius: 999px; font-size: 11px; font-weight: 650; }
.manual, .confirmed { background: #dff4e8; color: #17643a; }
.possible, .preserved { background: #fff0ce; color: #805800; }
.unknown { background: #edf0f4; color: #566273; }
.suggested { background: #e4edff; color: #2455a7; }
.meta { color: var(--muted); font-size: 12px; }
.preview { margin: 4px 0 0; }
.toolbar { display: flex; gap: 8px; flex-wrap: wrap; margin: 8px 0; }
.speakers { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 10px 0 14px; position: relative; }
.pill { border-radius: 999px; min-height: 30px; padding: 4px 10px; }
.pill[aria-expanded="true"] { box-shadow: 0 0 0 2px rgba(36,95,204,.25); }
.pill[aria-disabled="true"] { cursor: default; opacity: .85; }
.group { position: relative; padding: 14px; }
.group.active { border-color: #9fbbef; box-shadow: 0 0 0 1px rgba(36,95,204,.12); }
.group-head { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: space-between; }
.group-controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.group-progress { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; width: 100%; margin: 8px 0 4px; }
.group-progress progress { flex: 1 1 160px; min-width: 120px; width: 100%; height: 10px; max-height: 10px; appearance: none; -webkit-appearance: none; border: 0; border-radius: 999px; background: #edf0f4; overflow: hidden; }
.group-progress progress::-webkit-progress-bar { background: #edf0f4; border-radius: 999px; }
.group-progress progress::-webkit-progress-value, .group-progress progress::-moz-progress-bar { background: #245fcc; border-radius: 999px; }
.group-time { color: var(--muted); font-variant-numeric: tabular-nums; font-size: 12px; }
.group > p { margin: 7px 0; padding-left: 12px; border-left: 3px solid #d9dfe8; }
.identity-anchor { position: relative; display: inline-flex; min-width: 0; }
.play-toggle[aria-pressed="true"] { border-color: #abc3f3; background: var(--accent-soft); color: #184b9f; }
.popover { position: absolute; top: calc(100% + 8px); left: 0; z-index: 20; width: min(300px, calc(100vw - 24px)); padding: 12px; border: 1px solid var(--line); border-radius: 12px; background: #fff; box-shadow: 0 12px 32px rgba(24,33,47,.16); }
.person-option { display: flex; width: 100%; margin: 0 0 4px; text-align: left; }
.person-option[aria-pressed="true"] { border-color: #abc3f3; background: var(--accent-soft); }
.popover-footer { display: flex; justify-content: flex-end; gap: 8px; margin-top: 10px; }
.popover details { margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--line); }
#player-bar { position: sticky; bottom: 0; display: grid; grid-template-columns: minmax(160px, .7fr) minmax(280px, 1.4fr) auto auto; gap: 14px; align-items: center; min-height: 74px; padding: 10px 20px; padding-bottom: max(10px, env(safe-area-inset-bottom)); padding-left: max(20px, env(safe-area-inset-left)); padding-right: max(20px, env(safe-area-inset-right)); border-top: 1px solid var(--line); background: rgba(255,255,255,.96); box-shadow: 0 -8px 24px rgba(24,33,47,.06); }
#now-playing { margin: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
#player { width: min(520px, 100%); }
#playback-mode { display: flex; gap: 6px; }
#help { margin: 0; padding: 7px 20px; color: var(--muted); background: var(--surface); font-size: 11px; text-align: center; }
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
  h1 { font-size: 16px; }
  button, select, input, summary, .row, .pill, .person-option, .play-toggle, .replay { min-height: 44px; font-size: 16px; }
  .group-controls { width: 100%; }
  .group-controls button { flex: 1 1 120px; }
  .group-progress { flex-direction: column; align-items: stretch; }
  .group-progress progress { height: 10px; max-height: 10px; }
  #filters { width: 100%; margin: 0; padding: 0; border: 0; }
  #filters > summary { min-height: 44px; font-size: 16px; }
  #filters .filter-body { flex-direction: column; align-items: stretch; padding-top: 8px; }
  #filters .filter-body label:first-child { flex-direction: column; align-items: stretch; }
  #filters .filter-body input[type="search"] { min-width: 0; width: 100%; }
  #status { width: 100%; margin: 0; }
  #library { display: block; height: auto; min-height: 0; }
  aside, #pane, #people-view { overflow: visible; min-width: 0; }
  aside { width: auto; max-width: none; min-width: 0; padding: 16px; padding-left: max(16px, env(safe-area-inset-left)); padding-right: max(16px, env(safe-area-inset-right)); border-right: 0; }
  #pane { padding: 16px; padding-left: max(16px, env(safe-area-inset-left)); padding-right: max(16px, env(safe-area-inset-right)); padding-bottom: 96px; }
  #people-view { padding: 16px; padding-left: max(16px, env(safe-area-inset-left)); padding-right: max(16px, env(safe-area-inset-right)); padding-bottom: 96px; }
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
  header, #player-bar, #help { flex: 0 0 auto; }
  main { min-height: 0; overflow: hidden; display: flex; flex-direction: column; }
  #library, #people-view { flex: 1 1 auto; min-height: 0; }
  #filters { display: contents; }
  #filters > summary { display: none; }
  #filters .filter-body { display: contents; }
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
  const tabPeople = document.getElementById("tab-people");
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
  let identityDraft = null;
  let identityMode = "list";
  let createdPersonId = null;
  let createdPersonKey = null;
  let createdPersonName = null;
  let identitySample = false;
  let identityNewName = "";
  let mobileDetailOpen = false;
  let listScroll = 0;
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
      "score_below_0.60": "Not a close enough match",
      "score_below_0.85": "Not a close enough match",
      "margin_below_0.10": "Too similar to another person",
      need_3_samples: "Needs 3 voice samples",
      need_2_clips: "Needs samples from 2 recordings",
      need_20s: "Needs 20 seconds of confirmed voice",
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
      if (!showAll.checked && !text.trim() && turns.length === 0) return false;
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
  async function loadDay() {
    const requestId = ++dayRequest;
    status.textContent = "Loading";
    const response = await fetch("/v1/days/" + day.value, { headers: authHeaders(), cache: "no-store" });
    if (requestId !== dayRequest) return;
    if (!response.ok) { status.textContent = "Unavailable"; return; }
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
    if (Number(shadow.evaluated) > 0) {
      issues.push(Number(shadow.would_hold || 0) + "/" + Number(shadow.evaluated) + " shadow-hold");
      if (Number(shadow.hold_vad_positive) > 0) issues.push(Number(shadow.hold_vad_positive) + " hold/VAD disagreement");
    }
    status.textContent = issues.length ? issues.join(" · ") : "Loaded";
  }
  function setView(next) {
    view = next;
    tabRecordings.setAttribute("aria-pressed", String(view === "recordings"));
    tabPeople.setAttribute("aria-pressed", String(view === "people"));
    library.hidden = view !== "recordings";
    peopleView.hidden = view !== "people";
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
    const names = new Set();
    const classes = new Set();
    for (const turn of turns) {
      const state = identityState(turn);
      classes.add(state.cls);
      if (turn.person_id) names.add(turn.person_id);
      else if (turn.suggested_person_id) names.add("suggested:" + turn.suggested_person_id);
      else names.add(state.cls);
    }
    if (classes.size > 1 || names.size > 1) return { label: "Mixed labels", cls: "unknown", interactive: true };
    const turn = turns[0];
    const named = turns.find((item) => item.name);
    if (turn.preserved) return { label: named ? named.name + " · Earlier label" : "Earlier label", cls: "preserved", interactive: true };
    if (turn.person_id && named) return { label: named.name + " ✓", cls: "confirmed", interactive: true };
    if (turn.suggested_name) return { label: "Possibly " + turn.suggested_name, cls: "suggested", interactive: true };
    return { label: "Unknown · " + key, cls: "unknown", interactive: true };
  }
  function enrollmentCopy(person) {
    const samples = Number(person.sample_count || 0);
    const seconds = Number(person.sample_seconds || 0).toFixed(1);
    const clips = Number(person.clip_count || 0);
    const stats = person.name + "'s " + samples + " samples/" + seconds + "s/" + clips + " clips";
    if (person.enrollment_ready) return "Ready for automatic suggestions · " + stats;
    const remaining = Math.max(0, 3 - samples);
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
    const gapStart = Number(previous.ended);
    const gapEnd = Number(next.started);
    if (!Number.isFinite(gapStart) || !Number.isFinite(gapEnd) || gapEnd <= gapStart) return true;
    const skip = new Set((currentTurns.concat([next])).map((turn) => turn.id));
    return !(all || []).some((turn) => {
      if (skip.has(turn.id)) return false;
      if ((turn.run_id || "") === (previous.run_id || "") && (turn.speaker_key || "Unknown") === (previous.speaker_key || "Unknown")) return false;
      const start = Number(turn.started);
      const end = Number(turn.ended);
      return Number.isFinite(start) && Number.isFinite(end) && start < gapEnd && end > gapStart;
    });
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
        last.preserved = last.preserved || !!turn.preserved;
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
    // Speech events are internal processing artifacts. The source recordings
    // carry transcripts, speaker clips, and assignment controls, so listing
    // both creates duplicate rows with mostly empty detail screens.
    const events = [];
    const eventRows = document.createDocumentFragment();
    if (!isMobile() && !selectedId && chunks[0]) { selectedId = chunks[0].id; selectedKind = "chunk"; }
    for (const item of events) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "row";
      row.id = "event-" + item.id;
      row.setAttribute("aria-current", selectedKind === "event" && item.id === selectedId ? "true" : "false");
      const title = document.createElement("div");
      title.textContent = (item.started_local || item.started || "Speech") + " · " + Number(item.playable_duration || item.duration || 0).toFixed(0) + "s";
      const peek = document.createElement("div");
      peek.className = "preview meta";
      peek.textContent = "Speech event";
      const audioNote = document.createElement("div");
      audioNote.className = "meta";
      audioNote.textContent = item.audio_playable ? (item.enhanced_playable ? "Original and enhanced audio" : "Original audio") : "Audio not kept";
      row.appendChild(title);
      row.appendChild(peek);
      row.appendChild(audioNote);
      row.addEventListener("click", () => {
        selectedId = item.id;
        selectedKind = "event";
        openIdentityKey = null;
        if (isMobile()) {
          listScroll = list.scrollTop || window.scrollY || 0;
          mobileDetailOpen = true;
        }
        render();
      });
      eventRows.appendChild(row);
    }
    for (const chunk of chunks) {
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
      const audioNote = document.createElement("div");
      audioNote.className = "meta";
      audioNote.textContent = chunk.status === "pending" ? "Processing" : (chunk.status !== "complete" ? "Processing failed" : (chunk.audio_playable ? "Audio available" : "Audio not kept"));
      row.appendChild(title);
      row.appendChild(peek);
      row.appendChild(audioNote);
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
      list.appendChild(row);
    }
    list.appendChild(eventRows);
    if (!chunks.length && !events.length) {
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
  function actionLabel(group, personId) {
    if (!personId) return "Assign";
    if ((group.turns || []).some((turn) => turn.person_id === personId)) return "Change";
    if ((group.turns || []).some((turn) => turn.suggested_person_id === personId || turn.suggested_name)) return "Confirm";
    return "Assign";
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
    const same = personId && personId === currentPersonId(group);
    if (same && !useSample) {
      closePopover(true);
      return Promise.resolve();
    }
    identityError = "";
    return fetch("/v1/turns/" + turn.id + "/label", {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ person_id: personId, use_sample: !!useSample })
    }).then((response) => {
      if (!response.ok) throw new Error("label");
      createdPersonId = null;
      identityDraft = null;
      identityMode = "list";
      identitySample = false;
      identityError = "";
      openIdentityKey = null;
      return loadDay();
    }).catch((error) => {
      identityError = "Could not save that identity.";
      throw error;
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
    const sampleDetails = document.createElement("details");
    const sampleSummary = document.createElement("summary");
    sampleSummary.textContent = "Voice learning";
    const sample = document.createElement("label");
    const sampleBox = document.createElement("input");
    sampleBox.type = "checkbox";
    sampleBox.checked = identitySample;
    sample.appendChild(sampleBox);
    sample.append(" Save a voice sample from this detected voice cluster if it is long enough");
    sampleBox.addEventListener("change", () => {
      identitySample = sampleBox.checked;
      if (identityMode !== "new") render();
    });
    sampleDetails.appendChild(sampleSummary);
    sampleDetails.appendChild(sample);
    const footer = document.createElement("div");
    footer.className = "popover-footer";
    function currentId() { return currentPersonId(group); }
    function draftChanged() {
      return identityDraft && identityDraft !== currentId();
    }
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
      box.appendChild(sampleDetails);
      box.appendChild(footer);
      box.appendChild(note);
      setTimeout(() => name.focus(), 0);
      return box;
    }
    for (const person of (payload.people || [])) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "person-option";
      row.setAttribute("aria-pressed", String((identityDraft || currentId()) === person.id));
      row.textContent = person.name;
      row.addEventListener("click", () => {
        identityDraft = person.id;
        render();
        setTimeout(() => {
          const save = document.querySelector(".popover-footer button:last-child");
          if (save) save.focus();
        }, 0);
      });
      box.appendChild(row);
    }
    const create = document.createElement("button");
    create.type = "button";
    create.className = "person-option";
    create.textContent = "New person";
    create.addEventListener("click", () => { identityMode = "new"; identityDraft = null; identityNewName = ""; render(); });
    box.appendChild(create);
    box.appendChild(sampleDetails);
    if (draftChanged() || (identitySample && currentId())) {
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.textContent = "Cancel";
      cancel.addEventListener("click", () => closePopover(true));
      const save = document.createElement("button");
      save.type = "button";
      save.textContent = draftChanged() ? actionLabel(group, identityDraft) : "Save voice sample";
      save.addEventListener("click", async () => {
        note.textContent = "Saving…";
        try { await applyIdentity(group, draftChanged() ? identityDraft : currentId(), identitySample); }
        catch (error) { note.textContent = "Could not save that identity."; }
      });
      footer.appendChild(cancel);
      footer.appendChild(save);
      box.appendChild(footer);
    }
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
    const groups = groupTurns(chunk);
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
      pill.setAttribute("aria-label", info.label);
      pill.addEventListener("click", (event) => {
        event.stopPropagation();
        if (openIdentityKey === group.key) { closePopover(true); return; }
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
        setTimeout(() => {
          const first = document.querySelector(".popover .person-option");
          if (first) first.focus();
        }, 0);
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
      speakers.appendChild(anchor);
    }
    pane.appendChild(modes);
    if (detailMode === "transcript") {
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
        const playGroup = (fromStart) => {
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
        };
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
  function render() {
    if (!payload) return;
    const mobile = isMobile();
    document.body.classList.toggle("mobile-list", mobile && view === "recordings" && !mobileDetailOpen);
    document.body.classList.toggle("mobile-detail", mobile && view === "recordings" && mobileDetailOpen);
    document.body.classList.toggle("mobile-people", mobile && view === "people");
    if (!mobile) mobileDetailOpen = false;
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
  document.addEventListener("click", (event) => {
    if (!openIdentityKey) return;
    const pop = document.querySelector(".popover");
    const pill = document.querySelector('[data-identity-key="' + openIdentityKey + '"]');
    if (pop && pop.contains(event.target)) return;
    if (pill && pill.contains(event.target)) return;
    closePopover(true);
  });
  tabRecordings.addEventListener("click", () => setView("recordings"));
  tabPeople.addEventListener("click", () => setView("people"));
  document.getElementById("refresh").addEventListener("click", loadDay);
  day.addEventListener("change", loadDay);
  search.addEventListener("input", render);
  showAll.addEventListener("change", render);
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
        verifier = getattr(self.server, "access_verifier", None)
        if not config or verifier is None:
            return False
        token = self.headers.get("Cf-Access-Jwt-Assertion", "")
        try:
            verifier.validate(token)
            return True
        except AccessAuthError:
            return False

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

    def do_HEAD(self):
        if not self._host_ok() or not self._origin_ok():
            return self._json(403, {"error": "Forbidden"})
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
        if path.startswith("/v1/days/"):
            day = path.removeprefix("/v1/days/")
            try:
                datetime.strptime(day, "%Y-%m-%d")
            except ValueError:
                return self._json(400, {"error": "Invalid day"})
            return self._json(200, self._inbox().viewer_day(day))
        return self._json(404, {"error": "Not found"})

    def do_POST(self):
        if not self._host_ok() or not self._authorized():
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
        if path.startswith("/v1/turns/") and path.endswith("/label"):
            turn_id = path[len("/v1/turns/"):-len("/label")]
            if self._inbox().label_turn(turn_id, body.get("person_id", ""), bool(body.get("use_sample"))):
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
    inbox.viewer_error = None
    inbox.viewer_server = server
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    inbox.viewer_thread = thread
    return server
