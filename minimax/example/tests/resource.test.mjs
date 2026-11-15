import assert from "node:assert/strict";
import { test } from "node:test";
import { loadList } from "./fixture.mjs";

const list = await loadList();

test("RES-12 exact normalized resource excludes the adjacent room code", () => {
  assert.deepEqual(list({ resource: "  RoOm-A  " }), ["cancelled", "late"]);
});

test("RES-12 resource combines with the existing state filter", () => {
  assert.deepEqual(list({ resource: "room-a", state: "open" }), ["late"]);
});

test("RES-12 nonexistent code has no matching reservation", () => {
  assert.deepEqual(list({ resource: "room-z" }), []);
});

test("RES-12 whitespace-only filter is equivalent to omission", () => {
  assert.deepEqual(list({ resource: "   " }), list());
});
