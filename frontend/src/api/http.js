// REST 封装：同源部署（后端托管），直接相对路径
const BASE = "/api";

async function request(path, options = {}) {
  const token = localStorage.getItem("app_token") || "";
  const headers = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const resp = await fetch(BASE + path, {
    headers,
    ...options,
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch (_) {
      /* ignore */
    }
    throw new Error(`${resp.status} ${detail}`);
  }
  return resp.json();
}

export const http = {
  get: (path) => request(path),
  post: (path, data) =>
    request(path, { method: "POST", body: JSON.stringify(data) }),
  put: (path, data) =>
    request(path, { method: "PUT", body: JSON.stringify(data) }),
  delete: (path) => request(path, { method: "DELETE" }),
};

// ---- 业务接口 ----
export const api = {
  status: () => http.get("/status"),
  signals: (params = "") => http.get(`/signals${params}`),
  executeSignal: (id) => http.post(`/signals/${encodeURIComponent(id)}/execute`, {}),
  symbolSnapshot: (symbol) => http.get(`/symbol/snapshot?symbol=${encodeURIComponent(symbol)}`),
  watchlist: () => http.get("/watchlist"),
  addWatch: (symbol) => http.post("/watchlist", { symbol }),
  removeWatch: (symbol) => http.delete(`/watchlist?symbol=${encodeURIComponent(symbol)}`),
  positions: (status = "open") => http.get(`/positions?status=${status}`),
  createPosition: (data) => http.post("/positions", data),
  closePosition: (id) => http.post(`/positions/${id}/close`),
  positionStatus: (id) => http.get(`/positions/${id}/status`),
  macroEvents: () => http.get("/macro-events"),
  addMacroEvent: (data) => http.post("/macro-events", data),
  removeMacroEvent: (id) => http.delete(`/macro-events/${id}`),
  settings: () => http.get("/settings"),
  updateSetting: (key, value) => http.put(`/settings/${key}`, { value }),
  scanLogs: (limit = 50) => http.get(`/scan-logs?limit=${limit}`),
};
