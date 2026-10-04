// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { forwardRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createClient } from "./api/client";
import { App } from "./App";

vi.mock("./components/AppShell", () => ({ AppShell: ({ onView, player, children }: { onView: (view: string) => void; player: React.ReactNode; children: React.ReactNode }) => <><nav>{["recordings", "review", "people", "context", "health"].map(view => <button key={view} onClick={() => onView(view)}>{view}</button>)}</nav>{player}{children}</> }));
vi.mock("./components/AudioPlayer", () => ({ AudioPlayer: forwardRef(({ item }: { item: { id: string } | null }, _ref) => <audio data-testid="persistent-player" data-item={item?.id || ""} />) }));
vi.mock("./features/recordings/RecordingsView", () => ({ RecordingsView: ({ onRoute, onIdentityChange, onPlayItem }: { onRoute: (patch: unknown) => void; onIdentityChange: (id: string) => void; onPlayItem: (item: unknown) => void }) => <><button onClick={() => onRoute({ selectedId: "chunk-a", selectedKind: "chunk" })}>Select recording</button><button onClick={() => onPlayItem({ id: "chunk-a", kind: "chunk", audioPlayable: true, enhancedPlayable: false, label: "Synthetic", pinned: false })}>Play recording</button><button onClick={() => onIdentityChange("recording-change")}>Recordings identity edit</button><button onClick={() => onRoute({ day: "2026-10-05", selectedId: null })}>Next day</button></> }));
vi.mock("./features/review/ReviewView", () => ({ ReviewView: ({ onPlay, onIdentityChange }: { onPlay: (request: unknown) => void; onIdentityChange: (id: string) => void }) => <><button onClick={() => onPlay({ turnId: "turn-b", chunkId: "chunk-b", started: 1, ended: 5, audioUsable: true })}>Play review span</button><button onClick={() => onIdentityChange("review-change")}>Review identity edit</button></> }));
vi.mock("./features/people/PeopleView", () => ({ PeopleView: ({ lastChangeId }: { lastChangeId: string | null }) => <p>Undo target: {lastChangeId || "none"}</p> }));
vi.mock("./features/context/ContextView", () => ({ ContextView: () => <p>Context integrated</p> }));
vi.mock("./features/health/HealthView", () => ({ HealthView: () => <p>Health integrated</p> }));
afterEach(cleanup);
const payload = { chunks: [{ id: "chunk-a", audio_playable: true, transcript: "Synthetic only", speakers: [] }], people: [] };
function client() {
  return createClient({ getToken: () => "", isRemote: () => true, fetchImpl: async path => new Response(JSON.stringify(path === "/v1/days" ? { days: ["2026-10-04"] } : path === "/v1/health" ? {} : path.includes("2026-10-05") ? { chunks: [] } : payload), { status: 200, headers: { "Content-Type": "application/json" } }) });
}
describe("application integration", () => {
  it("keeps the same player and Review source across every navigation", async () => {
    history.replaceState(null, "", "/ui-v2/?day=2026-10-04"); render(<App client={client()} />);
    fireEvent.click(screen.getByRole("button", { name: "Select recording" }));
    expect(screen.getByTestId("persistent-player").getAttribute("data-item")).toBe("");
    fireEvent.click(screen.getByRole("button", { name: "Play recording" }));
    await waitFor(() => expect(screen.getByTestId("persistent-player").getAttribute("data-item")).toBe("chunk-a"));
    const player = screen.getByTestId("persistent-player");
    fireEvent.click(screen.getByRole("button", { name: "review" }));
    fireEvent.click(screen.getByRole("button", { name: "Play review span" }));
    expect(player.getAttribute("data-item")).toBe("chunk-b");
    for (const view of ["people", "context", "health", "recordings"]) {
      fireEvent.click(screen.getByRole("button", { name: view }));
      expect(screen.getByTestId("persistent-player")).toBe(player);
      expect(player.getAttribute("data-item")).toBe("chunk-b");
    }
  });
  it("shares undo targets from both human-label surfaces", async () => {
    history.replaceState(null, "", "/ui-v2/?day=2026-10-04"); render(<App client={client()} />);
    fireEvent.click(screen.getByRole("button", { name: "Recordings identity edit" }));
    fireEvent.click(screen.getByRole("button", { name: "people" }));
    expect(screen.getByText("Undo target: recording-change")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "review" }));
    fireEvent.click(screen.getByRole("button", { name: "Review identity edit" }));
    fireEvent.click(screen.getByRole("button", { name: "people" }));
    expect(screen.getByText("Undo target: review-change")).toBeTruthy();
  });
  it("maps a direct Context link and keeps playback through day filtering", async () => {
    history.replaceState(null, "", "/ui-v2/context?day=2026-10-04"); render(<App client={client()} />);
    expect(screen.getByText("Context integrated")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "recordings" }));
    fireEvent.click(screen.getByRole("button", { name: "Select recording" }));
    fireEvent.click(screen.getByRole("button", { name: "Play recording" }));
    await waitFor(() => expect(screen.getByTestId("persistent-player").getAttribute("data-item")).toBe("chunk-a"));
    fireEvent.click(screen.getByRole("button", { name: "Next day" }));
    await waitFor(() => expect(location.search).toContain("2026-10-05"));
    expect(screen.getByTestId("persistent-player").getAttribute("data-item")).toBe("chunk-a");
  });
});
