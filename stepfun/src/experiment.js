import { api } from "./api.js";
import { Embedder, embedInBatches } from "./embedder.js";
import { DOCUMENTS, MODEL, QUERIES } from "../shared/corpus.js";

const median = (values) => {
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
};

function quality(ids, relevant, k = 5) {
  const top = ids.slice(0, k);
  const hits = top.filter((id) => relevant.includes(id)).length;
  const dcg = top.reduce((sum, id, index) => sum + (relevant.includes(id) ? 1 / Math.log2(index + 2) : 0), 0);
  const ideal = Array.from({ length: Math.min(k, relevant.length) }, (_, index) => 1 / Math.log2(index + 2));
  return { recallAt5: hits / relevant.length, nDCGAt5: dcg / ideal.reduce((sum, value) => sum + value, 0) };
}

async function measureVariant(precision, modelFiles, onProgress) {
  const embedder = new Embedder();
  const collection = `bench_${precision}_${Date.now()}`;
  try {
    onProgress(`加载 ${precision.toUpperCase()} 模型（WASM 固定后端）`);
    const cold = await embedder.init(precision, "wasm");
    const warmStarted = performance.now();
    const warm = await embedder.init(precision, "wasm");
    const warmWallMs = performance.now() - warmStarted;
    const batching = {};
    let embeddings;
    for (const batchSize of [1, 8]) {
      const runs = [];
      for (let repeat = 0; repeat < 3; repeat++) {
        onProgress(`${precision.toUpperCase()} · batch=${batchSize} · 第 ${repeat + 1}/3 轮`);
        const started = performance.now();
        const embedded = await embedInBatches(embedder, DOCUMENTS, precision, "wasm", batchSize);
        const wallMs = performance.now() - started;
        runs.push({ wallMs, inferenceMs: embedded.inferenceMs });
        if (batchSize === 8 && repeat === 0) embeddings = embedded.embeddings;
      }
      batching[batchSize] = { runs, medianWallMs: median(runs.map((run) => run.wallMs)), medianInferenceMs: median(runs.map((run) => run.inferenceMs)) };
    }

    await api.createCollection(collection);
    const write = await api.addDocuments(collection, DOCUMENTS.map((document, index) => ({ ...document, embedding: embeddings[index] })));
    const queryRuns = [];
    const qualityRuns = [];
    for (const item of QUERIES) {
      for (let repeat = 0; repeat < 3; repeat++) {
        onProgress(`${precision.toUpperCase()} · ${item.id} · 查询 ${repeat + 1}/3`);
        const started = performance.now();
        const embedded = await embedder.embed([item.query], precision, "wasm");
        const response = await api.search(collection, { query: item.query, keyword: item.keyword, embedding: embedded.embeddings[0], nResults: 5 });
        const totalMs = performance.now() - started;
        const ids = response.hits.map((hit) => hit.id);
        const scores = quality(ids, item.relevant);
        queryRuns.push({ queryId: item.id, repeat, ids, queryEmbeddingMs: embedded.embeddingMs, databaseMs: response.databaseMs, totalMs, ...scores });
        if (repeat === 0) qualityRuns.push({ queryId: item.id, ...scores });
      }
    }
    return {
      precision,
      collection,
      backend: cold.backend,
      modelBytes: modelFiles.variants[precision].totalBytes,
      coldModelLoadMs: cold.loadMs,
      warmModelCacheHitMs: warmWallMs,
      warmModelReportedLoadMs: warm.loadMs,
      writeMs: write.writeMs,
      refreshMs: write.refreshMs,
      inserted: write.inserted,
      batching,
      medianQueryEmbeddingMs: median(queryRuns.map((run) => run.queryEmbeddingMs)),
      medianDatabaseMs: median(queryRuns.map((run) => run.databaseMs)),
      medianEndToEndMs: median(queryRuns.map((run) => run.totalMs)),
      recallAt5: qualityRuns.reduce((sum, run) => sum + run.recallAt5, 0) / qualityRuns.length,
      nDCGAt5: qualityRuns.reduce((sum, run) => sum + run.nDCGAt5, 0) / qualityRuns.length,
      qualityRuns,
      queryRuns,
    };
  } finally {
    embedder.close();
    await api.deleteCollection(collection).catch(() => undefined);
  }
}

export async function runMeasuredExperiment(onProgress = () => {}) {
  const modelFiles = await (await fetch("/models/manifest.json")).json();
  const variants = [];
  for (const precision of ["int8", "fp32"]) variants.push(await measureVariant(precision, modelFiles, onProgress));
  const [int8, fp32] = variants;
  const checks = {
    allDocumentsInserted: variants.every((variant) => variant.inserted === DOCUMENTS.length),
    allQueriesExecuted: variants.every((variant) => variant.queryRuns.length === QUERIES.length * 3),
    finiteMetrics: variants.every((variant) => [variant.recallAt5, variant.nDCGAt5, variant.medianQueryEmbeddingMs, variant.medianDatabaseMs, variant.medianEndToEndMs].every(Number.isFinite)),
  };
  const evidence = {
    result: Object.values(checks).every(Boolean) ? "PASS" : "FAIL",
    checks,
    testedAt: new Date().toISOString(),
    environment: {
      userAgent: navigator.userAgent,
      hardwareConcurrency: navigator.hardwareConcurrency,
      deviceMemory: navigator.deviceMemory || null,
      platform: navigator.platform,
    },
    fixedConfiguration: {
      corpus: "reproducible-corpus-v1 · 24 documents",
      queries: "6 queries · 3 repetitions each",
      model: MODEL.id,
      revision: MODEL.revision,
      backend: "wasm (quantization comparison)",
      batchComparison: [1, 8],
      batchRepetitions: 3,
      vectorDimension: MODEL.dimension,
      distance: "cosine",
      seekdb: "1.4.0 local server · HNSW + FULLTEXT · RRF(rank_constant=60, rank_window_size=60)",
      k: 5,
    },
    modelFiles,
    variants,
    comparison: {
      compressionRatio: modelFiles.compressionRatio,
      recallAt5Delta: int8.recallAt5 - fp32.recallAt5,
      nDCGAt5Delta: int8.nDCGAt5 - fp32.nDCGAt5,
      int8EmbeddingSpeedup: fp32.batching[8].medianWallMs / int8.batching[8].medianWallMs,
      batch8SpeedupInt8: int8.batching[1].medianWallMs / int8.batching[8].medianWallMs,
      batch8SpeedupFp32: fp32.batching[1].medianWallMs / fp32.batching[8].medianWallMs,
    },
  };
  return evidence;
}
