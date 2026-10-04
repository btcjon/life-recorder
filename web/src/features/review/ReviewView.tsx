import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useEffect, useRef, useState } from "react";
import { ApiError, identityFailureCopy, isAbortError, type ApiClient } from "../../api/client";
import type { Person, ReviewCandidate, ReviewDiagnostics } from "../../api/types";
import { canEnrollSample, filterReview, revisionReady } from "../../lib/identity";
import { formatTime } from "../../lib/recordings";
import { CONFLICT_COPY, FAILED_RELOAD_LOCK, UNKNOWN_OUTCOME, enrollmentFailure, isUnknownSaveOutcome } from "../identitySafety";

export type ReviewPlayRequest = {
  turnId: string;
  chunkId: string;
  started: number;
  ended: number;
  audioUsable: boolean;
};

export function ReviewView({
  client,
  people,
  onPlay,
  onOpenRecording,
  onIdentityChange,
}: {
  client: ApiClient;
  people: Person[];
  onPlay: (request: ReviewPlayRequest) => void;
  onOpenRecording: (target: { chunkId: string; startedAt?: string }) => void;
  onIdentityChange?: (changeId: string | null) => void;
}) {
  const [items, setItems] = useState<ReviewCandidate[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("These names are suggestions until you confirm one.");
  const [conflict, setConflict] = useState(false);
  const [diagnostics, setDiagnostics] = useState<ReviewDiagnostics | null>(null);
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [samples, setSamples] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const readSeq = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const revisionRef = useRef<number | undefined>(undefined);
  const cursorRef = useRef<string | null>(null);
  const conflictRef = useRef(false);
  const busyRef = useRef(false);
  const diagSeq = useRef(0);
  const mountedRef = useRef(true);

  function lock(message: string) {
    conflictRef.current = true;
    setConflict(true);
    setError(message);
  }

  function clearLock() {
    conflictRef.current = false;
    setConflict(false);
    setError("");
  }

  function claim(turnId: string): boolean {
    if (busyRef.current || conflictRef.current) return false;
    busyRef.current = true;
    setBusy(turnId);
    return true;
  }

  function release() {
    busyRef.current = false;
    setBusy(null);
  }

  async function load(reset: boolean): Promise<boolean> {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const seq = ++readSeq.current;
    const signal = controller.signal;
    setLoading(true);
    const current = () => seq === readSeq.current && !signal.aborted && mountedRef.current;
    try {
      const page = await client.speakerReview(reset ? null : cursorRef.current, signal);
      if (!current()) return false;
      const pageRevision = page.identity_revision;
      const incoming = page.items || [];
      if (!reset) {
        const same = revisionReady(revisionRef.current) && revisionReady(pageRevision) && revisionRef.current === pageRevision;
        if (!same) {
          lock(CONFLICT_COPY);
          return false;
        }
        setItems((existing) => existing.concat(incoming));
        cursorRef.current = page.next_cursor || null;
        setCursor(cursorRef.current);
        return true;
      }
      setItems(incoming);
      cursorRef.current = page.next_cursor || null;
      setCursor(cursorRef.current);
      setChoices({});
      setSamples({});
      if (revisionReady(pageRevision)) {
        revisionRef.current = pageRevision;
        clearLock();
      } else if (conflictRef.current) {
        lock(FAILED_RELOAD_LOCK);
        return false;
      }
      setNotice(incoming.length ? "These names are suggestions until you confirm one." : "No uncertain speaker turns.");
      return revisionReady(pageRevision) || !conflictRef.current;
    } catch (caught) {
      if (!current() || isAbortError(caught) || signal.aborted) return false;
      setError(conflictRef.current ? FAILED_RELOAD_LOCK : "Couldn't load speaker review.");
      return false;
    } finally {
      if (current()) setLoading(false);
    }
  }

  useEffect(() => {
    mountedRef.current = true;
    void load(true);
    return () => {
      mountedRef.current = false;
      abortRef.current?.abort();
      readSeq.current += 1;
    };
    // Diagnostics stay manual. Opening Review must not recompute them.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client]);

  async function loadDiagnostics() {
    if (busyRef.current) return;
    const seq = ++diagSeq.current;
    setError("");
    try {
      const next = await client.reviewDiagnostics();
      if (seq !== diagSeq.current || !mountedRef.current) return;
      setDiagnostics(next);
    } catch (caught) {
      if (isAbortError(caught) || seq !== diagSeq.current) return;
      setError("Couldn't load voice-check counts.");
    }
  }

  async function confirm(item: ReviewCandidate) {
    const personId = choices[item.turn_id] || "";
    if (!personId || !claim(item.turn_id)) return;
    if (!revisionReady(revisionRef.current)) {
      lock(CONFLICT_COPY);
      release();
      return;
    }
    const useSample = canEnrollSample(Number(item.clean_seconds || 0), !!item.audio_usable) && samples[item.turn_id] === true;
    try {
      const result = await client.labelTurn(item.turn_id, {
        person_id: personId,
        use_sample: useSample,
        revision: revisionRef.current,
      });
      if (!mountedRef.current) return;
      if (typeof result.change_id === "string") onIdentityChange?.(result.change_id);
      const failed = enrollmentFailure(useSample, result);
      setNotice(failed ? "Confirmed. " + failed : "Confirmed. That name is a human label.");
      const reread = await load(true);
      if (!reread && mountedRef.current && !conflictRef.current) lock(FAILED_RELOAD_LOCK);
    } catch (caught) {
      if (!mountedRef.current) return;
      if (isUnknownSaveOutcome(caught)) {
        lock(UNKNOWN_OUTCOME);
        return;
      }
      setError(identityFailureCopy(caught));
      if (caught instanceof ApiError && caught.status === 409) lock(CONFLICT_COPY);
    } finally {
      release();
    }
  }

  async function reject(item: ReviewCandidate, personId: string) {
    if (!personId || !claim(item.turn_id)) return;
    try {
      await client.rejectSuggestion(item.turn_id, personId);
      if (!mountedRef.current) return;
      setItems((current) => current.map((row) => row.turn_id === item.turn_id ? {
        ...row,
        suggestions: (row.suggestions || []).filter((suggestion) => suggestion.person_id !== personId),
        stored_person_id: row.stored_person_id === personId ? null : row.stored_person_id,
        stored_name: row.stored_person_id === personId ? null : row.stored_name,
      } : row));
      setChoices((current) => current[item.turn_id] === personId ? { ...current, [item.turn_id]: "" } : current);
      setNotice("Suggestion rejected. The turn was not relabeled.");
    } catch (caught) {
      if (!mountedRef.current) return;
      if (isUnknownSaveOutcome(caught)) {
        lock(UNKNOWN_OUTCOME);
        return;
      }
      if (caught instanceof ApiError && caught.status === 409) {
        lock(CONFLICT_COPY);
        return;
      }
      setError("Could not reject that suggestion.");
    } finally {
      release();
    }
  }

  const shown = filterReview(items, query);
  const locked = conflict || !!busy;

  return (
    <div className="h-full overflow-auto p-4 pb-28 md:p-8">
      <div className="mx-auto grid max-w-3xl gap-4">
        <header className="grid gap-2">
          <h2 className="text-2xl font-semibold tracking-tight">Review</h2>
          <p className="text-sm" aria-live="polite">{error || (loading ? "Loading unconfirmed speaker turns." : notice)}</p>
          <div className="flex flex-wrap gap-2">
            <Input aria-label="Filter review" placeholder="Filter loaded turns" value={query} onChange={(event) => setQuery(event.target.value)} className="w-56" disabled={!!busy} />
            <Button type="button" variant="outline" className="touch-target" disabled={!!busy} onClick={() => void loadDiagnostics()}>Voice-check counts</Button>
            <Button type="button" variant="outline" className="touch-target" disabled={loading || !!busy} onClick={() => void load(true)}>Reload review</Button>
          </div>
          {diagnostics ? (
            <p className="text-xs text-muted-foreground">Held-out groups {Number(diagnostics.groups || 0)} · rank 1 {Number(diagnostics.rank1_correct_groups || 0)} · auto acceptances {Number(diagnostics.rejection_reasons?.accepted || 0)}</p>
          ) : null}
        </header>
        {conflict ? (
          <Alert variant="destructive">
            <AlertTitle>Reload required</AlertTitle>
            <AlertDescription>{error || CONFLICT_COPY}</AlertDescription>
          </Alert>
        ) : null}
        {!shown.length && !loading && !error ? <p>No uncertain speaker turns.</p> : null}
        {shown.map((item) => {
          const start = Number(item.started);
          const end = Number(item.ended);
          const canSample = canEnrollSample(Number(item.clean_seconds || 0), !!item.audio_usable);
          const choice = choices[item.turn_id] || "";
          const suggestions = item.suggestions || [];
          const suggestedIds = new Set(suggestions.map((suggestion) => suggestion.person_id));
          return (
            <Card key={item.turn_id} data-review-turn={item.turn_id}>
              <CardHeader>
                <CardTitle className="text-base">Unconfirmed speaker</CardTitle>
                <Badge variant="outline">{item.label_source === "automatic" && item.stored_name ? "Unconfirmed automatic guess" : "Unconfirmed suggestion"}</Badge>
              </CardHeader>
              <CardContent className="grid gap-3">
                <p className="text-sm text-muted-foreground">{(Number.isFinite(start) ? start.toFixed(1) : "?") + "–" + (Number.isFinite(end) ? end.toFixed(1) : "?") + "s"}{item.audio_usable ? "" : " · audio unavailable"}</p>
                {item.label_source === "automatic" && item.stored_name ? <p>Automatic guess: {item.stored_name} · not confirmed</p> : null}
                {suggestions.map((suggestion) => (
                  <div key={suggestion.person_id} className="flex flex-wrap items-center justify-between gap-2 text-sm">
                    <span>{(suggestion.name || "Unnamed") + " · unconfirmed suggestion"}</span>
                    <Button type="button" size="sm" variant="outline" className="touch-target" aria-label={"Reject suggestion " + (suggestion.name || "person")} disabled={locked} onClick={() => void reject(item, suggestion.person_id)}>Reject</Button>
                  </div>
                ))}
                <label className="grid gap-1 text-sm">Identify as
                  <select aria-label="Person for this speaker turn" className="h-8 rounded-lg border bg-transparent px-2" value={choice} disabled={locked} onChange={(event) => setChoices((current) => ({ ...current, [item.turn_id]: event.target.value }))}>
                    <option value="">Choose a name</option>
                    {suggestions.map((suggestion) => (
                      <option key={suggestion.person_id} value={suggestion.person_id}>{(suggestion.name || "Unnamed") + " · unconfirmed"}</option>
                    ))}
                    {people.filter((person) => person.id && !suggestedIds.has(person.id)).map((person) => (
                      <option key={person.id} value={person.id}>{person.name || "Unnamed person"}</option>
                    ))}
                  </select>
                </label>
                <label className="flex items-center gap-2 text-sm">
                  <input type="checkbox" aria-label="Save a voice sample for this confirmation" checked={canSample && samples[item.turn_id] === true} disabled={!canSample || locked} onChange={(event) => setSamples((current) => ({ ...current, [item.turn_id]: event.target.checked === true }))} />
                  {canSample ? "Save a voice sample" : "Not enough clean speech to save a voice sample"}
                </label>
                <div className="flex flex-wrap gap-2">
                  <Button type="button" className="touch-target" aria-label="Confirm selected name" disabled={!choice || locked} onClick={() => void confirm(item)}>Confirm</Button>
                  <Button type="button" variant="outline" className="touch-target" aria-label="Play this speaker span" disabled={!item.audio_usable || !!busy} onClick={() => onPlay({
                    turnId: item.turn_id,
                    chunkId: item.chunk_id,
                    started: Number(item.started) || 0,
                    ended: Number(item.ended) || 0,
                    audioUsable: !!item.audio_usable,
                  })}>Play</Button>
                  <Button type="button" variant="outline" className="touch-target" aria-label="Open the source recording" disabled={!!busy} onClick={() => onOpenRecording({ chunkId: item.chunk_id, startedAt: item.clip_started })}>Open recording</Button>
                </div>
                <p className="text-xs text-muted-foreground tabular-nums">0:00 / {formatTime(Math.max(0, end - start))}</p>
              </CardContent>
            </Card>
          );
        })}
        {cursor && !conflict ? <Button type="button" variant="outline" className="touch-target w-fit" disabled={loading || !!busy} onClick={() => void load(false)}>More</Button> : null}
      </div>
    </div>
  );
}
