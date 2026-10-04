import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useEffect, useRef, useState } from "react";
import { ApiError, identityFailureCopy, isAbortError, type ApiClient } from "../../api/client";
import type { ClusterReport, IdentityProfile, IdentityProfiles, Person } from "../../api/types";
import { enrollmentCopy, reasonText, revisionReady } from "../../lib/identity";
import { formatTime } from "../../lib/recordings";
import { CONFLICT_COPY, FAILED_RELOAD_LOCK, UNKNOWN_OUTCOME, enrollmentFailure, isUnknownSaveOutcome } from "../identitySafety";

type ConfirmState = {
  title: string;
  body: string;
  run: () => Promise<void>;
};

export function PeopleView({
  client,
  catalog,
  onOpenRecording,
  onChanged,
  onIdentityChange,
  lastChangeId,
}: {
  client: ApiClient;
  catalog: Person[];
  onOpenRecording: (target: { chunkId: string; startedAt?: string }) => void;
  onChanged: () => void;
  onIdentityChange?: (changeId: string | null) => void;
  lastChangeId?: string | null;
}) {
  const [profiles, setProfiles] = useState<IdentityProfiles | null>(null);
  const [clusters, setClusters] = useState<ClusterReport | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [conflict, setConflict] = useState(false);
  const [changeId, setChangeId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const [names, setNames] = useState<Record<string, string>>({});
  const [groupIds, setGroupIds] = useState<string[]>([]);
  const [selectedTurns, setSelectedTurns] = useState<Record<string, string[]>>({});
  const [personChoice, setPersonChoice] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const readSeq = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const conflictRef = useRef(false);
  const busyRef = useRef(false);
  const mountedRef = useRef(true);

  function lock(message: string) {
    conflictRef.current = true;
    setConflict(true);
    setError(message);
  }

  function claim(): boolean {
    if (busyRef.current || conflictRef.current) return false;
    busyRef.current = true;
    setBusy(true);
    return true;
  }

  function release() {
    busyRef.current = false;
    setBusy(false);
  }

  function resetChoices() {
    setGroupIds([]);
    setSelectedTurns({});
    setPersonChoice({});
    setNames({});
  }

  async function load(): Promise<boolean> {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const seq = ++readSeq.current;
    const signal = controller.signal;
    setLoading(true);
    const current = () => seq === readSeq.current && !signal.aborted && mountedRef.current;
    try {
      const [nextProfiles, nextClusters] = await Promise.all([
        client.identityProfiles(signal),
        client.speakerClusters(signal),
      ]);
      if (!current()) return false;
      const matched = revisionReady(nextProfiles.revision) && revisionReady(nextClusters.revision) && nextProfiles.revision === nextClusters.revision;
      setProfiles(nextProfiles);
      setClusters(nextClusters);
      resetChoices();
      if (!matched) {
        lock(CONFLICT_COPY);
        return false;
      }
      conflictRef.current = false;
      setConflict(false);
      setError("");
      return true;
    } catch (caught) {
      if (!current() || isAbortError(caught) || signal.aborted) return false;
      setError(conflictRef.current ? FAILED_RELOAD_LOCK : (caught instanceof ApiError ? caught.message : "Could not load people."));
      return false;
    } finally {
      if (current()) setLoading(false);
    }
  }

  useEffect(() => {
    mountedRef.current = true;
    void load();
    return () => {
      mountedRef.current = false;
      abortRef.current?.abort();
      readSeq.current += 1;
    };
    // Load once per visit. Refresh is explicit so a stale read cannot be retried blindly.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client]);

  const revision = profiles?.revision;
  const people: IdentityProfile[] = profiles?.people?.length ? profiles.people : catalog.map((person) => ({ ...person, samples: [] }));
  const activeChangeId = lastChangeId !== undefined ? lastChangeId : changeId;
  const blocked = conflict || !revisionReady(revision) || busy;

  function ask(title: string, body: string, run: () => Promise<void>) {
    if (busyRef.current || conflictRef.current) return;
    setConfirm({ title, body, run });
  }

  function publish(action: string, nextId: string | undefined) {
    if (action === "undo") {
      setChangeId(null);
      onIdentityChange?.(null);
      return;
    }
    if (typeof nextId === "string" && nextId) {
      setChangeId(nextId);
      onIdentityChange?.(nextId);
    }
  }

  async function mutate(action: "remove_sample" | "undo" | "merge" | "split" | "name" | "label", body: Record<string, unknown>) {
    if (!claim()) {
      if (!busyRef.current) lock(CONFLICT_COPY);
      return;
    }
    if (!revisionReady(revision)) {
      lock(CONFLICT_COPY);
      release();
      return;
    }
    try {
      const result = await client.identityMutation(action, { ...body, revision });
      if (!mountedRef.current) return;
      publish(action, result.change_id);
      const requested = body.use_sample === true;
      const failed = enrollmentFailure(requested, result);
      if (failed) setNotice(failed);
      else if (result.enrollment?.enrolled) setNotice("Saved. A voice sample was enrolled only because that request asked for one.");
      else setNotice("Identity edit saved. Human labels and sample choices remain separate.");
      const reread = await load();
      if (!reread && mountedRef.current && !conflictRef.current) lock(FAILED_RELOAD_LOCK);
      onChanged();
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
      setError(identityFailureCopy(caught));
    } finally {
      release();
    }
  }

  async function rename(person: Person) {
    const name = (names[person.id] ?? person.name).trim();
    if (!name || name === person.name || !claim()) return;
    try {
      const result = await client.renamePerson(person.id, name);
      if (!mountedRef.current) return;
      const published = (result as Person & { change_id?: string }).change_id;
      publish("name", published);
      setNotice("Name saved. This does not enroll a voice sample.");
      const reread = await load();
      if (!reread && mountedRef.current && !conflictRef.current) lock(FAILED_RELOAD_LOCK);
      onChanged();
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
      setError(identityFailureCopy(caught));
    } finally {
      release();
    }
  }

  const gate = clusters?.gate;

  return (
    <div className="h-full overflow-auto p-4 md:p-8">
      <div className="mx-auto grid max-w-3xl gap-4">
        <header>
          <h2 className="text-2xl font-semibold tracking-tight">People</h2>
          <p className="text-sm text-muted-foreground">Samples are explicit opt-ins. Removing one withdraws its voice evidence and refreshes dependent automatic labels. Grouping and naming never enroll samples.</p>
        </header>
        {error ? (
          <Alert variant="destructive">
            <AlertTitle>People</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
            <Button type="button" className="touch-target mt-2" disabled={busy || loading} onClick={() => void load()}>Reload identities</Button>
          </Alert>
        ) : null}
        {notice ? <p className="text-sm" aria-live="polite">{notice}</p> : null}
        {loading && !profiles ? <p className="text-sm text-muted-foreground">Loading people…</p> : null}
        {!loading && !people.length && !error ? <p>No people yet. Identify a speaker from a recording to add someone.</p> : null}
        {activeChangeId ? (
          <Button type="button" variant="outline" className="touch-target w-fit" disabled={blocked} onClick={() => ask(
            "Undo latest identity edit",
            "This restores the previous labels and sample choices. It does not delete a person.",
            () => mutate("undo", { change_id: activeChangeId }),
          )}>Undo latest identity edit</Button>
        ) : null}
        {people.map((person) => (
          <Card key={person.id}>
            <CardHeader>
              <CardTitle className="text-base">{person.name}</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-3">
              <p className="text-sm">{enrollmentCopy(person)}</p>
              <p className="text-xs text-muted-foreground">
                {Number(person.sample_count || 0)} accepted samples · {Number(person.clip_count || 0)} recordings · {Math.round(Number(person.sample_seconds || 0))} clean seconds · {person.enrollment_ready ? "Ready" : (person.enrollment_reasons || []).map((reason) => reasonText(reason)).join(", ") || "Not ready"}
              </p>
              <form className="flex flex-wrap gap-2" onSubmit={(event) => { event.preventDefault(); void rename(person); }}>
                <Input aria-label={"Rename " + person.name} value={names[person.id] ?? person.name} disabled={blocked} onChange={(event) => setNames((current) => ({ ...current, [person.id]: event.target.value }))} />
                <Button type="submit" variant="outline" className="touch-target" disabled={blocked}>Rename</Button>
              </form>
              {(person.samples || []).map((sample) => (
                <div key={sample.id} className="rounded-lg border p-2 text-sm">
                  <p>Recording {String(sample.chunk_id || "missing").slice(0, 8)} · {Math.round(Number(sample.duration || 0))} seconds · {sample.status || "accepted"}{sample.legacy ? " · legacy" : ""} · accepted {sample.confirmed_at ? new Date(sample.confirmed_at * 1000).toLocaleString() : "unknown time"}</p>
                  <div className="mt-2 flex flex-wrap gap-2">
                    <Button type="button" size="sm" variant="outline" className="touch-target" disabled={!sample.chunk_id || busy} onClick={() => sample.chunk_id && onOpenRecording({ chunkId: sample.chunk_id, startedAt: sample.clip_started })}>Open sample recording</Button>
                    <Button type="button" size="sm" variant="outline" className="touch-target" disabled={blocked} onClick={() => ask(
                      "Remove voice sample",
                      "This withdraws the sample's voice evidence and refreshes dependent automatic labels. The person is not deleted, and no new sample is enrolled.",
                      () => mutate("remove_sample", { sample_id: sample.id }),
                    )}>Remove voice sample</Button>
                  </div>
                </div>
              ))}
            </CardContent>
          </Card>
        ))}
        <section className="grid gap-3" aria-label="Anonymous groups">
          <h3 className="text-lg font-medium">Voice profiles and anonymous groups</h3>
          <p className="text-sm text-muted-foreground">{gate?.enabled ? "Evaluated group suggestions still require a human decision." : "Cross-recording suggestions are off: " + (gate?.reason || "evaluation unavailable")}</p>
          <p className="text-sm" aria-live="polite">{groupIds.length < 2 ? "Select at least two anonymous groups to preview a merge. Merging does not name anyone or save a voice sample." : groupIds.length + " groups selected. Preview: only these groups merge. No voice samples will be saved."}</p>
          <Button type="button" variant="outline" className="touch-target w-fit" disabled={blocked || groupIds.length < 2} onClick={() => ask(
            "Merge selected anonymous groups",
            "Merge " + groupIds.length + " anonymous groups. This does not label speakers or save a voice sample.",
            () => mutate("merge", { track_ids: groupIds }),
          )}>Merge selected anonymous groups</Button>
          {(clusters?.clusters || []).map((cluster) => {
            const members = cluster.members || [];
            const picked = selectedTurns[cluster.id] || [];
            return (
              <Card key={cluster.id}>
                <CardContent className="grid gap-2 pt-4">
                  <label className="flex items-center gap-2 text-sm">
                    <input type="checkbox" aria-label={"Select group " + cluster.id.slice(0, 8)} checked={groupIds.includes(cluster.id)} disabled={blocked} onChange={(event) => {
                      setGroupIds((current) => event.target.checked ? current.concat(cluster.id) : current.filter((id) => id !== cluster.id));
                    }} />
                    Select group {cluster.id.slice(0, 8)}
                  </label>
                  <p className="text-sm">{(cluster.status || "open") + " · " + members.length + " stretches. Preview and select exact stretches below."}</p>
                  <p className="text-xs text-muted-foreground" aria-live="polite">{picked.length ? picked.length + " selected stretches; only these labels will change. No voice samples will be saved." : "No stretches selected"}</p>
                  {members.map((member) => (
                    <div key={member.turn_id} className="flex flex-wrap items-center gap-2 text-sm">
                      <label className="flex items-center gap-2">
                        <input type="checkbox" aria-label={"Recording " + member.chunk_id.slice(0, 8) + " · " + formatTime(Number(member.started || 0)) + "–" + formatTime(Number(member.ended || 0)) + " · " + (member.person_id ? "stored name" : "anonymous")} checked={picked.includes(member.turn_id)} disabled={blocked} onChange={(event) => {
                          setSelectedTurns((current) => {
                            const list = current[cluster.id] || [];
                            return { ...current, [cluster.id]: event.target.checked ? list.concat(member.turn_id) : list.filter((id) => id !== member.turn_id) };
                          });
                        }} />
                        Recording {member.chunk_id.slice(0, 8)} · {formatTime(Number(member.started || 0))}–{formatTime(Number(member.ended || 0))} · {member.person_id ? "stored name" : "anonymous"}
                      </label>
                      <Button type="button" size="sm" variant="ghost" className="touch-target" disabled={busy} onClick={() => onOpenRecording({ chunkId: member.chunk_id })}>Open this recording</Button>
                    </div>
                  ))}
                  <label className="grid gap-1 text-xs text-muted-foreground">Name selected stretches
                    <select aria-label="Name selected stretches" className="h-8 rounded-lg border bg-transparent px-2" value={personChoice[cluster.id] || ""} disabled={blocked} onChange={(event) => setPersonChoice((current) => ({ ...current, [cluster.id]: event.target.value }))}>
                      <option value="">Choose a person</option>
                      {(profiles?.people || catalog).map((person) => <option key={person.id} value={person.id}>{person.name}</option>)}
                    </select>
                  </label>
                  <div className="flex flex-wrap gap-2">
                    <Button type="button" size="sm" className="touch-target" disabled={blocked} onClick={() => {
                      if (!picked.length || !personChoice[cluster.id]) {
                        setError("Select stretches and a person first.");
                        return;
                      }
                      void mutate("name", { track_id: cluster.id, turn_ids: picked, person_id: personChoice[cluster.id] });
                    }}>Name selected stretches</Button>
                    <Button type="button" size="sm" variant="outline" className="touch-target" disabled={blocked} onClick={() => {
                      if (!picked.length) {
                        setError("Select stretches first.");
                        return;
                      }
                      ask("Remove selected labels", "Only the selected stretches lose their names. No voice sample is saved or enrolled.", () => mutate("label", { turn_ids: picked, person_id: null, use_sample: false }));
                    }}>Remove selected labels</Button>
                    <Button type="button" size="sm" variant="outline" className="touch-target" disabled={blocked} onClick={() => {
                      if (!picked.length || picked.length === members.length) {
                        setError("Select some, but not all, stretches.");
                        return;
                      }
                      ask("Split selected stretches", "Selected stretches move to a new anonymous group. No voice samples will be saved.", () => mutate("split", { track_id: cluster.id, turn_ids: picked }));
                    }}>Split selected stretches into new group</Button>
                  </div>
                </CardContent>
              </Card>
            );
          })}
          {(clusters?.proposals || []).map((proposal) => (
            <div key={proposal.id || proposal.track_ids.join("-")} className="rounded-lg border p-3 text-sm">
              <Badge variant="outline">Unconfirmed group suggestion</Badge>
              <p className="mt-2">Unconfirmed group suggestion · score {proposal.score} · margin {proposal.margin}. Accepting merges groups only. It does not enroll a sample.</p>
              <Button type="button" size="sm" variant="outline" className="touch-target mt-2" disabled={blocked} onClick={() => ask(
                "Accept this group merge",
                "Accept this unconfirmed group suggestion. It merges the groups and does not save a voice sample.",
                () => mutate("merge", { track_ids: proposal.track_ids }),
              )}>Accept this group merge</Button>
            </div>
          ))}
        </section>
      </div>
      <Dialog open={!!confirm} onOpenChange={(open) => { if (!open && !busy) setConfirm(null); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{confirm?.title || "Confirm"}</DialogTitle>
            <DialogDescription>{confirm?.body}</DialogDescription>
          </DialogHeader>
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" disabled={busy} onClick={() => setConfirm(null)}>Cancel</Button>
            <Button type="button" variant="destructive" autoFocus disabled={busy} onClick={() => {
              if (busyRef.current) return;
              const pending = confirm;
              setConfirm(null);
              if (pending) void pending.run();
            }}>Confirm</Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
