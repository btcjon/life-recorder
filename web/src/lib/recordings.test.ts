import { describe, expect, it } from "vitest";
import type { Chunk, DayPayload } from "../api/types";
import { audioStateLabel, briefStretch, buildRecordingList, derivedScopeCopy, eventSources, firstSearchTarget, groupTurns, hasPlayableRows, highlightSegments, isHiddenQuiet, relatedEvents, visibleChunks } from "./recordings";

function chunk(partial: Partial<Chunk> & { id: string }): Chunk {
  return { status: "complete", transcript: "", audio_playable: true, ...partial };
}

describe("recordings visibility", () => {
  it("hides quiet and pending blanks unless show quiet is on, and always keeps attention", () => {
    const quiet = chunk({ id: "quiet", status: "complete", transcript: "   ", speaker_turn_count: 0 });
    const pending = chunk({ id: "pending", status: "pending", transcript: "", audio_playable: false });
    const attention = chunk({ id: "attention", status: "needs_attention", transcript: "", audio_playable: false });
    const spoken = chunk({ id: "spoken", transcript: "Hello there" });
    expect(isHiddenQuiet(quiet, false)).toBe(true);
    expect(isHiddenQuiet(pending, false)).toBe(true);
    expect(isHiddenQuiet(attention, false)).toBe(false);
    expect(visibleChunks([quiet, pending, attention, spoken], "", false).map((item) => item.id)).toEqual(["attention", "spoken"]);
    expect(visibleChunks([quiet, pending], "", true)).toHaveLength(1);
    expect(visibleChunks([quiet, pending], "", true, true)).toHaveLength(2);
    expect(audioStateLabel(pending)).toBe("Processing transcript");
    expect(audioStateLabel(attention)).toBe("Transcript processing needs attention");
    expect(audioStateLabel(chunk({ id: "gone", audio_playable: false, transcript: "Text is kept" }))).toBe("Audio no longer retained");
  });

  it("shows an empty match set and does not invent event groups", () => {
    const payload: DayPayload = {
      chunks: [chunk({ id: "a", transcript: "alpha", started_local: "2026-10-04 09:00" }), chunk({ id: "b", transcript: "beta" })],
      display_blocks: [
        { kind: "chunk", chunk_id: "a" },
        { kind: "chunk", chunk_id: "b" },
      ],
    };
    const rows = buildRecordingList(payload, { query: "missing", showQuiet: true });
    expect(hasPlayableRows(rows)).toBe(false);
    const provided = buildRecordingList(payload, { query: "", showQuiet: true });
    expect(provided.filter((row) => row.type === "group")).toHaveLength(0);
    expect(provided.filter((row) => row.type === "chunk")).toHaveLength(2);
  });

  it("keeps the flat list explicit and does not turn speech events into sidebar rows", () => {
    const payload: DayPayload = {
      chunks: [
        chunk({ id: "a", transcript: "one", duration: 30 }),
        chunk({ id: "b", transcript: "two", duration: 30 }),
      ],
      display_blocks: [{ kind: "event", id: "evt", chunk_ids: ["a", "b"], source: "suggested", started_local: "2026-10-04 09:00:00", ended_local: "2026-10-04 09:02:00" }],
      events: [{ id: "speech-1", started_local: "2026-10-04 09:01", audio_playable: false, enhanced_playable: false }],
    };
    const grouped = buildRecordingList(payload, { query: "", showQuiet: false, groups: true });
    expect(grouped.some((row) => row.type === "group")).toBe(true);
    expect(grouped.some((row) => row.type === "chunk")).toBe(false);
    const flat = buildRecordingList(payload, { query: "", showQuiet: false });
    expect(flat.filter((row) => row.type === "chunk")).toHaveLength(2);
    expect(flat.some((row) => row.type === "group")).toBe(false);
    expect(grouped.map(row => row.key)).toEqual(["evt"]);
  });

  it("keeps topic singletons and uncovered chunk blocks, including quiet and pending when asked", () => {
    const payload: DayPayload = {
      chunks: [
        chunk({ id: "topic-only", transcript: "solo topic", started_local: "2026-10-04 09:00" }),
        chunk({ id: "quiet", status: "complete", transcript: "", speaker_turn_count: 0, started_local: "2026-10-04 09:10" }),
        chunk({ id: "pending", status: "pending", transcript: "", audio_playable: false, started_local: "2026-10-04 09:11" }),
        chunk({ id: "loose", transcript: "uncovered speech", started_local: "2026-10-04 09:12" }),
      ],
      display_blocks: [
        { kind: "event", id: "solo", source: "topic", title: "Solo topic", chunk_ids: ["topic-only"] },
        { kind: "chunk", chunk_id: "quiet" },
        { kind: "chunk", chunk_id: "pending" },
        { kind: "chunk", chunk_id: "loose" },
      ],
    };
    const shown = buildRecordingList(payload, { query: "", showQuiet: true, showPending: true, groups: true });
    expect(shown.map((row) => row.key)).toEqual(["solo", "quiet", "pending", "loose"]);
    expect(shown[0]).toMatchObject({ type: "group", block: { title: "Solo topic", source: "topic" } });
    const filtered = buildRecordingList(payload, { query: "", showQuiet: false, showPending: false, groups: true });
    expect(filtered.map((row) => row.key)).toEqual(["solo", "loose"]);
    const titled = buildRecordingList(payload, { query: "Solo topic", showQuiet: false, groups: true });
    expect(titled.map((row) => row.key)).toEqual(["solo"]);
    const passage = buildRecordingList(payload, { query: "uncovered", showQuiet: false, groups: true });
    expect(passage.map((row) => row.key)).toEqual(["loose"]);
  });

  it("preserves saved group IDs and picks the actual matching source text", () => {
    const payload: DayPayload = {
      chunks: [chunk({ id: "a", transcript: "first" }), chunk({ id: "b", transcript: "matching passage" })],
      display_blocks: [{ id: "saved", source: "manual", title: "Keep this title", kind: "event", chunk_ids: ["a", "b"] }],
    };
    const rows = buildRecordingList(payload, { groups: true, query: "matching", showQuiet: false });
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ key: "saved", matchId: "b", block: { title: "Keep this title" } });
    expect(firstSearchTarget(rows)).toEqual({ id: "saved", kind: "group" });
  });

  it("highlights literal matching text without changing or interpreting HTML", () => {
    const text = "Before <img onerror=unsafe> MATCH after match.";
    const segments = highlightSegments(text, "match");
    expect(segments.map(part => part.text).join("")).toBe(text);
    expect(segments.filter(part => part.match).map(part => part.text)).toEqual(["MATCH", "match"]);
    expect(highlightSegments(text, "[").every(part => !part.match)).toBe(true);
  });

  it("uses real chunk_id source links, including cross-day sources, rather than playback offsets", () => {
    const event = { id: "event", chunks: [{ chunk_id: "a", start: 42, end: 48 }, { chunk_id: "b", start: 0, end: 4, clip_started: "2026-10-05T04:00:00Z" }] };
    const sources = eventSources(event, [chunk({ id: "a", started: "2026-10-05T03:59:00Z" })]);
    expect(sources.map(source => source.id)).toEqual(["a", "b"]);
    expect(sources[1]).toMatchObject({ present: false, started: "2026-10-05T04:00:00Z" });
    expect(relatedEvents({ events: [event] }, "a")).toEqual([event]);
    expect(derivedScopeCopy(sources.length)).toContain("includes 2 recordings");
  });

  it("keeps sub-second turns brief and does not invent word timing", () => {
    const item = chunk({
      id: "clip",
      speakers: [
        { id: "t1", started: 1, ended: 1.4, text: "hi", speaker_key: "S1", run_id: "r" },
        { id: "t2", started: 4, ended: 6, text: "longer", speaker_key: "S2", run_id: "r" },
      ],
    });
    const groups = groupTurns(item);
    expect(groups).toHaveLength(2);
    expect(briefStretch(groups[0]!)).toBe(true);
    expect(briefStretch(groups[1]!)).toBe(false);
    expect(groups[0]).not.toHaveProperty("words");
  });
});
