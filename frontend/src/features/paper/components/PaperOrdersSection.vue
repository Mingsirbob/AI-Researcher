<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { ColDef } from "ag-grid-community";
import { Check, Search, X } from "lucide-vue-next";
import { money, pct } from "@/lib/format";
import ResearchDataGrid from "@/components/ResearchDataGrid.vue";

interface PaperOrder { order_id: string; security_code: string; security_name?: string; side: string; status: string; quantity: number; reference_price?: number; target_weight?: number }
const props = defineProps<{ orders: PaperOrder[] }>();
const emit = defineEmits<{ settle: []; trace: [order: PaperOrder]; review: [id: string, decision: "approve" | "reject"] }>();
const selectedOrderId = ref("");
watch(() => props.orders, (orders) => {
  if (!orders.some((item) => item.order_id === selectedOrderId.value)) selectedOrderId.value = orders[0]?.order_id || "";
}, { immediate: true, deep: true });
const selectedOrder = computed(() => props.orders.find((item) => item.order_id === selectedOrderId.value));
const columns: ColDef[] = [
  { field: "security_name", headerName: "证券", pinned: "left", minWidth: 128 },
  { field: "security_code", headerName: "代码", pinned: "left", minWidth: 110 },
  { field: "side", headerName: "方向", filter: true, maxWidth: 88 },
  { field: "status", headerName: "状态", filter: true, minWidth: 105 },
  { field: "quantity", headerName: "数量", filter: "agNumberColumnFilter", minWidth: 94 },
  { field: "reference_price", headerName: "参考价", valueFormatter: ({ value }) => money(value), minWidth: 108 },
  { field: "target_weight", headerName: "目标权重", valueFormatter: ({ value }) => pct(value), minWidth: 110 },
];
function selectOrder(row: Record<string, unknown>) {
  selectedOrderId.value = String(row.order_id || "");
}
</script>

<template>
  <section class="content-band white"><div class="section-heading"><div><span>HUMAN GATE</span><b>交易提案</b></div><button class="button secondary" @click="$emit('settle')">执行模拟撮合</button></div><div v-if="selectedOrder" class="order-command-bar" aria-live="polite"><div><span>当前提案</span><b>{{ selectedOrder.security_name || selectedOrder.security_code }} · {{ selectedOrder.security_code }}</b><small>{{ selectedOrder.side }} · {{ selectedOrder.quantity }} 股 · {{ selectedOrder.status }}</small></div><div><button class="button secondary" @click="emit('trace', selectedOrder)"><Search :size="13" />决策依据</button><template v-if="selectedOrder.status==='proposed'"><button class="button" @click="emit('review', selectedOrder.order_id, 'approve')"><Check :size="14" />批准</button><button class="button danger" @click="emit('review', selectedOrder.order_id, 'reject')"><X :size="14" />拒绝</button></template></div></div><ResearchDataGrid :rows="orders" :columns="columns" row-id="order_id" :height="390" empty-text="当前没有交易提案" @row-select="selectOrder" /></section>
</template>

<style scoped>
.order-command-bar { min-height: 58px; display: flex; align-items: center; justify-content: space-between; gap: 14px; margin-bottom: 10px; padding: 9px 11px; border: 1px solid var(--line); border-left: 3px solid var(--teal); background: #f8faf6; }
.order-command-bar span,.order-command-bar b,.order-command-bar small { display: block; }
.order-command-bar span,.order-command-bar small { color: var(--muted); font-size: 8px; }
.order-command-bar b { margin: 4px 0; font-size: 11px; }
.order-command-bar > div:last-child { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
@media (max-width: 560px) { .order-command-bar { align-items: stretch; flex-direction: column; } .order-command-bar > div:last-child { justify-content: flex-start; } }
</style>
