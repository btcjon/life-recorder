import { describe, expect, it } from "vitest";
import { healthDetails } from "./HealthView";

describe("health presentation", () => {
  it("distinguishes delayed work and partial storage accounting", () => {
    const text = healthDetails({ processing: { pending_count: 2, attention_count: 1, oldest_pending_age_seconds: 700 }, storage: { original_audio_bytes: 1048576, audio_usage_complete: false, low_space: true } }).join(" ");
    expect(text).toContain("Pending: 2"); expect(text).toContain("Needs attention: 1"); expect(text).toContain("Processing delayed"); expect(text).toContain("partial count"); expect(text).toContain("Low disk space"); expect(text).toContain("Source revision unavailable");
  });
});
