import { describe, expect, it } from "vitest";
import { ApiError, acceptSelectionResponse, createClient, createRequestGate, retryFailureCopy } from "./client";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("api client", () => {
  it("sends a local bearer and never puts the token in the URL", async () => {
    const calls: Array<{ url: string; headers: Headers }> = [];
    const client = createClient({
      getToken: () => "local-secret-token",
      isRemote: () => false,
      fetchImpl: async (url, init) => {
        calls.push({ url, headers: new Headers(init?.headers) });
        return jsonResponse(200, { days: ["2026-10-04"] });
      },
    });
    await client.days();
    expect(calls[0]?.url).toBe("/v1/days");
    expect(calls[0]?.headers.get("Authorization")).toBe("Bearer local-secret-token");
    expect(calls[0]?.url.includes("local-secret-token")).toBe(false);
  });

  it("omits the bearer for remote Access", async () => {
    const client = createClient({
      getToken: () => "ignored",
      isRemote: () => true,
      fetchImpl: async (_url, init) => {
        expect(new Headers(init?.headers).has("Authorization")).toBe(false);
        return jsonResponse(200, { ok: true });
      },
    });
    await client.getJson("/v1/health");
  });

  it("surfaces HTTP 409 without selecting a stale body", async () => {
    const client = createClient({
      getToken: () => "",
      isRemote: () => true,
      fetchImpl: async () => jsonResponse(409, { error: "Original audio unavailable" }),
    });
    await expect(client.retryChunk("chunk-1")).rejects.toMatchObject({ status: 409 });
    try {
      await client.retryChunk("chunk-1");
    } catch (error) {
      expect(error).toBeInstanceOf(ApiError);
      expect(retryFailureCopy(error)).toBe("Original audio is unavailable; this recording cannot be retried.");
      expect(String(error).includes("token")).toBe(false);
    }
  });

  it("refuses a path that contains the session token", async () => {
    const client = createClient({
      getToken: () => "local-secret-token",
      isRemote: () => false,
      fetchImpl: async () => jsonResponse(200, {}),
    });
    await expect(client.getJson("/v1/days?access=local-secret-token")).rejects.toThrow(/credential in URL/);
  });

  it("aborts the previous selection request and ignores it", () => {
    const gate = createRequestGate();
    const first = gate.start("day");
    const second = gate.start("day");
    expect(first.signal.aborted).toBe(true);
    expect(first.isCurrent()).toBe(false);
    expect(second.isCurrent()).toBe(true);
    expect(acceptSelectionResponse({ kind: "chunk", id: "a" }, { kind: "chunk", id: "b" })).toBe(false);
    expect(acceptSelectionResponse({ kind: "chunk", id: "b" }, { kind: "chunk", id: "b" })).toBe(true);
    expect(acceptSelectionResponse({ kind: "event", id: "b" }, { kind: "chunk", id: "b" })).toBe(false);
  });
});
