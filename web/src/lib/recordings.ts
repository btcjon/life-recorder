import type { Chunk, DayPayload, DisplayBlock, SpeakerTurn, SpeechEvent } from "../api/types";

export type TurnGroup = {
  key: string;
  chunk: Chunk;
  run_id?: string;
  speaker_key?: string;
  turns: SpeakerTurn[];
  preserved: boolean;
};

export type ListRow =
  | { type: "interval"; key: string; label: string; when: string; manual: boolean }
  | { type: "session"; key: string; text: string }
  | { type: "event"; key: string; block: DisplayBlock; members: Chunk[]; open: boolean }
  | { type: "chunk"; key: string; chunk: Chunk }
  | { type: "speech"; key: string; event: SpeechEvent };

export function preview(text: string | undefined): string {
  const clean = (text || "").replace(/\s+/g, " ").trim();
  return clean ? clean.slice(0, 90) + (clean.length > 90 ? "…" : "") : "No transcript yet";
}

export function turnCount(chunk: Chunk): number {
  const turns = chunk.speakers || [];
  return Number(chunk.speaker_turn_count || 0) || turns.length;
}

export function isHiddenQuiet(chunk: Chunk, showQuiet: boolean): boolean {
  if (showQuiet) return false;
  const text = chunk.transcript || "";
  if (chunk.status === "needs_attention") return false;
  return !text.trim() && turnCount(chunk) === 0;
}

export function visibleChunks(chunks: Chunk[], query: string, showQuiet: boolean): Chunk[] {
  const needle = query.toLowerCase();
  return chunks.filter((chunk) => {
    if (isHiddenQuiet(chunk, showQuiet)) return false;
    const text = chunk.transcript || "";
    return !needle || text.toLowerCase().includes(needle) || (chunk.started_local || "").toLowerCase().includes(needle);
  });
}

export function audioStateLabel(chunk: Chunk): string {
  if (chunk.status === "pending") return "Processing";
  if (chunk.status === "needs_attention") return "Processing needs attention";
  if (chunk.status !== "complete") return "Processing failed";
  if (!chunk.audio_playable) return "Audio not kept";
  return "";
}

export function recordedLength(seconds: number): string {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  return total >= 60 ? Math.round(total / 60) + " min" : total + "s";
}

export function dayStatus(payload: DayPayload | null): string {
  if (!payload) return "";
  const issues: string[] = [];
  if (payload.pending) issues.push(payload.pending + " pending");
  if (payload.errors) issues.push(payload.errors + " errors");
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

export function eventTitle(block: DisplayBlock): string {
  if (block.source === "manual" && block.title) return block.title;
  return "Event · " + eventRange(block);
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

export function buildRecordingList(
  payload: DayPayload | null,
  options: { query: string; showQuiet: boolean; flat: boolean; expanded: ReadonlySet<string> },
): ListRow[] {
  if (!payload) return [];
  const rows: ListRow[] = [];
  (payload.intervals || []).forEach((item, index) => {
    rows.push({
      type: "interval",
      key: "interval-" + index,
      label: item.label || (item.source === "manual" ? "Manual meeting" : "Possible event"),
      when: (item.started_at || "") + " -> " + (item.ended_at || "open"),
      manual: item.source === "manual",
    });
  });
  (payload.sessions || []).forEach((session, index) => {
    rows.push({
      type: "session",
      key: "session-" + index,
      text: (session.title || "Session") + " · " + (session.started || ""),
    });
  });
  const chunks = visibleChunks(payload.chunks || [], options.query, options.showQuiet);
  const visible = new Set(chunks.map((chunk) => chunk.id));
  const byId = new Map((payload.chunks || []).map((chunk) => [chunk.id, chunk]));
  const rendered = new Set<string>();
  const blocks: DisplayBlock[] = options.flat
    ? chunks.map((chunk) => ({ kind: "chunk", chunk_id: chunk.id }))
    : payload.display_blocks || chunks.map((chunk) => ({ kind: "chunk", chunk_id: chunk.id }));
  const pushChunk = (chunk: Chunk | undefined) => {
    if (!chunk || rendered.has(chunk.id) || !visible.has(chunk.id)) return;
    rendered.add(chunk.id);
    rows.push({ type: "chunk", key: chunk.id, chunk });
  };
  for (const block of blocks) {
    if (!options.flat && block.kind === "event") {
      const members = (block.chunk_ids || []).map((id) => byId.get(id)).filter((chunk): chunk is Chunk => !!chunk && visible.has(chunk.id));
      const named = members.filter((chunk) => (chunk.transcript || "").trim());
      const manual = block.source === "manual";
      if ((manual && members.length) || named.length >= 2) {
        const id = block.id || members.map((chunk) => chunk.id).join("-");
        rows.push({ type: "event", key: id, block: { ...block, id }, members, open: options.expanded.has(id) });
        for (const chunk of members) rendered.add(chunk.id);
        continue;
      }
      for (const chunk of members) pushChunk(chunk);
      continue;
    }
    pushChunk(byId.get(block.chunk_id || ""));
  }
  const needle = options.query.toLowerCase();
  for (const event of payload.events || []) {
    const label = event.started_local || event.id;
    if (needle && !label.toLowerCase().includes(needle) && !event.id.toLowerCase().includes(needle)) continue;
    rows.push({ type: "speech", key: "speech-" + event.id, event });
  }
  return rows;
}

export function hasPlayableRows(rows: ListRow[]): boolean {
  return rows.some((row) => row.type === "chunk" || row.type === "event" || row.type === "speech");
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

export function selectionStillPresent(payload: DayPayload, kind: "chunk" | "event", id: string | null): boolean {
  if (!id) return false;
  if (kind === "event") return (payload.events || []).some((event) => event.id === id);
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
