import { describe, expect, it } from "vitest";
import { audioIdentity, initialPlaybackState, reducePlayback, type PlayableItem } from "./playback";

const chunkA: PlayableItem = {
  id: "a",
  kind: "chunk",
  audioPlayable: true,
  enhancedPlayable: false,
  label: "Recording A",
  pinned: false,
};
const eventEnhanced: PlayableItem = {
  id: "e1",
  kind: "event",
  audioPlayable: true,
  enhancedPlayable: true,
  label: "Speech event",
  pinned: false,
};
const eventPlain: PlayableItem = { ...eventEnhanced, id: "e2", enhancedPlayable: false };
const missing: PlayableItem = { ...chunkA, id: "gone", audioPlayable: false, label: "Missing" };

describe("playback navigation", () => {
  it("defaults to original and does not reload the same audio", () => {
    const selected = reducePlayback(initialPlaybackState(), { type: "select", item: chunkA });
    expect(selected.state.source).toBe("original");
    expect(selected.fetch?.path).toBe("/v1/audio/a");
    expect(selected.state.loadedId).toBe("chunk:a:original");
    const again = reducePlayback(selected.state, { type: "select", item: { ...chunkA, label: "Recording A" } });
    expect(again.fetch).toBeNull();
    expect(again.revoke).toEqual([]);
    expect(again.state.generation).toBe(selected.state.generation);
  });

  it("switches source only when the user asks, and keeps that choice across events", () => {
    const selected = reducePlayback(initialPlaybackState(), { type: "select", item: eventEnhanced });
    expect(selected.state.source).toBe("original");
    const enhanced = reducePlayback(selected.state, { type: "setSource", source: "enhanced" });
    expect(enhanced.fetch?.path).toContain("kind=enhanced");
    expect(enhanced.state.source).toBe("enhanced");
    const next = reducePlayback(enhanced.state, { type: "select", item: { ...eventEnhanced, id: "e9" } });
    expect(next.state.source).toBe("enhanced");
    expect(next.fetch?.id).toBe("event:e9:enhanced");
    const span = reducePlayback(next.state, { type: "playSpan", key: "g", start: 1, end: 2, expectedId: "event:e9:enhanced" });
    expect(span.state.source).toBe("enhanced");
    expect(span.fetch).toBeNull();
    expect(span.state.clipStart).toBe(1);
  });

  it("does not silently keep enhanced when that copy is unavailable", () => {
    const enhanced = reducePlayback(initialPlaybackState(), { type: "setSource", source: "enhanced" });
    const selected = reducePlayback({ ...enhanced.state, item: eventEnhanced, source: "enhanced" }, { type: "select", item: eventPlain });
    expect(selected.state.source).toBe("original");
    expect(audioIdentity(eventPlain, "enhanced")).toBe("event:e2:original");
  });

  it("ignores a stale blob and releases the unused object URL", () => {
    const selected = reducePlayback(initialPlaybackState(), { type: "select", item: chunkA });
    const loaded = reducePlayback(selected.state, { type: "blobLoaded", requestId: "chunk:a:original", generation: selected.state.generation, url: "blob:current" });
    const stale = reducePlayback(loaded.state, { type: "blobLoaded", requestId: "chunk:old", generation: 0, url: "blob:stale" });
    expect(stale.state.objectUrl).toBe("blob:current");
    expect(stale.revoke).toEqual(["blob:stale"]);
  });

  it("queues a span until the selected source is ready and drops it when the source changes", () => {
    const queued = reducePlayback(initialPlaybackState(), {
      type: "queueSpan",
      key: "turn-1",
      start: 3,
      end: 8,
      expectedId: "chunk:a:original",
      fromStart: true,
    });
    expect(queued.state.loadedId).toBeNull();
    expect(queued.state.pendingSpan?.key).toBe("turn-1");
    expect(queued.state.clipStart).toBeNull();
    const selected = reducePlayback(queued.state, { type: "select", item: chunkA });
    expect(selected.state.loadedId).toBe("chunk:a:original");
    expect(selected.state.pendingSpan?.expectedId).toBe("chunk:a:original");
    expect(selected.fetch?.path).toBe("/v1/audio/a");
    const loaded = reducePlayback(selected.state, {
      type: "blobLoaded",
      requestId: "chunk:a:original",
      generation: selected.state.generation,
      url: "blob:ready",
    });
    expect(loaded.state.pendingSpan?.key).toBe("turn-1");
    expect(loaded.state.objectUrl).toBe("blob:ready");
    const stale = reducePlayback(loaded.state, { type: "setSource", source: "enhanced" });
    const other = reducePlayback(stale.state, { type: "select", item: eventEnhanced });
    expect(other.state.pendingSpan).toBeNull();
    expect(other.state.clipKey).toBeNull();
  });

  it("does not fetch missing audio and clears span when the file changes", () => {
    const first = reducePlayback(initialPlaybackState(), { type: "select", item: chunkA });
    const spanned = reducePlayback(first.state, { type: "playSpan", key: "g", start: 2, end: 4, expectedId: first.state.loadedId || "" });
    expect(spanned.state.clipKey).toBe("g");
    const next = reducePlayback(spanned.state, { type: "select", item: missing });
    expect(next.fetch).toBeNull();
    expect(next.state.loadedId).toBeNull();
    expect(next.state.clipKey).toBeNull();
    expect(next.state.error).toBeNull();
  });
});
