// @vitest-environment jsdom
import { act, cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { App } from '../../App';
import { createClient } from '../../api/client';

afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });

it('marks hung health unavailable, aborts its request and preserves the last good snapshot', async () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-29T16:00:00Z'));
  Object.defineProperty(window, 'localStorage', { configurable: true, value: { getItem: () => null, setItem: vi.fn() } });
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
  history.replaceState(null, '', '/ui-v2/?day=2026-10-29&view=health');
  HTMLMediaElement.prototype.load = vi.fn();
  const signals: AbortSignal[] = [];
  let finishHung!: (response: Response) => void;
  let healthCalls = 0;
  const client = createClient({ getToken: () => '', isRemote: () => true, fetchImpl: async (url, options) => {
    if (url === '/v1/health') {
      signals.push(options!.signal as AbortSignal);
      if (++healthCalls === 2) return new Promise<Response>(resolve => { finishHung = resolve; });
      return new Response(JSON.stringify({ checked_at: Date.now() / 1000, processing: { pending: healthCalls === 1 ? 2 : 0, engine_ready: true, last_completed_at: Date.now() / 1000 } }));
    }
    return new Response(JSON.stringify(url === '/v1/days' ? { days: ['2026-10-29'] } : { chunks: [] }));
  } });
  await act(async () => { render(<App client={client} />); });
  expect(within(screen.getByRole('banner')).getByText(/Pending: 2/)).toBeTruthy();
  await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(within(screen.getByRole('banner')).getByText(/Processing status unavailable/)).toBeTruthy();
  expect(signals[1]!.aborted).toBe(true);
  expect(screen.getByText('Pending: 2')).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Refresh health' }).hasAttribute('disabled')).toBe(false);
  await act(async () => { finishHung(new Response(JSON.stringify({ processing: { pending: 99 } }))); });
  expect(screen.queryByText('Pending: 99')).toBeNull();
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(within(screen.getByRole('banner')).getByText(/Pending: 0/)).toBeTruthy();
  expect(screen.queryByRole('alert')).toBeNull();
});
