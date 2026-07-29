<script setup lang="ts">
import { computed } from "vue";
import type { EChartsCoreOption } from "echarts/core";
import type { ChartSeries } from "@/api/types";
import EChart from "./EChart.vue";

const props = defineProps<{ series: ChartSeries[]; valueLabel: string }>();
const labels = computed(() => [...new Set(props.series.flatMap((item) => item.points.map((point) => point.label)))].sort());
const option = computed<EChartsCoreOption>(() => ({
  animationDuration: 350,
  color: props.series.map((item) => item.color),
  tooltip: {
    trigger: "axis",
    valueFormatter: (value: unknown) => {
      if (value === null || value === undefined || value === "") return "—";
      const numeric = Number(value);
      return Number.isFinite(numeric) ? numeric.toFixed(4) : "—";
    },
  },
  legend: { type: "scroll", top: 2, left: 4, right: 4, textStyle: { fontSize: 9 } },
  grid: { left: 48, right: 24, top: 42, bottom: 50 },
  dataZoom: [{ type: "inside" }, { type: "slider", bottom: 7, height: 17, borderColor: "#d9ded9", fillerColor: "rgba(0,107,94,.12)" }],
  xAxis: { type: "category", boundaryGap: false, data: labels.value, axisLabel: { color: "#69716c", fontSize: 9 }, axisLine: { lineStyle: { color: "#b9c1bb" } } },
  yAxis: { type: "value", scale: true, axisLabel: { color: "#69716c", fontSize: 9 }, splitLine: { lineStyle: { color: "#edf0eb" } } },
  series: props.series.map((item) => {
    const values = new Map(item.points.map((point) => [point.label, point.value]));
    return {
      name: item.name,
      type: "line",
      showSymbol: true,
      symbolSize: 4,
      connectNulls: false,
      data: labels.value.map((label) => values.get(label) ?? null),
      lineStyle: { width: item.name.includes("账户") ? 2.5 : 1.3 },
    };
  }),
}));
</script>

<template>
  <figure class="multi-chart">
    <EChart :option="option" :empty="!series.some((item) => item.points.length)" :height="320" :ariaLabel="`${valueLabel}，${series.length} 条曲线`" />
    <figcaption v-if="series.length"><span v-for="item in series" :key="item.name"><i :style="{background:item.color}" />{{ item.name }}</span></figcaption>
    <p v-if="!series.some((item) => item.points.length)" class="sr-only">暂无可比较的历史曲线</p>
  </figure>
</template>

<style scoped>
.multi-chart { margin: 0; min-width: 0; border: 1px solid var(--line); background: #f8faf6; }
.multi-chart figcaption { display: flex; flex-wrap: wrap; gap: 12px; padding: 0 14px 12px; font-size: 9px; }
.multi-chart figcaption span { display: inline-flex; align-items: center; gap: 5px; }
.multi-chart figcaption i { width: 14px; height: 3px; }
</style>
