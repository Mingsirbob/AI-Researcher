<script setup lang="ts">
import type { ColDef } from "ag-grid-community";
import { money, number, pct } from "@/lib/format";
import ResearchDataGrid from "@/components/ResearchDataGrid.vue";
interface PaperPosition { security_code: string; security_name?: string; quantity: number; available_quantity: number; close?: number; average_cost: number; daily_pnl?: number; weight?: number; unrealized_pnl?: number; unrealized_return?: number; risk_status?: string }
defineProps<{ positions: PaperPosition[] }>();
const pnlMoney = (value: unknown) => Number(value) < 0 ? `-${money(Math.abs(Number(value)))}` : money(value);
const columns: ColDef[] = [
  { field: "security_name", headerName: "证券", pinned: "left", minWidth: 145, flex: 1.3, valueFormatter: ({ value, data }) => `${value || data?.security_code || "—"} · ${data?.security_code || "—"}` },
  { headerName: "持仓/可卖", minWidth: 110, flex: 1, valueGetter: ({ data }) => data ? `${number(data.quantity, 0)} / ${number(data.available_quantity, 0)}` : "—" },
  { headerName: "现价/成本", minWidth: 135, flex: 1.15, valueGetter: ({ data }) => data ? `${money(data.close)} / ${money(data.average_cost)}` : "—" },
  { field: "unrealized_pnl", headerName: "总盈亏", filter: "agNumberColumnFilter", valueFormatter: ({ value }) => pnlMoney(value), minWidth: 108, flex: .95, cellClassRules: { negative: ({ value }) => Number(value) < 0, positive: ({ value }) => Number(value) > 0 } },
  { field: "daily_pnl", headerName: "单日盈亏", filter: "agNumberColumnFilter", valueFormatter: ({ value }) => pnlMoney(value), minWidth: 110, flex: 1, cellClassRules: { negative: ({ value }) => Number(value) < 0, positive: ({ value }) => Number(value) > 0 } },
  { field: "weight", headerName: "权重", filter: "agNumberColumnFilter", valueFormatter: ({ value }) => pct(value), minWidth: 86, flex: .8 },
  { field: "unrealized_return", headerName: "浮盈亏率", filter: "agNumberColumnFilter", valueFormatter: ({ value }) => pct(value), minWidth: 105, flex: .95, cellClassRules: { negative: ({ value }) => Number(value) < 0, positive: ({ value }) => Number(value) > 0 } },
  { field: "risk_status", headerName: "风险状态", minWidth: 100, flex: .9, cellClassRules: { negative: ({ value }) => value !== "正常" } },
];
</script>

<template>
  <section class="content-band"><div class="section-heading"><div><span>POSITIONS</span><b>当前持仓</b></div><small>{{ positions.length }} 只</small></div><ResearchDataGrid :rows="positions" :columns="columns" row-id="security_code" :height="Math.min(332, 80 + positions.length * 42)" empty-text="暂无持仓" /></section>
</template>

<style scoped>
:deep(.ag-cell.negative) { color: var(--danger); font-weight: 700; }
:deep(.ag-cell.positive) { color: #14745f; font-weight: 700; }
</style>
