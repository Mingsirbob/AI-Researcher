<script setup lang="ts">
import { computed } from "vue";
import type { LinePoint } from "@/api/types";

const props = withDefaults(defineProps<{ points: LinePoint[]; baseline?: number; valueLabel?: string }>(), {
  baseline: 1,
  valueLabel: "净值",
});
const width = 900, height = 260, pad = 28;
const valid = computed(() => props.points.filter((point) => Number.isFinite(Number(point.value))));
const values = computed(() => valid.value.map((point) => Number(point.value)));
const extent = computed(() => {
  const all = [...values.value, props.baseline];
  const min = Math.min(...all), max = Math.max(...all);
  const spread = Math.max(max - min, Math.abs(max) * 0.02, 0.01);
  return { min: min - spread * 0.12, max: max + spread * 0.12 };
});
const path = computed(() => valid.value.map((point, index) => {
  const x = pad + index * ((width - pad * 2) / Math.max(valid.value.length - 1, 1));
  const y = height - pad - ((Number(point.value) - extent.value.min) / (extent.value.max - extent.value.min)) * (height - pad * 2);
  return `${index ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)}`;
}).join(" "));
const baselineY = computed(() => height - pad - ((props.baseline - extent.value.min) / (extent.value.max - extent.value.min)) * (height - pad * 2));
</script>

<template>
  <div class="line-chart" role="img" :aria-label="`${valueLabel}走势，共 ${valid.length} 个数据点`">
    <svg v-if="valid.length" :viewBox="`0 0 ${width} ${height}`" preserveAspectRatio="none">
      <line :x1="pad" :x2="width-pad" :y1="baselineY" :y2="baselineY" class="chart-baseline" />
      <path :d="path" class="chart-line" />
    </svg>
    <p v-else class="muted">暂无可绘制数据</p>
    <footer v-if="valid.length"><span>{{ valid[0]?.label }}</span><b>{{ valueLabel }} {{ Number(valid.at(-1)?.value).toFixed(4) }}</b><span>{{ valid.at(-1)?.label }}</span></footer>
  </div>
</template>
