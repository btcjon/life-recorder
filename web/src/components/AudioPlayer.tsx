import { Button } from "@/components/ui/button";
import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import { isAbortError, type ApiClient } from "../api/client";
import {
  audioIdentity,
  initialPlaybackState,
  nowPlayingLabel,
  reducePlayback,
  type PlaybackResult,
  type PlayableItem,
} from "../lib/playback";

export type AudioPlayerHandle = {
  playSpan: (span: { key: string; start: number; end: number; expectedAudioId: string; fromStart: boolean }) => void;
  playFull: () => void;
  loadedAudioId: () => string | null;
};

export type PlayRequest = {
  nonce: number;
  span: { key: string; start: number; end: number; expectedAudioId: string; fromStart: boolean };
};

type AudioPlayerProps = {
  item: PlayableItem | null;
  client: ApiClient;
  onKeep?: (id: string) => void | Promise<void>;
  playRequest?: PlayRequest | null;
};

export const AudioPlayer = forwardRef<AudioPlayerHandle, AudioPlayerProps>(function AudioPlayer({ item, client, onKeep, playRequest }, ref) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const stateRef = useRef(initialPlaybackState());
  const abortRef = useRef<AbortController | null>(null);
  const fullIntent = useRef<{ id: string; generation: number } | null>(null);
  const keepBusy = useRef(false);
  const [keeping, setKeeping] = useState(false);
  const [keepError, setKeepError] = useState("");
  const [view, setView] = useState(initialPlaybackState());
  const [clock, setClock] = useState(0);
  const clientRef = useRef(client);
  clientRef.current = client;

  function commit(result: PlaybackResult) {
    for (const url of result.revoke) URL.revokeObjectURL(url);
    stateRef.current = result.state;
    setView(result.state);
    if (fullIntent.current && fullIntent.current.generation !== result.state.generation) fullIntent.current = null;
    const audio = audioRef.current;
    if (result.fetch) {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      if (audio) {
        audio.removeAttribute("src");
        try {
          audio.load();
        } catch {
          // Some test environments do not implement media loading.
        }
      }
      const { path, id, generation } = result.fetch;
      clientRef.current
        .getBlob(path, controller.signal)
        .then((blob) => {
          const url = URL.createObjectURL(blob);
          const next = reducePlayback(stateRef.current, { type: "blobLoaded", requestId: id, generation, url });
          for (const revoked of next.revoke) URL.revokeObjectURL(revoked);
          stateRef.current = next.state;
          setView(next.state);
          const audio = audioRef.current;
          if (next.state.objectUrl === url && audio) {
            const pending = next.state.pendingSpan;
            if (pending && pending.expectedId === next.state.loadedId) {
              const jump = () => playQueued(pending.expectedId, pending.key);
              audio.addEventListener("loadedmetadata", jump, { once: true });
            }
            if (fullIntent.current) audio.addEventListener("loadedmetadata", playFullWhenReady, { once: true });
            audio.src = url;
          }
        })
        .catch((error: unknown) => {
          const next = reducePlayback(stateRef.current, {
            type: "blobFailed",
            requestId: id,
            generation,
            aborted: isAbortError(error),
          });
          stateRef.current = next.state;
          setView(next.state);
        });
      return;
    }
    if (!result.state.loadedId && audio) {
      audio.removeAttribute("src");
    }
  }

  useEffect(() => {
    commit(reducePlayback(stateRef.current, { type: "select", item }));
    // Selection identity only. Source changes stay on the user buttons.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [item?.id, item?.kind, item?.audioPlayable, item?.enhancedPlayable, item?.label]);

  useEffect(() => {
    return () => {
      abortRef.current?.abort();
      const released = reducePlayback(stateRef.current, { type: "release" });
      for (const url of released.revoke) URL.revokeObjectURL(url);
      stateRef.current = released.state;
      fullIntent.current = null;
    };
  }, []);

  useImperativeHandle(ref, () => ({
    loadedAudioId: () => stateRef.current.loadedId,
    playFull: () => {
      commit(reducePlayback(stateRef.current, { type: "clearSpan" }));
      const audio = audioRef.current;
      const selected = stateRef.current.item;
      const id = selected ? audioIdentity(selected, stateRef.current.source) : null;
      if (!audio || !id) return;
      fullIntent.current = { id, generation: stateRef.current.generation };
      if (audio.src && audio.readyState >= 1) playFullWhenReady();
      else audio.addEventListener("loadedmetadata", playFullWhenReady, { once: true });
    },
    playSpan: (span) => queueSpan(span),
  }));

  function playFullWhenReady() {
    const intent = fullIntent.current, audio = audioRef.current;
    if (!intent || !audio || intent.id !== stateRef.current.loadedId || intent.generation !== stateRef.current.generation) return;
    fullIntent.current = null;
    audio.currentTime = 0;
    audio.play().catch(() => undefined);
  }

  function playQueued(expectedId: string, key: string) {
    const pending = stateRef.current.pendingSpan;
    const audio = audioRef.current;
    if (!pending || !audio || pending.expectedId !== expectedId || pending.key !== key) return;
    if (stateRef.current.loadedId !== expectedId) return;
    if (pending.fromStart || audio.currentTime < pending.start || audio.currentTime >= pending.end) audio.currentTime = pending.start;
    audio.play().catch(() => undefined);
  }

  function queueSpan(span: { key: string; start: number; end: number; expectedAudioId: string; fromStart: boolean }) {
    fullIntent.current = null;
    const playing = audioRef.current;
    if (!span.fromStart && playing && !playing.paused && stateRef.current.loadedId === span.expectedAudioId && stateRef.current.clipKey === span.key) {
      playing.pause();
      return;
    }
    commit(reducePlayback(stateRef.current, {
      type: "queueSpan",
      key: span.key,
      start: span.start,
      end: span.end,
      expectedId: span.expectedAudioId,
      fromStart: span.fromStart,
    }));
    const pending = stateRef.current.pendingSpan;
    const audio = audioRef.current;
    if (!pending || !audio || pending.expectedId !== span.expectedAudioId) return;
    if (stateRef.current.loadedId !== span.expectedAudioId) return;
    if (audio.src && audio.readyState >= 1) playQueued(span.expectedAudioId, span.key);
    else audio.addEventListener("loadedmetadata", () => playQueued(span.expectedAudioId, span.key), { once: true });
  }

  useEffect(() => {
    if (!playRequest) return;
    queueSpan(playRequest.span);
    // Runs after selection so a review play can wait for the new source.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playRequest?.nonce]);

  const showSource = view.item?.kind === "event";
  const canEnhanced = !!view.item?.enhancedPlayable;
  const label = nowPlayingLabel(view.item, view.error);

  return (
    <footer className="border-t bg-sidebar px-4 py-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]" aria-label="Playback">
      <div className="flex flex-col gap-3 md:grid md:grid-cols-[minmax(160px,0.8fr)_minmax(220px,1.4fr)_auto_auto] md:items-center">
        <p className="truncate text-sm font-medium" aria-live="polite">{label}</p>
        <audio
          ref={audioRef}
          controls
          className="w-full"
          preload="none"
          onTimeUpdate={() => {
            const audio = audioRef.current;
            const clipStart = stateRef.current.clipStart;
            const clipStop = stateRef.current.clipStop;
            if (!audio) return;
            if (clipStart != null && audio.currentTime < clipStart) audio.currentTime = clipStart;
            if (clipStop != null && audio.currentTime >= clipStop) {
              audio.pause();
              audio.currentTime = clipStop;
            }
            setClock(audio.currentTime);
            window.dispatchEvent(new CustomEvent("life-recorder-tick", {
              detail: {
                time: audio.currentTime,
                clipKey: stateRef.current.clipKey,
                clipStart: stateRef.current.clipStart,
                paused: audio.paused,
              },
            }));
          }}
          onPlay={() => {
            const audio = audioRef.current;
            if (!audio) return;
            if (stateRef.current.clipStop != null && audio.currentTime >= stateRef.current.clipStop && stateRef.current.clipStart != null) {
              audio.currentTime = stateRef.current.clipStart;
            }
          }}
        />
        {showSource ? (
          <div className="flex gap-2" role="group" aria-label="Audio source">
            <Button type="button" size="sm" variant={view.source === "original" ? "default" : "outline"} aria-pressed={view.source === "original"} onClick={() => commit(reducePlayback(stateRef.current, { type: "setSource", source: "original" }))}>
              Original
            </Button>
            <Button type="button" size="sm" variant={view.source === "enhanced" ? "default" : "outline"} aria-pressed={view.source === "enhanced"} disabled={!canEnhanced} onClick={() => commit(reducePlayback(stateRef.current, { type: "setSource", source: "enhanced" }))}>
              Enhanced
            </Button>
          </div>
        ) : <span className="sr-only">Original audio</span>}
        {view.item?.kind === "chunk" && view.item.audioPlayable ? (
          <Button type="button" size="sm" variant="outline" disabled={!!item?.pinned || !onKeep || keeping} onClick={async () => {
            if (!item || !onKeep || keepBusy.current) return;
            const id = item.id;
            keepBusy.current = true; setKeeping(true); setKeepError("");
            try { await onKeep(id); }
            catch { if (stateRef.current.item?.id === id) setKeepError("Could not confirm retention. Reload this recording before trying again."); }
            finally { keepBusy.current = false; setKeeping(false); }
          }}>
            {item?.pinned ? "Kept" : keeping ? "Keeping…" : "Keep audio"}
          </Button>
        ) : null}
      </div>
      {keepError && <p role="alert" className="mt-2 text-sm text-destructive">{keepError}</p>}
      <span className="sr-only">{clock.toFixed(1)}</span>
    </footer>
  );
});
