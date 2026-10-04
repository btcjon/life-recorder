// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { ContextClient } from "../context/contracts";
import { HealthView } from "./HealthView";

afterEach(cleanup);
it("reads health without diagnostics or mutations and reports failed refresh as stale", async () => {
  const get = vi.fn(async () => ({ processing: { pending_count: 2, attention_count: 1 }, stages: { topics: { enabled: false } } }));
  const post = vi.fn();
  const client: ContextClient = { getJson: get as ContextClient['getJson'], postJson: post as ContextClient['postJson'] };
  render(<HealthView client={client} />);
  await screen.findByText("Pending: 2");
  expect(get).toHaveBeenCalledExactlyOnceWith("/v1/health", expect.any(AbortSignal)); expect(post).not.toHaveBeenCalled();
  get.mockRejectedValueOnce(new Error("Offline")); fireEvent.click(screen.getByRole("button", { name: "Refresh health" }));
  await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("may be stale"));
  expect(screen.getByText("Pending: 2")).toBeTruthy(); expect(post).not.toHaveBeenCalled();
});
