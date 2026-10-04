import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useCallback, useEffect, useRef, useState } from "react";
import { contextError, placeBody, type ContextClient, type ContextEvidence, type Place, type PlacesReport, type TimelineItem, type TimelineReport, type TopicSuggestion } from "./contracts";
import { ObservedContext } from "./ObservedContext";

type Edit = (path: string, body: unknown, confirm?: string) => Promise<void>;

export function ContextView({ client, day, onDay }: { client: ContextClient; day: string; onDay: (day: string) => void }) {
  const [timeline, setTimeline] = useState<TimelineReport | null>(null);
  const [places, setPlaces] = useState<PlacesReport | null>(null);
  const [evidence, setEvidence] = useState<ContextEvidence | null>(null);
  const [error, setError] = useState("");
  const [observedError, setObservedError] = useState("");
  const [busy, setBusy] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [message, setMessage] = useState("");
  const [selected, setSelected] = useState<Record<string, number>>({});
  const [title, setTitle] = useState("");
  const generation = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const mutationBusy = useRef(false);
  const activeDay = useRef(day);
  activeDay.current = day;
  const load = useCallback(async () => {
    const current = ++generation.current;
    controller.current?.abort();
    const abort = new AbortController(); controller.current = abort;
    setError("");
    try {
      const query = day ? `?day=${encodeURIComponent(day)}` : "";
      const [nextTimeline, nextPlaces] = await Promise.all([
        client.getJson<TimelineReport>(`/v1/timeline${query}`, abort.signal), client.getJson<PlacesReport>("/v1/places", abort.signal)]);
      if (abort.signal.aborted || current !== generation.current) return false;
      setTimeline(nextTimeline); setPlaces(nextPlaces); setSelected({}); setConflict(false);
      return true;
    } catch (e) { if (!abort.signal.aborted && current === generation.current) setError(contextError(e).text); return false; }
  }, [client, day]);
  useEffect(() => { const requests = generation; setTimeline(null); setPlaces(null); void load(); return () => { requests.current++; controller.current?.abort(); }; }, [load]);
  useEffect(() => {
    let live = true, active = false;
    const abort = new AbortController();
    setEvidence(null); setObservedError("");
    const poll = async () => {
      if (!live || active || document.visibilityState !== "visible") return;
      active = true;
      try {
        const next = await client.getJson<ContextEvidence>(`/v1/context-evidence${day ? `?day=${encodeURIComponent(day)}` : ""}`, abort.signal);
        if (live) { setEvidence(next); setObservedError(""); }
      } catch { if (live) setObservedError("Observed context is unavailable. Last successful evidence, if shown, may be stale."); }
      finally { active = false; }
    };
    void poll(); const interval = window.setInterval(() => { void poll(); }, 5000);
    const visible = () => { void poll(); }; document.addEventListener("visibilitychange", visible);
    return () => { live = false; abort.abort(); clearInterval(interval); document.removeEventListener("visibilitychange", visible); };
  }, [client, day]);
  const edit: Edit = async (path, body, confirmation) => {
    if (mutationBusy.current || conflict) return;
    if (confirmation && !window.confirm(confirmation)) return;
    mutationBusy.current = true; setBusy(true); setMessage(""); setError("");
    const requestedDay = day;
    try {
      await client.postJson(path, body);
      if (activeDay.current !== requestedDay) return;
      if (await load()) setMessage("Saved. Reloaded current revisions.");
      else { setConflict(true); setError("Saved, but readback failed. Reload current state before another action."); }
    } catch (e) {
      if (activeDay.current !== requestedDay) return;
      const result = contextError(e);
      const uncertain = !e || typeof e !== "object" || !("status" in e) || typeof e.status !== "number" || e.status >= 500 || e.status === 408;
      setConflict(result.conflict || uncertain);
      setError(uncertain ? "The save outcome is uncertain. Reload current state before another action. Nothing was retried automatically." : result.text);
    }
    finally { mutationBusy.current = false; setBusy(false); }
  };
  const locked = busy || conflict;
  const selectedEntries = Object.entries(selected);
  return <div id="context-view" className="h-full overflow-y-auto p-3 md:p-5"><div className="mx-auto max-w-5xl space-y-5">
    <div className="flex flex-wrap items-center gap-3"><Label>Day<Input type="date" aria-label="Context day" value={day} onChange={e => onDay(e.target.value)} /></Label><Button variant="outline" onClick={() => void load()} disabled={busy}>Reload current revisions</Button></div>
    <p className="text-sm text-muted-foreground">Phone context and human event tags have separate sources. Mac output IO is not proof of audible sound or YouTube/X playback.</p>
    {error && <p role="alert" className="text-destructive">{error}</p>}{message && <p role="status">{message}</p>}
    <ObservedContext report={evidence} error={observedError} />
    <Card><CardHeader><CardTitle>Timeline</CardTitle></CardHeader><CardContent className="space-y-4 break-words [overflow-wrap:anywhere]">
      <p>Manual markers use the same meeting system as the phone. A later start closes an earlier open meeting conservatively.</p>
      {!timeline && <p>{error ? "Timeline is unavailable." : "Loading timeline…"}</p>}
      {(timeline?.devices || []).map(phone => <section key={phone.device_id} className="space-y-2 rounded-md border p-3"><h3>{phone.label}</h3><div className="flex flex-wrap gap-2">{["start", ...(phone.active_meeting_id ? ["end"] : [])].map(kind => <Button key={kind} disabled={locked} onClick={() => void edit("/v1/meeting-markers", { version: 1, event_id: crypto.randomUUID(), meeting_id: kind === "start" ? crypto.randomUUID() : phone.active_meeting_id, device_id: phone.device_id, kind, occurred_at: new Date().toISOString() })}>{kind === "start" ? "Start meeting" : "End meeting"}</Button>)}</div></section>)}
      <Label>Merged event title<Input value={title} onChange={e => setTitle(e.target.value)} /></Label>
      <Button disabled={locked || selectedEntries.length !== 2 || !title.trim()} onClick={() => void edit("/v1/timeline/merge", { ids: selectedEntries.map(([id]) => id), revisions: selectedEntries.map(([, rev]) => rev), title }, "Merge exactly these two selected topics?")}>Merge two selected topics</Button>
      {(timeline?.items || []).map(item => <section key={item.id} className="space-y-3 rounded-md border p-3"><h3 className="font-medium">{item.title || item.label || "Event"}</h3><p>{item.source} · {item.started_at} → {item.ended_at || "open"} · {item.status || "saved"}</p>{item.source === "human_topic" && <>
        <Label><Checkbox checked={item.id in selected} disabled={locked} onCheckedChange={checked => setSelected(current => { const next = { ...current }; if (checked === true) next[item.id] = item.revision; else delete next[item.id]; return next; })} />Select topic to merge</Label>
        {(item.chunk_ids || []).length >= 2 && <TopicSplit item={item} edit={edit} locked={locked} />}
      </>}</section>)}
      {(timeline?.suggestions || []).map(item => <Suggestion key={item.id} item={item} edit={edit} locked={locked} />)}
      {timeline?.truncated && <p>This timeline is limited. Select a day to narrow it.</p>}
    </CardContent></Card>
    <Card><CardHeader><CardTitle>Known places</CardTitle></CardHeader><CardContent className="space-y-4">
      <p>Phone locations describe the phone, not a speaker. Unknown, stale and overlapping matches remain explicit.</p>
      {!places && <p>{error ? "Places are unavailable." : "Loading places…"}</p>}
      {places?.last_known && <section className="space-y-2"><p>Phone last observed: {places.last_known.place?.name || "Unknown"} · {places.last_known.status} · age {Math.round(places.last_known.age_seconds || 0)} seconds · accuracy {places.last_known.accuracy_m == null ? "unknown" : `${Math.round(places.last_known.accuracy_m)} m`}</p>{places.last_known.observation_id && <Button disabled={locked} variant="outline" onClick={() => void edit("/v1/places/clear", { observation_id: places.last_known?.observation_id }, "Forget this exact phone observation and its derived labels? This cannot be undone.")}>Forget this phone observation and its labels</Button>}</section>}
      <PlaceEditor edit={edit} locked={locked} />
      {(places?.places || []).map(place => <PlaceEditor key={`${place.id}:${place.revision}`} place={place} edit={edit} locked={locked} />)}
      <h3 className="font-medium">Manual event place tags</h3>
      {(places?.events || []).map(event => <EventTag key={`${event.event_id}:${event.revision}`} event={event} places={places?.places || []} edit={edit} locked={locked} />)}
    </CardContent></Card>
  </div></div>;
}

function TopicSplit({ item, edit, locked }: { item: TimelineItem; edit: Edit; locked: boolean }) {
  const [before, setBefore] = useState(item.chunk_ids?.[1] || "");
  const [left, setLeft] = useState(item.title || ""), [right, setRight] = useState(item.title || "");
  return <div className="space-y-2"><Label>Split before recording<select className="w-full rounded-md border bg-background p-2" aria-label="Split before recording" value={before} onChange={e => setBefore(e.target.value)}>{item.chunk_ids?.slice(1).map(id => <option key={id} value={id}>Split before recording {id.slice(0, 8)}</option>)}</select></Label><Label>First topic title<Input value={left} onChange={e => setLeft(e.target.value)} /></Label><Label>Second topic title<Input value={right} onChange={e => setRight(e.target.value)} /></Label><p>Preview: first topic “{left}”; second topic “{right}”, beginning at recording {before.slice(0, 8)}.</p><Button disabled={locked || !before} onClick={() => void edit("/v1/timeline/split", { id: item.id, revision: item.revision, before_clip_id: before, titles: [left, right] }, "Save this split at the selected recording boundary?")}>Save split preview</Button></div>;
}
function Suggestion({ item, edit, locked }: { item: TopicSuggestion; edit: Edit; locked: boolean }) {
  const [title, setTitle] = useState(item.title);
  return <section className="space-y-2 rounded-md border p-3"><h3>Unconfirmed topic suggestion</h3><Label>Suggested topic title<Input value={title} onChange={e => setTitle(e.target.value)} /></Label><p>Source recordings {item.start_chunk_id.slice(0, 8)} → {item.end_chunk_id.slice(0, 8)} · {item.model}</p><div className="flex flex-wrap gap-2"><Button disabled={locked} onClick={() => void edit("/v1/timeline/accept", { id: item.id, revision: item.revision, title })}>Accept topic boundaries</Button><Button variant="outline" disabled={locked} onClick={() => void edit("/v1/timeline/reject", { id: item.id, revision: item.revision })}>Reject topic suggestion</Button></div></section>;
}
function PlaceEditor({ place, edit, locked }: { place?: Place; edit: Edit; locked: boolean }) {
  const [values, setValues] = useState({ name: place?.name || "", latitude: place ? String(place.latitude) : "", longitude: place ? String(place.longitude) : "", radius: String(place?.radius_m || 100) });
  const [error, setError] = useState("");
  return <section className="space-y-2 rounded-md border p-3"><h3 className="font-medium">{place ? "Edit known place" : "Add known place"}</h3><div className="grid gap-3 sm:grid-cols-2">{([['name', 'Place name'], ['latitude', 'Latitude'], ['longitude', 'Longitude'], ['radius', 'Radius in metres']] as const).map(([key, label]) => <Label key={key}>{label}<Input type={key === "name" ? "text" : "number"} step="any" value={values[key]} onChange={e => setValues({ ...values, [key]: e.target.value })} /></Label>)}</div>{error && <p role="alert">{error}</p>}<div className="flex flex-wrap gap-2"><Button disabled={locked} onClick={() => { try { const body = placeBody(values, place); setError(""); void edit("/v1/places/save", body); } catch (e) { setError(contextError(e).text); } }}>Save place</Button>{place && <Button disabled={locked} variant="outline" onClick={() => void edit("/v1/places/delete", { id: place.id, revision: place.revision }, "Delete this known place and its derived labels? This cannot be undone.")}>Delete place and its derived labels</Button>}</div></section>;
}
function EventTag({ event, places, edit, locked }: { event: NonNullable<PlacesReport['events']>[number]; places: Place[]; edit: Edit; locked: boolean }) {
  const [placeId, setPlaceId] = useState(event.place_id || "");
  return <section className="space-y-2 rounded-md border p-3"><p>{event.title || event.event_id}</p><Label>Event place tag<select aria-label="Event place tag" className="w-full rounded-md border bg-background p-2" value={placeId} onChange={e => setPlaceId(e.target.value)}><option value="">No manual place tag</option>{places.map(place => <option key={place.id} value={place.id}>{place.name}</option>)}</select></Label><Button disabled={locked} onClick={() => void edit("/v1/places/tag", { event_id: event.event_id, place_id: placeId || null, revision: event.revision })}>Save explicit event tag</Button></section>;
}
