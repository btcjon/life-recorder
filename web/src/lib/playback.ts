export type AudioSource = "original" | "enhanced";

export type PlayableItem = {
  id: string;
  kind: "chunk" | "event";
  audioPlayable: boolean;
  enhancedPlayable: boolean;
  label: string;
  pinned: boolean;
  preferredSource?: AudioSource;
};

export type PlaybackState = {
  item: PlayableItem | null;
  source: AudioSource;
  loadedId: string | null;
  clipKey: string | null;
  clipStart: number | null;
  clipStop: number | null;
  pendingSpan: SpanIntent | null;
  generation: number;
  objectUrl: string | null;
  error: string | null;
};

export type SpanIntent = {
  key: string;
  start: number;
  end: number;
  expectedId: string;
  fromStart: boolean;
};

export type PlaybackFetch = {
  path: string;
  id: string;
  generation: number;
};

export type PlaybackResult = {
  state: PlaybackState;
  revoke: string[];
  fetch: PlaybackFetch | null;
};

export function initialPlaybackState(): PlaybackState {
  return {
    item: null,
    source: "original",
    loadedId: null,
    clipKey: null,
    clipStart: null,
    clipStop: null,
    pendingSpan: null,
    generation: 0,
    objectUrl: null,
    error: null,
  };
}

export function effectiveSource(item: PlayableItem | null, source: AudioSource): AudioSource {
  if (!item) return source;
  if (item.kind === "event" && source === "enhanced" && !item.enhancedPlayable) return "original";
  return source;
}

export function audioPath(item: PlayableItem, source: AudioSource): string | null {
  if (!item.audioPlayable) return null;
  if (item.kind === "event") {
    const kind = effectiveSource(item, source) === "enhanced" ? "enhanced" : "original";
    return `/v1/events/${encodeURIComponent(item.id)}/audio?kind=${kind}`;
  }
  return `/v1/audio/${encodeURIComponent(item.id)}`;
}

export function audioIdentity(item: PlayableItem, source: AudioSource): string | null {
  if (!audioPath(item, source)) return null;
  if (item.kind === "event") {
    const kind = effectiveSource(item, source) === "enhanced" ? "enhanced" : "original";
    return `event:${item.id}:${kind}`;
  }
  return `chunk:${item.id}:original`;
}

function clearedClip(state: PlaybackState): PlaybackState {
  return { ...state, clipKey: null, clipStart: null, clipStop: null };
}

function release(state: PlaybackState): { state: PlaybackState; revoke: string[] } {
  const revoke = state.objectUrl ? [state.objectUrl] : [];
  return {
    revoke,
    state: { ...state, objectUrl: null },
  };
}

export function reducePlayback(
  state: PlaybackState,
  event:
    | { type: "select"; item: PlayableItem | null }
    | { type: "setSource"; source: AudioSource }
    | { type: "playSpan"; key: string; start: number; end: number; expectedId: string }
    | { type: "queueSpan"; key: string; start: number; end: number; expectedId: string; fromStart: boolean }
    | { type: "clearSpan" }
    | { type: "blobLoaded"; requestId: string; generation: number; url: string }
    | { type: "blobFailed"; requestId: string; generation: number; aborted: boolean }
    | { type: "release" },
): PlaybackResult {
  if (event.type === "release") {
    const released = release(state);
    return { state: { ...released.state, loadedId: null, generation: state.generation + 1 }, revoke: released.revoke, fetch: null };
  }
  if (event.type === "setSource") {
    return reducePlayback({ ...state, source: event.source }, { type: "select", item: state.item });
  }
  if (event.type === "queueSpan") {
    const intent: SpanIntent = {
      key: event.key,
      start: event.start,
      end: event.end,
      expectedId: event.expectedId,
      fromStart: event.fromStart,
    };
    if (state.loadedId === event.expectedId) {
      return {
        state: { ...state, pendingSpan: intent, clipKey: event.key, clipStart: event.start, clipStop: event.end },
        revoke: [],
        fetch: null,
      };
    }
    return { state: { ...state, pendingSpan: intent }, revoke: [], fetch: null };
  }
  if (event.type === "playSpan") {
    if (state.loadedId !== event.expectedId) return { state, revoke: [], fetch: null };
    return {
      state: { ...state, clipKey: event.key, clipStart: event.start, clipStop: event.end, pendingSpan: null },
      revoke: [],
      fetch: null,
    };
  }
  if (event.type === "clearSpan") {
    return { state: { ...clearedClip(state), pendingSpan: null }, revoke: [], fetch: null };
  }
  if (event.type === "blobLoaded") {
    if (state.loadedId !== event.requestId || state.generation !== event.generation) {
      return { state, revoke: [event.url], fetch: null };
    }
    const revoke = state.objectUrl && state.objectUrl !== event.url ? [state.objectUrl] : [];
    return { state: { ...state, objectUrl: event.url, error: null }, revoke, fetch: null };
  }
  if (event.type === "blobFailed") {
    if (event.aborted || state.loadedId !== event.requestId || state.generation !== event.generation) {
      return { state, revoke: [], fetch: null };
    }
    return {
      state: { ...state, loadedId: null, pendingSpan: null, error: "Audio could not be loaded · Refresh to retry" },
      revoke: [],
      fetch: null,
    };
  }
  const item = event.item;
  const source = effectiveSource(item, state.source);
  const nextId = item ? audioIdentity(item, source) : null;
  const path = item ? audioPath(item, source) : null;
  const pending = state.pendingSpan && nextId && state.pendingSpan.expectedId === nextId ? state.pendingSpan : null;
  const clip = pending
    ? { clipKey: pending.key, clipStart: pending.start, clipStop: pending.end, pendingSpan: pending }
    : { pendingSpan: null as SpanIntent | null };
  if (nextId && nextId === state.loadedId) {
    return { state: { ...state, item, source, error: null, ...clip }, revoke: [], fetch: null };
  }
  const released = release(clearedClip(state));
  const generation = state.generation + 1;
  const next: PlaybackState = {
    ...released.state,
    item,
    source,
    loadedId: nextId,
    generation,
    error: null,
    ...clip,
  };
  if (!path || !nextId) {
    return { state: next, revoke: released.revoke, fetch: null };
  }
  return { state: next, revoke: released.revoke, fetch: { path, id: nextId, generation } };
}

export function nowPlayingLabel(item: PlayableItem | null, error: string | null): string {
  if (error) return error;
  if (!item) return "Nothing playing";
  return item.label + (item.audioPlayable ? "" : " · audio unavailable");
}

export function playbackCaption(
  item: PlayableItem | null,
  error: string | null,
  selection: { key: string; label: string } | null,
): { primary: string; secondary: string } {
  const playing = nowPlayingLabel(item, error);
  const playingKey = item ? item.kind + ":" + item.id : "";
  const differs = !!selection && selection.key !== playingKey;
  if (!differs) return { primary: item ? playing : "Nothing playing", secondary: "" };
  return {
    primary: item ? "Now playing: " + playing : "Nothing playing",
    secondary: "Selected: " + selection.label,
  };
}
