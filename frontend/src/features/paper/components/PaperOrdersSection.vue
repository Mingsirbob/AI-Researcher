<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { Check, Search, X } from "lucide-vue-next";
import type { PaperOrder } from "@/api/types";
import { money, pct } from "@/lib/format";

type LedgerTab = "current" | "history" | "fills";

const props = defineProps<{
  orders: PaperOrder[];
  latestRunId?: string | null;
  latestAsOf?: string | null;
}>();
const emit = defineEmits<{
  settle: [];
  trace: [order: PaperOrder];
  review: [id: string, decision: "approve" | "reject"];
}>();
const activeTab = ref<LedgerTab>("current");
const clock = ref(new Date());
let clockTimer = 0;
const currentOrders = computed(() => props.orders.filter((item) => item.run_id === props.latestRunId));
const historicalOrders = computed(() => props.orders.filter((item) => item.run_id !== props.latestRunId));
const fills = computed(() => props.orders.filter((item) => item.status === "filled"));
const visibleOrders = computed(() => activeTab.value === "current"
  ? currentOrders.value
  : activeTab.value === "history" ? historicalOrders.value : fills.value);
const approvedCount = computed(() => currentOrders.value.filter((item) => item.status === "approved").length);
const marketSession = computed(() => {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai", weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(clock.value);
  const values = Object.fromEntries(parts.map((item) => [item.type, item.value]));
  const minutes = Number(values.hour) * 60 + Number(values.minute);
  if (["Sat", "Sun"].includes(values.weekday)) return "closed";
  if (minutes >= 570 && minutes <= 690) return "open";
  if (minutes > 690 && minutes < 780) return "lunch";
  if (minutes >= 780 && minutes <= 900) return "open";
  return "closed";
});
const marketSessionOpen = computed(() => marketSession.value === "open");
const marketSessionStatus = computed(() => marketSession.value === "lunch"
  ? "午间休市，13:00 后可撮合"
  : "已收市，下一交易日可撮合");
const settlementDisabled = computed(() => approvedCount.value === 0 || !marketSessionOpen.value);
const tabs = computed(() => [
  { id: "current" as const, label: "当前提案", count: currentOrders.value.length },
  { id: "history" as const, label: "历史提案", count: historicalOrders.value.length },
  { id: "fills" as const, label: "历史成交", count: fills.value.length },
]);
const statusLabels: Record<PaperOrder["status"], string> = {
  proposed: "待人工审批",
  approved: "已批准待撮合",
  rejected: "已拒绝",
  filled: "已成交",
  cancelled: "已撤销",
};
const sideLabels = { buy: "买入", sell: "卖出" };
function statusClass(status: PaperOrder["status"]) {
  return { warn: status === "proposed" || status === "approved", fail: status === "rejected" || status === "cancelled" };
}
onMounted(() => { clockTimer = window.setInterval(() => { clock.value = new Date(); }, 30_000); });
onBeforeUnmount(() => window.clearInterval(clockTimer));
</script>

<template>
  <section class="content-band white order-ledger">
    <div class="section-heading">
      <div><span>HUMAN GATE</span><b>交易提案与成交台账</b></div>
      <div class="settlement-control">
        <small>{{ !marketSessionOpen ? marketSessionStatus : approvedCount ? `${approvedCount} 条已人工批准` : "无待撮合订单" }}</small>
        <button class="button secondary" :disabled="settlementDisabled" :title="!marketSessionOpen ? '实时模拟撮合仅允许交易日 09:30–11:30、13:00–15:00' : approvedCount ? '按审批后的当前实时价执行模拟撮合' : '请先人工批准当前提案'" @click="$emit('settle')">执行模拟撮合</button>
      </div>
    </div>
    <div class="ledger-toolbar">
      <div class="segmented" role="tablist" aria-label="提案与成交视图">
        <button v-for="tab in tabs" :key="tab.id" type="button" role="tab" :aria-selected="activeTab === tab.id" :class="{ active: activeTab === tab.id }" @click="activeTab = tab.id">{{ tab.label }} <span>{{ tab.count }}</span></button>
      </div>
      <small v-if="activeTab === 'current'">研究日 {{ latestAsOf || '—' }}</small>
    </div>
    <div class="ledger-table-wrap">
      <table class="ledger-table">
        <thead><tr><th>研究日</th><th>证券</th><th>方向</th><th>数量</th><th>{{ activeTab === 'fills' ? '成交价' : '参考价' }}</th><th>{{ activeTab === 'fills' ? '成交金额 / 费用' : '目标权重' }}</th><th>状态</th><th>{{ activeTab === 'fills' ? '成交日' : '复核信息' }}</th><th><span class="sr-only">操作</span></th></tr></thead>
        <tbody>
          <tr v-for="order in visibleOrders" :key="order.order_id">
            <td data-label="研究日">{{ order.as_of }}</td>
            <td data-label="证券"><b>{{ order.security_name || order.security_code }}</b><small>{{ order.security_code }}</small></td>
            <td data-label="方向"><span :class="['side', order.side]">{{ sideLabels[order.side] }}</span></td>
            <td data-label="数量">{{ order.quantity.toLocaleString() }} 股</td>
            <td :data-label="activeTab === 'fills' ? '成交价' : '参考价'">{{ money(activeTab === 'fills' ? order.fill_price : order.reference_price) }}</td>
            <td v-if="activeTab === 'fills'" data-label="成交金额 / 费用"><b>{{ money(order.gross_amount) }}</b><small>费用 {{ money(order.fees) }}</small></td>
            <td v-else data-label="目标权重">{{ pct(order.target_weight) }}</td>
            <td data-label="状态"><span :class="['status', statusClass(order.status)]">{{ statusLabels[order.status] }}</span></td>
            <td v-if="activeTab === 'fills'" data-label="成交日"><b>{{ order.fill_date || '—' }}</b><small v-if="order.execution_quote_id">实时行情留痕</small></td>
            <td v-else data-label="复核信息"><b>{{ order.reviewer || '—' }}</b><small>{{ order.reviewed_at || (order.status === 'proposed' ? '等待人工复核' : '—') }}</small></td>
            <td class="row-actions">
              <button class="icon-button" title="查看决策依据" aria-label="查看决策依据" @click="emit('trace', order)"><Search :size="14" /></button>
              <template v-if="activeTab === 'current' && order.status === 'proposed'">
                <button class="icon-button approve" title="批准提案" aria-label="批准提案" @click="emit('review', order.order_id, 'approve')"><Check :size="14" /></button>
                <button class="icon-button reject" title="拒绝提案" aria-label="拒绝提案" @click="emit('review', order.order_id, 'reject')"><X :size="14" /></button>
              </template>
            </td>
          </tr>
          <tr v-if="!visibleOrders.length"><td colspan="9" class="empty">{{ activeTab === 'current' ? '最新完整研究批次没有生成交易提案' : activeTab === 'history' ? '暂无历史提案' : '暂无历史成交' }}</td></tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
.order-ledger { min-width: 0; overflow: hidden; padding-bottom: 18px; }
.settlement-control { display: flex; align-items: center; gap: 10px; }
.settlement-control small,.ledger-toolbar > small { color: var(--muted); }
.ledger-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.segmented { display: inline-flex; border: 1px solid var(--line); background: var(--paper); }
.segmented button { min-height: 32px; padding: 0 12px; border: 0; border-right: 1px solid var(--line); background: transparent; color: var(--muted); cursor: pointer; }
.segmented button:last-child { border-right: 0; }
.segmented button.active { background: #18201d; color: #fff; }
.segmented span { margin-left: 5px; font-size: 9px; opacity: .7; }
.ledger-table-wrap { overflow-x: auto; border: 1px solid var(--line); }
.ledger-table { width: 100%; min-width: 1040px; border-collapse: collapse; font-size: 10px; }
.ledger-table th { padding: 9px 10px; border-bottom: 1px solid var(--line); background: #f2f4ef; color: var(--muted); text-align: left; font-weight: 600; }
.ledger-table td { padding: 10px; border-bottom: 1px solid var(--line); vertical-align: middle; }
.ledger-table tbody tr:last-child td { border-bottom: 0; }
.ledger-table td b,.ledger-table td small { display: block; }
.ledger-table td small { margin-top: 3px; color: var(--muted); font-size: 8px; }
.side { font-weight: 700; }
.side.buy { color: #b8483e; }
.side.sell { color: #176f63; }
.row-actions { width: 120px; white-space: nowrap; text-align: right; }
.row-actions .icon-button { margin-left: 4px; }
.row-actions .approve { color: #176f63; }
.row-actions .reject { color: #b8483e; }
.empty { height: 96px; color: var(--muted); text-align: center; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0,0,0,0); }
@media (max-width: 700px) {
  .section-heading,.ledger-toolbar { align-items: stretch; flex-direction: column; }
  .settlement-control { justify-content: space-between; }
  .segmented { width: 100%; }
  .segmented button { flex: 1; min-width: 0; padding: 0 6px; }
  .ledger-table-wrap { overflow: hidden; }
  .ledger-table { display: block; min-width: 0; }
  .ledger-table thead { display: none; }
  .ledger-table tbody { display: block; }
  .ledger-table tr { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); padding: 8px; border-bottom: 1px solid var(--line); }
  .ledger-table tbody tr:last-child { border-bottom: 0; }
  .ledger-table td { min-width: 0; padding: 7px 8px; border: 0; overflow-wrap: anywhere; }
  .ledger-table td::before { display: block; margin-bottom: 4px; color: var(--muted); content: attr(data-label); font-size: 8px; }
  .ledger-table .row-actions,.ledger-table .empty { grid-column: 1 / -1; width: auto; }
  .ledger-table .row-actions::before,.ledger-table .empty::before { display: none; }
  .ledger-table .empty { height: auto; padding: 32px 8px; }
}
</style>
