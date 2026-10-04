import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useEffect, useRef, useState } from "react";
import { ApiError, labelFailureCopy, retryFailureCopy, type ApiClient } from "../../api/client";
import type { Chunk, DayPayload, IdentityMutationResult, Person, SpeechEvent } from "../../api/types";
import { audioIdentity, type PlayableItem } from "../../lib/playback";
import {
  briefDisclosureLabel,
  briefStretch,
  cleanSeconds,
  formatTime,
  groupBounds,
  groupLabel,
  groupTranscript,
  groupTurns,
  processingBits,
  type TurnGroup,
} from "../../lib/recordings";
import { fullyNamed, groupLabelBody } from "../../lib/identity";
import { CREATE_ASSIGN_NOTICE, UNKNOWN_OUTCOME, enrollmentFailure, isUnknownSaveOutcome } from "../identitySafety";

type DetailProps = {
  payload: DayPayload | null;
  loading: boolean;
  chunk: Chunk | null;
  event: SpeechEvent | null;
  detailMode: "transcript" | "turns";
  onDetailMode: (mode: "transcript" | "turns") => void;
  client: ApiClient;
  playerItem: PlayableItem | null;
  onPlaySpan: (span: { key: string; start: number; end: number; expectedAudioId: string; fromStart: boolean }) => void;
  onPlayFull: () => void;
  reviewNotice?: string;
  onReload: () => Promise<boolean> | boolean | void;
  onPeople: (person: Person) => void;
  onIdentityChange?: (changeId: string | null) => void;
};

export function RecordingDetail(props: DetailProps) {
  if (props.loading && !props.payload) {
    return <div className="grid gap-3 p-6"><Skeleton className="h-8 w-48" /><Skeleton className="h-24 w-full" /></div>;
  }
  if (props.event) return <EventDetail event={props.event} />;
  if (!props.chunk) {
    return <p className="p-6 text-sm text-muted-foreground">Select a recording.</p>;
  }
  return <ChunkDetail {...props} chunk={props.chunk} />;
}

function EventDetail({ event }: { event: SpeechEvent }) {
  return (
    <article className="grid max-w-3xl gap-3 p-4 md:p-8">
      <h2 className="text-2xl font-semibold tracking-tight">{event.started_local || "Speech event"}</h2>
      <p className="text-sm text-muted-foreground">{Number(event.playable_duration || event.duration || 0).toFixed(1)} seconds playable</p>
      <p>This is a derived speech event. Use the player below to hear it, or choose Original or Enhanced.</p>
      <p className="text-sm text-muted-foreground">{(event.chunks || []).length} source recording{(event.chunks || []).length === 1 ? "" : "s"} · Full transcripts and speaker assignment are available from the source recordings.</p>
    </article>
  );
}

function ChunkDetail({ chunk, payload, detailMode, onDetailMode, client, playerItem, onPlaySpan, onPlayFull, onReload, onPeople, onIdentityChange, reviewNotice }: DetailProps & { chunk: Chunk }) {
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
  const groups = Array.isArray(chunk.speakers) ? groupTurns(chunk) : [];
  const brief = groups.filter(briefStretch);
  const [briefOpen, setBriefOpen] = useState<boolean | null>(null);
  const briefExpanded = briefOpen ?? (brief.length === groups.length || brief.some((group) => group.key === openKey));
  const expectedId = playerItem ? audioIdentity(playerItem, "original") : null;

  function playGroup(group: TurnGroup, fromStart: boolean) {
    if (!chunk.audio_playable || !expectedId) return;
    const bounds = groupBounds(group);
    onPlaySpan({ key: group.key, start: bounds.start, end: bounds.end, expectedAudioId: expectedId, fromStart });
  }

  function lockIdentity(message: string) {
    conflictRef.current = true;
    setIdentityConflict(true);
    setIdentityError(message);
  }

  function claim(): boolean {
    if (pendingRef.current || conflictRef.current) return false;
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

  return (
    <article id="recording-detail" className="h-full min-w-0 overflow-y-auto p-4 pb-8 [overflow-wrap:anywhere] md:p-8" tabIndex={-1}>
      <div className="grid min-w-0 max-w-3xl gap-4">
        <header>
          <h2 className="text-2xl font-semibold tracking-tight">{chunk.started_local || "Recording"}</h2>
          <p className="text-sm text-muted-foreground">{Number(chunk.duration || 0).toFixed(1)} seconds</p>
        </header>
        {identityNotice && <p role="status" className="text-sm">{identityNotice}</p>}
        {reviewNotice ? (
          <Alert>
            <AlertTitle>Speakers</AlertTitle>
            <AlertDescription>{reviewNotice}</AlertDescription>
          </Alert>
        ) : null}
        {chunk.status === "needs_attention" ? (
          <Alert variant="destructive">
            <AlertTitle>Processing needs attention</AlertTitle>
            <AlertDescription>{originalUnavailable ? "Original audio is unavailable; this recording cannot be retried." : notice}</AlertDescription>
            <Button type="button" className="touch-target mt-2" disabled={retrying || retryBlocked || originalUnavailable} onClick={async () => {
              if (retryRef.current || retryBlocked || chunk.audio_playable === false) return;
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
        ) : null}
        <div className="flex flex-wrap gap-2">
          <Tabs value={detailMode} onValueChange={(value) => onDetailMode(value === "turns" ? "turns" : "transcript")}>
            <TabsList>
              <TabsTrigger value="transcript">Transcript</TabsTrigger>
              <TabsTrigger value="turns">Speaker turns</TabsTrigger>
            </TabsList>
          </Tabs>
          <Button type="button" variant="outline" className="touch-target" disabled={!chunk.audio_playable} onClick={onPlayFull}>Play full recording</Button>
        </div>
        {!Array.isArray(chunk.speakers) ? (
          <>
            <p className="text-sm text-muted-foreground">{chunk.status === "complete" ? "Loading speakers…" : "Speaker analysis has not finished."}</p>
            <p className="transcript">{chunk.transcript || "No transcript yet."}</p>
          </>
        ) : detailMode === "transcript" ? (
          <>
            <div className="flex flex-wrap gap-2" aria-label="Speakers">
              {!groups.length ? <Badge variant="outline">Unknown · No speaker turns available</Badge> : null}
              {groups.filter((group) => !briefStretch(group)).map((group) => (
                <SpeakerPill key={group.key} group={group} open={openKey === group.key} onOpen={(trigger) => { returnFocus.current = trigger; playGroup(group, true); setUseSample(false); setIdentityError(""); setOpenKey(group.key); }} />
              ))}
              {brief.length ? (
                <details className="w-full" open={briefExpanded} onToggle={(event) => setBriefOpen(event.currentTarget.open)}>
                  <summary className="cursor-pointer text-sm text-muted-foreground">{briefDisclosureLabel(brief.length)}</summary>
                  <div className="mt-2 flex flex-wrap gap-2">
                    {brief.map((group) => (
                      <SpeakerPill key={group.key} group={group} open={openKey === group.key} onOpen={(trigger) => { returnFocus.current = trigger; playGroup(group, true); setUseSample(false); setIdentityError(""); setOpenKey(group.key); }} />
                    ))}
                  </div>
                </details>
              ) : null}
            </div>
            <p className="transcript">{chunk.transcript || "No transcript yet."}</p>
          </>
        ) : (
          <div className="grid gap-3">
            {!groups.length ? <p>No speaker turns for this recording.</p> : null}
            {groups.map((group) => {
              const bounds = groupBounds(group);
              return (
                <section key={group.key} className="rounded-xl border p-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <SpeakerPill group={group} open={openKey === group.key} onOpen={(trigger) => { returnFocus.current = trigger; playGroup(group, true); setUseSample(false); setIdentityError(""); setOpenKey(group.key); }} />
                    <div className="flex gap-2">
                      <Button type="button" size="sm" className="touch-target" disabled={!chunk.audio_playable} aria-label="Play this speaker clip" onClick={() => playGroup(group, false)}>Play</Button>
                      <Button type="button" size="sm" variant="outline" className="touch-target" disabled={!chunk.audio_playable} aria-label="Replay this speaker clip" onClick={() => playGroup(group, true)}>Replay</Button>
                    </div>
                  </div>
                  <SpanMeter groupKey={group.key} start={bounds.start} end={bounds.end} />
                  <p className="transcript mt-2 border-l-2 pl-3">{groupTranscript(group) || "No transcript for this clip."}</p>
                </section>
              );
            })}
          </div>
        )}
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
        blocked={identityConflict}
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

function SpanMeter({ groupKey, start, end }: { groupKey: string; start: number; end: number }) {
  const [elapsed, setElapsed] = useState(0);
  const duration = Math.max(0, end - start);
  useEffect(() => {
    const onTick = (event: Event) => {
      const detail = (event as CustomEvent<{ time: number; clipKey: string | null; clipStart: number | null }>).detail;
      if (!detail || detail.clipKey !== groupKey || detail.clipStart == null) {
        setElapsed(0);
        return;
      }
      setElapsed(Math.max(0, Math.min(duration, detail.time - detail.clipStart)));
    };
    window.addEventListener("life-recorder-tick", onTick);
    return () => window.removeEventListener("life-recorder-tick", onTick);
  }, [duration, groupKey]);
  return (
    <div className="mt-2 flex items-center gap-2">
      <Progress value={duration ? (elapsed / duration) * 100 : 0} aria-label="Speaker clip progress" className="flex-1" />
      <span className="text-xs text-muted-foreground tabular-nums">{formatTime(elapsed)} / {formatTime(duration)}</span>
    </div>
  );
}

function SpeakerPill({ group, open, onOpen }: { group: TurnGroup; open: boolean; onOpen: (trigger: HTMLButtonElement) => void }) {
  const info = groupLabel(group);
  const bounds = groupBounds(group);
  const start = Number.isFinite(bounds.start) ? bounds.start.toFixed(1) : "?";
  const end = Number.isFinite(bounds.end) ? bounds.end.toFixed(1) : "?";
  return (
    <Button type="button" size="sm" className="touch-target h-auto max-w-full whitespace-normal break-words text-left" variant={info.tone === "suggested" ? "outline" : "secondary"} aria-expanded={open} aria-haspopup="dialog" aria-label={"Play " + start + " to " + end + " seconds"} onClick={(event) => onOpen(event.currentTarget)}>
      {info.label + " · " + start + "–" + end + "s"}
    </Button>
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
              <Checkbox checked={sample} disabled={!ready || pending || blocked} aria-label="Save a voice sample" onCheckedChange={(checked) => onUseSample(ready && checked === true)} />
              Save a voice sample
            </label>
            <p className="text-xs text-muted-foreground">
              {ready
                ? sample
                  ? "Tagging this stretch also saves a voice sample."
                  : "Leave this unchecked to name the stretch without saving a voice sample."
                : "This piece is under 5 seconds of one clean voice, so it can be named but not saved as a voice sample."}
            </p>
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
                  <Button key={person.id} type="button" variant="outline" className="touch-target justify-start whitespace-normal break-words" disabled={blocked || pending} onClick={() => void onLabel(group, person.id, sample)}>{person.name}</Button>
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
