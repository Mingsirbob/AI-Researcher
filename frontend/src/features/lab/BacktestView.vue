<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { Play } from "lucide-vue-next";
import { api, jsonBody } from "@/api/client";
import { number, pct, money } from "@/lib/format";
import { useUiStore } from "@/stores/ui";
import PageHeader from "@/components/PageHeader.vue";
import MetricStrip from "@/components/MetricStrip.vue";
import AsyncState from "@/components/AsyncState.vue";
import BacktestAnalytics from "@/components/BacktestAnalytics.vue";
import ResearchDataGrid from "@/components/ResearchDataGrid.vue";
import type { ColDef } from "ag-grid-community";

interface FactorMetric { factor_id: string; factor_name?: string; factor_version?: number }
interface Evaluation { item?: { run?: { evaluation_id: string; requested_start_date?: string; requested_end_date?: string }; metrics?: FactorMetric[] } }
interface BacktestNav { trading_date: string; nav: number }
interface Rebalance { signal_date: string; execution_date: string; executed_buy_count: number; executed_sell_count: number; blocked_buy_count: number; blocked_sell_count: number; turnover: number; explicit_cost: number; slippage_cost: number }
interface BacktestRun { backtest_id: string; metrics?: Record<string, number>; limitations?: string[] }
interface Backtest { item?: { run?: BacktestRun; nav?: BacktestNav[]; rebalances?: Rebalance[] } }
interface Artifact { artifact_type: string; sha256?: string; path?: string }
interface ModelRun { item?: { model_run_id: string; model_version?: string; workflow_version?: string; data_fingerprint?: string; prediction_snapshot?: { end_date?: string }; artifacts?: Artifact[]; limitations?: string[] } }
interface Gate { name: string; passed: boolean; observed?: unknown; expected?: unknown }
interface Validation { item?: { status?: string; data_fingerprint?: string; gates?: Gate[]; metrics?: Record<string, unknown> } }
interface Shadow { item?: { snapshot_id: string; as_of: string; data_fingerprint?: string; gates?: Gate[]; limitations?: string[] } }
interface Signal { security_code: string; security_name?: string; cross_section_rank: number; score: number; percentile: number; realized_label?: number | null }
interface SignalList { items: Signal[]; as_of?: string; total?: number }

const ui = useUiStore();
const client = useQueryClient();
const signalMode = ref<"current" | "historical">("current");
const signalDate = ref("");
const evaluation = useQuery({ queryKey: ["factor-lab", "evaluation"], queryFn: () => api<Evaluation>("/api/factor-lab/evaluations/latest") });
const latest = useQuery({ queryKey: ["factor-lab", "backtest"], queryFn: () => api<Backtest>("/api/factor-lab/backtests/latest") });
const model = useQuery({ queryKey: ["model-run", "latest"], queryFn: () => api<ModelRun>("/api/model-runs/latest"), retry: false });
const modelId = computed(() => model.data.value?.item?.model_run_id);
watch(() => model.data.value?.item?.prediction_snapshot?.end_date, (value) => { if (value && !signalDate.value) signalDate.value = value; }, { immediate: true });
const validation = useQuery({ queryKey: computed(() => ["model-run", modelId.value, "validation"]), queryFn: () => api<Validation>(`/api/model-runs/${modelId.value}/validation`), enabled: computed(() => Boolean(modelId.value)) });
const currentShadow = useQuery({ queryKey: ["current-shadow", "latest"], queryFn: () => api<Shadow>("/api/current-shadow/latest"), retry: false });
const signalPath = computed(() => {
  if (signalMode.value === "current" && currentShadow.data.value?.item?.snapshot_id) return `/api/current-shadow/${currentShadow.data.value.item.snapshot_id}/signals?limit=100`;
  if (!modelId.value) return "";
  const params = new URLSearchParams({ limit: "100" });
  if (signalDate.value) params.set("as_of", signalDate.value);
  return `/api/model-runs/${modelId.value}/signals?${params}`;
});
const signals = useQuery({ queryKey: computed(() => ["shadow-signals", signalMode.value, signalPath.value]), queryFn: () => api<SignalList>(signalPath.value), enabled: computed(() => Boolean(signalPath.value)) });
const data = computed(() => latest.data.value?.item);
const factorOptions = computed(() => {
  const seen = new Set<string>();
  return (evaluation.data.value?.item?.metrics || []).filter((item) => !seen.has(item.factor_id) && Boolean(seen.add(item.factor_id)));
});
const form = reactive({ factor_id: "", start_date: "", end_date: "", top_n: 30, rebalance_step: 20 });
watch(factorOptions, (items) => { if (!form.factor_id && items.length) form.factor_id = items[0].factor_id; }, { immediate: true });
const metrics = computed(() => {
  const values = data.value?.run?.metrics || {};
  return [
    { label: "累计收益", value: pct(values.cumulative_return), note: "扣除成本" },
    { label: "相对等权代理", value: pct(values.excess_cumulative_return), note: "非官方指数" },
    { label: "最大回撤", value: pct(values.max_drawdown), note: "逐日净值" },
    { label: "夏普比率", value: number(values.sharpe_ratio, 3), note: "无风险利率为0" },
    { label: "平均换手", value: pct(values.average_turnover), note: "每次调仓" },
    { label: "总成本", value: money(values.total_cost), note: "佣金、税费与滑点" },
  ];
});
const navPoints = computed(() => (data.value?.nav || []).map((item) => ({ label: item.trading_date, value: item.nav })));
const rebalanceColumns: ColDef[] = [
  { field: "signal_date", headerName: "信号日", pinned: "left", minWidth: 112 },
  { field: "execution_date", headerName: "成交日", minWidth: 112 },
  { field: "executed_buy_count", headerName: "买入", maxWidth: 88 },
  { field: "executed_sell_count", headerName: "卖出", maxWidth: 88 },
  { headerName: "受阻", valueGetter: ({ data }) => Number(data?.blocked_buy_count || 0) + Number(data?.blocked_sell_count || 0), maxWidth: 88 },
  { field: "turnover", headerName: "换手", valueFormatter: ({ value }) => pct(value), minWidth: 105 },
  { headerName: "成本", valueGetter: ({ data }) => Number(data?.explicit_cost || 0) + Number(data?.slippage_cost || 0), valueFormatter: ({ value }) => money(value), minWidth: 115 },
];
const run = useMutation({ mutationFn: () => api("/api/factor-lab/backtests", { method: "POST", ...jsonBody({ ...form, evaluation_id: evaluation.data.value?.item?.run?.evaluation_id, start_date: form.start_date || null, end_date: form.end_date || null }) }), onSuccess: async () => { ui.notify("策略回测完成"); await client.invalidateQueries({ queryKey: ["factor-lab", "backtest"] }); }, onError: (error: Error) => ui.notify(error.message) });
</script>

<template>
  <PageHeader eyebrow="BACKTEST & SHADOW" title="策略回测与 Shadow" description="信号在收盘后形成并于下一交易日开盘成交。先验证成本后表现，再决定是否进入发布审查。">
    <label for="backtest-factor">因子<select id="backtest-factor" v-model="form.factor_id"><option v-for="item in factorOptions" :key="item.factor_id" :value="item.factor_id">{{ item.factor_name||item.factor_id }}</option></select></label>
    <label for="backtest-top-n">Top N<select id="backtest-top-n" v-model.number="form.top_n"><option :value="10">10</option><option :value="20">20</option><option :value="30">30</option><option :value="50">50</option></select></label>
    <button class="button" :disabled="run.isPending.value||!form.factor_id" @click="run.mutate()"><Play :size="14" />运行回测</button>
  </PageHeader>
  <MetricStrip :items="metrics" />
  <section class="content-band white"><AsyncState :loading="latest.isPending.value" :error="latest.error.value" :empty="!data"><div class="section-heading"><div><span>NAV & DRAWDOWN</span><b>扣费后策略净值与逐日回撤</b></div><small>{{ navPoints.length }} 个交易日</small></div><BacktestAnalytics :points="data?.nav || []" /><div class="section-heading" style="margin-top:24px"><div><span>REBALANCE LEDGER</span><b>调仓与交易成本</b></div><small>{{ data?.run?.backtest_id }}</small></div><ResearchDataGrid :rows="[...(data?.rebalances || [])].reverse()" :columns="rebalanceColumns" row-id="execution_date" :height="390" empty-text="当前回测没有调仓记录" /><div class="section-heading" style="margin-top:24px"><div><span>BOUNDARIES</span><b>强制边界</b></div></div><div class="list"><article v-for="item in data?.run?.limitations||[]" :key="item" class="list-item"><p>{{ item }}</p></article></div></AsyncState></section>
  <section class="content-band"><div class="section-heading"><div><span>MODEL PROVENANCE</span><b>模型验证与 Shadow 信号</b></div><div class="tabs" style="padding:0;border:0"><button :class="{active:signalMode==='current'}" @click="signalMode='current'">当前</button><button :class="{active:signalMode==='historical'}" @click="signalMode='historical'">历史</button></div></div><div v-if="signalMode==='historical'" class="control-grid" style="margin-bottom:16px"><label for="historical-signal-date">历史交易日<input id="historical-signal-date" v-model="signalDate" type="date" /></label></div><div class="panel-grid"><article class="panel"><h3>模型运行</h3><p class="mono">{{ model.data.value?.item?.model_run_id||'尚未导入' }}</p><p>{{ model.data.value?.item?.model_version||model.data.value?.item?.workflow_version }}</p></article><article class="panel"><h3>验证状态</h3><p>{{ validation.data.value?.item?.status||'尚未验证' }}</p><p class="mono">{{ validation.data.value?.item?.data_fingerprint }}</p></article><article class="panel"><h3>当前 Shadow</h3><p>{{ currentShadow.data.value?.item?.as_of||'尚未生成' }}</p><p class="mono">{{ currentShadow.data.value?.item?.snapshot_id }}</p></article></div><div class="section-heading" style="margin-top:20px"><div><span>ARTIFACT LEDGER</span><b>模型产物与指纹</b></div></div><div class="list"><article v-for="item in model.data.value?.item?.artifacts||[]" :key="item.artifact_type" class="list-item"><header><h3>{{ item.artifact_type }}</h3><code>{{ item.sha256||'—' }}</code></header><p>{{ item.path }}</p></article><article class="list-item"><h3>模型数据指纹</h3><p class="mono">{{ model.data.value?.item?.data_fingerprint||'—' }}</p></article><article class="list-item"><h3>当前数据指纹</h3><p class="mono">{{ currentShadow.data.value?.item?.data_fingerprint||'—' }}</p></article></div><div class="section-heading" style="margin-top:20px"><div><span>VALIDATION GATES</span><b>模型与当前数据门禁</b></div></div><div class="panel-grid"><article v-for="gate in [...(validation.data.value?.item?.gates||[]),...(currentShadow.data.value?.item?.gates||[])]" :key="`${gate.name}-${String(gate.observed)}`" class="panel"><span :class="['status',gate.passed?'':'fail']">{{ gate.passed?'PASS':'STOP' }}</span><h3 style="margin-top:8px">{{ gate.name }}</h3><p>观测 {{ gate.observed }} · 期望 {{ gate.expected??'—' }}</p></article></div><div class="data-table-wrap" style="margin-top:16px"><table class="data-table"><thead><tr><th>排名</th><th>证券</th><th>分数</th><th>百分位</th><th>实际标签</th></tr></thead><tbody><tr v-for="item in signals.data.value?.items||[]" :key="item.security_code"><td>#{{ item.cross_section_rank }}</td><td><b>{{ item.security_name||item.security_code }}</b><br><small>{{ item.security_code }}</small></td><td>{{ number(item.score,6) }}</td><td>{{ number(item.percentile,2) }}%</td><td>{{ item.realized_label==null?'待评价':pct(item.realized_label) }}</td></tr><tr v-if="!signals.data.value?.items?.length"><td colspan="5">{{ signalMode==='historical'?'该交易日没有历史信号':'当前没有可用信号' }}</td></tr></tbody></table></div></section>
</template>
