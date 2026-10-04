import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useEffect, useRef, useState } from "react";
import { ApiError, eventEditFailureCopy, labelFailureCopy, retryFailureCopy, type ApiClient } from "../../api/client";
import type { Chunk, DayPayload, DisplayBlock, IdentityMutationResult, Person, SpeechEvent } from "../../api/types";
import { audioIdentity, type PlayableItem } from "../../lib/playback";
import {
  audioRetentionLabel,
  briefDisclosureLabel,
  briefStretch,
  cleanSeconds,
  derivedScopeCopy,
  eventRange,
  eventSources,
  eventTimeRange,
  groupBounds,
  groupLabel,
  groupTurns,
  highlightSegments,
  processingBits,
  relatedEvents,
  speakerPendingLabel,
  toPlayableChunk,
  toPlayableEvent,
  transcriptState,
  type TurnGroup,
} from "../../lib/recordings";
import { fullyNamed, groupLabelBody } from "../../lib/identity";
import { CREATE_ASSIGN_NOTICE, UNKNOWN_OUTCOME, enrollmentFailure, isUnknownSaveOutcome } from "../identitySafety";

type SpanRequest = { key: string; start: number; end: number; expectedAudioId: string; fromStart: boolean };

type DetailProps = {
  payload: DayPayload | null;
  loading: boolean;
  chunk: Chunk | null;
  event: SpeechEvent | null;
  group?: { block: DisplayBlock; members: Chunk[] } | null;
  query?: string;
  detailMode: "transcript" | "turns";
  onDetailMode: (mode: "transcript" | "turns") => void;
  client: ApiClient;
  playerItem: PlayableItem | null;
  onPlaySpan: (span: SpanRequest) => void;
  onPlayFull: () => void;
  onPlayItem?: (item: PlayableItem, span?: SpanRequest | null) => void;
  onOpenSource?: (id: string, started?: string) => void;
  reviewNotice?: string;
  onReload: () => Promise<boolean> | boolean | void;
  onPeople: (person: Person) => void;
  onIdentityChange?: (changeId: string | null) => void;
  readOnly?: boolean;
};

export function RecordingDetail(props: DetailProps) {
  if (props.loading && !props.payload) {
    return <div className="grid gap-3 p-6"><Skeleton className="h-8 w-48" /><Skeleton className="h-24 w-full" /></div>;
  }
  if (props.group) {
    return <GroupDetail {...props} group={props.group} />;
  }
  if (props.event) {
    return <EventContext {...props} event={props.event} />;
  }
  if (!props.chunk) {
    return <p className="p-6 text-sm text-muted-foreground">Select a recording.</p>;
  }
  return <ChunkDetail {...props} chunk={props.chunk} />;
}

function HighlightedTranscript({ text, query, label }: { text: string; query: string; label?: string }) {
  const ref = useRef<HTMLParagraphElement>(null);
  const segments = highlightSegments(text, query);
  const hasMatch = segments.some((part) => part.match);
  useEffect(() => {
    if (!hasMatch) return;
    ref.current?.querySelector("mark")?.scrollIntoView({ block: "nearest" });
  }, [hasMatch, text, query]);
  return (
    <p ref={ref} className="transcript max-w-3xl leading-relaxed" aria-label={label}>
      {segments.length ? segments.map((part, index) => part.match
        ? <mark key={index} className="rounded-sm bg-primary/20 text-inherit">{part.text}</mark>
        : <span key={index}>{part.text}</span>) : "No transcript text."}
    </p>
  );
}

function AudioOptions({
  events,
  chunks,
  onPlayItem,
  onOpenSource,
}: {
  events: SpeechEvent[];
  chunks: Chunk[];
  onPlayItem?: (item: PlayableItem, span?: SpanRequest | null) => void;
  onOpenSource?: (id: string, started?: string) => void;
}) {
  if (!events.length) return null;
  return (
    <details aria-label="Audio options" className="max-w-3xl rounded-xl border p-3">
      <summary className="touch-target cursor-pointer text-sm font-medium">Audio options · {events.length} speech {events.length === 1 ? "clip" : "clips"}</summary>
      <div className="grid gap-3 pt-2">
      <p className="text-xs text-muted-foreground">Speech clips skip silence and use their own clock. The source recording is unchanged.</p>
      {events.map((event) => {
        const sources = eventSources(event, chunks);
        const item: PlayableItem = toPlayableEvent(event);
        item.label = eventTimeRange(event);
        item.preferredSource = "original";
        return (
          <div key={event.id} className="grid gap-2 border-t pt-2">
            <p className="text-sm">{eventTimeRange(event)}</p>
            <p className="text-sm text-muted-foreground">{derivedScopeCopy(sources.length)}</p>
            <ul className="grid gap-1" aria-label="Included source recordings">
              {sources.map((source) => (
                <li key={source.id}>
                  <button type="button" disabled={!source.present && !source.started} className="touch-target text-left text-sm underline disabled:no-underline disabled:text-muted-foreground" onClick={() => onOpenSource?.(source.id, source.started)}>
                    {source.label}{source.present ? "" : " · not in this day"}
                  </button>
                </li>
              ))}
            </ul>
            <div className="flex flex-wrap gap-2">
              <Button type="button" size="sm" variant="outline" className="touch-target" disabled={!event.audio_playable || !onPlayItem} onClick={() => onPlayItem?.(item, null)}>Play speech-only</Button>
              <Button type="button" size="sm" variant="outline" className="touch-target" disabled={!event.enhanced_playable || !onPlayItem} onClick={() => onPlayItem?.({ ...item, preferredSource: "enhanced" }, null)}>Play enhanced</Button>
            </div>
          </div>
        );
      })}
      </div>
    </details>
  );
}

function SourceTranscripts({
  members,
  query,
  onPlayItem,
  onOpenSource,
}: {
  members: Chunk[];
  query: string;
  onPlayItem?: (item: PlayableItem, span?: SpanRequest | null) => void;
  onOpenSource?: (id: string) => void;
}) {
  return (
    <div className="grid max-w-3xl gap-4">
      {members.map((chunk) => (
        <section key={chunk.id} className="grid gap-2 border-t pt-3" aria-label={chunk.started_local || "Source recording"}>
          <h3 className="text-base font-medium">{chunk.started_local || chunk.started || "Recording"}</h3>
          <p className="text-xs text-muted-foreground">{Number(chunk.duration || 0).toFixed(0)} seconds · source boundary</p>
          <div className="flex flex-wrap gap-2">
            <Button type="button" size="sm" className="touch-target" disabled={!toPlayableChunk(chunk).audioPlayable} onClick={() => onPlayItem?.(toPlayableChunk(chunk), null)}>Play recording</Button>
            <Button type="button" size="sm" variant="outline" className="touch-target" onClick={() => onOpenSource?.(chunk.id)}>Speaker naming</Button>
          </div>
          {transcriptState(chunk) ? <p role="status" className="text-sm">{transcriptState(chunk)}</p> : null}
          {audioRetentionLabel(chunk) ? <p role="status" className="text-sm">Audio no longer retained</p> : null}
          <HighlightedTranscript text={chunk.transcript || ""} query={query} />
        </section>
      ))}
    </div>
  );
}

function EventContext({ event, payload, query = "", onPlayItem, onOpenSource }: DetailProps & { event: SpeechEvent }) {
  const chunks = payload?.chunks || [];
  const sources = eventSources(event, chunks);
  const members = sources.map((source) => chunks.find((chunk) => chunk.id === source.id)).filter((chunk): chunk is Chunk => !!chunk);
  return (
    <article className="h-full min-w-0 overflow-y-auto p-4 pb-8 [overflow-wrap:anywhere] md:p-8" tabIndex={-1}>
      <div className="grid min-w-0 max-w-3xl gap-4">
        <header>
          <h2 className="text-xl font-semibold tracking-tight">Speech-only clip</h2>
          <p className="text-sm text-muted-foreground">{eventTimeRange(event)}</p>
        </header>
        <p className="text-sm">{derivedScopeCopy(sources.length)}</p>
        <AudioOptions events={[event]} chunks={chunks} onPlayItem={onPlayItem} onOpenSource={onOpenSource} />
        {members.length ? <SourceTranscripts members={members} query={query} onPlayItem={onPlayItem} onOpenSource={onOpenSource} /> : <p className="text-sm text-muted-foreground">Source transcripts for this clip are not in the loaded day.</p>}
      </div>
    </article>
  );
}

function GroupDetail({ group, payload, query = "", client, onPlayItem, onOpenSource, onReload, readOnly }: DetailProps & { group: { block: DisplayBlock; members: Chunk[] } }) {
  const saved = group.block.source === "manual";
  return (
    <article id="recording-detail" className="h-full min-w-0 overflow-y-auto p-4 pb-8 [overflow-wrap:anywhere] md:p-8" tabIndex={-1}>
      <div className="grid min-w-0 max-w-3xl gap-4">
        <header>
          <h2 className="text-xl font-semibold tracking-tight">{saved && group.block.title ? group.block.title : saved ? "Saved group" : "Suggested group"}</h2>
          <p className="text-sm text-muted-foreground">{saved ? "Saved group" : "Suggested group, not an established conversation"} · {eventRange(group.block)}</p>
          {group.block.day_portion ? <p className="text-xs text-muted-foreground">This day shows part of the saved boundaries.</p> : null}
        </header>
        {group.block.summary_state === "ready" && group.block.summary ? <p className="text-sm">{group.block.summary}</p> : null}
        <BoundaryEditor block={group.block} payload={payload} client={client} onReload={onReload} readOnly={readOnly} />
        <SourceTranscripts members={group.members} query={query} onPlayItem={onPlayItem} onOpenSource={onOpenSource} />
      </div>
    </article>
  );
}

function BoundaryEditor({
  block,
  payload,
  client,
  onReload,
  readOnly,
}: {
  block: DisplayBlock;
  payload: DayPayload | null;
  client: ApiClient;
  onReload: () => Promise<boolean> | boolean | void;
  readOnly?: boolean;
}) {
  const manual = block.source === "manual";
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState(manual ? block.title || "" : "");
  const [start, setStart] = useState(block.start_chunk_id || "");
  const [end, setEnd] = useState(block.end_chunk_id || "");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [conflict, setConflict] = useState(false);
  const writeBusy = useRef(false);
  const options = (payload?.chunks || []).filter((chunk) => chunk.started_local || chunk.started);
  useEffect(() => {
    setTitle(manual ? block.title || "" : "");
    setStart(block.start_chunk_id || "");
    setEnd(block.end_chunk_id || "");
    setError("");
    setConflict(false);
    setSaving(false);
    setOpen(false);
  }, [block.id, block.revision, block.title, block.start_chunk_id, block.end_chunk_id, manual]);

  async function save() {
    if (readOnly || writeBusy.current || conflict || saving) return;
    if (!start || !end || !title.trim()) {
      setError("Enter a title and both recording boundaries.");
      return;
    }
    writeBusy.current = true;
    setSaving(true);
    setError("");
    const body: Record<string, unknown> = {
      title,
      start_chunk_id: start,
      end_chunk_id: end,
      expected_revision: manual ? Number(block.revision || 0) : 0,
    };
    if (manual && block.id) body.id = block.id;
    try {
      await client.saveEventEdit(body);
      setOpen(false);
      await onReload();
    } catch (caught) {
      const uncertain = isUnknownSaveOutcome(caught);
      setConflict(uncertain || caught instanceof ApiError && caught.status === 409);
      setError(uncertain ? "The save outcome is uncertain. Reload current boundaries before another save; nothing was retried." : eventEditFailureCopy(caught));
    } finally {
      writeBusy.current = false;
      setSaving(false);
    }
  }

  return (
    <section className="grid gap-2" aria-label="Group boundaries">
      <p className="text-xs text-muted-foreground">Boundaries stay on the saved recordings. This does not create a combined playback.</p>
      <Button type="button" size="sm" variant="outline" className="touch-target w-fit" disabled={readOnly || saving || conflict} aria-label={manual ? "Edit event title and boundaries" : "Edit this suggested event"} onClick={() => setOpen((value) => !value)}>
        {open ? "Close" : "Edit title and boundaries"}
      </Button>
      {open ? (
        <form className="grid gap-2" onSubmit={(event) => { event.preventDefault(); void save(); }}>
          <label className="grid gap-1 text-xs text-muted-foreground">Title
            <Input aria-label="Event title" maxLength={80} value={title} onChange={(event) => setTitle(event.target.value)} />
          </label>
          <label className="grid gap-1 text-xs text-muted-foreground">Starts with
            <select aria-label="Starts with" className="h-8 max-w-full rounded-lg border bg-transparent px-2" value={start} onChange={(event) => setStart(event.target.value)}>
              <option value="">Choose a recording</option>
              {options.map((chunk) => <option key={chunk.id} value={chunk.id}>{chunk.started_local || chunk.started || "Recording"}</option>)}
            </select>
          </label>
          <label className="grid gap-1 text-xs text-muted-foreground">Ends with
            <select aria-label="Ends with" className="h-8 max-w-full rounded-lg border bg-transparent px-2" value={end} onChange={(event) => setEnd(event.target.value)}>
              <option value="">Choose a recording</option>
              {options.map((chunk) => <option key={chunk.id} value={chunk.id}>{chunk.started_local || chunk.started || "Recording"}</option>)}
            </select>
          </label>
          <Button type="submit" size="sm" className="touch-target w-fit" disabled={readOnly || saving || conflict}>{saving ? "Saving" : "Save"}</Button>
          {error ? <p role="alert" className="text-xs text-destructive">{error}</p> : null}
          {conflict ? <Button type="button" variant="outline" disabled={readOnly || saving} onClick={async () => { const ok = await onReload(); if (ok === true) setConflict(false); }}>Reload current boundaries</Button> : null}
        </form>
      ) : null}
    </section>
  );
}

function ChunkDetail({ chunk, payload, detailMode, client, onPlaySpan, onPlayFull, onPlayItem, onOpenSource, onReload, onPeople, onIdentityChange, reviewNotice, query = "", readOnly }: DetailProps & { chunk: Chunk }) {
  const [notice, setNotice] = useState("Mac processing stopped after repeated failures. The original recording is kept if available.");
  const [retrying, setRetrying] = useState(false);
  const [retryBlocked, setRetryBlocked] = useState(false);
  const [openKey, setOpenKey] = useState<string | null>(null);
  const [useSample, setUseSample] = useState(false);
  const [identityError, setIdentityError] = useState("");
  const [identityNotice, setIdentityNotice] = useState("");
  const [identityConflict, setIdentityConflict] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [pending, setPending] = useState(false);
  const [createdPerson, setCreatedPerson] = useState<Person | null>(null);
  const pendingRef = useRef(false);
  const returnFocus = useRef<HTMLElement | null>(null);
  const conflictRef = useRef(false);
  const chunkIdRef = useRef(chunk.id);
  const retryRef = useRef(false);
  chunkIdRef.current = chunk.id;
  const originalUnavailable = chunk.audio_playable === false;
  useEffect(() => {
    setRetryBlocked(false);
    setRetrying(false);
    retryRef.current = false;
    conflictRef.current = false;
    setIdentityConflict(false);
    setIdentityError("");
    setIdentityNotice("");
    setOpenKey(null);
    setUseSample(false);
    setCreating(false);
    setNewName("");
    setBriefOpen(null);
    setCreatedPerson(null);
    pendingRef.current = false;
    setPending(false);
  }, [chunk.id]);
  useEffect(() => {
    if (detailMode === "turns") document.getElementById("speaker-controls")?.focus();
  }, [detailMode, chunk.id]);
  const groups = Array.isArray(chunk.speakers) ? groupTurns(chunk) : [];
  const brief = groups.filter(briefStretch);
  const [briefOpen, setBriefOpen] = useState<boolean | null>(null);
  const briefExpanded = briefOpen ?? (brief.length === groups.length || brief.some((group) => group.key === openKey));
  const playerItem = toPlayableChunk(chunk);
  const expectedId = audioIdentity(playerItem, "original");
  const stateLabel = transcriptState(chunk);
  const events = relatedEvents(payload, chunk.id);

  function playRecording() {
    if (onPlayItem) onPlayItem(playerItem, null);
    else onPlayFull();
  }

  function playGroup(group: TurnGroup, fromStart: boolean) {
    if (!playerItem.audioPlayable || !expectedId) return;
    const bounds = groupBounds(group);
    const span = { key: group.key, start: bounds.start, end: bounds.end, expectedAudioId: expectedId, fromStart };
    if (onPlayItem) onPlayItem(playerItem, span);
    else onPlaySpan(span);
  }

  function lockIdentity(message: string) {
    conflictRef.current = true;
    setIdentityConflict(true);
    setIdentityError(message);
  }

  function claim(): boolean {
    if (readOnly || pendingRef.current || conflictRef.current) return false;
    pendingRef.current = true;
    setPending(true);
    return true;
  }

  function release() {
    pendingRef.current = false;
    setPending(false);
  }

  function publishSaved(result: IdentityMutationResult, requestedSample: boolean) {
    if (typeof result.change_id === "string" && result.change_id) onIdentityChange?.(result.change_id);
    const failed = enrollmentFailure(requestedSample, result);
    setIdentityNotice(failed || (requestedSample && result.enrollment?.enrolled ? "Saved. A voice sample was enrolled only because that request asked for one." : ""));
  }

  async function afterReload() {
    try {
      const ok = await onReload();
      if (ok === false) lockIdentity("Reload and read back before editing.");
    } catch {
      lockIdentity("Reload and read back before editing.");
    }
  }

  async function applyLabel(group: TurnGroup, personId: string | null, sample: boolean): Promise<boolean> {
    const requestedId = chunk.id;
    if (personId && fullyNamed(group, personId) && sample !== true) {
      setOpenKey(null);
      setUseSample(false);
      return true;
    }
    const built = groupLabelBody(group, personId, sample === true, chunk.identity_revision ?? payload?.identity_revision);
    if ("error" in built) {
      setIdentityError(built.error);
      return false;
    }
    setIdentityError("");
    try {
      const result = await client.labelTurns(built.body);
      if (chunkIdRef.current !== requestedId) return false;
      publishSaved(result, sample === true);
      setOpenKey(null);
      setUseSample(false);
      setCreatedPerson(null);
      await afterReload();
      return true;
    } catch (error) {
      if (chunkIdRef.current !== requestedId) return false;
      if (isUnknownSaveOutcome(error)) {
        lockIdentity(UNKNOWN_OUTCOME);
        return false;
      }
      setIdentityError(labelFailureCopy(error));
      if (error instanceof ApiError && error.status === 409) lockIdentity(labelFailureCopy(error));
      return false;
    }
  }

  async function label(group: TurnGroup, personId: string, sample: boolean) {
    if (!claim()) return;
    try {
      await applyLabel(group, personId, sample);
    } finally {
      release();
    }
  }

  function openName(group: TurnGroup, trigger: HTMLButtonElement) {
    returnFocus.current = trigger;
    setUseSample(false);
    setIdentityError("");
    setOpenKey(group.key);
  }

  return (
    <article id="recording-detail" className="h-full min-w-0 overflow-y-auto p-4 pb-8 [overflow-wrap:anywhere] md:p-8" tabIndex={-1}>
      <div className="grid min-w-0 max-w-3xl gap-4">
        <header>
          <h2 className="text-xl font-semibold tracking-tight">{chunk.started_local || "Recording"}</h2>
          <p className="text-sm text-muted-foreground">{Number(chunk.duration || 0).toFixed(1)} seconds</p>
        </header>
        <Button type="button" className="touch-target w-fit" disabled={!playerItem.audioPlayable} onClick={playRecording}>Play recording</Button>
        {identityNotice && <p role="status" className="text-sm">{identityNotice}</p>}
        {reviewNotice ? (
          <Alert>
            <AlertTitle>Speakers</AlertTitle>
            <AlertDescription>{reviewNotice}</AlertDescription>
          </Alert>
        ) : null}
        {chunk.status === "needs_attention" ? (
          <Alert variant="destructive">
            <AlertTitle>Transcript processing needs attention</AlertTitle>
            <AlertDescription>{originalUnavailable ? "Original audio is unavailable; this recording cannot be retried." : notice}</AlertDescription>
            <Button type="button" className="touch-target mt-2" disabled={readOnly || retrying || retryBlocked || originalUnavailable} onClick={async () => {
              if (readOnly || retryRef.current || retryBlocked || chunk.audio_playable === false) return;
              retryRef.current = true;
              setRetrying(true);
              try {
                await client.retryChunk(chunk.id);
                await onReload();
              } catch (error) {
                if (chunkIdRef.current !== chunk.id) return;
                setNotice(retryFailureCopy(error));
                if (error instanceof ApiError && error.status === 409) setRetryBlocked(true);
              } finally {
                retryRef.current = false;
                setRetrying(false);
              }
            }}>Retry processing</Button>
          </Alert>
        ) : stateLabel ? <p role="status" className="text-sm">{stateLabel}</p> : null}
        {audioRetentionLabel(chunk) ? <p role="status" className="text-sm">Audio no longer retained</p> : null}
        {(speakerPendingLabel(chunk) || (!Array.isArray(chunk.speakers) && chunk.status === "complete")) ? <p role="status" className="text-sm">Speaker analysis pending</p> : null}
        <HighlightedTranscript text={chunk.transcript || ""} query={query} label="Transcript" />
        <div id="speaker-controls" tabIndex={-1} className="grid gap-2 outline-none" aria-label="Speakers">
          <h3 className="text-sm font-medium">Speakers</h3>
          {Array.isArray(chunk.speakers) ? (
            <>
              {!groups.length ? <Badge variant="outline">Unknown · No speaker turns available</Badge> : null}
              {groups.filter((group) => !briefStretch(group)).map((group) => (
                <SpeakerRow key={group.key} group={group} open={openKey === group.key} audio={playerItem.audioPlayable} onName={openName} onPlay={() => playGroup(group, true)} />
              ))}
              {brief.length ? (
                <details className="w-full" open={briefExpanded} onToggle={(event) => setBriefOpen(event.currentTarget.open)}>
                  <summary className="cursor-pointer text-sm text-muted-foreground">{briefDisclosureLabel(brief.length)}</summary>
                  <div className="mt-2 grid gap-2">
                    {brief.map((group) => (
                      <SpeakerRow key={group.key} group={group} open={openKey === group.key} audio={playerItem.audioPlayable} onName={openName} onPlay={() => playGroup(group, true)} />
                    ))}
                  </div>
                </details>
              ) : null}
            </>
          ) : <p className="text-sm text-muted-foreground">Speaker names appear here when analysis is ready.</p>}
        </div>
        <AudioOptions events={events} chunks={payload?.chunks || []} onPlayItem={onPlayItem} onOpenSource={onOpenSource} />
        <details className="text-sm">
          <summary className="cursor-pointer text-muted-foreground">Processing details</summary>
          <p className="mt-2 text-muted-foreground">{processingBits(chunk)}</p>
        </details>
      </div>
      <IdentityDialog
        returnFocus={returnFocus}
        group={groups.find((group) => group.key === openKey) || null}
        people={createdPerson && !(payload?.people || []).some((person) => person.id === createdPerson.id) ? (payload?.people || []).concat(createdPerson) : (payload?.people || [])}
        useSample={useSample}
        error={identityError}
        notice={identityNotice}
        creating={creating}
        newName={newName}
        pending={pending}
        onUseSample={setUseSample}
        onNewName={setNewName}
        onCreating={setCreating}
        blocked={identityConflict || readOnly}
        onClose={() => { if (!pendingRef.current) setOpenKey(null); }}
        onLabel={label}
        onCreate={async (group, name) => {
          if (!claim()) return;
          let person = createdPerson;
          try {
            if (!person) {
              try {
                person = await client.createPerson(name);
              } catch (error) {
                if (isUnknownSaveOutcome(error)) lockIdentity(UNKNOWN_OUTCOME);
                else {
                  setIdentityError(labelFailureCopy(error));
                  if (error instanceof ApiError && error.status === 409) lockIdentity(labelFailureCopy(error));
                }
                return;
              }
              if (chunkIdRef.current !== chunk.id) return;
              setCreatedPerson(person);
              onPeople(person);
            }
            const assigned = await applyLabel(group, person.id, useSample);
            if (!assigned) {
              setCreating(false);
              setIdentityNotice(CREATE_ASSIGN_NOTICE);
            }
          } finally {
            release();
          }
        }}
        onClear={async (group) => {
          if (!window.confirm(`Remove names from exactly ${group.turns.length} selected stretches? No voice sample will be enrolled.`)) return;
          if (!claim()) return;
          try {
            await applyLabel(group, null, false);
          } finally {
            release();
          }
        }}
        onReload={async () => {
          if (pendingRef.current) return;
          try {
            const ok = await onReload();
            if (ok !== true) return;
            conflictRef.current = false;
            setIdentityConflict(false);
            setIdentityError("");
          } catch {
            // Failed reload keeps the lock.
          }
        }}
      />
    </article>
  );
}

function SpeakerRow({ group, open, audio, onName, onPlay }: { group: TurnGroup; open: boolean; audio: boolean; onName: (group: TurnGroup, trigger: HTMLButtonElement) => void; onPlay: () => void }) {
  const info = groupLabel(group);
  const bounds = groupBounds(group);
  const start = Number.isFinite(bounds.start) ? bounds.start.toFixed(1) : "?";
  const end = Number.isFinite(bounds.end) ? bounds.end.toFixed(1) : "?";
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-2">
      <Button type="button" size="sm" className="wrapping-control touch-target h-auto max-w-full whitespace-normal break-words text-left" variant={info.tone === "suggested" ? "outline" : "secondary"} aria-expanded={open} aria-haspopup="dialog" aria-label={"Name speaker, " + start + " to " + end + " seconds"} onClick={(event) => onName(group, event.currentTarget)}>
        {info.label + " · " + start + "–" + end + "s"}
      </Button>
      <Button type="button" size="sm" variant="outline" className="touch-target" disabled={!audio} aria-label={"Play speaker, " + start + " to " + end + " seconds"} onClick={onPlay}>Play</Button>
    </div>
  );
}

function IdentityDialog({
  returnFocus,
  group,
  people,
  useSample,
  error,
  notice,
  pending = false,
  creating,
  newName,
  onUseSample,
  onNewName,
  onCreating,
  onClose,
  onLabel,
  onCreate,
  onClear,
  blocked,
  onReload,
}: {
  group: TurnGroup | null;
  people: Person[];
  useSample: boolean;
  error: string;
  notice?: string;
  pending?: boolean;
  creating: boolean;
  newName: string;
  blocked?: boolean;
  onUseSample: (value: boolean) => void;
  onNewName: (value: string) => void;
  onCreating: (value: boolean) => void;
  returnFocus: { current: HTMLElement | null };
  onClose: () => void;
  onLabel: (group: TurnGroup, personId: string, sample: boolean) => Promise<void>;
  onCreate: (group: TurnGroup, name: string) => Promise<void>;
  onClear: (group: TurnGroup) => Promise<void>;
  onReload: () => Promise<void>;
}) {
  const ready = group ? cleanSeconds(group) >= 5 : false;
  const sample = ready && useSample;
  return (
    <Dialog open={!!group} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent finalFocus={returnFocus}>
        <DialogHeader>
          <DialogTitle>Identify speaker</DialogTitle>
          <DialogDescription>Assign this stretch. Saving a voice sample stays off unless you check it.</DialogDescription>
        </DialogHeader>
        {group ? (
          <div className="grid gap-3">
            <label className="flex items-center gap-2 text-sm">
              <Checkbox checked={sample} disabled={!ready || pending || blocked} onCheckedChange={(checked) => onUseSample(ready && checked === true)} />
              Save a voice sample
            </label>
            <p className="text-xs text-muted-foreground">
              {ready
                ? sample
                  ? "Tagging this stretch also saves a voice sample."
                  : "Leave this unchecked to name the stretch without saving a voice sample."
                : "This piece is under 5 seconds of one clean voice, so it can be named but not saved as a voice sample."}
            </p>
            {!people.length ? <p className="text-sm">No people yet. Create a person here to name this stretch. This does not restore names from older recordings.</p> : null}
            {creating ? (
              <form className="grid gap-2" onSubmit={(event) => { event.preventDefault(); if (!pending && !blocked && newName.trim()) void onCreate(group, newName.trim()); }}>
                <Input aria-label="New person name" disabled={pending || blocked} value={newName} placeholder="Name" onChange={(event) => onNewName(event.target.value)} />
                <div className="flex justify-end gap-2">
                  <Button type="button" disabled={pending} variant="outline" onClick={() => onCreating(false)}>Back</Button>
                  <Button type="submit" disabled={pending || blocked || !newName.trim()}>Create and assign</Button>
                </div>
              </form>
            ) : (
              <div className="grid gap-1">
                {people.map((person) => (
                  <Button key={person.id} type="button" variant="outline" className="wrapping-control touch-target justify-start whitespace-normal break-words" disabled={blocked || pending} onClick={() => void onLabel(group, person.id, sample)}>{person.name}</Button>
                ))}
                <Button type="button" variant="ghost" className="touch-target justify-start" disabled={blocked || pending} onClick={() => onCreating(true)}>New person</Button>
                {group.turns.some((turn) => turn.person_id) ? (
                  <Button type="button" variant="ghost" className="touch-target justify-start" disabled={blocked || pending} onClick={() => void onClear(group)}>Remove names from these stretches</Button>
                ) : null}
                {blocked ? <Button type="button" disabled={pending} variant="outline" className="touch-target" onClick={() => void onReload()}>Reload before editing</Button> : null}
              </div>
            )}
            <p className="text-sm" aria-live="polite">{error || "Tap a name to assign this stretch."}</p>
            {notice && <p role="status" className="text-sm">{notice}</p>}
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
