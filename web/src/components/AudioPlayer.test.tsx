import { StrictMode, createRef } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AudioPlayer, type AudioPlayerHandle } from "./AudioPlayer";
import { createClient } from "../api/client";
import { freezeConversation } from '../lib/conversation';

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

it('advances a conversation only on ended and pauses unexpected failure for Retry or Skip', async () => {
  const next = vi.fn();
  const fetch = vi.fn(async () => new Response(new Blob(['audio'])));
  const client = createClient({ getToken: () => '', isRemote: () => true, fetchImpl: fetch });
  const queue = freezeConversation([a, { ...a, id: 'b' }], 'Conversation')!;
  const view = render(<AudioPlayer item={a} client={client} queue={queue} onQueueAdvance={next} playRequest={{nonce: 1}} />);
  const audio = view.container.querySelector('audio')!;
  await waitFor(() => expect(audio.src).toContain('blob:'));
  expect(fetch).toHaveBeenCalledTimes(1);
  fireEvent.play(audio);
  fireEvent.error(audio);
  fireEvent.ended(audio);
  expect(next).not.toHaveBeenCalled();
  expect(screen.getByRole('button', {name: 'Retry'})).toBeTruthy();
  fireEvent.click(screen.getByRole('button', {name: 'Retry'}));
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  fireEvent.play(audio);
  fireEvent.ended(audio);
  expect(next).toHaveBeenCalledTimes(1);
  fireEvent.error(audio);
  fireEvent.click(screen.getByRole('button', {name: 'Skip source'}));
  expect(next).toHaveBeenCalledTimes(2);
});

it('offers explicit Resume when the browser rejects automatic playback', async () => {
  HTMLMediaElement.prototype.play = vi.fn().mockRejectedValueOnce(new DOMException('blocked', 'NotAllowedError')).mockResolvedValue(undefined);
  const client = createClient({ getToken: () => '', isRemote: () => true, fetchImpl: async () => new Response(new Blob(['audio'])) });
  const view = render(<AudioPlayer item={a} client={client} playRequest={{nonce: 1}} />);
  const audio = view.container.querySelector('audio')!;
  await waitFor(() => expect(audio.src).toContain('blob:'));
  fireEvent.loadedMetadata(audio);
  const resume = await screen.findByRole('button', {name: 'Resume playback'});
  fireEvent.click(resume);
  await waitFor(() => expect(screen.queryByRole('button', {name: 'Resume playback'})).toBeNull());
});
