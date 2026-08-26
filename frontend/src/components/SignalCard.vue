<template>
  <div class="sig-card" :class="[card.direction, expired ? 'expired' : '']">
    <!-- 行1 头部：方向 + 币种 + 扳机 ······ 置信度 -->
    <div class="head">
      <span class="dir-badge" :class="card.direction">
        {{ card.direction === "long" ? "做多" : "做空" }}
      </span>
      <span class="symbol">{{ shortSymbol(card.symbol) }}</span>
      <span class="strat-chip" v-if="card.strategy === 'ema_trend'">EMA趋势</span>
      <span class="trigger-chip" v-if="card.trigger_level">
        {{ levelText }}
      </span>
      <span class="confidence" :class="confClass">{{ card.confidence }}</span>
    </div>

    <!-- 行2 元信息：生成时间 + 生命周期状态 + 剩余有效期 -->
    <div class="meta">
      <span class="meta-time">{{ fmtTime(card.created_at) }}</span>
      <span class="status-badge" :class="statusClass">{{ statusText }}</span>
      <span class="ttl" :class="{ warn: ttlMin < 10 }" v-if="!expired">
        {{ card.strategy === "ema_trend" ? "趋势跟踪" : `有效 ${ttlMin}min` }}
      </span>
    </div>

    <!-- 行3 信号摘要（新信号已精简为扳机+量比；旧数据最多 2 行省略） -->
    <div class="reason" v-if="card.reason">{{ card.reason }}</div>
    <div class="exit-reason" v-if="exitReason">离场原因：{{ exitReason }}</div>

    <!-- 行4 引擎关卡 -->
    <div class="levels">
      <span
        v-for="(v, k) in card.levels"
        :key="k"
        class="level-item"
        :class="okClass(v)"
      >
        {{ LEVEL_LABEL[k] || k }}:{{ v === true ? "✓" : v === false ? "✗" : v }}
      </span>
    </div>

    <!-- 行5 实时行情（60s 高频监控更新） -->
    <div class="live" v-if="card.live_price">
      <span class="live-label">实时</span>
      <span class="live-price" :class="liveClass">{{ fmtPrice(card.live_price) }} {{ quote }}</span>
      <span class="live-pct" :class="liveClass">{{ livePct }}</span>
      <span class="live-ts">{{ card.live_updated_at ? fmtClock(card.live_updated_at) : "" }}</span>
    </div>

    <!-- 行6 执行参数 2×2 网格 -->
    <div class="exec">
      <div class="exec-row">
        <span class="exec-cell main">
          <i>市价</i>{{ fmtPrice(card.execution?.market_price) }} {{ quote }}<em>×{{ card.execution?.market_pct ?? 70 }}%</em>
        </span>
        <span class="exec-cell" v-if="card.execution?.limit_price">
          <i>限价</i>{{ fmtPrice(card.execution.limit_price) }} {{ quote }}<em>×{{ card.execution?.limit_pct ?? 30 }}%</em>
        </span>
      </div>
      <div class="exec-row">
        <span class="exec-cell stop">
          <i>止损</i>{{ fmtPrice(card.execution?.stop_loss) }} {{ quote }}
        </span>
        <span class="exec-cell" v-if="card.execution?.target || card.strategy === 'ema_trend'">
          <i>第一目标</i>{{ card.execution?.target ? fmtPrice(card.execution.target) : '随趋势(EMA50)' }} {{ quote }}
        </span>
      </div>
    </div>

    <!-- 行7 底部：盈亏比 + 费率档位 + 一键执行 -->
    <div class="foot">
      <span class="rr">{{ card.strategy === "ema_trend" ? "盈亏比 趋势跟踪" : `盈亏比 ${rr}` }}</span>
      <span class="funding" :style="{ color: FUNDING_TIER[card.funding?.tier]?.color }">
        {{ FUNDING_TIER[card.funding?.tier]?.text || "费率" }}
        <template v-if="card.funding?.rate"> {{ fmtPct(card.funding.rate, 3) }}</template>
        <template v-if="card.funding?.position_factor && card.funding.position_factor < 1">
          ·仓位×{{ card.funding.position_factor }}
        </template>
      </span>
      <!-- 暂隐藏「确认执行」：待高危项（鉴权/平仓发单/reduceOnly 等）修复后再启用 -->
      <van-button v-if="false" size="mini" type="primary" class="exec-btn" :disabled="executing" @click="onExecute">
        {{ executing ? "执行中…" : "确认执行" }}
      </van-button>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from "vue";
import { showConfirmDialog, showToast } from "vant";
import {
  fmtClock, fmtPct, fmtPrice, fmtTime, shortSymbol, quoteSymbol,
  FUNDING_TIER, LEVEL_LABEL,
} from "../utils/format";
import { api } from "../api/http";

const props = defineProps({ card: Object });

const quote = quoteSymbol(props.card.symbol);

const now = ref(Date.now());
let timer = null;
onMounted(() => { timer = setInterval(() => { now.value = Date.now(); }, 30000); });
onUnmounted(() => clearInterval(timer));

const expired = computed(() =>
  props.card.status === "expired" || now.value > (props.card.expires_at || 0),
);
const ttlMin = computed(() =>
  Math.max(0, Math.round(((props.card.expires_at || 0) - now.value) / 60000)),
);

// 生命周期状态（高频监控判定）：有效 / 已止损 / 已过期
const statusText = computed(() => {
  if (props.card.status === "stopped_out") return "已离场";
  if (expired.value) return "已过期";
  return "有效";
});
const statusClass = computed(() => {
  if (props.card.status === "stopped_out") return "gray";
  if (expired.value) return "gray";
  return "ok";
});

// (stopped_out) 离场原因：提取 reason 中"离场：xxx"（如"收盘跌破 EMA50"）
const exitReason = computed(() => {
  const m = (props.card.reason || "").match(/离场[：:]\s*(.+)$/);
  return m ? m[1] : "";
});

// 实时价相对入场价浮动（方向色：做多涨绿跌红，做空反之）
const livePct = computed(() => {
  const entry = props.card.execution?.market_price;
  const live = props.card.live_price;
  if (!entry || !live) return "";
  const pct = ((live - entry) / entry) * 100;
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%`;
});
const liveClass = computed(() => {
  if (!props.card.live_price) return "";
  const entry = props.card.execution?.market_price;
  const up = props.card.live_price >= (entry || 0);
  const good = props.card.direction === "long" ? up : !up;
  return good ? "up" : "down";
});

// 盈亏比保留 2 位小数（后端原始值可能是长浮点）
const rr = computed(() => {
  const v = props.card.execution?.risk_reward;
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return Number(v).toFixed(2);
});

const levelText = computed(() =>
  props.card.trigger_level === "A" ? "A级回踩"
    : props.card.trigger_level === "B" ? "B级突破"
    : props.card.trigger_level === "C" ? "C级RSI" : "",
);
const confClass = computed(() =>
  props.card.confidence >= 70 ? "high" : props.card.confidence >= 50 ? "mid" : "low",
);

// 一键执行：仅有效且未执行过的信号可点（dry_run 下为纸面模拟）
const canExecute = computed(() =>
  !props.card.executed && props.card.status !== "stopped_out" && props.card.status !== "expired"
    && now.value <= (props.card.expires_at || 0),
);
const executing = ref(false);
async function onExecute() {
  try {
    await showConfirmDialog({
      title: "确认执行",
      message: `确认对 ${shortSymbol(props.card.symbol)} 下达 ${props.card.direction === "long" ? "做多" : "做空"} 市价单？`,
    });
  } catch (_) {
    return;
  }
  executing.value = true;
  try {
    const r = await api.executeSignal(props.card.id);
    showToast(r.dry_run ? "纸面模拟下单成功" : `实盘下单成功（${r.side} ${r.amount}）`);
  } catch (e) {
    showToast(e.message);
  } finally {
    executing.value = false;
  }
}
function okClass(v) {
  if (v === true) return "ok";
  if (v === false) return "bad";
  return "info";
}
</script>

<style scoped>
.sig-card {
  background: #1c1c1e;
  border-radius: 12px;
  padding: 12px;
  margin-bottom: 10px;
  border-left: 4px solid #34c759;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
}
.sig-card.short { border-left-color: #ff453a; }
/* 过期：整体置灰 + 降透明度（沉底排序在 SignalFeed） */
.sig-card.expired {
  background: #232326;
  border-left-color: #48484a;
  opacity: 0.72;
  filter: grayscale(0.5);
}

/* 行1 头部 */
.head { display: flex; align-items: center; gap: 8px; }
.dir-badge {
  padding: 2px 8px; border-radius: 6px; font-size: 12px; font-weight: 600;
}
.dir-badge.long { background: rgba(52, 199, 89, 0.18); color: #34c759; }
.dir-badge.short { background: rgba(255, 69, 58, 0.18); color: #ff453a; }
.symbol { font-size: 16px; font-weight: 700; }
.strat-chip {
  font-size: 11px; padding: 1px 6px; border-radius: 4px;
  background: rgba(0, 122, 255, 0.15); color: #0a84ff;
}
.trigger-chip {
  font-size: 11px; padding: 1px 6px; border-radius: 4px;
  background: rgba(255, 159, 28, 0.15); color: #ff9f1c;
}
.confidence { margin-left: auto; font-size: 18px; font-weight: 800; }
.confidence.high { color: #34c759; }
.confidence.mid { color: #ff9f1c; }
.confidence.low { color: #8e8e93; }

/* 行2 元信息：时间 + 状态徽章 + 剩余有效期 */
.meta {
  display: flex; align-items: center; gap: 8px;
  margin-top: 6px; font-size: 11px; color: #8e8e93;
}
.status-badge {
  font-size: 11px; padding: 1px 8px; border-radius: 4px;
  font-weight: 600;
}
.status-badge.ok { background: rgba(52, 199, 89, 0.15); color: #34c759; }
.status-badge.bad { background: rgba(255, 69, 58, 0.15); color: #ff453a; }
.status-badge.gray { background: #2c2c2e; color: #8e8e93; }
.ttl { font-size: 11px; }
.ttl.warn { color: #ff9f1c; }

/* 行3 摘要：最多 2 行省略 */
.reason {
  margin: 8px 0; font-size: 13px; color: #c7c7cc; line-height: 1.5;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
}
.exit-reason {
  margin: 4px 0 8px; font-size: 12px; color: #ff9f1c;
  background: rgba(255, 159, 28, 0.08); border-radius: 6px; padding: 4px 8px;
}

/* 行4 关卡 */
.levels { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 8px; }
.level-item {
  font-size: 11px; padding: 1px 6px; border-radius: 4px;
  background: #2c2c2e; color: #8e8e93;
}
.level-item.ok { background: rgba(52, 199, 89, 0.12); color: #34c759; }
.level-item.bad { background: rgba(255, 69, 58, 0.12); color: #ff453a; }

/* 行5 实时行情 */
.live {
  display: flex; align-items: center; gap: 6px;
  background: rgba(255, 255, 255, 0.04); border-radius: 8px;
  padding: 5px 8px; margin-bottom: 8px; font-size: 12px;
}
.live-label { color: #8e8e93; }
.live-price { font-weight: 700; }
.live-pct { font-weight: 600; }
.live-ts { margin-left: auto; color: #8e8e93; font-size: 11px; }
.live-price.up, .live-pct.up { color: #34c759; }
.live-price.down, .live-pct.down { color: #ff453a; }

/* 行6 执行参数 2×2 网格 */
.exec { display: flex; flex-direction: column; gap: 5px; margin-bottom: 8px; }
.exec-row { display: grid; grid-template-columns: 1fr 1fr; gap: 5px; }
.exec-cell {
  display: flex; align-items: baseline; gap: 4px;
  font-size: 12px; padding: 4px 8px; border-radius: 6px;
  background: #2c2c2e; color: #e8e8ea;
}
.exec-cell i { font-style: normal; color: #8e8e93; font-size: 11px; }
.exec-cell em { font-style: normal; font-size: 10px; color: #8e8e93; }
.exec-cell.stop { color: #ff9f1c; }
.exec-cell.main { background: rgba(52, 199, 89, 0.15); color: #34c759; font-weight: 600; }

/* 行7 底部 */
.foot {
  display: flex; justify-content: space-between; align-items: center;
  border-top: 1px solid #2c2c2e; padding-top: 8px;
}
.exec-btn { margin-left: auto; }
.rr { font-size: 12px; color: #e8e8ea; font-weight: 600; }
.funding { font-size: 12px; }
</style>
