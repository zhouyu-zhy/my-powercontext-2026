import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { SeekdbClient } from "seekdb";
import { seekdb } from "@oceanbase/seekdb-node";

console.log("Opening local seekdb server");
const runtime = await seekdb.open(resolve("data", "managed-db-smoke"), { parameters: { memory_budget: "2G", mysql_port_mode: "random" } });
const endpoint = runtime.connectionOptions();
if (endpoint.transport !== "tcp") throw new Error(`Expected TCP, received ${endpoint.transport}`);
const client = new SeekdbClient({ host: endpoint.host, port: endpoint.port, user: endpoint.user, password: "", database: "test", queryTimeout: 60000 });
console.log("Local seekdb server opened");
const name = `smoke_${Date.now()}`;
const vector = (value) => Array.from({ length: 384 }, (_, index) => index === 0 ? value : 0);
try {
  const collection = await client.getOrCreateCollection({
    name,
    configuration: { hnsw: { dimension: 384, distance: "cosine" }, fulltextConfig: { analyzer: "ik" } },
    embeddingFunction: null,
  });
  console.log("Collection ready");
  const writeStarted = performance.now();
  await collection.add({
    ids: ["keyword", "semantic", "other"],
    documents: ["needle hybrid retrieval original text", "related vector document without the term", "unrelated cooking note"],
    embeddings: [vector(0.8), vector(1), vector(-1)],
    metadatas: [{ order: 1 }, { order: 2 }, { order: 3 }],
  });
  const writeMs = performance.now() - writeStarted;
  console.log("Documents written");
  const refreshStarted = performance.now();
  await collection.refresh_index();
  const refreshMs = performance.now() - refreshStarted;
  console.log("Index refreshed");
  const searchStarted = performance.now();
  const result = await collection.hybridSearch({
    query: { whereDocument: { $contains: "needle" }, nResults: 10 },
    knn: { queryEmbeddings: vector(1), nResults: 10 },
    rank: { rrf: { rankWindowSize: 60, rankConstant: 60 } },
    nResults: 3,
    include: ["documents", "metadatas"],
  });
  const databaseMs = performance.now() - searchStarted;
  const ids = result.ids[0];
  if (!ids.includes("keyword") || !ids.includes("semantic")) throw new Error(`Hybrid result incomplete: ${ids}`);
  if (!result.documents[0][ids.indexOf("keyword")]?.includes("original text")) throw new Error("Original document missing");
  await collection.add({ ids: ["incremental"], documents: ["needle incremental document"], embeddings: [vector(0.9)], metadatas: [{ order: 4 }] });
  await collection.refresh_index();
  if (await collection.count() !== 4) throw new Error("Incremental write failed");
  const evidence = {
    result: "PASS",
    testedAt: new Date().toISOString(),
    sdk: "seekdb@1.4.0",
    mode: "local-server-tcp",
    checks: { databaseHybridReturnsBothBranches: true, originalTextReturned: true, indexRefreshAndIncrementalWrite: true },
    initialIds: ids,
    writeMs,
    refreshMs,
    databaseMs,
  };
  await mkdir("evidence", { recursive: true });
  await writeFile("evidence/db-smoke.json", `${JSON.stringify(evidence, null, 2)}\n`);
  console.log(JSON.stringify(evidence, null, 2));
} finally {
  await client.deleteCollection(name).catch(() => undefined);
  await client.close();
  runtime.close();
}
