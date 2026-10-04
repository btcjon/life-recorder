import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from "@/components/ui/resizable";
import { RefreshCw } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type RefObject, type SetStateAction } from "react";
import { acceptSelectionResponse, isAbortError, type ApiClient } from "../../api/client";
import type { DayPayload, Person, ReviewPayload } from "../../api/types";
import { REVIEW_ATTEMPT_CAP, REVIEW_RETRY_MS, reviewFetchPlan, type ReviewFetchRecord } from "../../lib/reviewFetch";
import type { AudioPlayerHandle } from "../../components/AudioPlayer";
import { useIsMobile } from "@/hooks/use-mobile";
import { buildRecordingList, firstSearchTarget, groupMembers, qualifiesAsGroup, selectionStillPresent } from "../../lib/recordings";
import type { PlayableItem } from "../../lib/playback";
import { easternDay } from "../../lib/identity";
import type { RouteState } from "../../lib/route";
import { RecordingDetail } from "./RecordingDetail";
import { RecordingList } from "./RecordingList";

type SpanRequest = { key: string; start: number; end: number; expectedAudioId: string; fromStart: boolean };

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
  onPlayItem,
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
  playerRef: RefObject<AudioPlayerHandle | null>;
  onRoute: (patch: Partial<RouteState>, historyMode?: "push" | "replace") => void;
  onPlayItem?: (item: PlayableItem, span?: SpanRequest | null) => void;
  onReload: () => Promise<boolean> | boolean | void;
  onRefresh: () => void;
  onIdentityChange?: (changeId: string | null) => void;
}) {
  const mobile = useIsMobile();
  const reviewRecords = useRef<Record<string, ReviewFetchRecord>>({});
  const [reviewTick, setReviewTick] = useState(0);
  const [reviewNotice, setReviewNotice] = useState("");
  const [filtersOpen, setFiltersOpen] = useState(false);
  const restoreFocus = useRef<string | null>(null);
  const listScroll = useRef(0);
  const listRef = useRef<HTMLDivElement>(null);
  const appliedQuery = useRef<string | null>(null);
  const selectedRef = useRef({ kind: route.selectedKind, id: route.selectedId });
  selectedRef.current = { kind: route.selectedKind, id: route.selectedId };
  const rows = useMemo(
    () => buildRecordingList(payload, { query: route.query, showQuiet: route.showQuiet, showPending: route.showPending, groups: route.groups }),
    [payload, route.query, route.showQuiet, route.showPending, route.groups],
  );
  const chunk = route.selectedKind === "chunk" ? (payload?.chunks || []).find((item) => item.id === route.selectedId) || null : null;
  const event = route.selectedKind === "event" ? (payload?.events || []).find((item) => item.id === route.selectedId) || null : null;
  const groupBlock = route.selectedKind === "group" ? (payload?.display_blocks || []).find((block) => (block.id || (block.chunk_ids || []).join("-")) === route.selectedId) || null : null;
  const group = groupBlock && payload && qualifiesAsGroup(groupBlock, groupMembers(payload, groupBlock, true, true))
    ? { block: groupBlock, members: groupMembers(payload, groupBlock, true, true) }
    : null;
  const speakerChunk = chunk;
  const hasSpeakerDetail = Array.isArray(speakerChunk?.speakers);

  useEffect(() => {
    if (!payload || route.selectedId || mobile || route.groups || route.selectedKind === "event") return;
    const first = rows.find((row) => row.type === "chunk");
    if (first && first.type === "chunk") onRoute({ selectedId: first.chunk.id, selectedKind: "chunk" }, "replace");
  }, [payload, route.selectedId, route.groups, route.selectedKind, mobile, onRoute, rows]);

  useEffect(() => {
    if (!route.query.trim() || mobile) {
      appliedQuery.current = route.query;
      return;
    }
    if (appliedQuery.current === route.query) return;
    if (route.selectedId && appliedQuery.current === null) {
      appliedQuery.current = route.query;
      return;
    }
    const match = firstSearchTarget(rows);
    if (!match) return;
    appliedQuery.current = route.query;
    onRoute({ selectedId: match.id, selectedKind: match.kind, pane: "detail" }, "replace");
  }, [route.query, route.selectedId, rows, mobile, onRoute]);

  useEffect(() => {
    if (!payload || !route.selectedId || status === "Loading") return;
    if (!selectionStillPresent(payload, route.selectedKind, route.selectedId)) {
      onRoute({ selectedId: null, selectedKind: "chunk", pane: "list" }, "replace");
    }
  }, [payload, route.selectedId, route.selectedKind, onRoute, status]);

  useEffect(() => {
    const current = speakerChunk;
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
    // Speakers are read for the selected recording only. A stale response cannot apply or start audio.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [speakerChunk?.id, speakerChunk?.status, hasSpeakerDetail, speakerChunk?.voice_pending, reviewTick, client]);

  useEffect(() => {
    if (mobile && route.pane === "detail") document.getElementById("back-recordings")?.focus();
    if (mobile && route.pane === "list") {
      if (listRef.current) listRef.current.scrollTop = listScroll.current;
      if (restoreFocus.current) {
        document.getElementById("recording-" + restoreFocus.current)?.focus();
        restoreFocus.current = null;
      }
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

  function rememberScroll() {
    if (listRef.current) listScroll.current = listRef.current.scrollTop;
  }

  function selectChunk(id: string) {
    rememberScroll();
    onRoute({ selectedId: id, selectedKind: "chunk", pane: mobile ? "detail" : route.pane }, mobile ? "push" : "replace");
  }

  function openSource(id: string, started?: string) {
    const day = started ? easternDay(started) : route.day;
    if (!day || day === route.day) {
      selectChunk(id);
      return;
    }
    rememberScroll();
    onRoute({ day, selectedId: id, selectedKind: "chunk", pane: "detail" }, "push");
  }

  function selectGroup(id: string) {
    rememberScroll();
    onRoute({ selectedId: id, selectedKind: "group", groups: true, pane: mobile ? "detail" : route.pane }, mobile ? "push" : "replace");
  }

  function back() {
    restoreFocus.current = route.selectedId;
    rememberScroll();
    onRoute({ pane: "list" }, "replace");
  }

  async function reloadCurrent() {
    const result = await onReload();
    if (result === true && chunk) {
      delete reviewRecords.current[chunk.id];
      setReviewTick((tick) => tick + 1);
    }
    return result === true;
  }

  const readOnly = status === "Loading" || status.startsWith("Couldn't") || status.startsWith("Open through");
  const detail = (
    <RecordingDetail
      payload={payload}
      loading={!payload}
      chunk={chunk}
      event={event}
      group={group}
      query={route.query}
      detailMode={route.detailMode}
      onDetailMode={(detailMode) => onRoute({ detailMode }, "replace")}
      client={client}
      playerItem={null}
      onPlaySpan={() => undefined}
      onPlayFull={() => playerRef.current?.playFull()}
      onPlayItem={onPlayItem}
      onOpenSource={openSource}
      reviewNotice={reviewNotice}
      onIdentityChange={onIdentityChange}
      onReload={reloadCurrent}
      readOnly={readOnly}
      onPeople={(person: Person) => {
        if (!payload) return;
        setPayload({ ...payload, people: (payload.people || []).concat(person) });
      }}
    />
  );

  const list = payload ? (
    <RecordingList
      rows={rows}
      selectedId={route.selectedId}
      selectedKind={route.selectedKind}
      listRef={listRef}
      onSelectChunk={selectChunk}
      onSelectGroup={selectGroup}
    />
  ) : <p className="p-4 text-sm text-muted-foreground">Loading recordings…</p>;

  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col overflow-x-hidden">
      <div className="flex min-w-0 flex-wrap items-center gap-2 border-b px-3 py-2">
        <div role="group" aria-label="Library view" className="flex gap-1">
          <Button type="button" size="sm" variant={route.groups ? "outline" : "secondary"} className="touch-target" aria-pressed={!route.groups} onClick={() => onRoute({ groups: false, selectedId: null, selectedKind: "chunk", pane: "list" }, "replace")}>Recordings</Button>
          <Button type="button" size="sm" variant={route.groups ? "secondary" : "outline"} className="touch-target" aria-pressed={route.groups} onClick={() => onRoute({ groups: true, selectedId: null, selectedKind: "group", pane: "list" }, "replace")}>Groups</Button>
        </div>
        <Label className="text-xs text-muted-foreground">Day
          <Input type="date" aria-label="Day" value={route.day} list="recording-days" className="w-full max-w-40" onChange={(event) => onRoute({ day: event.target.value, selectedId: null, pane: "list" }, "replace")} />
        </Label>
        <datalist id="recording-days">{days.map((day) => <option key={day} value={day} />)}</datalist>
        <Input type="search" aria-label="Search recordings" placeholder="Search recordings" value={route.query} className="w-full max-w-48" onChange={(event) => onRoute({ query: event.target.value }, "replace")} />
        <Button type="button" size="sm" variant="outline" className="touch-target" aria-expanded={filtersOpen} aria-controls="recording-filters" onClick={() => setFiltersOpen((open) => !open)}>Filters</Button>
        {filtersOpen ? (
          <div id="recording-filters" className="flex w-full flex-wrap gap-3 rounded-lg border p-2">
            <Label className="text-xs text-muted-foreground">
              <Checkbox checked={route.showQuiet} onCheckedChange={(checked) => onRoute({ showQuiet: checked === true }, "replace")} />
              Show quiet recordings
            </Label>
            <Label className="text-xs text-muted-foreground">
              <Checkbox checked={route.showPending} onCheckedChange={(checked) => onRoute({ showPending: checked === true }, "replace")} />
              Show pending processing
            </Label>
            <p className="text-xs text-muted-foreground">Failed processing stays visible.</p>
          </div>
        ) : null}
        <Button type="button" size="sm" variant="outline" className="touch-target" onClick={() => {
          if (chunk) delete reviewRecords.current[chunk.id];
          setReviewNotice("");
          setReviewTick((count) => count + 1);
          onRefresh();
        }}><RefreshCw /> Refresh</Button>
        <p className={"min-w-0 text-xs " + (alert ? "text-destructive" : "text-muted-foreground")} aria-live="polite">{status}</p>
      </div>
      {mobile ? (
        route.pane === "detail" && route.selectedId ? (
          <section aria-label="Recording detail" className="flex min-h-0 min-w-0 flex-1 flex-col">
            <div className="px-3 pt-3"><Button id="back-recordings" type="button" variant="outline" className="touch-target" aria-label="Back to recordings" onClick={back}>Back</Button></div>
            <div className="min-h-0 min-w-0 flex-1 overflow-hidden">{detail}</div>
          </section>
        ) : <div className="min-h-0 min-w-0 flex-1">{list}</div>
      ) : (
        <ResizablePanelGroup orientation="horizontal" className="min-h-0 min-w-0 flex-1">
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
