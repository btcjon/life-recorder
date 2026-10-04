import { describe, expect, it } from "vitest";
import { TOKEN_STORAGE_KEY, bootstrapAuth } from "./auth";

function memoryStorage(initial: Record<string, string> = {}) {
  const data = { ...initial };
  return {
    data,
    getItem(key: string) {
      return Object.prototype.hasOwnProperty.call(data, key) ? data[key] : null;
    },
    setItem(key: string, value: string) {
      data[key] = value;
    },
    removeItem(key: string) {
      delete data[key];
    },
  };
}

describe("auth bootstrap", () => {
  it("stores a local fragment in sessionStorage and clears it without dropping search", () => {
    const session = memoryStorage();
    const local = memoryStorage();
    const result = bootstrapAuth({
      hostname: "127.0.0.1",
      hash: "#secret-token-value",
      pathname: "/ui-v2/",
      search: "?view=recordings&day=2026-10-04",
      session,
      local,
    });
    expect(result.remote).toBe(false);
    expect(result.token).toBe("secret-token-value");
    expect(result.clearedFragment).toBe(true);
    expect(result.url).toBe("/ui-v2/?view=recordings&day=2026-10-04");
    expect(session.data[TOKEN_STORAGE_KEY]).toBe("secret-token-value");
    expect(local.data[TOKEN_STORAGE_KEY]).toBeUndefined();
    expect(result.url.includes("secret-token-value")).toBe(false);
    expect(result.url.includes("#")).toBe(false);
  });

  it("reuses the session token when the fragment is empty", () => {
    const session = memoryStorage({ [TOKEN_STORAGE_KEY]: "stored-token" });
    const result = bootstrapAuth({
      hostname: "localhost",
      hash: "",
      pathname: "/ui-v2/",
      search: "",
      session,
    });
    expect(result.token).toBe("stored-token");
    expect(result.clearedFragment).toBe(false);
    expect(result.url).toBe("/ui-v2/");
  });

  it("does not store a remote fragment and still clears it", () => {
    const session = memoryStorage();
    const local = memoryStorage({ [TOKEN_STORAGE_KEY]: "should-not-stay" });
    const result = bootstrapAuth({
      hostname: "lr.genr8ive.ai",
      hash: "#access-fragment",
      pathname: "/ui-v2/",
      search: "?day=2026-10-04",
      session,
      local,
    });
    expect(result.remote).toBe(true);
    expect(result.token).toBe("");
    expect(session.data[TOKEN_STORAGE_KEY]).toBeUndefined();
    expect(local.data[TOKEN_STORAGE_KEY]).toBeUndefined();
    expect(result.url).toBe("/ui-v2/?day=2026-10-04");
  });
});
