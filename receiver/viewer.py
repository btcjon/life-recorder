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
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'none'; media-src 'self' blob:; "
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
<a class="skip" href="#pane">Skip to recording</a>
<header>
  <h1>Life Recorder</h1>
  <nav aria-label="Library">
    <button id="tab-recordings" type="button" aria-pressed="true">Recordings</button>
    <button id="tab-people" type="button" aria-pressed="false">People</button>
  </nav>
  <label>Day <input id="day" type="date"></label>
  <label>Search <input id="search" type="search" placeholder="Search recordings"></label>
  <label><input id="show-all" type="checkbox"> Show quiet/pending</label>
  <button id="refresh" type="button">Refresh</button>
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
  <button id="keep" type="button" hidden>Keep audio</button>
</footer>
<p id="help">Possible event labels are heuristic. TV or podcasts can still match. Audio is retained for seven days by default; Keep protects a clip.</p>
<script src="/app.js"></script>
</body>
</html>
"""
CSS = """
:root { color-scheme: light; --sidebar: 304px; --canvas: #f5f6f8; --rail: #eceff3; --surface: #fff; --text: #18212f; --muted: #596577; --line: #dce1e8; --accent: #245fcc; --accent-soft: #eaf1ff; }
* { box-sizing: border-box; }
html, body { margin: 0; font: 14px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif; background: var(--canvas); color: var(--text); }
.skip { position: absolute; left: -999px; }
.skip:focus { left: 12px; top: 12px; z-index: 10; background: #fff; padding: 8px; border-radius: 8px; }
header { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; min-height: 64px; padding: 12px 20px; background: rgba(255,255,255,.94); border-bottom: 1px solid var(--line); }
h1 { margin: 0 12px 0 0; font-size: 18px; letter-spacing: -.01em; }
h2 { margin: 0; font-size: 22px; letter-spacing: -.02em; }
h3 { margin: 0 0 6px; font-size: 14px; }
nav { display: flex; gap: 6px; }
button, select, input { min-height: 34px; padding: 6px 10px; border: 1px solid #c9d0da; border-radius: 8px; background: var(--surface); color: var(--text); font: inherit; }
button { cursor: pointer; font-weight: 550; }
button:hover { border-color: #9eabbc; background: #f8fafc; }
button[aria-pressed="true"] { border-color: #abc3f3; background: var(--accent-soft); color: #184b9f; }
button:disabled { cursor: default; opacity: .55; }
button:focus-visible, select:focus-visible, input:focus-visible, .row:focus-visible, summary:focus-visible { outline: 3px solid rgba(36,95,204,.3); outline-offset: 2px; }
header label { display: flex; gap: 6px; align-items: center; color: var(--muted); font-size: 12px; }
#status { margin: 0 0 0 auto; color: var(--muted); font-size: 12px; }
#library { display: grid; grid-template-columns: var(--sidebar) minmax(0, 1fr); height: calc(100vh - 166px); min-height: 460px; }
aside, #pane, #people-view { overflow: auto; }
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
.group > p { margin: 7px 0; padding-left: 12px; border-left: 3px solid #d9dfe8; }
.identity-anchor { position: static; display: inline-flex; }
.popover { position: absolute; top: calc(100% + 8px); left: 0; z-index: 20; width: min(300px, calc(100vw - 24px)); padding: 12px; border: 1px solid var(--line); border-radius: 12px; background: #fff; box-shadow: 0 12px 32px rgba(24,33,47,.16); }
.person-option { display: flex; width: 100%; margin: 0 0 4px; text-align: left; }
.person-option[aria-pressed="true"] { border-color: #abc3f3; background: var(--accent-soft); }
.popover-footer { display: flex; justify-content: flex-end; gap: 8px; margin-top: 10px; }
.popover details { margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--line); }
@media (max-width: 390px) { .popover { width: calc(100vw - 44px); } }
#player-bar { position: sticky; bottom: 0; display: grid; grid-template-columns: minmax(160px, .7fr) minmax(280px, 1.4fr) auto; gap: 14px; align-items: center; min-height: 74px; padding: 10px 20px; border-top: 1px solid var(--line); background: rgba(255,255,255,.96); box-shadow: 0 -8px 24px rgba(24,33,47,.06); }
#now-playing { margin: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
#player { width: min(520px, 100%); }
#help { margin: 0; padding: 7px 20px; color: var(--muted); background: var(--surface); font-size: 11px; text-align: center; }
details { margin-top: 20px; padding-top: 14px; border-top: 1px solid var(--line); }
summary { cursor: pointer; color: var(--muted); }
.hidden { display: none; }
[hidden] { display: none !important; }
@media (max-width: 900px) {
  #status { width: 100%; margin-left: 0; }
  #library { grid-template-columns: 1fr; height: auto; }
  aside { min-width: 0; max-width: none; width: auto; max-height: 36vh; border-right: 0; border-bottom: 1px solid var(--line); }
  #player-bar { grid-template-columns: 1fr; }
}
@media (max-width: 390px) {
  header, #player-bar { padding: 8px; gap: 8px; }
  h1 { font-size: 15px; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { transition: none !important; animation: none !important; }
}
"""
JS = r"""
(() => {
  let token = location.hash.replace(/^#/, "") || sessionStorage.getItem("life-recorder-token") || "";
  if (location.hash) {
    sessionStorage.setItem("life-recorder-token", token);
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
  const tabRecordings = document.getElementById("tab-recordings");
  const tabPeople = document.getElementById("tab-people");
  let payload = null;
  let audioUrl = null;
  let loadedAudioId = null;
  let audioController = null;
  let audioGeneration = 0;
  let dayRequest = 0;
  let view = "recordings";
  let selectedId = null;
  let detailMode = "transcript";
  let openIdentityKey = null;
  let identityDraft = null;
  let identityMode = "list";
  let createdPersonId = null;
  let createdPersonKey = null;
  let createdPersonName = null;
  let identitySample = false;
  let identityNewName = "";
  function authHeaders() {
    return { Authorization: "Bearer " + token };
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
    if (selectedId && !(payload.chunks || []).some((chunk) => chunk.id === selectedId)) selectedId = null;
    render();
    const issues = [];
    if (payload.pending) issues.push(payload.pending + " pending");
    if (payload.errors) issues.push(payload.errors + " errors");
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
  function groupTurns(chunk) {
    const groups = [];
    const index = new Map();
    for (const turn of chunk.speakers || []) {
      const key = chunk.id + ":" + (turn.run_id || "") + ":" + (turn.speaker_key || "Unknown");
      if (!index.has(key)) {
        const group = { key, chunk, run_id: turn.run_id, speaker_key: turn.speaker_key, turns: [], preserved: !!turn.preserved };
        index.set(key, group);
        groups.push(group);
      }
      const group = index.get(key);
      group.turns.push(turn);
      group.preserved = group.preserved || !!turn.preserved;
    }
    return groups;
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
    if (!selectedId && chunks[0]) selectedId = chunks[0].id;
    for (const chunk of chunks) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "row";
      row.setAttribute("aria-current", chunk.id === selectedId ? "true" : "false");
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
        if (selectedId !== chunk.id) openIdentityKey = null;
        selectedId = chunk.id;
        render();
      });
      list.appendChild(row);
    }
    if (!chunks.length) {
      const empty = document.createElement("p");
      empty.className = "meta";
      empty.textContent = "No recordings match this day or search.";
      list.appendChild(empty);
    }
  }
  function attachAudio(chunk) {
    const nextAudioId = chunk && chunk.audio_playable ? chunk.id : null;
    keepButton.hidden = !chunk || !chunk.audio_playable;
    keepButton.disabled = !!(chunk && chunk.audio_pinned);
    keepButton.textContent = chunk && chunk.audio_pinned ? "Kept" : "Keep audio";
    nowPlaying.textContent = chunk ? ((chunk.started_local || "Recording") + (chunk.audio_playable ? "" : " · audio unavailable")) : "No recording selected";
    keepButton.onclick = chunk && chunk.audio_playable ? async () => {
      const keptId = chunk.id;
      const response = await fetch("/v1/chunks/" + keptId + "/keep", { method: "POST", headers: authHeaders() });
      if (!response.ok) return;
      const current = (payload.chunks || []).find((item) => item.id === keptId);
      if (current) current.audio_pinned = true;
      if (selectedId === keptId) {
        keepButton.textContent = "Kept";
        keepButton.disabled = true;
      }
    } : null;
    if (nextAudioId === loadedAudioId) return;
    loadedAudioId = nextAudioId;
    audioGeneration += 1;
    if (audioController) audioController.abort();
    audioController = null;
    if (audioUrl) { URL.revokeObjectURL(audioUrl); audioUrl = null; }
    player.removeAttribute("src");
    if (!chunk || !chunk.audio_playable) return;
    const requestedId = chunk.id;
    audioController = new AbortController();
    fetch("/v1/audio/" + chunk.id, { headers: authHeaders(), signal: audioController.signal }).then((r) => r.ok ? r.blob() : Promise.reject()).then((blob) => {
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
      openIdentityKey = null;
      return loadDay();
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
    const sampleDetails = document.createElement("details");
    const sampleSummary = document.createElement("summary");
    sampleSummary.textContent = "Voice learning";
    const sample = document.createElement("label");
    const sampleBox = document.createElement("input");
    sampleBox.type = "checkbox";
    sampleBox.checked = identitySample;
    sample.appendChild(sampleBox);
    sample.append(" Save a voice sample from this clip only if it is long enough");
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
    const chunk = (payload.chunks || []).find((item) => item.id === selectedId);
    attachAudio(chunk);
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
    modes.appendChild(transcriptBtn);
    modes.appendChild(turnsBtn);
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
      pill.textContent = info.label;
      pill.setAttribute("data-identity-key", group.key);
      pill.setAttribute("aria-expanded", String(openIdentityKey === group.key));
      pill.setAttribute("aria-haspopup", "dialog");
      pill.setAttribute("aria-label", info.label);
      pill.addEventListener("click", (event) => {
        event.stopPropagation();
        if (openIdentityKey === group.key) { closePopover(true); return; }
        openIdentityKey = group.key;
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
      if (openIdentityKey === group.key) anchor.appendChild(identityPopover(group));
      speakers.appendChild(anchor);
    }
    pane.appendChild(speakers);
    pane.appendChild(modes);
    if (detailMode === "transcript") {
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
        const heading = document.createElement("div");
        heading.textContent = groupLabel(group).label;
        card.appendChild(heading);
        for (const turn of group.turns) {
          const line = document.createElement("p");
          const start = Number.isFinite(Number(turn.started)) ? Number(turn.started).toFixed(1) : "?";
          const end = Number.isFinite(Number(turn.ended)) ? Number(turn.ended).toFixed(1) : "?";
          const seek = document.createElement("button");
          seek.type = "button";
          seek.textContent = start + "–" + end + "s";
          seek.setAttribute("aria-label", "Play speaker turn at " + start + " seconds");
          seek.disabled = !chunk.audio_playable;
          seek.addEventListener("click", () => {
            const expectedId = chunk.id;
            const expectedGeneration = audioGeneration;
            const jump = () => {
              if (selectedId !== expectedId || loadedAudioId !== expectedId || audioGeneration !== expectedGeneration) return;
              player.currentTime = Number(turn.started) || 0;
              player.play().catch(() => {});
            };
            if (player.readyState) jump(); else player.addEventListener("loadedmetadata", jump, { once: true });
          });
          line.appendChild(seek);
          line.append(turn.text ? " — " + turn.text : "");
          card.appendChild(line);
        }
        pane.appendChild(card);
      }
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
    if (view === "people") {
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
        if path.startswith("/v1/audio/"):
            chunk_id = path.removeprefix("/v1/audio/")
            row = self._inbox().receipt(chunk_id)
            if not row or row["status"] != "complete" or row["audio_state"] != "present":
                return self._json(404, {"error": "Audio unavailable"})
            audio = Path(row["path"])
            if not audio.is_file():
                return self._json(404, {"error": "Audio unavailable"})
            size = audio.stat().st_size
            start, end, status = 0, max(0, size - 1), 200
            range_header = self.headers.get("Range")
            if range_header:
                if not range_header.startswith("bytes=") or "," in range_header:
                    return self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
                try:
                    left, right = range_header[6:].split("-", 1)
                    start = int(left) if left else max(0, size - int(right))
                    end = int(right) if right else size - 1
                    if start < 0 or end < start or start >= size:
                        raise ValueError
                    end = min(end, size - 1); status = 206
                except (ValueError, TypeError):
                    return self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
            with audio.open("rb") as handle:
                handle.seek(start); body = handle.read(end - start + 1)
            extra = {"Accept-Ranges": "bytes"}
            if status == 206:
                extra["Content-Range"] = f"bytes {start}-{end}/{size}"
            return self._send(status, body, "audio/mp4", extra)
        if path.startswith("/v1/days/"):
            day = path.removeprefix("/v1/days/")
            try:
                datetime.strptime(day, "%Y-%m-%d")
            except ValueError:
                return self._json(400, {"error": "Invalid day"})
            return self._json(200, self._inbox().viewer_day(day))
        return self._json(404, {"error": "Not found"})

    def do_POST(self):
        if not self._host_ok() or not self._origin_ok() or not self._authorized():
            return self._json(401, {"error": "Unauthorized"})
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = {}
        if length:
            if length > 4096:
                return self._json(413, {"error": "Request too large"})
            try:
                body = json.loads(self.rfile.read(length))
            except (ValueError, json.JSONDecodeError):
                return self._json(400, {"error": "Invalid JSON"})
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
