import { shallowMount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import PaperPositionsSection from "@/features/paper/components/PaperPositionsSection.vue";
import ResearchDataGrid from "@/components/ResearchDataGrid.vue";

describe("PaperPositionsSection", () => {
  it("uses the paper dashboard position contract for the compact seven-column view", () => {
    const position = {
      security_code: "000408.SZ",
      security_name: "藏格矿业",
      quantity: 1300,
      available_quantity: 700,
      close: 79.6,
      average_cost: 78.36,
      daily_pnl: 247,
      weight: 0.1045,
      unrealized_pnl: 1612,
      unrealized_return: 0.0158,
      risk_status: "正常",
    };
    const wrapper = shallowMount(PaperPositionsSection, { props: { positions: [position] } });
    const grid = wrapper.getComponent(ResearchDataGrid);
    const columns = grid.props("columns");

    expect(columns.map((column: { headerName: string }) => column.headerName)).toEqual([
      "证券", "持仓/可卖", "现价/成本", "总盈亏", "单日盈亏", "权重", "浮盈亏率", "风险状态",
    ]);
    expect(columns[1].valueGetter({ data: position })).toBe("1,300 / 700");
    expect(columns[2].valueGetter({ data: position })).toBe("¥79.6 / ¥78.36");
    expect(columns[3].valueFormatter({ value: -598.31 })).toBe("-¥598.31");
    expect(columns[4].valueFormatter({ value: -55 })).toBe("-¥55");
    expect(grid.props("height")).toBe(122);
  });
});
