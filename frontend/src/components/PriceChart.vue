<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import {
  CandlestickSeries,
  ColorType,
  HistogramSeries,
  createSeriesMarkers,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
} from "lightweight-charts";

interface Marker { date: string; side: "buy" | "sell"; label?: string }
const props = withDefaults(defineProps<{
  dates: string[];
  close: number[];
  open?: number[];
  high?: number[];
  low?: number[];
  volume?: number[];
  markers?: Marker[];
}>(), { open: () => [], high: () => [], low: () => [], volume: () => [], markers: () => [] });

const container = ref<HTMLElement>();
let chart: IChartApi | undefined;
let candles: ISeriesApi<"Candlestick"> | undefined;
let volumes: ISeriesApi<"Histogram"> | undefined;
let markerPlugin: ISeriesMarkersPluginApi<Time> | undefined;
let observer: ResizeObserver | undefined;

function finite(value: unknown, fallback: number) {
  const result = Number(value);
  return Number.isFinite(result) ? result : fallback;
}

function setData() {
  if (!candles || !volumes) return;
  const points = props.dates.map((date, index) => {
    const close = finite(props.close[index], 0);
    const previous = finite(props.close[index - 1], close);
    const open = finite(props.open[index], previous);
    const high = Math.max(open, close, finite(props.high[index], Math.max(open, close)));
    const low = Math.min(open, close, finite(props.low[index], Math.min(open, close)));
    return { time: date as Time, open, high, low, close };
  }).filter((item) => item.close > 0);
  candles.setData(points);
  volumes.setData(props.dates.map((date, index) => {
    const current = finite(props.close[index], 0);
    const previous = finite(props.close[index - 1], current);
    return {
      time: date as Time,
      value: Math.max(0, finite(props.volume[index], 0)),
      color: current >= previous ? "rgba(162,60,52,.35)" : "rgba(24,112,79,.35)",
    };
  }));
  markerPlugin?.setMarkers(props.markers.map((item) => ({
    time: item.date as Time,
    position: item.side === "buy" ? "belowBar" : "aboveBar",
    color: item.side === "buy" ? "#a23c34" : "#18704f",
    shape: item.side === "buy" ? "arrowUp" : "arrowDown",
    text: item.label || (item.side === "buy" ? "买入" : "卖出"),
  } satisfies SeriesMarker<Time>)));
  chart?.timeScale().fitContent();
}

onMounted(() => {
  if (!container.value) return;
  chart = createChart(container.value, {
    width: container.value.clientWidth,
    height: 330,
    layout: { background: { type: ColorType.Solid, color: "#ffffff" }, textColor: "#69716c", fontFamily: '"Microsoft YaHei UI", sans-serif', fontSize: 10 },
    grid: { vertLines: { color: "#edf0eb" }, horzLines: { color: "#edf0eb" } },
    rightPriceScale: { borderColor: "#d9ded9", scaleMargins: { top: 0.08, bottom: 0.25 } },
    timeScale: { borderColor: "#d9ded9", timeVisible: true, rightOffset: 4 },
    crosshair: { vertLine: { color: "#006b5e", labelBackgroundColor: "#006b5e" }, horzLine: { color: "#006b5e", labelBackgroundColor: "#006b5e" } },
    handleScroll: true,
    handleScale: true,
  });
  candles = chart.addSeries(CandlestickSeries, { upColor: "#a23c34", downColor: "#18704f", borderVisible: false, wickUpColor: "#a23c34", wickDownColor: "#18704f" });
  markerPlugin = createSeriesMarkers(candles);
  volumes = chart.addSeries(HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "volume" });
  chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  setData();
  observer = new ResizeObserver(([entry]) => chart?.applyOptions({ width: Math.floor(entry.contentRect.width) }));
  observer.observe(container.value);
});

watch(() => [props.dates, props.close, props.open, props.high, props.low, props.volume, props.markers], setData, { deep: true });
onBeforeUnmount(() => { observer?.disconnect(); chart?.remove(); chart = undefined; });
</script>

<template>
  <div class="financial-chart" role="img" aria-label="证券K线与成交量图">
    <div ref="container" />
    <p v-if="!dates.length" class="muted">暂无可绘制行情数据</p>
  </div>
</template>

<style scoped>
.financial-chart { position: relative; width: 100%; min-height: 330px; background: white; }
.financial-chart > div { width: 100%; min-height: 330px; }
.financial-chart > p { position: absolute; inset: 0; display: grid; place-items: center; margin: 0; background: white; }
</style>
