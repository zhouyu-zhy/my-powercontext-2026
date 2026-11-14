async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

export const api = {
  health: () => request("/api/health"),
  collections: () => request("/api/collections"),
  createCollection: (name) => request("/api/collections", { method: "POST", body: JSON.stringify({ name }) }),
  deleteCollection: (name) => request(`/api/collections/${name}`, { method: "DELETE" }),
  addDocuments: (collection, records) => request(`/api/collections/${collection}/documents`, { method: "POST", body: JSON.stringify({ records }) }),
  search: (collection, payload) => request(`/api/collections/${collection}/search`, { method: "POST", body: JSON.stringify(payload) }),
};
