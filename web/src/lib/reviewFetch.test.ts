import { describe, expect, it } from "vitest";
import { REVIEW_ATTEMPT_CAP, reviewFetchPlan, shouldApplyReview } from "./reviewFetch";

describe("review fetch cap", () => {
  it("caps retries per chunk and ignores another chunk's count", () => {
    const first = reviewFetchPlan(null, "chunk-a", { hasSpeakers: false, voicePending: false, complete: true });
    expect(first.action).toBe("fetch");
    expect(first.delayMs).toBe(0);
    expect(first.next.attempts).toBe(1);
    const other = reviewFetchPlan(first.next, "chunk-b", { hasSpeakers: false, voicePending: true, complete: true });
    expect(other.next).toEqual({ chunkId: "chunk-b", attempts: 1 });
    let record = first.next;
    for (let attempt = 2; attempt <= REVIEW_ATTEMPT_CAP; attempt += 1) {
      const next = reviewFetchPlan(record, "chunk-a", { hasSpeakers: false, voicePending: true, complete: true });
      expect(next.action).toBe("fetch");
      record = next.next;
    }
    const blocked = reviewFetchPlan(record, "chunk-a", { hasSpeakers: false, voicePending: true, complete: true });
    expect(blocked.action).toBe("fail");
    expect(blocked.message).toContain("Refresh");
    const pending = reviewFetchPlan(record, "chunk-a", { hasSpeakers: true, voicePending: true, complete: true });
    expect(pending.action).toBe("fail");
    expect(pending.message).toContain("pending");
  });

  it("does not apply an aborted or replaced read", () => {
    expect(shouldApplyReview("a", "a", false)).toBe(true);
    expect(shouldApplyReview("a", "b", false)).toBe(false);
    expect(shouldApplyReview("a", "a", true)).toBe(false);
  });
});
