<template>
  <div class="watch">
    <div class="toolbar">
      <span class="title">自选币</span>
      <van-button size="small" type="primary" plain icon="plus" @click="showAdd = true">
        添加
      </van-button>
    </div>

    <van-empty v-if="!loading && symbols.length === 0" description="暂无自选，点击右上角添加" />

    <van-cell-group inset v-for="s in rows" :key="s.symbol" class="watch-row">
      <van-cell
        :title="shortSymbol(s.symbol)"
        :label="rowLabel(s)"
        is-link
        @click="toggle(s)"
      >
        <template #value>
          <span class="price" v-if="s.snap">{{ fmtNum(s.snap['15m']?.close, s.snap['15m']?.close >= 100 ? 0 : 2) }}</span>
          <span class="price loading" v-else-if="s.loading">加载中…</span>
        </template>
      </van-cell>

      <!-- 展开详情：11 指标读数 -->
      <div v-if="s.expanded && s.snap" class="detail">
        <div class="grid">
          <div class="grid-item">
            <div class="k">4h 趋势</div>
            <div class="v" :class="trendClass(s.snap['4h'])">
              {{ s.snap['4h']?.structure || "-" }}
              <span class="sub">{{ s.snap['4h']?.above_ema55 ? "EMA55上" : "EMA55下" }}</span>
            </div>
          </div>
          <div class="grid-item">
            <div class="k">1h 趋势</div>
            <div class="v" :class="trendClass(s.snap['1h'])">
              {{ s.snap['1h']?.structure || "-" }}
            </div>
          </div>
          <div class="grid-item">
            <div class="k">ADX(4h)</div>
            <div class="v" :class="(s.snap['4h']?.adx || 0) > 25 ? 'ok' : ''">
              {{ fmtNum(s.snap['4h']?.adx, 0) }}
            </div>
          </div>
          <div class="grid-item">
            <div class="k">RSI(15m)</div>
            <div class="v" :class="rsiClass(s.snap['15m']?.rsi)">
              {{ fmtNum(s.snap['15m']?.rsi, 0) }}
            </div>
          </div>
          <div class="grid-item">
            <div class="k">KDJ 温度</div>
            <div class="v">{{ kdjTemp(s.snap['15m']?.kdj) }}</div>
          </div>
          <div class="grid-item">
            <div class="k">量比</div>
            <div class="v" :class="volClass(s.snap['15m']?.volume_ratio)">
              {{ fmtNum(s.snap['15m']?.volume_ratio, 1) }}
            </div>
          </div>
          <div class="grid-item">
            <div class="k">ATR(15m)</div>
            <div class="v">{{ fmtNum(s.snap['15m']?.atr) }}</div>
          </div>
          <div class="grid-item">
            <div class="k">波动系数</div>
            <div class="v">{{ s.coef ?? "-" }}</div>
          </div>
          <div class="grid-item">
            <div class="k">费率档位</div>
            <div class="v" :style="{ color: FUNDING_TIER[s.funding?.tier]?.color }">
              {{ FUNDING_TIER[s.funding?.tier]?.text || "-" }}
            </div>
          </div>
          <div class="grid-item">
            <div class="k">OI 变化率</div>
            <div class="v" :class="oiClass(s.oi_change)">
              {{ s.oi_change != null ? (s.oi_change * 100).toFixed(2) + "%" : "-" }}
            </div>
          </div>
          <div class="grid-item" v-if="s.liq_dist != null">
            <div class="k">清算距离</div>
            <div class="v warn">{{ fmtNum(s.liq_dist) }}</div>
          </div>
          <div class="grid-item">
            <div class="k">MACD(1h)</div>
            <div class="v" :class="(s.snap['1h']?.macd_above_zero) ? 'ok' : ''">
              {{ fmtNum(s.snap['1h']?.macd_dif, 3) }}
            </div>
          </div>
        </div>
        <div class="row-actions">
          <van-button size="mini" plain type="danger" @click.stop="remove(s.symbol)">移出自选</van-button>
        </div>
      </div>
    </van-cell-group>

    <!-- 添加自选 -->
    <van-dialog v-model:show="showAdd" title="添加自选币" show-cancel-button @confirm="addSymbol">
      <van-field v-model="newSymbol" placeholder="输入交易对，如 SOL/USDT:USDT" class="add-input" />
    </van-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { showToast } from "vant";
import { api } from "../api/http";
import { fmtNum, shortSymbol, FUNDING_TIER } from "../utils/format";

const symbols = ref([]);
const rows = ref([]);
const loading = ref(true);
const showAdd = ref(false);
const newSymbol = ref("");

const loadingRows = computed(() => rows.value.filter((r) => r.loading).length);

async function loadWatchlist() {
  loading.value = true;
  try {
    const data = await api.watchlist();
    symbols.value = data.symbols;
    rows.value = data.symbols.map((sym) => ({ symbol: sym, expanded: false, loading: false, snap: null }));
    // 并行拉前 10 个快照（后端 fetcher TTL 缓存兜底）
    await Promise.allSettled(rows.value.slice(0, 10).map((r) => loadSnap(r)));
  } catch (e) {
    console.error("加载自选失败", e);
  } finally {
    loading.value = false;
  }
}

async function loadSnap(row) {
  row.loading = true;
  try {
    const data = await api.symbolSnapshot(row.symbol);
    row.snap = data.snap;
    row.funding = data.funding;
    row.oi_change = data.oi_change;
    row.coef = data.volatility_coef;
    row.liq_dist = data.liq_dist;
  } catch (e) {
    console.error(`快照失败 ${row.symbol}`, e);
  } finally {
    row.loading = false;
  }
}

function toggle(row) {
  row.expanded = !row.expanded;
  if (row.expanded && !row.snap && !row.loading) loadSnap(row);
}

async function addSymbol() {
  const sym = newSymbol.value.trim();
  if (!sym) return;
  try {
    await api.addWatch(sym);
    newSymbol.value = "";
    showAdd.value = false;
    await loadWatchlist();
  } catch (e) {
    showToast(e.message);
  }
}

async function remove(symbol) {
  await api.removeWatch(symbol);
  rows.value = rows.value.filter((r) => r.symbol !== symbol);
  symbols.value = symbols.value.filter((s) => s !== symbol);
}

function rowLabel(row) {
  if (!row.snap) return "点击加载指标";
  const s4h = row.snap["4h"] || {};
  const s15 = row.snap["15m"] || {};
  return `4h ${s4h.structure || "-"} · 量比 ${fmtNum(s15.volume_ratio, 1)} · ${FUNDING_TIER[row.funding?.tier]?.text || "费率-"}`;
}

function trendClass(s) {
  if (!s) return "";
  return s.structure === "uptrend" ? "ok" : s.structure === "downtrend" ? "bad" : "";
}
function rsiClass(v) {
  if (v == null) return "";
  return v > 70 ? "warn" : v < 30 ? "ok" : "";
}
function volClass(v) {
  if (v == null) return "";
  return v > 1.5 ? "ok" : v < 0.7 ? "warn" : "";
}
function oiClass(v) {
  if (v == null) return "";
  return v > 0.01 ? "ok" : v < 0 ? "bad" : "";
}
function kdjTemp(kdj) {
  if (!kdj) return "-";
  const j = kdj.j;
  if (j >= 100) return "🔥超买";
  if (j > 80) return "偏热";
  if (j <= 0) return "❄️超卖";
  if (j < 20) return "偏冷";
  return "中性";
}

onMounted(loadWatchlist);
</script>

<style scoped>
.toolbar {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 10px; padding: 0 4px;
}
.title { font-size: 18px; font-weight: 700; }
.watch-row { margin-bottom: 8px; border-radius: 12px; overflow: hidden; }
.price { font-weight: 600; color: #e8e8ea; }
.price.loading { color: #8e8e93; font-size: 12px; font-weight: 400; }
.detail { background: #1c1c1e; padding: 10px 12px 12px; }
.grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.grid-item {
  background: #2c2c2e; border-radius: 8px; padding: 6px 8px;
}
.k { font-size: 11px; color: #8e8e93; margin-bottom: 2px; }
.v { font-size: 14px; font-weight: 600; }
.v.sub { font-size: 11px; color: #8e8e93; }
.v.ok { color: #34c759; }
.v.bad { color: #ff453a; }
.v.warn { color: #ff9f1c; }
.row-actions { margin-top: 10px; text-align: right; }
.add-input { background: #2c2c2e; border-radius: 8px; margin: 8px; }
</style>
