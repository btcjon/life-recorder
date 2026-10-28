import { describe, expect, it } from "vitest";
import { healthDetails } from "./HealthView";

describe("health presentation", () => {
  it('shows engine unavailability, a stalled backlog and last success prominently', () => {
    const lines = healthDetails({processing: {engine_ready: false, stalled: true, backlog_count: 4, backlog_duration_seconds: 120, last_completed_at: 100}});
    expect(lines[0]).toContain('Last success:');
    expect(lines).toContain('Engine: unavailable');
    expect(lines).toContain('Processing stalled');
    expect(lines).toContain('Backlog: 4 recordings · 2 minutes');
  });
  it("distinguishes delayed work and partial storage accounting", () => {
    const text = healthDetails({ processing: { pending_count: 2, attention_count: 1, oldest_pending_age_seconds: 700 }, storage: { original_audio_bytes: 1048576, audio_usage_complete: false, low_space: true } }).join(" ");
    expect(text).toContain("Pending: 2"); expect(text).toContain("Needs attention: 1"); expect(text).toContain("Processing delayed"); expect(text).toContain("partial count"); expect(text).toContain("Low disk space"); expect(text).toContain("Source revision unavailable");
  });
});
