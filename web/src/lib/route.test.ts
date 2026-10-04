import { describe, expect, it } from "vitest";
import { DEFAULT_ROUTE, readRoute, routeUrl, writeRoute } from "./route";

describe("route state", () => {
  it("opens legacy selected deep links in detail and preserves an explicit Back-to-list state", () => {
    const state = readRoute(new URLSearchParams("day=2026-10-04&id=old&kind=event"));
    expect(state.pane).toBe("detail");
    const returned = readRoute(new URLSearchParams(writeRoute({ ...state, pane: "list" })));
    expect(returned.pane).toBe("list");
    expect(returned.selectedId).toBe("old");
    expect(readRoute(new URLSearchParams("groups=1&kind=group&id=saved")).selectedKind).toBe("group");
  });
  it("maps allowlisted deep links and canonicalizes later navigation", () => {
    expect(readRoute(new URLSearchParams(), "/ui-v2/context").view).toBe("context");
    expect(readRoute(new URLSearchParams("view=review"), "/ui-v2/context").view).toBe("review");
    const state = readRoute(new URLSearchParams("view=recordings&day=2026-10-04"), "/ui-v2/health");
    expect(routeUrl("/ui-v2/health", state)).toBe("/ui-v2/?day=2026-10-04&groups=1");
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

  it("defaults to grouped context and roundtrips flat, groups, and older deep links", () => {
    expect(DEFAULT_ROUTE.groups).toBe(true);
    expect(readRoute(new URLSearchParams("day=2026-10-04")).groups).toBe(true);
    expect(readRoute(new URLSearchParams("groups=0&id=clip&kind=chunk"))).toMatchObject({ groups: false, selectedId: "clip", selectedKind: "chunk" });
    expect(readRoute(new URLSearchParams("flat=1&id=speech&kind=event"))).toMatchObject({ groups: false, flat: true, selectedKind: "event", pane: "detail" });
    expect(readRoute(new URLSearchParams("groups=1&kind=group&id=saved&pane=list"))).toMatchObject({ groups: true, selectedKind: "group", pane: "list" });
    const grouped = readRoute(new URLSearchParams("day=2026-10-04&id=topic&kind=group"));
    expect(readRoute(new URLSearchParams(writeRoute(grouped)))).toMatchObject({ groups: true, selectedId: "topic", selectedKind: "group", pane: "detail" });
    const flat = readRoute(new URLSearchParams("flat=1&day=2026-10-04&id=clip"));
    const writtenFlat = writeRoute({ ...flat, pane: "list" });
    expect(writtenFlat).toContain("groups=0");
    expect(writtenFlat).toContain("flat=1");
    expect(readRoute(new URLSearchParams(writtenFlat))).toMatchObject({ groups: false, flat: true, selectedId: "clip", selectedKind: "chunk", pane: "list" });
  });
});
