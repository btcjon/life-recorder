import { Button } from "@/components/ui/button";
import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import { isAbortError, type ApiClient } from "../api/client";
import {
  audioIdentity,
  initialPlaybackState,
  playbackCaption,
  reducePlayback,
  type PlaybackResult,
  type PlayableItem,
} from "../lib/playback";
import type { ConversationQueue } from '../lib/conversation';

export type AudioPlayerHandle = {
  playSpan: (span: { key: string; start: number; end: number; expectedAudioId: string; fromStart: boolean }) => void;
  playFull: () => void;
  loadedAudioId: () => string | null;
};

export type PlayRequest = {
  nonce: number;
  span?: { key: string; start: number; end: number; expectedAudioId: string; fromStart: boolean } | null;
};

type AudioPlayerProps = {
  item: PlayableItem | null;
  client: ApiClient;
  onKeep?: (id: string) => void | Promise<void>;
  playRequest?: PlayRequest | null;
  selection?: { key: string; label: string } | null;
  queue?: ConversationQueue;
  onQueueAdvance?: (skip?: boolean) => void;
};

export const AudioPlayer = forwardRef<AudioPlayerHandle, AudioPlayerProps>(function AudioPlayer({ item, client, onKeep, playRequest, selection, queue, onQueueAdvance }, ref) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const stateRef = useRef(initialPlaybackState());
  const abortRef = useRef<AbortController | null>(null);
  const fullIntent = useRef<{ id: string; generation: number } | null>(null);
  const keepBusy = useRef(false);
  const [keeping, setKeeping] = useState(false);
  const [keepError, setKeepError] = useState("");
  const [view, setView] = useState(initialPlaybackState());
  const [clock, setClock] = useState(0);
  const [resumeNeeded, setResumeNeeded] = useState(false);
  const [mediaError, setMediaError] = useState(false);
  const playingRef = useRef(false);
  const clientRef = useRef(client);
  clientRef.current = client;

  function commit(result: PlaybackResult) {
    for (const url of result.revoke) URL.revokeObjectURL(url);
    stateRef.current = result.state;
    setView(result.state);
    if (fullIntent.current && fullIntent.current.generation !== result.state.generation) fullIntent.current = null;
    const audio = audioRef.current;
    if (result.fetch) {
      playingRef.current = false;
      setClock(0);
      setMediaError(false);
      setResumeNeeded(false);
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
    const current = item?.preferredSource ? { ...stateRef.current, source: item.preferredSource } : stateRef.current;
    commit(reducePlayback(current, { type: "select", item }));
    // Selection identity only. Source changes stay on the user buttons.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [item?.id, item?.kind, item?.audioPlayable, item?.enhancedPlayable, item?.label, item?.preferredSource, playRequest?.nonce]);

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
    attemptPlay(audio);
  }

  function attemptPlay(audio: HTMLAudioElement) {
    const generation = stateRef.current.generation;
    setResumeNeeded(false);
    audio.play().catch((error: unknown) => {
      if (generation !== stateRef.current.generation || isAbortError(error)) return;
      if (error instanceof DOMException && error.name === 'NotAllowedError') setResumeNeeded(true);
      else { audio.pause(); playingRef.current = false; setMediaError(true); }
    });
  }

  function playQueued(expectedId: string, key: string) {
    const pending = stateRef.current.pendingSpan;
    const audio = audioRef.current;
    if (!pending || !audio || pending.expectedId !== expectedId || pending.key !== key) return;
    if (stateRef.current.loadedId !== expectedId) return;
    if (pending.fromStart || audio.currentTime < pending.start || audio.currentTime >= pending.end) audio.currentTime = pending.start;
    attemptPlay(audio);
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
    if (playRequest.span) {
      queueSpan(playRequest.span);
      return;
    }
    commit(reducePlayback(stateRef.current, { type: "clearSpan" }));
    const audio = audioRef.current;
    const selected = stateRef.current.item;
    const id = selected ? audioIdentity(selected, stateRef.current.source) : null;
    if (!audio || !id) return;
    fullIntent.current = { id, generation: stateRef.current.generation };
    if (audio.src && audio.readyState >= 1) playFullWhenReady();
    else audio.addEventListener("loadedmetadata", playFullWhenReady, { once: true });
    // Runs after the item effect so an explicit play can wait for the new source.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playRequest?.nonce]);

  const showSource = view.item?.kind === "event";
  const canEnhanced = !!view.item?.enhancedPlayable;
  const captionItem = view.item?.kind === "event" ? { ...view.item, label: (view.source === "enhanced" ? "Enhanced · " : "Speech-only · ") + view.item.label } : view.item;
  const caption = playbackCaption(captionItem, view.error, selection ?? null);

  function retryAudio() {
    const selected = stateRef.current.item;
    if (!selected) return;
    audioRef.current?.pause();
    abortRef.current?.abort();
    commit(reducePlayback(stateRef.current, { type: 'release' }));
    commit(reducePlayback(stateRef.current, { type: 'select', item: selected }));
    const id = audioIdentity(selected, stateRef.current.source);
    if (id) fullIntent.current = { id, generation: stateRef.current.generation };
  }

  return (
    <footer hidden={!view.item} className="min-w-0 overflow-x-hidden border-t bg-background px-3 py-2 pb-[max(0.5rem,env(safe-area-inset-bottom))]" aria-label="Playback">
      {queue && <div className="mb-1 text-xs text-muted-foreground" aria-live="polite"><p className="truncate" title={queue.label}>{queue.label} · {queue.finished ? 'Conversation complete · ' : ''}Source {queue.sources[queue.index]?.position} of {queue.total}</p><p>{Math.floor(clock / 60)}:{String(Math.floor(clock % 60)).padStart(2, '0')} in this source · Gaps are not recorded audio.</p></div>}
      <div className="flex min-w-0 flex-col gap-2 md:grid md:grid-cols-[minmax(0,0.8fr)_minmax(0,1.4fr)_auto_auto] md:items-center">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium" aria-live="polite">{caption.primary}</p>
          {caption.secondary ? <p className="truncate text-xs text-muted-foreground">{caption.secondary}</p> : null}
        </div>
        <audio
          ref={audioRef}
          controls
          className="w-full max-w-full"
          preload="none"
          onEnded={() => {
            if (queue && !queue.finished && playingRef.current && !stateRef.current.pendingSpan && stateRef.current.clipStop == null && !mediaError && !view.error) {
              playingRef.current = false;
              onQueueAdvance?.();
            }
          }}
          onError={() => {
            if (!stateRef.current.objectUrl) return;
            audioRef.current?.pause();
            playingRef.current = false;
            setMediaError(true);
          }}
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
            playingRef.current = true;
            setResumeNeeded(false);
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
        {view.item?.kind === "chunk" && view.item.audioPlayable && view.item.keepEligible !== false ? (
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
      {(view.error || mediaError) && <div role="alert" className="mt-2 flex flex-wrap items-center gap-2 text-sm">
        <p>Playback paused because this source could not be played.</p>
        <Button type="button" size="sm" variant="outline" onClick={retryAudio}>Retry</Button>
        {queue && <Button type="button" size="sm" variant="outline" onClick={() => onQueueAdvance?.(true)}>Skip source</Button>}
      </div>}
      {resumeNeeded && <Button type="button" size="sm" className="mt-2" onClick={() => { if (audioRef.current) attemptPlay(audioRef.current); }}>Resume playback</Button>}
      {keepError && <p role="alert" className="mt-2 text-sm text-destructive">{keepError}</p>}
      <span className="sr-only">{clock.toFixed(1)}</span>
    </footer>
  );
});
