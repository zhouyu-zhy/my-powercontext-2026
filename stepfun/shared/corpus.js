export const MODEL = {
  id: "Xenova/all-MiniLM-L6-v2",
  revision: "751bff37182d3f1213fa05d7196b954e230abad9",
  dimension: 384,
};

export const DOCUMENTS = [
  ["d01", "Hybrid search combines full text keyword matching with dense vector similarity so a knowledge base can retrieve exact terms and related meaning.", "retrieval"],
  ["d02", "Reciprocal rank fusion in hybrid retrieval merges the full text ranking and vector ranking without requiring comparable raw scores.", "retrieval"],
  ["d03", "An HNSW vector index approximates nearest neighbors and reduces the work needed to search a large collection of embeddings.", "retrieval"],
  ["d04", "A full text index stores tokenized terms and supports exact keyword matching over the original document.", "retrieval"],
  ["d05", "WebGPU accelerates transformer inference with the computer GPU while the model stays on the local device.", "inference"],
  ["d06", "A WASM fallback keeps browser inference available when WebGPU is missing or model initialization on the GPU fails.", "inference"],
  ["d07", "Quantization converts FP32 model weights into smaller INT8 representations to reduce download size memory use and inference cost.", "inference"],
  ["d08", "ONNX Runtime Web executes quantized transformer models through WebGPU or WASM and keeps quantization inference on the device.", "inference"],
  ["d09", "Browser local embedding keeps private notes on the user device because neither documents nor queries are sent to a cloud inference API.", "privacy"],
  ["d10", "A Web Worker moves embedding inference away from the interface thread so the application remains responsive during local computation.", "performance"],
  ["d11", "Model caching avoids repeated embedding model downloads and initialization when a user runs another search.", "performance"],
  ["d12", "Batching documents improves embedding throughput by amortizing tokenizer and inference overhead across several texts.", "performance"],
  ["d13", "seekdb stores embeddings original documents stable identifiers and JSON metadata together in a persistent collection.", "seekdb"],
  ["d14", "The seekdb asynchronous index pipeline refreshes vector indexes after writes so newly imported records become searchable.", "seekdb"],
  ["d15", "seekdb collections separate knowledge bases and preserve independent state when a user switches between projects.", "seekdb"],
  ["d16", "Incremental imports add new seekdb records without replacing the documents already stored in the selected collection.", "seekdb"],
  ["d17", "Recall at K measures retrieval quality as the fraction of relevant documents that appear in the first K ranked results.", "metrics"],
  ["d18", "Normalized discounted cumulative gain evaluates retrieval ranking by giving graded relevant documents more credit near the top.", "metrics"],
  ["d19", "Cold start includes model loading and graph compilation while warm timing reuses an initialized runtime.", "metrics"],
  ["d20", "Median latency summarizes repeated measurements more robustly than selecting the fastest run or relying on one observation.", "metrics"],
  ["d21", "A sourdough starter needs flour water and regular feeding before it can leaven a loaf of bread.", "life"],
  ["d22", "A city cycling route with protected lanes and secure parking makes a daily commute more convenient.", "life"],
  ["d23", "A weekly review can collect unfinished tasks choose priorities and reserve time for focused work.", "life"],
  ["d24", "Chinese tea can be brewed with cooler water and shorter infusions to adjust its aroma and bitterness.", "life"],
].map(([id, text, category]) => ({ id, text, metadata: { category, source: "reproducible-corpus-v1" } }));

export const QUERIES = [
  { id: "q1", query: "How does hybrid search combine keywords and vectors?", keyword: "hybrid", relevant: ["d01", "d02"] },
  { id: "q2", query: "Why quantize a model to INT8?", keyword: "quantization", relevant: ["d07", "d08"] },
  { id: "q3", query: "How does the app work without WebGPU?", keyword: "WASM", relevant: ["d06", "d08"] },
  { id: "q4", query: "What does seekdb persist?", keyword: "seekdb", relevant: ["d13", "d14", "d15", "d16"] },
  { id: "q5", query: "How is retrieval quality measured?", keyword: "retrieval", relevant: ["d17", "d18"] },
  { id: "q6", query: "How can embedding latency be reduced?", keyword: "embedding", relevant: ["d10", "d11", "d12"] },
];

export const DEFAULT_QUERY = QUERIES[0];
