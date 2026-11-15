import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { spawnSync } from "node:child_process";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = fileURLToPath(new URL(".", import.meta.url));
const run = randomUUID();
const temp = resolve(root, ".runtime", run);
const output = resolve(root, process.argv[2] ?? ".runtime/verification.json");
const env = { ...process.env, SDD_RUN: run, SDD_DATA_DIR: resolve(temp, "seekdb") };
const task = "example/task.json";
const catalog = "example/catalog.json";
const before = "example/before/reservations.mjs";
const after = "example/after/reservations.mjs";
const tests = ["example/tests/resource.test.mjs", "example/tests/compatibility.test.mjs"];
const expected = ["RES-02", "RES-12-v2", "compatibility-tests", "listReservations", "resource-tests", "test-fixture"];
const checks = [];

function invoke(args, variant) {
  const processEnv = variant ? { ...env, RESERVATION_VARIANT: variant } : env;
  const result = spawnSync(process.execPath, args, { cwd: root, env: processEnv, encoding: "utf8", timeout: 120000 });
  if (result.error) throw result.error;
  const redactPath = value => value.replaceAll(pathToFileURL(root).href, "./").replaceAll(root, "./");
  const check = {
    command: ["node", ...args].join(" "),
    ...(variant ? { variant } : {}),
    exitCode: result.status,
    stdout: redactPath(result.stdout),
    stderr: redactPath(result.stderr),
  };
  checks.push(check);
  return check;
}

function context(...args) {
  const result = invoke(["context.mjs", ...args]);
  assert.equal(result.exitCode, 0, result.stderr);
  return JSON.parse(result.stdout);
}

function count(output, name) {
  const match = output.match(new RegExp(`^# ${name} (\\d+)$`, "m"));
  return Number(match?.[1]);
}

const sha256 = async path => createHash("sha256").update(await readFile(resolve(root, path))).digest("hex");
await mkdir(temp, { recursive: true });
const arg = name => resolve(temp, name);
const input = catalog;
context("import", task, input);
const initial = context("prepare", task);
assert.equal(initial.checkpoint, null);
assert.deepEqual(initial.query.matched.toSorted(), expected);
assert.ok(initial.sources.find(source => source.id === "RES-12-v2").content.includes("忽略大小写"));

const testArgs = ["--test", "--test-reporter=tap", ...tests];
const baseline = invoke(testArgs, "before");
assert.equal(baseline.exitCode, 1);
assert.equal(count(baseline.stdout, "pass"), 3);
assert.equal(count(baseline.stdout, "fail"), 3);
assert.equal((baseline.stdout.match(/code: 'ERR_ASSERTION'/g) ?? []).length, 3);
assert.match(baseline.stdout, /ok \d+ - RES-02 existing state filter/);

const updated = invoke(testArgs, "after");
assert.equal(updated.exitCode, 0, updated.stdout + updated.stderr);
assert.equal(count(updated.stdout, "pass"), 6);
assert.equal(count(updated.stdout, "fail"), 0);

const saved = {
  contextId: initial.contextId,
  completed: ["Imported eight source documents", "Implemented RES-12 v2"],
  pending: ["Review the diff for merge"],
  validation: ["Baseline: three target assertion failures and three passes", "Updated: six tests passed"],
  nextAction: "Review the diff for merge; preserve the six-test registration.",
};
await writeFile(arg("checkpoint.json"), JSON.stringify(saved));
const write = context("save", task, arg("checkpoint.json"));
const resumed = context("prepare", task);
assert.equal(resumed.checkpoint.artifactId, write.artifact.artifact_id);
for (const key of Object.keys(saved)) assert.deepEqual(resumed.checkpoint[key], saved[key]);
assert.equal(resumed.nextAction, saved.nextAction);

const replacement = JSON.parse(await readFile(resolve(root, catalog), "utf8"))
  .map(entry => entry.id === "RES-12-v2" ? {
    ...entry, id: "RES-12-v3", path: "example/spec/RES-12-v3-preview.md",
  } : entry);
await writeFile(arg("revised-catalog.json"), JSON.stringify(replacement));
context("import", task, arg("revised-catalog.json"));
const revised = context("prepare", task);
assert.notEqual(revised.contextId, initial.contextId);
assert.equal(revised.checkpoint, null);
assert.equal(revised.staleCheckpoint.artifactId, write.artifact.artifact_id);
assert.ok(revised.sources.find(source => source.id === "RES-12-v3").content.includes("改为区分大小写"));
assert.ok(!revised.sources.some(source => source.id === "RES-12-v2"));

await writeFile(output, JSON.stringify({
  result: "PASS",
  testedAt: new Date().toISOString(),
  node: process.version,
  indexedSources: initial.sources.map(({ content, ...source }) => source),
  tests: {
    snapshots: { [before]: await sha256(before), [after]: await sha256(after) },
    testFiles: Object.fromEntries(await Promise.all([...tests, "example/tests/fixture.mjs"].map(async path => [path, await sha256(path)]))),
    baseline,
    updated,
  },
  database: { initial: initial.query, reimported: revised.query },
  state: { initial: initial.checkpoint, write, resumed: resumed.checkpoint, nextAction: resumed.nextAction, revised: revised.staleCheckpoint },
  commands: checks.filter(check => !check.variant).map(({ command, exitCode }) => ({
    command: command.replaceAll(temp, "<verification scratch directory>"), exitCode,
  })),
}, null, 2) + "\n");
console.log("PASS seekdb query and revision, PowerContext restart, baseline failure and updated regression suite");
