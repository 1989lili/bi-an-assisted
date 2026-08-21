<template>
  <div class="review">
    <div class="toolbar">
      <span class="title">信号复盘</span>
      <van-tabs v-model:active="tab" shrink class="tabs">
        <van-tab title="全部" />
        <van-tab title="做多" />
        <van-tab title="做空" />
      </van-tabs>
    </div>

    <van-empty v-if="!loading && filtered.length === 0" description="暂无信号记录" />

    <!-- 统计条 -->
    <div class="stats" v-if="filtered.length">
      <div class="stat">
        <div class="sv">{{ filtered.length }}</div>
        <div class="sk">信号数</div>
      </div>
      <div class="stat">
        <div class="sv ok">{{ highCount }}</div>
        <div class="sk">强信号(≥70)</div>
      </div>
      <div class="stat">
        <div class="sv warn">{{ aCount }}</div>
        <div class="sk">A级扳机</div>
      </div>
      <div class="stat">
        <div class="sv">{{ expiredCount }}</div>
        <div class="sk">已过期</div>
      </div>
    </div>

    <!-- 信号历史列表（轻量） -->
    <van-cell-group inset v-for="s in filtered" :key="s.id" class="row">
      <van-cell
        :title="`${s.direction === 'long' ? '做多' : '做空'} ${shortSymbol(s.symbol)}`"
        :label="`${fmtTime(s.created_at)} · ${s.trigger_level || ''}级 · ${s.reason ? s.reason.slice(0, 40) : ''}`"
      >
        <template #value>
          <span class="conf" :class="s.confidence >= 70 ? 'high' : 'mid'">{{ s.confidence }}</span>
          <span class="st" :class="s.status">{{ statusText(s) }}</span>
        </template>
      </van-cell>
    </van-cell-group>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { api } from "../api/http";
import { fmtTime, shortSymbol } from "../utils/format";

const signals = ref([]);
const loading = ref(true);
const tab = ref(0);

const filtered = computed(() => {
  if (tab.value === 0) return signals.value;
  const dir = tab.value === 1 ? "long" : "short";
  return signals.value.filter((s) => s.direction === dir);
});

const highCount = computed(() => signals.value.filter((s) => s.confidence >= 70).length);
const aCount = computed(() => signals.value.filter((s) => s.trigger_level === "A").length);
const expiredCount = computed(() => {
  const now = Date.now();
  return signals.value.filter((s) => !s.expires_at || s.expires_at < now).length;
});

function statusText(s) {
  const now = Date.now();
  if (!s.expires_at || s.expires_at < now) return "已过期";
  return s.status === "confirmed" ? "已确认" : "生效中";
}

onMounted(async () => {
  try {
    signals.value = await api.signals("?limit=100");
  } catch (e) {
    console.error("加载复盘失败", e);
  } finally {
    loading.value = false;
  }
});
</script>

<style scoped>
.toolbar { margin-bottom: 10px; }
.title { font-size: 18px; font-weight: 700; display: block; margin-bottom: 8px; }
.tabs { --van-tabs-bottom-bar-color: #34c759; }
.stats {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 10px;
}
.stat { background: #1c1c1e; border-radius: 10px; padding: 10px 0; text-align: center; }
.sv { font-size: 18px; font-weight: 800; }
.sv.ok { color: #34c759; }
.sv.warn { color: #ff9f1c; }
.sk { font-size: 11px; color: #8e8e93; }
.row { margin-bottom: 8px; border-radius: 10px; overflow: hidden; }
.conf { font-weight: 800; margin-right: 6px; }
.conf.high { color: #34c759; }
.conf.mid { color: #ff9f1c; }
.st { font-size: 11px; padding: 1px 6px; border-radius: 4px; background: #2c2c2e; color: #8e8e93; }
.st.expired { color: #8e8e93; }
</style>
