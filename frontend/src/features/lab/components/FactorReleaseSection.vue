<script setup lang="ts">
import { Send } from "lucide-vue-next";
import { shortId } from "@/lib/format";
import type { FactorLabRun, FactorRelease } from "@/api/types";

defineProps<{
  evaluation?: FactorLabRun;
  backtest?: FactorLabRun;
  releases: FactorRelease[];
  releaseTarget: FactorLabRun | null;
  releaseForm: { acknowledged: boolean; reviewer: string };
  releaseNotes: Record<string, string>;
  creating: boolean;
}>();
defineEmits<{ create: []; decide: [releaseId: string, decision: "approve" | "reject"] }>();
</script>

<template>
  <aside class="content-band">
    <div class="section-heading"><div><span>RESEARCH CONTRACT</span><b>发布证据</b></div><Send :size="16" /></div>
    <div class="list"><article class="list-item"><h3>最近评价</h3><p class="mono">{{ evaluation?.evaluation_id || "尚未运行" }}</p><p>{{ evaluation?.end_date || "—" }}</p></article><article class="list-item"><h3>最近回测</h3><p class="mono">{{ backtest?.backtest_id || "尚未运行" }}</p><p>{{ backtest?.factor_id || "—" }}</p></article><article class="list-item"><h3>发布候选</h3><p>{{ releases.length }} 个候选</p><p>建立候选不会自动改变模型或交易。</p></article></div>
    <div class="section-heading" style="margin-top:24px"><div><span>RELEASE GATES</span><b>候选发布审查</b></div></div>
    <form class="form-grid" @submit.prevent="$emit('create')"><label for="release-reviewer">审查人<input id="release-reviewer" v-model="releaseForm.reviewer" /></label><label class="wide"><span><input v-model="releaseForm.acknowledged" type="checkbox" /> 已阅读并接受回测边界</span></label><button class="button" :disabled="!releaseTarget || !releaseForm.acknowledged || creating"><Send :size="13" />建立候选</button></form>
    <div class="list" style="margin-top:16px"><article v-for="release in releases" :key="release.candidate.release_id" class="list-item"><header><div><span :class="['status',release.candidate.status==='gate_failed'?'fail':release.candidate.status==='gate_passed'?'warn':'']">{{ release.candidate.status }}</span><h3 style="margin-top:8px">{{ release.candidate.factor_id }}@v{{ release.candidate.factor_version }}</h3></div><code>{{ shortId(release.candidate.release_id) }}</code></header>
      <div class="panel-grid" style="margin-top:10px"><article class="panel"><h3>门禁版本</h3><p class="mono">{{ release.candidate.gate_version || '—' }}</p></article><article class="panel"><h3>结果哈希</h3><p class="mono">{{ shortId(release.candidate.result_hash) }}</p></article><article class="panel"><h3>评价证据</h3><p class="mono">{{ shortId(release.candidate.evaluation_result_hash) }}</p></article><article class="panel"><h3>回测证据</h3><p class="mono">{{ shortId(release.candidate.backtest_result_hash) }}</p></article></div>
      <div v-if="release.candidate.limitations?.length" style="margin-top:12px"><b>已知边界</b><ul><li v-for="item in release.candidate.limitations" :key="item">{{ item }}</li></ul></div>
      <div class="list" style="margin-top:10px"><div v-for="gate in release.gates" :key="gate.name||gate.label" class="panel"><span :class="['status',gate.passed?'':'fail']">{{ gate.passed?'PASS':'STOP' }}</span><h3 style="margin-top:8px">{{ gate.label||gate.name }}</h3><p>{{ gate.detail }} · 观测 {{ gate.observed }} · {{ gate.comparator }} {{ gate.threshold }}</p></div></div>
      <div v-if="release.decisions?.length" style="margin-top:12px"><b>审批历史</b><div class="list"><article v-for="decision in release.decisions" :key="decision.decision_id" class="list-item"><header><h3>{{ decision.reviewer }}</h3><span class="status">{{ decision.decision }}</span></header><p>{{ decision.note }}</p><small>{{ decision.created_at }}</small></article></div></div>
      <template v-if="['gate_passed','gate_failed'].includes(release.candidate.status)"><label :for="`release-note-${release.candidate.release_id}`" style="margin-top:12px">审查说明<textarea :id="`release-note-${release.candidate.release_id}`" v-model="releaseNotes[release.candidate.release_id]" rows="2" /></label><footer><button v-if="release.candidate.status==='gate_passed'" class="button" @click="$emit('decide', release.candidate.release_id, 'approve')">批准进入 Shadow</button><button class="button danger" @click="$emit('decide', release.candidate.release_id, 'reject')">拒绝</button></footer></template>
    </article></div>
  </aside>
</template>
