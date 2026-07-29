from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.migrations import apply_migration
from app.core.primitives import canonical_hash
from app.research.store import utc_now
from app.quant.store import QuantStore


FACTOR_RELEASE_GATE_VERSION = "factor-release-gate-v1"
FACTOR_RELEASE_TABLES = (
    "factor_release_candidate",
    "factor_release_gate",
    "factor_release_decision",
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode_candidate(row: sqlite3.Row | dict | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    item["limitations"] = json.loads(item.pop("limitations_json"))
    item["limitations_acknowledged"] = bool(item["limitations_acknowledged"])
    return item


class FactorReleaseService:
    """Auditable bridge from historical evidence to forward-only Shadow observation."""

    def __init__(self, store: QuantStore):
        self.store = store
        apply_migration(store.connect, "0012_factor_release", self._create_schema)
        if hasattr(store, "migrate_legacy"):
            store.migrate_legacy(
                "0021_split_factor_release_database", FACTOR_RELEASE_TABLES
            )

    def _create_schema(self) -> None:
        with self.store.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS factor_release_candidate (
                    release_id TEXT PRIMARY KEY,
                    factor_id TEXT NOT NULL,
                    factor_version INTEGER NOT NULL,
                    evaluation_id TEXT NOT NULL,
                    evaluation_result_hash TEXT NOT NULL,
                    backtest_id TEXT NOT NULL,
                    backtest_result_hash TEXT NOT NULL,
                    gate_version TEXT NOT NULL,
                    target_status TEXT NOT NULL,
                    status TEXT NOT NULL,
                    blocking_failure_count INTEGER NOT NULL,
                    limitations_json TEXT NOT NULL,
                    limitations_acknowledged INTEGER NOT NULL,
                    result_hash TEXT NOT NULL UNIQUE,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    decided_at TEXT,
                    FOREIGN KEY (factor_id, factor_version)
                        REFERENCES factor_version(factor_id, version),
                    FOREIGN KEY (evaluation_id)
                        REFERENCES factor_evaluation_run(evaluation_id),
                    FOREIGN KEY (backtest_id)
                        REFERENCES factor_backtest_run(backtest_id)
                );

                CREATE TABLE IF NOT EXISTS factor_release_gate (
                    release_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    gate_key TEXT NOT NULL,
                    label TEXT NOT NULL,
                    observed_json TEXT NOT NULL,
                    comparator TEXT NOT NULL,
                    threshold_json TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    blocking INTEGER NOT NULL,
                    detail TEXT NOT NULL,
                    PRIMARY KEY (release_id, position),
                    UNIQUE (release_id, gate_key),
                    FOREIGN KEY (release_id) REFERENCES factor_release_candidate(release_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS factor_release_decision (
                    decision_id TEXT PRIMARY KEY,
                    release_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (release_id) REFERENCES factor_release_candidate(release_id)
                );

                CREATE INDEX IF NOT EXISTS idx_factor_release_candidate_created
                ON factor_release_candidate(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_factor_release_decision_release
                ON factor_release_decision(release_id, created_at);
                """
            )

    @staticmethod
    def _gate(
        key: str,
        label: str,
        observed: Any,
        comparator: str,
        threshold: Any,
        passed: bool,
        detail: str,
        *,
        blocking: bool = True,
    ) -> dict:
        return {
            "gate_key": key,
            "label": label,
            "observed": observed,
            "comparator": comparator,
            "threshold": threshold,
            "passed": bool(passed),
            "blocking": blocking,
            "detail": detail,
        }

    def create_candidate(
        self,
        *,
        factor_id: str,
        factor_version: int,
        evaluation_id: str,
        backtest_id: str,
        limitations_acknowledged: bool,
        created_by: str,
    ) -> dict:
        with self.store.connect() as conn:
            factor = conn.execute(
                "SELECT * FROM factor_version WHERE factor_id=? AND version=?",
                (factor_id, factor_version),
            ).fetchone()
            evaluation = conn.execute(
                "SELECT * FROM factor_evaluation_run WHERE evaluation_id=?",
                (evaluation_id,),
            ).fetchone()
            backtest = conn.execute(
                "SELECT * FROM factor_backtest_run WHERE backtest_id=?",
                (backtest_id,),
            ).fetchone()
            metrics = conn.execute(
                """
                SELECT * FROM factor_evaluation_metric
                WHERE evaluation_id=? AND factor_id=? AND factor_version=?
                ORDER BY CASE WHEN horizon=20 THEN 0 ELSE 1 END, horizon DESC LIMIT 1
                """,
                (evaluation_id, factor_id, factor_version),
            ).fetchone()
        if factor is None:
            raise KeyError("因子版本不存在")
        if evaluation is None or evaluation["status"] != "completed" or not evaluation["result_hash"]:
            raise ValueError("因子评价不存在、未完成或缺少结果哈希")
        if backtest is None or backtest["status"] != "completed" or not backtest["result_hash"]:
            raise ValueError("策略回测不存在、未完成或缺少结果哈希")
        if metrics is None:
            raise ValueError("该评价没有目标因子的指标")
        if (
            backtest["evaluation_id"] != evaluation_id
            or backtest["factor_id"] != factor_id
            or int(backtest["factor_version"]) != factor_version
        ):
            raise ValueError("回测、评价与因子版本没有形成同一证据链")
        if evaluation["source_contract_hash"] != backtest["source_contract_hash"]:
            raise ValueError("评价与回测的数据源合同不一致")

        backtest_metrics = json.loads(backtest["metrics_json"])
        limitations = list(dict.fromkeys(
            json.loads(evaluation["limitations_json"])
            + json.loads(backtest["limitations_json"])
        ))
        with self.store.connect() as conn:
            published = conn.execute(
                "SELECT factor_id, version FROM factor_version "
                "WHERE lifecycle_status IN ('shadow', 'approved') "
                "AND NOT (factor_id=? AND version=?)",
                (factor_id, factor_version),
            ).fetchall()
            correlations = conn.execute(
                """
                SELECT * FROM factor_evaluation_correlation WHERE evaluation_id=? AND (
                    (left_factor_id=? AND left_factor_version=?) OR
                    (right_factor_id=? AND right_factor_version=?))
                """,
                (evaluation_id, factor_id, factor_version, factor_id, factor_version),
            ).fetchall()
        published_keys = {(row["factor_id"], int(row["version"])) for row in published}
        relevant_correlations = []
        for row in correlations:
            other = (
                (row["right_factor_id"], int(row["right_factor_version"]))
                if (row["left_factor_id"], int(row["left_factor_version"]))
                == (factor_id, factor_version)
                else (row["left_factor_id"], int(row["left_factor_version"]))
            )
            if other in published_keys:
                relevant_correlations.append(abs(float(row["mean_rank_correlation"])))
        max_correlation = max(relevant_correlations) if relevant_correlations else None

        value = dict(metrics)
        gates = [
            self._gate("lifecycle", "生命周期", factor["lifecycle_status"], "=", "testing", factor["lifecycle_status"] == "testing", "只有测试中因子可申请 Shadow"),
            self._gate("observations", "评价观察数", value["observation_count"], ">=", 12, value["observation_count"] >= 12, "优先使用20日周期，至少12个横截面观察"),
            self._gate("rank_ic", "方向 Rank IC", value["mean_rank_ic"], ">=", 0.02, value["mean_rank_ic"] is not None and value["mean_rank_ic"] >= 0.02, "因子方向调整后的平均秩相关"),
            self._gate(
                "rank_icir", "Rank ICIR",
                {"icir": value["rank_icir"], "ic_std": value["rank_ic_std"]},
                ">=", 0.20,
                (
                    value["rank_icir"] is not None and value["rank_icir"] >= 0.20
                ) or (
                    value["rank_ic_std"] == 0
                    and value["mean_rank_ic"] is not None
                    and value["mean_rank_ic"] >= 0.02
                ),
                "IC均值相对波动的稳定性；正IC且零标准差视为稳定",
            ),
            self._gate("positive_ic", "正 IC 比例", value["positive_ic_ratio"], ">=", 0.55, value["positive_ic_ratio"] is not None and value["positive_ic_ratio"] >= 0.55, "观察期内方向 IC 为正的比例"),
            self._gate("layer_spread", "分层收益差", value["mean_layer_spread"], ">", 0.0, value["mean_layer_spread"] is not None and value["mean_layer_spread"] > 0, "最高层平均收益应高于最低层"),
            self._gate("monotonicity", "分层单调性", value["layer_monotonicity"], ">=", 0.60, value["layer_monotonicity"] is not None and value["layer_monotonicity"] >= 0.60, "收益应随因子分层大体有序"),
            self._gate("published_correlation", "已发布因子相关性", max_correlation, "<=", 0.85, max_correlation is None or max_correlation <= 0.85, "无已发布可比因子时记为通过"),
            self._gate("max_drawdown", "策略最大回撤", backtest_metrics.get("max_drawdown"), ">=", -0.35, backtest_metrics.get("max_drawdown") is not None and backtest_metrics["max_drawdown"] >= -0.35, "历史策略回撤不得超过35%"),
            self._gate("average_turnover", "平均单次换手", backtest_metrics.get("average_turnover"), "<=", 1.0, backtest_metrics.get("average_turnover") is not None and backtest_metrics["average_turnover"] <= 1.0, "单次调仓平均成交不超过净值的100%"),
            self._gate("cost_rate", "累计成本率", backtest_metrics.get("total_cost_rate"), "<=", 0.15, backtest_metrics.get("total_cost_rate") is not None and backtest_metrics["total_cost_rate"] <= 0.15, "累计显式成本和滑点不超过初始资金15%"),
            self._gate("excess_return", "基准超额收益", backtest_metrics.get("excess_cumulative_return"), ">", 0.0, backtest_metrics.get("excess_cumulative_return") is not None and backtest_metrics["excess_cumulative_return"] > 0, "扣除成本后应跑赢回测基准"),
            self._gate("limitations_ack", "已知边界确认", limitations_acknowledged, "=", True, limitations_acknowledged, "人工确认幸存者偏差、基准与成交模型等限制"),
        ]
        blocking_failure_count = sum(not item["passed"] and item["blocking"] for item in gates)
        payload = {
            "gate_version": FACTOR_RELEASE_GATE_VERSION,
            "factor_id": factor_id,
            "factor_version": factor_version,
            "evaluation_id": evaluation_id,
            "evaluation_result_hash": evaluation["result_hash"],
            "backtest_id": backtest_id,
            "backtest_result_hash": backtest["result_hash"],
            "gates": gates,
            "limitations": limitations,
            "limitations_acknowledged": limitations_acknowledged,
        }
        result_hash = canonical_hash(payload)
        release_id = canonical_hash({"factor_release": result_hash})
        now = utc_now()
        status = "gate_passed" if blocking_failure_count == 0 else "gate_failed"
        with self.store.connect() as conn:
            existing = conn.execute(
                "SELECT release_id FROM factor_release_candidate WHERE result_hash=?",
                (result_hash,),
            ).fetchone()
            if existing:
                return {**self.release(existing["release_id"]), "reused": True}
            conn.execute(
                """
                INSERT INTO factor_release_candidate VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, 'shadow', ?, ?, ?, ?, ?, ?, ?, NULL
                )
                """,
                (
                    release_id, factor_id, factor_version, evaluation_id,
                    evaluation["result_hash"], backtest_id, backtest["result_hash"],
                    FACTOR_RELEASE_GATE_VERSION, status, blocking_failure_count,
                    _json(limitations), int(limitations_acknowledged), result_hash,
                    created_by, now,
                ),
            )
            conn.executemany(
                "INSERT INTO factor_release_gate VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        release_id, position, gate["gate_key"], gate["label"],
                        _json(gate["observed"]), gate["comparator"],
                        _json(gate["threshold"]), int(gate["passed"]),
                        int(gate["blocking"]), gate["detail"],
                    )
                    for position, gate in enumerate(gates, 1)
                ],
            )
        return {**self.release(release_id), "reused": False}

    def release(self, release_id: str) -> dict | None:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM factor_release_candidate WHERE release_id=?", (release_id,)
            ).fetchone()
            if row is None:
                return None
            gates = [dict(item) for item in conn.execute(
                "SELECT * FROM factor_release_gate WHERE release_id=? ORDER BY position",
                (release_id,),
            )]
            decisions = [dict(item) for item in conn.execute(
                "SELECT * FROM factor_release_decision WHERE release_id=? ORDER BY created_at",
                (release_id,),
            )]
        for gate in gates:
            gate["observed"] = json.loads(gate.pop("observed_json"))
            gate["threshold"] = json.loads(gate.pop("threshold_json"))
            gate["passed"] = bool(gate["passed"])
            gate["blocking"] = bool(gate["blocking"])
        return {"candidate": _decode_candidate(row), "gates": gates, "decisions": decisions}

    def latest(self, limit: int = 20) -> dict:
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT release_id FROM factor_release_candidate ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return {"items": [self.release(row["release_id"]) for row in rows]}

    def decide(self, release_id: str, decision: str, reviewer: str, note: str) -> dict:
        if decision not in {"approve", "reject"}:
            raise ValueError("未知发布决定")
        now = utc_now()
        with self.store.connect() as conn:
            candidate = conn.execute(
                "SELECT * FROM factor_release_candidate WHERE release_id=?", (release_id,)
            ).fetchone()
            if candidate is None:
                raise KeyError("发布候选不存在")
            if candidate["status"] not in {"gate_passed", "gate_failed"}:
                raise ValueError("该发布候选已经完成决定")
            if decision == "approve" and candidate["status"] != "gate_passed":
                raise ValueError("存在阻断门禁，不能批准进入 Shadow")
            factor = conn.execute(
                "SELECT lifecycle_status FROM factor_version WHERE factor_id=? AND version=?",
                (candidate["factor_id"], candidate["factor_version"]),
            ).fetchone()
            if factor is None:
                raise KeyError("因子版本不存在")
            if decision == "approve" and factor["lifecycle_status"] != "testing":
                raise ValueError("因子已不在 testing 状态，请重新建立发布候选")
            decision_id = canonical_hash(
                {"release_id": release_id, "decision": decision, "reviewer": reviewer, "note": note, "created_at": now}
            )
            conn.execute(
                "INSERT INTO factor_release_decision VALUES (?, ?, ?, ?, ?, ?)",
                (decision_id, release_id, decision, reviewer, note, now),
            )
            final_status = "approved" if decision == "approve" else "rejected"
            conn.execute(
                "UPDATE factor_release_candidate SET status=?, decided_at=? WHERE release_id=?",
                (final_status, now, release_id),
            )
            if decision == "approve":
                conn.execute(
                    "UPDATE factor_version SET lifecycle_status='shadow', status_changed_at=? "
                    "WHERE factor_id=? AND version=?",
                    (now, candidate["factor_id"], candidate["factor_version"]),
                )
                conn.execute(
                    """
                    INSERT INTO factor_release_review VALUES (?, ?, ?, 'testing', 'shadow', ?, ?, ?)
                    """,
                    (
                        decision_id, candidate["factor_id"], candidate["factor_version"],
                        reviewer, f"M11.4发布批准：{note}", now,
                    ),
                )
        return self.release(release_id)
