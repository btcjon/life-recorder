import { ApiError } from "../api/client";
import type { IdentityMutationResult } from "../api/types";

export const UNKNOWN_OUTCOME = "Save outcome is unknown. Reload and read back before editing. Nothing was retried.";
export const CONFLICT_COPY = "Speaker identities changed. Reload before editing. The failed change was not retried.";
export const FAILED_RELOAD_LOCK = "Reload and read back before editing. The failed reload did not clear the lock.";
export const CREATE_ASSIGN_NOTICE = "Person was created, but assignment did not finish. Choose the existing person or reload and read back. Another person was not created.";

export function isUnknownSaveOutcome(error: unknown): boolean {
  // A proxy error or aborted response cannot prove the server did not commit.
  return !(error instanceof ApiError) || error.status >= 500 || error.status === 408;
}

export function enrollmentFailure(requested: boolean, result: IdentityMutationResult | null | undefined): string | null {
  if (!requested || result?.enrollment?.enrolled) return null;
  const reason = result?.enrollment?.reason;
  return reason ? "Voice sample was not enrolled: " + reason : "Voice sample was requested but was not enrolled.";
}
