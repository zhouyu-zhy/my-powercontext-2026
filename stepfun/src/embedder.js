export class Embedder {
  constructor() {
    this.worker = new Worker(new URL("./embedder.worker.js", import.meta.url), { type: "module" });
    this.pending = new Map();
    this.serial = 0;
    this.queue = Promise.resolve();
    this.worker.onmessage = ({ data }) => {
      if (!data.requestId) return;
      const pending = this.pending.get(data.requestId);
      if (!pending) return;
      this.pending.delete(data.requestId);
      if (data.type === "error") pending.reject(new Error(data.error));
      else pending.resolve(data);
    };
    this.worker.onerror = (event) => {
      for (const pending of this.pending.values()) pending.reject(new Error(event.message));
      this.pending.clear();
    };
  }

  request(type, payload) {
    const operation = this.queue.then(() => new Promise((resolve, reject) => {
      const requestId = ++this.serial;
      this.pending.set(requestId, { resolve, reject });
      this.worker.postMessage({ type, requestId, ...payload });
    }));
    this.queue = operation.catch(() => undefined);
    return operation;
  }

  init(precision, backend) {
    return this.request("init", { precision, backend });
  }

  embed(texts, precision, backend) {
    return this.request("embed", { texts, precision, backend });
  }

  close() {
    this.worker.terminate();
  }
}

export async function embedInBatches(embedder, documents, precision, backend, batchSize, onBatch) {
  const embeddings = [];
  let inferenceMs = 0;
  for (let offset = 0; offset < documents.length; offset += batchSize) {
    const batch = documents.slice(offset, offset + batchSize);
    const result = await embedder.embed(batch.map((document) => document.text), precision, backend);
    embeddings.push(...result.embeddings);
    inferenceMs += result.embeddingMs;
    onBatch?.(result, Math.min(offset + batchSize, documents.length), documents.length);
  }
  return { embeddings, inferenceMs };
}
