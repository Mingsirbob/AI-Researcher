<script setup lang="ts">
import { computed, reactive, watch } from "vue";
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { Save } from "lucide-vue-next";
import { api, jsonBody } from "@/api/client";
import { useUiStore } from "@/stores/ui";
import AsyncState from "@/components/AsyncState.vue";
import PageHeader from "@/components/PageHeader.vue";
import type { ApiList, FactorVersion } from "@/api/types";

interface FactorTemplate { template_id: string; name: string; category: string; description?: string; expression_template?: string; default_window: number; min_window?: number; max_window?: number }

const ui = useUiStore();
const client = useQueryClient();
const templates = useQuery({ queryKey: ["factor-lab", "templates"], queryFn: () => api<ApiList<FactorTemplate>>("/api/factor-lab/templates") });
const factors = useQuery({ queryKey: ["factor-lab", "factors"], queryFn: () => api<ApiList<FactorVersion>>("/api/factor-lab/factors") });
const form = reactive({ name: "", factor_id: "", description: "", template_id: "", window: 20, direction: "" });
const availableTemplates = computed(() => (templates.data.value?.items || []).filter((item) => item.template_id !== "alpha158_bundle"));
const template = computed(() => availableTemplates.value.find((item) => item.template_id === form.template_id));
const expression = computed(() => template.value?.expression_template?.replaceAll("{window}", String(form.window)) || "选择一个白名单模板");
watch(availableTemplates, (items) => { const first = items[0]; if (first && !form.template_id) { form.template_id = first.template_id; form.window = first.default_window; } }, { immediate: true });
watch(template, (value, previous) => { if (value && value.template_id !== previous?.template_id) form.window = value.default_window; });
const create = useMutation({
  mutationFn: () => api<FactorVersion>("/api/factor-lab/factors", { method: "POST", ...jsonBody({ ...form, direction: form.direction || null, owner: "human" }) }),
  onSuccess: async () => { ui.notify("候选因子已保存为草稿"); Object.assign(form, { name: "", factor_id: "", description: "", direction: "" }); await client.invalidateQueries({ queryKey: ["factor-lab", "factors"] }); },
  onError: (error: Error) => ui.notify(error.message),
});
</script>

<template>
  <PageHeader eyebrow="FACTOR WORKSHOP" title="因子开发" description="从白名单结构化公式创建候选因子。每次修改生成新版本，旧版本保持不可变。"><span class="status">禁止任意代码</span></PageHeader>
  <div class="two-column"><section class="content-band white"><form class="form-grid" @submit.prevent="create.mutate()"><label for="factor-name">因子名称<input id="factor-name" v-model="form.name" required minlength="2" maxlength="80" /></label><label for="factor-id">因子 ID<input id="factor-id" v-model="form.factor_id" required pattern="[a-z0-9_]+" /></label><label for="factor-template">公式模板<select id="factor-template" v-model="form.template_id" required><option v-for="item in availableTemplates" :key="item.template_id" :value="item.template_id">{{ item.name }} · {{ item.category }}</option></select></label><label for="factor-window">观察窗口<input id="factor-window" v-model.number="form.window" type="number" :min="template?.min_window||1" :max="template?.max_window||500" /></label><label for="factor-direction">排序方向<select id="factor-direction" v-model="form.direction"><option value="">模板默认</option><option value="positive">越大越好</option><option value="negative">越小越好</option></select></label><label for="factor-description" class="wide">研究假设<textarea id="factor-description" v-model="form.description" required minlength="4" rows="4" /></label><div class="wide panel" style="color:white;background:var(--ink);border-left:3px solid var(--acid)"><span class="eyebrow">FORMULA PREVIEW</span><p class="mono" style="margin-top:10px;color:var(--acid)">{{ expression }}</p><p>{{ template?.description }}</p></div><button class="button" :disabled="create.isPending.value"><Save :size="14" />保存为草稿</button></form></section>
    <aside class="content-band"><div class="section-heading"><div><span>WORKBENCH</span><b>最近因子</b></div><small>{{ factors.data.value?.items?.length||0 }}</small></div><AsyncState :loading="factors.isPending.value" :error="factors.error.value"><div class="list"><article v-for="item in (factors.data.value?.items||[]).slice(0,10)" :key="`${item.factor_id}-${item.version}`" class="list-item"><header><h3>{{ item.name }} · v{{ item.version }}</h3><span class="status">{{ item.lifecycle_status||item.status }}</span></header><p class="mono">{{ item.expression }}</p><p>{{ item.model_used?'当前模型使用中':'当前模型未使用' }}</p></article></div></AsyncState></aside>
  </div>
</template>
