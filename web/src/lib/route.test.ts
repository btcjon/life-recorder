import { describe, expect, it } from "vitest";
import { readRoute, routeUrl, writeRoute } from "./route";

describe("route state", () => {
  it("maps allowlisted deep links and canonicalizes later navigation", () => {
    expect(readRoute(new URLSearchParams(), "/ui-v2/context").view).toBe("context");
    expect(readRoute(new URLSearchParams("view=review"), "/ui-v2/context").view).toBe("review");
    const state = readRoute(new URLSearchParams("view=recordings&day=2026-10-04"), "/ui-v2/health");
    expect(routeUrl("/ui-v2/health", state)).toBe("/ui-v2/?day=2026-10-04");
  });
  it("uses search params and never a hash or token", () => {
    const state = readRoute(new URLSearchParams("day=2026-10-04&q=hello&show=1&flat=1&id=abc&kind=event&detail=turns&pane=detail&view=recordings"));
    expect(state.day).toBe("2026-10-04");
    expect(state.showQuiet).toBe(true);
    expect(state.flat).toBe(true);
    expect(state.selectedKind).toBe("event");
    expect(state.detailMode).toBe("turns");
    const url = routeUrl("/ui-v2/", state);
    expect(url.startsWith("/ui-v2/?")).toBe(true);
    expect(url.includes("#")).toBe(false);
    expect(url.includes("token")).toBe(false);
    expect(writeRoute({ ...state, view: "review" })).toContain("view=review");
  });
});
