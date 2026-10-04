import { describe, expect, it } from "vitest";
import { filterReview, fullyNamed, groupLabelBody, reviewChoiceValue } from "./identity";
import type { TurnGroup } from "./recordings";

function group(): TurnGroup {
  return {
    key: "g",
    chunk: { id: "chunk-1" },
    turns: [
      { id: "turn-a", text: "one" },
      { id: "turn-b", text: "two" },
    ],
    preserved: false,
  };
}

describe("group labels", () => {
  it("labels every turn and keeps enrollment off unless asked", () => {
    const off = groupLabelBody(group(), "person-1", false, 4);
    expect(off).toEqual({
      body: { turn_ids: ["turn-a", "turn-b"], person_id: "person-1", use_sample: false, revision: 4 },
    });
    const on = groupLabelBody(group(), "person-1", true, 4);
    expect("body" in on && on.body.use_sample).toBe(true);
    expect(groupLabelBody(group(), null, true, 4)).toEqual({ error: "Removing names cannot save a voice sample." });
    expect(groupLabelBody(group(), "person-1", false, undefined)).toEqual({ error: "Reload before editing." });
  });

  it("does not treat a partial name as complete", () => {
    const partial = group();
    partial.turns[0].person_id = "person-1";
    expect(fullyNamed(partial, "person-1")).toBe(false);
    partial.turns[1].person_id = "person-1";
    expect(fullyNamed(partial, "person-1")).toBe(true);
  });
});

describe("review choices", () => {
  it("does not preselect a suggestion and filters loaded text only", () => {
    expect(reviewChoiceValue(undefined)).toBe("");
    const items = [
      { turn_id: "t1", chunk_id: "c1", stored_name: "Ada", suggestions: [{ person_id: "p1", name: "Ada" }] },
      { turn_id: "t2", chunk_id: "c2", suggestions: [{ person_id: "p2", name: "<script>" }] },
    ];
    expect(filterReview(items, "script").map((item) => item.turn_id)).toEqual(["t2"]);
    expect(filterReview(items, "").length).toBe(2);
  });
});
