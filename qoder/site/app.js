const mode = document.querySelector("#mode");
const search = document.querySelector("#search");
const write = document.querySelector("#write");
const examples = document.querySelector("#examples");
const results = document.querySelector("#results");
const searchStatus = document.querySelector("#search-status");
const saveStatus = document.querySelector("#save-status");

async function request(path, body) {
  const response = await fetch(path, body ? {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  } : {});
  if (!response.headers.get("Content-Type")?.includes("application/json")) {
    throw new Error("服务响应不可用。");
  }
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "请求失败，请重试。");
  return payload;
}

function element(tag, text) {
  const node = document.createElement(tag);
  node.textContent = text;
  return node;
}

function render(query, data, context = "") {
  searchStatus.textContent = `「${query}」召回 ${data.items.length} 条笔记。${context}`;
  if (!data.items.length) searchStatus.textContent = `「${query}」没有匹配的笔记。${context}`;
  results.replaceChildren(...data.items.map(item => {
    const card = document.createElement("article");
    card.className = "record";
    const citation = document.createElement("footer");
    citation.append(element("small", `PowerContext · ${item.artifact.artifact_id} · v${item.artifact.revision}`));
    card.append(
      element("small", item.project),
      element("h3", item.title),
      element("p", item.notes),
      element("p", `下一步　${item.next_step}`),
      citation,
    );
    return card;
  }));
}

search.addEventListener("submit", async event => {
  event.preventDefault();
  const query = new FormData(search).get("query").trim();
  const button = search.querySelector("button");
  button.disabled = true;
  searchStatus.textContent = "正在从 PowerContext 召回…";
  results.replaceChildren();
  try {
    render(query, await request("./api/recall", { query }));
  } catch (error) {
    searchStatus.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

write.addEventListener("submit", async event => {
  event.preventDefault();
  const button = write.querySelector("button");
  const note = Object.fromEntries(new FormData(write));
  button.disabled = true;
  saveStatus.textContent = "正在保存…";
  try {
    const { artifact } = await request("./api/notes", note);
    write.reset();
    search.elements.query.value = note.project.trim();
    saveStatus.textContent = `已保存 · ${artifact.artifact_id} · v${artifact.revision}。可按项目关键词检索。`;
  } catch (error) {
    saveStatus.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

async function start() {
  const connection = await request("./api/mode").catch(() => null);
  if (connection?.mode === "live") {
    mode.textContent = "实时检索";
    search.hidden = write.hidden = false;
    searchStatus.textContent = "输入项目关键词查找已保存的记录。";
    return;
  }
  mode.textContent = "只读演示";
  document.querySelector("#guide").hidden = false;
  const demo = await request("./demo.json");
  const recorded = new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Shanghai",
  }).format(new Date(demo.generated_at));
  const buttons = demo.examples.map(({ label, query, result }) => {
    const button = element("button", label);
    button.type = "button";
    button.setAttribute("aria-pressed", "false");
    button.addEventListener("click", () => {
      for (const choice of buttons) choice.setAttribute("aria-pressed", String(choice === button));
      render(query, result, ` 回放于 ${recorded} 记录的响应。`);
    });
    return button;
  });
  examples.replaceChildren(...buttons);
  examples.hidden = false;
  buttons[0]?.click();
}

start().catch(() => {
  searchStatus.textContent = "无法加载演示数据，请刷新页面重试。";
});
