<template>
  <div class="settings">
    <div class="toolbar"><span class="title">设置</span></div>

    <!-- 宏观日历 -->
    <div class="section">
      <div class="sec-head">
        <span>宏观静默期日历</span>
        <van-button size="mini" type="primary" plain icon="plus" @click="showMacro = true">
          添加事件
        </van-button>
      </div>
      <van-empty v-if="macroEvents.length === 0" description="暂无事件（CPI/非农/FOMC 前后 15 分钟暂停开仓）" image-size="60" />
      <van-cell-group inset v-for="e in macroEvents" :key="e.id" class="row">
        <van-cell :title="e.title" :label="fmtEventTime(e.event_time)">
          <template #right-icon>
            <van-icon name="cross" class="del" @click="removeMacro(e.id)" />
          </template>
        </van-cell>
      </van-cell-group>
    </div>

    <!-- 引擎参数 -->
    <div class="section">
      <div class="sec-head"><span>引擎参数</span></div>
      <van-cell-group inset>
        <van-cell
          v-for="(val, key) in editableParams"
          :key="key"
          :title="key"
          :value="String(val)"
          is-link
          @click="editParam(key, val)"
        />
      </van-cell-group>
    </div>

    <!-- 关于 -->
    <div class="section about">
      <div>币安 U 本位合约辅助决策工具 v0.3.0</div>
      <div class="sub">决策辅助 · 不自动下单 · 本地运行</div>
      <div class="conn-line">
        WS: <span :class="wsState.connected ? 'ok' : 'warn'">
          {{ wsState.connected ? "已连接" : "重连中" }}
        </span>
        · 服务: {{ serverOk ? "正常" : "异常" }}
      </div>
    </div>

    <!-- 添加宏观事件 -->
    <van-popup v-model:show="showMacro" position="bottom" round class="form-popup">
      <div class="form-title">添加宏观事件</div>
      <van-cell-group inset>
        <van-field v-model="macroForm.title" label="事件" placeholder="如 CPI / FOMC / 非农" />
        <van-field label="时间">
          <template #input>
            <input v-model="macroForm.time" type="datetime-local" class="dt-input" />
          </template>
        </van-field>
      </van-cell-group>
      <div class="form-actions">
        <van-button block type="primary" @click="submitMacro">保存</van-button>
      </div>
    </van-popup>

    <!-- 参数编辑 -->
    <van-dialog v-model:show="showParam" :title="editingKey" show-cancel-button @confirm="saveParam">
      <van-field v-model="editingValue" type="text" class="add-input" />
    </van-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from "vue";
import { showToast } from "vant";
import { api } from "../api/http";
import { onWsEvent, startWs, wsState } from "../api/ws";

const macroEvents = ref([]);
const settings = ref({});
const serverOk = ref(true);
const showMacro = ref(false);
const macroForm = reactive({ title: "", time: "" });
const showParam = ref(false);
const editingKey = ref("");
const editingValue = ref("");

// 设置页只展示可调的关键参数（排除路径/内部项）
const EDITABLE_KEYS = [
  "ADX_TREND_TH", "TRIGGER_MOMENTUM_BARS", "VOL_RATIO_VETO", "VOL_RATIO_HOT", "VOL_RATIO_LOW",
  "OI_GROWTH_VETO", "FUNDING_NORMAL_MAX", "FUNDING_STABLE_MAX",
  "FUNDING_SURGE_TIMES", "FUNDING_STABLE_FLUCT", "FUNDING_POSITION_FACTOR",
  "BW_NARROW_FACTOR", "BW_WIDE_FACTOR", "ATR_COEF_NARROW", "ATR_COEF_NORMAL",
  "ATR_COEF_WIDE", "MIN_RISK_REWARD", "RISK_PER_TRADE", "EXEC_MARKET_PCT",
  "EXEC_LIMIT_PCT", "EXEC_LIMIT_TTL_BARS", "SIGNAL_TTL_BARS", "SL_INIT_COEF",
  "BE_PROFIT_ATR", "TRAIL_PROFIT_ATR", "MACRO_SILENCE_MINUTES",
];

const editableParams = computed(() => {
  const out = {};
  for (const k of EDITABLE_KEYS) {
    if (k in settings.value) out[k] = settings.value[k];
  }
  return out;
});

function fmtEventTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function load() {
  try {
    const [evts, st] = await Promise.all([api.macroEvents(), api.settings()]);
    macroEvents.value = evts;
    settings.value = st;
    serverOk.value = true;
  } catch (e) {
    serverOk.value = false;
  }
}

async function submitMacro() {
  if (!macroForm.title || !macroForm.time) {
    showToast("请填写完整");
    return;
  }
  try {
    await api.addMacroEvent({ title: macroForm.title, event_time: new Date(macroForm.time).toISOString() });
    showMacro.value = false;
    Object.assign(macroForm, { title: "", time: "" });
    await load();
  } catch (e) {
    showToast(e.message);
  }
}

async function removeMacro(id) {
  await api.removeMacroEvent(id);
  macroEvents.value = macroEvents.value.filter((e) => e.id !== id);
}

function editParam(key, val) {
  editingKey.value = key;
  editingValue.value = String(val);
  showParam.value = true;
}

async function saveParam() {
  try {
    const v = Number(editingValue.value);
    await api.updateSetting(editingKey.value, Number.isNaN(v) ? editingValue.value : v);
    showToast("已更新（重启恢复默认）");
    await load();
  } catch (e) {
    showToast(e.message);
  }
}

onMounted(() => {
  load();
  startWs();
  onWsEvent(() => {});
});
</script>

<style scoped>
.toolbar { margin-bottom: 10px; }
.title { font-size: 18px; font-weight: 700; }
.section { margin-bottom: 14px; }
.sec-head {
  display: flex; justify-content: space-between; align-items: center;
  font-size: 14px; font-weight: 600; color: #8e8e93;
  padding: 0 4px 6px;
}
.row { margin-bottom: 6px; border-radius: 8px; overflow: hidden; }
.del { color: #ff453a; padding: 0 8px; }
.about {
  background: #1c1c1e; border-radius: 12px; padding: 14px; text-align: center;
  font-size: 13px; color: #e8e8ea;
}
.about .sub { font-size: 11px; color: #8e8e93; margin-top: 4px; }
.conn-line { font-size: 12px; color: #8e8e93; margin-top: 8px; }
.conn-line .ok { color: #34c759; }
.conn-line .warn { color: #ff9f1c; }
.form-popup { background: #1c1c1e; padding-bottom: 20px; }
.form-title {
  text-align: center; font-size: 16px; font-weight: 700; padding: 14px 0 6px;
}
.form-actions { padding: 12px 16px; }
.dt-input {
  background: transparent; border: none; color: #e8e8ea;
  font-size: 14px; width: 100%; outline: none;
}
.add-input { background: #2c2c2e; border-radius: 8px; margin: 8px; }
</style>
