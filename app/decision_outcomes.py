from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timezone

from .primitives import canonical_hash, sha256_text
from .research_store import ResearchStore


EVALUATION_VERSION = "decision-outcome-v1"
ADJUSTMENT = "forward"
IFIND_PARAMS = "CPS:2"
MIN_RELIABLE_GROUP_SIZE = 5


def _normalize_rows(rows: list[dict], label: str) -> list[dict]:
    normalized: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        trading_date = str(row.get("time") or row.get("date") or "")[:10]
        try:
            date.fromisoformat(trading_date)
            close = float(row.get("close"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}包含非法日期或收盘价") from exc
        if not math.isfinite(close) or close <= 0:
            raise ValueError(f"{label}在 {trading_date} 的收盘价必须为有限正数")
        if trading_date in seen:
            raise ValueError(f"{label}包含重复交易日 {trading_date}")
        seen.add(trading_date)
        normalized.append({"date": trading_date, "close": close})
    normalized.sort(key=lambda item: item["date"])
    return normalized


def _bucket(value: float, *, kind: str) -> str:
    if kind == "risk":
        return "low_0_39" if value < 40 else "medium_40_59" if value < 60 else "high_60_100"
    return "low_0_39" if value < 40 else "medium_40_69" if value < 70 else "high_70_100"


class DecisionOutcomeService:
    def __init__(self, research_store: ResearchStore):
        self.research_store = research_store

    def evaluate(
        self,
        case_id: str,
        *,
        security_rows: list[dict],
        benchmark_rows: list[dict],
        data_source: str,
        adjustment: str = ADJUSTMENT,
        ifind_params: str = IFIND_PARAMS,
    ) -> dict:
        case = self.research_store.decision_case(case_id)
        if case is None:
            raise KeyError(case_id)

        gates: list[dict] = []

        def gate(name: str, passed: bool, observed: object, expected: str, detail: str) -> None:
            gates.append({
                "name": name, "passed": bool(passed), "observed": observed,
                "expected": expected, "detail": detail,
            })

        contract_error: str | None = None
        try:
            security = _normalize_rows(security_rows, "证券序列")
            benchmark = _normalize_rows(benchmark_rows, "基准序列")
        except ValueError as exc:
            security, benchmark = [], []
            contract_error = str(exc)

        gate("adjustment", adjustment == ADJUSTMENT and ifind_params == IFIND_PARAMS,
             f"{adjustment}/{ifind_params}", "forward/CPS:2", "结果评价固定使用前复权收盘价。")
        gate("row_contract", contract_error is None, contract_error or "valid", "日期唯一且 close 为有限正数", "异常字段不得进入结果计算。")

        eligible = [item for item in security if item["date"] >= case["as_of"]]
        entry = eligible[0] if eligible else None
        horizon = int(case["decision_horizon_days"])
        observed_days = max(0, len(eligible) - 1) if entry else 0
        completed = observed_days >= horizon
        evaluation_rows = eligible[: horizon + 1] if completed else eligible
        benchmark_by_date = {item["date"]: item for item in benchmark}
        missing_benchmark_dates = [
            item["date"] for item in evaluation_rows if item["date"] not in benchmark_by_date
        ]
        gate("entry_available", entry is not None, entry["date"] if entry else None,
             f">= {case['as_of']}", "案例截止日当日或之后必须有有效证券收盘价。")
        gate("benchmark_alignment", not missing_benchmark_dates,
             missing_benchmark_dates[:5], "证券评价日均有基准收盘价", "证券和基准必须按相同交易日对齐。")

        gate("horizon_matured", completed, observed_days, f">= {horizon}",
             "周期未满属于正常 pending，不计算或展示部分收益。")

        hard_gates = {"adjustment", "row_contract", "benchmark_alignment"}
        rejected = any(not item["passed"] and item["name"] in hard_gates for item in gates)
        status = "data_rejected" if rejected else "completed" if completed else "pending"
        metrics: dict = {
            "observed_trading_days": min(observed_days, horizon),
            "required_trading_days": horizon,
            "remaining_trading_days": max(0, horizon - observed_days),
            "progress": round(min(observed_days / horizon, 1.0), 6),
            "return_convention": "close_to_close_total_return",
            "mae_convention": "minimum_forward_adjusted_close_return_from_entry",
        }
        entry_date = entry["date"] if entry else None
        entry_price = entry["close"] if entry else None
        target_date = evaluation_rows[horizon]["date"] if completed else None
        exit_date = target_date if status == "completed" else None
        exit_price = evaluation_rows[horizon]["close"] if status == "completed" else None
        security_return = benchmark_return = excess_return = mae = None

        if status == "completed":
            benchmark_entry = benchmark_by_date[entry_date]["close"]
            benchmark_exit = benchmark_by_date[exit_date]["close"]
            security_return = round(exit_price / entry_price - 1, 10)
            benchmark_return = round(benchmark_exit / benchmark_entry - 1, 10)
            excess_return = round(security_return - benchmark_return, 10)
            mae = round(min(0.0, min(item["close"] / entry_price - 1 for item in evaluation_rows)), 10)
            metrics.update({
                "security_return": security_return,
                "benchmark_return": benchmark_return,
                "excess_return": excess_return,
                "maximum_adverse_excursion": mae,
            })

        data_payload = {
            "security_code": case["security_code"],
            "benchmark_code": case["benchmark_code"],
            "adjustment": adjustment,
            "ifind_params": ifind_params,
            "security": security,
            "benchmark": benchmark,
        }
        data_fingerprint = canonical_hash(data_payload)
        snapshot = {
            "schema_version": EVALUATION_VERSION,
            "case": {
                "case_id": case_id,
                "snapshot_hash": case["snapshot_hash"],
                "policy_version": case["policy_version"],
                "rule_status": case["rule_status"],
                "review_status": case["review_status"],
                "scores": case["scores"],
            },
            "evaluation_contract": {
                "adjustment": adjustment,
                "ifind_params": ifind_params,
                "entry": "first_valid_close_on_or_after_case_as_of",
                "exit": "Nth_subsequent_security_observation",
                "mae": metrics["mae_convention"],
            },
            "data": data_payload,
            "data_fingerprint": data_fingerprint,
            "status": status,
            "metrics": metrics,
            "gates": gates,
        }
        snapshot_hash = canonical_hash(snapshot)
        outcome_id = sha256_text(
            f"decision-outcome:{case_id}:{EVALUATION_VERSION}:{data_fingerprint}"
        )
        return self.research_store.save_decision_outcome({
            "outcome_id": outcome_id,
            "case_id": case_id,
            "evaluation_version": EVALUATION_VERSION,
            "status": status,
            "entry_date": entry_date,
            "entry_price": entry_price,
            "horizon_trading_days": horizon,
            "target_date": target_date,
            "exit_date": exit_date,
            "exit_price": exit_price,
            "security_return": security_return,
            "benchmark_return": benchmark_return,
            "excess_return": excess_return,
            "maximum_adverse_excursion": mae,
            "security_data_source": data_source,
            "benchmark_data_source": data_source,
            "adjustment": adjustment,
            "ifind_params": ifind_params,
            "data_fingerprint": data_fingerprint,
            "metrics": metrics,
            "gates": gates,
            "snapshot": snapshot,
            "snapshot_hash": snapshot_hash,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })

    def attribution(self) -> dict:
        outcomes = self.research_store.latest_decision_outcomes()
        completed = [item for item in outcomes if item["status"] == "completed"]

        def summarize(items: list[dict]) -> dict:
            excess = [float(item["excess_return"]) for item in items]
            security = [float(item["security_return"]) for item in items]
            mae = [float(item["maximum_adverse_excursion"]) for item in items]
            count = len(items)
            return {
                "count": count,
                "reliable": count >= MIN_RELIABLE_GROUP_SIZE,
                "minimum_reliable_sample": MIN_RELIABLE_GROUP_SIZE,
                "mean_security_return": statistics.fmean(security) if security else None,
                "median_security_return": statistics.median(security) if security else None,
                "mean_excess_return": statistics.fmean(excess) if excess else None,
                "median_excess_return": statistics.median(excess) if excess else None,
                "positive_excess_ratio": sum(value > 0 for value in excess) / count if count else None,
                "mean_maximum_adverse_excursion": statistics.fmean(mae) if mae else None,
            }

        dimensions = {
            "rule_status": lambda item: item["rule_status"],
            "review_status": lambda item: item["review_status"],
            "decision_horizon_days": lambda item: str(item["decision_horizon_days"]),
            "policy_version": lambda item: item["policy_version"],
            "benchmark_code": lambda item: item["benchmark_code"],
            "attractiveness_bucket": lambda item: _bucket(float(item["scores"]["attractiveness"]["score"]), kind="score"),
            "evidence_confidence_bucket": lambda item: _bucket(float(item["scores"]["evidence_confidence"]["score"]), kind="score"),
            "risk_severity_bucket": lambda item: _bucket(float(item["scores"]["risk_severity"]["score"]), kind="risk"),
        }
        groups: dict[str, list[dict]] = {}
        for dimension, key_fn in dimensions.items():
            buckets: dict[str, list[dict]] = {}
            for item in completed:
                buckets.setdefault(str(key_fn(item)), []).append(item)
            groups[dimension] = [
                {"key": key, **summarize(values)} for key, values in sorted(buckets.items())
            ]
        counts = {status: sum(item["status"] == status for item in outcomes)
                  for status in ("completed", "pending", "data_rejected")}
        return {
            "evaluation_version": EVALUATION_VERSION,
            "counts": {"cases": len(outcomes), **counts},
            "overall": summarize(completed),
            "groups": groups,
            "boundary": "仅汇总每个案例最新的 completed 结果；低于最小样本数的分组不可解释为有效性证据。",
        }
