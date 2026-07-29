import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import MultiLineChart from "@/components/MultiLineChart.vue";

describe("MultiLineChart", () => {
  it("renders one accessible path and legend entry per series", () => {
    const wrapper = mount(MultiLineChart, { props: { valueLabel: "累计净值", series: [
      { name: "组合", color: "#111111", points: [{ label: "2026-01-01", value: 1 }, { label: "2026-01-02", value: 1.1 }] },
      { name: "沪深300", color: "#237a57", points: [{ label: "2026-01-01", value: 1 }, { label: "2026-01-02", value: 1.05 }] },
    ] } });
    expect(wrapper.get("[role=img]").attributes("aria-label")).toBe("累计净值，2 条曲线");
    expect(wrapper.findAll("path")).toHaveLength(2);
    expect(wrapper.text()).toContain("组合");
    expect(wrapper.text()).toContain("沪深300");
  });

  it("keeps an explicit empty state when all values are missing", () => {
    const wrapper = mount(MultiLineChart, { props: { valueLabel: "累计净值", series: [{ name: "组合", color: "#111111", points: [] }] } });
    expect(wrapper.text()).toContain("暂无可比较的历史曲线");
  });
});
