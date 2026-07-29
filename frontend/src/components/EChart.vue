<script setup lang="ts">
import { computed } from "vue";
import VChart from "vue-echarts";
import { use } from "echarts/core";
import { BarChart, HeatmapChart, LineChart } from "echarts/charts";
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
  VisualMapComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { EChartsCoreOption } from "echarts/core";

use([
  BarChart,
  HeatmapChart,
  LineChart,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
  VisualMapComponent,
  CanvasRenderer,
]);

const props = withDefaults(defineProps<{
  option: EChartsCoreOption;
  height?: number;
  ariaLabel: string;
  empty?: boolean;
}>(), { height: 300, empty: false });

const style = computed(() => ({ height: `${props.height}px` }));
</script>

<template>
  <div class="analytics-chart" role="img" :aria-label="ariaLabel" :style="style">
    <VChart v-if="!empty" :option="option" autoresize />
    <p v-else class="muted">暂无可绘制数据</p>
  </div>
</template>

<style scoped>
.analytics-chart { width: 100%; min-width: 0; background: #f8faf6; }
.analytics-chart > div { width: 100%; height: 100%; }
.analytics-chart > p { display: grid; height: 100%; place-items: center; margin: 0; }
</style>
