import { copyFile, mkdir, readdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const source = resolve(root, "node_modules", "onnxruntime-web", "dist");
const target = resolve(root, "public", "ort");
await mkdir(target, { recursive: true });
const files = (await readdir(source)).filter((name) => /^ort-wasm.*\.(?:wasm|mjs|js)$/.test(name));
await Promise.all(files.map((name) => copyFile(resolve(source, name), resolve(target, name))));
console.log(`Prepared ${files.length} local ONNX Runtime Web assets`);
