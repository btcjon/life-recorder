import { StrictMode, createRef } from "react";
import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AudioPlayer, type AudioPlayerHandle } from "./AudioPlayer";
import { createClient } from "../api/client";

beforeEach(() => {
  HTMLMediaElement.prototype.play = vi.fn(() => Promise.resolve());
  HTMLMediaElement.prototype.pause = vi.fn();
  HTMLMediaElement.prototype.load = vi.fn();
  URL.createObjectURL = vi.fn(() => "blob:synthetic-audio");
  URL.revokeObjectURL = vi.fn();
});
afterEach(cleanup);
const a = { id: "a", kind: "chunk" as const, audioPlayable: true, enhancedPlayable: false, label: "A", pinned: false };

it("waits for metadata before full playback and clears prior clip bounds", async () => {
  let finish!: (value: Response) => void;
  const client = createClient({ getToken: () => "", isRemote: () => true, fetchImpl: () => new Promise((resolve) => { finish = resolve; }) });
  const ref = createRef<AudioPlayerHandle>();
  const { container } = render(<AudioPlayer item={a} client={client} ref={ref} />);
  act(() => {
    ref.current?.playSpan({ key: "t", start: 2, end: 3, expectedAudioId: "chunk:a:original", fromStart: true });
    ref.current?.playFull();
  });
  expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  await act(async () => { finish(new Response(new Blob(["synthetic"]))); });
  const audio = container.querySelector("audio")!;
  expect(audio.src).toContain("blob:synthetic-audio");
  fireEvent.loadedMetadata(audio);
  expect(HTMLMediaElement.prototype.play).toHaveBeenCalled();
  expect(audio.currentTime).toBe(0);
  audio.currentTime = 5;
  fireEvent.timeUpdate(audio);
  expect(audio.currentTime).toBe(5);
});

it("does not carry a pending full-play intent into another selection", async () => {
  const pending: Array<(value: Response) => void> = [];
  const client = createClient({ getToken: () => "", isRemote: () => true, fetchImpl: () => new Promise((resolve) => { pending.push(resolve); }) });
  const ref = createRef<AudioPlayerHandle>();
  const view = render(<AudioPlayer item={a} client={client} ref={ref} />);
  act(() => ref.current?.playFull());
  view.rerender(<AudioPlayer item={{ ...a, id: "b" }} client={client} ref={ref} />);
  await act(async () => { pending[0]!(new Response(new Blob(["old"]))); pending[1]!(new Response(new Blob(["new"]))); });
  fireEvent.loadedMetadata(view.container.querySelector("audio")!);
  expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  expect(URL.revokeObjectURL).toHaveBeenCalled();
});

it("loads correctly after StrictMode cleanup and releases its blob on unmount", async () => {
  const client = createClient({ getToken: () => "", isRemote: () => true, fetchImpl: async () => new Response(new Blob(["synthetic"])) });
  const ref = createRef<AudioPlayerHandle>();
  const view = render(<StrictMode><AudioPlayer item={a} client={client} ref={ref} /></StrictMode>);
  await waitFor(() => expect(view.container.querySelector("audio")?.src).toContain("blob:synthetic-audio"));
  expect(ref.current?.loadedAudioId()).toBe("chunk:a:original");
  view.unmount();
  expect(URL.revokeObjectURL).toHaveBeenCalled();
});
