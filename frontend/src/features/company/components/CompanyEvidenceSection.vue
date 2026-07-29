<script setup lang="ts">
import { ExternalLink, Search } from "lucide-vue-next";

export interface EvidenceItem { id: string; category: string; label: string; value: string; as_of?: string; method?: string; source?: string; title?: string; retrieval_score?: number }
defineProps<{ evidence: EvidenceItem[]; uncertainties: string[]; results?: { items: EvidenceItem[]; retrieval_method: string }; searching: boolean }>();
defineEmits<{ search: [] }>();
const query = defineModel<string>({ required: true });
</script>

<template>
  <section class="content-band">
    <form class="control-grid" @submit.prevent="$emit('search')"><label for="evidence-search">全文证据搜索<input id="evidence-search" v-model="query" required minlength="1" placeholder="营业收入、现金流、诉讼…" /></label><button class="button secondary" :disabled="searching"><Search :size="14" />BM25 搜索</button></form>
    <div v-if="results" class="section-heading" style="margin-top:18px"><div><span>SEARCH · {{ results.retrieval_method }}</span><b>检索结果</b></div><small>{{ results.items.length }}</small></div>
    <div v-if="results" class="list"><article v-for="item in results.items" :key="item.id||item.title" class="list-item"><header><h3>{{ item.title||item.label }}</h3><span>{{ item.retrieval_score }}</span></header><p>{{ item.value }}</p><footer><a v-if="item.source" class="source-link" :href="item.source" target="_blank" rel="noopener"><ExternalLink :size="12" />打开原文</a></footer></article></div>
    <div class="panel-grid" style="margin-top:20px"><article v-for="item in evidence" :key="item.id" class="panel"><span class="eyebrow">{{ item.category }} · {{ item.id }}</span><h3>{{ item.label }}</h3><p>{{ item.value }}</p><details><summary>来源 · {{ item.as_of }}</summary><p>{{ item.method }}</p><a v-if="item.source" class="source-link" :href="item.source" target="_blank" rel="noopener"><ExternalLink :size="12" />打开来源</a></details></article></div>
    <div class="section-heading" style="margin-top:22px"><div><span>BOUNDARIES</span><b>已知边界</b></div></div><div class="list"><article v-for="item in uncertainties" :key="item" class="list-item"><p>{{ item }}</p></article></div>
  </section>
</template>
