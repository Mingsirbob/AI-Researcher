<script setup lang="ts">
import { computed } from "vue";
import { useQuery } from "@tanstack/vue-query";
import { api } from "@/api/client";
import { money, number, pct } from "@/lib/format";
import PageHeader from "@/components/PageHeader.vue";
import MetricStrip from "@/components/MetricStrip.vue";
import AsyncState from "@/components/AsyncState.vue";
import BacktestAnalytics from "@/components/BacktestAnalytics.vue";

interface BacktestPoint { trading_date: string; nav?: number; benchmark_nav?: number; drawdown?: number }
interface BacktestRun { backtest_id: string; target_type: string; target_id: string; status: string; metrics: Record<string, number>; limitations?: string[] }
interface Backtest { item?: { run: BacktestRun; nav: BacktestPoint[]; rebalances: unknown[] } }

const latest = useQuery({
  queryKey: ["quant-research", "backtest", "latest"],
  queryFn: () => api<Backtest>("/api/quant-research/backtests/latest"),
});
const data = computed(() => latest.data.value?.item);
const chartPoints = computed(() =>
  (data.value?.nav || [])
    .filter((item): item is BacktestPoint & { nav: number } => Number.isFinite(item.nav))
    .map((item) => ({ trading_date: item.trading_date, nav: item.nav })),
);
const metrics = computed(() => {
  const values = data.value?.run.metrics || {};
  return [
    { label: "累计收益", value: pct(values.cumulative_return), note: "扣除成本" },
    { label: "超额收益", value: pct(values.excess_cumulative_return), note: "相对回测基准" },
    { label: "最大回撤", value: pct(values.max_drawdown), note: "逐日净值" },
    { label: "夏普比率", value: number(values.sharpe_ratio, 3), note: "无风险利率为 0" },
    { label: "平均换手", value: pct(values.average_turnover), note: "每次调仓" },
    { label: "总成本", value: money(values.total_cost), note: "佣金、税费与滑点" },
  ];
});
</script>

<template>
  <PageHeader eyebrow="BACKTEST" title="回测" description="统一查看因子或策略的历史回测结果。" />
  <MetricStrip :items="metrics" />
  <section class="content-band white">
    <AsyncState :loading="latest.isPending.value" :error="latest.error.value" :empty="!data" @retry="latest.refetch()">
      <div class="section-heading">
        <div><span>{{ data?.run.target_type }}</span><b>{{ data?.run.target_id }}</b></div>
        <small>{{ data?.run.backtest_id }}</small>
      </div>
      <BacktestAnalytics :points="chartPoints" />
      <div class="section-heading" style="margin-top:24px">
        <div><span>RESULT</span><b>回测结果</b></div>
        <small>{{ data?.run.status }}</small>
      </div>
      <div class="list">
        <article v-for="item in data?.run.limitations || []" :key="item" class="list-item"><p>{{ item }}</p></article>
      </div>
    </AsyncState>
  </section>
</template>
