// WS 客户端：断线重连（指数退避），事件分发
import { reactive } from "vue";

const EVENTS = ["signal:new", "signal:update", "scan:report", "status:update"];

export const wsState = reactive({
  connected: false,
  lastSignalAt: 0,
  lastReport: null,
});

const listeners = new Set();

export function onWsEvent(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

let ws = null;
let retry = 0;
let timer = null;

function notify(event, data) {
  if (event === "signal:new") wsState.lastSignalAt = Date.now();
  if (event === "scan:report") wsState.lastReport = data;
  for (const fn of listeners) fn(event, data);
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/api/ws`);

  ws.onopen = () => {
    wsState.connected = true;
    retry = 0;
  };
  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (EVENTS.includes(msg.event)) notify(msg.event, msg.data);
    } catch (_) {
      /* ignore */
    }
  };
  ws.onclose = () => {
    wsState.connected = false;
    scheduleReconnect();
  };
  ws.onerror = () => ws.close();
}

function scheduleReconnect() {
  // 指数退避：1s → 2s → 4s → … 上限 30s
  const delay = Math.min(1000 * 2 ** retry, 30000);
  retry += 1;
  clearTimeout(timer);
  timer = setTimeout(connect, delay);
}

export function startWs() {
  connect();
}
