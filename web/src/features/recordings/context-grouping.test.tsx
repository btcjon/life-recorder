// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../../App";
import { createClient } from "../../api/client";
import type { Chunk, DayPayload, DisplayBlock } from "../../api/types";

vi.mock("@/components/ui/resizable", () => ({
  ResizablePanelGroup: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  ResizablePanel: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  ResizableHandle: () => <span />,
}));

const chunks: Chunk[] = [
  { id: "a", transcript: "Launch milestones and schedule." },
  { id: "b", transcript: "A full launch passage with cedar budget." },
  { id: "c", transcript: "Irrigating the lavender orchard." },
  { id: "d", transcript: "One brief budget decision." },
  { id: "pending", transcript: "", status: "pending" },
  { id: "quiet", transcript: "" },
].map((item, index) => ({ duration: 20, status: "complete", audio_playable: true,
  started: `2026-10-04T12:0${index}:00Z`, started_local: `08:0${index}`, speakers: [], ...item }));
const blocks: DisplayBlock[] = [
  { id: "launch", kind: "event", source: "topic", title: "Project launch planning", chunk_ids: ["a", "b"], start_chunk_id: "a", end_chunk_id: "b" },
  { id: "orchard", kind: "event", source: "topic", title: "Orchard irrigation", chunk_ids: ["c"], start_chunk_id: "c", end_chunk_id: "c" },
  { id: "budget", kind: "event", source: "manual", title: "Saved budget decision", chunk_ids: ["d"], start_chunk_id: "d", end_chunk_id: "d", revision: 1 },
  { kind: "chunk", chunk_id: "pending" }, { kind: "chunk", chunk_id: "quiet" },
];
const payload: DayPayload = { day: "2026-10-04", chunks, display_blocks: blocks, people: [] };
function setupPayload(read = () => payload) {
  const paths: string[] = [];
  const client = createClient({ getToken: () => "", isRemote: () => true, fetchImpl: async url => {
    const path = String(url); paths.push(path);
    if (path.includes("/audio/")) return new Response(new Blob(["synthetic"]), { status: 200 });
    const body = path === "/v1/days" ? { days: ["2026-10-04"] } : path.startsWith("/v1/days/") ? read() : {};
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  } });
  render(<App client={client} />);
  return paths;
}

beforeEach(() => {
  Object.defineProperty(window, "PointerEvent", { configurable: true, value: MouseEvent });
  const values = new Map<string, string>();
  const storage = { getItem: (key: string) => values.get(key) || null, setItem: (key: string, value: string) => values.set(key, value), removeItem: (key: string) => values.delete(key) };
  for (const key of ["localStorage", "sessionStorage"]) Object.defineProperty(window, key, { configurable: true, value: storage });
  history.replaceState(null, "", "/ui-v2/?day=2026-10-04");
  Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) });
  Object.defineProperty(window, "ResizeObserver", { configurable: true, value: class { observe() {} unobserve() {} disconnect() {} } });
  Element.prototype.getAnimations = () => [];
  Element.prototype.scrollIntoView = vi.fn();
  HTMLMediaElement.prototype.play = vi.fn(async () => undefined);
  HTMLMediaElement.prototype.pause = vi.fn();
  HTMLMediaElement.prototype.load = vi.fn();
  URL.createObjectURL = vi.fn(() => "blob:group-source");
  URL.revokeObjectURL = vi.fn();
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("context-first sidebar", () => {
  it("defaults to meaningful inferred titles with one-click ordered full text", async () => {
    const paths = setupPayload();
    const row = await screen.findByRole("button", { name: /Project launch planning/ });
    expect(screen.getByRole("button", { name: "Groups" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: /Orchard irrigation/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Saved budget decision/ })).toBeTruthy();
    fireEvent.click(row);
    await screen.findByRole("heading", { name: "Project launch planning" });
    expect([...document.querySelectorAll(".transcript")].map(node => node.textContent)).toEqual([chunks[0].transcript, chunks[1].transcript]);
    expect(paths.some(path => path.includes("/audio/"))).toBe(false);
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    expect(screen.getByText(/Not a confirmed conversation/)).toBeTruthy();
  });

  it("expands actual sources and opens source text without playback", async () => {
    const paths = setupPayload();
    await screen.findByRole("button", { name: /Project launch planning/ });
    const list = screen.getByRole("list", { name: "Source recordings for Project launch planning" });
    const disclosure = list.closest("details")!;
    fireEvent.click(disclosure.querySelector("summary")!);
    fireEvent.click(within(list).getByRole("button", { name: "Open source recording 08:01" }));
    await waitFor(() => expect(screen.getByLabelText("Transcript").textContent).toBe(chunks[1].transcript));
    expect(new URLSearchParams(location.search).get("id")).toBe("b");
    expect(paths.some(path => path.includes("/audio/"))).toBe(false);
  });

  it("searches title and the exact matching source passage", async () => {
    setupPayload();
    await screen.findByRole("button", { name: /Project launch planning/ });
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "orchard irrigation" } });
    await screen.findByRole("heading", { name: "Orchard irrigation" });
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "cedar budget" } });
    await screen.findByRole("heading", { name: "Project launch planning" });
    expect(document.querySelector("mark")?.textContent).toBe("cedar budget");
  });

  it("keeps uncovered quiet and pending sources available under explicit filters", async () => {
    setupPayload();
    await screen.findByRole("button", { name: /Project launch planning/ });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Filters" }));
    await user.click(screen.getByRole("checkbox", { name: "Show quiet recordings" }));
    await user.click(screen.getByRole("checkbox", { name: "Show pending processing" }));
    expect(document.getElementById("recording-pending")).toBeTruthy();
    expect(document.getElementById("recording-quiet")).toBeTruthy();
    expect(screen.getByText("Processing transcript")).toBeTruthy();
  });

  it("roundtrips secondary individual-recording navigation and original source links", async () => {
    setupPayload();
    await screen.findByRole("button", { name: /Project launch planning/ });
    fireEvent.click(screen.getByRole("button", { name: "Individual recordings" }));
    expect(new URLSearchParams(location.search).get("groups")).toBe("0");
    fireEvent.click(screen.getByRole("button", { name: /08:01 · 20s A full launch/ }));
    await waitFor(() => expect(screen.getByLabelText("Transcript").textContent).toBe(chunks[1].transcript));
    fireEvent.click(screen.getByRole("button", { name: "Groups" }));
    expect(new URLSearchParams(location.search).get("groups")).toBe("1");
    expect(screen.getByRole("button", { name: /Project launch planning/ })).toBeTruthy();
  });

  it("recovers a regrouped selection by its source without replacing active playback", async () => {
    let current = payload;
    const paths = setupPayload(() => current);
    fireEvent.click(await screen.findByRole("button", { name: /Project launch planning/ }));
    fireEvent.click(screen.getAllByRole("button", { name: "Play recording" })[0]);
    const audio = document.querySelector("audio")!;
    await waitFor(() => expect(audio.src).toBe("blob:group-source"));
    Object.defineProperty(audio, "duration", { configurable: true, value: 20 });
    fireEvent.loadedMetadata(audio);
    await waitFor(() => expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(1));
    audio.currentTime = 5;
    current = { ...payload, display_blocks: [{ ...blocks[0], id: "new-launch", title: "Revised launch topic" }, ...blocks.slice(1)] };
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await screen.findByRole("heading", { name: "Revised launch topic" });
    expect(new URLSearchParams(location.search).get("id")).toBe("new-launch");
    expect(audio.src).toBe("blob:group-source");
    expect(audio.currentTime).toBe(5);
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(1);
    expect(paths.filter(path => path.includes("/audio/"))).toHaveLength(1);
  });

  it("keeps inferred edit title prefilled without silently saving it", async () => {
    const paths = setupPayload();
    fireEvent.click(await screen.findByRole("button", { name: /Project launch planning/ }));
    fireEvent.click(screen.getByRole("button", { name: "Edit title and boundaries" }));
    expect(screen.getByRole("textbox", { name: "Event title" })).toHaveProperty("value", "Project launch planning");
    expect(screen.getByRole("combobox", { name: "Starts with" })).toHaveProperty("value", "a");
    expect(screen.getByRole("combobox", { name: "Ends with" })).toHaveProperty("value", "b");
    expect(paths.some(path => path === "/v1/event-edits")).toBe(false);
  });
});
