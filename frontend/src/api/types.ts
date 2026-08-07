export interface ApiList<T> { items: T[] }

export interface FactorRow {
  security_code: string;
  security_name?: string;
  quality_status: "passed" | "excluded";
  in_candidate_pool?: boolean;
  return_20d?: number | null;
  return_60d?: number | null;
  volatility_60d?: number | null;
  avg_traded_value_20d?: number | null;
  max_drawdown_250d?: number | null;
  range_position_52w?: number | null;
  industry_l1?: string | null;
  market_cap?: number | null;
}

export interface FactorRowsResponse extends ApiList<FactorRow> { total: number }

export interface LinePoint { label: string; value: number | null | undefined }
export interface ChartSeries { name: string; color: string; points: LinePoint[] }

export interface RuntimeEvent {
  event_id: string;
  seq: number;
  event_type: string;
  status?: string;
  step_name?: string;
  tool_name?: string;
  message?: string;
  created_at: string;
  payload?: Record<string, unknown>;
}

export interface BatchStep {
  step_name: string;
  status: string;
  error?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  result?: Record<string, unknown> | null;
}

export interface DailyBatch {
  batch_id: string;
  status: string;
  as_of: string;
  current_step?: string | null;
  error?: string | null;
  steps: BatchStep[];
}

export interface BenchmarkSeries {
  code: string;
  name: string;
  status: string;
  latest_return?: number | null;
  excess_return?: number | null;
  coverage?: number | null;
  points: Array<{ date: string; cumulative_return: number }>;
}

export interface PaperTarget {
  security_code: string;
  security_name: string;
  shadow_rank?: number | null;
  score?: number | null;
  current_research_status?: string;
  target_reason?: string;
  current_assessment?: Record<string, unknown> | null;
}

export interface PaperOrder {
  order_id: string;
  run_id: string;
  account_id: string;
  as_of: string;
  run_status: string;
  strategy_version: string;
  security_code: string;
  security_name?: string;
  side: "buy" | "sell";
  status: "proposed" | "approved" | "rejected" | "filled" | "cancelled";
  quantity: number;
  reference_price?: number | null;
  target_weight?: number | null;
  reason?: Record<string, unknown>;
  reviewer?: string | null;
  review_note?: string | null;
  reviewed_at?: string | null;
  fill_date?: string | null;
  fill_price?: number | null;
  gross_amount?: number | null;
  fees?: number | null;
  execution_quote_id?: string | null;
  created_at: string;
}

export interface PaperOrderLedger {
  account_id: string;
  latest_run_id?: string | null;
  latest_as_of?: string | null;
  items: PaperOrder[];
}

export interface MaturityItem {
  case_id: string;
  security_code: string;
  as_of: string;
  horizon_trading_days: number;
  estimated_observed_trading_days: number;
  estimated_remaining_trading_days: number;
  status: "ready" | "pending" | "completed";
}

export interface ReleaseGate {
  name?: string;
  label?: string;
  passed: boolean;
  observed?: unknown;
  comparator?: string;
  threshold?: unknown;
  detail?: string;
}

export interface FactorRelease {
  candidate: {
    release_id: string;
    factor_id: string;
    factor_version: number;
    status: string;
    blocking_failure_count: number;
    limitations?: string[];
    gate_version?: string;
    result_hash?: string;
    evaluation_id?: string;
    evaluation_result_hash?: string;
    backtest_id?: string;
    backtest_result_hash?: string;
    created_by?: string;
    created_at?: string;
    decided_at?: string | null;
  };
  gates: ReleaseGate[];
  decisions: Array<{ decision_id: string; decision: string; reviewer: string; note: string; created_at: string }>;
}

export interface FactorVersion {
  factor_id: string;
  version: number;
  name: string;
  description?: string;
  expression?: string;
  formula?: string;
  template_id?: string;
  direction?: string;
  status?: string;
  lifecycle_status?: string;
  model_used?: boolean;
  category?: string;
  usage_scope?: "model_bundle" | "strategy_component" | "research_only" | "retired";
  strategy_compatible?: boolean;
  strategy_eligible?: boolean;
  strategy_runtime_field?: string | null;
}

export interface FactorLabRun {
  evaluation_id?: string;
  backtest_id?: string;
  factor_id?: string;
  factor_version?: number;
  end_date?: string;
}

export interface DecisionArtifact { snapshot_hash?: string; schema_version?: string }
export interface DecisionCase {
  case_id: string;
  security_code: string;
  security_name?: string;
  as_of: string;
  decision_horizon_days: number;
  benchmark_name?: string;
  rule_status: string;
  review_status: string;
  policy_version?: string;
  shadow_snapshot_id?: string;
  scores?: Record<string, { score?: number }>;
  gates?: Array<{ name: string; passed: boolean; detail?: string; observed?: unknown }>;
  artifacts?: Record<string, DecisionArtifact>;
  reviews?: Array<{ reviewer: string; decision: string; note: string }>;
  reused?: boolean;
}

export interface DecisionOutcome {
  outcome_id?: string;
  case_id: string;
  status: string;
  security_return?: number | null;
  excess_return?: number | null;
  maximum_adverse_excursion?: number | null;
  metrics?: { observed_trading_days?: number; remaining_trading_days?: number };
}

export interface MaturityScan extends ApiList<MaturityItem> {
  as_of: string;
  counts: { ready: number; pending: number; completed: number };
  boundary: string;
}

export interface BatchEvaluationResult {
  results: Array<{ case_id: string; status: "ok" | "failed"; outcome?: DecisionOutcome; error?: unknown; http_status?: number }>;
  completed: number;
  failed: number;
}

export interface UniverseMember extends UnknownRecord {
  security_code: string;
  security_name?: string;
  universe?: string;
  universe_name?: string;
  universe_source?: string;
  effective_from?: string;
  effective_to?: string | null;
  universe_as_of?: string;
}

export interface UniverseMembership extends ApiList<UniverseMember> {
  as_of: string;
  universe?: string | null;
  source: string;
  history_contract?: string;
}

export interface NeutralizedRow extends FactorRow {
  industry_l1?: string;
  market_cap?: number | null;
  factor_value?: number | null;
  neutralized_value?: number;
  neutralized_zscore?: number;
  neutralization_reason?: string;
}

export interface NeutralizationResult {
  items: NeutralizedRow[];
  excluded: NeutralizedRow[];
  method: string;
  industry_count: number;
  baseline_industry?: string;
  coefficients?: number[];
}

export interface Thesis {
  id?: string;
  thesis_id?: string;
  security_code?: string;
  code?: string;
  title: string;
  core_claim: string;
  horizon: string;
  status: string;
  monitor?: { baseline?: { run_id?: string; as_of?: string } | null };
}

export interface ThesisMonitor {
  thesis?: Thesis;
  claims?: Array<{ claim_id?: string; statement: string; last_verdict?: string }>;
  evaluations?: Array<{ evaluation_id: string; statement: string; suggested_verdict?: string; rationale?: string; status: string; confirmed_verdict?: string; verdict?: string }>;
}

export interface AssistantHistoryItem {
  interaction_id: string;
  interaction_type: string;
  question: string;
  mode: string;
  created_at: string;
  evidence_count: number;
}

export type UnknownRecord = Record<string, unknown>;
