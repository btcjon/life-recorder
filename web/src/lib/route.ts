export const VIEWS = ["recordings", "review", "people", "context", "health"] as const;
export type AppView = (typeof VIEWS)[number];
export type SelectedKind = "chunk" | "event" | "group";
export type DetailMode = "transcript" | "turns";
export type PaneMode = "list" | "detail";

export type RouteState = {
  view: AppView;
  day: string;
  query: string;
  showQuiet: boolean;
  showPending: boolean;
  flat: boolean;
  groups: boolean;
  selectedId: string | null;
  selectedKind: SelectedKind;
  detailMode: DetailMode;
  pane: PaneMode;
};

export const DEFAULT_ROUTE: RouteState = {
  view: "recordings",
  day: "",
  query: "",
  showQuiet: false,
  showPending: false,
  flat: false,
  groups: false,
  selectedId: null,
  selectedKind: "chunk",
  detailMode: "transcript",
  pane: "list",
};

function isView(value: string | null): value is AppView {
  return !!value && (VIEWS as readonly string[]).includes(value);
}

export function readRoute(params: URLSearchParams, pathname = ""): RouteState {
  const pathView = pathname.replace(/\/$/, "").split("/").at(-1) || null;
  const view = params.get("view") || pathView;
  const kind = params.get("kind");
  const detail = params.get("detail");
  const pane = params.get("pane");
  return {
    view: isView(view) ? view : "recordings",
    day: params.get("day") || "",
    query: params.get("q") || "",
    showQuiet: params.get("show") === "1",
    showPending: params.get("pending") === "1",
    flat: params.get("flat") === "1",
    groups: params.get("groups") === "1",
    selectedId: params.get("id") || null,
    selectedKind: kind === "event" ? "event" : kind === "group" ? "group" : "chunk",
    detailMode: detail === "turns" ? "turns" : "transcript",
    pane: pane === "list" ? "list" : pane === "detail" || params.get("id") ? "detail" : "list",
  };
}

export function writeRoute(state: RouteState): string {
  const params = new URLSearchParams();
  if (state.view !== "recordings") params.set("view", state.view);
  if (state.day) params.set("day", state.day);
  if (state.query) params.set("q", state.query);
  if (state.showQuiet) params.set("show", "1");
  if (state.showPending) params.set("pending", "1");
  if (state.flat) params.set("flat", "1");
  if (state.groups) params.set("groups", "1");
  if (state.selectedId) {
    params.set("id", state.selectedId);
    if (state.selectedKind === "event") params.set("kind", "event");
    if (state.selectedKind === "group") params.set("kind", "group");
  }
  if (state.detailMode === "turns") params.set("detail", "turns");
  if (state.pane === "detail") params.set("pane", "detail");
  else if (state.selectedId) params.set("pane", "list");
  return params.toString();
}

export function routeUrl(pathname: string, state: RouteState): string {
  const search = writeRoute(state);
  const path = pathname.startsWith("/ui-v2") ? "/ui-v2/" : pathname;
  return path + (search ? `?${search}` : "");
}

export function isLaterView(view: AppView): view is "context" | "health" {
  return view === "context" || view === "health";
}
