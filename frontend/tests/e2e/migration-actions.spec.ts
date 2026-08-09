import { expect, test, type Page, type Route } from "@playwright/test";

const json = (route: Route, body: unknown) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });

test("paper shows an empty-account state after its database is recreated", async ({ page }) => {
  let dashboardRequests = 0;
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/paper/accounts")) return json(route, { items: [] });
    if (url.pathname.endsWith("/paper/dashboard")) dashboardRequests += 1;
    return json(route, { items: [] });
  });

  await page.goto("/");

  await expect(page.getByText("暂无模拟账户，请点击上方 + 创建账户")).toBeVisible();
  await expect(page.getByText("正在读取数据")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "运行研究" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "运行完整批次" })).toBeDisabled();
  expect(dashboardRequests).toBe(0);
});

test("backtest renders run-level metrics and the current rebalance contract", async ({ page }) => {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/factor-lab/backtests/latest")) return json(route, { item: {
      run: { backtest_id: "backtest-1", metrics: { cumulative_return: 0.3, excess_cumulative_return: 0.1, max_drawdown: -0.2, sharpe_ratio: 0.8, average_turnover: 0.4, total_cost: 1234.5 }, limitations: ["仅用于历史研究"] },
      nav: [{ trading_date: "2026-07-24", nav: 1_300_000 }],
      rebalances: [{ signal_date: "2026-07-23", execution_date: "2026-07-24", executed_buy_count: 3, executed_sell_count: 2, blocked_buy_count: 1, blocked_sell_count: 1, turnover: 0.4, explicit_cost: 100, slippage_cost: 50 }],
    } });
    if (url.pathname.endsWith("/factor-lab/evaluations/latest")) return json(route, { item: { run: { evaluation_id: "evaluation-1" }, metrics: [{ factor_id: "momentum", factor_name: "动量" }] } });
    if (url.pathname.endsWith("/model-runs/latest") || url.pathname.endsWith("/current-shadow/latest")) return json(route, { item: null });
    return json(route, { items: [] });
  });
  await page.goto("/backtest");
  const metrics = page.locator(".metric-strip");
  await expect(metrics).toContainText("30.00%");
  await expect(metrics).toContainText("10.00%");
  await expect(metrics).toContainText("-20.00%");
  const rebalance = page.getByRole("row").filter({ hasText: "2026-07-23" });
  await expect(rebalance).toBeVisible();
  await expect(rebalance).toContainText("2026-07-24");
  await expect(rebalance.getByRole("gridcell").nth(4)).toHaveText("2");
  await expect(rebalance.getByRole("gridcell").nth(6)).toContainText("150");
  await expect(page.getByText("仅用于历史研究")).toBeVisible();
});

test("daily research candidate action posts without touching the real database", async ({ page }) => {
  let submitted: Record<string, unknown> | null = null;
  let realtimeRefreshes = 0;
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/paper/realtime-quotes/refresh") && route.request().method() === "POST") {
      realtimeRefreshes += 1;
      return json(route, { quote_count: 6, latest_quote_time: "2026-07-28 14:30:03+08:00", missing: [] });
    }
    if (url.pathname.endsWith("/paper/research-assessments") && route.request().method() === "POST") {
      submitted = route.request().postDataJSON();
      return json(route, { completed: 1, results: [{}] });
    }
    if (url.pathname.endsWith("/paper/accounts")) return json(route, { items: [{ account_id: "account-1", name: "模型组合", initial_cash: 1_000_000, cash: 1_000_000, benchmark_code: "000300.SH", strategy_id: "lightgbm_shadow_v1", strategy: { strategy_id: "lightgbm_shadow_v1", version: "1.0.0", name: "LightGBM Shadow", signal_source: "current_shadow", config: { target_gross_exposure: .5 } } }] });
    if (url.pathname.endsWith("/paper/strategies")) return json(route, { items: [] });
    if (url.pathname.endsWith("/paper/dashboard")) return json(route, { as_of: "2026-07-28", nav: 1_020_000, cumulative_return: .02, cash_weight: 1, account: { account_id: "account-1", name: "模型组合", cash: 1_000_000, strategy: { strategy_id: "lightgbm_shadow_v1", version: "1.0.0", name: "LightGBM Shadow", signal_source: "current_shadow", config: { target_gross_exposure: .5 } } }, positions: [], runs: [], nav_history: [], latest_shadow: { snapshot_id: "shadow-1", as_of: "2026-07-25", status: "current_shadow_ready" }, benchmark_comparison: { status: "live", valuation_mode: "intraday", latest_quote_time: "2026-07-28 14:30:03+08:00", inception_date: "2026-07-23", portfolio: { name: "模型组合", latest_return: .02, points: [{ date: "2026-07-28", cumulative_return: .02 }] }, benchmarks: [
      ["000001.SH", "上证指数", .01], ["000300.SH", "沪深300", .012], ["399001.SZ", "深证成指", .008],
      ["399006.SZ", "创业板指", -.004], ["000688.SH", "科创50", .006], ["000510.CSI", "中证A500", .009],
    ].map(([code, name, latestReturn]) => ({ code, name, status: "complete", latest_return: latestReturn, excess_return: .02 - Number(latestReturn), coverage: 1, points: [{ date: "2026-07-28", cumulative_return: latestReturn }] })), limitations: [] } });
    if (url.pathname.endsWith("/factor-snapshots/latest")) return json(route, { snapshot_id: "features-1", as_of: "2026-07-25", status: "completed" });
    if (url.pathname.endsWith("/paper/research-targets")) return json(route, { targets: [] });
    if (url.pathname.endsWith("/paper/daily-batches/latest")) return json(route, { item: null });
    return json(route, { items: [] });
  });
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "模拟盘" })).toBeVisible();
  await expect(page.getByText("账户收益对比")).toBeVisible();
  await expect(page.getByText("上证指数").first()).toBeVisible();
  await expect(page.getByText("科创50").first()).toBeVisible();
  await expect(page.getByText("中证A500").first()).toBeVisible();
  await expect(page.getByText("盘中估算 · 14:30:03 · 30秒更新")).toBeVisible();
  await expect.poll(() => realtimeRefreshes).toBeGreaterThanOrEqual(1);
  await expect(page.getByText("运行事件时间线")).toHaveCount(0);
  await page.getByRole("button", { name: "研究候选" }).click();
  await expect.poll(() => submitted).not.toBeNull();
  expect(submitted).toMatchObject({ account_id: "account-1", include_holdings: true, depth: "quick" });
});

test("paper switches account strategies and creates an isolated account", async ({ page }) => {
  let created: Record<string, unknown> | null = null;
  let archivedAccountId = "";
  const strategyById = {
    lightgbm_shadow_v1: { strategy_id: "lightgbm_shadow_v1", version: "1.0.0", name: "LightGBM Shadow", signal_source: "current_shadow", config: { target_gross_exposure: .5 } },
    multifactor_linear_v1: { strategy_id: "multifactor_linear_v1", version: "1.0.0", name: "线性多因子", signal_source: "multifactor_linear", config: { target_gross_exposure: .8 } },
  };
  const account = (id: string, name: string, strategyId: keyof typeof strategyById) => ({ account_id: id, name, initial_cash: 1_000_000, cash: 1_000_000, benchmark_code: "000300.SH", strategy_id: strategyId, strategy: strategyById[strategyId] });
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (/\/paper\/accounts\/[^/]+$/.test(url.pathname) && route.request().method() === "DELETE") {
      archivedAccountId = url.pathname.split("/").at(-1) || "";
      return route.fulfill({ status: 204 });
    }
    if (url.pathname.endsWith("/paper/accounts") && route.request().method() === "POST") { created = route.request().postDataJSON(); return json(route, account("account-3", String(created?.name), "multifactor_linear_v1")); }
    if (url.pathname.endsWith("/paper/accounts")) return json(route, { items: [
      account("account-1", "模型组合", "lightgbm_shadow_v1"),
      account("account-2", "因子组合", "multifactor_linear_v1"),
      ...(created ? [account("account-3", "新因子账户", "multifactor_linear_v1")] : []),
    ].filter((item) => item.account_id !== archivedAccountId) });
    if (url.pathname.endsWith("/paper/strategies")) return json(route, { items: Object.values(strategyById) });
    if (url.pathname.endsWith("/strategy-versions")) return json(route, { items: [{ strategy_version_id: "published-1", name: "发布版多因子", version: 1, strategy_kind: "structured" }] });
    if (url.pathname.endsWith("/paper/dashboard")) {
      const id = url.searchParams.get("account_id") || "account-1";
      const selected = id === "account-2" || id === "account-3" ? account(id, id === "account-2" ? "因子组合" : "新因子账户", "multifactor_linear_v1") : account("account-1", "模型组合", "lightgbm_shadow_v1");
      return json(route, { as_of: "2026-07-25", nav: 1_000_000, cumulative_return: 0, cash_weight: 1, account: selected, positions: [], runs: [], nav_history: [], latest_shadow: null, benchmark_comparison: { portfolio: null, benchmarks: [], limitations: [] } });
    }
    if (url.pathname.endsWith("/factor-snapshots/latest")) return json(route, { snapshot_id: "features-1", as_of: "2026-07-25", status: "completed" });
    if (url.pathname.endsWith("/paper/daily-batches/latest")) return json(route, { item: null });
    return json(route, { items: [] });
  });
  await page.goto("/");
  await page.locator("#paper-account").selectOption("account-2");
  await expect(page.getByText("线性多因子", { exact: true }).first()).toBeVisible();
  await page.getByRole("button", { name: "新建模拟账户" }).click();
  const accountForm = page.locator(".account-form");
  await expect(accountForm.getByLabel("策略选择")).toBeVisible();
  await expect(accountForm.getByText("已发布策略版本")).toHaveCount(0);
  await expect(accountForm.getByText("内置策略")).toHaveCount(0);
  await expect(accountForm.getByText("基准", { exact: true })).toHaveCount(0);
  await expect(accountForm.locator("#new-account-strategy-selection option")).toHaveCount(2);
  await page.getByLabel("账户名称").fill("新因子账户");
  await page.getByLabel("策略选择").selectOption("线性多因子");
  await page.getByRole("button", { name: "创建账户" }).click();
  await expect.poll(() => created).not.toBeNull();
  expect(created).toMatchObject({ name: "新因子账户", initial_cash: 1_000_000, strategy_name: "线性多因子" });

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "停用当前模拟账户" }).click();
  await expect.poll(() => archivedAccountId).toBe("account-3");
  await expect(page.locator("#paper-account")).toHaveValue("account-1");
});

test("paper order review and realtime settlement require explicit clicks", async ({ page }) => {
  const reviews: Array<{ path: string; body: Record<string, unknown> }> = [];
  let settlements = 0;
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (/\/paper\/orders\/order-1\/(approve|reject)$/.test(url.pathname) && route.request().method() === "POST") {
      reviews.push({ path: url.pathname, body: route.request().postDataJSON() });
      return json(route, {});
    }
    if (url.pathname.endsWith("/paper/settle/realtime") && route.request().method() === "POST") {
      settlements += 1;
      expect(route.request().postDataJSON()).toEqual({ account_id: "account-1" });
      return json(route, { filled: 1 });
    }
    if (url.pathname.endsWith("/paper/accounts")) return json(route, { items: [{ account_id: "account-1", name: "审批账户", initial_cash: 1_000_000, cash: 900_000, benchmark_code: "000300.SH", strategy_id: "lightgbm_shadow_v1", strategy: { strategy_id: "lightgbm_shadow_v1", version: "1.0.0", name: "LightGBM Shadow", signal_source: "current_shadow", config: {} } }] });
    if (url.pathname.endsWith("/paper/strategies")) return json(route, { items: [] });
    if (url.pathname.endsWith("/paper/dashboard")) return json(route, { as_of: "2026-07-28", nav: 1_000_000, cumulative_return: 0, cash_weight: .9, account: { account_id: "account-1", name: "审批账户", cash: 900_000, strategy: { strategy_id: "lightgbm_shadow_v1", version: "1.0.0", name: "LightGBM Shadow", signal_source: "current_shadow", config: {} } }, positions: [], runs: [{ run_id: "run-1", status: "proposed", orders: [{ order_id: "order-1", security_code: "300750.SZ", security_name: "宁德时代", side: "BUY", status: "proposed", quantity: 100, reference_price: 250, target_weight: .1 }] }], nav_history: [], benchmark_comparison: { portfolio: null, benchmarks: [], limitations: [] } });
    if (url.pathname.endsWith("/paper/orders")) {
      const proposal = { order_id: "order-1", run_id: "run-1", account_id: "account-1", as_of: "2026-07-28", run_status: "awaiting_review", strategy_version: "lightgbm_shadow_v1@1", security_code: "300750.SZ", security_name: "宁德时代", side: "buy", status: "proposed", quantity: 100, reference_price: 250, target_weight: .1, created_at: "2026-07-28T18:00:00+08:00" };
      return json(route, { account_id: "account-1", latest_run_id: "run-1", latest_as_of: "2026-07-28", items: [proposal], proposals: [proposal], trades: [] });
    }
    if (url.pathname.endsWith("/factor-snapshots/latest")) return json(route, { snapshot_id: "features-1", as_of: "2026-07-28", status: "completed" });
    if (url.pathname.endsWith("/paper/daily-batches/latest")) return json(route, { item: null });
    return json(route, { items: [] });
  });
  await page.goto("/");
  const order = page.getByRole("row").filter({ hasText: "300750.SZ" });
  await expect(order).toBeVisible();
  await expect(order).toContainText("宁德时代");
  expect(reviews).toHaveLength(0);
  expect(settlements).toBe(0);
  await page.getByRole("button", { name: "批准提案" }).click();
  await expect.poll(() => reviews.length).toBe(1);
  await page.getByRole("button", { name: "拒绝提案" }).click();
  await expect.poll(() => reviews.length).toBe(2);
  await page.getByRole("button", { name: "执行模拟撮合" }).click();
  await expect.poll(() => settlements).toBe(1);
  expect(reviews.map((item) => item.path)).toEqual(["/api/paper/orders/order-1/approve", "/api/paper/orders/order-1/reject"]);
  expect(reviews[0].body).toEqual({});
  expect(reviews[1].body).toEqual({});
});

test("factor release keeps an independent review note and submits approval", async ({ page }) => {
  let approval: Record<string, unknown> | null = null;
  const release = { candidate: { release_id: "release-1", factor_id: "momentum", factor_version: 1, status: "gate_passed", blocking_failure_count: 0, limitations: ["仅历史样本"], gate_version: "factor-release-gate-v1", result_hash: "result-hash", evaluation_result_hash: "evaluation-hash", backtest_result_hash: "backtest-hash" }, gates: [{ gate_key: "rank_ic", label: "方向 Rank IC", passed: true, observed: 0.05, comparator: ">=", threshold: 0.02, detail: "通过" }], decisions: [] };
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/releases/release-1/approve")) { approval = route.request().postDataJSON(); return json(route, { ...release, candidate: { ...release.candidate, status: "approved" } }); }
    if (url.pathname.endsWith("/factor-lab/overview")) return json(route, { factor_count: 1, status_counts: { testing: 1 } });
    if (url.pathname.endsWith("/factor-lab/factors")) return json(route, { items: [{ factor_id: "momentum", version: 1, name: "动量", status: "testing", lifecycle_status: "testing" }] });
    if (url.pathname.endsWith("/factor-lab/snapshots/latest")) return json(route, { item: null });
    if (url.pathname.endsWith("/factor-lab/evaluations/latest")) return json(route, { item: { run: { evaluation_id: "evaluation-1" } } });
    if (url.pathname.endsWith("/factor-lab/backtests/latest")) return json(route, { item: { run: { factor_id: "momentum", factor_version: 1, evaluation_id: "evaluation-1", backtest_id: "backtest-1" } } });
    if (url.pathname.endsWith("/factor-lab/releases/latest")) return json(route, { items: [release] });
    return json(route, { items: [] });
  });
  await page.goto("/factor-library");
  await page.getByLabel("审查说明").fill("证据链已复核");
  await page.getByRole("button", { name: "批准进入 Shadow" }).click();
  await expect.poll(() => approval).not.toBeNull();
  expect(approval).toEqual({ reviewer: "human", note: "证据链已复核" });
});

test("factor release reject is blocked until a review note is supplied", async ({ page }) => {
  let rejection: Record<string, unknown> | null = null;
  const release = { candidate: { release_id: "release-2", factor_id: "value", factor_version: 1, status: "gate_failed", limitations: [], gate_version: "v1" }, gates: [], decisions: [] };
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/releases/release-2/reject")) { rejection = route.request().postDataJSON(); return json(route, release); }
    if (url.pathname.endsWith("/factor-lab/overview")) return json(route, { factor_count: 1, status_counts: {} });
    if (url.pathname.endsWith("/factor-lab/factors")) return json(route, { items: [] });
    if (url.pathname.endsWith("/factor-lab/releases/latest")) return json(route, { items: [release] });
    if (url.pathname.endsWith("/factor-lab/snapshots/latest") || url.pathname.endsWith("/factor-lab/evaluations/latest") || url.pathname.endsWith("/factor-lab/backtests/latest")) return json(route, { item: null });
    return json(route, { items: [] });
  });
  await page.goto("/factor-library");
  await page.getByRole("button", { name: "拒绝", exact: true }).click();
  await page.waitForTimeout(100);
  expect(rejection).toBeNull();
  await page.getByLabel("审查说明").fill("门禁未通过");
  await page.getByRole("button", { name: "拒绝", exact: true }).click();
  await expect.poll(() => rejection).not.toBeNull();
  expect(rejection).toEqual({ reviewer: "human", note: "门禁未通过" });
});

test("decision maturity scan only evaluates explicitly selected cases", async ({ page }) => {
  let batch: Record<string, unknown> | null = null;
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/decision-outcomes/batch-evaluate")) { batch = route.request().postDataJSON(); return json(route, { results: [{ case_id: "case-ready", status: "ok", outcome: { case_id: "case-ready", status: "completed" } }], completed: 1, failed: 0 }); }
    if (url.pathname.endsWith("/decision-outcomes/maturity-scan")) return json(route, { as_of: "2026-07-27", counts: { ready: 1, pending: 1, completed: 0 }, boundary: "工作日估算", items: [{ case_id: "case-ready", security_code: "300750.SZ", as_of: "2026-01-01", horizon_trading_days: 60, estimated_observed_trading_days: 80, estimated_remaining_trading_days: 0, status: "ready" }, { case_id: "case-pending", security_code: "000001.SZ", as_of: "2026-07-01", horizon_trading_days: 60, estimated_observed_trading_days: 20, estimated_remaining_trading_days: 40, status: "pending" }] });
    if (url.pathname.endsWith("/decision-outcomes/attribution")) return json(route, { counts: { completed: 0 }, overall: {}, groups: {} });
    if (url.pathname.endsWith("/decision-outcomes")) return json(route, { items: [] });
    if (url.pathname.endsWith("/decision-cases")) return json(route, { items: [] });
    return json(route, { items: [] });
  });
  await page.goto("/decisions");
  await page.getByLabel("选择案例 case-ready").check();
  await page.getByRole("button", { name: "评价所选 1 项" }).click();
  await expect.poll(() => batch).not.toBeNull();
  expect(batch).toMatchObject({ case_ids: ["case-ready"] });
  await expect(page.getByText("结果状态：completed")).toBeVisible();
});

test("thesis claim confirmation is a deliberate PATCH", async ({ page }) => {
  let verdict: Record<string, unknown> | null = null;
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/claim-evaluations/evaluation-1")) { verdict = route.request().postDataJSON(); return json(route, {}); }
    if (url.pathname.endsWith("/theses/thesis-1/monitor")) return json(route, { thesis: { thesis_id: "thesis-1", title: "需求增长", core_claim: "需求持续增长", horizon: "6-12个月", status: "观察" }, claims: [], evaluations: [{ evaluation_id: "evaluation-1", statement: "订单增长", suggested_verdict: "维持", rationale: "证据稳定", status: "pending" }] });
    if (url.pathname.endsWith("/theses")) return json(route, { items: [{ thesis_id: "thesis-1", security_code: "300750.SZ", title: "需求增长", core_claim: "需求持续增长", horizon: "6-12个月", status: "观察", monitor: {} }] });
    return json(route, { items: [] });
  });
  await page.goto("/theses");
  await page.getByRole("button", { name: "查看详情" }).click();
  await page.getByRole("button", { name: "维持", exact: true }).click();
  await expect.poll(() => verdict).not.toBeNull();
  expect(verdict).toEqual({ verdict: "维持" });
});

test("thesis deletion honors cancel and only deletes after confirmation", async ({ page }) => {
  let deletions = 0;
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/theses/thesis-delete") && route.request().method() === "DELETE") { deletions += 1; return json(route, {}); }
    if (url.pathname.endsWith("/theses")) return json(route, { items: [{ thesis_id: "thesis-delete", security_code: "300750.SZ", title: "待删除论点", core_claim: "测试删除确认", horizon: "6-12个月", status: "观察", monitor: {} }] });
    return json(route, { items: [] });
  });
  await page.goto("/theses");
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.getByRole("button", { name: "删除论点" }).click();
  await page.waitForTimeout(100);
  expect(deletions).toBe(0);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "删除论点" }).click();
  await expect.poll(() => deletions).toBe(1);
});

test("company BM25 search renders a clickable source", async ({ page }) => {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/evidence-search")) return json(route, { retrieval_method: "fts5_bm25", items: [{ id: "e-1", category: "公告", label: "收入增长", title: "2025 年报", value: "营业收入增长", retrieval_score: 8.2, source: "/documents/report.pdf#page=12" }] });
    if (url.pathname.endsWith("/analysis")) return json(route, { security: { code: "300750.SZ", name: "宁德时代" }, as_of: "2026-07-25", metrics: {}, series: { dates: [], close: [], volume: [] }, evidence: [], claims: [], uncertainties: [] });
    if (url.pathname.endsWith("/master")) return json(route, { security_code: "300750.SZ", security_name: "宁德时代", source: "security_master" });
    if (url.pathname.includes("/research-runs") || url.pathname.includes("/assistant-history") || url.pathname.includes("/financial-facts")) return json(route, { items: [] });
    return json(route, { items: [] });
  });
  await page.goto("/company");
  await page.getByLabel("全文证据搜索").fill("营业收入");
  await page.getByRole("button", { name: "BM25 搜索" }).click();
  await expect(page.getByText("SEARCH · fts5_bm25")).toBeVisible();
  await expect(page.getByRole("link", { name: "打开原文" })).toHaveAttribute("href", "/documents/report.pdf#page=12");
});
