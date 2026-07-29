<script setup lang="ts">
import { AlertCircle, LoaderCircle, RefreshCw } from "lucide-vue-next";

defineProps<{ loading?: boolean; error?: unknown; empty?: boolean; emptyText?: string }>();
defineEmits<{ retry: [] }>();
</script>

<template>
  <div v-if="loading" class="async-state"><LoaderCircle class="spin" :size="18" /><span>正在读取数据</span></div>
  <div v-else-if="error" class="async-state error">
    <AlertCircle :size="18" /><span>{{ error instanceof Error ? error.message : String(error) }}</span>
    <button class="icon-button" title="重新加载" @click="$emit('retry')"><RefreshCw :size="16" /></button>
  </div>
  <div v-else-if="empty" class="async-state"><span>{{ emptyText || "暂无数据" }}</span></div>
  <slot v-else />
</template>
