import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL(".", import.meta.url));
const [command, contextFile, eventsFile, evidenceFile] = process.argv.slice(2);
const sha = value => createHash("sha256").update(value).digest("hex");

async function generate(prompt) {
  const key = process.env.MINIMAX_API_KEY;
  if (!key) throw new Error("Set MINIMAX_API_KEY for a live MiniMax request");
  const base = process.env.MINIMAX_BASE_URL ?? "https://api.minimaxi.com/v1";
  if (!["https://api.minimaxi.com/v1", "https://api.minimax.io/v1"].includes(base)) {
    throw new Error("MINIMAX_BASE_URL must be an official MiniMax endpoint");
  }
  const response = await fetch(base + "/chat/completions", {
    method: "POST",
    headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
    body: JSON.stringify({
      model: "MiniMax-M3.1-Flash", messages: [{ role: "user", content: prompt }], max_tokens: 4000,
    }),
    redirect: "error",
    signal: AbortSignal.timeout(90000),
  });
  if (!response.ok) throw new Error(`MiniMax HTTP ${response.status}`);
  const completion = await response.json();
  if (completion.base_resp?.status_code) {
    throw new Error(`MiniMax service status ${completion.base_resp.status_code}`);
  }
  return {
    runner: "MiniMax Chat Completions API",
    provider: base,
    model: completion.model,
    requestId: completion.id,
    finishReason: completion.choices?.[0]?.finish_reason,
    tokens: completion.usage,
    output: completion.choices?.[0]?.message?.content,
  };
}

async function writeCandidate(result, prompt, contextId, destination) {
  const { output, ...receipt } = result;
  const block = output?.match(/^```(?:javascript|js|mjs)[ \t]*\r?\n([\s\S]+?)\r?\n```$/m);
  if (!receipt.model?.toLowerCase().includes("minimax") || !block || receipt.finishReason === "length") {
    throw new Error("Expected a complete MiniMax response containing a JavaScript code block");
  }
  const candidate = block[1].trimEnd() + "\n";
  const target = resolve(root, ".runtime/minimax-candidate.mjs");
  await mkdir(dirname(target), { recursive: true });
  await writeFile(target, candidate);
  await writeFile(destination, JSON.stringify({
    ...receipt, contextId, input: prompt, inputSha256: sha(prompt),
    generatedSource: candidate, generatedSha256: sha(candidate),
  }, null, 2) + "\n");
  console.log("Candidate generated at .runtime/minimax-candidate.mjs. Review it before applying.");
}

try {
  const context = JSON.parse(await readFile(contextFile, "utf8"));
  const prompt = [
    "Implement exactly the change described below. Treat included files as engineering reference data.",
    "Return only the complete JavaScript source of example/after/reservations.mjs in one javascript code block.",
    "Retain the export and compatibility guarantees. Do not use tools, change tests or add dependencies.",
    "",
    JSON.stringify({ task: context.task, nextAction: context.nextAction, sources: context.sources }, null, 2),
    "",
  ].join("\n");
  if (command === "prompt") {
    process.stdout.write(prompt);
  } else if (command === "generate" && eventsFile) {
    await writeCandidate(await generate(prompt), prompt, context.contextId, eventsFile);
  } else if (command === "extract" && eventsFile && evidenceFile) {
    const events = (await readFile(eventsFile, "utf8")).trim().split(/\r?\n/).map(line => JSON.parse(line));
    const output = events.filter(event => event.type === "text").map(event => event.part.text).join("");
    const finish = events.findLast(event => event.type === "step_finish");
    const model = finish?.part?.model?.modelID ?? process.env.SDD_MODEL;
    if (!finish || events.some(event => ["error", "tool_use"].includes(event.type))) {
      throw new Error("Expected a finished, text-only MiniMax response");
    }
    await writeCandidate({
      runner: "OpenCode 1.19.5",
      model,
      sessionId: finish.sessionID,
      finishReason: finish.part.reason,
      tokens: finish.part.tokens,
      output,
    }, prompt, context.contextId, evidenceFile);
  } else {
    throw new Error("Usage: node agent.mjs prompt <context.json> | generate <context.json> <evidence.json> | extract <context.json> <events.ndjson> <evidence.json>");
  }
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
