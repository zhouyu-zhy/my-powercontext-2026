import { AutoModel, BertTokenizer, env, mean_pooling } from "@huggingface/transformers";
import { MODEL } from "../shared/corpus.js";

env.allowRemoteModels = false;
env.allowLocalModels = true;
env.localModelPath = `${self.location.origin}/models/`;
env.backends.onnx.wasm.wasmPaths = `${self.location.origin}/ort/`;
env.backends.onnx.wasm.numThreads = 1;

const extractors = new Map();

async function loadTokenizer() {
  const base = `${env.localModelPath}${MODEL.id}/`;
  const [tokenizerResponse, configResponse] = await Promise.all([
    fetch(`${base}tokenizer.json`),
    fetch(`${base}tokenizer_config.json`),
  ]);
  if (!tokenizerResponse.ok || !configResponse.ok) {
    throw new Error("本地分词器文件加载失败");
  }
  const [tokenizerJSON, tokenizerConfig] = await Promise.all([
    tokenizerResponse.json(),
    configResponse.json(),
  ]);
  return new BertTokenizer(tokenizerJSON, tokenizerConfig);
}

async function createExtractor(precision, requestedBackend) {
  const attempts = requestedBackend === "auto"
    ? [navigator.gpu ? "webgpu" : "wasm", "wasm"]
    : [requestedBackend];
  const errors = [];
  for (const backend of [...new Set(attempts)]) {
    const key = `${precision}:${backend}`;
    if (extractors.has(key)) return { extractor: extractors.get(key), backend, cached: true, loadMs: 0, fallback: requestedBackend === "auto" && backend !== "webgpu" };
    const started = performance.now();
    try {
      // Transformers.js 4.3.2's pipeline registry skips browser-local tokenizer files.
      const [tokenizer, model] = await Promise.all([
        loadTokenizer(),
        AutoModel.from_pretrained(MODEL.id, {
          device: backend,
          dtype: precision === "int8" ? "q8" : "fp32",
          progress_callback: (event) => self.postMessage({ type: "model-progress", event }),
        }),
      ]);
      const extractor = { tokenizer, model };
      extractors.set(key, extractor);
      return { extractor, backend, cached: false, loadMs: performance.now() - started, fallback: requestedBackend === "auto" && backend !== "webgpu" };
    } catch (error) {
      errors.push(`${backend}: ${error.message}`);
      if (requestedBackend !== "auto") throw error;
    }
  }
  throw new Error(errors.join(" | "));
}

async function embed(texts, precision, backend) {
  const loaded = await createExtractor(precision, backend);
  const started = performance.now();
  const inputs = loaded.extractor.tokenizer(texts, { padding: true, truncation: true });
  const outputs = await loaded.extractor.model(inputs);
  const hiddenStates = outputs.last_hidden_state ?? outputs.logits ?? outputs.token_embeddings;
  const output = mean_pooling(hiddenStates, inputs.attention_mask).normalize(2, -1);
  return {
    embeddings: output.tolist(),
    embeddingMs: performance.now() - started,
    backend: loaded.backend,
    fallback: loaded.fallback,
    modelCached: loaded.cached,
    loadMs: loaded.loadMs,
  };
}

self.onmessage = async ({ data }) => {
  try {
    if (data.type === "init") {
      const result = await createExtractor(data.precision, data.backend);
      self.postMessage({ type: "ready", requestId: data.requestId, backend: result.backend, fallback: result.fallback, modelCached: result.cached, loadMs: result.loadMs });
      return;
    }
    if (data.type === "embed") {
      const result = await embed(data.texts, data.precision, data.backend);
      self.postMessage({ type: "embedded", requestId: data.requestId, ...result });
    }
  } catch (error) {
    self.postMessage({ type: "error", requestId: data.requestId, error: error.stack || error.message });
  }
};
