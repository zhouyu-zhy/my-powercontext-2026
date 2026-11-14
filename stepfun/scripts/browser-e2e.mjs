import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const evidenceDir = resolve(root, "evidence");
const port = Number(process.env.TEST_PORT || 5187);
const baseURL = process.env.BASE_URL || `http://127.0.0.1:${port}`;
const suffix = Date.now().toString(36);
const names = {
  main: `e2e_main_${suffix}`,
  empty: `e2e_empty_${suffix}`,
  fallback: `e2e_fallback_${suffix}`,
  webgpu: `e2e_webgpu_${suffix}`,
};
const createdCollections = new Set();
let serverProcess;
let serverOutput = "";
let browser;

async function launchChrome(args) {
  const systemChrome = "C:/Program Files/Google/Chrome/Application/chrome.exe";
  const executablePath = process.env.BROWSER_EXECUTABLE || (existsSync(systemChrome) ? systemChrome : undefined);
  return chromium.launch({ executablePath, headless: process.env.HEADED !== "1", args });
}

async function waitForServer() {
  const deadline = Date.now() + 120000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${baseURL}/api/health`);
      if (response.ok) {
        const health = await response.json();
        assert.equal(health.database, "seekdb");
        assert.equal(health.embedding, "browser-only");
        return health;
      }
    } catch {}
    if (serverProcess?.exitCode !== null && serverProcess?.exitCode !== undefined) {
      throw new Error(`应用服务退出：${serverOutput}`);
    }
    await new Promise((resolveWait) => setTimeout(resolveWait, 300));
  }
  throw new Error(`等待应用服务超时：${serverOutput}`);
}

async function createCollection(page, name) {
  await page.locator("#new-collection").fill(name);
  await page.locator("#create-collection").click();
  await page.waitForFunction((expected) => window.kbApp.state.collection === expected, name);
  createdCollections.add(name);
}

async function openReadyPage(path, initScript, targetBrowser = browser) {
  const page = await targetBrowser.newPage();
  if (initScript) await page.addInitScript(initScript);
  await page.goto(`${baseURL}/${path}`, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => window.kbApp?.state.ready === true, null, { timeout: 120000 });
  return page;
}

async function search(page, query, keyword) {
  await page.locator("#query").fill(query);
  await page.locator("#keyword").fill(keyword);
  await page.locator("#search").click();
  await page.waitForFunction(() => !document.querySelector("#search").disabled, null, { timeout: 120000 });
}

async function readResults(page) {
  return page.locator("#results .result-card").evaluateAll((cards) => cards.map((card) => ({
    id: card.querySelector("code")?.textContent,
    text: card.querySelector(".result-text")?.textContent,
    score: card.querySelector(".score")?.textContent,
  })));
}

async function probeWebGPU() {
  const page = await browser.newPage();
  await page.goto(`${baseURL}/?backend=webgpu`, { waitUntil: "domcontentloaded" });
  try {
    try {
      await page.waitForFunction(() => window.kbApp?.state.ready === true || document.querySelector("#model-state")?.textContent === "加载失败", null, { timeout: 120000 });
    } catch (error) {
      return { status: "unavailable", reason: error.message };
    }
    const ready = await page.evaluate(() => window.kbApp.state.ready);
    if (!ready) {
      return { status: "unavailable", reason: await page.locator("#toast").textContent() };
    }
    await createCollection(page, names.webgpu);
    await page.evaluate(() => window.kbApp.importDocuments([{ id: "webgpu_probe", text: "WebGPU executes local transformer inference on the device.", metadata: { source: "browser-e2e" } }]));
    await search(page, "local transformer inference", "WebGPU");
    const hits = await readResults(page);
    assert.equal(await page.evaluate(() => window.kbApp.state.backend), "webgpu");
    assert.ok(hits.some((hit) => hit.id === "webgpu_probe"));
    return { status: "PASS", backend: "webgpu", hits };
  } finally {
    await page.close();
  }
}

try {
  await mkdir(evidenceDir, { recursive: true });
  if (!process.env.BASE_URL) {
    serverProcess = spawn(process.execPath, ["server.mjs"], {
      cwd: root,
      env: { ...process.env, PORT: String(port) },
      stdio: ["ignore", "pipe", "pipe"],
    });
    serverProcess.stdout.on("data", (chunk) => { serverOutput += chunk; });
    serverProcess.stderr.on("data", (chunk) => { serverOutput += chunk; });
  }
  const health = await waitForServer();
  browser = await launchChrome(["--enable-unsafe-webgpu"]);

  const page = await openReadyPage("?backend=wasm");
  const pageErrors = [];
  const externalRequests = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("request", (request) => {
    if (request.url().startsWith("http") && !request.url().startsWith(baseURL)) externalRequests.push(request.url());
  });
  await createCollection(page, names.main);
  await page.locator("#import-sample").click();
  await page.waitForFunction(() => document.querySelector("#import-status")?.textContent.includes("已写入 24 篇"), null, { timeout: 120000 });
  const importStatus = await page.locator("#import-status").textContent();
  assert.match(importStatus, /已写入 24 篇/);

  await search(page, "How does hybrid search combine keywords and vectors?", "hybrid");
  const initialHits = await readResults(page);
  assert.ok(initialHits.some((hit) => hit.id === "d01"));
  assert.ok(initialHits.some((hit) => hit.id === "d02"));
  assert.ok(initialHits.find((hit) => hit.id === "d01")?.text.includes("exact terms and related meaning"));
  assert.ok(initialHits.every((hit) => hit.score?.startsWith("RRF ")));
  await page.screenshot({ path: resolve(evidenceDir, "browser-search.png"), fullPage: true });

  await page.locator("#file-input").setInputFiles({
    name: "incremental.json",
    mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify([
      { id: "e2e_extra_1", text: "Incremental hybrid imports preserve existing local documents.", metadata: { category: "test" } },
      { id: "e2e_extra_2", text: "Collection switching keeps independent knowledge base state.", metadata: { category: "test" } },
    ])),
  });
  await page.waitForFunction(() => document.querySelector("#import-status")?.textContent.includes("已写入 2 篇"), null, { timeout: 120000 });

  await createCollection(page, names.empty);
  assert.equal(await page.evaluate(() => window.kbApp.state.count), 0);
  await search(page, "How does hybrid search combine keywords and vectors?", "hybrid");
  await page.locator("#results .empty-state").waitFor({ timeout: 120000 });
  await page.locator("#collection").selectOption(names.main);
  await page.waitForFunction(() => window.kbApp.state.count === 26);
  await search(page, "How does hybrid search combine keywords and vectors?", "hybrid");
  const persistedHits = await readResults(page);
  assert.ok(persistedHits.some((hit) => hit.id === "d01"));
  assert.ok(persistedHits.some((hit) => hit.id === "e2e_extra_1"));

  console.log("Running FP32/INT8 quality and performance experiment");
  const performanceEvidence = await page.evaluate(() => window.kbApp.runExperiment());
  await writeFile(resolve(evidenceDir, "performance.json"), `${JSON.stringify(performanceEvidence, null, 2)}\n`);
  assert.equal(performanceEvidence.result, "PASS");
  await page.screenshot({ path: resolve(evidenceDir, "experiment.png"), fullPage: true });
  assert.deepEqual(pageErrors, []);
  assert.deepEqual(externalRequests, []);
  await page.close();

  const fallbackPage = await openReadyPage("?backend=auto", () => {
    const NativeWorker = window.Worker;
    window.Worker = class extends NativeWorker {
      constructor(url, options) {
        const workerURL = new URL(url.toString(), location.href);
        if (!workerURL.pathname.includes("embedder.worker")) {
          super(url, options);
          return;
        }
        const source = `Object.defineProperty(navigator, "gpu", { value: undefined, configurable: true }); import ${JSON.stringify(workerURL.href)};`;
        super(URL.createObjectURL(new Blob([source], { type: "text/javascript" })), options);
      }
    };
  });
  await createCollection(fallbackPage, names.fallback);
  await fallbackPage.evaluate(() => window.kbApp.importDocuments([{ id: "fallback_probe", text: "WASM fallback keeps local inference available without WebGPU.", metadata: { source: "browser-e2e" } }]));
  await search(fallbackPage, "local inference without WebGPU", "WASM");
  const fallbackHits = await readResults(fallbackPage);
  const fallbackState = await fallbackPage.evaluate(() => ({ ...window.kbApp.state }));
  assert.equal(fallbackState.backend, "wasm");
  assert.equal(fallbackState.fallback, true);
  assert.ok(fallbackHits.some((hit) => hit.id === "fallback_probe"));
  await fallbackPage.close();

  console.log("Probing WebGPU availability and inference");
  const webgpu = await probeWebGPU();
  const evidence = {
    result: "PASS",
    testedAt: new Date().toISOString(),
    browserVersion: browser.version(),
    health,
    checks: {
      browserImportAndHybridSearch: true,
      originalTextAndRrfScores: true,
      incrementalJsonImport: true,
      collectionIsolationAndPersistence: true,
      automaticWasmFallback: true,
      quantizationAndPerformanceExperiment: performanceEvidence.result === "PASS",
      noMainFlowPageErrors: pageErrors.length === 0,
      noExternalHttpRequests: externalRequests.length === 0,
    },
    main: { importStatus, initialHits, persistedHits },
    fallback: { state: fallbackState, hits: fallbackHits },
    webgpu,
    performanceEvidence: "evidence/performance.json",
    screenshots: ["evidence/browser-search.png", "evidence/experiment.png"],
  };
  await writeFile(resolve(evidenceDir, "browser-e2e.json"), `${JSON.stringify(evidence, null, 2)}\n`);
  console.log(JSON.stringify({ result: evidence.result, checks: evidence.checks, webgpu: webgpu.status }, null, 2));
} catch (error) {
  console.error(error);
  if (serverOutput) console.error(serverOutput);
  process.exitCode = 1;
} finally {
  await Promise.all([...createdCollections].map((name) => fetch(`${baseURL}/api/collections/${name}`, { method: "DELETE" }).catch(() => undefined)));
  await browser?.close();
  if (serverProcess && serverProcess.exitCode === null) {
    serverProcess.kill("SIGTERM");
  }
}
