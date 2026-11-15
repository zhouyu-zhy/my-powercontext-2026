import { createHash } from "node:crypto";
import { mkdir, readFile, realpath } from "node:fs/promises";
import { isAbsolute, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { seekdb } from "@oceanbase/seekdb-node";
import mysql from "mysql2/promise";

const root = fileURLToPath(new URL(".", import.meta.url));
const data = resolve(process.env.SDD_DATA_DIR ?? resolve(root, ".runtime/seekdb"));
const server = (process.env.POWERCONTEXT_SERVER_URL ?? "http://127.0.0.1:8791").replace(/\/+$/, "");
const token = process.env.POWERCONTEXT_API_TOKEN;
const run = process.env.SDD_RUN ?? "default";
const marker = "sdd-checkpoint-v1";
const kinds = ["spec", "code", "test"];
const [command, taskFile, input] = process.argv.slice(2);
const hash = text => createHash("sha256").update(text).digest("hex");
const json = async path => JSON.parse(await readFile(path, "utf8"));

async function loadSource(path) {
  const actual = await realpath(resolve(root, path));
  const fromRoot = relative(await realpath(root), actual);
  if (!fromRoot || fromRoot === ".." || fromRoot.startsWith(`..${sep}`) || isAbsolute(fromRoot)) {
    throw new Error(`Source escapes project directory: ${path}`);
  }
  return readFile(actual, "utf8");
}

async function importCatalog(db, task, catalog) {
  if (!Array.isArray(catalog) || !catalog.length) throw new Error("Expected a non-empty catalog");
  const index = [];
  const ids = new Set();
  for (const entry of catalog) {
    if (!/^[A-Za-z0-9_-]{1,100}$/.test(entry.id) || ids.has(entry.id) ||
        !kinds.includes(entry.kind) || typeof entry.active !== "boolean" ||
        typeof entry.topics !== "string" || entry.topics.length > 500 ||
        typeof entry.path !== "string" || entry.path.length > 255) {
      throw new Error(`Invalid catalog entry: ${entry.id}`);
    }
    ids.add(entry.id);
    const content = await loadSource(entry.path);
    index.push([task.id, entry.id, entry.kind, entry.path, entry.topics.toLowerCase(), entry.active, content, hash(content)]);
  }
  await db.beginTransaction();
  try {
    await db.execute("DELETE FROM engineering_documents WHERE project = ?", [task.id]);
    const placeholders = index.map(() => "(?, ?, ?, ?, ?, ?, ?, ?)").join(", ");
    await db.execute(`INSERT INTO engineering_documents VALUES ${placeholders}`, index.flat());
    await db.commit();
  } catch (error) {
    await db.rollback();
    throw error;
  }
  return { engine: "seekdb", imported: index.length, task: task.id };
}

async function prepare(db, task) {
  const params = [task.id, ...task.keywords.map(word => `% ${word.toLowerCase()} %`)];
  const filters = task.keywords.map(() => "CONCAT(' ', topics, ' ') LIKE ?").join(" AND ");
  const sql = `SELECT id, kind, path, sha256, content FROM engineering_documents
    WHERE project = ? AND active = 1 AND ${filters} ORDER BY kind, id`;
  const [rows] = await db.execute(sql, params);
  const [[{ version }]] = await db.query("SELECT VERSION() AS version");
  for (const kind of kinds) {
    if (!rows.some(row => row.kind === kind)) throw new Error(`No active ${kind} sources; check the catalog and task keywords`);
  }
  const sources = rows.map(row => ({ ...row, lines: [1, row.content.trimEnd().split("\n").length] }));
  const contextId = hash(JSON.stringify(sources.map(row => [row.id, row.path, row.sha256])));
  const history = await recall(task);
  const checkpoint = history?.contextId === contextId ? history : null;
  return {
    task, contextId, sources, checkpoint,
    nextAction: checkpoint?.nextAction ?? "Implement the active specification and run the listed tests.",
    staleCheckpoint: history && !checkpoint ? { artifactId: history.artifactId, contextId: history.contextId } : null,
    query: { engine: "seekdb", version, sql, params, matched: rows.map(row => row.id) },
  };
}

async function pc(path, body) {
  const response = await fetch(server + path, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) throw new Error(`PowerContext ${path} returned HTTP ${response.status}`);
  return response.json();
}

async function stateScope(task) {
  const idempotency_key = `sdd-${task.id}-${hash(run).slice(0, 20)}`;
  const scope = await pc("/v1/scopes", {
    title: `SDD ${task.id}`, summary: "Task checkpoints and validation status", idempotency_key,
  });
  return scope.scope_id;
}

async function recall(task) {
  const response = await pc("/v1/context/prepare", {
    scope_id: await stateScope(task), query: marker, max_bytes: 24000,
  });
  if (response.status === "empty") return null;
  const match = response.content?.match(/BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1\r?\n([\s\S]+?)\r?\nEND_POWERCONTEXT_PREPARED_CONTEXT_V1/);
  if (response.status !== "ready" || !match) throw new Error("PowerContext returned an invalid PreparedContext");
  return JSON.parse(match[1]).items
    .filter(item => !item.truncated && item.content?.startsWith(marker + " "))
    .map(item => ({
      ...JSON.parse(item.content.slice(marker.length + 1)),
      artifactId: item.citation.artifact_ref.artifact_id,
    }))
    .sort((left, right) => right.savedAt.localeCompare(left.savedAt))[0] ?? null;
}

async function save(task, record) {
  if (!/^[a-f0-9]{64}$/.test(record.contextId) || typeof record.nextAction !== "string" ||
      !["completed", "pending", "validation"].every(key => Array.isArray(record[key]) &&
        record[key].every(value => typeof value === "string"))) {
    throw new Error("Invalid checkpoint. Expected contextId, nextAction and completed/pending/validation arrays");
  }
  const state = {
    contextId: record.contextId, nextAction: record.nextAction,
    completed: record.completed, pending: record.pending, validation: record.validation,
    savedAt: new Date().toISOString(),
  };
  const text = marker + " " + JSON.stringify(state);
  if (text.length > 3000) throw new Error("Checkpoint exceeds 3000 characters");
  const receipt = await pc("/v1/memory/remember", {
    scope_id: await stateScope(task),
    kind: "sdd_checkpoint", reason: "Checkpoint from verified development workflow",
    description: `${marker} ${task.id}`, text,
  });
  const artifact = receipt.records?.[0]?.artifact;
  if (!artifact?.artifact_id) throw new Error("PowerContext write had no artifact receipt");
  return { engine: "PowerContext", artifact };
}

async function main() {
  if (!["import", "prepare", "save"].includes(command) || !taskFile || (command !== "prepare" && !input)) {
    throw new Error("Usage: node context.mjs <import|prepare|save> <task.json> [catalog.json|checkpoint.json]");
  }
  const task = await json(taskFile);
  if (!/^[a-z][a-z0-9-]{0,59}$/.test(task.id) || !Array.isArray(task.keywords) ||
      !task.keywords.length || task.keywords.length > 10 ||
      !task.keywords.every(word => typeof word === "string" && /^[a-z0-9-]{1,40}$/i.test(word))) {
    throw new Error("Task requires an id and one to ten simple keywords");
  }
  if (command === "save") return save(task, await json(input));

  await mkdir(data, { recursive: true });
  const runtime = await seekdb.open(data, { parameters: { memory_budget: "2G", mysql_port_mode: "random" } });
  let db;
  try {
    const endpoint = runtime.connectionOptions();
    if (endpoint.transport !== "tcp") throw new Error("Expected local seekdb TCP endpoint");
    db = await mysql.createConnection({ host: endpoint.host, port: endpoint.port, user: endpoint.user, database: "test" });
    await db.execute(`CREATE TABLE IF NOT EXISTS engineering_documents (
      project VARCHAR(60), id VARCHAR(100), kind VARCHAR(8), path VARCHAR(255), topics VARCHAR(500),
      active BOOLEAN, content TEXT, sha256 CHAR(64), PRIMARY KEY(project, id)
    )`);
    return command === "import" ? await importCatalog(db, task, await json(input)) : await prepare(db, task);
  } finally {
    try {
      await db?.end();
    } finally {
      await runtime.close();
    }
  }
}

try {
  console.log(JSON.stringify(await main(), null, 2));
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
