import type { LabelTurnsBody, Person, ReviewCandidate } from "../api/types";
import type { TurnGroup } from "./recordings";

export function reasonText(code: string | undefined): string {
  const map: Record<string, string> = {
    "score_below_0.60": "Not a close enough match",
    "score_below_0.85": "Close, but not sure enough to name it",
    "margin_below_0.10": "Too similar to another person",
    need_2_samples: "Needs 2 voice samples",
    need_2_clips: "Needs samples from 2 recordings",
    need_10s: "Needs 10 seconds of confirmed voice",
    need_5s: "Needs 5 seconds of one clean voice",
    confirmed: "Confirmed",
    matched: "Suggested match",
  };
  return (code && map[code]) || "Needs a closer look";
}

export function enrollmentCopy(person: Person): string {
  const samples = Number(person.sample_count || 0);
  const seconds = Number(person.sample_seconds || 0).toFixed(1);
  const clips = Number(person.clip_count || 0);
  const stats = person.name + "'s " + samples + " samples/" + seconds + "s/" + clips + " clips";
  if (person.enrollment_ready) return "Ready to auto-tag matches · " + stats;
  const remaining = Math.max(0, 2 - samples);
  if (remaining === 1) return "One more confirmed voice sample needed for " + stats;
  if (remaining > 1) return remaining + " more confirmed voice samples needed for " + stats;
  const reasons = (person.enrollment_reasons || []).map((reason) => reasonText(reason)).join(". ");
  return (reasons || "Still learning this voice") + " · " + stats;
}

export function easternDay(value: string | number | null | undefined): string {
  if (value == null || value === "") return "";
  const date = typeof value === "number" ? new Date(value > 10_000_000_000 ? value : value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).slice(0, 10);
  try {
    return new Intl.DateTimeFormat("en-CA", {
      timeZone: "America/New_York",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(date);
  } catch {
    return String(value).slice(0, 10);
  }
}

export function revisionReady(revision: number | undefined): revision is number {
  return typeof revision === "number" && Number.isInteger(revision) && revision >= 0;
}

export function groupLabelBody(
  group: TurnGroup,
  personId: string | null,
  useSample: boolean,
  revision: number | undefined,
): { body: LabelTurnsBody } | { error: string } {
  const turnIds = group.turns.map((turn) => turn.id).filter((id): id is string => !!id);
  if (!turnIds.length) return { error: "No speaker turns to label." };
  if (!revisionReady(revision)) return { error: "Reload before editing." };
  const sample = useSample === true;
  if (personId == null && sample) return { error: "Removing names cannot save a voice sample." };
  return {
    body: {
      turn_ids: turnIds,
      person_id: personId,
      use_sample: sample,
      revision,
    },
  };
}

export function fullyNamed(group: TurnGroup, personId: string): boolean {
  return !!personId && group.turns.length > 0 && group.turns.every((turn) => turn.person_id === personId);
}

export function filterReview(items: ReviewCandidate[], query: string): ReviewCandidate[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return items;
  return items.filter((item) => {
    const names = (item.suggestions || []).map((suggestion) => suggestion.name || "");
    const hay = [item.stored_name, item.speaker_key, item.chunk_id, item.turn_id, ...names].join(" ").toLowerCase();
    return hay.includes(needle);
  });
}

export function reviewChoiceValue(stored: string | undefined): string {
  return stored || "";
}

export function canEnrollSample(cleanSeconds: number, audioUsable: boolean): boolean {
  return cleanSeconds >= 5 && audioUsable;
}
