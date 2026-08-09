import { createRouter, createWebHistory } from "vue-router";

const routes = [
  { path: "/", name: "paper", component: () => import("@/features/paper/PaperView.vue"), meta: { title: "模拟盘", group: "daily" } },
  { path: "/paper", redirect: "/" },
  { path: "/strategies", name: "strategies", component: () => import("@/features/strategy/StrategyEditorView.vue"), meta: { title: "策略编辑器", group: "lab" } },
  { path: "/:pathMatch(.*)*", redirect: "/" },
];

const router = createRouter({
  history: createWebHistory("/"),
  routes,
  scrollBehavior: () => ({ top: 0 }),
});

router.afterEach((to) => {
  document.title = `${String(to.meta.title ?? "工作台")} · 迹研`;
});

export default router;
