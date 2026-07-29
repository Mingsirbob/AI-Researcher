<script setup lang="ts">
import { computed, ref } from "vue";
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { History, Play, ShieldCheck } from "lucide-vue-next";
import { api, jsonBody } from "@/api/client";
import { shortId } from "@/lib/format";
import { useUiStore } from "@/stores/ui";
import AsyncState from "@/components/AsyncState.vue";
import PageHeader from "@/components/PageHeader.vue";
import MetricStrip from "@/components/MetricStrip.vue";

interface Metric { key: string; label: string; value?: number; display_value?: string; threshold?: number; passed: boolean; numerator?: number; denominator?: number }
interface DocumentSample { label?: string; document_id?: string; title?: string; security_code?: string; expected_status?: string; actual_status?: string; terms?: Array<{ term: string; found: boolean }>; passed: boolean }
interface GoldenFact { document_sha256: string; metric_code: string; expected: Record<string, unknown>; actual?: Record<string, unknown> | null; found: boolean; exact: boolean }
interface FailureMap { pdf_files?: Record<string, unknown>[]; financial_citations?: Record<string, unknown>[]; chunks?: Record<string, unknown>[]; research_citations?: Record<string, unknown>[]; extraction_consistency?: Record<string, unknown>[] }
interface Acceptance { run_id?: string; acceptance_run_id?: string; manifest_version?: string; manifest_hash?: string; data_fingerprint?: string; created_at?: string; completed_at?: string; status: string; metrics: Metric[]; document_samples: DocumentSample[]; golden_facts: GoldenFact[]; failures: FailureMap; failed_metrics: string[]; known_gaps: Array<string | { message?: string; description?: string }> }
interface HistoryItem { run_id?: string; acceptance_run_id?: string; status: string; created_at?: string; completed_at?: string }

const ui = useUiStore();
const client = useQueryClient();
const selected = ref<Acceptance | null>(null);
const latest = useQuery({ queryKey: ["acceptance", "latest"], queryFn: () => api<{ item: Acceptance | null }>("/api/evidence-acceptance-runs/latest") });
const history = useQuery({ queryKey: ["acceptance", "history"], queryFn: () => api<{ items: HistoryItem[] }>("/api/evidence-acceptance-runs?limit=20") });
const result = computed(() => selected.value ?? latest.data.value?.item);
const metrics = computed(() => (result.value?.metrics || []).map((item) => ({ label: item.label || item.key, value: item.display_value ?? item.value ?? "—", note: item.threshold == null ? (item.passed ? "通过" : "未通过") : `门槛 ${item.threshold}`, tone: item.passed ? "neutral" as const : "negative" as const })));
const failureRows = computed(() => Object.entries(result.value?.failures || {}).flatMap(([category, items]) => (items || []).map((item: Record<string, unknown>) => ({ category, item }))));
const run = useMutation({ mutationFn: () => api<Acceptance>("/api/evidence-acceptance-runs", { method: "POST", ...jsonBody({}) }), onSuccess: async (data) => { selected.value = data; ui.notify("证据质量验收已完成"); await client.invalidateQueries({ queryKey: ["acceptance"] }); }, onError: (error: Error) => ui.notify(error.message) });
async function openRun(id: string) { try { selected.value = await api<Acceptance>(`/api/evidence-acceptance-runs/${encodeURIComponent(id)}`); } catch (error) { ui.notify((error as Error).message); } }
</script>

<template>
  <PageHeader eyebrow="EVIDENCE ACCEPTANCE" title="证据质量验收" description="用冻结样本和确定性门禁检查文档、引用、财务事实与研究快照。验收结果只描述当前样本覆盖。"><button class="button" :disabled="run.isPending.value" @click="run.mutate()"><Play :size="15" />{{ run.isPending.value ? "正在核验" : "运行验收" }}</button></PageHeader>
  <AsyncState :loading="latest.isPending.value" :error="latest.error.value" @retry="latest.refetch()">
    <MetricStrip v-if="metrics.length" :items="metrics" />
    <div class="two-column"><section class="content-band white"><div class="section-heading"><div><span>ACCEPTANCE RESULT</span><b>当前验收快照</b></div><span :class="['status',result?.status==='passed'?'':'fail']"><ShieldCheck :size="12" /> {{ result?.status||'未运行' }}</span></div><div class="panel-grid"><article class="panel"><h3>运行标识</h3><p class="mono">{{ result?.run_id||result?.acceptance_run_id||'—' }}</p></article><article class="panel"><h3>样本合同</h3><p>{{ result?.manifest_version||'—' }}</p><p class="mono">{{ result?.manifest_hash }}</p></article><article class="panel"><h3>数据指纹</h3><p class="mono">{{ result?.data_fingerprint||'—' }}</p></article></div>
      <div class="section-heading" style="margin-top:24px"><div><span>DOCUMENT SAMPLES</span><b>真实文档样本</b></div><small>{{ result?.document_samples?.length||0 }} 项</small></div><div class="data-table-wrap"><table class="data-table"><thead><tr><th>文档</th><th>证券</th><th>预期/实际文本层</th><th>关键术语</th><th>状态</th></tr></thead><tbody><tr v-for="item in result?.document_samples||[]" :key="item.document_id||item.label"><td>{{ item.title||item.label }}</td><td>{{ item.security_code||'—' }}</td><td>{{ item.expected_status }} / {{ item.actual_status }}</td><td><span v-for="term in item.terms||[]" :key="term.term" :class="['status',term.found?'':'fail']">{{ term.term }}</span></td><td><span :class="['status',item.passed?'':'fail']">{{ item.passed?'通过':'未通过' }}</span></td></tr><tr v-if="!result?.document_samples?.length"><td colspan="5">暂无样本</td></tr></tbody></table></div>
      <div class="section-heading" style="margin-top:24px"><div><span>GOLDEN FINANCIAL FACTS</span><b>金标财务事实逐项比对</b></div><small>{{ result?.golden_facts?.length||0 }} 项</small></div><div class="data-table-wrap"><table class="data-table"><thead><tr><th>指标</th><th>预期值</th><th>实际值</th><th>召回</th><th>精确</th></tr></thead><tbody><tr v-for="item in result?.golden_facts||[]" :key="`${item.document_sha256}-${item.metric_code}`"><td><b>{{ item.metric_code }}</b><br><small class="mono">{{ shortId(item.document_sha256) }}</small></td><td><pre>{{ JSON.stringify(item.expected,null,2) }}</pre></td><td><pre>{{ JSON.stringify(item.actual,null,2) }}</pre></td><td><span :class="['status',item.found?'':'fail']">{{ item.found?'FOUND':'MISS' }}</span></td><td><span :class="['status',item.exact?'':'fail']">{{ item.exact?'EXACT':'DIFF' }}</span></td></tr><tr v-if="!result?.golden_facts?.length"><td colspan="5">暂无金标财务事实</td></tr></tbody></table></div>
      <div class="section-heading" style="margin-top:24px"><div><span>INTEGRITY FAILURES</span><b>完整性失败明细</b></div><small>{{ failureRows.length }}</small></div><div class="list"><article v-for="(row,index) in failureRows" :key="`${row.category}-${index}`" class="list-item"><header><h3>{{ row.category }}</h3><span class="status fail">FAIL</span></header><pre>{{ JSON.stringify(row.item,null,2) }}</pre></article><article v-if="!failureRows.length" class="list-item"><p>当前快照没有完整性失败。</p></article></div>
      <div class="section-heading" style="margin-top:24px"><div><span>KNOWN GAPS</span><b>已知边界</b></div></div><div class="list"><article v-for="gap in result?.known_gaps||[]" :key="String(gap)" class="list-item"><p>{{ typeof gap==='string'?gap:gap.message||gap.description }}</p></article><article v-if="!result?.known_gaps?.length" class="list-item"><p>当前快照没有登记额外缺口。</p></article></div></section>
      <aside class="content-band"><div class="section-heading"><div><span>RUN HISTORY</span><b><History :size="14" /> 历史运行</b></div><small>{{ history.data.value?.items?.length||0 }}</small></div><AsyncState :loading="history.isPending.value" :error="history.error.value" :empty="!history.data.value?.items?.length"><div class="list"><button v-for="item in history.data.value?.items||[]" :key="item.run_id||item.acceptance_run_id" class="list-item" style="text-align:left" @click="openRun(item.run_id||item.acceptance_run_id||'')"><header><h3>{{ item.status }}</h3><span class="mono">{{ shortId(item.run_id||item.acceptance_run_id) }}</span></header><p>{{ item.created_at||item.completed_at }}</p></button></div></AsyncState></aside></div>
  </AsyncState>
</template>
