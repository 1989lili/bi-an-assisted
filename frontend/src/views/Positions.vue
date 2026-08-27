<template>
  <div class="positions">
    <div class="toolbar">
      <span class="title">持仓管理</span>
      <van-button size="small" type="primary" plain icon="plus" @click="showForm = true">
        录入开仓
      </van-button>
    </div>

    <van-empty v-if="!loading && positions.length === 0" description="暂无持仓" />

    <!-- 持仓卡片 -->
    <div v-for="p in positions" :key="p.id" class="pos-card" :class="p.direction">
      <div class="pos-head">
        <span class="dir-badge" :class="p.direction">
          {{ p.direction === "long" ? "多" : "空" }}
        </span>
        <span class="pos-symbol">{{ shortSymbol(p.symbol) }}</span>
        <span class="pos-pnl" :class="pnlClass(p)">{{ fmtNum(p.pnl_pct, 2) }}%</span>
        <van-button size="mini" plain type="danger" class="close-btn" @click="closePos(p)">
          平仓
        </van-button>
      </div>

      <div class="pos-metrics">
        <div class="m">
          <div class="mk">开仓价</div>
          <div class="mv">{{ fmtNum(p.entry_price) }}</div>
        </div>
        <div class="m">
          <div class="mk">杠杆</div>
          <div class="mv">{{ p.leverage ? p.leverage + "x" : "-" }}</div>
        </div>
        <div class="m">
          <div class="mk">止损阶段</div>
          <div class="mv stage">{{ stageText(p) }}</div>
        </div>
        <div class="m">
          <div class="mk">当前止损</div>
          <div class="mv" :class="stopClass(p)">{{ fmtNum(p.stop_price) }}</div>
        </div>
        <div class="m">
          <div class="mk">建议</div>
          <div class="mv action" :class="p.action">{{ actionText(p) }}</div>
        </div>
      </div>
      <div class="pos-reason" v-if="p.action_reason && p.action_reason !== '无动作'">
        {{ p.action_reason }}
      </div>
    </div>

    <!-- 录入开仓 -->
    <van-popup v-model:show="showForm" position="bottom" round class="form-popup">
      <div class="form-title">录入开仓</div>
      <van-cell-group inset>
        <van-field v-model="form.symbol" label="币种" placeholder="如 BTC/USDT:USDT" />
        <van-field v-model="form.entry" label="开仓价" type="number" placeholder="实际成交均价" />
        <van-field v-model="form.qty" label="数量" type="number" placeholder="合约数量" />
        <van-field name="direction" label="方向">
          <template #input>
            <van-radio-group v-model="form.direction" direction="horizontal">
              <van-radio name="long">做多</van-radio>
              <van-radio name="short">做空</van-radio>
            </van-radio-group>
          </template>
        </van-field>
      </van-cell-group>
      <div class="form-actions">
        <van-button block type="primary" @click="submitForm">保存</van-button>
      </div>
    </van-popup>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from "vue";
import { showToast } from "vant";
import { api } from "../api/http";
import { onWsEvent, startWs } from "../api/ws";
import { fmtNum, shortSymbol } from "../utils/format";

const positions = ref([]);
const loading = ref(true);
const showForm = ref(false);
const form = reactive({ symbol: "", entry: "", qty: "", direction: "long" });

async function load() {
  loading.value = true;
  try {
    const list = await api.positions("open");
    // 逐个拉实时状态（止损阶段/浮盈/建议）
    const snaps = await Promise.allSettled(list.map((p) => api.positionStatus(p.id)));
    positions.value = list.map((p, i) => {
      const s = snaps[i].status === "fulfilled" ? snaps[i].value : {};
      return { ...p, ...s };
    });
  } catch (e) {
    console.error("加载持仓失败", e);
  } finally {
    loading.value = false;
  }
}

async function submitForm() {
  if (!form.symbol || !form.entry || !form.qty) {
    showToast("请填写完整");
    return;
  }
  try {
    await api.createPosition({
      symbol: form.symbol.trim(),
      direction: form.direction,
      entry_price: Number(form.entry),
      qty: Number(form.qty),
    });
    showForm.value = false;
    Object.assign(form, { symbol: "", entry: "", qty: "", direction: "long" });
    await load();
  } catch (e) {
    showToast(e.message);
  }
}

async function closePos(p) {
  try {
    await api.closePosition(p.id);
    showToast("已平仓");
    await load();
  } catch (e) {
    showToast(e.message);
  }
}

function pnlClass(p) {
  const v = Number(p.pnl_pct) || 0;
  return v >= 0 ? "ok" : "bad";
}
function stageText(p) {
  if (p.stage === 2) return "保本";
  if (p.stage === 3) return "EMA21跟踪";
  return "初始";
}
function stopClass(p) {
  return p.action === "move_stop" ? "warn" : "";
}
function actionText(p) {
  if (!p.action || p.action === "hold") return "持有";
  if (p.action === "move_stop") return "移止损↑";
  if (p.action === "exit") return "离场!";
  return p.action;
}

function handleWs(event) {
  // 自动平仓等变更 → 实时刷新持仓列表
  if (event === "position:update") load();
}

onMounted(() => {
  startWs();
  onWsEvent(handleWs);
  load();
});
</script>

<style scoped>
.toolbar {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 10px; padding: 0 4px;
}
.title { font-size: 18px; font-weight: 700; }
.pos-card {
  background: #1c1c1e; border-radius: 12px; padding: 12px; margin-bottom: 10px;
  border-left: 4px solid #34c759;
}
.pos-card.short { border-left-color: #ff453a; }
.pos-head { display: flex; align-items: center; gap: 8px; }
.dir-badge {
  padding: 2px 8px; border-radius: 6px; font-size: 12px; font-weight: 600;
}
.dir-badge.long { background: rgba(52, 199, 89, 0.18); color: #34c759; }
.dir-badge.short { background: rgba(255, 69, 58, 0.18); color: #ff453a; }
.pos-symbol { font-size: 16px; font-weight: 700; }
.pos-pnl { margin-left: auto; font-size: 16px; font-weight: 700; }
.pos-pnl.ok { color: #34c759; }
.pos-pnl.bad { color: #ff453a; }
.close-btn { margin-left: 8px; }
.pos-metrics {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(82px, 1fr)); gap: 6px; margin-top: 10px;
}
.m { background: #2c2c2e; border-radius: 8px; padding: 6px 8px; }
.mk { font-size: 11px; color: #8e8e93; }
.mv { font-size: 13px; font-weight: 600; }
.mv.stage { color: #ff9f1c; }
.mv.warn { color: #ff9f1c; }
.mv.action.exit { color: #ff453a; }
.mv.action.move_stop { color: #ff9f1c; }
.pos-reason {
  margin-top: 8px; font-size: 12px; color: #ff9f1c;
  background: rgba(255, 159, 28, 0.08); border-radius: 6px; padding: 6px 8px;
}
.form-popup { background: #1c1c1e; padding-bottom: 20px; }
.form-title {
  text-align: center; font-size: 16px; font-weight: 700; padding: 14px 0 6px;
}
.form-actions { padding: 12px 16px; }
</style>
