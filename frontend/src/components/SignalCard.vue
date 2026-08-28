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
      <span class="confidence" :class="confClass" @click="showScore = true">{{ card.confidence }}</span>
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

    <!-- 行4 引擎关卡（点击"详情"查看各关卡判定数值） -->
    <div class="levels">
      <span
        v-for="(v, k) in card.levels"
        :key="k"
        class="level-item"
        :class="okClass(v)"
      >
        {{ LEVEL_LABEL[k] || k }}:{{ v === true ? "✓" : v === false ? "✗" : v }}
      </span>
      <span class="level-detail-link" v-if="levelDetailRows.length" @click="showLevels = true">
        <van-icon name="records" /> 详情
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
        <span class="exec-cell" v-if="targetDisplay || card.strategy === 'ema_trend'">
          <i>第一目标</i>{{ targetDisplay ? fmtPrice(targetDisplay) : '随趋势(EMA50)' }} {{ quote }}
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
      <van-button v-if="canExecute" size="mini" type="primary" class="exec-btn" :disabled="executing" @click="onExecute">
        {{ executing ? "执行中…" : "确认执行" }}
      </van-button>
    </div>

    <!-- 一键执行确认弹窗（可调预算） -->
    <van-dialog v-model:show="showExecDialog" title="确认执行" show-cancel-button @confirm="confirmExecute">
      <div class="exec-dialog">
        <div class="ed-row"><span>标的</span><b>{{ shortSymbol(card.symbol) }} {{ card.direction === "long" ? "做多" : "做空" }}</b></div>
        <div class="ed-row">
          <span>杠杆(倍)</span>
          <input v-model="execLeverage" type="number" min="1" max="10" step="1" class="ed-input" />
        </div>
        <div class="ed-row"><span>拆分</span><b>市价 70% + 限价 30%</b></div>
        <div class="ed-row"><span>止损</span><b>{{ fmtPrice(card.execution?.stop_loss) }}</b></div>
        <div class="ed-row"><span>第一目标</span><b>{{ targetDisplay ? fmtPrice(targetDisplay) : "-" }}</b></div>
        <div class="ed-row">
          <span>预算(USDT)</span>
          <input v-model="execBudget" type="number" step="0.01" min="0" class="ed-input" />
        </div>
        <div class="ed-tip">
          默认直接下单：10x 杠杆 / 5U 本金（名义 = 5×10 = 50U）
          <template v-if="totalBalance">；余额 {{ totalBalance.toFixed(2) }} USDT</template>
          ；已有持仓时自动跳过
        </div>
      </div>
    </van-dialog>

    <!-- 评分明细弹窗（点击分数） -->
    <van-popup v-model:show="showScore" position="bottom" round class="detail-popup">
      <div class="detail-head">评分明细 · {{ card.confidence }} 分<span class="pass-line">通过线 {{ card.score_pass_line ?? 60 }}</span></div>
      <div class="score-group" v-for="g in scoreGroups" :key="g.cat">
        <div class="score-cat">{{ g.cat }}（{{ g.sum }}/{{ g.maxSum }}）</div>
        <div class="score-row" v-for="it in g.items" :key="it.name">
          <span class="score-name">{{ it.name }}</span>
          <span class="score-note">{{ it.note }}</span>
          <b class="score-val" :class="{ miss: it.score < it.max && it.max > 0 }">{{ it.score }}/{{ it.max }}</b>
        </div>
      </div>
      <div class="detail-empty" v-if="!card.score_detail?.length">（旧信号无评分明细）</div>
    </van-popup>

    <!-- 关卡详情弹窗（点击"详情"） -->
    <van-popup v-model:show="showLevels" position="bottom" round class="detail-popup">
      <div class="detail-head">信号满足条件（关卡判定值）</div>
      <div class="score-group" v-for="(kv, cat) in card.levels_detail" :key="cat">
        <div class="score-cat">{{ cat }}</div>
        <div class="score-row" v-for="(v, k) in kv" :key="k">
          <span class="score-name">{{ k }}</span>
          <b class="score-val">{{ fmtDetailVal(v) }}</b>
        </div>
      </div>
      <div class="detail-empty" v-if="!levelDetailRows.length">（旧信号无关卡明细）</div>
    </van-popup>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from "vue";
import { showToast } from "vant";
import {
  fmtClock, fmtPct, fmtPrice, fmtTime, shortSymbol, quoteSymbol,
  FUNDING_TIER, LEVEL_LABEL,
} from "../utils/format";
import { api } from "../api/http";

const props = defineProps({ card: Object });

const showScore = ref(false);
const showLevels = ref(false);

// 评分明细按类别分组（含各类合计）
const scoreGroups = computed(() => {
  const items = props.card.score_detail || [];
  const groups = [];
  for (const it of items) {
    let g = groups.find((x) => x.cat === it.cat);
    if (!g) {
      g = { cat: it.cat, items: [], sum: 0, maxSum: 0 };
      groups.push(g);
    }
    g.items.push(it);
    g.sum += it.score;
    g.maxSum += it.max;
  }
  return groups;
});
// 关卡详情行数（决定"详情"入口是否显示）
const levelDetailRows = computed(() => {
  const ld = props.card.levels_detail;
  return ld && typeof ld === "object" ? Object.keys(ld) : [];
});
function fmtDetailVal(v) {
  if (v === true) return "✓";
  if (v === false) return "✗";
  if (typeof v === "object" && v !== null) return JSON.stringify(v);
  return String(v);
}

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

// 第一目标止盈价：优先用执行计划真值；旧信号缺 target 时按 2.5×止损距离估算
const targetDisplay = computed(() => {
  const e = props.card.execution;
  if (e?.target) return e.target;
  const price = e?.market_price, stop = e?.stop_loss;
  if (price && stop && stop > 0) {
    const stopDist = Math.abs(price - stop);
    const dir = props.card.direction === "long" ? 1 : -1;
    return +(price + dir * stopDist * 2.5).toFixed(8);
  }
  return null;
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
  props.card.confidence >= 60 ? "high" : props.card.confidence >= 50 ? "mid" : "low",
);

// 一键执行：仅有效且未执行过的信号可点（dry_run 下为纸面模拟）
const canExecute = computed(() =>
  !props.card.executed && props.card.status !== "stopped_out" && props.card.status !== "expired"
    && now.value <= (props.card.expires_at || 0),
);
const executing = ref(false);
const showExecDialog = ref(false);
const execBudget = ref(0);
const execLeverage = ref(10); // 默认 10 倍，可选 1~10
const totalBalance = ref(0);

// 一键执行：先立即弹窗（秒开），余额后台拉取后提示；默认本金固定 5U（10x/5U 自动下单同款参数）
async function onExecute() {
  execLeverage.value = 10;
  totalBalance.value = 0;
  execBudget.value = 5; // 默认本金 5U
  showExecDialog.value = true;
  try {
    const acct = await api.account();
    if (acct.ok) {
      totalBalance.value = Number(acct.total) || 0;
    }
  } catch (e) {
    /* 余额获取失败不阻塞弹窗，用户可手动输入预算 */
  }
}
async function confirmExecute() {
  const b = Number(execBudget.value) || 0;
  if (b <= 0) {
    showToast("预算需大于 0");
    return;
  }
  const lev = Math.min(10, Math.max(1, Number(execLeverage.value) || 10)); // 限制 1~10 倍
  showExecDialog.value = false;
  executing.value = true;
  try {
    const r = await api.executeSignal(props.card.id, { budget_usdt: b, leverage: lev });
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
.level-detail-link {
  font-size: 11px; padding: 1px 6px; border-radius: 4px; cursor: pointer;
  background: rgba(255, 159, 28, 0.10); color: #ff9f1c;
  display: inline-flex; align-items: center; gap: 2px;
}

/* 评分明细 / 关卡详情弹窗 */
.detail-popup { max-height: 70vh; overflow-y: auto; padding: 14px 16px 24px; }
.detail-head {
  font-size: 15px; font-weight: 700; margin-bottom: 10px;
  display: flex; align-items: baseline; gap: 8px;
}
.detail-head .pass-line { font-size: 11px; color: #ff9f1c; font-weight: 400; }
.score-group { margin-bottom: 12px; }
.score-cat {
  font-size: 12px; font-weight: 700; color: #ff9f1c;
  margin-bottom: 4px; padding-bottom: 2px; border-bottom: 1px solid #2c2c2e;
}
.score-row {
  display: flex; align-items: center; gap: 8px;
  font-size: 12px; padding: 4px 0;
}
.score-name { flex: 0 0 118px; color: #e8e8ea; }
.score-note { flex: 1; color: #8e8e93; font-size: 11px; text-align: right; }
.score-val { flex: 0 0 52px; text-align: right; color: #34c759; font-weight: 700; }
.score-val.miss { color: #ff9f1c; }
.detail-empty { text-align: center; color: #8e8e93; font-size: 12px; padding: 20px 0; }

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
.exec-dialog { padding: 6px 16px 4px; }
.ed-row {
  display: flex; justify-content: space-between; align-items: center;
  font-size: 13px; padding: 7px 0; border-bottom: 1px solid #2c2c2e;
}
.ed-row span { color: #8e8e93; }
.ed-row b { color: #e8e8ea; }
.ed-input {
  width: 120px; text-align: right; background: #2c2c2e; border: none;
  border-radius: 6px; padding: 6px 8px; color: #34c759; font-size: 15px; outline: none;
}
.ed-tip { font-size: 11px; color: #8e8e93; padding: 8px 0 4px; }
.rr { font-size: 12px; color: #e8e8ea; font-weight: 600; }
.funding { font-size: 12px; }
</style>
