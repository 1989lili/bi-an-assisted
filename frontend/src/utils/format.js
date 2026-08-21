// 公共格式化工具

export function fmtTime(ms) {
  if (!ms) return "-";
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function fmtClock(ms) {
  if (!ms) return "-";
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export function fmtNum(v, digits = 4) {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  if (Math.abs(v) >= 1000) return v.toFixed(0);
  return Number(v).toFixed(digits);
}

export function fmtPrice(v) {
  // 价格：千分位 + 智能小数位（≥1 保留 1-2 位，<1 保留 6 位）
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  if (Math.abs(v) >= 1) {
    return Number(v).toLocaleString("en-US", {
      minimumFractionDigits: 1,
      maximumFractionDigits: 2,
    });
  }
  return Number(v).toFixed(6);
}

export function quoteSymbol(symbol) {
  // 计价币：BTC/USDT:USDT → USDT
  return symbol.split("/")[1]?.split(":")[0] || "USDT";
}

export function fmtPct(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return `${(v * 100).toFixed(digits)}%`;
}

export function shortSymbol(symbol) {
  return symbol.replace("/USDT:USDT", "").replace(":USDT", "");
}

export const FUNDING_TIER = {
  danger: { text: "费率飙升", color: "#ee4d38", desc: "24h 飙升≥3倍，拦截开仓" },
  stable_high: { text: "高位稳定", color: "#ff9f1c", desc: "0.03%~0.1%，仓位×0.7" },
  normal: { text: "正常", color: "#34c759", desc: "<0.03%" },
  unknown: { text: "未知", color: "#8e8e93", desc: "无费率历史" },
};

export const LEVEL_LABEL = {
  market_env: "市场环境",
  direction_gate: "方向门",
  trigger: "双周期扳机",
  volume_veto: "量能",
  risk_brake: "风控刹车",
  candle_check: "K线形态",
  macro_silence: "宏观静默",
};
