<script setup lang="ts">
import { computed } from "vue";
import type { EChartsCoreOption } from "echarts/core";
import EChart from "./EChart.vue";

interface NavPoint { trading_date: string; nav: number }
const props = defineProps<{ points: NavPoint[] }>();
const chartData = computed(() => {
  let peak = 0;
  return props.points.filter((item) => Number.isFinite(Number(item.nav))).map((item) => {
    const nav = Number(item.nav);
    peak = Math.max(peak, nav);
    return { date: item.trading_date, nav, drawdown: peak ? nav / peak - 1 : 0 };
  });
});
const option = computed<EChartsCoreOption>(() => ({
  animationDuration: 350,
  color: ["#006b5e", "#a23c34"],
  tooltip: { trigger: "axis", valueFormatter: (value: unknown) => Number(value).toFixed(4) },
  legend: { top: 2, right: 6, textStyle: { fontSize: 9 } },
  grid: { left: 48, right: 48, top: 38, bottom: 52 },
  dataZoom: [{ type: "inside", start: Math.max(0, 100 - 250 / Math.max(chartData.value.length, 1) * 100) }, { type: "slider", height: 18, bottom: 8, borderColor: "#d9ded9", fillerColor: "rgba(0,107,94,.12)" }],
  xAxis: { type: "category", boundaryGap: false, data: chartData.value.map((item) => item.date), axisLabel: { color: "#69716c", fontSize: 9 }, axisLine: { lineStyle: { color: "#b9c1bb" } } },
  yAxis: [
    { type: "value", name: "净值", scale: true, nameTextStyle: { fontSize: 9 }, axisLabel: { color: "#69716c", fontSize: 9 }, splitLine: { lineStyle: { color: "#edf0eb" } } },
    { type: "value", name: "回撤", max: 0, axisLabel: { formatter: (value: number) => `${(value * 100).toFixed(0)}%`, color: "#69716c", fontSize: 9 }, splitLine: { show: false } },
  ],
  series: [
    { name: "策略净值", type: "line", showSymbol: false, data: chartData.value.map((item) => item.nav), lineStyle: { width: 2 }, markLine: { silent: true, symbol: "none", lineStyle: { color: "#b9c1bb", type: "dashed" }, data: [{ yAxis: 1 }] } },
    { name: "回撤", type: "line", yAxisIndex: 1, showSymbol: false, data: chartData.value.map((item) => item.drawdown), lineStyle: { width: 1 }, areaStyle: { opacity: .12 } },
  ],
}));
</script>

<template><EChart :option="option" :empty="!chartData.length" :height="340" ariaLabel="扣费后策略净值与逐日回撤图" /></template>
