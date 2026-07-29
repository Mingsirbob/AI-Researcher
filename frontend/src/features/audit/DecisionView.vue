<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { ClipboardPlus, Play, ScanSearch } from "lucide-vue-next";
import { api, openApiJsonBody } from "@/api/client";
import type { OpenApiSchema } from "@/api/client";
import { number, pct, shortId } from "@/lib/format";
import { useUiStore } from "@/stores/ui";
import PageHeader from "@/components/PageHeader.vue";
import MetricStrip from "@/components/MetricStrip.vue";
import AsyncState from "@/components/AsyncState.vue";
import type { ApiList, BatchEvaluationResult, DecisionCase, DecisionOutcome, MaturityScan } from "@/api/types";

interface AttributionGroup { key: string; count: number; mean_excess_return?: number; positive_excess_ratio?: number; reliable?: boolean }
interface Attribution { counts?: { completed?: number }; overall?: { mean_security_return?: number; mean_excess_return?: number; positive_excess_ratio?: number; mean_maximum_adverse_excursion?: number }; groups?: Record<string, AttributionGroup[]> }

const ui = useUiStore();
const client = useQueryClient();
const selected = ref<DecisionCase | null>(null);
const review = reactive({ reviewer: "human", note: "" });
const form = reactive<OpenApiSchema<"DecisionCaseCreate">>({ code: "300750.SZ", as_of: "", decision_horizon: "60d", benchmark_code: "000300.SH" });
const maturityAsOf = ref("");
const selectedReady = ref<string[]>([]);
const batchResult = ref<BatchEvaluationResult | null>(null);
const cases = useQuery({ queryKey: ["decisions"], queryFn: () => api<ApiList<DecisionCase>>("/api/decision-cases?limit=100") });
const outcomes = useQuery({ queryKey: ["decision-outcomes"], queryFn: () => api<ApiList<DecisionOutcome>>("/api/decision-outcomes?limit=500") });
const attribution = useQuery({ queryKey: ["decision-outcomes", "attribution"], queryFn: () => api<Attribution>("/api/decision-outcomes/attribution") });
const maturity = useQuery({ queryKey: computed(() => ["decision-outcomes", "maturity", maturityAsOf.value]), queryFn: () => api<MaturityScan>(`/api/decision-outcomes/maturity-scan${maturityAsOf.value ? `?as_of=${maturityAsOf.value}` : ""}`) });
const items = computed(() => cases.data.value?.items || []);
const active = computed(() => selected.value || items.value[0]);
const outcome = computed(() => outcomes.data.value?.items?.find((item) => item.case_id === active.value?.case_id));
const artifactEntries = computed(() => Object.entries(active.value?.artifacts || {}));
const readyItems = computed(() => (maturity.data.value?.items || []).filter((item) => item.status === "ready"));
const allReadySelected = computed(() => readyItems.value.length > 0 && readyItems.value.every((item) => selectedReady.value.includes(item.case_id)));
const summary = computed(() => [
  { label: "案例总数", value: items.value.length, note: "不可变快照" },
  { label: "待人工审批", value: items.value.filter((item) => item.review_status === "pending").length, note: "规则已先行" },
  { label: "成熟待评价", value: maturity.data.value?.counts.ready ?? "—", note: "仍需人工勾选" },
  { label: "已进入跟踪", value: items.value.filter((item) => item.review_status === "approve_for_tracking").length, note: "M8 Shadow" },
]);
watch(readyItems, (rows) => { selectedReady.value = selectedReady.value.filter((id) => rows.some((item) => item.case_id === id)); });

const create = useMutation({
  mutationFn: () => api<DecisionCase>("/api/decision-cases", { method: "POST", ...openApiJsonBody<"DecisionCaseCreate">({ ...form, as_of: form.as_of || null }) }),
  onSuccess: async (data) => { selected.value = data; ui.notify(data.reused ? "已复用相同数据合同的案例" : "决策案例已建立"); await client.invalidateQueries({ queryKey: ["decisions"] }); },
  onError: (error: Error) => ui.notify(error.message),
});
const batchEvaluate = useMutation({
  mutationFn: () => {
    if (!selectedReady.value.length) throw new Error("请先勾选成熟案例");
    return api<BatchEvaluationResult>("/api/decision-outcomes/batch-evaluate", { method: "POST", ...openApiJsonBody<"DecisionOutcomeBatchEvaluate">({ case_ids: selectedReady.value, end_date: maturityAsOf.value || null }) });
  },
  onSuccess: async (data) => {
    batchResult.value = data;
    ui.notify(`批量评价完成：成功 ${data.completed}，失败 ${data.failed}`);
    selectedReady.value = [];
    await Promise.all([client.invalidateQueries({ queryKey: ["decision-outcomes"] }), client.invalidateQueries({ queryKey: ["decision-outcomes", "attribution"] })]);
  },
  onError: (error: Error) => ui.notify(error.message),
});
async function evaluate() {
  if (!active.value) return;
  try {
    await api(`/api/decision-cases/${active.value.case_id}/outcome`, { method: "POST", ...openApiJsonBody<"DecisionOutcomeEvaluate">({}) });
    ui.notify("结果评价已更新");
    await Promise.all([client.invalidateQueries({ queryKey: ["decision-outcomes"] }), client.invalidateQueries({ queryKey: ["decision-outcomes", "attribution"] }), client.invalidateQueries({ queryKey: ["decision-outcomes", "maturity"] })]);
  } catch (error) { ui.notify(error instanceof Error ? error.message : String(error)); }
}
async function submit(decision: OpenApiSchema<"DecisionCaseReview">["decision"]) {
  if (!active.value) return;
  if (review.note.trim().length < 2) { ui.notify("请填写审批说明"); return; }
  try {
    await api(`/api/decision-cases/${active.value.case_id}/reviews`, { method: "POST", ...openApiJsonBody<"DecisionCaseReview">({ decision, reviewer: review.reviewer, note: review.note }) });
    review.note = ""; selected.value = null; ui.notify("人工审批已保存");
    await client.invalidateQueries({ queryKey: ["decisions"] });
  } catch (error) { ui.notify(error instanceof Error ? error.message : String(error)); }
}
function toggleReady() { selectedReady.value = allReadySelected.value ? [] : readyItems.value.map((item) => item.case_id); }
</script>

<template>
  <PageHeader eyebrow="OUTCOME EVALUATION" title="决策案例与结果评价" description="冻结案例合同，确定性规则先行；人工批准只授权进入 Shadow 评价，不授权交易。">
    <label for="decision-code">证券<input id="decision-code" v-model="form.code" /></label><label for="decision-date">截止日<input id="decision-date" v-model="form.as_of" type="date" /></label>
    <label for="decision-horizon">周期<select id="decision-horizon" v-model="form.decision_horizon"><option value="20d">20日</option><option value="60d">60日</option><option value="120d">120日</option><option value="250d">250日</option></select></label>
    <label for="decision-benchmark">基准<select id="decision-benchmark" v-model="form.benchmark_code"><option value="000300.SH">沪深300</option><option value="000905.SH">中证500</option><option value="000852.SH">中证1000</option></select></label>
    <button class="button" :disabled="create.isPending.value" @click="create.mutate()"><ClipboardPlus :size="14" />建立案例</button>
  </PageHeader>
  <MetricStrip :items="summary" />
  <section class="content-band white">
    <div class="section-heading"><div><span>MATURITY SCAN</span><b>成熟案例扫描与批量评价</b></div><small>{{ maturity.data.value?.boundary }}</small></div>
    <div class="control-grid"><label for="maturity-date">评价截止日<input id="maturity-date" v-model="maturityAsOf" type="date" /></label><button class="button secondary" :disabled="maturity.isFetching.value" @click="maturity.refetch()"><ScanSearch :size="14" />重新扫描</button><button class="button" :disabled="!selectedReady.length || batchEvaluate.isPending.value" @click="batchEvaluate.mutate()"><Play :size="14" />评价所选 {{ selectedReady.length }} 项</button></div>
    <AsyncState :loading="maturity.isPending.value" :error="maturity.error.value" @retry="maturity.refetch()"><div class="data-table-wrap"><table class="data-table"><thead><tr><th><input type="checkbox" aria-label="选择全部成熟案例" :checked="allReadySelected" :disabled="!readyItems.length" @change="toggleReady" /></th><th>案例</th><th>证券</th><th>起点</th><th>已观察/目标</th><th>状态</th></tr></thead><tbody><tr v-for="item in maturity.data.value?.items || []" :key="item.case_id"><td><input v-if="item.status==='ready'" v-model="selectedReady" type="checkbox" :value="item.case_id" :aria-label="`选择案例 ${item.case_id}`" /></td><td class="mono">{{ shortId(item.case_id) }}</td><td>{{ item.security_code }}</td><td>{{ item.as_of }}</td><td>{{ item.estimated_observed_trading_days }} / {{ item.horizon_trading_days }}</td><td><span :class="['status',item.status==='ready'?'warn':item.status==='pending'?'':'']">{{ item.status }}</span></td></tr></tbody></table></div></AsyncState>
    <div v-if="batchResult" class="list" style="margin-top:14px"><article v-for="item in batchResult.results" :key="item.case_id" class="list-item"><header><h3>{{ shortId(item.case_id) }}</h3><span :class="['status',item.status==='failed'?'fail':'']">{{ item.status }}</span></header><p v-if="item.status==='ok'">结果状态：{{ item.outcome?.status }}</p><p v-else>{{ item.error }}</p></article></div>
  </section>
  <div class="two-column">
    <aside class="content-band" style="order:0;border-left:0;border-right:1px solid var(--line)"><div class="section-heading"><div><span>CASES</span><b>案例列表</b></div></div><AsyncState :loading="cases.isPending.value" :error="cases.error.value" :empty="!items.length"><div class="list"><button v-for="item in items" :key="item.case_id" class="list-item" style="text-align:left" @click="selected=item"><header><span :class="['status',item.rule_status==='eligible_for_review'?'':'warn']">{{ item.rule_status }}</span><span>{{ item.review_status }}</span></header><h3 style="margin-top:9px">{{ item.security_name }} · {{ item.security_code }}</h3><p>{{ item.as_of }} · {{ item.decision_horizon_days }}日 · {{ item.benchmark_name }}</p></button></div></AsyncState></aside>
    <main class="content-band white"><AsyncState :empty="!active" empty-text="尚未建立决策案例">
      <div class="section-heading"><div><span>FROZEN CASE · {{ shortId(active?.case_id) }}</span><b>{{ active?.security_name }} · {{ active?.security_code }}</b></div><span class="status">{{ active?.rule_status }}</span></div>
      <MetricStrip :items="[{label:'吸引力',value:number(active?.scores?.attractiveness?.score,1),note:'Shadow / 动量'},{label:'证据可信度',value:number(active?.scores?.evidence_confidence?.score,1),note:'事实 / 引用 / 时点'},{label:'风险严重度',value:number(active?.scores?.risk_severity?.score,1),note:'越高风险越大'}]" />
      <div class="section-heading" style="margin-top:24px"><div><span>POLICY GATES</span><b>确定性准入门禁</b></div></div><div class="panel-grid"><article v-for="gate in active?.gates||[]" :key="gate.name" class="panel"><span :class="['status',gate.passed?'':'fail']">{{ gate.passed?'PASS':'STOP' }}</span><h3 style="margin-top:10px">{{ gate.name }}</h3><p>{{ gate.detail }}</p><p class="mono">{{ gate.observed }}</p></article></div>
      <div class="section-heading" style="margin-top:24px"><div><span>FROZEN CONTRACT</span><b>案例数据合同</b></div></div><div class="panel-grid"><article class="panel"><h3>策略版本</h3><p class="mono">{{ active?.policy_version }}</p></article><article v-for="([name,artifact]) in artifactEntries" :key="name" class="panel"><h3>{{ name }}</h3><p class="mono">{{ artifact.snapshot_hash||'缺失' }}</p><p>{{ artifact.schema_version }}</p></article><article class="panel"><h3>Shadow</h3><p class="mono">{{ active?.shadow_snapshot_id||'缺失' }}</p></article></div>
      <div class="section-heading" style="margin-top:24px"><div><span>DECISION OUTCOME</span><b>周期结果</b></div><button class="button secondary" @click="evaluate"><Play :size="13" />评价当前案例</button></div><div v-if="outcome" class="panel-grid"><article class="panel"><h3>状态</h3><p>{{ outcome.status }}</p><p v-if="outcome.status==='pending'">已观察 {{ outcome.metrics?.observed_trading_days||0 }} 日，剩余 {{ outcome.metrics?.remaining_trading_days||0 }} 日</p></article><article class="panel"><h3>证券收益</h3><p>{{ pct(outcome.security_return) }}</p></article><article class="panel"><h3>超额收益</h3><p>{{ pct(outcome.excess_return) }}</p></article><article class="panel"><h3>最大不利波动</h3><p>{{ pct(outcome.maximum_adverse_excursion) }}</p></article></div>
      <div class="section-heading" style="margin-top:24px"><div><span>HUMAN APPROVAL</span><b>人工审批</b></div><span class="status">{{ active?.review_status }}</span></div><form v-if="active?.review_status==='pending'" class="form-grid" @submit.prevent><label for="decision-reviewer">审批人<input id="decision-reviewer" v-model="review.reviewer" /></label><label for="decision-note" class="wide">审批说明<textarea id="decision-note" v-model="review.note" required rows="3" /></label><div class="wide" style="display:flex;gap:8px;flex-wrap:wrap"><button v-if="active?.rule_status==='eligible_for_review'" class="button" @click="submit('approve_for_tracking')">进入 Shadow 跟踪</button><button class="button secondary" @click="submit('return_for_research')">退回研究</button><button class="button danger" @click="submit('reject')">拒绝案例</button></div></form><div v-else class="list"><article v-for="item in active?.reviews||[]" :key="`${item.reviewer}-${item.decision}`" class="list-item"><header><h3>{{ item.reviewer }}</h3><span>{{ item.decision }}</span></header><p>{{ item.note }}</p></article></div>
    </AsyncState>
    <div class="section-heading" style="margin-top:28px"><div><span>CROSS-CASE ATTRIBUTION</span><b>跨案例归因</b></div><small>{{ attribution.data.value?.counts?.completed||0 }} 个完整案例</small></div><MetricStrip :items="[{label:'平均证券收益',value:pct(attribution.data.value?.overall?.mean_security_return),note:'仅完整周期'},{label:'平均超额收益',value:pct(attribution.data.value?.overall?.mean_excess_return),note:'基准调整'},{label:'正超额比例',value:pct(attribution.data.value?.overall?.positive_excess_ratio),note:'样本内'},{label:'平均 MAE',value:pct(attribution.data.value?.overall?.mean_maximum_adverse_excursion),note:'不利波动'}]" /><div class="data-table-wrap"><table class="data-table"><thead><tr><th>维度</th><th>分组</th><th>样本</th><th>平均超额</th><th>正超额比例</th><th>可靠性</th></tr></thead><tbody><template v-for="(groups,dimension) in attribution.data.value?.groups||{}" :key="dimension"><tr v-for="group in groups" :key="`${dimension}-${group.key}`"><td>{{ dimension }}</td><td>{{ group.key }}</td><td>{{ group.count }}</td><td>{{ pct(group.mean_excess_return) }}</td><td>{{ pct(group.positive_excess_ratio) }}</td><td><span :class="['status',group.reliable?'':'warn']">{{ group.reliable?'可观察':'低样本' }}</span></td></tr></template></tbody></table></div>
    </main>
  </div>
</template>
