import { shallowMount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import MultiLineChart from "@/components/MultiLineChart.vue";
import EChart from "@/components/EChart.vue";

describe("MultiLineChart", () => {
  it("passes aligned series to ECharts and keeps an accessible legend", () => {
    const wrapper = shallowMount(MultiLineChart, { props: { valueLabel: "累计净值", series: [
      { name: "组合", color: "#111111", points: [{ label: "2026-01-01", value: 1 }, { label: "2026-01-02", value: 1.1 }] },
      { name: "沪深300", color: "#237a57", points: [{ label: "2026-01-01", value: 1 }, { label: "2026-01-02", value: 1.05 }] },
    ] } });
    const chart = wrapper.getComponent(EChart);
    expect(chart.props("ariaLabel")).toBe("累计净值，2 条曲线");
    expect(chart.props("option").series).toHaveLength(2);
    expect(wrapper.text()).toContain("组合");
    expect(wrapper.text()).toContain("沪深300");
  });

  it("keeps an explicit empty state when all values are missing", () => {
    const wrapper = shallowMount(MultiLineChart, { props: { valueLabel: "累计净值", series: [{ name: "组合", color: "#111111", points: [] }] } });
    expect(wrapper.getComponent(EChart).props("empty")).toBe(true);
    expect(wrapper.text()).toContain("暂无可比较的历史曲线");
  });

  it("keeps date gaps explicit without rendering NaN and shows isolated live points", () => {
    const wrapper = shallowMount(MultiLineChart, { props: { valueLabel: "累计净值", series: [
      { name: "模拟组合", color: "#111111", points: [
        { label: "2026-07-28", value: 0.983 },
        { label: "2026-07-29", value: 0.9902 },
      ] },
      { name: "上证指数", color: "#237a57", points: [
        { label: "2026-07-27", value: 0.9977 },
        { label: "2026-07-29", value: 0.99 },
      ] },
    ] } });
    const option = wrapper.getComponent(EChart).props("option");
    expect(option.xAxis.data).toEqual(["2026-07-27", "2026-07-28", "2026-07-29"]);
    expect(option.series[1].data).toEqual([0.9977, null, 0.99]);
    expect(option.series[1].showSymbol).toBe(true);
    expect(option.tooltip.valueFormatter(null)).toBe("—");
    expect(option.tooltip.valueFormatter(Number.NaN)).toBe("—");
  });
});
