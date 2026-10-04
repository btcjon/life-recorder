import { expect, it } from "vitest";
import { ApiError } from "../api/client";
import { isUnknownSaveOutcome } from "./identitySafety";
it("treats proxy failures and response aborts as uncertain dispatched saves", () => {
  for (const status of [408, 500, 502, 504]) expect(isUnknownSaveOutcome(new ApiError(status, {}))).toBe(true);
  expect(isUnknownSaveOutcome(new DOMException("Response lost", "AbortError"))).toBe(true);
  for (const status of [400, 401, 403, 409, 422]) expect(isUnknownSaveOutcome(new ApiError(status, {}))).toBe(false);
});
