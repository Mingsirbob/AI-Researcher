<script setup lang="ts">
import { computed } from "vue";
import { AgGridVue } from "ag-grid-vue3";
import {
  CellStyleModule,
  ClientSideRowModelModule,
  ColumnAutoSizeModule,
  ModuleRegistry,
  NumberFilterModule,
  RowSelectionModule,
  TextFilterModule,
  themeQuartz,
  type CellClickedEvent,
  type ColDef,
  type RowClickedEvent,
} from "ag-grid-community";

ModuleRegistry.registerModules([
  ClientSideRowModelModule,
  ColumnAutoSizeModule,
  TextFilterModule,
  NumberFilterModule,
  CellStyleModule,
  RowSelectionModule,
]);

const props = withDefaults(defineProps<{
  rows: object[];
  columns: ColDef[];
  height?: number;
  rowId?: string;
  emptyText?: string;
}>(), { height: 360, rowId: "id", emptyText: "暂无数据" });

const emit = defineEmits<{
  cellAction: [action: string, row: Record<string, unknown>];
  rowSelect: [row: Record<string, unknown>];
}>();
const gridTheme = themeQuartz.withParams({
  accentColor: "#006b5e",
  backgroundColor: "#ffffff",
  borderColor: "#d9ded9",
  browserColorScheme: "light",
  chromeBackgroundColor: "#f3f5f0",
  fontFamily: '"Microsoft YaHei UI", "PingFang SC", sans-serif',
  foregroundColor: "#161b18",
  headerBackgroundColor: "#eef0eb",
  headerFontSize: 10,
  headerFontWeight: 700,
  rowHoverColor: "#e8f3ef",
  spacing: 6,
});
const defaultColDef = { sortable: true, filter: true, resizable: true, minWidth: 92 } satisfies ColDef;
const style = computed(() => ({ height: `${props.height}px` }));

function onCellClicked(event: CellClickedEvent) {
  const element = event.event?.target as HTMLElement | null;
  const action = element?.closest<HTMLElement>("[data-grid-action]")?.dataset.gridAction;
  if (action && event.data) emit("cellAction", action, event.data as Record<string, unknown>);
}
function onRowClicked(event: RowClickedEvent) {
  if (event.data) emit("rowSelect", event.data as Record<string, unknown>);
}
</script>

<template>
  <div class="research-grid" :style="style">
    <AgGridVue
      v-if="rows.length"
      style="width:100%;height:100%"
      :theme="gridTheme"
      :row-data="rows"
      :column-defs="columns"
      :default-col-def="defaultColDef"
      :get-row-id="(params: { data: object }) => { const row = params.data as Record<string, unknown>; return String(row[rowId] ?? JSON.stringify(row)); }"
      :row-height="42"
      :header-height="38"
      :animate-rows="false"
      :row-selection="{ mode: 'singleRow', checkboxes: false, enableClickSelection: true }"
      :suppress-column-virtualisation="true"
      :suppress-cell-focus="false"
      @cell-clicked="onCellClicked"
      @row-clicked="onRowClicked"
    />
    <p v-else class="grid-empty">{{ emptyText }}</p>
  </div>
</template>

<style scoped>
.research-grid { width: 100%; min-width: 0; border: 1px solid var(--line); background: white; }
.research-grid :deep(.ag-root-wrapper) { border: 0; border-radius: 0; }
.research-grid :deep(.ag-cell) { display: flex; align-items: center; font-size: 10px; }
.research-grid :deep(.ag-header-cell-text) { font-size: 9px; }
.research-grid :deep(.ag-paging-panel) { font-size: 9px; }
.grid-empty { display: grid; height: 100%; place-items: center; margin: 0; color: var(--muted); font-size: 10px; }
</style>
