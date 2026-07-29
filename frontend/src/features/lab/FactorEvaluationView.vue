<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { Play } from "lucide-vue-next";
import { api, jsonBody } from "@/api/client";
import { number, pct, shortId } from "@/lib/format";
import { useUiStore } from "@/stores/ui";
import PageHeader from "@/components/PageHeader.vue";
import MetricStrip from "@/components/MetricStrip.vue";
import AsyncState from "@/components/AsyncState.vue";

interface FactorMetric {
  factor_id: string;
  factor_version: number;
  factor_name: string;
  direction: string;
  horizon: number;
  observation_count: number;
  average_security_count: number;
  mean_rank_ic: number | null;
  rank_icir: number | null;
  positive_ic_ratio: number | null;
  top_layer_turnover: number | null;
  mean_layer_spread: number | null;
  layer_returns: Record<string, number>;
}
interface FactorCorrelation { left_factor_id: string; right_factor_id: string; mean_rank_correlation: number }
interface EvaluationRun { evaluation_id?: string; requested_start_date?: string; requested_end_date?: string; effective_start_date?: string; effective_end_date?: string; rebalance_step?: number; adjustment?: string; factor_count?: number; rebalance_count?: number; universe?: string; result_hash?: string }
interface EvaluationData { run: EvaluationRun; metrics: FactorMetric[]; correlations: FactorCorrelation[] }

const ui = useUiStore();
const client = useQueryClient();
const selectedFactor = ref("");
const form = reactive({ start_date: "2021-01-01", end_date: "", rebalance_step: 20, horizons: [1, 5, 20], layer_count: 5 });
const latest = useQuery({ queryKey: ["factor-lab", "evaluation"], queryFn: () => api<{ item: EvaluationData | null }>("/api/factor-lab/evaluations/latest") });
const data = computed(() => latest.data.value?.item);
const factorOptions = computed(() => {
  const options = new Map<string, FactorMetric>();
  for (const item of data.value?.metrics || []) if (!options.has(item.factor_id)) options.set(item.factor_id, item);
  return [...options.values()];
});

watch(factorOptions, (options) => {
  if (!options.some((item) => item.factor_id === selectedFactor.value)) selectedFactor.value = options[0]?.factor_id || "";
}, { immediate: true });
watch(data, (evaluation) => {
  const run = evaluation?.run;
  if (!run) return;
  form.start_date = run.requested_start_date || form.start_date;
  form.end_date = run.requested_end_date || "";
  form.rebalance_step = run.rebalance_step || form.rebalance_step;
}, { immediate: true });

const selectedOption = computed(() => factorOptions.value.find((item) => item.factor_id === selectedFactor.value));
const selectedMetrics = computed(() => (data.value?.metrics || [])
  .filter((item) => item.factor_id === selectedFactor.value)
  .sort((left, right) => left.horizon - right.horizon));
const primary = computed(() => selectedMetrics.value.find((item) => item.horizon === 20) || selectedMetrics.value.at(-1));
const best = computed(() => [...selectedMetrics.value].filter((item) => item.mean_rank_ic != null)
  .sort((left, right) => Number(right.mean_rank_ic) - Number(left.mean_rank_ic))[0]);
const correlations = computed(() => (data.value?.correlations || [])
  .filter((item) => item.left_factor_id !== item.right_factor_id && (item.left_factor_id === selectedFactor.value || item.right_factor_id === selectedFactor.value))
  .sort((left, right) => Math.abs(right.mean_rank_correlation) - Math.abs(left.mean_rank_correlation)));
const strongestCorrelation = computed(() => correlations.value[0]);
const strongestOther = computed(() => strongestCorrelation.value ? otherFactorName(strongestCorrelation.value) : "没有其他可比因子");
const layerEntries = computed(() => Object.entries(primary.value?.layer_returns || {}));
const maxLayerAbs = computed(() => Math.max(...layerEntries.value.map(([, value]) => Math.abs(Number(value))), 0.000001));
const summary = computed(() => [
  { label: `${primary.value?.horizon || 20}日 Rank IC`, value: number(primary.value?.mean_rank_ic, 4), note: `${primary.value?.observation_count || "—"} 个评价截面` },
  { label: `${primary.value?.horizon || 20}日 ICIR`, value: number(primary.value?.rank_icir, 3), note: "年化，采样间隔已校正" },
  { label: "高低层收益差", value: pct(primary.value?.mean_layer_spread), note: "最高层减最低层" },
  { label: "最佳周期", value: best.value ? `${best.value.horizon} 日` : "—", note: `Rank IC ${number(best.value?.mean_rank_ic, 4)}` },
  { label: "Top层换手", value: pct(primary.value?.top_layer_turnover), note: "相邻截面的成员变化" },
  { label: "最高因子相关", value: number(strongestCorrelation.value?.mean_rank_correlation, 3), note: strongestOther.value },
]);

function otherFactorName(item: FactorCorrelation) {
  const id = item.left_factor_id === selectedFactor.value ? item.right_factor_id : item.left_factor_id;
  return factorOptions.value.find((factor) => factor.factor_id === id)?.factor_name || id;
}

const run = useMutation({
  mutationFn: () => api("/api/factor-lab/evaluations", { method: "POST", ...jsonBody({ ...form, end_date: form.end_date || null }) }),
  onSuccess: async () => { ui.notify("因子评价完成"); await client.invalidateQueries({ queryKey: ["factor-lab", "evaluation"] }); },
  onError: (error) => ui.notify(error instanceof Error ? error.message : String(error)),
});
</script>

<template>
  <PageHeader eyebrow="FACTOR EVALUATION" title="因子评价" description="评价批次计算全部测试中因子；下方选择一个因子，查看它在不同预测周期的有效性、分层收益和相关性。">
    <label for="evaluation-start">开始日期<input id="evaluation-start" v-model="form.start_date" type="date" /></label>
    <label for="evaluation-end">结束日期<input id="evaluation-end" v-model="form.end_date" type="date" /></label>
    <label for="evaluation-step">截面间隔<select id="evaluation-step" v-model.number="form.rebalance_step"><option :value="5">5日</option><option :value="20">20日</option><option :value="60">60日</option></select></label>
    <button class="button" :disabled="run.isPending.value" @click="run.mutate()"><Play :size="14" />{{ run.isPending.value ? "正在评价" : "运行评价" }}</button>
  </PageHeader>

  <AsyncState :loading="latest.isPending.value" :error="latest.error.value" :empty="!data" @retry="latest.refetch()">
    <section class="factor-focus">
      <label for="evaluation-factor"><span>查看评价因子</span><select id="evaluation-factor" v-model="selectedFactor" data-testid="evaluation-factor-select"><option v-for="item in factorOptions" :key="item.factor_id" :value="item.factor_id">{{ item.factor_name }} · {{ item.factor_id }}@v{{ item.factor_version }}</option></select></label>
      <div><span class="eyebrow">ACTIVE FACTOR</span><b>{{ selectedOption?.factor_name || "尚无评价因子" }}</b><small>{{ selectedOption?.direction === "negative" ? "原始值越小越好" : "原始值越大越好" }} · {{ selectedMetrics.length }} 个预测周期</small></div>
      <div class="factor-contract-stamp"><span>{{ data?.run?.adjustment }}</span><b>{{ data?.run?.factor_count }} 因子 · {{ data?.run?.rebalance_count }} 截面</b></div>
    </section>
    <MetricStrip :items="summary" />

    <section class="content-band white">
      <div class="evaluation-detail-layout">
        <section>
          <div class="section-heading"><div><span>IC DECAY</span><b>{{ selectedOption?.factor_name }}周期衰减</b></div><small>{{ shortId(data?.run?.evaluation_id) }}</small></div>
          <div class="data-table-wrap"><table class="data-table"><thead><tr><th>预测周期</th><th>Rank IC</th><th>ICIR</th><th>正 IC 占比</th><th>Top 换手</th><th>样本</th></tr></thead><tbody><tr v-for="item in selectedMetrics" :key="item.horizon"><td><b>{{ item.horizon }} 日</b></td><td>{{ number(item.mean_rank_ic, 4) }}</td><td>{{ number(item.rank_icir, 3) }}</td><td>{{ pct(item.positive_ic_ratio) }}</td><td>{{ pct(item.top_layer_turnover) }}</td><td>{{ item.observation_count }} × {{ Math.round(item.average_security_count) }}</td></tr></tbody></table></div>
        </section>
        <section>
          <div class="section-heading"><div><span>LAYER RETURN</span><b>{{ primary?.horizon || 20 }}日五分层平均收益</b></div></div>
          <div class="layer-return-list"><div v-for="([layer, value]) in layerEntries" :key="layer" class="layer-return-row"><span>第{{ layer }}层</span><div><i :class="{ negative: Number(value) < 0 }" :style="{ width: `${Math.max(2, Math.abs(Number(value)) / maxLayerAbs * 100)}%` }"></i></div><b>{{ pct(value) }}</b></div></div>
        </section>
        <section>
          <div class="section-heading"><div><span>CORRELATION</span><b>与其他因子的相关性</b></div></div>
          <div class="correlation-list"><div v-for="item in correlations.slice(0, 10)" :key="`${item.left_factor_id}-${item.right_factor_id}`"><span>{{ otherFactorName(item) }}</span><b>{{ number(item.mean_rank_correlation, 3) }}</b></div><p v-if="!correlations.length" class="muted">没有其他因子的共同截面。</p></div>
        </section>
      </div>
      <div class="evaluation-contract-line"><span>有效区间 <b>{{ data?.run?.effective_start_date }} → {{ data?.run?.effective_end_date }}</b></span><span>股票池 <b>{{ data?.run?.universe }}</b></span><span>结果哈希 <code>{{ shortId(data?.run?.result_hash) }}</code></span></div>
    </section>
  </AsyncState>
</template>
