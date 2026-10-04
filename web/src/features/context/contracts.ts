export type ContextClient = {
  getJson: <T>(path: string, signal?: AbortSignal) => Promise<T>;
  postJson: <T>(path: string, body: unknown, signal?: AbortSignal) => Promise<T>;
};
export type Gap = { started_at: string; ended_at?: string; open?: boolean; duration_seconds?: number; uncertain?: boolean; uncertainties?: string[]; reason?: string };
export type EvidenceInterval = { signal: string; state: string; app?: string; started_at: string; ended_at?: string; effective_until?: string; fresh?: boolean; stale?: boolean; boundary_exact?: boolean; uncertain?: boolean; precise_transcript_association?: boolean; open_recording_gap?: boolean; title?: string; call_state?: string; reason?: string };
export type ContextEvidence = { statement?: string; browser_adapter?: { available?: boolean; note?: string }; freshness?: { label?: string; detail?: string }; open_capture_gaps?: Gap[]; capture_gaps?: Gap[]; intervals?: EvidenceInterval[]; truncated?: boolean };
export type TimelineItem = { id: string; revision: number; title?: string; label?: string; source: string; started_at: string; ended_at?: string; status?: string; chunk_ids?: string[] };
export type TopicSuggestion = { id: string; revision: number; title: string; start_chunk_id: string; end_chunk_id: string; model?: string };
export type TimelineReport = { devices?: { label: string; device_id: string; active_meeting_id?: string }[]; items?: TimelineItem[]; suggestions?: TopicSuggestion[]; truncated?: boolean };
export type Place = { id: string; revision: number; name: string; latitude: number; longitude: number; radius_m: number };
export type PlacesReport = { last_known?: { place?: { name: string }; status?: string; age_seconds?: number; accuracy_m?: number; observation_id?: string }; places?: Place[]; events?: { title?: string; event_id: string; place_id?: string; revision: number }[] };

export function placeBody(values: { name: string; latitude: string; longitude: string; radius: string }, place?: Place) {
  const latitude = Number(values.latitude), longitude = Number(values.longitude), radius = Number(values.radius);
  if (!values.name.trim() || !values.latitude.trim() || !values.longitude.trim() || !values.radius.trim()
      || !Number.isFinite(latitude) || Math.abs(latitude) > 90 || !Number.isFinite(longitude) || Math.abs(longitude) > 180 || !Number.isFinite(radius) || radius <= 0) {
    throw new Error("Enter a name, valid coordinates and a positive radius.");
  }
  return { ...(place ? { id: place.id, revision: place.revision } : {}), name: values.name.trim(), latitude, longitude, radius_m: radius };
}

export function contextError(error: unknown): { text: string; conflict: boolean } {
  const conflict = !!error && typeof error === "object" && "status" in error && error.status === 409;
  return { conflict, text: conflict ? "This information changed. Reload current revisions before saving again."
    : error instanceof Error ? error.message : "Context is unavailable. Refresh to try again." };
}
