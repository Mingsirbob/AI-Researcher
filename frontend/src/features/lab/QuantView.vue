<script setup lang="ts">
import { computed, reactive, ref } from "vue";
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { Plus, RefreshCw, Scale, Trash2 } from "lucide-vue-next";
import { api, jsonBody } from "@/api/client";
import { number, pct } from "@/lib/format";
import { useUiStore } from "@/stores/ui";
import AsyncState from "@/components/AsyncState.vue";
import MetricStrip from "@/components/MetricStrip.vue";
import PageHeader from "@/components/PageHeader.vue";
import type { ApiList, FactorRow, FactorRowsResponse, NeutralizationResult, UniverseMembership } from "@/api/types";

interface FeatureSnapshot { snapshot_id: string; as_of: string; status?: string; passed_securities?: number; excluded_securities?: number; factor_version?: string }
interface CandidateRow extends FactorRow { source_snapshot_id?: string }

const ui = useUiStore();
const client = useQueryClient();
const asOf = ref("");
const universe = ref("");
const neutralization = ref<NeutralizationResult | null>(null);
const filters = reactive({ q: "", exchange: "all", quality_status: "passed", sort: "return_60d", direction: "desc" });
const thresholds = reactive({ min_return_20d: "", min_return_60d: "", max_volatility_60d: "", min_avg_traded_value_20d: "" });
const snapshot = useQuery({ queryKey: ["quant", "snapshot"], queryFn: () => api<FeatureSnapshot>("/api/factor-snapshots/latest"), retry: false });
const candidates = useQuery({ queryKey: ["quant", "candidates"], queryFn: () => api<ApiList<CandidateRow>>("/api/research-candidates") });
const snapshotId = computed(() => snapshot.data.value?.snapshot_id);
const contractDate = computed(() => asOf.value || snapshot.data.value?.as_of || "");
const membership = useQuery({
  queryKey: computed(() => ["quant", "membership", contractDate.value, universe.value]),
  queryFn: () => api<UniverseMembership>(`/api/market/universe-membership?as_of=${contractDate.value}${universe.value ? `&universe=${encodeURIComponent(universe.value)}` : ""}`),
  enabled: computed(() => Boolean(contractDate.value)),
});
const rows = useQuery({
  queryKey: computed(() => ["quant", "rows", snapshotId.value, { ...filters }, { ...thresholds }]),
  queryFn: () => {
    const params = new URLSearchParams({ ...filters, limit: "100" });
    Object.entries(thresholds).forEach(([key, value]) => { if (value !== "") params.set(key, value); });
    return api<FactorRowsResponse>(`/api/factor-snapshots/${snapshotId.value}/securities?${params}`);
  },
  enabled: computed(() => Boolean(snapshotId.value)),
});
const currentRows = computed(() => rows.data.value?.items || []);
const neutralizationReady = computed(() => currentRows.value.filter((item) => item.return_60d != null && item.market_cap != null && item.market_cap > 0 && Boolean(item.industry_l1)).length);
const metrics = computed(() => [
  { label: "快照截止日", value: snapshot.data.value?.as_of || "—", note: snapshot.data.value?.status || "尚未生成" },
  { label: "正常排名", value: snapshot.data.value?.passed_securities ?? "—", note: "通过质量门禁" },
  { label: "成分记录", value: membership.data.value?.items.length ?? "—", note: membership.data.value?.history_contract || "等待时点合同" },
  { label: "中性化就绪", value: neutralizationReady.value, note: `当前表格 ${currentRows.value.length} 行` },
]);
const generate = useMutation({
  mutationFn: () => api<FeatureSnapshot>("/api/factor-snapshots", { method: "POST", ...jsonBody({ as_of: asOf.value || null }) }),
  onSuccess: async (data) => { asOf.value = data.as_of; neutralization.value = null; ui.notify("全市场特征快照已完成"); await client.invalidateQueries({ queryKey: ["quant"] }); },
  onError: (error: Error) => ui.notify(error.message),
});
const runNeutralization = useMutation({
  mutationFn: () => api<NeutralizationResult>("/api/quant/neutralize", { method: "POST", ...jsonBody({ rows: currentRows.value.map((item) => ({ ...item, factor_value: item.return_60d })), factor_key: "factor_value", industry_key: "industry_l1", market_cap_key: "market_cap" }) }),
  onSuccess: (data) => { neutralization.value = data; ui.notify(`中性化完成：有效 ${data.items.length}，隔离 ${data.excluded.length}`); },
  onError: (error: Error) => ui.notify(error.message),
});
async function candidate(item: FactorRow) {
  try {
    if (item.in_candidate_pool) await api(`/api/research-candidates/${encodeURIComponent(item.security_code)}`, { method: "DELETE" });
    else await api("/api/research-candidates", { method: "POST", ...jsonBody({ code: item.security_code, snapshot_id: snapshotId.value }) });
    await Promise.all([client.invalidateQueries({ queryKey: ["quant", "rows"] }), client.invalidateQueries({ queryKey: ["quant", "candidates"] })]);
  } catch (error) { ui.notify(error instanceof Error ? error.message : String(error)); }
}
</script>

<template>
  <PageHeader eyebrow="DATA & FEATURES" title="数据与特征" description="确认行情覆盖、指数成分时点合同和质量隔离后，再查看基础特征扫描。这里不代表正式模型选股。"><label for="quant-date">截止日<input id="quant-date" v-model="asOf" type="date" /></label><button class="button" :disabled="generate.isPending.value" @click="generate.mutate()"><RefreshCw :size="14" />生成快照</button></PageHeader>
  <MetricStrip :items="metrics" />
  <section class="content-band white">
    <div class="section-heading"><div><span>UNIVERSE CONTRACT</span><b>指数成分快照</b></div><small>{{ membership.data.value?.source || '等待数据源' }}</small></div>
    <div class="control-grid"><label for="universe-filter">成分范围<input id="universe-filter" v-model="universe" placeholder="例如 CSI300；留空查看全部" /></label><div class="panel"><h3>查询时点</h3><p>{{ contractDate || '—' }}</p></div><div class="panel"><h3>历史合同</h3><p>{{ membership.data.value?.history_contract || '—' }}</p></div><div class="panel"><h3>数据来源</h3><p>{{ membership.data.value?.source || '—' }}</p></div></div>
    <AsyncState :loading="membership.isFetching.value" :error="membership.error.value" :empty="!contractDate" empty-text="请先选择或生成一个截止日"><div class="data-table-wrap"><table class="data-table"><thead><tr><th>证券</th><th>成分</th><th>生效起点</th><th>生效终点</th><th>来源</th></tr></thead><tbody><tr v-for="item in membership.data.value?.items || []" :key="`${item.security_code}-${item.universe || item.universe_name}`"><td><b>{{ item.security_name || item.security_code }}</b><br><small>{{ item.security_code }}</small></td><td>{{ item.universe || item.universe_name }}</td><td>{{ item.effective_from || item.universe_as_of || '—' }}</td><td>{{ item.effective_to || '持续有效' }}</td><td>{{ item.universe_source || membership.data.value?.source }}</td></tr><tr v-if="!membership.data.value?.items.length"><td colspan="5">该时点没有可用的成分记录。</td></tr></tbody></table></div></AsyncState>
  </section>
  <div class="two-column"><section class="content-band white"><div class="control-grid"><label for="quant-search">搜索<input id="quant-search" v-model="filters.q" placeholder="代码或简称" /></label><label for="quant-exchange">市场<select id="quant-exchange" v-model="filters.exchange"><option value="all">全部</option><option value="SZ">深市</option><option value="SH">沪市</option><option value="BJ">北交所</option></select></label><label for="quant-quality">质量<select id="quant-quality" v-model="filters.quality_status"><option value="passed">正常</option><option value="excluded">隔离</option><option value="all">全部</option></select></label><label for="quant-sort">排序<select id="quant-sort" v-model="filters.sort"><option value="return_60d">60日收益</option><option value="return_20d">20日收益</option><option value="volatility_60d">波动率</option><option value="avg_traded_value_20d">成交额</option></select></label><label for="return-20">20日收益下限<input id="return-20" v-model="thresholds.min_return_20d" type="number" step="0.01" placeholder="不限制" /></label><label for="return-60">60日收益下限<input id="return-60" v-model="thresholds.min_return_60d" type="number" step="0.01" placeholder="不限制" /></label><label for="volatility-60">60日波动上限<input id="volatility-60" v-model="thresholds.max_volatility_60d" type="number" step="0.01" placeholder="不限制" /></label><label for="traded-value-20">20日成交额下限<input id="traded-value-20" v-model="thresholds.min_avg_traded_value_20d" type="number" step="1000000" placeholder="不限制" /></label></div>
    <div class="section-heading" style="margin-top:20px"><div><span>CROSS SECTION</span><b>因子排名</b></div><small>{{ rows.data.value?.total || 0 }} 只证券</small></div><AsyncState :loading="rows.isFetching.value" :error="rows.error.value" :empty="!snapshotId" empty-text="尚未生成全市场快照"><div class="data-table-wrap"><table class="data-table"><thead><tr><th>证券</th><th>20日</th><th>60日</th><th>波动</th><th>行业</th><th>市值</th><th>动作</th></tr></thead><tbody><tr v-for="item in currentRows" :key="item.security_code"><td><b>{{ item.security_name || item.security_code }}</b><br><small>{{ item.security_code }}</small></td><td>{{ pct(item.return_20d) }}</td><td>{{ pct(item.return_60d) }}</td><td>{{ pct(item.volatility_60d) }}</td><td>{{ item.industry_l1 || '缺失' }}</td><td>{{ item.market_cap == null ? '缺失' : number(item.market_cap,0) }}</td><td><button v-if="item.quality_status === 'passed'" class="icon-button" :title="item.in_candidate_pool ? '移出候选' : '加入候选'" :aria-label="item.in_candidate_pool ? '移出候选' : '加入候选'" @click="candidate(item)"><Trash2 v-if="item.in_candidate_pool" :size="14" /><Plus v-else :size="14" /></button><span v-else class="status fail">质量异常</span></td></tr></tbody></table></div></AsyncState>
    <div class="section-heading" style="margin-top:24px"><div><span>NEUTRALIZATION</span><b>行业与市值中性化</b></div><button class="button" :disabled="!currentRows.length || runNeutralization.isPending.value" @click="runNeutralization.mutate()"><Scale :size="14" />中性化当前表格</button></div><p class="muted">使用 60 日收益作为当前因子值。缺少行业、市值或因子值的行由后端质量隔离，不会补造数据。</p>
    <div v-if="neutralization" class="panel-grid"><article class="panel"><h3>方法</h3><p>{{ neutralization.method }}</p></article><article class="panel"><h3>有效横截面</h3><p>{{ neutralization.items.length }}</p></article><article class="panel"><h3>质量隔离</h3><p>{{ neutralization.excluded.length }}</p></article><article class="panel"><h3>行业数量</h3><p>{{ neutralization.industry_count }}</p></article></div>
    <div v-if="neutralization" class="data-table-wrap"><table class="data-table"><thead><tr><th>证券</th><th>原值</th><th>中性值</th><th>Z 分数</th><th>质量状态</th></tr></thead><tbody><tr v-for="item in neutralization.items" :key="`ok-${item.security_code}`"><td>{{ item.security_name || item.security_code }}</td><td>{{ number(item.factor_value,4) }}</td><td>{{ number(item.neutralized_value,4) }}</td><td>{{ number(item.neutralized_zscore,3) }}</td><td><span class="status">有效</span></td></tr><tr v-for="(item,index) in neutralization.excluded" :key="`excluded-${item.security_code}-${index}`"><td>{{ item.security_name || item.security_code }}</td><td>{{ number(item.factor_value,4) }}</td><td>—</td><td>—</td><td><span class="status fail">{{ item.neutralization_reason }}</span></td></tr></tbody></table></div>
    </section>
    <aside class="content-band"><div class="section-heading"><div><span>WATCHLIST</span><b>人工观察名单</b></div><small>{{ candidates.data.value?.items?.length || 0 }}</small></div><div class="list"><article v-for="item in candidates.data.value?.items || []" :key="item.security_code" class="list-item"><header><h3>{{ item.security_name || item.security_code }}</h3><span class="mono">{{ item.security_code }}</span></header><p>60日 {{ pct(item.return_60d) }} · 波动 {{ pct(item.volatility_60d) }}</p><footer><RouterLink class="button secondary" :to="{ path:'/company', query:{ code:item.security_code, snapshot_id:item.source_snapshot_id } }">进入研究</RouterLink></footer></article></div></aside>
  </div>
</template>
