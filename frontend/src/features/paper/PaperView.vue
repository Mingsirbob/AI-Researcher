<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { Plus, Play, RefreshCw, Search, X } from "lucide-vue-next";
import { api, openApiJsonBody } from "@/api/client";
import type { OpenApiSchema } from "@/api/client";
import type { BenchmarkSeries, ChartSeries, DailyBatch, PaperTarget } from "@/api/types";
import { money, pct } from "@/lib/format";
import { useUiStore } from "@/stores/ui";
import PageHeader from "@/components/PageHeader.vue";
import MetricStrip from "@/components/MetricStrip.vue";
import AsyncState from "@/components/AsyncState.vue";
import PaperPerformanceSection from "./components/PaperPerformanceSection.vue";
import PaperOrdersSection from "./components/PaperOrdersSection.vue";
import PaperPositionsSection from "./components/PaperPositionsSection.vue";

interface PaperOrder { order_id: string; security_code: string; security_name?: string; side: string; status: string; quantity: number; reference_price?: number; target_weight?: number; reason?: Record<string, unknown> }
interface PaperPosition { security_code: string; security_name?: string; quantity: number; sellable_quantity: number; current_price?: number; market_value?: number; weight?: number; unrealized_pnl?: number; shadow_rank?: number }
interface PaperRun { run_id: string; status: string; orders: PaperOrder[]; market_summary?: Record<string, unknown> }
interface Comparison {
  status?: string;
  as_of?: string;
  inception_date?: string | null;
  valuation_mode?: string;
  latest_quote_time?: string | null;
  portfolio?: { name?: string; latest_return?: number | null; points: Array<{ date: string; cumulative_return: number }> } | null;
  benchmarks: BenchmarkSeries[];
  limitations?: string[];
}
interface PaperStrategy { strategy_id: string; version: string; name: string; description: string; signal_source: string; config: Record<string, unknown> }
interface PaperAccount { account_id: string; name: string; initial_cash: number; cash: number; benchmark_code: string; strategy_id: string; strategy: PaperStrategy }
interface PaperDashboard { as_of: string; nav: number; cumulative_return: number; cash_weight: number; account: PaperAccount; positions: PaperPosition[]; runs: PaperRun[]; nav_history: Array<{ drawdown?: number }>; benchmark_comparison: Comparison; latest_shadow?: { snapshot_id: string; as_of: string; status: string } | null; realtime?: { quote_count: number; latest_quote_time?: string | null; high_frequency_used?: boolean } }
interface TargetSet { targets: PaperTarget[] }
interface FeatureSnapshot { snapshot_id: string; as_of: string; status: string }
interface TraceState extends PaperTarget { order?: PaperOrder; run?: PaperRun }
interface EvidenceLine { statement: string; evidence_ids?: string[]; severity?: string }
interface PerformanceRow { code: string; name: string; latest_return?: number | null; excess_return?: number | null; coverage?: number | null; status: string }

const ui = useUiStore();
const client = useQueryClient();
const asOf = ref("");
const selectedAccountId = ref("");
const showAccountForm = ref(false);
const accountForm = reactive<OpenApiSchema<"PaperAccountCreate">>({ name: "", initial_cash: 1_000_000, benchmark_code: "000300.SH", strategy_id: "lightgbm_shadow_v1" });
const batchId = ref("");
const poll = ref(0);
const realtimePoll = ref(0);
const realtimeRefreshing = ref(false);
const realtimeError = ref("");
const trace = ref<TraceState | null>(null);
const accounts = useQuery({ queryKey: ["paper", "accounts"], queryFn: () => api<{ items: PaperAccount[] }>("/api/paper/accounts") });
const strategies = useQuery({ queryKey: ["paper", "strategies"], queryFn: () => api<{ items: PaperStrategy[] }>("/api/paper/strategies") });
watch(() => accounts.data.value?.items, (items) => { if (!selectedAccountId.value && items?.length) selectedAccountId.value = items[0].account_id; }, { immediate: true });
watch(selectedAccountId, () => { batchId.value = ""; trace.value = null; });
const dashboard = useQuery({ queryKey: computed(() => ["paper", "dashboard", selectedAccountId.value, asOf.value]), queryFn: () => { const params = new URLSearchParams(); if (selectedAccountId.value) params.set("account_id", selectedAccountId.value); if (asOf.value) params.set("as_of", asOf.value); return api<PaperDashboard>(`/api/paper/dashboard?${params}`); } });
const featureSnapshot = useQuery({ queryKey: ["paper", "feature-snapshot", "latest"], queryFn: () => api<FeatureSnapshot>("/api/factor-snapshots/latest"), retry: false });
const data = computed(() => dashboard.data.value);
const researchDate = computed(() => asOf.value || data.value?.latest_shadow?.as_of || "");
const latestResearchContractReady = computed(() => Boolean(data.value?.latest_shadow?.as_of && data.value.latest_shadow.as_of === featureSnapshot.data.value?.as_of));
const targets = useQuery({ queryKey: computed(() => ["paper", "targets", selectedAccountId.value, researchDate.value]), queryFn: () => api<TargetSet>(`/api/paper/research-targets?as_of=${researchDate.value}&account_id=${selectedAccountId.value}&limit=5&hold_rank_buffer=30`), enabled: computed(() => Boolean(selectedAccountId.value && researchDate.value && (asOf.value || latestResearchContractReady.value))) });
const latestBatch = useQuery({ queryKey: computed(() => ["paper", "batch", "latest", data.value?.account?.account_id, asOf.value]), queryFn: () => api<{ item: DailyBatch | null }>(`/api/paper/daily-batches/latest?account_id=${data.value?.account.account_id}${asOf.value ? `&as_of=${asOf.value}` : ""}`), enabled: computed(() => Boolean(data.value?.account?.account_id)) });
watch(() => latestBatch.data.value?.item?.batch_id, (value) => { if (value && !batchId.value) batchId.value = value; }, { immediate: true });
const latestRun = computed(() => data.value?.runs?.[0]);
const activeStrategy = computed(() => data.value?.account?.strategy);
const orders = computed(() => latestRun.value?.orders || []);
const currentBatch = computed(() => batch.data.value || latestBatch.data.value?.item || null);
const batchSteps = computed(() => currentBatch.value?.steps || []);
const completedBatchSteps = computed(() => batchSteps.value.filter((step) => step.status === "completed").length);
const targetByCode = computed(() => new Map((targets.data.value?.targets || []).map((item) => [item.security_code, item])));
const comparisonSeries = computed<ChartSeries[]>(() => {
  const comparison = data.value?.benchmark_comparison;
  if (!comparison) return [];
  const colors: Record<string, string> = { portfolio: "#176f63", "000001.SH": "#b8483e", "000300.SH": "#526fa6", "399001.SZ": "#be7b23", "399006.SZ": "#2b7b73", "000688.SH": "#785b8f", "000510.SH": "#59645f" };
  const series: ChartSeries[] = [];
  if (comparison.portfolio) series.push({ name: "模拟组合", color: colors.portfolio, points: comparison.portfolio.points.map((item) => ({ label: item.date, value: 1 + item.cumulative_return })) });
  comparison.benchmarks.forEach((item) => series.push({ name: item.name, color: colors[item.code] || "#59645f", points: item.points.map((point) => ({ label: point.date, value: 1 + point.cumulative_return })) }));
  return series;
});
const metrics = computed(() => [
  { label: "组合净值", value: money(data.value?.nav), note: data.value?.as_of || "—" },
  { label: "累计收益", value: pct(data.value?.cumulative_return), note: "模拟组合" },
  { label: "可用现金", value: money(data.value?.account?.cash), note: "只读快照" },
  { label: "持仓权重", value: pct(1 - Number(data.value?.cash_weight || 1)), note: `${data.value?.positions?.length || 0} 只持仓` },
  { label: "当前回撤", value: pct(data.value?.nav_history?.at(-1)?.drawdown), note: "峰值至今" },
]);
const performanceRows = computed(() => {
  const comparison = data.value?.benchmark_comparison;
  if (!comparison) return [];
  const rows: PerformanceRow[] = comparison.portfolio ? [{
    code: "portfolio",
    name: comparison.portfolio.name || data.value?.account?.name || "模拟组合",
    latest_return: comparison.portfolio.latest_return,
    excess_return: null,
    coverage: 1,
    status: comparison.status || "complete",
  }] : [];
  return rows.concat(comparison.benchmarks.map<PerformanceRow>((item) => ({
    code: item.code,
    name: item.name,
    latest_return: item.latest_return,
    excess_return: item.excess_return,
    coverage: item.coverage,
    status: item.status,
  })));
});
const assessment = computed(() => trace.value?.current_assessment || {});
const evidence = computed(() => (assessment.value.fundamental_evidence as EvidenceLine[] | undefined) || []);
const negatives = computed(() => (assessment.value.material_negatives as EvidenceLine[] | undefined) || []);
const limitations = computed(() => (assessment.value.limitations as string[] | undefined) || []);
async function reload(message?: string) { await client.invalidateQueries({ queryKey: ["paper"] }); if (message) ui.notify(message); }
async function refreshRealtime(notify = false) {
  if (!selectedAccountId.value || asOf.value || document.visibilityState === "hidden" || realtimeRefreshing.value) return;
  realtimeRefreshing.value = true;
  try {
    await api("/api/paper/realtime-quotes/refresh", { method: "POST", ...openApiJsonBody<"PaperRealtimeRequest">({ account_id: selectedAccountId.value }) });
    realtimeError.value = "";
    await dashboard.refetch();
    if (notify) ui.notify("实时行情与收益对比已更新");
  } catch (error) {
    realtimeError.value = (error as Error).message;
    if (notify) notifyError(error as Error);
  } finally {
    realtimeRefreshing.value = false;
  }
}
function startRealtimePolling() {
  window.clearInterval(realtimePoll.value);
  if (!selectedAccountId.value || asOf.value || document.visibilityState === "hidden") return;
  void refreshRealtime();
  realtimePoll.value = window.setInterval(() => void refreshRealtime(), 30_000);
}
function handleVisibilityChange() { startRealtimePolling(); }
const daily = useMutation({ mutationFn: () => api<PaperRun>("/api/paper/daily-runs", { method: "POST", ...openApiJsonBody<"PaperDailyRunCreate">({ as_of: asOf.value || null, account_id: selectedAccountId.value, top_n: 5, hold_rank_buffer: 30 }) }), onSuccess: () => reload("每日研究批次已生成"), onError: notifyError });
const assess = useMutation({ mutationFn: () => api<{ completed: number; results: unknown[] }>("/api/paper/research-assessments", { method: "POST", ...openApiJsonBody<"PaperResearchBatchRequest">({ as_of: researchDate.value || null, account_id: selectedAccountId.value, include_holdings: true, limit: 5, hold_rank_buffer: 30, lookback_days: 730, financial_download_limit: 2, announcement_download_limit: 3, depth: "quick" }) }), onSuccess: async (result) => { ui.notify(`候选研究完成 ${result.completed}/${result.results.length}`); await reload(); }, onError: notifyError });
const benchmark = useMutation({ mutationFn: () => api<{ benchmark_count: number }>("/api/paper/benchmarks/refresh", { method: "POST", ...openApiJsonBody<"PaperBenchmarkRefresh">({ account_id: data.value?.account?.account_id, as_of: asOf.value || data.value?.as_of }) }), onSuccess: async (result) => { ui.notify(`已更新 ${result.benchmark_count} 个指数基准`); await reload(); }, onError: notifyError });
const full = useMutation({ mutationFn: () => api<DailyBatch & { reused: boolean }>("/api/paper/daily-batches", { method: "POST", ...openApiJsonBody<"PaperDailyBatchRequest">({ as_of: asOf.value || null, account_id: selectedAccountId.value, shadow_lookback_days: 240, shadow_batch_size: 20, top_n: 5, hold_rank_buffer: 30, research_lookback_days: 730, financial_download_limit: 2, announcement_download_limit: 3, depth: "quick" }) }), onSuccess: (result) => { batchId.value = result.batch_id; ui.notify(result.reused ? "已复用每日完整批次" : "完整批次已开始"); startPoll(); }, onError: notifyError });
const createAccount = useMutation({ mutationFn: () => api<PaperAccount>("/api/paper/accounts", { method: "POST", ...openApiJsonBody<"PaperAccountCreate">({ ...accountForm }) }), onSuccess: async (account) => { selectedAccountId.value = account.account_id; showAccountForm.value = false; accountForm.name = ""; await client.invalidateQueries({ queryKey: ["paper"] }); ui.notify("模拟账户已创建"); }, onError: notifyError });
const batch = useQuery({ queryKey: computed(() => ["paper", "batch", batchId.value]), queryFn: () => api<DailyBatch>(`/api/paper/daily-batches/${batchId.value}`), enabled: computed(() => Boolean(batchId.value)) });
function notifyError(error: Error) { ui.notify(error.message); }
function startPoll() { window.clearInterval(poll.value); poll.value = window.setInterval(async () => { const result = await batch.refetch(); if (["completed", "failed"].includes(result.data?.status || "")) { window.clearInterval(poll.value); reload(result.data?.status === "completed" ? "完整批次已完成" : "完整批次执行失败"); } }, 2000); }
watch([selectedAccountId, asOf], startRealtimePolling);
onMounted(() => { document.addEventListener("visibilitychange", handleVisibilityChange); startRealtimePolling(); });
onBeforeUnmount(() => { window.clearInterval(poll.value); window.clearInterval(realtimePoll.value); document.removeEventListener("visibilitychange", handleVisibilityChange); });
async function settle() { try { await api("/api/paper/settle/realtime", { method: "POST", ...openApiJsonBody<"PaperSettleRequest">({ account_id: selectedAccountId.value }) }); await reload("实时模拟撮合已执行"); } catch (error) { notifyError(error as Error); } }
async function review(id: string, decision: string) { try { await api(`/api/paper/orders/${encodeURIComponent(id)}/${decision}`, { method: "POST", ...openApiJsonBody<"PaperOrderReview">({ reviewer: "human", note: "人工确认模拟订单" }) }); await reload(decision === "approve" ? "订单已批准" : "订单已拒绝"); } catch (error) { notifyError(error as Error); } }
function openTargetTrace(item: PaperTarget) { trace.value = item; }
function openOrderTrace(order: PaperOrder) { trace.value = { ...(targetByCode.value.get(order.security_code) || { security_code: order.security_code, security_name: order.security_name || order.security_code }), order, run: latestRun.value }; }
</script>

<template>
  <PageHeader eyebrow="PAPER TRADING DESK" title="模拟盘" description="账户绩效、策略信号、人工审批和模拟成交统一管理；完整批次不会自动批准或成交订单。">
    <label for="paper-account">账户<select id="paper-account" v-model="selectedAccountId"><option v-for="item in accounts.data.value?.items||[]" :key="item.account_id" :value="item.account_id">{{ item.name }}</option></select></label>
    <button class="icon-button" title="新建模拟账户" aria-label="新建模拟账户" @click="showAccountForm=!showAccountForm"><Plus :size="15" /></button>
    <label for="paper-as-of">研究日<input id="paper-as-of" v-model="asOf" type="date" /></label>
    <button class="button secondary" :disabled="assess.isPending.value || (!asOf && !latestResearchContractReady)" :title="!asOf && !latestResearchContractReady ? '最新 Current Shadow 与因子快照日期未对齐' : '研究候选'" @click="assess.mutate()"><Search :size="14" />研究候选</button>
    <button class="button secondary" :disabled="daily.isPending.value" @click="daily.mutate()"><Play :size="14" />运行研究</button>
    <button class="button" :disabled="full.isPending.value" @click="full.mutate()"><Play :size="14" />运行完整批次</button>
    <button class="icon-button" title="立即刷新实时行情" aria-label="立即刷新实时行情" :disabled="realtimeRefreshing" @click="refreshRealtime(true)"><RefreshCw :size="15" :class="{ spinning: realtimeRefreshing }" /></button>
  </PageHeader>
  <section v-if="showAccountForm" class="content-band white account-create-band"><form class="account-form" @submit.prevent="createAccount.mutate()"><label for="new-account-name">账户名称<input id="new-account-name" v-model="accountForm.name" required maxlength="80" /></label><label for="new-account-capital">初始资金<input id="new-account-capital" v-model.number="accountForm.initial_cash" type="number" min="1" max="1000000000" required /></label><label for="new-account-strategy">策略<select id="new-account-strategy" v-model="accountForm.strategy_id"><option v-for="item in strategies.data.value?.items||[]" :key="item.strategy_id" :value="item.strategy_id">{{ item.name }}</option></select></label><label for="new-account-benchmark">基准<select id="new-account-benchmark" v-model="accountForm.benchmark_code"><option value="000300.SH">沪深300</option><option value="000905.SH">中证500</option><option value="000852.SH">中证1000</option></select></label><button class="button" type="submit" :disabled="createAccount.isPending.value"><Plus :size="14" />创建账户</button></form></section>
  <AsyncState :loading="dashboard.isPending.value" :error="dashboard.error.value" @retry="dashboard.refetch()">
    <section class="strategy-ledger"><div><span>ACTIVE ACCOUNT</span><b>{{ data?.account?.name || '—' }}</b><small>{{ data?.account?.account_id }}</small></div><div><span>STRATEGY CONTRACT</span><b>{{ activeStrategy?.name || '—' }}</b><small>{{ activeStrategy?.strategy_id }} @ {{ activeStrategy?.version }}</small></div><div><span>SIGNAL SOURCE</span><b>{{ activeStrategy?.signal_source || '—' }}</b><small>目标仓位 {{ pct(Number(activeStrategy?.config?.target_gross_exposure || 0)) }}</small></div></section>
    <MetricStrip :items="metrics" />
    <PaperPerformanceSection :account-id="data?.account?.account_id" :comparison="data?.benchmark_comparison" :rows="performanceRows" :series="comparisonSeries" :realtime-error="realtimeError" :refreshing="benchmark.isPending.value" @refresh="benchmark.mutate()" />
    <section class="content-band white"><div class="section-heading"><div><span>RESEARCH TARGETS</span><b>Shadow 候选与研究判断</b></div><small>{{ targets.data.value?.targets?.length||0 }} 个目标</small></div><div class="list"><article v-if="!asOf && !latestResearchContractReady" class="list-item"><header><h3>研究合同未对齐</h3><span class="status warn">WAIT</span></header><p>Current Shadow {{ data?.latest_shadow?.as_of || '缺失' }} · 因子快照 {{ featureSnapshot.data.value?.as_of || '缺失' }}。请生成同日完整批次，或明确选择已有共同快照的研究日。</p></article><article v-for="item in targets.data.value?.targets||[]" :key="item.security_code" class="list-item"><header><div><span class="status">#{{ item.shadow_rank||'—' }}</span><h3 style="margin-top:8px">{{ item.security_name }} · {{ item.security_code }}</h3></div><span :class="['status',item.current_research_status==='veto'?'fail':item.current_research_status==='defer'?'warn':'']">{{ item.current_research_status||'未研究' }}</span></header><p>{{ item.target_reason }} · score {{ item.score }}</p><footer><button class="button secondary" @click="openTargetTrace(item)"><Search :size="13" />决策依据</button></footer></article><article v-if="(asOf || latestResearchContractReady) && !targets.data.value?.targets?.length" class="list-item"><p>当前没有可研究的 Shadow 目标。</p></article></div></section>
    <section v-if="batchId" class="content-band batch-summary"><div class="section-heading"><div><span>RUN STATUS</span><b>批次执行状态</b></div><span :class="['status',currentBatch?.status==='failed'?'fail':currentBatch?.status==='completed'?'':'warn']">{{ currentBatch?.status||'queued' }}</span></div><div class="batch-overview"><div><span>进度</span><b>{{ completedBatchSteps }} / {{ batchSteps.length }}</b></div><div><span>当前步骤</span><b>{{ currentBatch?.current_step || (currentBatch?.status==='completed'?'全部完成':'等待执行') }}</b></div><div><span>批次编号</span><small>{{ batchId }}</small></div></div><ol v-if="batchSteps.length" class="batch-step-list"><li v-for="step in batchSteps" :key="step.step_name"><span :class="['status',step.status==='failed'?'fail':step.status==='completed'?'':'warn']">{{ step.status }}</span><b>{{ step.step_name }}</b><small v-if="step.error">{{ step.error }}</small></li></ol></section>
    <PaperOrdersSection :orders="orders" @settle="settle" @trace="openOrderTrace" @review="review" />
    <PaperPositionsSection :positions="data?.positions || []" />
  </AsyncState>
  <button v-if="trace" class="drawer-backdrop" aria-label="关闭决策依据" @click="trace=null" />
  <aside v-if="trace" class="drawer" role="dialog" aria-modal="true" aria-labelledby="decision-trace-title"><div class="section-heading"><div><span>DECISION TRACE</span><b id="decision-trace-title">{{ trace.security_name }} · {{ trace.security_code }}</b></div><button class="icon-button" title="关闭" aria-label="关闭决策依据" @click="trace=null"><X :size="16" /></button></div><div class="panel-grid"><article class="panel"><h3>1 · Shadow Signal</h3><p>#{{ trace.shadow_rank||'—' }} · {{ trace.score??'—' }}</p></article><article class="panel"><h3>2 · Research Assessment</h3><p>{{ trace.current_research_status||'未研究' }}</p></article><article class="panel"><h3>3 · Portfolio Gate</h3><p>{{ trace.order?'已形成订单提案':'尚未进入订单阶段' }}</p></article><article class="panel"><h3>4 · Human / Fill</h3><p>{{ trace.order?.status||'等待组合决策' }}</p></article></div><div class="section-heading" style="margin-top:20px"><div><span>EVIDENCE</span><b>研究证据与边界</b></div></div><div class="list"><article v-for="item in evidence" class="list-item"><p>{{ item.statement }}</p><footer>{{ item.evidence_ids?.join(' · ') }}</footer></article><article v-for="item in negatives" class="list-item"><span class="status fail">{{ item.severity }}</span><p>{{ item.statement }}</p></article><article v-for="item in limitations" class="list-item"><p>{{ item }}</p></article><article v-if="trace.order?.reason" class="list-item"><h3>订单确定性原因</h3><pre>{{ JSON.stringify(trace.order.reason,null,2) }}</pre></article><article v-if="trace.run?.market_summary" class="list-item"><h3>组合门禁快照</h3><pre>{{ JSON.stringify(trace.run.market_summary,null,2) }}</pre></article></div></aside>
</template>

<style scoped>
.account-create-band { border-top: 0; }
.account-form { display: grid; grid-template-columns: minmax(160px, 1.2fr) repeat(3, minmax(140px, 1fr)) auto; gap: 12px; align-items: end; }
.strategy-ledger { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); border-bottom: 1px solid var(--line); background: #18201d; color: #f2f3ed; }
.strategy-ledger > div { min-width: 0; padding: 17px 22px; border-right: 1px solid rgba(255,255,255,.14); }
.strategy-ledger > div:last-child { border-right: 0; }
.strategy-ledger span, .strategy-ledger small { display: block; color: #a8b7af; }
.strategy-ledger span { margin-bottom: 7px; font-size: 10px; }
.strategy-ledger b { display: block; overflow-wrap: anywhere; }
.strategy-ledger small { margin-top: 5px; overflow-wrap: anywhere; }
.batch-summary { padding-bottom: 18px; }
.batch-overview { display: grid; grid-template-columns: 120px minmax(160px, .7fr) minmax(240px, 1.3fr); border: 1px solid var(--line); background: var(--paper); }
.batch-overview > div { min-width: 0; padding: 12px 14px; border-right: 1px solid var(--line); }
.batch-overview > div:last-child { border-right: 0; }
.batch-overview span, .batch-overview small { display: block; color: var(--muted); font-size: 9px; overflow-wrap: anywhere; }
.batch-overview b { display: block; margin-top: 5px; overflow-wrap: anywhere; }
.batch-step-list { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0 0; padding: 0; list-style: none; }
.batch-step-list li { display: flex; min-width: 0; align-items: center; gap: 7px; padding: 6px 8px; border: 1px solid var(--line); background: #fff; }
.batch-step-list b, .batch-step-list small { font-size: 9px; overflow-wrap: anywhere; }
.batch-step-list small { color: var(--danger); }
.spinning { animation: realtime-spin .9s linear infinite; }
@keyframes realtime-spin { to { transform: rotate(360deg); } }
@media (max-width: 900px) { .account-form { grid-template-columns: 1fr 1fr; } .strategy-ledger { grid-template-columns: 1fr; } .strategy-ledger > div { border-right: 0; border-bottom: 1px solid rgba(255,255,255,.14); } }
@media (max-width: 900px) { .batch-overview { grid-template-columns: 1fr; } .batch-overview > div { border-right: 0; border-bottom: 1px solid var(--line); } }
@media (max-width: 560px) { .account-form { grid-template-columns: 1fr; } }
</style>
