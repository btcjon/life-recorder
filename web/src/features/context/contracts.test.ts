import { describe, expect, it } from "vitest";
import { contextError, placeBody } from "./contracts";

describe("context mutation contracts", () => {
  it("keeps exact revision and explicitly parses coordinates", () => {
    expect(placeBody({ name: " Office ", latitude: "0", longitude: "-70", radius: "100" }, { id: "place", revision: 9, name: "old", latitude: 1, longitude: 2, radius_m: 3 })).toEqual({ id: "place", revision: 9, name: "Office", latitude: 0, longitude: -70, radius_m: 100 });
  });
  it("rejects blank, nonfinite and out-of-range coordinates", () => {
    for (const latitude of ["", "NaN", "Infinity", "91"]) expect(() => placeBody({ name: "Place", latitude, longitude: "0", radius: "100" })).toThrow();
    expect(() => placeBody({ name: "Place", latitude: "0", longitude: "181", radius: "100" })).toThrow();
    expect(() => placeBody({ name: "Place", latitude: "0", longitude: "0", radius: "0" })).toThrow();
  });
  it("requires reread after conflict, never a blind retry", () => {
    expect(contextError({ status: 409 }).conflict).toBe(true);
    expect(contextError({ status: 409 }).text).toContain("Reload current revisions");
    expect(contextError(new Error("Unavailable")).conflict).toBe(false);
  });
});
