import assert from "node:assert/strict";
import { test } from "node:test";
import { loadList, reservations } from "./fixture.mjs";

const list = await loadList();

test("RES-02 no filter returns every reservation sorted by start time", () => {
  assert.deepEqual(list(), ["early", "cancelled", "adjacent", "late"]);
  assert.deepEqual(reservations.map(row => row.id), ["late", "early", "cancelled", "adjacent"]);
});

test("RES-02 existing state filter remains exact and results stay sorted", () => {
  assert.deepEqual(list({ state: "open" }), ["early", "adjacent", "late"]);
  assert.deepEqual(list({ state: "OPEN" }), []);
});
