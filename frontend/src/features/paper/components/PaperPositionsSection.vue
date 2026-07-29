<script setup lang="ts">
import { money, pct } from "@/lib/format";
interface PaperPosition { security_code: string; security_name?: string; quantity: number; sellable_quantity: number; current_price?: number; market_value?: number; weight?: number; unrealized_pnl?: number; shadow_rank?: number }
defineProps<{ positions: PaperPosition[] }>();
</script>

<template>
  <section class="content-band"><div class="section-heading"><div><span>POSITIONS</span><b>当前持仓</b></div><small>{{ positions.length }} 只</small></div><div class="data-table-wrap"><table class="data-table"><thead><tr><th>证券</th><th>持仓/可卖</th><th>现价</th><th>市值</th><th>权重</th><th>浮动盈亏</th><th>Shadow</th></tr></thead><tbody><tr v-for="item in positions" :key="item.security_code"><td><b>{{ item.security_name||item.security_code }}</b><br><small>{{ item.security_code }}</small></td><td>{{ item.quantity }} / {{ item.sellable_quantity }}</td><td>{{ money(item.current_price) }}</td><td>{{ money(item.market_value) }}</td><td>{{ pct(item.weight) }}</td><td>{{ money(item.unrealized_pnl) }}</td><td>#{{ item.shadow_rank||'—' }}</td></tr><tr v-if="!positions.length"><td colspan="7">暂无持仓</td></tr></tbody></table></div></section>
</template>
