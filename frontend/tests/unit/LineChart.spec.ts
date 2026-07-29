import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import LineChart from "@/components/LineChart.vue";

describe("LineChart", () => {
  it("renders a stable path and accessible label", () => {
    const wrapper = mount(LineChart, { props: { points: [{ label: "2026-01-01", value: 1 }, { label: "2026-01-02", value: 1.1 }], valueLabel: "策略净值" } });
    expect(wrapper.get("[role=img]").attributes("aria-label")).toContain("2 个数据点");
    expect(wrapper.get("path").attributes("d")).toContain("L");
    expect(wrapper.text()).toContain("策略净值 1.1000");
  });

  it("keeps the layout when no values are available", () => {
    const wrapper = mount(LineChart, { props: { points: [] } });
    expect(wrapper.text()).toContain("暂无可绘制数据");
  });
});
