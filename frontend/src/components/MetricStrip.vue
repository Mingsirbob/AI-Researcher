<script setup lang="ts">
export interface MetricItem { label: string; value: string | number; note?: string; tone?: "positive" | "negative" | "neutral" }
defineProps<{ items: MetricItem[] }>();
</script>

<template>
  <div class="metric-strip">
    <div v-for="item in items" :key="item.label">
      <span>{{ item.label }}</span><strong :class="item.tone">{{ item.value }}</strong><small>{{ item.note }}</small>
    </div>
  </div>
</template>
