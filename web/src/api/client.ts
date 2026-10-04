import type {
  ClusterReport,
  DayPayload,
  DaysPayload,
  HealthReport,
  IdentityMutationResult,
  IdentityProfiles,
  LabelTurnsBody,
  Person,
  ReviewDiagnostics,
  ReviewPage,
  ReviewPayload,
  SelectionRef,
} from "./types";

export class ApiError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(status: number, payload: unknown) {
    super(apiErrorMessage(status, payload));
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }

  get conflict(): boolean {
    return this.status === 409;
  }
}

export function apiErrorMessage(status: number, payload: unknown): string {
  if (payload && typeof payload === "object" && "error" in payload && typeof payload.error === "string") {
    return payload.error;
  }
  if (status === 401) return "Unauthorized";
  if (status === 409) return "Conflict";
  return "Request failed";
}

export function isAbortError(error: unknown): boolean {
  return !!error && typeof error === "object" && "name" in error && error.name === "AbortError";
}

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export type ApiClientOptions = {
  fetchImpl?: FetchLike;
  getToken: () => string;
  isRemote: () => boolean;
};

export type RequestTicket = {
  key: string;
  generation: number;
  signal: AbortSignal;
  isCurrent: () => boolean;
};

export function createRequestGate() {
  const generations = new Map<string, number>();
  const controllers = new Map<string, AbortController>();
  return {
    start(key: string): RequestTicket {
      controllers.get(key)?.abort();
      const controller = new AbortController();
      controllers.set(key, controller);
      const generation = (generations.get(key) ?? 0) + 1;
      generations.set(key, generation);
      return {
        key,
        generation,
        signal: controller.signal,
        isCurrent: () => generations.get(key) === generation,
      };
    },
    isCurrent(key: string, generation: number): boolean {
      return generations.get(key) === generation;
    },
  };
}

export function acceptSelectionResponse(requested: SelectionRef, current: SelectionRef | null): boolean {
  return !!current && requested.kind === current.kind && requested.id === current.id;
}

export function createClient(options: ApiClientOptions) {
  const fetchImpl = options.fetchImpl ?? fetch;

  function assertNoCredentialInUrl(path: string): void {
    const token = options.getToken();
    if (token && token.length >= 8 && path.includes(token)) {
      throw new Error("Refusing credential in URL");
    }
    if (path.includes("#")) {
      throw new Error("Refusing fragment in API path");
    }
  }

  async function request(path: string, init: RequestInit = {}): Promise<Response> {
    assertNoCredentialInUrl(path);
    const headers = new Headers(init.headers);
    if (!options.isRemote()) {
      const token = options.getToken();
      if (token) headers.set("Authorization", `Bearer ${token}`);
    }
    return fetchImpl(path, {
      ...init,
      headers,
      cache: "no-store",
      credentials: "same-origin",
    });
  }

  async function readError(response: Response): Promise<ApiError> {
    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    return new ApiError(response.status, payload);
  }

  async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
    const response = await request(path, { method: "GET", signal });
    if (!response.ok) throw await readError(response);
    return (await response.json()) as T;
  }

  async function postJson<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
    const response = await request(path, {
      method: "POST",
      signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) throw await readError(response);
    return (await response.json()) as T;
  }

  async function getBlob(path: string, signal?: AbortSignal): Promise<Blob> {
    const response = await request(path, { method: "GET", signal });
    if (!response.ok) throw await readError(response);
    return response.blob();
  }

  return {
    getJson,
    postJson,
    getBlob,
    days: (signal?: AbortSignal) => getJson<DaysPayload>("/v1/days", signal),
    day: (day: string, signal?: AbortSignal) => getJson<DayPayload>(`/v1/days/${encodeURIComponent(day)}`, signal),
    review: (chunkId: string, signal?: AbortSignal) =>
      getJson<ReviewPayload>(`/v1/chunks/${encodeURIComponent(chunkId)}/review`, signal),
    health: (signal?: AbortSignal) => getJson<HealthReport>("/v1/health", signal),
    keepAudio: (chunkId: string) => postJson<{ kept?: boolean }>(`/v1/chunks/${encodeURIComponent(chunkId)}/keep`, {}),
    retryChunk: (chunkId: string) => postJson<unknown>(`/v1/chunks/${encodeURIComponent(chunkId)}/retry`, {}),
    saveEventEdit: (body: Record<string, unknown>) => postJson<unknown>("/v1/event-edits", body),
    labelTurn: (turnId: string, body: { person_id: string; use_sample: boolean; revision?: number }) =>
      postJson<IdentityMutationResult>(`/v1/turns/${encodeURIComponent(turnId)}/label`, body),
    labelTurns: (body: LabelTurnsBody) => postJson<IdentityMutationResult>("/v1/identities/label", body),
    createPerson: (name: string) => postJson<{ id: string; name: string }>("/v1/people", { name }),
    renamePerson: (personId: string, name: string) =>
      postJson<Person>(`/v1/people/${encodeURIComponent(personId)}`, { name }),
    clearLabels: (body: LabelTurnsBody) => postJson<IdentityMutationResult>("/v1/identities/label", body),
    identityProfiles: (signal?: AbortSignal) => getJson<IdentityProfiles>("/v1/identity-profiles", signal),
    speakerClusters: (signal?: AbortSignal) => getJson<ClusterReport>("/v1/speaker-clusters", signal),
    identityMutation: (action: "remove_sample" | "undo" | "merge" | "split" | "name" | "label", body: Record<string, unknown>) =>
      postJson<IdentityMutationResult>(`/v1/identities/${action}`, body),
    speakerReview: (cursor: string | null, signal?: AbortSignal) => {
      const params = new URLSearchParams();
      params.set("limit", "20");
      if (cursor) params.set("cursor", cursor);
      return getJson<ReviewPage>(`/v1/speaker-review?${params.toString()}`, signal);
    },
    reviewDiagnostics: (signal?: AbortSignal) => getJson<ReviewDiagnostics>("/v1/speaker-review/diagnostics", signal),
    rejectSuggestion: (turnId: string, personId: string) =>
      postJson<{ rejected?: boolean }>(`/v1/turns/${encodeURIComponent(turnId)}/reject`, { person_id: personId }),
  };
}

export type ApiClient = ReturnType<typeof createClient>;

export function retryFailureCopy(error: unknown): string {
  if (error instanceof ApiError && error.status === 409) {
    return "Original audio is unavailable; this recording cannot be retried.";
  }
  return "Retry did not start. Please try again.";
}

export function eventEditFailureCopy(error: unknown): string {
  if (error instanceof ApiError && error.status === 409) {
    return "This event changed. Refresh before saving again.";
  }
  if (error instanceof ApiError && error.message) return error.message;
  return "Couldn't save this event.";
}

export function labelFailureCopy(error: unknown): string {
  if (error instanceof ApiError && error.status === 409) {
    return "Speaker identities changed. Reload before editing.";
  }
  return "Could not save that identity.";
}

export function identityFailureCopy(error: unknown): string {
  if (error instanceof ApiError && error.message && error.status !== 409) return error.message;
  return labelFailureCopy(error);
}

export function healthSummary(report: HealthReport | null, failed: boolean): string {
  if (failed || !report) return "Processing status unavailable. Check that your Mac receiver is reachable.";
  const queue = report.processing || report.queue || {};
  const pending = Number(queue.pending_count ?? queue.pending ?? 0);
  const attention = Number(queue.attention_count ?? queue.needs_attention ?? 0);
  return `Pending: ${pending} · Needs attention: ${attention}`;
}
