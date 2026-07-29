<script setup lang="ts">
import { computed } from "vue";
import type { EChartsCoreOption } from "echarts/core";
import EChart from "./EChart.vue";

interface FactorMetric { horizon: number; mean_rank_ic: number | null; rank_icir: number | null; layer_returns: Record<string, number> }
interface Correlation { name: string; value: number }
const props = defineProps<{ metrics: FactorMetric[]; primary?: FactorMetric; correlations: Correlation[] }>();
const axis = { axisLine: { lineStyle: { color: "#b9c1bb" } }, axisLabel: { color: "#69716c", fontSize: 9 } };

const decayOption = computed<EChartsCoreOption>(() => ({
  animationDuration: 350,
  color: ["#006b5e", "#9a5b00"],
  tooltip: { trigger: "axis", valueFormatter: (value: unknown) => Number(value).toFixed(4) },
  legend: { top: 2, right: 4, textStyle: { fontSize: 9 } },
  grid: { left: 46, right: 20, top: 36, bottom: 32 },
  xAxis: { ...axis, type: "category", data: props.metrics.map((item) => `${item.horizon}日`) },
  yAxis: [{ ...axis, type: "value", name: "Rank IC", nameTextStyle: { fontSize: 9 } }, { ...axis, type: "value", name: "ICIR", nameTextStyle: { fontSize: 9 } }],
  series: [
    { name: "Rank IC", type: "bar", data: props.metrics.map((item) => item.mean_rank_ic), barMaxWidth: 34 },
    { name: "ICIR", type: "line", yAxisIndex: 1, symbolSize: 6, data: props.metrics.map((item) => item.rank_icir) },
  ],
}));
const layerEntries = computed(() => Object.entries(props.primary?.layer_returns || {}));
const layerOption = computed<EChartsCoreOption>(() => ({
  animationDuration: 350,
  tooltip: { trigger: "axis", valueFormatter: (value: unknown) => `${(Number(value) * 100).toFixed(2)}%` },
  grid: { left: 44, right: 16, top: 14, bottom: 34 },
  xAxis: { ...axis, type: "category", data: layerEntries.value.map(([name]) => `第${name}层`) },
  yAxis: { ...axis, type: "value", axisLabel: { formatter: (value: number) => `${(value * 100).toFixed(1)}%`, color: "#69716c", fontSize: 9 } },
  series: [{ type: "bar", data: layerEntries.value.map(([, value]) => ({ value, itemStyle: { color: value >= 0 ? "#006b5e" : "#a23c34" } })), barMaxWidth: 38 }],
}));
const correlationOption = computed<EChartsCoreOption>(() => ({
  animationDuration: 350,
  tooltip: { trigger: "axis", valueFormatter: (value: unknown) => Number(value).toFixed(3) },
  grid: { left: 110, right: 30, top: 12, bottom: 24 },
  xAxis: { ...axis, type: "value", min: -1, max: 1 },
  yAxis: { ...axis, type: "category", inverse: true, data: props.correlations.slice(0, 10).map((item) => item.name), axisLabel: { color: "#69716c", width: 92, overflow: "truncate", fontSize: 9 } },
  series: [{ type: "bar", data: props.correlations.slice(0, 10).map((item) => ({ value: item.value, itemStyle: { color: item.value >= 0 ? "#006b5e" : "#a23c34" } })), barMaxWidth: 18 }],
}));
</script>

<template>
  <div class="factor-analytics">
    <section><div class="section-heading"><div><span>IC DECAY</span><b>周期衰减</b></div></div><EChart :option="decayOption" :empty="!metrics.length" :height="260" ariaLabel="因子 Rank IC 和 ICIR 周期衰减图" /></section>
    <section><div class="section-heading"><div><span>LAYER RETURN</span><b>分层平均收益</b></div></div><EChart :option="layerOption" :empty="!layerEntries.length" :height="260" ariaLabel="因子分层平均收益柱状图" /></section>
    <section><div class="section-heading"><div><span>CORRELATION</span><b>相关性暴露</b></div></div><EChart :option="correlationOption" :empty="!correlations.length" :height="260" ariaLabel="因子相关性条形图" /></section>
  </div>
</template>

<style scoped>
.factor-analytics { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(240px, .75fr) minmax(260px, .8fr); gap: 22px; }
.factor-analytics > section { min-width: 0; }
@media (max-width: 1100px) { .factor-analytics { grid-template-columns: 1fr 1fr; } .factor-analytics > section:last-child { grid-column: 1 / -1; } }
@media (max-width: 700px) { .factor-analytics { grid-template-columns: 1fr; } .factor-analytics > section:last-child { grid-column: auto; } }
</style>
