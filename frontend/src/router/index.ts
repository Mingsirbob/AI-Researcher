import { createRouter, createWebHistory } from "vue-router";

const routes = [
  { path: "/", name: "paper", component: () => import("@/features/paper/PaperView.vue"), meta: { title: "模拟盘", group: "daily" } },
  { path: "/paper", redirect: "/" },
  { path: "/company", name: "company", component: () => import("@/features/company/CompanyView.vue"), meta: { title: "公司分析", group: "company" } },
  { path: "/theses", name: "theses", component: () => import("@/features/company/ThesisView.vue"), meta: { title: "长期论点", group: "company" } },
  { path: "/quant", redirect: "/factor-development" },
  { path: "/factor-development", name: "factor-development", component: () => import("@/features/lab/FactorDevelopmentView.vue"), meta: { title: "因子开发", group: "lab" } },
  { path: "/factor-evaluation", redirect: "/factor-development" },
  { path: "/backtest", name: "backtest", component: () => import("@/features/lab/BacktestView.vue"), meta: { title: "策略回测与 Shadow", group: "lab" } },
  { path: "/factor-library", redirect: "/factor-development" },
  { path: "/strategies", name: "strategies", component: () => import("@/features/strategy/StrategyEditorView.vue"), meta: { title: "策略编辑器", group: "lab" } },
  { path: "/decisions", name: "decisions", component: () => import("@/features/audit/DecisionView.vue"), meta: { title: "案例评价", group: "audit" } },
  { path: "/acceptance", name: "acceptance", component: () => import("@/features/audit/AcceptanceView.vue"), meta: { title: "质量验收", group: "audit" } },
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
