<script setup lang="ts">
import { computed, reactive, watch } from "vue";
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { Bot, Check, FolderCode, LoaderCircle, Sparkles } from "lucide-vue-next";
import { api, jsonBody } from "@/api/client";
import AsyncState from "@/components/AsyncState.vue";
import PageHeader from "@/components/PageHeader.vue";
import { useUiStore } from "@/stores/ui";

interface PoolItem {
  pool_id: string;
  name: string;
}

interface StrategyCatalog {
  pools: PoolItem[];
  llm_configured: boolean;
}

interface StrategyDraft {
  draft_id: string;
  name: string;
}

interface StrategyVersion {
  strategy_version_id: string;
  draft_id: string;
  name: string;
  version: number;
  strategy_kind?: string;
  source_path?: string | null;
  published_at: string;
  definition?: {
    modules?: {
      universe?: {
        config?: {
          pool_id?: string;
        };
      };
    };
  };
}

interface CompileResult {
  status: string;
  errors?: Array<{ message?: string }>;
}

const ui = useUiStore();
const client = useQueryClient();
const form = reactive({ name: "", pool_id: "csi300", requirement: "" });

const catalog = useQuery({
  queryKey: ["strategy", "components"],
  queryFn: () => api<StrategyCatalog>("/api/strategy-components"),
});

const versions = useQuery({
  queryKey: ["strategy", "versions"],
  queryFn: () => api<{ items: StrategyVersion[] }>("/api/strategy-versions"),
});

const poolNames = computed(() => new Map(
  (catalog.data.value?.pools || []).map((item) => [item.pool_id, item.name]),
));

const repository = computed(() => (versions.data.value?.items || []).filter(
  (item) => item.strategy_kind === "python_code",
));

watch(
  () => catalog.data.value?.pools,
  (pools) => {
    if (!pools?.length || pools.some((item) => item.pool_id === form.pool_id)) return;
    form.pool_id = pools[0].pool_id;
  },
  { immediate: true },
);

function strategyPool(item: StrategyVersion) {
  const poolId = item.definition?.modules?.universe?.config?.pool_id || "";
  return poolNames.value.get(poolId) || poolId || "未记录";
}

const createStrategy = useMutation({
  mutationFn: async () => {
    const draft = await api<StrategyDraft>("/api/strategy-generate", {
      method: "POST",
      ...jsonBody({
        name: form.name,
        pool_id: form.pool_id,
        requirement: form.requirement,
        filter_pipeline_id: "factor_quality_passed",
        params: {},
      }),
    });
    const compilation = await api<CompileResult>(`/api/strategy-drafts/${draft.draft_id}/compile`, {
      method: "POST",
    });
    if (compilation.status !== "valid") {
      const reason = compilation.errors?.map((item) => item.message).filter(Boolean).join("；");
      throw new Error(reason || "模型生成的策略未通过代码校验");
    }
    return api<StrategyVersion>(`/api/strategy-drafts/${draft.draft_id}/publish`, {
      method: "POST",
    });
  },
  onSuccess: async (item) => {
    form.name = "";
    form.requirement = "";
    await Promise.all([
      client.invalidateQueries({ queryKey: ["strategy", "drafts"] }),
      client.invalidateQueries({ queryKey: ["strategy", "versions"] }),
    ]);
    ui.notify(`${item.name} 已生成并存入策略仓库`);
  },
  onError: (error: Error) => ui.notify(error.message),
});
</script>

<template>
  <PageHeader
    eyebrow="AI STRATEGY STUDIO"
    title="策略编辑器"
    description="描述你的选股逻辑，由大模型生成并保存可执行策略。"
  >
    <span class="status">MiMo</span>
  </PageHeader>

  <section class="create-band">
    <div class="section-heading">
      <div class="heading-icon"><Sparkles :size="18" /></div>
      <div>
        <span>CREATE</span>
        <h2>创建策略</h2>
      </div>
      <p>{{ catalog.data.value?.llm_configured ? "模型已连接" : "模型未配置" }}</p>
    </div>

    <form class="strategy-form" @submit.prevent="createStrategy.mutate()">
      <label>
        <span>策略名称</span>
        <input
          v-model.trim="form.name"
          required
          minlength="2"
          maxlength="120"
          placeholder="例如：高股息低波动"
        />
      </label>
      <label>
        <span>策略股票池</span>
        <select v-model="form.pool_id" required>
          <option
            v-for="item in catalog.data.value?.pools || []"
            :key="item.pool_id"
            :value="item.pool_id"
          >
            {{ item.name }}
          </option>
          <option v-if="!catalog.data.value?.pools?.length" value="csi300">沪深300</option>
        </select>
      </label>
      <label class="requirement-field">
        <span>策略需求</span>
        <textarea
          v-model.trim="form.requirement"
          required
          minlength="8"
          maxlength="4000"
          rows="7"
          placeholder="描述选股条件、排序逻辑、调仓偏好，以及你希望策略如何处理已有持仓。"
        />
        <small>{{ form.requirement.length }} / 4000</small>
      </label>
      <button
        class="button create-button"
        :disabled="createStrategy.isPending.value || !catalog.data.value?.llm_configured"
      >
        <LoaderCircle v-if="createStrategy.isPending.value" class="spinning" :size="16" />
        <Bot v-else :size="16" />
        {{ createStrategy.isPending.value ? "正在生成策略" : "创建策略" }}
      </button>
    </form>
  </section>

  <section class="repository-band">
    <div class="section-heading repository-heading">
      <div class="heading-icon"><FolderCode :size="18" /></div>
      <div>
        <span>REPOSITORY</span>
        <h2>策略仓库</h2>
      </div>
      <p>{{ repository.length }} 个策略</p>
    </div>

    <AsyncState
      :loading="versions.isPending.value || catalog.isPending.value"
      :error="versions.error.value || catalog.error.value"
      @retry="versions.refetch()"
    >
      <div v-if="repository.length" class="repository-table">
        <div class="repository-header" aria-hidden="true">
          <span>策略名称</span>
          <span>策略股票池</span>
          <span>策略存储位置</span>
        </div>
        <article v-for="item in repository" :key="item.strategy_version_id" class="strategy-row">
          <div class="strategy-name">
            <span class="ready-mark"><Check :size="13" /></span>
            <div>
              <b>{{ item.name }}</b>
              <small>版本 {{ item.version }}</small>
            </div>
          </div>
          <div class="pool-cell" data-label="策略股票池">{{ strategyPool(item) }}</div>
          <div class="path-cell" data-label="策略存储位置">
            <code>{{ item.source_path || "quant_research.db" }}</code>
          </div>
        </article>
      </div>
      <div v-else class="empty-repository">
        <FolderCode :size="28" />
        <b>策略仓库为空</b>
        <p>创建第一条策略后，代码文件会显示在这里。</p>
      </div>
    </AsyncState>
  </section>
</template>

<style scoped>
.create-band,.repository-band{padding:24px 28px;border-bottom:1px solid var(--line);background:#fff}.repository-band{min-height:360px;background:#f4f5f1}.section-heading{display:grid;grid-template-columns:38px 1fr auto;align-items:center;gap:11px;margin-bottom:20px}.heading-icon{display:grid;width:38px;height:38px;place-items:center;border:1px solid var(--line-dark);color:var(--teal);background:#f7faf8}.section-heading span,.section-heading h2{display:block}.section-heading span{color:var(--muted);font-size:8px;font-weight:700}.section-heading h2{margin:3px 0 0;font-size:15px}.section-heading p{margin:0;color:var(--muted);font-size:10px}.strategy-form{display:grid;grid-template-columns:minmax(220px,1fr) minmax(200px,.75fr);gap:16px;max-width:920px}.strategy-form label{display:grid;gap:7px}.strategy-form label>span{font-size:9px;font-weight:700}.strategy-form input,.strategy-form select,.strategy-form textarea{width:100%;border:1px solid var(--line-dark);background:#fff;color:var(--ink);font:inherit}.strategy-form input,.strategy-form select{height:42px;padding:0 11px}.strategy-form textarea{min-height:150px;padding:12px;line-height:1.65;resize:vertical}.strategy-form input:focus,.strategy-form select:focus,.strategy-form textarea:focus{border-color:var(--teal);outline:2px solid color-mix(in srgb,var(--teal) 18%,transparent);outline-offset:0}.requirement-field{position:relative;grid-column:1/-1}.requirement-field small{position:absolute;right:10px;bottom:9px;color:var(--muted);font-size:8px}.create-button{justify-self:start;min-width:142px;height:40px}.spinning{animation:spin .8s linear infinite}.repository-heading{margin-bottom:14px}.repository-table{border-top:1px solid var(--line-dark);background:#fff}.repository-header,.strategy-row{display:grid;grid-template-columns:minmax(210px,.8fr) minmax(150px,.45fr) minmax(320px,1.4fr);align-items:center;column-gap:18px}.repository-header{min-height:34px;padding:0 16px;border-bottom:1px solid var(--line);color:var(--muted);font-size:8px;font-weight:700}.strategy-row{min-height:68px;padding:10px 16px;border-bottom:1px solid var(--line)}.strategy-row:last-child{border-bottom:0}.strategy-name{display:flex;align-items:center;min-width:0;gap:10px}.strategy-name b,.strategy-name small{display:block}.strategy-name b{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.strategy-name small{margin-top:4px;color:var(--muted)}.ready-mark{display:grid;width:24px;height:24px;flex:0 0 24px;place-items:center;border:1px solid #94aaa0;color:#27614f;background:#edf5f1}.pool-cell{font-weight:600}.path-cell{min-width:0}.path-cell code{display:block;overflow:hidden;color:#3f4b47;font-size:10px;text-overflow:ellipsis;white-space:nowrap}.empty-repository{display:grid;min-height:230px;place-items:center;align-content:center;gap:8px;border-top:1px solid var(--line-dark);color:var(--muted);background:#fff}.empty-repository p{margin:0}.empty-repository b{color:var(--ink)}@keyframes spin{to{transform:rotate(360deg)}}
@media(max-width:760px){.create-band,.repository-band{padding:20px 16px}.strategy-form{grid-template-columns:1fr}.requirement-field{grid-column:auto}.repository-header{display:none}.strategy-row{grid-template-columns:1fr;gap:10px;padding:15px 14px}.pool-cell,.path-cell{display:grid;grid-template-columns:95px minmax(0,1fr);align-items:start;gap:8px}.pool-cell:before,.path-cell:before{content:attr(data-label);color:var(--muted);font-size:8px;font-weight:700}.section-heading{grid-template-columns:38px 1fr}.section-heading>p{grid-column:2}.path-cell code{white-space:normal;overflow-wrap:anywhere}}
</style>
