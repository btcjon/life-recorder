// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ContextView } from "./ContextView";
import type { ContextClient } from "./contracts";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
function fixture() {
  const post = vi.fn(async (_path: string, _body: unknown) => ({}));
  const get = vi.fn(async (path: string) => {
    if (path.startsWith("/v1/context-evidence")) return { freshness: { label: "stale" }, browser_adapter: { available: false },
      open_capture_gaps: [{ started_at: "2026-10-04T12:00Z", open: true, uncertain: true, duration_seconds: 40, uncertainties: ["no_fresh_observer"] }],
      intervals: [{ signal: "browser_media", state: "unknown", started_at: "2026-10-04T12:01Z", title: "Synthetic <img src=x>", boundary_exact: false, stale: true, precise_transcript_association: false }] };
    if (path === "/v1/places") return { places: [{ id: "place", revision: 4, name: "Office", latitude: 0, longitude: -70, radius_m: 100 }], events: [{ event_id: "event", revision: 8, title: "Meeting" }] };
    return { devices: [{ label: "Synthetic phone", device_id: "device", active_meeting_id: "meeting" }],
      items: [{ id: "topic", revision: 7, title: "Topic", source: "human_topic", started_at: "noon", chunk_ids: ["one", "two"] }], suggestions: [{ id: "suggestion", revision: 3, title: "Draft", start_chunk_id: "one", end_chunk_id: "two" }] };
  });
  const client: ContextClient = { getJson: get as ContextClient['getJson'], postJson: post as ContextClient['postJson'] };
  return { client, post, get };
}
describe("Context view", () => {
  it("renders unknown/stale evidence as uncertainty, not silence or attribution", async () => {
    const { client, post } = fixture(); render(<ContextView client={client} day="2026-10-04" onDay={() => undefined} />);
    await screen.findByText("Synthetic <img src=x>");
    expect(document.querySelector("img")).toBeNull();
    expect(screen.getByText(/No fresh observer/)).toBeTruthy();
    expect(screen.getByText(/Clock skew excludes/)).toBeTruthy();
    expect(screen.getByText(/Mac output IO is not proof/)).toBeTruthy();
    expect(post).not.toHaveBeenCalled();
  });
  it("sends explicit topic revisions, then locks a conflict until reread", async () => {
    const { client, post, get } = fixture(); post.mockRejectedValueOnce({ status: 409 });
    render(<ContextView client={client} day="2026-10-04" onDay={() => undefined} />);
    fireEvent.click(await screen.findByRole("button", { name: "Accept topic boundaries" }));
    await screen.findByText(/Reload current revisions before saving again/);
    expect(post).toHaveBeenCalledExactlyOnceWith("/v1/timeline/accept", { id: "suggestion", revision: 3, title: "Draft" });
    expect((screen.getByRole("button", { name: "Reject topic suggestion" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Reload current revisions" }));
    await waitFor(() => expect((screen.getByRole("button", { name: "Reject topic suggestion" }) as HTMLButtonElement).disabled).toBe(false));
    expect(get.mock.calls.filter(([path]) => path.startsWith("/v1/timeline")).length).toBe(2);
  });
  it("requires confirmation for deletion and preserves exact target/revision", async () => {
    const { client, post } = fixture(); const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<ContextView client={client} day="2026-10-04" onDay={() => undefined} />);
    const button = await screen.findByRole("button", { name: "Delete place and its derived labels" });
    fireEvent.click(button); expect(post).not.toHaveBeenCalled();
    confirm.mockReturnValue(true); fireEvent.click(button);
    await waitFor(() => expect(post).toHaveBeenCalledExactlyOnceWith("/v1/places/delete", { id: "place", revision: 4 }));
  });
  it("keeps uncertain saves locked and never retries automatically", async () => {
    const { client, post } = fixture(); post.mockRejectedValueOnce(new TypeError("Network error"));
    render(<ContextView client={client} day="2026-10-04" onDay={() => undefined} />);
    fireEvent.click(await screen.findByRole("button", { name: "End meeting" }));
    await screen.findByText(/save outcome is uncertain/);
    expect(post).toHaveBeenCalledTimes(1);
    expect((screen.getByRole("button", { name: "Start meeting" }) as HTMLButtonElement).disabled).toBe(true);
  });
  it("validates place fields and sends no mutation for blanks", async () => {
    const { client, post } = fixture(); render(<ContextView client={client} day="2026-10-04" onDay={() => undefined} />);
    const heading = await screen.findByRole("heading", { name: "Add known place" });
    const section = within(heading.closest("section")!);
    fireEvent.click(section.getByRole("button", { name: "Save place" }));
    expect(section.getByRole("alert").textContent).toContain("valid coordinates"); expect(post).not.toHaveBeenCalled();
    fireEvent.change(section.getByLabelText("Place name"), { target: { value: "New place" } });
    fireEvent.change(section.getByLabelText("Latitude"), { target: { value: "0" } });
    fireEvent.change(section.getByLabelText("Longitude"), { target: { value: "10" } });
    fireEvent.click(section.getByRole("button", { name: "Save place" }));
    await waitFor(() => expect(post).toHaveBeenCalledExactlyOnceWith("/v1/places/save", { name: "New place", latitude: 0, longitude: 10, radius_m: 100 }));
  });
});
