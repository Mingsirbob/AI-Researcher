<script setup lang="ts">
import { CircleCheck, CircleX, Clock3, Wrench } from "lucide-vue-next";
import type { RuntimeEvent } from "@/api/types";

defineProps<{ events: RuntimeEvent[] }>();
function label(item: RuntimeEvent) { return item.step_name || item.tool_name || item.event_type; }
</script>

<template>
  <ol class="audit-timeline">
    <li v-for="item in events" :key="item.event_id || item.seq">
      <CircleX v-if="item.status==='failed'||item.event_type.includes('fail')" :size="15" class="negative" />
      <CircleCheck v-else-if="item.status==='completed'||item.event_type.includes('complete')" :size="15" />
      <Wrench v-else-if="item.tool_name" :size="15" />
      <Clock3 v-else :size="15" />
      <div><header><b>{{ label(item) }}</b><code>#{{ item.seq }}</code></header><p>{{ item.message || item.status || item.event_type }}</p><small>{{ item.created_at }}</small><details v-if="item.payload && Object.keys(item.payload).length"><summary>事件载荷</summary><pre>{{ JSON.stringify(item.payload,null,2) }}</pre></details></div>
    </li>
    <li v-if="!events.length"><Clock3 :size="15" /><div><p>该批次尚无运行事件。</p></div></li>
  </ol>
</template>
