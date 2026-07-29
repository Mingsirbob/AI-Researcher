<script setup lang="ts">
import { reactive, ref } from "vue";
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { Plus, RefreshCw, Trash2 } from "lucide-vue-next";
import { api, openApiJsonBody } from "@/api/client";
import type { OpenApiSchema } from "@/api/client";
import { useUiStore } from "@/stores/ui";
import PageHeader from "@/components/PageHeader.vue";
import AsyncState from "@/components/AsyncState.vue";
import type { ApiList, Thesis, ThesisMonitor } from "@/api/types";

const ui = useUiStore();
const client = useQueryClient();
const show = ref(false);
const detail = ref<Record<string, ThesisMonitor | null>>({});
type ThesisStatus = OpenApiSchema<"ThesisUpdate">["status"];
type ClaimVerdict = OpenApiSchema<"ClaimEvaluationConfirm">["verdict"];
const form = reactive<OpenApiSchema<"ThesisCreate"> & { invalidating: string }>({ code: "300750.SZ", title: "", core_claim: "", horizon: "6-12个月", status: "观察", invalidating: "" });
const statuses: ThesisStatus[] = ["观察", "验证中", "基本成立", "证据减弱", "已经证伪", "研究终止"];
const verdicts: ClaimVerdict[] = ["增强", "维持", "减弱", "证伪", "无法判断"];
const theses = useQuery({ queryKey: ["theses"], queryFn: () => api<ApiList<Thesis>>("/api/theses") });
const create = useMutation({
  mutationFn: () => api<Thesis>("/api/theses", { method: "POST", ...openApiJsonBody<"ThesisCreate">({ code: form.code, title: form.title, core_claim: form.core_claim, horizon: form.horizon, status: form.status, invalidating_conditions: form.invalidating.split("\n").map((item) => item.trim()).filter(Boolean) }) }),
  onSuccess: async () => { show.value = false; ui.notify("研究论点已保存"); await client.invalidateQueries({ queryKey: ["theses"] }); },
  onError: (error: Error) => ui.notify(error.message),
});
function thesisId(item: Thesis) { return item.id || item.thesis_id || ""; }
async function action(id: string, type: "baseline" | "check" | "delete") {
  if (type === "delete" && !window.confirm("确认删除这条研究论点？该操作不可撤销。")) return;
  try {
    if (type === "delete") await api(`/api/theses/${encodeURIComponent(id)}`, { method: "DELETE" });
    else if (type === "baseline") await api(`/api/theses/${encodeURIComponent(id)}/monitor/baseline`, { method: "POST", ...openApiJsonBody<"ThesisMonitorBaselineRequest">({}) });
    else await api(`/api/theses/${encodeURIComponent(id)}/monitor/check`, { method: "POST", ...openApiJsonBody<"ThesisMonitorCheckRequest">({}) });
    ui.notify(type === "baseline" ? "监控基线已建立" : type === "check" ? "更新检查已完成" : "论点已删除");
    await client.invalidateQueries({ queryKey: ["theses"] });
    if (type !== "delete" && detail.value[id]) detail.value[id] = await api<ThesisMonitor>(`/api/theses/${encodeURIComponent(id)}/monitor`);
  } catch (error) { ui.notify(error instanceof Error ? error.message : String(error)); }
}
async function open(id: string) {
  try { detail.value[id] = detail.value[id] ? null : await api<ThesisMonitor>(`/api/theses/${encodeURIComponent(id)}/monitor`); }
  catch (error) { ui.notify(error instanceof Error ? error.message : String(error)); }
}
async function updateStatus(id: string, status: ThesisStatus) {
  try { await api(`/api/theses/${encodeURIComponent(id)}`, { method: "PATCH", ...openApiJsonBody<"ThesisUpdate">({ status }) }); ui.notify("论点状态已更新"); await client.invalidateQueries({ queryKey: ["theses"] }); }
  catch (error) { ui.notify(error instanceof Error ? error.message : String(error)); }
}
function updateStatusFromEvent(id: string, event: Event) {
  void updateStatus(id, (event.target as HTMLSelectElement).value as ThesisStatus);
}
async function confirm(id: string, evaluationId: string, verdict: ClaimVerdict) {
  try {
    await api(`/api/claim-evaluations/${encodeURIComponent(evaluationId)}`, { method: "PATCH", ...openApiJsonBody<"ClaimEvaluationConfirm">({ verdict }) });
    detail.value[id] = await api<ThesisMonitor>(`/api/theses/${encodeURIComponent(id)}/monitor`);
    ui.notify(`已确认：${verdict}`);
  } catch (error) { ui.notify(error instanceof Error ? error.message : String(error)); }
}
</script>

<template>
  <PageHeader eyebrow="THESIS MONITOR" title="投资论点库" description="把研究结论变成能够持续核验、能够被公开证据证伪的对象。"><button class="button" @click="show=!show"><Plus :size="14" />建立论点</button></PageHeader>
  <section v-if="show" class="content-band white"><form class="form-grid" @submit.prevent="create.mutate()"><label for="thesis-code">证券代码<input id="thesis-code" v-model="form.code" required /></label><label for="thesis-title">标题<input id="thesis-title" v-model="form.title" required /></label><label for="thesis-horizon">研究周期<select id="thesis-horizon" v-model="form.horizon"><option>1-3个月</option><option>6-12个月</option><option>1-3年</option></select></label><label for="thesis-status">状态<select id="thesis-status" v-model="form.status"><option v-for="status in statuses" :key="status">{{ status }}</option></select></label><label for="thesis-claim" class="wide">核心论点<textarea id="thesis-claim" v-model="form.core_claim" required rows="3" /></label><label for="thesis-invalidating" class="wide">证伪条件，每行一个<textarea id="thesis-invalidating" v-model="form.invalidating" rows="3" /></label><button class="button" :disabled="create.isPending.value">保存论点</button></form></section>
  <section class="content-band"><AsyncState :loading="theses.isPending.value" :error="theses.error.value" :empty="!theses.data.value?.items?.length"><div class="list"><article v-for="item in theses.data.value?.items||[]" :key="thesisId(item)" class="list-item"><header><div><label :for="`thesis-state-${thesisId(item)}`">状态<select :id="`thesis-state-${thesisId(item)}`" :value="item.status" @change="updateStatusFromEvent(thesisId(item), $event)"><option v-for="status in statuses" :key="status">{{ status }}</option></select></label><h3 style="margin-top:8px">{{ item.title }}</h3></div><span class="mono">{{ item.security_code||item.code }}</span></header><p>{{ item.core_claim }}</p><p>周期 {{ item.horizon }} · 基线 {{ item.monitor?.baseline?'已建立':'未建立' }}</p><footer><button class="button secondary" @click="open(thesisId(item))">{{ detail[thesisId(item)] ? '收起详情' : '查看详情' }}</button><button class="button secondary" @click="action(thesisId(item),'baseline')">建立基线</button><button class="button secondary" @click="action(thesisId(item),'check')"><RefreshCw :size="13" />检查更新</button><button class="icon-button" title="删除" aria-label="删除论点" @click="action(thesisId(item),'delete')"><Trash2 :size="14" /></button></footer>
      <div v-if="detail[thesisId(item)]" class="panel" style="margin-top:12px"><h3>监控 Claim</h3><p v-if="detail[thesisId(item)]?.thesis?.monitor?.baseline" class="mono">基线 {{ detail[thesisId(item)]?.thesis?.monitor?.baseline?.run_id }} · {{ detail[thesisId(item)]?.thesis?.monitor?.baseline?.as_of }}</p><div class="list"><article v-for="claim in detail[thesisId(item)]?.claims||[]" :key="claim.claim_id || claim.statement" class="list-item"><p>{{ claim.statement }}</p><footer>最近判断 {{ claim.last_verdict||'尚未确认' }}</footer></article><article v-for="evaluation in detail[thesisId(item)]?.evaluations||[]" :key="evaluation.evaluation_id" class="list-item"><header><h3>{{ evaluation.statement }}</h3><span class="status warn">建议 {{ evaluation.suggested_verdict }}</span></header><p>{{ evaluation.rationale }}</p><footer v-if="evaluation.status==='pending'"><button v-for="verdict in verdicts" :key="verdict" class="button secondary" @click="confirm(thesisId(item),evaluation.evaluation_id,verdict)">{{ verdict }}</button></footer><footer v-else>已确认 {{ evaluation.confirmed_verdict||evaluation.verdict }}</footer></article></div></div>
    </article></div></AsyncState></section>
</template>
