import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, createRequestGate, healthSummary, isAbortError, type ApiClient } from "./api/client";
import type { DayPayload } from "./api/types";
import { AppShell } from "./components/AppShell";
import { AudioPlayer, type AudioPlayerHandle } from "./components/AudioPlayer";
import { PeopleView } from "./features/people/PeopleView";
import { RecordingsView } from "./features/recordings/RecordingsView";
import { ReviewView } from "./features/review/ReviewView";
import { ContextView } from "./features/context/ContextView";
import { HealthView, type HealthSnapshot } from "./features/health/HealthView";
import { easternDay } from "./lib/identity";
import { audioIdentity, type PlayableItem } from "./lib/playback";
import { dayStatus } from "./lib/recordings";
import { readRoute, routeUrl, type RouteState } from "./lib/route";

export function App({ client }: { client: ApiClient }) {
  const gate = useMemo(() => createRequestGate(), []);
  const playerRef = useRef<AudioPlayerHandle>(null);
  const playNonce = useRef(0);
  const payloadDay = useRef("");
  const [lastChangeId, setLastChangeId] = useState<string | null>(null);
  const [playback, setPlayback] = useState<{
    item: PlayableItem;
    nonce: number;
    span: { key: string; start: number; end: number; expectedAudioId: string; fromStart: boolean } | null;
  } | null>(null);
  const [route, setRoute] = useState<RouteState>(() => readRoute(new URLSearchParams(window.location.search), window.location.pathname));
  const [days, setDays] = useState<string[]>([]);
  const [payload, setPayload] = useState<DayPayload | null>(null);
  const [status, setStatus] = useState("Loading");
  const [alert, setAlert] = useState(false);
  const [authFailed, setAuthFailed] = useState(false);
  const [health, setHealth] = useState("Checking processing status…");
  const [healthReport, setHealthReport] = useState<HealthSnapshot | null>(null);
  const [healthError, setHealthError] = useState('');
  const [healthLoading, setHealthLoading] = useState(false);
  const routeRef = useRef(route);
  routeRef.current = route;

  const updateRoute = useCallback((patch: Partial<RouteState>, historyMode: "push" | "replace" = "replace") => {
    const next = { ...routeRef.current, ...patch };
    const url = routeUrl(window.location.pathname, next);
    if (historyMode === "push") history.pushState(null, "", url);
    else history.replaceState(null, "", url);
    routeRef.current = next;
    setRoute(next);
  }, []);

  const loadHealth = useCallback(async () => {
    const ticket = gate.start("health");
    setHealthLoading(true);
    try {
      const report = await client.health(ticket.signal);
      if (!ticket.isCurrent()) return;
      setHealth(healthSummary(report, false));
      setHealthReport(report);
      setHealthError('');
    } catch (error) {
      if (isAbortError(error) || !ticket.isCurrent()) return;
      setHealth(healthSummary(null, true));
      setHealthError('Processing status unavailable. Previous status may be stale.');
    } finally {
      if (ticket.isCurrent()) setHealthLoading(false);
    }
  }, [client, gate]);

  useEffect(() => {
    const refresh = () => {
      if (document.visibilityState === 'visible') void loadHealth();
    };
    document.addEventListener('visibilitychange', refresh);
    const timer = window.setInterval(refresh, 15000);
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', refresh); };
  }, [loadHealth]);

  const loadDay = useCallback(async (day: string): Promise<boolean> => {
    if (!day || day !== routeRef.current.day) return false;
    const ticket = gate.start("day");
    if (payloadDay.current !== day) setPayload(null);
    payloadDay.current = day;
    setStatus("Loading");
    setAlert(false);
    try {
      const next = await client.day(day, ticket.signal);
      if (!ticket.isCurrent()) return false;
      setPayload({ ...next, day });
      const text = dayStatus(next);
      setStatus(text);
      setAlert(!!text);
      setAuthFailed(false);
      return true;
    } catch (error) {
      if (isAbortError(error) || !ticket.isCurrent()) return false;
      setAlert(true);
      setStatus(error instanceof ApiError && error.status === 401 ? "Open through the local launcher." : "Couldn't load this day. Refresh to try again.");
      if (error instanceof ApiError && error.status === 401) setAuthFailed(true);
      return false;
    }
  }, [client, gate]);

  useEffect(() => {
    const onPop = () => {
      const next = readRoute(new URLSearchParams(window.location.search), window.location.pathname);
      routeRef.current = next;
      setRoute(next);
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  useEffect(() => {
    const ticket = gate.start("days");
    client.days(ticket.signal).then((data) => {
      if (!ticket.isCurrent()) return;
      const list = data.days || [];
      setDays(list);
      if (!routeRef.current.day) {
        const last = list.length ? list[list.length - 1] : new Date().toISOString().slice(0, 10);
        updateRoute({ day: last }, "replace");
      }
    }).catch((error: unknown) => {
      if (isAbortError(error) || !ticket.isCurrent()) return;
      const unauthorized = error instanceof ApiError && error.status === 401;
      setAuthFailed(unauthorized);
      setStatus(unauthorized ? "Open through the local launcher." : "Couldn't load available days. Refresh to try again.");
      setAlert(true);
    });
    void loadHealth();
  }, [client, gate, loadHealth, updateRoute]);

  useEffect(() => {
    if (route.day) void loadDay(route.day);
  }, [route.day, route.view, loadDay]);

  const requestPlay = useCallback((item: PlayableItem, span?: { key: string; start: number; end: number; expectedAudioId: string; fromStart: boolean } | null) => {
    playNonce.current += 1;
    setPlayback({ item, nonce: playNonce.current, span: span ?? null });
  }, []);

  const selectedChunk = route.selectedKind === "chunk" ? (payload?.chunks || []).find((item) => item.id === route.selectedId) : undefined;
  const selectedEvent = route.selectedKind === "event" ? (payload?.events || []).find((item) => item.id === route.selectedId) : undefined;
  const selectedGroup = route.selectedKind === "group"
    ? (payload?.display_blocks || []).find((block) => (block.id || (block.chunk_ids || []).join("-")) === route.selectedId)
    : undefined;
  const selection = route.selectedId ? {
    key: route.selectedKind + ":" + route.selectedId,
    label: selectedChunk?.started_local || selectedEvent?.started_local || selectedGroup?.title || selectedGroup?.started_local || route.selectedId,
  } : null;

  return (
    <AppShell view={route.view} health={authFailed ? "Open through the local launcher." : (healthReport?.checked_at && Date.now() / 1000 - healthReport.checked_at > 45 ? 'Stale processing status · ' : '') + health} onView={(view) => {
      updateRoute({ view }, "push");
    }} player={
      <AudioPlayer
        ref={playerRef}
        item={playback?.item ?? null}
        selection={selection}
        client={client}
        playRequest={playback ? { nonce: playback.nonce, span: playback.span } : null}
        onKeep={async (id) => {
          await client.keepAudio(id);
          setPayload((current) => current ? { ...current, chunks: (current.chunks || []).map((chunk) => chunk.id === id ? { ...chunk, audio_pinned: true } : chunk) } : current);
          setPlayback((current) => current && current.item.kind === "chunk" && current.item.id === id
            ? { ...current, item: { ...current.item, pinned: true } } : current);
        }}
      />
    }>
      {route.view === "recordings" ? (
        <RecordingsView
          route={route}
          payload={payload?.day === route.day ? payload : null}
          setPayload={setPayload}
          days={days}
          status={status}
          alert={alert}
          client={client}
          playerRef={playerRef}
          onIdentityChange={setLastChangeId}
          onRoute={updateRoute}
          onPlayItem={requestPlay}
          onReload={() => route.day ? loadDay(route.day) : false}
          onRefresh={() => { void loadHealth(); if (route.day) void loadDay(route.day); }}
        />
      ) : route.view === "review" ? (
        <ReviewView
          client={client}
          people={payload?.people || []}
          onIdentityChange={setLastChangeId}
          onPlay={(request) => {
            if (!request.audioUsable) return;
            const item: PlayableItem = {
              id: request.chunkId,
              kind: "chunk",
              audioPlayable: true,
              enhancedPlayable: false,
              label: "Speaker span · not confirmed",
              pinned: false,
            };
            const expectedAudioId = audioIdentity(item, "original");
            if (!expectedAudioId) return;
            requestPlay(item, { key: request.turnId, start: request.started, end: request.ended, expectedAudioId, fromStart: true });
          }}
          onOpenRecording={(target) => {
            updateRoute({
              view: "recordings",
              day: easternDay(target.startedAt) || routeRef.current.day,
              selectedId: target.chunkId,
              selectedKind: "chunk",
              pane: "detail",
              detailMode: "turns",
            }, "push");
          }}
        />
      ) : route.view === "people" ? (
        <PeopleView
          client={client}
          catalog={payload?.people || []}
          lastChangeId={lastChangeId}
          onIdentityChange={setLastChangeId}
          onChanged={() => { if (route.day) void loadDay(route.day); }}
          onOpenRecording={(target) => {
            updateRoute({
              view: "recordings",
              day: easternDay(target.startedAt) || routeRef.current.day,
              selectedId: target.chunkId,
              selectedKind: "chunk",
              pane: "detail",
              detailMode: "turns",
            }, "push");
          }}
        />
      ) : route.view === "context" ? (
        <ContextView client={client} day={route.day} onDay={(day) => updateRoute({ day, selectedId: null, pane: "list" })} />
      ) : route.view === "health" ? (
        <HealthView client={client} shared={{ report: healthReport, error: healthError || (healthReport?.checked_at && Date.now() / 1000 - healthReport.checked_at > 45 ? 'Processing status is stale.' : ''), loading: healthLoading, refresh: () => void loadHealth() }} />
      ) : null}
    </AppShell>
  );
}
