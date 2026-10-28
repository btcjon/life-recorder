// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../../App";
import { createClient, type ApiClient } from "../../api/client";
import type { Chunk, DayPayload, SpeechEvent } from "../../api/types";
import { AudioPlayer } from "../../components/AudioPlayer";
import { RecordingDetail } from "./RecordingDetail";

vi.mock("@/components/ui/resizable", () => ({
  ResizablePanelGroup: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  ResizablePanel: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  ResizableHandle: () => <span />,
}));

const first: Chunk = { id: "a", started: "2026-10-04T12:00:00Z", started_local: "08:00", duration: 20, status: "complete", audio_playable: true, transcript: "First full transcript", identity_revision: 3,
  speakers: [{ id: "turn-a", started: 0, ended: 8, speaker_key: "S1", clean_seconds: 8 }] };
const second: Chunk = { ...first, id: "b", started: "2026-10-04T12:01:00Z", started_local: "08:01", transcript: "Second full transcript with lavender orchard", speakers: [{ id: "turn-b", started: 2, ended: 10, speaker_key: "S2" }] };
const event: SpeechEvent = { id: "speech", started: first.started, ended: "2026-10-04T12:01:04Z", audio_playable: true, enhanced_playable: true, playable_duration: 8,
  chunks: [{ chunk_id: "a", start: 0, end: 4 }, { chunk_id: "b", start: 2, end: 6 }] };
const group = { id: "saved", kind: "event", source: "manual", title: "Saved title", chunk_ids: ["a", "b"], start_chunk_id: "a", end_chunk_id: "b", revision: 2 };
const payload: DayPayload = { day: "2026-10-04", chunks: [first, second], events: [event], display_blocks: [group], people: [{ id: "person", name: "Synthetic person" }], identity_revision: 3 };

function response(body: unknown) { return new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } }); }
function client() { return createClient({ getToken: () => "", isRemote: () => true, fetchImpl: async () => response({}) }); }
function detail(extra: Partial<React.ComponentProps<typeof RecordingDetail>> = {}, api: ApiClient = client()) {
  return <RecordingDetail payload={payload} loading={false} chunk={first} event={null} detailMode="transcript" onDetailMode={() => undefined} client={api} playerItem={null}
    onPlaySpan={() => undefined} onPlayFull={() => undefined} onReload={() => true} onPeople={() => undefined} {...extra} />;
}

beforeEach(() => {
  const storage = { getItem: () => null, setItem: () => undefined, removeItem: () => undefined };
  Object.defineProperty(window, "localStorage", { configurable: true, value: storage });
  Object.defineProperty(window, "sessionStorage", { configurable: true, value: storage });
  history.replaceState(null, "", "/ui-v2/?day=2026-10-04&groups=0");
  Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) });
  Object.defineProperty(window, "ResizeObserver", { configurable: true, value: class { observe() {} unobserve() {} disconnect() {} } });
  Element.prototype.getAnimations = () => [];
  Element.prototype.scrollIntoView = vi.fn();
  HTMLMediaElement.prototype.play = vi.fn(async () => undefined);
  HTMLMediaElement.prototype.pause = vi.fn();
  HTMLMediaElement.prototype.load = vi.fn();
  URL.createObjectURL = vi.fn(() => "blob:synthetic");
  URL.revokeObjectURL = vi.fn();
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });

describe("transcript-first workflow", () => {
  it("names a speaker without playing, and keeps enrollment unchecked", async () => {
    const play = vi.fn(), span = vi.fn(), label = vi.fn(async () => ({ revision: 4 }));
    const api = { ...client(), labelTurns: label };
    render(detail({ onPlayItem: play, onPlaySpan: span }, api));
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Name speaker, 0.0 to 8.0 seconds" }));
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.getByRole("checkbox", { name: "Save a voice sample" }).getAttribute("aria-checked")).toBe("false");
    expect(play).not.toHaveBeenCalled(); expect(span).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Synthetic person" }));
    expect(label).toHaveBeenCalledExactlyOnceWith({ turn_ids: ["turn-a"], person_id: "person", use_sample: false, revision: 3 });
  });

  it("auditions source seconds without opening the name picker", () => {
    const play = vi.fn(); render(detail({ onPlayItem: play }));
    fireEvent.click(screen.getByRole("button", { name: "Play speaker, 0.0 to 8.0 seconds" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(play.mock.calls[0]?.[1]).toMatchObject({ start: 0, end: 8, expectedAudioId: "chunk:a:original" });
  });

  it("keeps expired-audio text and the literal matching passage readable", () => {
    render(detail({ chunk: { ...second, audio_playable: false }, query: "lavender orchard" }));
    expect(screen.getByLabelText("Transcript").textContent).toBe(second.transcript);
    expect(screen.getByRole("button", { name: "Play recording" })).toHaveProperty("disabled", true);
    expect(screen.getByText("Audio no longer retained")).toBeTruthy();
    expect(document.querySelector("mark")?.textContent).toBe("lavender orchard");
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
  });

  it("shows saved-group full source text in order and opens exact naming sources", () => {
    const source = vi.fn(), play = vi.fn(); render(detail({ chunk: null, group: { block: group, members: [first, second] }, onOpenSource: source, onPlayItem: play }));
    expect(screen.getByRole("heading", { name: "Saved title" })).toBeTruthy();
    const texts = [...document.querySelectorAll(".transcript")].map(node => node.textContent);
    expect(texts).toEqual([first.transcript, second.transcript]);
    fireEvent.click(screen.getByText('Sources · 2 recordings'));
    fireEvent.click(screen.getAllByRole("button", { name: "Open source · speakers and processing" })[1]);
    expect(source).toHaveBeenCalledExactlyOnceWith("b"); expect(play).not.toHaveBeenCalled();
  });

  it("resolves old event links to full transcripts and explicitly scoped audio options", async () => {
    const play = vi.fn(); render(detail({ chunk: null, event, onPlayItem: play }));
    expect(screen.getByText(first.transcript!)).toBeTruthy();
    expect(screen.getByText(second.transcript!)).toBeTruthy();
    await userEvent.setup().click(screen.getByText("Audio options · 1 speech clip"));
    expect(screen.getAllByText(/includes 2 recordings/).length).toBe(2);
    fireEvent.click(screen.getByRole("button", { name: "Play enhanced" }));
    expect(play.mock.calls[0]?.[0]).toMatchObject({ id: "speech", preferredSource: "enhanced" });
    expect(play.mock.calls[0]?.[1]).toBeNull();
  });

  it("blocks source mutations while a refreshed day is unavailable", async () => {
    const label = vi.fn(async () => ({}));
    render(detail({ readOnly: true }, { ...client(), labelTurns: label }));
    await userEvent.setup().click(screen.getByRole("button", { name: "Name speaker, 0.0 to 8.0 seconds" }));
    expect(screen.getByRole("button", { name: "Synthetic person" })).toHaveProperty("disabled", true);
    expect(label).not.toHaveBeenCalled();
  });

  it("shows processing text without hiding already available transcript", () => {
    render(detail({ chunk: { ...first, status: "pending", speakers: undefined, diarization_status: "pending" } }));
    expect(screen.getByText("Processing transcript")).toBeTruthy();
    expect(screen.getByText(first.transcript!)).toBeTruthy();
    expect(screen.getByText("Speaker analysis blocked by transcription")).toBeTruthy();
  });

  it("opens one-click full text from the flat list with no audio fetch and no day-wide review burst", async () => {
    const paths: string[] = [];
    const api = createClient({ getToken: () => "", isRemote: () => true, fetchImpl: async url => {
      const path = String(url); paths.push(path);
      if (path === "/v1/days") return response({ days: ["2026-10-04"] });
      if (path.startsWith("/v1/days/")) return response({ ...payload, chunks: payload.chunks?.map(chunk => ({ ...chunk, speakers: undefined })) });
      if (path.endsWith("/review")) return response({ speakers: [], voice_pending: false, identity_revision: 3 });
      return response({});
    } });
    render(<App client={api} />);
    const row = await screen.findByRole("button", { name: /08:01 · 20s Second full transcript/ });
    fireEvent.click(row);
    await waitFor(() => expect(screen.getByLabelText("Transcript").textContent).toBe(second.transcript));
    expect(screen.queryByText("Speech-only clip")).toBeNull();
    expect(paths.some(path => path.includes("/audio"))).toBe(false);
    await waitFor(() => expect(paths).toContain("/v1/chunks/b/review"));
    expect(paths.filter(path => path.endsWith("/review")).length).toBeLessThanOrEqual(2);
  });

  it("opens More with keyboard navigation and returns focus on Escape", async () => {
    render(<App client={client()} />);
    const user = userEvent.setup(), more = screen.getByRole("button", { name: "More" });
    more.focus(); await user.keyboard("{ArrowDown}");
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "Review" }));
    await user.keyboard("{ArrowDown}");
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "Context" }));
    await user.keyboard("{Escape}");
    expect(document.activeElement).toBe(more); expect(screen.queryByRole("menu")).toBeNull();
  });

  it("discards an old selected-recording review even if its server ignores abort", async () => {
    const pending = new Map<string, (response: Response) => void>();
    const api = createClient({ getToken: () => "", isRemote: () => true, fetchImpl: async url => {
      const path = String(url);
      if (path === "/v1/days") return response({ days: ["2026-10-04"] });
      if (path.startsWith("/v1/days/")) return response({ ...payload, chunks: payload.chunks?.map(chunk => ({ ...chunk, speakers: undefined })) });
      if (path.endsWith("/review")) return new Promise<Response>(resolve => pending.set(path, resolve));
      return response({});
    } });
    render(<App client={api} />);
    await waitFor(() => expect(pending.has("/v1/chunks/a/review")).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: /08:01 · 20s Second full transcript/ }));
    await waitFor(() => expect(pending.has("/v1/chunks/b/review")).toBe(true));
    await act(async () => pending.get("/v1/chunks/b/review")!(response({ speakers: second.speakers, voice_pending: false, identity_revision: 3 })));
    await screen.findByRole("button", { name: "Name speaker, 2.0 to 10.0 seconds" });
    await act(async () => pending.get("/v1/chunks/a/review")!(response({ transcript: "Wrong stale text", speakers: first.speakers, voice_pending: false })));
    await waitFor(() => expect(screen.getByLabelText("Transcript").textContent).toBe(second.transcript));
    expect(screen.queryByText("Wrong stale text")).toBeNull();
    expect(screen.queryByRole("button", { name: "Name speaker, 0.0 to 8.0 seconds" })).toBeNull();
  });

  it("reloads speaker controls after an identity save instead of leaving them pending", async () => {
    let assigned = false, reads = 0;
    const posts: unknown[] = [];
    const api = createClient({ getToken: () => "", isRemote: () => true, fetchImpl: async (url, init) => {
      const path = String(url);
      if (path === "/v1/days") return response({ days: ["2026-10-04"] });
      if (path.startsWith("/v1/days/")) return response({ ...payload, chunks: [{ ...first, speakers: undefined }] });
      if (path.endsWith("/review")) {
        reads++;
        return response({ speakers: first.speakers?.map(turn => assigned ? { ...turn, name: "Synthetic person", person_id: "person", label_source: "confirmed" } : turn), voice_pending: false, identity_revision: assigned ? 4 : 3 });
      }
      if (path === "/v1/identities/label") {
        assigned = true; posts.push(JSON.parse(String(init?.body)));
        return response({ revision: 4 });
      }
      return response({});
    } });
    render(<App client={api} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Name speaker, 0.0 to 8.0 seconds" }));
    await user.click(screen.getByRole("button", { name: "Synthetic person" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Name speaker, 0.0 to 8.0 seconds" }).textContent).toContain("Synthetic person ✓"));
    expect(posts).toEqual([{ turn_ids: ["turn-a"], person_id: "person", use_sample: false, revision: 3 }]);
    expect(reads).toBe(2);
  });

  it("opens a cross-midnight source in its own day without the previous payload clearing the target", async () => {
    history.replaceState(null, "", "/ui-v2/?day=2026-10-04&id=speech&kind=event");
    const crossing = { ...event, chunks: [{ chunk_id: "b", start: 0, end: 4, clip_started: "2026-10-05T04:00:00Z" }] };
    const api = createClient({ getToken: () => "", isRemote: () => true, fetchImpl: async url => {
      const path = String(url);
      if (path === "/v1/days") return response({ days: ["2026-10-04", "2026-10-05"] });
      if (path.includes("/v1/days/2026-10-05")) return response({ day: "2026-10-05", chunks: [second] });
      if (path.startsWith("/v1/days/")) return response({ ...payload, chunks: [first], events: [crossing] });
      return response({});
    } });
    render(<App client={api} />);
    await screen.findByRole("heading", { name: "Speech-only clip" });
    await userEvent.setup().click(screen.getByText("Audio options · 1 speech clip"));
    fireEvent.click(screen.getByRole("button", { name: /2026-10-05T04:00:00Z/ }));
    await waitFor(() => expect(screen.getByLabelText("Transcript").textContent).toBe(second.transcript));
    expect(location.search).toContain("day=2026-10-05");
    expect(location.search).toContain("id=b");
  });

  it("recovers exhausted speaker reads through visible Refresh without restarting audio", async () => {
    vi.useFakeTimers();
    let healthy = false, reads = 0;
    const api = createClient({ getToken: () => "", isRemote: () => true, fetchImpl: async url => {
      const path = String(url);
      if (path === "/v1/days") return response({ days: ["2026-10-04"] });
      if (path.startsWith("/v1/days/")) return response({ ...payload, chunks: [{ ...first, speakers: undefined }] });
      if (path.endsWith("/review")) {
        reads++;
        return healthy ? response({ speakers: first.speakers, voice_pending: false, identity_revision: 3 }) : new Response("unavailable", { status: 503 });
      }
      if (path === "/v1/audio/a") return new Response(new Blob(["synthetic audio"]));
      return response({});
    } });
    render(<App client={api} />);
    for (let attempt = 0; attempt < 4; attempt++) await act(async () => vi.advanceTimersByTimeAsync(4000));
    expect(reads).toBe(3);
    expect(screen.getByText("Couldn't load speakers for this recording. Refresh to try again.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Play recording" }));
    await act(async () => vi.advanceTimersByTimeAsync(0));
    const audio = document.querySelector("audio")!;
    fireEvent.loadedMetadata(audio);
    audio.currentTime = 5;
    const url = audio.src, playCount = vi.mocked(HTMLMediaElement.prototype.play).mock.calls.length;
    expect(playCount).toBe(1);
    healthy = true;
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await act(async () => vi.advanceTimersByTimeAsync(0));
    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(screen.getByRole("button", { name: "Name speaker, 0.0 to 8.0 seconds" })).toBeTruthy();
    expect(reads).toBe(4);
    expect(document.querySelector("audio")).toBe(audio);
    expect(audio.src).toBe(url); expect(audio.currentTime).toBe(5);
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(playCount);
  });
});

describe("explicit derived playback", () => {
  it("fetches enhanced on its own clock, and a repeated speech-only request returns to original", async () => {
    const blob = vi.fn(async (_path: string, _signal?: AbortSignal) => new Blob(["synthetic audio"]));
    const api = { ...client(), getBlob: blob };
    const item = { id: "speech", kind: "event" as const, audioPlayable: true, enhancedPlayable: true, label: "Synthetic clip", pinned: false, preferredSource: "enhanced" as const };
    const view = render(<AudioPlayer item={item} client={api} playRequest={{ nonce: 1 }} />);
    await waitFor(() => expect(blob.mock.calls[0]?.[0]).toBe("/v1/events/speech/audio?kind=enhanced"));
    fireEvent.loadedMetadata(document.querySelector("audio")!);
    expect(screen.getByRole("button", { name: "Enhanced" }).getAttribute("aria-pressed")).toBe("true");
    view.rerender(<AudioPlayer item={{ ...item, preferredSource: "original" }} client={api} playRequest={{ nonce: 2 }} />);
    await waitFor(() => expect(blob.mock.calls[1]?.[0]).toBe("/v1/events/speech/audio?kind=original"));
    const footer = within(screen.getByRole("contentinfo", { name: "Playback" }));
    expect(footer.getByText("Speech-only · Synthetic clip")).toBeTruthy();
    expect(document.querySelector("audio")!.currentTime).toBe(0);
  });
});
