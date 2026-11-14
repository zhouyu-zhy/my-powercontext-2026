import { createReadStream } from "node:fs";
import { mkdir, stat } from "node:fs/promises";
import { createServer } from "node:http";
import { dirname, extname, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { SeekdbClient } from "seekdb";
import { seekdb } from "@oceanbase/seekdb-node";

const root = dirname(fileURLToPath(import.meta.url));
const production = process.env.NODE_ENV === "production";
const port = Number(process.env.PORT || 5173);
const dimension = 384;
const collectionPattern = /^[A-Za-z][A-Za-z0-9_]{2,31}$/;

await mkdir(resolve(root, "data"), { recursive: true });
const runtime = await seekdb.open(resolve(root, "data", "seekdb"), {
  parameters: { memory_budget: "2G", mysql_port_mode: "random" },
});
const endpoint = runtime.connectionOptions();
if (endpoint.transport !== "tcp") throw new Error(`需要 TCP transport，实际为 ${endpoint.transport}`);
const client = new SeekdbClient({
  host: endpoint.host,
  port: endpoint.port,
  user: endpoint.user,
  password: "",
  database: "test",
  queryTimeout: 60000,
});

let databaseQueue = Promise.resolve();
function serialize(operation) {
  const result = databaseQueue.then(operation, operation);
  databaseQueue = result.then(() => undefined, () => undefined);
  return result;
}

async function getCollection(name) {
  if (!collectionPattern.test(name)) {
    throw Object.assign(new Error("集合名须为 3–32 位字母、数字或下划线，且以字母开头"), { status: 400 });
  }
  return client.getOrCreateCollection({
    name,
    configuration: {
      hnsw: { dimension, distance: "cosine" },
      fulltextConfig: { analyzer: "ik", properties: { ik_mode: "smart" } },
    },
    embeddingFunction: null,
  });
}

async function listCollections() {
  const collections = await client.listCollections({ withEmbeddingFunction: false });
  return Promise.all(collections.map(async ({ name }) => {
    const collection = await getCollection(name);
    return { name, count: await collection.count() };
  }));
}

function validateEmbedding(value) {
  if (!Array.isArray(value) || value.length !== dimension || !value.every(Number.isFinite)) {
    throw Object.assign(new Error(`向量必须是 ${dimension} 个有限数值`), { status: 400 });
  }
  return value;
}

async function handleApi(request, response, url) {
  if (request.method === "GET" && url.pathname === "/api/health") {
    const rows = await client.execute("SELECT VERSION() AS version");
    return { ok: true, database: "seekdb", version: rows?.[0]?.version, embedding: "browser-only", dimension };
  }

  if (request.method === "GET" && url.pathname === "/api/collections") {
    return { collections: await serialize(listCollections) };
  }

  if (request.method === "POST" && url.pathname === "/api/collections") {
    const body = await readJson(request);
    const collection = await serialize(() => getCollection(body.name));
    return { name: collection.name, count: await serialize(() => collection.count()) };
  }

  const deleteMatch = url.pathname.match(/^\/api\/collections\/([A-Za-z0-9_]+)$/);
  if (request.method === "DELETE" && deleteMatch) {
    await serialize(() => client.deleteCollection(deleteMatch[1]));
    return { deleted: deleteMatch[1] };
  }

  const match = url.pathname.match(/^\/api\/collections\/([A-Za-z0-9_]+)\/(documents|search)$/);
  if (!match) throw Object.assign(new Error("API 不存在"), { status: 404 });
  const [, name, action] = match;

  if (request.method === "POST" && action === "documents") {
    const body = await readJson(request);
    if (!Array.isArray(body.records) || body.records.length === 0 || body.records.length > 100) {
      throw Object.assign(new Error("每批须包含 1–100 条文档"), { status: 400 });
    }
    const records = body.records.map((record) => {
      if (typeof record.id !== "string" || !/^[\w:.-]{1,96}$/.test(record.id)) {
        throw Object.assign(new Error("文档 ID 格式不正确"), { status: 400 });
      }
      if (typeof record.text !== "string" || !record.text.trim() || record.text.length > 20000) {
        throw Object.assign(new Error("文档正文须为 1–20000 字符"), { status: 400 });
      }
      return {
        id: record.id,
        text: record.text,
        embedding: validateEmbedding(record.embedding),
        metadata: record.metadata && typeof record.metadata === "object" ? record.metadata : {},
      };
    });
    return serialize(async () => {
      const collection = await getCollection(name);
      const writeStarted = performance.now();
      await collection.add({
        ids: records.map((record) => record.id),
        documents: records.map((record) => record.text),
        embeddings: records.map((record) => record.embedding),
        metadatas: records.map((record) => ({ ...record.metadata, source: String(record.metadata.source || "upload").slice(0, 128) })),
      });
      const writeMs = performance.now() - writeStarted;
      const refreshStarted = performance.now();
      await collection.refresh_index();
      const refreshMs = performance.now() - refreshStarted;
      const count = await collection.count();
      return {
        inserted: records.length,
        count,
        writeMs,
        refreshMs,
      };
    });
  }

  if (request.method === "POST" && action === "search") {
    const body = await readJson(request);
    if (typeof body.query !== "string" || !body.query.trim() || body.query.length > 500) {
      throw Object.assign(new Error("查询正文须为 1–500 字符"), { status: 400 });
    }
    if (typeof body.keyword !== "string" || !body.keyword.trim() || body.keyword.length > 100) {
      throw Object.assign(new Error("混合检索须提供一个全文关键词"), { status: 400 });
    }
    const embedding = validateEmbedding(body.embedding);
    const nResults = Math.max(1, Math.min(10, Number(body.nResults) || 5));
    return serialize(async () => {
      const collection = await getCollection(name);
      const started = performance.now();
      const results = await collection.hybridSearch({
        query: { whereDocument: { $contains: body.keyword.trim() }, nResults: 30 },
        knn: { queryEmbeddings: embedding, nResults: 30 },
        rank: { rrf: { rankWindowSize: 60, rankConstant: 60 } },
        nResults,
        include: ["documents", "metadatas"],
      });
      const databaseMs = performance.now() - started;
      return {
        hits: results.ids[0].map((id, index) => ({
          id,
          text: results.documents[0][index],
          metadata: results.metadatas[0][index],
          score: results.distances[0][index],
        })),
        databaseMs,
        engine: "seekdb DBMS_HYBRID_SEARCH · HNSW + FULLTEXT · RRF",
      };
    });
  }

  throw Object.assign(new Error("API 不存在"), { status: 404 });
}

function readJson(request) {
  return new Promise((resolveBody, reject) => {
    let size = 0;
    const chunks = [];
    request.on("data", (chunk) => {
      size += chunk.length;
      if (size > 20 * 1024 * 1024) {
        reject(Object.assign(new Error("请求体超过 20 MB"), { status: 413 }));
        request.destroy();
      } else {
        chunks.push(chunk);
      }
    });
    request.on("end", () => {
      try {
        resolveBody(JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}"));
      } catch {
        reject(Object.assign(new Error("JSON 格式不正确"), { status: 400 }));
      }
    });
    request.on("error", reject);
  });
}

function sendJson(response, status, value) {
  response.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
  response.end(JSON.stringify(value));
}

const mime = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".onnx": "application/octet-stream", ".wasm": "application/wasm", ".txt": "text/plain" };
function serveProduction(request, response) {
  const pathname = decodeURIComponent(new URL(request.url, "http://localhost").pathname);
  const base = resolve(root, "dist");
  const file = resolve(base, `.${pathname === "/" ? "/index.html" : pathname}`);
  if (file !== base && !file.startsWith(base + sep)) {
    response.writeHead(403).end();
    return;
  }
  stat(file).then((info) => {
    if (!info.isFile()) throw new Error("not file");
    response.writeHead(200, { "Content-Type": `${mime[extname(file)] || "application/octet-stream"}; charset=utf-8`, "Content-Length": info.size });
    createReadStream(file).pipe(response);
  }).catch(() => response.writeHead(404).end("Not found"));
}

const vite = production ? null : await (await import("vite")).createServer({
  root,
  server: { middlewareMode: true, watch: { ignored: ["**/data/**", "**/public/models/**", "**/public/ort/**", "**/evidence/**"] } },
  appType: "spa",
});
const server = createServer(async (request, response) => {
  const url = new URL(request.url, `http://${request.headers.host || "localhost"}`);
  if (url.pathname.startsWith("/api/")) {
    try {
      sendJson(response, 200, await handleApi(request, response, url));
    } catch (error) {
      sendJson(response, error.status || 500, { error: error.message });
    }
    return;
  }
  if (production) {
    serveProduction(request, response);
  } else {
    vite.middlewares(request, response, () => response.writeHead(404).end("Not found"));
  }
});

server.listen(port, "127.0.0.1", () => {
  console.log(`Local Light running at http://127.0.0.1:${port}`);
});

async function shutdown() {
  server.close();
  await vite?.close();
  await serialize(() => client.close());
  runtime.close();
  process.exit(0);
}
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
