<script setup lang="ts">
import { computed } from "vue";
import type { ChartSeries } from "@/api/types";

const props = defineProps<{ series: ChartSeries[]; valueLabel: string }>();
const width = 900, height = 280, pad = 30;
const labels = computed(() => [...new Set(props.series.flatMap((item) => item.points.map((point) => point.label)))].sort());
const values = computed(() => props.series.flatMap((item) => item.points.map((point) => Number(point.value))).filter(Number.isFinite));
const extent = computed(() => {
  const min = Math.min(...values.value, 1), max = Math.max(...values.value, 1);
  const spread = Math.max(max - min, 0.02);
  return { min: min - spread * .1, max: max + spread * .1 };
});
const paths = computed(() => props.series.map((item) => {
  const lookup = new Map(item.points.map((point) => [point.label, Number(point.value)]));
  let drawing = false;
  const d = labels.value.map((label, index) => {
    const value = lookup.get(label);
    if (!Number.isFinite(value)) { drawing = false; return ""; }
    const x = pad + index * ((width - pad * 2) / Math.max(labels.value.length - 1, 1));
    const y = height - pad - ((Number(value) - extent.value.min) / (extent.value.max - extent.value.min)) * (height - pad * 2);
    const command = drawing ? "L" : "M"; drawing = true;
    return `${command}${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(" ");
  return { ...item, d };
}));
const baselineY = computed(() => height - pad - ((1 - extent.value.min) / (extent.value.max - extent.value.min)) * (height - pad * 2));
</script>

<template>
  <figure class="multi-chart" role="img" :aria-label="`${valueLabel}，${series.length} 条曲线`">
    <svg v-if="values.length" :viewBox="`0 0 ${width} ${height}`" preserveAspectRatio="none">
      <line :x1="pad" :x2="width-pad" :y1="baselineY" :y2="baselineY" class="chart-baseline" />
      <path v-for="item in paths" :key="item.name" :d="item.d" class="chart-line" :style="{stroke:item.color}" />
    </svg>
    <p v-else class="muted">暂无可比较的历史曲线</p>
    <figcaption><span v-for="item in series" :key="item.name"><i :style="{background:item.color}" />{{ item.name }}</span></figcaption>
  </figure>
</template>
