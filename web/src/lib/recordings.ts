import type { Chunk, DayPayload, DisplayBlock, SpeakerTurn, SpeechEvent, SpeechEventChunk } from "../api/types";

export type TurnGroup = {
  key: string;
  chunk: Chunk;
  run_id?: string;
  speaker_key?: string;
  turns: SpeakerTurn[];
  preserved: boolean;
};

export type ListRow =
  | { type: "heading"; key: string; label: string }
  | { type: "chunk"; key: string; chunk: Chunk }
  | { type: "group"; key: string; block: DisplayBlock; members: Chunk[]; matchId: string | null };

export type TextSegment = { text: string; match: boolean };

export function preview(text: string | undefined): string {
  const clean = (text || "").replace(/\s+/g, " ").trim();
  return clean ? clean.slice(0, 90) + (clean.length > 90 ? "…" : "") : "No transcript";
}

export function turnCount(chunk: Chunk): number {
  const turns = chunk.speakers || [];
  return Number(chunk.speaker_turn_count || 0) || turns.length;
}

export function isHiddenQuiet(chunk: Chunk, showQuiet: boolean, showPending = false): boolean {
  if (chunk.status === "needs_attention") return false;
  if (chunk.status && chunk.status !== "complete" && chunk.status !== "pending") return false;
  const blank = !(chunk.transcript || "").trim() && turnCount(chunk) === 0;
  if (!blank) return false;
  if (chunk.status === "pending") return !showPending;
  return !showQuiet;
}

export function visibleChunks(chunks: Chunk[], query: string, showQuiet: boolean, showPending = false): Chunk[] {
  const needle = query.trim().toLowerCase();
  return chunks.filter((chunk) => {
    if (isHiddenQuiet(chunk, showQuiet, showPending)) return false;
    const text = chunk.transcript || "";
    return !needle || text.toLowerCase().includes(needle) || (chunk.started_local || chunk.started || "").toLowerCase().includes(needle);
  });
}

export function transcriptState(chunk: Chunk): string {
  if (chunk.status === "pending") return "Processing transcript";
  if (chunk.status === "needs_attention" || (chunk.status && chunk.status !== "complete" && chunk.status !== "pending")) {
    return "Transcript processing needs attention";
  }
  if (!(chunk.transcript || "").trim() && turnCount(chunk) === 0) return "No speech detected";
  return "";
}

export function audioRetentionLabel(chunk: Chunk): string {
  if (!chunk.status || chunk.status === "pending" || chunk.status === "needs_attention" || chunk.status !== "complete") return "";
  return chunk.audio_playable ? "" : "Audio no longer retained";
}

export function speakerPendingLabel(chunk: Chunk): string {
  if (chunk.speaker_analysis_state) return 'Speaker analysis ' + chunk.speaker_analysis_state;
  if (chunk.status === 'pending' || chunk.status === 'needs_attention') return 'Speaker analysis blocked by transcription';
  const status = chunk.diarization_status || chunk.diarization?.outcome || "";
  if (status === 'running') return 'Speaker analysis running';
  if (status === 'failed' || status === 'error') return 'Speaker analysis failed';
  if (status === 'complete' || status === 'success') return 'Speaker analysis complete';
  if (chunk.voice_pending || status === 'pending') return 'Speaker analysis queued';
  return "";
}

export function audioStateLabel(chunk: Chunk): string {
  return [transcriptState(chunk), audioRetentionLabel(chunk), speakerPendingLabel(chunk)].filter(Boolean).join(" · ");
}

export function hourHeading(value: string | undefined): string {
  const match = (value || "").match(/(\d{4}-\d{2}-\d{2})[ T](\d{2}):\d{2}/);
  return match ? match[1] + " " + match[2] + ":00" : "Time unknown";
}

export function clockLabel(value: string | undefined): string {
  const match = (value || "").match(/(\d{2}:\d{2})/);
  return match?.[1] || value || "Recording";
}

export function highlightSegments(text: string, query: string): TextSegment[] {
  const source = text || "";
  const needle = query.trim();
  if (!source) return [];
  if (!needle) return [{ text: source, match: false }];
  const lower = source.toLowerCase();
  const want = needle.toLowerCase();
  const parts: TextSegment[] = [];
  let cursor = 0;
  while (cursor < source.length) {
    const at = lower.indexOf(want, cursor);
    if (at < 0) {
      parts.push({ text: source.slice(cursor), match: false });
      break;
    }
    if (at > cursor) parts.push({ text: source.slice(cursor, at), match: false });
    parts.push({ text: source.slice(at, at + want.length), match: true });
    cursor = at + want.length;
  }
  return parts.filter((part) => part.text.length > 0);
}

export function eventChunkId(part: SpeechEventChunk): string {
  return part.chunk_id || part.id || "";
}

export function eventTimeRange(event: SpeechEvent): string {
  const start = event.started_local || event.started || "Unknown start";
  const end = event.ended_local || event.ended || "unknown end";
  const endLabel = event.ended_local && start.slice(0, 10) === end.slice(0, 10) ? end.slice(11) : end;
  const playable = Number(event.playable_duration || event.duration || 0);
  return start + " – " + endLabel + (playable ? " · " + playable.toFixed(1) + "s playable" : "");
}

export function eventSources(event: SpeechEvent, chunks: Chunk[]): Array<{ id: string; label: string; present: boolean; started?: string }> {
  return (event.chunks || []).map((part) => {
    const id = eventChunkId(part);
    const chunk = chunks.find((item) => item.id === id);
    return {
      id,
      label: chunk?.started_local || chunk?.started || part.clip_started || (id ? "Recording " + id.slice(0, 8) : "Unknown recording"),
      present: !!chunk,
      started: chunk?.started || part.clip_started || undefined,
    };
  }).filter((item) => item.id);
}

export function derivedScopeCopy(sourceCount: number): string {
  if (sourceCount > 1) return "This speech-only clip includes " + sourceCount + " recordings. It is not an enhancement of only the selected recording.";
  if (sourceCount === 1) return "This speech-only clip comes from one source recording. Enhanced audio is a separate copy and does not share that recording's clock.";
  return "This speech-only clip has no source recording in the loaded day.";
}

export function relatedEvents(payload: DayPayload | null, chunkId: string | null): SpeechEvent[] {
  if (!payload || !chunkId) return [];
  return (payload.events || []).filter((event) => (event.chunks || []).some((part) => eventChunkId(part) === chunkId));
}

export function recordedLength(seconds: number): string {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  return total >= 60 ? Math.round(total / 60) + " min" : total + "s";
}

export function dayStatus(payload: DayPayload | null): string {
  if (!payload) return "";
  const issues: string[] = [];
  const counts = payload.day_processing || payload;
  if (counts.pending) issues.push(counts.pending + " pending");
  if (counts.errors) issues.push(counts.errors + " errors");
  const hold = Number(payload.activity_shadow?.hold_vad_positive || 0);
  if (hold > 0) issues.push(hold + " hold/VAD disagreement");
  return issues.join(" · ");
}

export function eventRange(block: DisplayBlock): string {
  const startLocal = block.started_local || "";
  const endLocal = block.ended_local || "";
  const sameDay = startLocal.slice(0, 10) && startLocal.slice(0, 10) === endLocal.slice(0, 10);
  if (sameDay && startLocal.length > 16 && endLocal.length > 16) {
    return startLocal.slice(11, 16) + "–" + endLocal.slice(11);
  }
  return startLocal + " – " + endLocal;
}

export type GroupSourceKind = "manual" | "topic" | "meeting" | "time";

export function groupSourceKind(block: DisplayBlock): GroupSourceKind {
  if (block.source === "manual") return "manual";
  if (block.source === "topic") return "topic";
  if (block.source === "meeting") return "meeting";
  return "time";
}

export function groupSourceLabel(block: DisplayBlock): string {
  const kind = groupSourceKind(block);
  if (kind === "manual") return "Saved group";
  if (kind === "topic") return "AI topic (inferred)";
  if (kind === "meeting") return "Meeting";
  return "Time group";
}

export function groupSourceDetail(block: DisplayBlock): string {
  const kind = groupSourceKind(block);
  if (kind === "topic") return "AI topic (inferred). Not a confirmed conversation.";
  if (kind === "time") return "Time group · no verified topic";
  return groupSourceLabel(block);
}

export function eventTitle(block: DisplayBlock): string {
  const title = (block.title || "").trim();
  if (title) return title;
  return groupSourceLabel(block);
}

export function summaryStatus(block: DisplayBlock): string {
  if (block.summary_state === "ready" && block.summary) return "";
  if (block.summary_state === "off") return "AI summary off";
  if (block.summary_state === "unavailable") return "Summary unavailable";
  return "Summary pending";
}

export function suggestionText(block: DisplayBlock): string {
  const parts = block.suggestion?.parts || [];
  if (!parts.length) return "";
  return parts
    .map((part) => {
      if (part.kind === "event") return (part.started_local || "") + " – " + (part.ended_local || "");
      return part.started_local || "Recording";
    })
    .join("; ");
}

export type ListOptions = {
  query: string;
  showQuiet: boolean;
  showPending?: boolean;
  groups?: boolean;
};

function byStart(left: Chunk, right: Chunk): number {
  return String(left.started || left.started_local || left.id).localeCompare(String(right.started || right.started_local || right.id));
}

function chunkMatches(chunk: Chunk, needle: string): boolean {
  if (!needle) return false;
  return (chunk.transcript || "").toLowerCase().includes(needle) || (chunk.started_local || chunk.started || "").toLowerCase().includes(needle);
}

export function groupMembers(payload: DayPayload, block: DisplayBlock, showQuiet: boolean, showPending: boolean): Chunk[] {
  const byId = new Map((payload.chunks || []).map((chunk) => [chunk.id, chunk]));
  return (block.chunk_ids || [])
    .map((id) => byId.get(id))
    .filter((chunk): chunk is Chunk => !!chunk && !isHiddenQuiet(chunk, showQuiet, showPending))
    .sort(byStart);
}

export function isEventBlock(block: DisplayBlock): boolean {
  return block.kind === "event" || (!block.kind && !block.chunk_id && (block.chunk_ids || []).length > 0);
}

export function qualifiesAsGroup(block: DisplayBlock, members: Chunk[]): boolean {
  return isEventBlock(block) && members.length > 0;
}

export function buildRecordingList(payload: DayPayload | null, options: ListOptions): ListRow[] {
  if (!payload) return [];
  const showPending = !!options.showPending;
  if (options.groups) {
    const needle = options.query.trim().toLowerCase();
    const rows: ListRow[] = [];
    const byId = new Map((payload.chunks || []).map((chunk) => [chunk.id, chunk]));
    for (const block of payload.display_blocks || []) {
      if (!isEventBlock(block)) {
        const chunkId = block.chunk_id || "";
        const chunk = chunkId ? byId.get(chunkId) : undefined;
        if (!chunk || isHiddenQuiet(chunk, options.showQuiet, showPending)) continue;
        if (needle && !chunkMatches(chunk, needle)) continue;
        rows.push({ type: "chunk", key: chunk.id, chunk });
        continue;
      }
      const members = groupMembers(payload, block, options.showQuiet, showPending);
      if (!qualifiesAsGroup(block, members)) continue;
      const match = members.find((chunk) => chunkMatches(chunk, needle)) || null;
      const titleHit = !!needle && [block.title, block.preview].some((value) => (value || "").toLowerCase().includes(needle));
      if (needle && !match && !titleHit) continue;
      const id = block.id || (block.chunk_ids || []).join("-");
      rows.push({ type: "group", key: id, block: { ...block, id }, members, matchId: match?.id || null });
    }
    return rows;
  }
  const chunks = visibleChunks(payload.chunks || [], options.query, options.showQuiet, showPending).slice().sort(byStart);
  const rows: ListRow[] = [];
  let heading = "";
  for (const chunk of chunks) {
    const label = hourHeading(chunk.started_local || chunk.started);
    if (label !== heading) {
      heading = label;
      rows.push({ type: "heading", key: "heading-" + label, label });
    }
    rows.push({ type: "chunk", key: chunk.id, chunk });
  }
  return rows;
}

export function hasPlayableRows(rows: ListRow[]): boolean {
  return rows.some((row) => row.type === "chunk" || row.type === "group");
}

export function firstSearchTarget(rows: ListRow[]): { id: string; kind: "chunk" | "group" } | null {
  for (const row of rows) {
    if (row.type === "chunk") return { id: row.chunk.id, kind: "chunk" };
    if (row.type === "group") return { id: row.key, kind: "group" };
  }
  return null;
}

export function formatTime(value: number): string {
  const seconds = Math.max(0, Number(value) || 0);
  const whole = Math.floor(seconds);
  const minutes = Math.floor(whole / 60);
  const rest = whole % 60;
  return minutes + ":" + String(rest).padStart(2, "0");
}

export function groupBounds(group: TurnGroup): { start: number; end: number } {
  const starts = group.turns.map((turn) => Number(turn.started)).filter((value) => Number.isFinite(value));
  const ends = group.turns.map((turn) => Number(turn.ended)).filter((value) => Number.isFinite(value));
  return {
    start: starts.length ? Math.min(...starts) : 0,
    end: ends.length ? Math.max(...ends) : 0,
  };
}

export function groupTranscript(group: TurnGroup): string {
  return group.turns.map((turn) => (turn.text || "").trim()).filter(Boolean).join(" ");
}

function gapHasOtherSpeaker(turns: SpeakerTurn[], left: SpeakerTurn, right: SpeakerTurn): boolean {
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

function canMergeTurns(currentTurns: SpeakerTurn[], next: SpeakerTurn, all: SpeakerTurn[]): boolean {
  const previous = currentTurns[currentTurns.length - 1];
  if (!previous || !next) return false;
  if ((previous.run_id || "") !== (next.run_id || "")) return false;
  if ((previous.speaker_key || "Unknown") !== (next.speaker_key || "Unknown")) return false;
  const people = new Set<string>();
  for (const turn of currentTurns.concat([next])) {
    if (turn.person_id) people.add(turn.person_id);
  }
  if (people.size > 1) return false;
  return !gapHasOtherSpeaker(all, previous, next);
}

export function groupTurns(chunk: Chunk): TurnGroup[] {
  const turns = (chunk.speakers || []).slice().sort((left, right) => {
    const start = (Number(left.started) || 0) - (Number(right.started) || 0);
    if (start) return start;
    const end = (Number(left.ended) || 0) - (Number(right.ended) || 0);
    if (end) return end;
    return String(left.id || "").localeCompare(String(right.id || ""));
  });
  const groups: TurnGroup[] = [];
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

export function briefStretch(group: TurnGroup): boolean {
  const bounds = groupBounds(group);
  const duration = bounds.end - bounds.start;
  return Number.isFinite(duration) && duration > 0 && duration < 1;
}

export function briefDisclosureLabel(count: number): string {
  return count === 1 ? "1 brief turn · under 1s" : count + " brief turns · under 1s each";
}

export function groupLabel(group: TurnGroup): { label: string; tone: string } {
  const turns = group.turns || [];
  const key = group.speaker_key || turns[0]?.speaker_key || "S1";
  if (!turns.length) return { label: "Unknown · No speaker turns available", tone: "unknown" };
  const confirmedIds = new Set(turns.filter((turn) => turn.person_id).map((turn) => turn.person_id));
  if (confirmedIds.size > 1) return { label: "Mixed labels", tone: "unknown" };
  const named = turns.find((item) => item.name);
  if (confirmedIds.size === 1 && named) {
    const complete = turns.every((turn) => turn.person_id === named.person_id);
    if (turns.every((turn) => turn.preserved)) return { label: named.name + " · Earlier label", tone: "preserved" };
    if (complete && turns.every((turn) => turn.label_source === "automatic")) return { label: named.name + " · Auto", tone: "confirmed" };
    if (complete) return { label: named.name + " ✓", tone: "confirmed" };
    return { label: named.name || "Named", tone: "confirmed" };
  }
  if (turns.every((turn) => turn.preserved)) return { label: "Earlier label", tone: "preserved" };
  const suggested = turns.find((turn) => turn.suggested_name);
  if (suggested && turns.every((turn) => !turn.suggested_name || turn.suggested_name === suggested.suggested_name)) {
    return { label: "Possibly " + suggested.suggested_name, tone: "suggested" };
  }
  return { label: "Unknown · " + key, tone: "unknown" };
}

export function processingBits(chunk: Chunk): string {
  const diar = chunk.diarization || {};
  const bits: string[] = [];
  const outcome = diar.outcome || chunk.diarization_status;
  if (outcome) bits.push(outcome);
  if (Number.isFinite(Number(diar.speech_seconds))) bits.push(Number(diar.speech_seconds).toFixed(1) + "s speech");
  if (Number.isFinite(Number(diar.coverage))) bits.push(Math.round(Number(diar.coverage) * 100) + "% coverage");
  if (diar.turn_count != null) bits.push(diar.turn_count + " turns");
  if (diar.embedding_count != null) bits.push(diar.embedding_count + " embeddings");
  if (diar.cluster_count != null) bits.push(diar.cluster_count + " clusters");
  return bits.join(" · ") || "No processing details yet.";
}

export function cleanSeconds(group: TurnGroup): number {
  return group.turns.reduce((sum, turn) => sum + (Number(turn.clean_seconds) || 0), 0);
}

export function selectionStillPresent(payload: DayPayload, kind: "chunk" | "event" | "group", id: string | null): boolean {
  if (!id) return false;
  if (kind === "event") return (payload.events || []).some((event) => event.id === id);
  if (kind === "group") {
    return (payload.display_blocks || []).some((block) => {
      if (block.kind !== "event") return false;
      const key = block.id || (block.chunk_ids || []).join("-");
      return key === id;
    });
  }
  return (payload.chunks || []).some((chunk) => chunk.id === id);
}

export function toPlayableChunk(chunk: Chunk) {
  return {
    id: chunk.id,
    kind: "chunk" as const,
    audioPlayable: !!chunk.audio_playable,
    enhancedPlayable: false,
    label: chunk.started_local || chunk.started || "Recording",
    pinned: !!chunk.audio_pinned,
  };
}

export function toPlayableEvent(event: SpeechEvent) {
  return {
    id: event.id,
    kind: "event" as const,
    audioPlayable: !!event.audio_playable,
    enhancedPlayable: !!event.enhanced_playable,
    label: event.started_local || "Speech event",
    pinned: false,
  };
}
