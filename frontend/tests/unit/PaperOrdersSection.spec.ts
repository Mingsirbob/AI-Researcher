import { mount } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";
import PaperOrdersSection from "@/features/paper/components/PaperOrdersSection.vue";
import type { PaperOrder } from "@/api/types";

const orders: PaperOrder[] = [
  {
    order_id: "current-proposal", run_id: "run-current", account_id: "account-1",
    as_of: "2026-08-04", run_status: "awaiting_review", strategy_version: "strategy@1",
    security_code: "000001.SZ", security_name: "平安银行", side: "buy", status: "proposed",
    quantity: 100, reference_price: 12.3, target_weight: 0.1, created_at: "2026-08-04T18:00:00+08:00",
  },
  {
    order_id: "historical-fill", run_id: "run-old", account_id: "account-1",
    as_of: "2026-08-01", run_status: "completed", strategy_version: "strategy@1",
    security_code: "000408.SZ", security_name: "藏格矿业", side: "sell", status: "filled",
    quantity: 1300, reference_price: 48, target_weight: 0, reviewer: "human",
    reviewed_at: "2026-08-01T08:30:00+08:00", fill_date: "2026-08-04", fill_price: 47.8,
    gross_amount: 62140, fees: 68, created_at: "2026-08-01T18:00:00+08:00",
  },
];

afterEach(() => vi.useRealTimers());

describe("PaperOrdersSection", () => {
  it("shows only the latest run as current proposals", async () => {
    const wrapper = mount(PaperOrdersSection, { props: {
      orders, latestRunId: "run-current", latestAsOf: "2026-08-04",
    } });

    expect(wrapper.text()).toContain("平安银行");
    expect(wrapper.text()).not.toContain("藏格矿业");
    expect(wrapper.text()).toContain("待人工审批");
    expect(wrapper.find('button[aria-label="批准提案"]').exists()).toBe(true);
    expect(wrapper.get("button.button").attributes("disabled")).toBeDefined();

    await wrapper.findAll('[role="tab"]')[1].trigger("click");
    expect(wrapper.text()).toContain("藏格矿业");
    expect(wrapper.find('button[aria-label="批准提案"]').exists()).toBe(false);
  });

  it("shows fill details in historical trades", async () => {
    const wrapper = mount(PaperOrdersSection, { props: {
      orders, latestRunId: "run-current", latestAsOf: "2026-08-04",
    } });

    await wrapper.findAll('[role="tab"]')[2].trigger("click");
    expect(wrapper.text()).toContain("藏格矿业");
    expect(wrapper.text()).toContain("2026-08-04");
    expect(wrapper.text()).toContain("费用");
    expect(wrapper.text()).toContain("已成交");
  });

  it("disables settlement during the lunch break", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-06T12:00:00+08:00"));
    const approvedOrders = [{ ...orders[0], status: "approved" as const }];
    const wrapper = mount(PaperOrdersSection, { props: {
      orders: approvedOrders, latestRunId: "run-current", latestAsOf: "2026-08-04",
    } });

    expect(wrapper.text()).toContain("午间休市，13:00 后可撮合");
    expect(wrapper.get("button.button").attributes("disabled")).toBeDefined();
  });

  it("enables settlement after the afternoon session opens", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-06T13:05:00+08:00"));
    const approvedOrders = [{ ...orders[0], status: "approved" as const }];
    const wrapper = mount(PaperOrdersSection, { props: {
      orders: approvedOrders, latestRunId: "run-current", latestAsOf: "2026-08-04",
    } });

    expect(wrapper.text()).toContain("1 条已人工批准");
    expect(wrapper.get("button.button").attributes("disabled")).toBeUndefined();
  });
});
