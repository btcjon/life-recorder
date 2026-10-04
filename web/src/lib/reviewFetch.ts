export const REVIEW_ATTEMPT_CAP = 3;
export const REVIEW_RETRY_MS = 2000;

export type ReviewFetchRecord = {
  chunkId: string;
  attempts: number;
};

export function reviewFetchPlan(
  record: ReviewFetchRecord | null,
  chunkId: string,
  options: { hasSpeakers: boolean; voicePending: boolean; complete: boolean },
): { action: "skip" | "fetch" | "fail"; delayMs: number; next: ReviewFetchRecord; message: string } {
  const same = record && record.chunkId === chunkId ? record : null;
  if (!options.complete || (options.hasSpeakers && !options.voicePending)) {
    return {
      action: "skip",
      delayMs: 0,
      next: same || { chunkId, attempts: 0 },
      message: "",
    };
  }
  const attempts = same ? same.attempts : 0;
  if (attempts >= REVIEW_ATTEMPT_CAP) {
    return {
      action: "fail",
      delayMs: 0,
      next: { chunkId, attempts },
      message: options.hasSpeakers
        ? "Speaker analysis is still pending. Refresh to try again."
        : "Couldn't load speakers for this recording. Refresh to try again.",
    };
  }
  return {
    action: "fetch",
    delayMs: attempts === 0 ? 0 : REVIEW_RETRY_MS,
    next: { chunkId, attempts: attempts + 1 },
    message: "",
  };
}

export function shouldApplyReview(requestedId: string, selectedId: string | null, aborted: boolean): boolean {
  return !aborted && selectedId === requestedId;
}
