<script setup lang="ts">
import { Check, Search, X } from "lucide-vue-next";
import { money, pct } from "@/lib/format";

interface PaperOrder { order_id: string; security_code: string; security_name?: string; side: string; status: string; quantity: number; reference_price?: number; target_weight?: number }
defineProps<{ orders: PaperOrder[] }>();
defineEmits<{ settle: []; trace: [order: PaperOrder]; review: [id: string, decision: "approve" | "reject"] }>();
</script>

<template>
  <section class="content-band white"><div class="section-heading"><div><span>HUMAN GATE</span><b>交易提案</b></div><button class="button secondary" @click="$emit('settle')">执行模拟撮合</button></div><div class="list"><article v-for="order in orders" :key="order.order_id" class="list-item"><header><div><span :class="['status',order.status==='proposed'?'warn':'']">{{ order.side }} · {{ order.status }}</span><h3 style="margin-top:8px">{{ order.security_name }} · {{ order.security_code }}</h3></div><b>{{ order.quantity }} 股</b></header><p>参考价 {{ money(order.reference_price) }} · 目标权重 {{ pct(order.target_weight) }}</p><footer><button class="button secondary" @click="$emit('trace', order)"><Search :size="13" />完整决策链</button><template v-if="order.status==='proposed'"><button class="button" title="批准" @click="$emit('review', order.order_id, 'approve')"><Check :size="14" />批准</button><button class="button danger" title="拒绝" @click="$emit('review', order.order_id, 'reject')"><X :size="14" />拒绝</button></template></footer></article><article v-if="!orders.length" class="list-item"><p>当前没有交易提案。</p></article></div></section>
</template>
