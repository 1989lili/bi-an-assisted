<template>
  <div class="feed">
    <!-- 市场环境面板 -->
    <div class="env-panel" v-if="env">
      <div class="env-main">
        <span class="env-dot" :class="env.env"></span>
        <span class="env-text">
          {{ env.env === "bull" ? "大盘多头" : env.env === "bear" ? "大盘空头" : "震荡" }}
        </span>
        <span class="env-sub">
          BTC 4h {{ env.btc_bull ? "多头" : "空头" }} · 涨跌
          {{ env.up_count != null ? env.up_count + ":" + env.down_count : "-" }}
        </span>
      </div>
      <div class="env-right">
        <span class="conn" :class="wsState.connected ? 'on' : 'off'">
          {{ wsState.connected ? "实时连接" : "重连中…" }}
        </span>
        <span class="env-ts" v-if="env.ts">{{ fmtClock(env.ts) }}</span>
      </div>
    </div>

    <!-- 宏观静默横幅 -->
    <div class="macro-banner" v-if="macroSilence || nextEvent" :class="{ silent: macroSilence }">
      <van-icon :name="macroSilence ? 'warning-o' : 'clock-o'" />
      <span v-if="macroSilence">宏观静默中：{{ nextEvent ? nextEvent.title : "高影响力数据" }} 窗口期，暂停新开仓，持仓已收紧止损</span>
      <span v-else>距宏观事件 {{ nextEvent.title }} 约 {{ nextEvent.minsAway }} 分钟（届时暂停开仓）</span>
    </div>

    <!-- 扫描状态 -->
    <div class="scan-report">
      <van-icon name="replay" class="spin-icon" />
      <span>
        {{ reportTs ? "上次精扫 " + fmtClock(reportTs) : "引擎扫描中…" }}
        · 候选 {{ reportCandidates.length }} 币 · 信号 {{ reportSignalCount }} 条
      </span>
      <span class="report-ts" v-if="env">{{ fmtClock(env.ts) }}</span>
    </div>

    <!-- 候选池（默认折叠，点标题展开） -->
    <div class="pool" v-if="reportCandidates.length">
      <div class="pool-head" @click="showPool = !showPool">
        <span>候选池（成交额Top100 + 涨幅Top100 + 异动 + 自选，已滤僵尸币）</span>
        <span class="pool-toggle">
          共 {{ reportCandidates.length }} 币
          <van-icon :name="showPool ? 'arrow-up' : 'arrow-down'" />
        </span>
      </div>
      <!-- 引擎过滤入口：始终可见，不受候选池折叠影响（易发现性优化） -->
      <div class="pool-filter" @click.stop="showRejections = !showRejections">
        <span>看引擎过滤 {{ rejectionList.length }} 条</span>
        <van-icon :name="showRejections ? 'arrow-up' : 'arrow-down'" />
      </div>
      <template v-if="showPool">
        <div class="pool-chips">
          <span v-for="s in reportCandidates" :key="s" class="chip" :class="{ rejected: rejectMap[s] }">
            {{ shortSymbol(s) }}
          </span>
        </div>
      </template>
      <div v-if="showRejections" class="rejections">
        <div v-for="r in rejectionList" :key="r.symbol" class="rej-row">
          <span class="rej-symbol">{{ shortSymbol(r.symbol) }}</span>
          <span class="rej-reason">{{ r.reason }}</span>
        </div>
        <div v-if="!rejectionList.length" class="rej-empty">本轮暂无否决记录</div>
      </div>
    </div>

    <!-- 启动感知监控池（成交额20~220实时监听，默认折叠） -->
    <div class="pool launch-pool" v-if="launchWatchlist.length">
      <div class="pool-head" @click="showLaunch = !showLaunch">
        <span>🚀 启动感知池（24h成交额排名20~220，实时监听）</span>
        <span class="pool-toggle">
          共 {{ launchWatchlist.length }} 币 · 观察 {{ launchPool.length }} 币
          <van-icon :name="showLaunch ? 'arrow-up' : 'arrow-down'" />
        </span>
      </div>
      <div class="launch-watch-line" v-if="launchPool.length">
        <span class="launch-watch-label">L3实时观察：</span>
        <span v-for="s in launchPool" :key="s" class="chip watch">{{ shortSymbol(s) }}</span>
      </div>
      <div class="launch-watch-line dim" v-else>观察集为空：等待 1h K 线收盘评估 L1+L2（新入池标的已启动初始评估）</div>
      <template v-if="showLaunch">
        <div class="pool-chips">
          <span v-for="s in launchWatchlist" :key="s" class="chip" :class="{ watch: launchPool.includes(s) }">
            {{ shortSymbol(s) }}
          </span>
        </div>
      </template>
    </div>

    <!-- 信号列表：仅显示有效信号（离场/过期自动隐藏） -->
    <van-empty v-if="!loading && signals.length === 0" description="暂无信号（引擎持续监控中，触发后实时推送）" />
    <div v-for="card in activeSignals" :key="card.id" class="signal-item" :class="{ fresh: card.id === newestId }">
      <SignalCard :card="card" />
    </div>
    <van-empty v-if="!loading && signals.length > 0 && activeSignals.length === 0" description="当前无有效信号（已离场/过期信号已隐藏）" />
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from "vue";
import { showToast } from "vant";
import { api } from "../api/http";
import { onWsEvent, startWs, wsState } from "../api/ws";
import { fmtClock, shortSymbol } from "../utils/format";
import SignalCard from "../components/SignalCard.vue";

const signals = ref([]);
const env = ref(null);
const macroSilence = ref(false);
const nextEvent = ref(null);
const reportTs = ref(0);
const reportCandidates = ref([]);
const launchWatchlist = ref([]);
const launchPool = ref([]);
const showLaunch = ref(false);
const reportSignalCount = ref(0);
const rejections = ref({});
const showPool = ref(false);   // 候选池币种列表默认折叠
const showRejections = ref(false);
const loading = ref(true);
const newestId = ref("");
let timer = null;

const rejectMap = computed(() => rejections.value);

// 过期/止损信号沉底：有效（含本地时间未到期）置顶，同级内按时间倒序
function sigRank(card) {
  if (card.status === "stopped_out" || card.status === "expired") return 2;
  if (Date.now() > (card.expires_at || 0)) return 2;
  return 1;
}
function sortSignals(list) {
  return [...list].sort((a, b) => sigRank(a) - sigRank(b) || b.created_at - a.created_at);
}
const activeSignals = computed(() => {
  // 只显示有效信号；并按 symbol+direction+strategy 收敛，只保留最新一条（去历史重复/堆积）
  const list = signals.value.filter((s) => sigRank(s) === 1);
  const seen = new Map();
  for (const s of [...list].sort((a, b) => b.created_at - a.created_at)) {
    const key = `${s.symbol}|${s.direction}|${s.strategy || "short"}`;
    if (!seen.has(key)) seen.set(key, s);
  }
  return [...seen.values()];
});
const rejectionList = computed(() =>
  Object.entries(rejections.value)
    .map(([symbol, reason]) => ({ symbol, reason }))
    .sort((a, b) => a.symbol.localeCompare(b.symbol)),
);

function mergeSignal(card) {
  signals.value = sortSignals([card, ...signals.value.filter((s) => s.id !== card.id)]).slice(0, 50);
  newestId.value = card.id;
  setTimeout(() => { if (newestId.value === card.id) newestId.value = ""; }, 5000);
}

function applyReport(data) {
  if (!data) return;
  if (data.ts) reportTs.value = data.ts;
  if (Array.isArray(data.candidates)) reportCandidates.value = data.candidates;
  if (Array.isArray(data.launch_watchlist)) launchWatchlist.value = data.launch_watchlist;
  if (Array.isArray(data.launch_pool)) launchPool.value = data.launch_pool;
  if (data.signal_count != null) reportSignalCount.value = data.signal_count;
  if (data.rejections) rejections.value = data.rejections;
  if (data.market_env) env.value = { ...data.market_env, ts: data.ts };
  macroSilence.value = !!data.macro_silence;
  const ne = data.next_macro_event;
  if (ne && ne.event_time) {
    nextEvent.value = {
      ...ne,
      minsAway: Math.round((new Date(ne.event_time).getTime() - Date.now()) / 60000),
    };
  } else {
    nextEvent.value = null;
  }
}

async function refresh() {
  try {
    const [sigList, st] = await Promise.all([api.signals("?limit=30"), api.status()]);
    signals.value = sortSignals(sigList);
    applyReport({
      ts: st.last_scan_ts,
      candidates: st.candidates,
      signal_count: 0,
      rejections: st.rejections,
      market_env: st.market_env,
      macro_silence: st.macro_silence,
      next_macro_event: st.next_macro_event,
      launch_watchlist: st.launch_watchlist,
      launch_pool: st.launch_pool,
    });
  } catch (e) {
    console.error("加载信号失败", e);
    if ((e.message || "").includes("401")) {
      showToast("未授权访问：请到设置页填写访问令牌（Token）");
    }
  } finally {
    loading.value = false;
  }
}

function handleWs(event, data) {
  if (event === "signal:new") {
    for (const s of data.signals || []) mergeSignal(s);
    try { navigator.vibrate?.(200); } catch (_) { /* ignore */ }
  } else if (event === "signal:update") {
    // 高频监控变更：按 id 合并实时价/状态，有效置顶、过期沉底
    const map = new Map(signals.value.map((s) => [s.id, s]));
    for (const s of data.signals || []) map.set(s.id, { ...map.get(s.id), ...s });
    signals.value = sortSignals([...map.values()]);
  } else if (event === "scan:report") {
    applyReport(data);
  }
}

onMounted(() => {
  refresh();
  startWs();
  onWsEvent(handleWs);
  timer = setInterval(() => {
    if (!document.hidden) refresh();
  }, 30000);
});
onUnmounted(() => clearInterval(timer));
</script>

<style scoped>
.env-panel {
  display: flex; justify-content: space-between; align-items: center;
  background: linear-gradient(135deg, #1c2b24, #1c1c1e);
  border-radius: 12px; padding: 12px; margin-bottom: 10px;
}
.env-main { display: flex; align-items: center; gap: 8px; }
.env-dot { width: 10px; height: 10px; border-radius: 50%; }
.env-dot.bull { background: #34c759; box-shadow: 0 0 8px #34c759; }
.env-dot.bear { background: #ff453a; box-shadow: 0 0 8px #ff453a; }
.env-dot.neutral { background: #ff9f1c; }
.env-text { font-size: 15px; font-weight: 700; }
.env-sub { font-size: 12px; color: #8e8e93; }
.env-right { display: flex; flex-direction: column; align-items: flex-end; gap: 2px; }
.conn { font-size: 11px; }
.conn.on { color: #34c759; }
.conn.off { color: #ff9f1c; }
.env-ts { font-size: 11px; color: #8e8e93; }
.macro-banner {
  display: flex; align-items: center; gap: 8px;
  background: rgba(255, 159, 28, 0.10); border: 1px solid rgba(255, 159, 28, 0.35);
  border-radius: 8px; padding: 8px 12px;
  font-size: 12px; color: #ffb84d; margin-bottom: 10px;
}
.macro-banner.silent {
  background: rgba(255, 69, 58, 0.10); border-color: rgba(255, 69, 58, 0.45);
  color: #ff6b5e; font-weight: 600;
}
.scan-report {
  display: flex; align-items: center; gap: 6px;
  background: #1c1c1e; border-radius: 8px; padding: 8px 12px;
  font-size: 12px; color: #8e8e93; margin-bottom: 10px;
}
.spin-icon { animation: spin 2s linear infinite; color: #34c759; }
@keyframes spin { to { transform: rotate(360deg); } }
.report-ts { margin-left: auto; }
.pool { background: #1c1c1e; border-radius: 12px; padding: 10px 12px; margin-bottom: 10px; }
.pool-head {
  display: flex; justify-content: space-between; align-items: center;
  font-size: 12px; color: #8e8e93; margin-bottom: 8px;
  cursor: pointer; user-select: none;
}
.pool-toggle { color: #ff9f1c; display: flex; align-items: center; gap: 2px; flex-shrink: 0; }
.pool-filter {
  display: flex; justify-content: center; align-items: center; gap: 4px;
  font-size: 12px; color: #ff9f1c; padding: 6px 0;
  background: rgba(255, 159, 28, 0.06); border-radius: 6px;
  margin-top: 2px; cursor: pointer; user-select: none;
}
.pool-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip {
  font-size: 12px; padding: 2px 8px; border-radius: 6px;
  background: #2c2c2e; color: #e8e8ea;
}
.chip.rejected { background: rgba(255, 159, 28, 0.1); color: #ff9f1c; }
.chip.watch { background: rgba(255, 159, 28, 0.16); color: #ffb84d; border: 1px solid rgba(255, 159, 28, 0.4); }
/* 启动感知池区块 */
.pool.launch-pool {
  border: 1px solid rgba(255, 159, 28, 0.28);
  background: linear-gradient(135deg, rgba(255, 159, 28, 0.06), #1c1c1e);
}
.launch-watch-line {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  font-size: 12px; margin: 6px 0;
}
.launch-watch-label { color: #ff9f1c; font-weight: 600; flex-shrink: 0; }
.launch-watch-line.dim { color: #8e8e93; }
.rejections {
  margin-top: 8px; border-top: 1px solid #2c2c2e; padding-top: 8px;
  max-height: 260px; overflow-y: auto;
}
.rej-row {
  display: flex; gap: 8px; align-items: baseline;
  font-size: 12px; padding: 3px 0;
}
.rej-symbol { color: #e8e8ea; font-weight: 600; min-width: 52px; }
.rej-reason { color: #8e8e93; }
.rej-empty { font-size: 12px; color: #8e8e93; text-align: center; padding: 8px 0; }
.signal-item.fresh { animation: flash 0.5s ease; }
@keyframes flash {
  0% { transform: scale(1.02); box-shadow: 0 0 0 2px rgba(52, 199, 89, 0.6); }
  100% { transform: scale(1); box-shadow: none; }
}
</style>
