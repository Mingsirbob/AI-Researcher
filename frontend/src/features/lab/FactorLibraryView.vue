<script setup lang="ts">
import { computed, reactive, ref } from "vue";
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { Database, Play } from "lucide-vue-next";
import { api, openApiJsonBody } from "@/api/client";
import { shortId } from "@/lib/format";
import { useUiStore } from "@/stores/ui";
import AsyncState from "@/components/AsyncState.vue";
import MetricStrip from "@/components/MetricStrip.vue";
import PageHeader from "@/components/PageHeader.vue";
import type { ApiList, FactorLabRun, FactorRelease, FactorVersion } from "@/api/types";
import FactorReleaseSection from "./components/FactorReleaseSection.vue";

interface Overview { factor_count?: number; status_counts?: Record<string, number> }
interface Snapshot { snapshot_id: string; as_of: string }
interface SnapshotValue { security_code: string; security_name?: string; cross_section_rank?: number; raw_value?: number; percentile?: number; factor_id: string; factor_version: number }

const ui = useUiStore();
const client = useQueryClient();
const status = ref("");
const asOf = ref("");
const selectedFactor = ref("");
const releaseForm = reactive({ acknowledged: false, reviewer: "human" });
const releaseNotes = reactive<Record<string, string>>({});
const overview = useQuery({ queryKey: ["factor-lab", "overview"], queryFn: () => api<Overview>("/api/factor-lab/overview") });
const factors = useQuery({ queryKey: computed(() => ["factor-lab", "factors", status.value]), queryFn: () => api<ApiList<FactorVersion>>(`/api/factor-lab/factors${status.value ? `?status=${status.value}` : ""}`) });
const snapshot = useQuery({ queryKey: ["factor-lab", "snapshot"], queryFn: () => api<{ item: Snapshot | null }>("/api/factor-lab/snapshots/latest") });
const evaluation = useQuery({ queryKey: ["factor-lab", "evaluation"], queryFn: () => api<{ item: { run: FactorLabRun } | null }>("/api/factor-lab/evaluations/latest") });
const backtest = useQuery({ queryKey: ["factor-lab", "backtest"], queryFn: () => api<{ item: { run: FactorLabRun } | null }>("/api/factor-lab/backtests/latest") });
const releases = useQuery({ queryKey: ["factor-lab", "releases"], queryFn: () => api<ApiList<FactorRelease>>("/api/factor-lab/releases/latest") });
const snapshotItem = computed(() => snapshot.data.value?.item);
const values = useQuery({
  queryKey: computed(() => ["factor-lab", "values", snapshotItem.value?.snapshot_id, selectedFactor.value]),
  queryFn: () => api<ApiList<SnapshotValue>>(`/api/factor-lab/snapshots/${snapshotItem.value?.snapshot_id}/values?factor_id=${encodeURIComponent(selectedFactor.value)}&limit=100`),
  enabled: computed(() => Boolean(snapshotItem.value?.snapshot_id && selectedFactor.value)),
});
const summary = computed(() => [
  { label: "注册因子", value: overview.data.value?.factor_count ?? factors.data.value?.items?.length ?? "—", note: "包含所有状态" },
  { label: "测试中", value: overview.data.value?.status_counts?.testing ?? "—", note: "尚未进入模型" },
  { label: "Shadow", value: overview.data.value?.status_counts?.shadow ?? "—", note: "只观察不交易" },
  { label: "最近快照", value: snapshotItem.value?.as_of || "—", note: shortId(snapshotItem.value?.snapshot_id) },
]);
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
  <PageHeader eyebrow="FACTOR REGISTRY" title="因子库与发布" description="统一查看因子版本、生命周期、每日横截面快照以及评价和回测证据。">
    <label for="factor-snapshot-date">快照日期<input id="factor-snapshot-date" v-model="asOf" type="date" /></label>
    <button class="button" :disabled="createSnapshot.isPending.value" @click="createSnapshot.mutate()"><Database :size="15" />计算每日快照</button>
  </PageHeader>
  <MetricStrip :items="summary" />
  <div class="two-column">
    <section class="content-band white">
      <div class="section-heading"><div><span>FACTOR VERSIONS</span><b>因子注册表</b></div><label for="factor-status">状态 <select id="factor-status" v-model="status"><option value="">全部</option><option value="draft">草稿</option><option value="testing">测试中</option><option value="shadow">Shadow</option><option value="approved">批准</option><option value="deprecated">停用</option></select></label></div>
      <AsyncState :loading="factors.isPending.value" :error="factors.error.value" :empty="!factors.data.value?.items?.length" @retry="factors.refetch()">
        <div class="list"><article v-for="item in factors.data.value?.items || []" :key="`${item.factor_id}-${item.version}`" class="list-item"><header><div><span :class="['status', item.status === 'draft' ? 'warn' : '']">{{ item.lifecycle_status || item.status }}</span><h3 style="margin-top:8px">{{ item.name }} <small>v{{ item.version }}</small></h3></div><code>{{ item.factor_id }}</code></header><p>{{ item.description }}</p><p class="mono">{{ item.expression || item.formula }}</p><footer><button v-if="(item.lifecycle_status || item.status) === 'draft'" class="button secondary" @click="moveToTesting(item)"><Play :size="13" />进入测试</button><span class="muted">{{ item.template_id }} · {{ item.direction }}</span></footer></article></div>
      </AsyncState>
      <div class="section-heading" style="margin-top:26px"><div><span>SNAPSHOT VALUES</span><b>横截面排名</b></div><label for="factor-selection" class="sr-only">选择因子</label><select id="factor-selection" v-model="selectedFactor"><option value="">选择因子</option><option v-for="item in factors.data.value?.items || []" :key="`${item.factor_id}-${item.version}`" :value="item.factor_id">{{ item.name }} · v{{ item.version }}</option></select></div>
      <AsyncState :loading="values.isFetching.value" :error="values.error.value" :empty="!selectedFactor" empty-text="请先选择要查看的因子"><div class="data-table-wrap"><table class="data-table"><thead><tr><th>排名</th><th>证券</th><th>原始值</th><th>百分位</th><th>版本</th></tr></thead><tbody><tr v-for="item in values.data.value?.items || []" :key="item.security_code"><td>#{{ item.cross_section_rank }}</td><td><b>{{ item.security_name || item.security_code }}</b><br><small>{{ item.security_code }}</small></td><td>{{ item.raw_value }}</td><td>{{ item.percentile }}%</td><td><code>{{ item.factor_id }}@v{{ item.factor_version }}</code></td></tr></tbody></table></div></AsyncState>
    </section>
    <FactorReleaseSection :evaluation="evaluation.data.value?.item?.run" :backtest="backtest.data.value?.item?.run" :releases="releases.data.value?.items || []" :release-target="releaseTarget" :release-form="releaseForm" :release-notes="releaseNotes" :creating="createRelease.isPending.value" @create="createRelease.mutate()" @decide="decideRelease" />
  </div>
</template>
