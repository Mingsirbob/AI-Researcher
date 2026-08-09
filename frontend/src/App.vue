<script setup lang="ts">
import { useRoute } from "vue-router";
import { useQuery } from "@tanstack/vue-query";
import { Activity, GitBranch, Menu, X } from "lucide-vue-next";
import { api } from "@/api/client";
import { useUiStore } from "@/stores/ui";

const route = useRoute();
const ui = useUiStore();

const groups = [
  { label: "交易", items: [{ to: "/", label: "模拟盘", icon: Activity }] },
  { label: "策略", items: [{ to: "/strategies", label: "策略", icon: GitBranch }] },
];

const health = useQuery({ queryKey: ["health"], queryFn: () => api<any>("/api/health"), refetchInterval: 60_000 });
</script>

<template>
  <div class="app-frame">
    <header class="topbar">
      <button class="icon-button mobile-menu" title="打开导航" @click="ui.navOpen = true"><Menu :size="19" /></button>
      <RouterLink to="/" class="brand"><span>迹</span><div><b>迹研</b><small>EVIDENCE RESEARCH</small></div></RouterLink>
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
