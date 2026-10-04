import { describe, expect, it } from "vitest";
import type { Chunk, DayPayload } from "../api/types";
import { audioStateLabel, briefStretch, buildRecordingList, groupTurns, hasPlayableRows, isHiddenQuiet, visibleChunks } from "./recordings";

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
    expect(visibleChunks([quiet, pending], "", true)).toHaveLength(2);
    expect(audioStateLabel(pending)).toBe("Processing");
    expect(audioStateLabel(attention)).toBe("Processing needs attention");
    expect(audioStateLabel(chunk({ id: "gone", audio_playable: false }))).toBe("Audio not kept");
  });

  it("shows an empty match set and does not invent event groups", () => {
    const payload: DayPayload = {
      chunks: [chunk({ id: "a", transcript: "alpha", started_local: "2026-10-04 09:00" }), chunk({ id: "b", transcript: "beta" })],
      display_blocks: [
        { kind: "chunk", chunk_id: "a" },
        { kind: "chunk", chunk_id: "b" },
      ],
    };
    const rows = buildRecordingList(payload, { query: "missing", showQuiet: true, flat: false, expanded: new Set() });
    expect(hasPlayableRows(rows)).toBe(false);
    const provided = buildRecordingList(payload, { query: "", showQuiet: true, flat: false, expanded: new Set() });
    expect(provided.filter((row) => row.type === "event")).toHaveLength(0);
    expect(provided.filter((row) => row.type === "chunk")).toHaveLength(2);
  });

  it("uses server event groups unless flat view is requested", () => {
    const payload: DayPayload = {
      chunks: [
        chunk({ id: "a", transcript: "one", duration: 30 }),
        chunk({ id: "b", transcript: "two", duration: 30 }),
      ],
      display_blocks: [{ kind: "event", id: "evt", chunk_ids: ["a", "b"], source: "suggested", started_local: "2026-10-04 09:00:00", ended_local: "2026-10-04 09:02:00" }],
      events: [{ id: "speech-1", started_local: "2026-10-04 09:01", audio_playable: false, enhanced_playable: false }],
    };
    const grouped = buildRecordingList(payload, { query: "", showQuiet: false, flat: false, expanded: new Set() });
    expect(grouped.some((row) => row.type === "event")).toBe(true);
    expect(grouped.some((row) => row.type === "chunk")).toBe(false);
    const flat = buildRecordingList(payload, { query: "", showQuiet: false, flat: true, expanded: new Set() });
    expect(flat.filter((row) => row.type === "chunk")).toHaveLength(2);
    expect(flat.some((row) => row.type === "event")).toBe(false);
    expect(grouped.some((row) => row.type === "speech" && row.event.id === "speech-1")).toBe(true);
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
