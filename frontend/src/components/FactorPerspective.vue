<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import perspective from "@perspective-dev/client";
import "@perspective-dev/viewer";
import "@perspective-dev/viewer-datagrid";
import "@perspective-dev/viewer/dist/css/pro.css";

interface PerspectiveViewerElement extends HTMLElement {
  load(table: unknown): Promise<void>;
  restore(config: Record<string, unknown>): Promise<void>;
}
const props = defineProps<{ rows: object[] }>();
const viewer = ref<PerspectiveViewerElement>();
let worker: Awaited<ReturnType<typeof perspective.worker>> | undefined;
let table: Awaited<ReturnType<Awaited<ReturnType<typeof perspective.worker>>["table"]>> | undefined;

async function loadRows() {
  if (!viewer.value || !props.rows.length) return;
  await table?.delete();
  worker ||= await perspective.worker();
  table = await worker.table(props.rows as Record<string, unknown>[]);
  await viewer.value.load(table);
  await viewer.value.restore({ plugin: "Datagrid", settings: false });
}

onMounted(async () => { await nextTick(); await loadRows(); });
watch(() => props.rows, loadRows, { deep: true });
onBeforeUnmount(async () => { await table?.delete(); await worker?.terminate(); });
</script>

<template>
  <div class="perspective-shell">
    <perspective-viewer v-if="rows.length" ref="viewer" />
    <p v-else class="muted">暂无可透视的因子截面数据</p>
  </div>
</template>

<style scoped>
.perspective-shell { height: 520px; min-width: 0; border: 1px solid var(--line); background: white; }
perspective-viewer { width: 100%; height: 100%; --plugin--background: #fff; }
.perspective-shell > p { display: grid; height: 100%; place-items: center; margin: 0; }
</style>
