<script setup lang="ts">
import { RefreshCw } from "lucide-vue-next";
import MultiLineChart from "@/components/MultiLineChart.vue";
import type { ChartSeries } from "@/api/types";
import { pct } from "@/lib/format";
import ResearchDataGrid from "@/components/ResearchDataGrid.vue";
import type { ColDef } from "ag-grid-community";

interface ComparisonState {
  as_of?: string;
  valuation_mode?: string;
  latest_quote_time?: string | null;
  limitations?: string[];
}
interface PerformanceRow { code: string; name: string; latest_return?: number | null; excess_return?: number | null; coverage?: number | null; status: string }

defineProps<{
  accountId?: string;
  comparison?: ComparisonState;
  rows: PerformanceRow[];
  series: ChartSeries[];
  realtimeError: string;
  refreshing: boolean;
}>();
defineEmits<{ refresh: [] }>();
const columns: ColDef[] = [
  { field: "name", headerName: "对比对象", pinned: "left", width: 140, minWidth: 120 },
  { field: "code", headerName: "代码", width: 112, minWidth: 105 },
  { field: "latest_return", headerName: "累计收益", filter: "agNumberColumnFilter", valueFormatter: ({ value }) => pct(value), width: 105, minWidth: 96 },
  { field: "excess_return", headerName: "账户超额", filter: "agNumberColumnFilter", valueFormatter: ({ value, data }) => data?.code === "portfolio" ? "—" : pct(value), width: 105, minWidth: 96 },
  { field: "coverage", headerName: "覆盖率", valueFormatter: ({ value, data }) => data?.code === "portfolio" ? "—" : pct(value), width: 92, minWidth: 84 },
  { field: "status", headerName: "状态", filter: true, width: 94, minWidth: 86 },
];
</script>

<template>
  <section class="content-band white performance-band">
    <div class="section-heading"><div><span>ACCOUNT PERFORMANCE</span><b>账户收益对比</b></div><div class="performance-actions"><div class="live-state"><i :class="{ active: comparison?.valuation_mode==='intraday' }" /><small>{{ comparison?.valuation_mode==='intraday' ? `盘中估算 · ${comparison?.latest_quote_time?.slice(11,19)||'—'} · 30秒更新` : `日终收盘 · ${comparison?.as_of||'—'}` }}</small></div><button class="button secondary" :disabled="refreshing" @click="$emit('refresh')"><RefreshCw :size="13" />更新日线基准</button></div></div>
    <p v-if="realtimeError" class="realtime-warning">自动更新暂不可用：{{ realtimeError }}</p>
    <div v-if="rows.length" class="comparison-strip"><div v-for="item in rows" :key="item.code"><span>{{ item.name }}</span><b>{{ pct(item.latest_return) }}</b><small>{{ item.code === 'portfolio' ? '账户累计收益' : `相对账户 ${pct(item.excess_return)}` }}</small></div></div>
    <MultiLineChart :series="series" value-label="账户与指数累计净值对比" />
    <div class="comparison-grid"><ResearchDataGrid :rows="rows" :columns="columns" row-id="code" :height="310" :empty-text="comparison?.limitations?.join('；') || '暂无账户收益对比数据'" /></div>
    <p v-if="comparison?.limitations?.length" class="comparison-note">{{ comparison.limitations.join('；') }}</p>
  </section>
</template>

<style scoped>
.performance-band { padding-bottom: 0; }
.performance-actions { display: flex; align-items: center; gap: 12px; }
.live-state { display: flex; align-items: center; gap: 7px; }
.live-state i { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); }
.live-state i.active { background: #1f8a70; box-shadow: 0 0 0 3px rgba(31,138,112,.12); }
.realtime-warning { margin: -4px 0 12px; color: var(--danger); font-size: 9px; }
.comparison-strip { display: grid; grid-template-columns: repeat(auto-fit, minmax(135px, 1fr)); margin: 0 -22px 16px; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.comparison-strip > div { min-width: 0; padding: 13px 16px; border-right: 1px solid var(--line); }
.comparison-strip > div:last-child { border-right: 0; }
.comparison-strip span, .comparison-strip b, .comparison-strip small { display: block; overflow-wrap: anywhere; }
.comparison-strip span, .comparison-strip small { color: var(--muted); }
.comparison-strip span { font-size: 9px; }
.comparison-strip b { margin: 6px 0 4px; font-size: 18px; }
.comparison-strip small { font-size: 8px; }
.comparison-grid { margin: 16px -22px 0; }
.comparison-note { margin: 0 -22px; padding: 10px 22px; color: var(--muted); border-top: 1px solid var(--line); font-size: 9px; }
@media (max-width: 900px) { .comparison-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); } .comparison-strip > div { border-bottom: 1px solid var(--line); } }
@media (max-width: 560px) { .performance-actions { align-items: flex-end; flex-direction: column; gap: 6px; } .comparison-strip { grid-template-columns: 1fr; } .comparison-strip > div { border-right: 0; } }
</style>
