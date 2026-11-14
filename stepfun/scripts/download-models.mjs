import { createWriteStream } from "node:fs";
import { mkdir, readFile, rename, stat } from "node:fs/promises";
import { createHash } from "node:crypto";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import { writeFile } from "node:fs/promises";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const model = "Xenova/all-MiniLM-L6-v2";
const revision = "751bff37182d3f1213fa05d7196b954e230abad9";
const endpoint = process.env.HF_ENDPOINT || "https://hf-mirror.com";
const target = resolve(root, "public", "models", model);
const shared = ["config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "vocab.txt"];
const variants = { fp32: "onnx/model.onnx", int8: "onnx/model_quantized.onnx" };

async function download(path) {
  const destination = resolve(target, path);
  await mkdir(dirname(destination), { recursive: true });
  const url = `${endpoint}/${model}/resolve/${revision}/${path}`;
  const started = performance.now();
  const response = await fetch(url, { signal: AbortSignal.timeout(180000), redirect: "follow" });
  if (!response.ok || !response.body) throw new Error(`${response.status} ${url}`);
  const temporary = `${destination}.part`;
  await pipeline(Readable.fromWeb(response.body), createWriteStream(temporary));
  await rename(temporary, destination);
  const info = await stat(destination);
  const sha256 = createHash("sha256").update(await readFile(destination)).digest("hex");
  return { path, bytes: info.size, downloadMs: performance.now() - started, sha256 };
}

const downloads = [];
for (const path of [...shared, ...Object.values(variants)]) {
  const result = await download(path);
  downloads.push(result);
  console.log(`${path}: ${result.bytes} bytes`);
}
const byPath = Object.fromEntries(downloads.map((item) => [item.path, item]));
const sharedBytes = shared.reduce((sum, path) => sum + byPath[path].bytes, 0);
const manifest = {
  model,
  revision,
  source: `https://huggingface.co/${model}/tree/${revision}`,
  downloadEndpoint: endpoint,
  generatedAt: new Date().toISOString(),
  accounting: "每个精度总大小 = 相同共享配置与分词文件 + 对应 ONNX 权重；不重复计算共享文件",
  sharedBytes,
  variants: Object.fromEntries(Object.entries(variants).map(([precision, path]) => [precision, {
    onnxFile: path,
    onnxBytes: byPath[path].bytes,
    totalBytes: sharedBytes + byPath[path].bytes,
  }])),
  downloads,
};
manifest.compressionRatio = manifest.variants.fp32.totalBytes / manifest.variants.int8.totalBytes;
await mkdir(resolve(root, "evidence"), { recursive: true });
await writeFile(resolve(root, "public", "models", "manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`);
await writeFile(resolve(root, "evidence", "model-files.json"), `${JSON.stringify(manifest, null, 2)}\n`);
console.log(`FP32 / INT8 = ${manifest.compressionRatio.toFixed(3)}x`);
