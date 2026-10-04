import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from "@/components/ui/resizable";
import { RefreshCw } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type SetStateAction } from "react";
import { acceptSelectionResponse, isAbortError, type ApiClient } from "../../api/client";
import type { DayPayload, Person, ReviewPayload } from "../../api/types";
import { REVIEW_ATTEMPT_CAP, REVIEW_RETRY_MS, reviewFetchPlan, type ReviewFetchRecord } from "../../lib/reviewFetch";
import type { AudioPlayerHandle } from "../../components/AudioPlayer";
import { useIsMobile } from "@/hooks/use-mobile";
import { buildRecordingList, selectionStillPresent, toPlayableChunk } from "../../lib/recordings";
import type { RouteState } from "../../lib/route";
import { RecordingDetail } from "./RecordingDetail";
import { RecordingList } from "./RecordingList";

export function RecordingsView({
  route,
  payload,
  setPayload,
  days,
  status,
  alert,
  client,
  playerRef,
  onRoute,
  onReload,
  onRefresh,
  onIdentityChange,
}: {
  route: RouteState;
  payload: DayPayload | null;
  setPayload: (value: SetStateAction<DayPayload | null>) => void;
  days: string[];
  status: string;
  alert: boolean;
  client: ApiClient;
  playerRef: React.RefObject<AudioPlayerHandle | null>;
  onRoute: (patch: Partial<RouteState>, historyMode?: "push" | "replace") => void;
  onReload: () => Promise<boolean> | boolean | void;
  onRefresh: () => void;
  onIdentityChange?: (changeId: string | null) => void;
}) {
  const mobile = useIsMobile();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const reviewRecords = useRef<Record<string, ReviewFetchRecord>>({});
  const [reviewTick, setReviewTick] = useState(0);
  const [reviewNotice, setReviewNotice] = useState("");
  const restoreFocus = useRef<string | null>(null);
  const selectedRef = useRef({ kind: route.selectedKind, id: route.selectedId });
  selectedRef.current = { kind: route.selectedKind, id: route.selectedId };
  const rows = useMemo(
    () => buildRecordingList(payload, { query: route.query, showQuiet: route.showQuiet, flat: route.flat, expanded }),
    [payload, route.query, route.showQuiet, route.flat, expanded],
  );
  const chunk = route.selectedKind === "chunk" ? (payload?.chunks || []).find((item) => item.id === route.selectedId) || null : null;
  const event = route.selectedKind === "event" ? (payload?.events || []).find((item) => item.id === route.selectedId) || null : null;

  useEffect(() => {
    if (!payload || route.selectedId || mobile) return;
    const first = (payload.chunks || []).find((item) => item.transcript?.trim() || item.status === "needs_attention");
    if (first) onRoute({ selectedId: first.id, selectedKind: "chunk" }, "replace");
  }, [payload, route.selectedId, mobile, onRoute]);

  useEffect(() => {
    if (!payload || !route.selectedId || status === "Loading") return;
    if (!selectionStillPresent(payload, route.selectedKind, route.selectedId)) {
      onRoute({ selectedId: null, selectedKind: "chunk", pane: "list" }, "replace");
    }
  }, [payload, route.selectedId, route.selectedKind, onRoute, status]);

  useEffect(() => {
    const current = chunk;
    if (!current) return;
    const plan = reviewFetchPlan(reviewRecords.current[current.id] || null, current.id, {
      hasSpeakers: Array.isArray(current.speakers),
      voicePending: !!current.voice_pending,
      complete: current.status === "complete",
    });
    if (plan.action === "skip") {
      setReviewNotice("");
      return;
    }
    if (plan.action === "fail") {
      setReviewNotice(plan.message);
      return;
    }
    const controller = new AbortController();
    let cancelled = false;
    const requested = { kind: "chunk" as const, id: current.id };
    const timer = window.setTimeout(() => {
      reviewRecords.current[current.id] = plan.next;
      client.review(current.id, controller.signal).then((detail) => {
        if (cancelled || controller.signal.aborted) return;
        const selected = selectedRef.current;
        if (!acceptSelectionResponse(requested, selected.kind === "chunk" && selected.id ? { kind: "chunk", id: selected.id } : null)) return;
        applyReview(current.id, detail);
        if (!detail.voice_pending) {
          setReviewNotice("");
          return;
        }
        if (plan.next.attempts >= REVIEW_ATTEMPT_CAP) {
          setReviewNotice("Speaker analysis is still pending. Refresh to try again.");
          return;
        }
        window.setTimeout(() => {
          if (!cancelled) setReviewTick((count) => count + 1);
        }, REVIEW_RETRY_MS);
      }).catch((error: unknown) => {
        if (cancelled || isAbortError(error) || controller.signal.aborted) return;
        const selected = selectedRef.current;
        if (!acceptSelectionResponse(requested, selected.kind === "chunk" && selected.id ? { kind: "chunk", id: selected.id } : null)) return;
        if (plan.next.attempts >= REVIEW_ATTEMPT_CAP) {
          setReviewNotice("Couldn't load speakers for this recording. Refresh to try again.");
          return;
        }
        setReviewNotice("Couldn't load speakers. Retrying…");
        window.setTimeout(() => {
          if (!cancelled) setReviewTick((count) => count + 1);
        }, REVIEW_RETRY_MS);
      });
    }, plan.delayMs);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      controller.abort();
    };
    // Speakers are read at start. A stale response cannot restart this effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chunk?.id, chunk?.status, reviewTick, client]);

  useEffect(() => {
    if (mobile && route.pane === "detail") document.getElementById("back-recordings")?.focus();
    if (mobile && route.pane === "list" && restoreFocus.current) {
      document.getElementById("recording-" + restoreFocus.current)?.focus();
      restoreFocus.current = null;
    }
  }, [mobile, route.pane, route.selectedId]);

  function applyReview(chunkId: string, detail: ReviewPayload) {
    setPayload((current) => {
      if (!current || !(current.chunks || []).some((item) => item.id === chunkId)) return current;
      return {
        ...current,
        people: detail.people || current.people,
        chunks: (current.chunks || []).map((item) => item.id === chunkId ? {
          ...item,
          speakers: detail.speakers || [],
          voice_pending: !!detail.voice_pending,
          identity_revision: detail.identity_revision ?? item.identity_revision,
          diarization: detail.diarization || item.diarization,
          diarization_status: detail.diarization_status || item.diarization_status,
          transcript: detail.transcript || item.transcript,
        } : item),
      };
    });
  }

  function selectChunk(id: string) {
    onRoute({ selectedId: id, selectedKind: "chunk", pane: mobile ? "detail" : route.pane }, mobile ? "push" : "replace");
  }

  function back() {
    restoreFocus.current = route.selectedId;
    onRoute({ pane: "list" }, "replace");
  }

  const detail = (
    <RecordingDetail
      payload={payload}
      loading={!payload}
      chunk={chunk}
      event={event}
      detailMode={route.detailMode}
      onDetailMode={(detailMode) => onRoute({ detailMode }, "replace")}
      client={client}
      playerItem={chunk ? toPlayableChunk(chunk) : null}
      onPlaySpan={(span) => playerRef.current?.playSpan(span)}
      reviewNotice={reviewNotice}
      onPlayFull={() => playerRef.current?.playFull()}
      onIdentityChange={onIdentityChange}
      onReload={onReload}
      onPeople={(person: Person) => {
        if (!payload) return;
        setPayload({ ...payload, people: (payload.people || []).concat(person) });
      }}
    />
  );

  const list = payload ? (
    <RecordingList
      rows={rows}
      payload={payload}
      client={client}
      selectedId={route.selectedId}
      selectedKind={route.selectedKind}
      expanded={expanded}
      onToggle={(id) => setExpanded((current) => {
        const next = new Set(current);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        return next;
      })}
      onSelectChunk={selectChunk}
      onSelectEvent={(id) => onRoute({ selectedId: id, selectedKind: "event", pane: mobile ? "detail" : route.pane }, mobile ? "push" : "replace")}
      onSaved={onReload}
      readOnly={status === "Loading" || status.startsWith("Couldn't") || status.startsWith("Open through")}
    />
  ) : <p className="p-4 text-sm text-muted-foreground">Loading recordings…</p>;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-wrap items-center gap-3 border-b px-3 py-2">
        <Label className="text-xs text-muted-foreground">Day
          <Input type="date" aria-label="Day" value={route.day} list="recording-days" className="w-40" onChange={(event) => onRoute({ day: event.target.value, selectedId: null, pane: "list" }, "replace")} />
        </Label>
        <datalist id="recording-days">{days.map((day) => <option key={day} value={day} />)}</datalist>
        <Input type="search" aria-label="Search recordings" placeholder="Search recordings" value={route.query} className="w-48 max-w-full" onChange={(event) => onRoute({ query: event.target.value }, "replace")} />
        <Label className="text-xs text-muted-foreground">
          <Checkbox checked={route.showQuiet} onCheckedChange={(checked) => onRoute({ showQuiet: checked === true }, "replace")} />
          Show quiet/pending
        </Label>
        <Label className="text-xs text-muted-foreground">
          <Checkbox checked={route.flat} onCheckedChange={(checked) => onRoute({ flat: checked === true }, "replace")} />
          Flat list
        </Label>
        <Button type="button" size="sm" variant="outline" className="touch-target" onClick={() => {
          if (chunk) delete reviewRecords.current[chunk.id];
          setReviewNotice("");
          setReviewTick((count) => count + 1);
          onRefresh();
        }}><RefreshCw /> Refresh</Button>
        <p className={"text-xs " + (alert ? "text-destructive" : "text-muted-foreground")} aria-live="polite">{status}</p>
      </div>
      {mobile ? (
        route.pane === "detail" && route.selectedId ? (
          <section aria-label="Recording detail" className="flex min-h-0 min-w-0 flex-1 flex-col">
            <div className="px-4 pt-3"><Button id="back-recordings" type="button" variant="outline" className="touch-target" onClick={back}>Back to recordings</Button></div>
            <div className="min-h-0 min-w-0 flex-1 overflow-hidden">{detail}</div>
          </section>
        ) : <div className="min-h-0 flex-1">{list}</div>
      ) : (
        <ResizablePanelGroup orientation="horizontal" className="min-h-0 flex-1">
          <ResizablePanel defaultSize={34} minSize={24}>
            {list}
          </ResizablePanel>
          <ResizableHandle withHandle />
          <ResizablePanel defaultSize={66} minSize={36}>
            {detail}
          </ResizablePanel>
        </ResizablePanelGroup>
      )}
    </div>
  );
}
