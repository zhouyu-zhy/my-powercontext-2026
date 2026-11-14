import "./style.css";
import { api } from "./api.js";
import { Embedder, embedInBatches } from "./embedder.js";
import { DEFAULT_QUERY, DOCUMENTS, QUERIES } from "../shared/corpus.js";
import { runMeasuredExperiment } from "./experiment.js";

const $ = (selector) => document.querySelector(selector);
const state = { collection: "personal_kb", backend: null, fallback: false, ready: false, count: 0, lastEvidence: null };
let embedder;
let toastTimer;

function toast(message) {
  $("#toast").textContent = message;
  $("#toast").classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("#toast").classList.remove("show"), 4000);
}

function getEmbedder() {
  if (!embedder) embedder = new Embedder();
  return embedder;
}

function options() {
  return { precision: $("#precision").value, backend: $("#backend").value, batchSize: Number($("#batch-size").value) };
}

async function initializeModel() {
  const { precision, backend } = options();
  $("#model-state").textContent = "加载中…";
  $("#load-model").disabled = true;
  try {
    const result = await getEmbedder().init(precision, backend);
    state.backend = result.backend;
    state.fallback = result.fallback;
    state.ready = true;
    $("#backend-badge").textContent = `${result.backend.toUpperCase()} / ${precision.toUpperCase()}${result.fallback ? " · 已降级" : ""}`;
    $("#model-state").textContent = result.modelCached ? "缓存命中" : "已就绪";
    return result;
  } catch (error) {
    state.ready = false;
    $("#model-state").textContent = "加载失败";
    toast(`模型初始化失败：${error.message}`);
    throw error;
  } finally {
    $("#load-model").disabled = false;
  }
}

async function refreshCollections(preferred) {
  const { collections } = await api.collections();
  if (!collections.some((collection) => collection.name === "personal_kb")) await api.createCollection("personal_kb");
  const updated = !collections.some((collection) => collection.name === "personal_kb") ? (await api.collections()).collections : collections;
  const select = $("#collection");
  select.replaceChildren(...updated.map((collection) => new Option(`${collection.name} · ${collection.count}`, collection.name)));
  state.collection = updated.some((collection) => collection.name === preferred) ? preferred : updated[0]?.name || "personal_kb";
  select.value = state.collection;
  state.count = updated.find((collection) => collection.name === state.collection)?.count || 0;
  updateCollection();
}

function updateCollection() {
  $("#active-collection").textContent = state.collection;
  $("#doc-count").textContent = `${state.count} 篇`;
}

async function importDocuments(documents) {
  if (!documents.length || documents.length > 100) throw new Error("每次导入须为 1–100 篇文档");
  const { precision, backend, batchSize } = options();
  $("#import-progress").hidden = false;
  $("#import-status").textContent = "浏览器正在生成向量…";
  const started = performance.now();
  const embedded = await embedInBatches(getEmbedder(), documents, precision, backend, batchSize, (result, done, total) => {
    state.backend = result.backend;
    $("#backend-badge").textContent = `${result.backend.toUpperCase()} / ${precision.toUpperCase()}${result.fallback ? " · 已降级" : ""}`;
    $("#import-status").textContent = `本地向量化 ${done}/${total} · ${result.embeddingMs.toFixed(1)} ms/批`;
  });
  const response = await api.addDocuments(state.collection, documents.map((document, index) => ({
    id: document.id,
    text: document.text,
    metadata: document.metadata || {},
    embedding: embedded.embeddings[index],
  })));
  state.count = response.count;
  updateCollection();
  await refreshCollections(state.collection);
  $("#import-status").textContent = `已写入 ${response.inserted} 篇 · 写入 ${response.writeMs.toFixed(1)} ms · 索引刷新 ${response.refreshMs.toFixed(1)} ms · 总计 ${(performance.now() - started).toFixed(0)} ms`;
  $("#import-progress").hidden = true;
  return response;
}

function renderResults(response, elapsed, queryEmbeddingMs) {
  $("#result-count").textContent = `${response.hits.length} 条`;
  $("#query-ms").textContent = `${queryEmbeddingMs.toFixed(1)} ms`;
  $("#db-ms").textContent = `${response.databaseMs.toFixed(1)} ms`;
  $("#total-ms").textContent = `${elapsed.toFixed(1)} ms`;
  if (!response.hits.length) {
    $("#results").innerHTML = '<div class="empty-state"><h3>没有同时命中的结果</h3><p>尝试更短的全文关键词，或先向当前集合导入相关文档。</p></div>';
    return;
  }
  $("#results").replaceChildren(...response.hits.map((hit, index) => {
    const article = document.createElement("article");
    article.className = "result-card";
    const top = document.createElement("div");
    top.className = "result-top";
    const rank = document.createElement("span"); rank.className = "rank"; rank.textContent = String(index + 1).padStart(2, "0");
    const title = document.createElement("h4"); title.textContent = hit.metadata?.source || "本地文档";
    const score = document.createElement("span"); score.className = "score"; score.textContent = `RRF ${Number(hit.score).toFixed(5)}`;
    top.append(rank, title, score);
    const text = document.createElement("p"); text.className = "result-text"; text.textContent = hit.text;
    const meta = document.createElement("div"); meta.className = "result-meta";
    const id = document.createElement("code"); id.textContent = hit.id;
    const category = document.createElement("span"); category.textContent = `${hit.metadata?.category || "uncategorized"} · 原文未截断`;
    meta.append(id, category);
    article.append(top, text, meta);
    return article;
  }));
}

async function search() {
  const query = $("#query").value.trim();
  const keyword = $("#keyword").value.trim();
  if (!query || !keyword) throw new Error("请同时填写语义查询和全文关键词");
  $("#search").disabled = true;
  const started = performance.now();
  try {
    const { precision, backend } = options();
    const embedded = await getEmbedder().embed([query], precision, backend);
    state.backend = embedded.backend;
    $("#backend-badge").textContent = `${embedded.backend.toUpperCase()} / ${precision.toUpperCase()}${embedded.fallback ? " · 已降级" : ""}`;
    const response = await api.search(state.collection, { query, keyword, embedding: embedded.embeddings[0], nResults: 5 });
    renderResults(response, performance.now() - started, embedded.embeddingMs);
    return response;
  } finally {
    $("#search").disabled = false;
  }
}

async function parseFiles(files) {
  const documents = [];
  for (const file of files) {
    const text = await file.text();
    if (file.name.endsWith(".json")) {
      const value = JSON.parse(text);
      const rows = Array.isArray(value) ? value : value.documents;
      if (!Array.isArray(rows)) throw new Error(`${file.name} 须为数组或包含 documents 数组`);
      rows.forEach((row, index) => documents.push(typeof row === "string"
        ? { id: `${crypto.randomUUID()}-${index}`, text: row, metadata: { source: file.name } }
        : { id: String(row.id || crypto.randomUUID()), text: String(row.text || ""), metadata: { ...row.metadata, source: file.name } }));
    } else {
      text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).forEach((line) => documents.push({ id: crypto.randomUUID(), text: line, metadata: { source: file.name } }));
    }
  }
  return documents;
}

async function handleFiles(files) {
  try {
    const documents = await parseFiles(files);
    await importDocuments(documents);
  } catch (error) {
    $("#import-progress").hidden = true;
    $("#import-status").textContent = error.message;
    toast(error.message);
  }
}

function renderExperiment(evidence) {
  const [int8, fp32] = evidence.variants;
  $("#experiment-content").innerHTML = `
    <div class="experiment-summary">
      <div><strong>${evidence.comparison.compressionRatio.toFixed(3)}×</strong><span>同口径模型压缩比</span></div>
      <div><strong>${(int8.recallAt5 * 100).toFixed(1)}%</strong><span>INT8 Recall@5（FP32 ${(fp32.recallAt5 * 100).toFixed(1)}%）</span></div>
      <div><strong>${evidence.comparison.batch8SpeedupInt8.toFixed(2)}×</strong><span>INT8 batch=8 相对 batch=1 加速</span></div>
    </div>
    <table><thead><tr><th>精度</th><th>冷加载</th><th>文档向量化 batch=8</th><th>查询向量</th><th>seekdb</th><th>nDCG@5</th></tr></thead><tbody>
    ${evidence.variants.map((variant) => `<tr><td>${variant.precision.toUpperCase()}</td><td>${variant.coldModelLoadMs.toFixed(1)} ms</td><td>${variant.batching[8].medianWallMs.toFixed(1)} ms</td><td>${variant.medianQueryEmbeddingMs.toFixed(1)} ms</td><td>${variant.medianDatabaseMs.toFixed(1)} ms</td><td>${variant.nDCGAt5.toFixed(4)}</td></tr>`).join("")}
    </tbody></table><p>固定 WASM 后端，24 篇文档、6 个查询，每轮重复 3 次，报告 Wall time 中位数。完整逐轮结果已保存在可下载 JSON 中。</p>`;
}

async function runExperiment() {
  $("#experiment-dialog").showModal();
  $("#start-experiment").disabled = true;
  try {
    const evidence = await runMeasuredExperiment((message) => { $("#experiment-content").textContent = message; });
    state.lastEvidence = evidence;
    window.lastEvidence = evidence;
    renderExperiment(evidence);
    $("#download-evidence").disabled = false;
    return evidence;
  } catch (error) {
    $("#experiment-content").textContent = `实验失败：${error.message}`;
    throw error;
  } finally {
    $("#start-experiment").disabled = false;
  }
}

$("#load-model").addEventListener("click", () => initializeModel().catch(() => undefined));
$("#import-sample").addEventListener("click", () => importDocuments(DOCUMENTS).catch((error) => { $("#import-progress").hidden = true; $("#import-status").textContent = error.message; toast(error.message); }));
$("#search").addEventListener("click", () => search().catch((error) => toast(error.message)));
$("#query").addEventListener("keydown", (event) => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter") search().catch((error) => toast(error.message)); });
$("#collection").addEventListener("change", async (event) => {
  state.collection = event.target.value;
  const { collections } = await api.collections();
  state.count = collections.find((collection) => collection.name === state.collection)?.count || 0;
  updateCollection();
});
$("#create-collection").addEventListener("click", async () => {
  try {
    const name = $("#new-collection").value.trim();
    await api.createCollection(name);
    $("#new-collection").value = "";
    await refreshCollections(name);
    toast(`已切换到空集合 ${name}`);
  } catch (error) { toast(error.message); }
});
$("#file-input").addEventListener("change", (event) => handleFiles([...event.target.files]));
$("#dropzone").addEventListener("dragover", (event) => event.preventDefault());
$("#dropzone").addEventListener("drop", (event) => { event.preventDefault(); handleFiles([...event.dataTransfer.files]); });
$("#run-experiment").addEventListener("click", () => { $("#experiment-dialog").showModal(); });
$("#start-experiment").addEventListener("click", () => runExperiment().catch(() => undefined));
$("#close-experiment").addEventListener("click", () => $("#experiment-dialog").close());
$("#download-evidence").addEventListener("click", () => {
  const url = URL.createObjectURL(new Blob([JSON.stringify(state.lastEvidence, null, 2)], { type: "application/json" }));
  const anchor = document.createElement("a"); anchor.href = url; anchor.download = "local-light-evidence.json"; anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
for (const item of QUERIES) {
  const button = document.createElement("button");
  button.textContent = item.query;
  button.addEventListener("click", () => { $("#query").value = item.query; $("#keyword").value = item.keyword; });
  $("#query-presets").append(button);
}

const forcedBackend = new URLSearchParams(location.search).get("backend");
if (["auto", "wasm", "webgpu"].includes(forcedBackend)) $("#backend").value = forcedBackend;

window.kbApp = { state, initializeModel, importDocuments, search, runExperiment, refreshCollections, DOCUMENTS, DEFAULT_QUERY };

try {
  const health = await api.health();
  $("#db-badge").innerHTML = `<i class="status-dot"></i>seekdb ${health.version || "已连接"}`;
  await refreshCollections("personal_kb");
  await initializeModel();
} catch (error) {
  toast(error.message);
}
