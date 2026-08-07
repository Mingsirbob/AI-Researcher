<script setup lang="ts">
import { computed, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useQuery } from "@tanstack/vue-query";
import { Activity, BarChart3, Beaker, BookOpenCheck, Building2, ChevronRight, ClipboardCheck, FileSearch, FlaskConical, GitBranch, Menu, Search, ShieldCheck, X } from "lucide-vue-next";
import { api } from "@/api/client";
import { useUiStore } from "@/stores/ui";

const route = useRoute();
const router = useRouter();
const ui = useUiStore();
const query = ref("");
const searchOpen = ref(false);

const groups = [
  { label: "交易", items: [{ to: "/", label: "模拟盘", icon: Activity }] },
  { label: "公司", items: [{ to: "/company", label: "公司分析", icon: Building2 }, { to: "/theses", label: "长期论点", icon: FileSearch }] },
  { label: "量化实验室", items: [{ to: "/quant", label: "数据与特征", icon: BarChart3 }, { to: "/factor-development", label: "因子开发", icon: FlaskConical }, { to: "/factor-evaluation", label: "因子评价", icon: Beaker }, { to: "/backtest", label: "策略回测", icon: BookOpenCheck }, { to: "/factor-library", label: "因子库", icon: ClipboardCheck }, { to: "/strategies", label: "策略编辑器", icon: GitBranch }] },
  { label: "审计", items: [{ to: "/decisions", label: "案例评价", icon: ShieldCheck }, { to: "/acceptance", label: "质量验收", icon: ClipboardCheck }] },
];

const health = useQuery({ queryKey: ["health"], queryFn: () => api<any>("/api/health"), refetchInterval: 60_000 });
const securities = useQuery({
  queryKey: computed(() => ["securities", query.value]),
  queryFn: () => api<any>(`/api/securities?q=${encodeURIComponent(query.value)}&limit=8`),
  enabled: computed(() => query.value.trim().length > 0),
});

function openSecurity(code: string) {
  query.value = code;
  searchOpen.value = false;
  ui.navOpen = false;
  router.push({ path: "/company", query: { code } });
}
</script>

<template>
  <div class="app-frame">
    <header class="topbar">
      <button class="icon-button mobile-menu" title="打开导航" @click="ui.navOpen = true"><Menu :size="19" /></button>
      <RouterLink to="/" class="brand"><span>迹</span><div><b>迹研</b><small>EVIDENCE RESEARCH</small></div></RouterLink>
      <div class="global-search">
        <Search :size="17" />
        <input v-model="query" placeholder="输入代码或公司名称" aria-label="搜索股票" @focus="searchOpen = true" @keydown.enter="openSecurity(query.trim().toUpperCase())" />
        <div v-if="searchOpen && query" class="search-results">
          <button v-for="item in securities.data.value?.items || []" :key="item.code" @click="openSecurity(item.code)">
            <span><b>{{ item.name || item.security_name }}</b><small>{{ item.code }}</small></span><ChevronRight :size="15" />
          </button>
        </div>
      </div>
      <div class="system-status"><i :class="{ down: health.isError.value }"></i><span>{{ health.data.value ? `${Number(health.data.value.securities).toLocaleString()} 只证券` : "连接数据" }}</span></div>
    </header>

    <div v-if="ui.navOpen" class="nav-backdrop" @click="ui.navOpen = false"></div>
    <aside class="sidebar" :class="{ open: ui.navOpen }">
      <button class="icon-button nav-close" title="关闭导航" @click="ui.navOpen = false"><X :size="18" /></button>
      <nav>
        <section v-for="group in groups" :key="group.label">
          <h2>{{ group.label }}</h2>
          <RouterLink v-for="item in group.items" :key="item.to" :to="item.to" :class="{ active: route.path === item.to }" @click="ui.navOpen = false">
            <component :is="item.icon" :size="16" /><span>{{ item.label }}</span>
          </RouterLink>
        </section>
      </nav>
      <div class="migration-note"><span>VUE 3</span><b>研究工作区</b></div>
    </aside>

    <main class="workspace"><RouterView /></main>
    <Transition name="toast"><div v-if="ui.toast" class="toast" role="status">{{ ui.toast }}</div></Transition>
  </div>
</template>
