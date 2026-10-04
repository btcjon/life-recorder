import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { ApiError, createClient, type ApiClient } from "../api/client";
import type { DayPayload } from "../api/types";
import { AudioPlayer } from "../components/AudioPlayer";
import { RecordingDetail } from "./recordings/RecordingDetail";
import { PeopleView } from "./people/PeopleView";
import { ReviewView } from "./review/ReviewView";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function installBrowserShims() {
  const memory = new Map<string, string>();
  const storage = {
    getItem: (key: string) => memory.get(key) ?? null,
    setItem: (key: string, value: string) => { memory.set(key, value); },
    removeItem: (key: string) => { memory.delete(key); },
  };
  Object.defineProperty(window, "localStorage", { configurable: true, value: storage });
  Object.defineProperty(window, "sessionStorage", { configurable: true, value: storage });
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      dispatchEvent: () => false,
    }),
  });
  class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  window.ResizeObserver = ResizeObserver;
  Element.prototype.getAnimations = () => [];
  HTMLMediaElement.prototype.play = () => Promise.resolve();
  HTMLMediaElement.prototype.pause = () => undefined;
  HTMLMediaElement.prototype.load = () => undefined;
}

beforeEach(() => {
  installBrowserShims();
});

afterEach(() => {
  cleanup();
});

describe("recordings identity", () => {
  it.each([502, 504, "abort"] as const)("locks a committed identity edit when its response is lost: %s", async (failure) => {
    let serverApplied = 0;
    const client = createClient({ getToken: () => "", isRemote: () => true, fetchImpl: async () => {
      serverApplied++;
      if (failure === "abort") throw new DOMException("Response interrupted after commit", "AbortError");
      return json(failure, { error: "Proxy response failed after commit" });
    } });
    const user = userEvent.setup();
    render(<RecordingDetail payload={{ people: [{ id: "p1", name: "Ada" }], identity_revision: 3 }} loading={false}
      chunk={{ id: "c", status: "complete", audio_playable: true, identity_revision: 3,
        speakers: [{ id: "t", started: 0, ended: 4, speaker_key: "S1", text: "synthetic" }] }} event={null}
      detailMode="turns" onDetailMode={() => undefined} client={client} playerItem={null}
      onPlaySpan={() => undefined} onPlayFull={() => undefined} onReload={() => false} onPeople={() => undefined} />);
    await user.click(screen.getByRole("button", { name: /Name speaker, 0.0 to 4.0 seconds/ }));
    await user.click(await screen.findByRole("button", { name: "Ada" }));
    expect(screen.getByRole("button", { name: "Ada" })).toHaveProperty("disabled", true);
    await user.click(screen.getByRole("button", { name: "Reload before editing" }));
    expect(screen.getByRole("button", { name: "Ada" })).toHaveProperty("disabled", true);
    expect(serverApplied).toBe(1);
  });

  it("locks a formerly playable retry after the server reports missing original", async () => {
    const fetchImpl = vi.fn(async () => json(409, { error: "Original audio unavailable" }));
    const client = createClient({ getToken: () => "", isRemote: () => true, fetchImpl });
    const user = userEvent.setup();
    render(<RecordingDetail payload={{}} loading={false}
      chunk={{ id: "missing", status: "needs_attention", audio_playable: true }} event={null}
      detailMode="transcript" onDetailMode={() => undefined} client={client} playerItem={null}
      onPlaySpan={() => undefined} onPlayFull={() => undefined} onReload={() => false} onPeople={() => undefined} />);
    const retry = screen.getByRole("button", { name: "Retry processing" });
    await user.click(retry);
    await waitFor(() => expect(retry).toHaveProperty("disabled", true));
    await user.click(retry);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("keeps identity saves locked when conflict reload fails", async () => {
    const fetchImpl = vi.fn(async () => json(409, { error: "Identity changed" }));
    const reload = vi.fn(() => false);
    const client = createClient({ getToken: () => "", isRemote: () => true, fetchImpl });
    const user = userEvent.setup();
    render(<RecordingDetail payload={{ people: [{ id: "p1", name: "Ada" }], identity_revision: 3 }} loading={false}
      chunk={{ id: "c", status: "complete", audio_playable: true, identity_revision: 3,
        speakers: [{ id: "t", started: 0, ended: 4, speaker_key: "S1", text: "synthetic" }] }} event={null}
      detailMode="turns" onDetailMode={() => undefined} client={client} playerItem={null}
      onPlaySpan={() => undefined} onPlayFull={() => undefined} onReload={reload} onPeople={() => undefined} />);
    await user.click(screen.getByRole("button", { name: /Name speaker, 0.0 to 4.0 seconds/ }));
    await user.click(await screen.findByRole("button", { name: "Ada" }));
    await user.click(screen.getByRole("button", { name: "Reload before editing" }));
    expect(reload).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Ada" })).toHaveProperty("disabled", true);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("labels every group turn without enrollment and does not retry a conflict", async () => {
    const posts: Array<{ url: string; body: Record<string, unknown> }> = [];
    const client = createClient({
      getToken: () => "",
      isRemote: () => true,
      fetchImpl: async (url, init) => {
        const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : {};
        posts.push({ url: String(url), body });
        if (posts.length === 1) return json(409, { error: "Speaker identities changed. Reload before editing." });
        return json(200, { change_id: "c1", revision: 5 });
      },
    });
    const user = userEvent.setup();
    render(
      <RecordingDetail
        payload={{ people: [{ id: "p1", name: "Ada" }], identity_revision: 3 }}
        loading={false}
        chunk={{
          id: "chunk-1",
          status: "complete",
          audio_playable: true,
          identity_revision: 3,
          speakers: [
            { id: "t1", started: 0, ended: 2, text: "hello", speaker_key: "S1" },
            { id: "t2", started: 2, ended: 4, text: "there", speaker_key: "S1" },
          ],
        }}
        event={null}
        detailMode="turns"
        onDetailMode={() => undefined}
        client={client}
        playerItem={null}
        onPlaySpan={() => undefined}
        onPlayFull={() => undefined}
        onReload={() => true}
        onPeople={() => undefined}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Name speaker, 0.0 to 4.0 seconds/ }));
    await user.click(await screen.findByRole("button", { name: "Ada" }));
    expect(posts).toHaveLength(1);
    expect(posts[0]?.url).toBe("/v1/identities/label");
    expect(posts[0]?.body).toEqual({ turn_ids: ["t1", "t2"], person_id: "p1", use_sample: false, revision: 3 });
    expect(screen.getByRole("button", { name: "Ada" })).toHaveProperty("disabled", true);
    expect(posts).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Reload before editing" })).toBeTruthy();
  });

  it("disables retry after original audio is unavailable", async () => {
    const calls: string[] = [];
    const client = createClient({
      getToken: () => "",
      isRemote: () => true,
      fetchImpl: async (url) => {
        calls.push(String(url));
        return json(409, { error: "Original audio unavailable" });
      },
    });
    const user = userEvent.setup();
    render(
      <RecordingDetail
        payload={{}}
        loading={false}
        chunk={{ id: "chunk-9", status: "needs_attention", transcript: "kept", audio_playable: false }}
        event={null}
        detailMode="transcript"
        onDetailMode={() => undefined}
        client={client}
        playerItem={null}
        onPlaySpan={() => undefined}
        onPlayFull={() => undefined}
        onReload={() => false}
        onPeople={() => undefined}
      />,
    );
    const button = screen.getByRole("button", { name: "Retry processing" });
    await user.click(button);
    expect(calls).toEqual([]);
    expect(button).toHaveProperty("disabled", true);
    expect(calls).toHaveLength(0);
  });
});

describe("review and people mutations", () => {
  it("keeps suggestions unconfirmed, escapes hostile text, and does not enroll by default", async () => {
    const calls: Array<{ url: string; body?: unknown }> = [];
    const client = createClient({
      getToken: () => "",
      isRemote: () => true,
      fetchImpl: async (url, init) => {
        const path = String(url);
        calls.push({ url: path, body: init?.body ? JSON.parse(String(init.body)) : undefined });
        if (path.startsWith("/v1/speaker-review?")) {
          return json(200, {
            identity_revision: 8,
            items: [{
              turn_id: "turn-1",
              group_id: "g1",
              chunk_id: "chunk-1",
              started: 1,
              ended: 3,
              audio_usable: true,
              clean_seconds: 8,
              suggestions: [{ person_id: "p1", name: "<img src=x onerror=alert(1)>" }],
              confirmed: false,
            }],
            next_cursor: "next",
          });
        }
        return json(200, { labeled: true, change_id: "c1" });
      },
    });
    const user = userEvent.setup();
    const { container } = render(
      <ReviewView client={client} people={[{ id: "p2", name: "Bea" }]} onPlay={() => undefined} onOpenRecording={() => undefined} />,
    );
    expect((await screen.findAllByText(/<img src=x onerror=alert\(1\)>/)).length).toBeGreaterThan(0);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(calls.some((call) => call.url.includes("/diagnostics"))).toBe(false);
    const choice = screen.getByLabelText("Person for this speaker turn") as HTMLSelectElement;
    expect(choice.value).toBe("");
    await user.selectOptions(choice, "p1");
    await user.click(screen.getByRole("button", { name: "Confirm selected name" }));
    await waitFor(() => expect(calls.some((call) => call.url === "/v1/turns/turn-1/label")).toBe(true));
    const label = calls.find((call) => call.url === "/v1/turns/turn-1/label");
    expect(label?.body).toEqual({ person_id: "p1", use_sample: false, revision: 8 });
    await user.click(screen.getByRole("button", { name: "Voice-check counts" }));
    await waitFor(() => expect(calls.some((call) => call.url === "/v1/speaker-review/diagnostics")).toBe(true));
  });

  it("confirms withdrawal and does not retry a 409 or enroll a sample", async () => {
    const posts: Array<{ url: string; body: Record<string, unknown> }> = [];
    const client = createClient({
      getToken: () => "",
      isRemote: () => true,
      fetchImpl: async (url, init) => {
        const path = String(url);
        if (init?.method === "POST") {
          const body = JSON.parse(String(init.body)) as Record<string, unknown>;
          posts.push({ url: path, body });
          return json(409, { error: "Speaker identities changed. Reload before editing." });
        }
        if (path === "/v1/identity-profiles") {
          return json(200, {
            revision: 2,
            people: [{
              id: "p1",
              name: "Ada",
              sample_count: 1,
              sample_seconds: 6,
              clip_count: 1,
              enrollment_ready: false,
              enrollment_reasons: ["need_2_samples"],
              samples: [{ id: "sample-1", chunk_id: "chunk-1", duration: 6, status: "accepted", confirmed_at: 1 }],
            }],
          });
        }
        return json(200, { revision: 2, clusters: [], gate: { enabled: false, reason: "off" }, proposals: [] });
      },
    });
    const user = userEvent.setup();
    render(<PeopleView client={client} catalog={[]} onOpenRecording={() => undefined} onChanged={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: "Remove voice sample" }));
    await user.click(await screen.findByRole("button", { name: "Confirm" }));
    expect(posts).toEqual([{ url: "/v1/identities/remove_sample", body: { sample_id: "sample-1", revision: 2 } }]);
    expect(JSON.stringify(posts)).not.toContain("use_sample");
    expect(screen.getByRole("button", { name: "Remove voice sample" })).toHaveProperty("disabled", true);
    expect(posts).toHaveLength(1);
  });
});

describe("playback across views", () => {
  it("keeps one player mounted and can queue a span before metadata", async () => {
    const calls: string[] = [];
    const client = createClient({
      getToken: () => "",
      isRemote: () => true,
      fetchImpl: async (url) => {
        calls.push(String(url));
        if (String(url) === "/v1/days") return json(200, { days: ["2026-10-04"] });
        if (String(url).startsWith("/v1/days/")) return json(200, { chunks: [], people: [] } satisfies DayPayload);
        if (String(url) === "/v1/health") return json(200, { processing: { pending_count: 0, attention_count: 0 } });
        if (String(url).startsWith("/v1/speaker-review")) return json(200, { items: [], identity_revision: 1 });
        if (String(url) === "/v1/identity-profiles") return json(200, { revision: 1, people: [] });
        if (String(url) === "/v1/speaker-clusters") return json(200, { revision: 1, clusters: [], gate: { enabled: false } });
        return json(200, {});
      },
    });
    const user = userEvent.setup();
    render(<App client={client} />);
    expect(await screen.findByRole("contentinfo", { name: "Playback" })).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "More" }));
    await user.click(screen.getByRole("menuitem", { name: "Review" }));
    await user.click(screen.getByRole("button", { name: "People" }));
    expect(screen.getAllByRole("contentinfo", { name: "Playback" })).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "More" }));
    expect(screen.getByRole("menuitem", { name: "Context" }).textContent).not.toContain("Later");
    expect(screen.getByRole("menuitem", { name: "Review" }).textContent || "").not.toContain("Later");

    const played = vi.fn(() => Promise.resolve());
    HTMLMediaElement.prototype.play = played;
    const handle = { current: null as { playSpan: (span: { key: string; start: number; end: number; expectedAudioId: string; fromStart: boolean }) => void } | null };
    const item = { id: "a", kind: "chunk" as const, audioPlayable: true, enhancedPlayable: false, label: "A", pinned: false };
    const view = render(
      <AudioPlayer
        ref={(node) => { handle.current = node; }}
        item={null}
        client={client}
      />,
    );
    handle.current?.playSpan({ key: "turn-1", start: 1, end: 2, expectedAudioId: "chunk:a:original", fromStart: true });
    expect(played).not.toHaveBeenCalled();
    view.rerender(<AudioPlayer ref={(node) => { handle.current = node; }} item={item} client={client} />);
    await waitFor(() => expect(calls.some((url) => url === "/v1/audio/a")).toBe(true));
    expect(played).not.toHaveBeenCalled();
    expect(calls.some((url) => url.includes("token"))).toBe(false);
  });
});

describe("stale review read", () => {
  it("does not let a replaced chunk response satisfy the new selection", () => {
    const requested = { kind: "chunk" as const, id: "old" };
    const current = { kind: "chunk" as const, id: "new" };
    expect(requested.id === current.id).toBe(false);
    expect(new ApiError(409, { error: "conflict" }).conflict).toBe(true);
    const unused: ApiClient = createClient({ getToken: () => "", isRemote: () => true, fetchImpl: async () => json(200, {}) });
    expect(unused).toBeTruthy();
  });
});
