<script setup lang="ts">
import { computed, defineAsyncComponent, reactive, ref } from "vue";
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { ChartNoAxesCombined, ChevronDown, Database, Play, SlidersHorizontal, Table2 } from "lucide-vue-next";
import { api, openApiJsonBody } from "@/api/client";
import { useUiStore } from "@/stores/ui";
import AsyncState from "@/components/AsyncState.vue";
import MetricStrip from "@/components/MetricStrip.vue";
import PageHeader from "@/components/PageHeader.vue";
import type { ApiList, FactorLabRun, FactorRelease, FactorVersion } from "@/api/types";
import FactorReleaseSection from "./components/FactorReleaseSection.vue";
import ResearchDataGrid from "@/components/ResearchDataGrid.vue";
import type { ColDef } from "ag-grid-community";

const FactorPerspective = defineAsyncComponent(() => import("@/components/FactorPerspective.vue"));

interface AlphaCategory { category_id: string; name: string; count: number; families: string[] }
interface Overview { factor_count?: number; status_counts?: Record<string, number>; strategy_compatible_count?: number; strategy_eligible_count?: number; alpha158?: { feature_count: number; generator_count: number; category_count: number; categories: AlphaCategory[] } }
interface Snapshot { snapshot_id: string; as_of: string }
interface SnapshotValue { security_code: string; security_name?: string; cross_section_rank?: number; raw_value?: number; percentile?: number; factor_id: string; factor_version: number }

const ui = useUiStore();
const client = useQueryClient();
const usage = ref("strategy_component");
const advancedOpen = ref(false);
const asOf = ref("");
const selectedFactor = ref("");
const valueView = ref<"grid" | "perspective">("grid");
const releaseForm = reactive({ acknowledged: false, reviewer: "human" });
const releaseNotes = reactive<Record<string, string>>({});
const overview = useQuery({ queryKey: ["factor-lab", "overview"], queryFn: () => api<Overview>("/api/factor-lab/overview") });
const factors = useQuery({ queryKey: ["factor-lab", "factors"], queryFn: () => api<ApiList<FactorVersion>>("/api/factor-lab/factors") });
const snapshot = useQuery({ queryKey: ["factor-lab", "snapshot"], queryFn: () => api<{ item: Snapshot | null }>("/api/factor-lab/snapshots/latest") });
const evaluation = useQuery({ queryKey: ["factor-lab", "evaluation"], queryFn: () => api<{ item: { run: FactorLabRun } | null }>("/api/factor-lab/evaluations/latest") });
const backtest = useQuery({ queryKey: ["factor-lab", "backtest"], queryFn: () => api<{ item: { run: FactorLabRun } | null }>("/api/factor-lab/backtests/latest") });
const releases = useQuery({ queryKey: ["factor-lab", "releases"], queryFn: () => api<ApiList<FactorRelease>>("/api/factor-lab/releases/latest") });
const snapshotItem = computed(() => snapshot.data.value?.item);
const values = useQuery({
  queryKey: computed(() => ["factor-lab", "values", snapshotItem.value?.snapshot_id, selectedFactor.value]),
  queryFn: () => api<ApiList<SnapshotValue>>(`/api/factor-lab/snapshots/${snapshotItem.value?.snapshot_id}/values?factor_id=${encodeURIComponent(selectedFactor.value)}&limit=1000`),
  enabled: computed(() => Boolean(snapshotItem.value?.snapshot_id && selectedFactor.value)),
});
const valueColumns: ColDef[] = [
  { field: "cross_section_rank", headerName: "排名", sort: "asc", pinned: "left", maxWidth: 96, valueFormatter: ({ value }) => value == null ? "—" : `#${value}` },
  { field: "security_name", headerName: "证券", pinned: "left", minWidth: 130 },
  { field: "security_code", headerName: "代码", minWidth: 112 },
  { field: "raw_value", headerName: "原始值", filter: "agNumberColumnFilter", minWidth: 120 },
  { field: "percentile", headerName: "百分位", filter: "agNumberColumnFilter", valueFormatter: ({ value }) => value == null ? "—" : `${value}%`, minWidth: 110 },
  { field: "factor_id", headerName: "因子", minWidth: 150 },
  { field: "factor_version", headerName: "版本", maxWidth: 86 },
];
const summary = computed(() => [
  { label: "当前模型", value: "Alpha158", note: "LightGBM 冻结特征包" },
  { label: "策略兼容", value: overview.data.value?.strategy_compatible_count ?? "—", note: "字段已经对齐" },
  { label: "正式可用", value: overview.data.value?.strategy_eligible_count ?? "—", note: "通过研究门禁" },
  { label: "已停用", value: overview.data.value?.status_counts?.deprecated ?? "—", note: "仅保留历史" },
]);
const visibleFactors = computed(() => (factors.data.value?.items || []).filter((item) => !usage.value || item.usage_scope === usage.value));
const usageLabel: Record<string, string> = { model_bundle: "模型特征包", strategy_component: "策略兼容", research_only: "仅研究", retired: "已停用" };
const lifecycleLabel: Record<string, string> = { draft: "草稿", testing: "测试中", shadow: "观察中", approved: "已批准", deprecated: "已停用" };
const usageFilters = [
  { value: "strategy_component", label: "策略兼容" },
  { value: "research_only", label: "研究中" },
  { value: "model_bundle", label: "模型包" },
  { value: "retired", label: "已停用" },
  { value: "", label: "全部" },
];
const snapshotFactors = computed(() => (factors.data.value?.items || []).filter((item) => item.usage_scope !== "model_bundle" && item.usage_scope !== "retired"));
const releaseTarget = computed<FactorLabRun | null>(() => {
  const run = backtest.data.value?.item?.run;
  const evaluationRun = evaluation.data.value?.item?.run;
  if (!run?.factor_id || !run.factor_version || !run.evaluation_id || !run.backtest_id || run.evaluation_id !== evaluationRun?.evaluation_id) return null;
  const factor = (factors.data.value?.items || []).find((item) => item.factor_id === run.factor_id && item.version === run.factor_version);
  return factor && (factor.status === "testing" || factor.lifecycle_status === "testing") ? run : null;
});

const createSnapshot = useMutation({
  mutationFn: () => api<Snapshot>("/api/factor-lab/snapshots", { method: "POST", ...openApiJsonBody<"FactorLabSnapshotRequest">({ as_of: asOf.value || null }) }),
  onSuccess: async () => { ui.notify("每日因子快照已生成"); await client.invalidateQueries({ queryKey: ["factor-lab"] }); },
  onError: (error: Error) => ui.notify(error.message),
});
const createRelease = useMutation({
  mutationFn: () => {
    const target = releaseTarget.value;
    if (!target?.factor_id || !target.factor_version || !target.evaluation_id || !target.backtest_id) throw new Error("最近评价与回测未形成完整证据链");
    return api<FactorRelease>("/api/factor-lab/releases", { method: "POST", ...openApiJsonBody<"FactorReleaseCreate">({ factor_id: target.factor_id, factor_version: target.factor_version, evaluation_id: target.evaluation_id, backtest_id: target.backtest_id, limitations_acknowledged: releaseForm.acknowledged, created_by: releaseForm.reviewer }) });
  },
  onSuccess: async (data) => { ui.notify(data.candidate.status === "gate_passed" ? "发布门禁通过，等待人工审批" : "发布门禁存在阻断项"); await client.invalidateQueries({ queryKey: ["factor-lab", "releases"] }); },
  onError: (error: Error) => ui.notify(error.message),
});
async function decideRelease(releaseId: string, decision: "approve" | "reject") {
  const note = (releaseNotes[releaseId] || "").trim();
  if (note.length < 2) { ui.notify("请填写至少 2 个字的审查说明"); return; }
  try {
    await api<FactorRelease>(`/api/factor-lab/releases/${encodeURIComponent(releaseId)}/${decision}`, { method: "POST", ...openApiJsonBody<"FactorReleaseDecision">({ reviewer: releaseForm.reviewer, note }) });
    delete releaseNotes[releaseId];
    ui.notify(decision === "approve" ? "已批准进入 Shadow" : "发布候选已拒绝");
    await Promise.all([client.invalidateQueries({ queryKey: ["factor-lab", "releases"] }), client.invalidateQueries({ queryKey: ["factor-lab"] })]);
  } catch (error) { ui.notify(error instanceof Error ? error.message : String(error)); }
}
async function moveToTesting(item: FactorVersion) {
  try {
    await api(`/api/factor-lab/factors/${encodeURIComponent(item.factor_id)}/versions/${item.version}/status`, { method: "POST", ...openApiJsonBody<"FactorLifecycleChange">({ to_status: "testing", reviewer: "human", note: "Vue 工作台提交测试" }) });
    ui.notify("因子已进入测试状态"); await client.invalidateQueries({ queryKey: ["factor-lab"] });
  } catch (error) { ui.notify(error instanceof Error ? error.message : String(error)); }
}
</script>

<template>
  <PageHeader eyebrow="FACTOR REGISTRY" title="因子库" description="看清哪些因子用于模型，哪些因子可以进入策略。">
    <button class="button secondary" :class="{ active: advancedOpen }" @click="advancedOpen = !advancedOpen"><SlidersHorizontal :size="15" />{{ advancedOpen ? '收起高级工具' : '高级工具' }}</button>
  </PageHeader>
  <MetricStrip :items="summary" />
  <section class="content-band verdict-band">
    <div class="verdict-copy"><span>CURRENT CONCLUSION</span><h2>模型用 Alpha158，策略使用独立因子</h2><p>Alpha158 的 158 个特征只服务于 LightGBM。当前有 5 个独立因子已接通策略字段，但尚未完成正式批准。</p></div>
    <details class="taxonomy"><summary>查看 Alpha158 的 8 类构成 <ChevronDown :size="15" /></summary><div class="alpha-grid"><article v-for="item in overview.data.value?.alpha158?.categories || []" :key="item.category_id"><b>{{ item.name }}</b><strong>{{ item.count }}</strong><small>{{ item.families.join(' · ') }}</small></article></div></details>
  </section>
  <section class="content-band white registry-band">
      <div class="section-heading"><div><span>FACTOR CATALOG</span><b>独立因子</b></div><div class="usage-tabs" aria-label="因子用途"><button v-for="item in usageFilters" :key="item.value" :class="{ active: usage === item.value }" @click="usage = item.value">{{ item.label }}</button></div></div>
      <AsyncState :loading="factors.isPending.value" :error="factors.error.value" :empty="!visibleFactors.length" @retry="factors.refetch()">
        <div class="factor-table"><div class="factor-table-head"><span>因子</span><span>用途</span><span>状态</span><span>策略字段</span></div><article v-for="item in visibleFactors" :key="`${item.factor_id}-${item.version}`" class="factor-row"><div class="factor-name"><b>{{ item.name }}</b><small>{{ item.factor_id }} · v{{ item.version }}</small></div><span class="plain-label">{{ usageLabel[item.usage_scope || 'research_only'] }}</span><span :class="['status', (item.lifecycle_status || item.status) === 'draft' ? 'warn' : '']">{{ lifecycleLabel[item.lifecycle_status || item.status || ''] || item.lifecycle_status }}</span><div class="runtime-field"><code v-if="item.strategy_runtime_field">{{ item.strategy_runtime_field }}</code><span v-else>—</span><small v-if="item.strategy_compatible && !item.strategy_eligible">待批准</small></div><details><summary>查看定义 <ChevronDown :size="13" /></summary><p>{{ item.description }}</p><code>{{ item.expression || item.formula }}</code><button v-if="(item.lifecycle_status || item.status) === 'draft'" class="button secondary" @click="moveToTesting(item)"><Play :size="13" />进入测试</button></details></article></div>
      </AsyncState>
  </section>
  <template v-if="advancedOpen">
    <section class="content-band advanced-band">
      <div class="section-heading"><div><span>DAILY SNAPSHOT</span><b>横截面数据</b></div><div class="snapshot-actions"><label for="factor-snapshot-date">日期<input id="factor-snapshot-date" v-model="asOf" type="date" /></label><button class="button" :disabled="createSnapshot.isPending.value" @click="createSnapshot.mutate()"><Database :size="15" />计算快照</button></div></div>
      <div class="section-heading" style="margin-top:26px"><div><span>SNAPSHOT VALUES</span><b>横截面排名与交互透视</b></div><div class="snapshot-tools"><label for="factor-selection" class="sr-only">选择因子</label><select id="factor-selection" v-model="selectedFactor"><option value="">选择因子</option><option v-for="item in snapshotFactors" :key="`${item.factor_id}-${item.version}`" :value="item.factor_id">{{ item.name }} · v{{ item.version }}</option></select><div class="segmented" aria-label="横截面显示方式"><button :class="{active:valueView==='grid'}" title="数据表" @click="valueView='grid'"><Table2 :size="14" /></button><button :class="{active:valueView==='perspective'}" title="交互透视" @click="valueView='perspective'"><ChartNoAxesCombined :size="14" /></button></div></div></div>
      <AsyncState :loading="values.isFetching.value" :error="values.error.value" :empty="!selectedFactor" empty-text="请先选择要查看的因子"><ResearchDataGrid v-if="valueView==='grid'" :rows="values.data.value?.items || []" :columns="valueColumns" row-id="security_code" :height="520" empty-text="该因子没有横截面数据" /><Suspense v-else><FactorPerspective :rows="values.data.value?.items || []" /><template #fallback><p class="perspective-loading">正在加载交互透视引擎…</p></template></Suspense></AsyncState>
    </section>
    <FactorReleaseSection :evaluation="evaluation.data.value?.item?.run" :backtest="backtest.data.value?.item?.run" :releases="releases.data.value?.items || []" :release-target="releaseTarget" :release-form="releaseForm" :release-notes="releaseNotes" :creating="createRelease.isPending.value" @create="createRelease.mutate()" @decide="decideRelease" />
  </template>
</template>

<style scoped>
.verdict-band { display:grid;grid-template-columns:minmax(0,1.5fr) minmax(260px,.8fr);gap:28px;align-items:center;border-bottom:1px solid var(--line);background:#f4f6f2; }
.verdict-copy span{color:var(--teal);font-size:8px;font-weight:800}.verdict-copy h2{margin:6px 0 7px;font-size:18px;letter-spacing:0}.verdict-copy p{max-width:720px;margin:0;color:var(--muted)}
.taxonomy{border-left:1px solid var(--line);padding-left:24px}.taxonomy summary,.factor-row details summary{display:flex;align-items:center;justify-content:space-between;cursor:pointer;font-weight:700;list-style:none}.taxonomy[open] summary svg,.factor-row details[open] summary svg{transform:rotate(180deg)}
.alpha-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); margin-top:14px;border: 1px solid var(--line); background: white; }
.alpha-grid article { min-width: 0; padding: 10px; border-right: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.alpha-grid article:nth-child(4n) { border-right: 0; }
.alpha-grid article:nth-child(2n) { border-right: 0; }
.alpha-grid article:nth-last-child(-n+4) { border-bottom: 0; }
.alpha-grid b,.alpha-grid strong,.alpha-grid small { display: block; }
.alpha-grid b{font-size:9px}.alpha-grid strong { margin: 2px 0; font-size: 15px; }
.alpha-grid small { overflow: hidden; color: var(--muted); text-overflow: ellipsis; white-space: nowrap; }
.usage-tabs{display:flex;border-bottom:1px solid var(--line)}.usage-tabs button{padding:8px 11px;border:0;border-bottom:2px solid transparent;background:transparent;color:var(--muted);font-size:9px}.usage-tabs button.active{border-bottom-color:var(--teal);color:var(--ink);font-weight:800}
.factor-table{border:1px solid var(--line)}.factor-table-head,.factor-row{display:grid;grid-template-columns:minmax(190px,1.4fr) minmax(100px,.7fr) minmax(90px,.6fr) minmax(150px,1fr);align-items:center}.factor-table-head{padding:8px 14px;border-bottom:1px solid var(--line);background:#f4f5f1;color:var(--muted);font-size:8px;font-weight:800}.factor-row{position:relative;min-height:58px;padding:9px 14px;border-bottom:1px solid var(--line)}.factor-row:last-child{border-bottom:0}.factor-name b,.factor-name small,.runtime-field small{display:block}.factor-name small,.runtime-field small,.plain-label{margin-top:3px;color:var(--muted)}.runtime-field small{color:#a66622}.factor-row>details{grid-column:1/-1;padding-top:0}.factor-row>details summary{position:absolute;right:14px;top:20px;width:94px;color:var(--muted);font-size:8px}.factor-row>details p{margin:12px 0 5px;color:var(--muted)}.factor-row>details>code{display:block;padding:9px;background:#f5f6f2}.factor-row>details .button{margin-top:9px}
.advanced-band{border-top:1px solid var(--line);background:white}.snapshot-actions,.snapshot-tools { display: flex; align-items: end; gap: 8px; }.snapshot-actions label{display:grid;gap:4px;color:var(--muted);font-size:8px;font-weight:700}
.segmented { display: inline-grid; grid-template-columns: repeat(2, 34px); border: 1px solid var(--line-dark); }
.segmented button { width: 34px; height: 34px; display: grid; place-items: center; padding: 0; border: 0; border-right: 1px solid var(--line-dark); color: var(--muted); background: white; }
.segmented button:last-child { border-right: 0; }
.segmented button.active { color: white; background: var(--ink); }
.perspective-loading { height: 520px; display: grid; place-items: center; margin: 0; border: 1px solid var(--line); color: var(--muted); }
@media (max-width: 900px) { .verdict-band{grid-template-columns:1fr}.taxonomy{padding:16px 0 0;border-left:0;border-top:1px solid var(--line)} }
@media (max-width: 700px) { .verdict-copy h2{max-width:310px;font-size:16px;overflow-wrap:anywhere}.verdict-copy p{overflow-wrap:anywhere}.snapshot-tools,.snapshot-actions { width: 100%; align-items: stretch;flex-wrap:wrap } .snapshot-tools select { min-width: 0; flex: 1; }.usage-tabs{width:100%;overflow-x:auto}.factor-table-head{display:none}.factor-row{grid-template-columns:1fr auto;gap:5px 12px;padding:12px 14px}.factor-row>.plain-label,.factor-row>.status{grid-column:2}.runtime-field{grid-column:1}.factor-row>details summary{top:auto;bottom:14px}.alpha-grid{grid-template-columns:1fr}.alpha-grid article{border-right:0!important;border-bottom:1px solid var(--line)!important}.alpha-grid article:last-child{border-bottom:0!important} }
</style>
