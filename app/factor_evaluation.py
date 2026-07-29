from __future__ import annotations

import json
import math
import sqlite3
import statistics
import threading
from collections import defaultdict
from datetime import date
from typing import Any

import pandas as pd

from .data_access import StockRepository
from .factor_lab import FactorLabService, evaluate_template
from .migrations import apply_migration
from .primitives import canonical_hash
from .quant import source_fingerprint
from .research_store import ResearchStore, utc_now


FACTOR_EVALUATION_VERSION = "factor-evaluation-v1"
DEFAULT_HORIZONS = (1, 5, 20)
MIN_CROSS_SECTION = 30


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _spearman(left: pd.Series, right: pd.Series) -> float | None:
    pair = pd.concat([left, right], axis=1).dropna()
    if len(pair) < 3 or pair.iloc[:, 0].nunique() < 2 or pair.iloc[:, 1].nunique() < 2:
        return None
    value = pair.iloc[:, 0].rank(method="average").corr(
        pair.iloc[:, 1].rank(method="average"), method="pearson"
    )
    return float(value) if value is not None and math.isfinite(value) else None


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _cumulative_return(values: list[float]) -> float | None:
    if not values:
        return None
    wealth = 1.0
    for value in values:
        wealth *= 1 + value
    return wealth - 1


def _decode_run(row: sqlite3.Row | dict | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    for key in ("horizons_json", "limitations_json"):
        item[key.removesuffix("_json")] = json.loads(item.pop(key))
    return item


class FactorEvaluationService:
    def __init__(
        self,
        store: ResearchStore,
        repository: StockRepository,
        factor_lab: FactorLabService,
    ):
        self.store = store
        self.repository = repository
        self.factor_lab = factor_lab
        self._lock = threading.Lock()
        apply_migration(store.connect, "0009_factor_evaluation", self._create_schema)
        apply_migration(
            store.connect,
            "0010_factor_qfq_liquidity_contract",
            self._deprecate_incompatible_qfq_turnover,
        )

    def _deprecate_incompatible_qfq_turnover(self) -> None:
        now = utc_now()
        with self.store.connect() as conn:
            rows = conn.execute(
                """
                SELECT factor_id, version, lifecycle_status FROM factor_version
                WHERE template_id='turnover_mean' AND adjustment='CPS:2'
                """
            ).fetchall()
            for row in rows:
                if row["lifecycle_status"] != "deprecated":
                    conn.execute(
                        "UPDATE factor_version SET lifecycle_status='deprecated', "
                        "status_changed_at=? WHERE factor_id=? AND version=?",
                        (now, row["factor_id"], row["version"]),
                    )
                    conn.execute(
                        """
                        INSERT INTO factor_release_review (
                            review_id, factor_id, factor_version, from_status, to_status,
                            reviewer, note, created_at
                        ) VALUES (?, ?, ?, ?, 'deprecated', 'system', ?, ?)
                        """,
                        (
                            canonical_hash(
                                {
                                    "migration": "0010_factor_qfq_liquidity_contract",
                                    "factor_id": row["factor_id"],
                                    "version": row["version"],
                                }
                            ),
                            row["factor_id"], row["version"], row["lifecycle_status"],
                            "前复权VWAP乘真实成交量不等于历史真实成交额", now,
                        ),
                    )
            conn.execute(
                """
                UPDATE factor_evaluation_run SET status='superseded'
                WHERE status='completed' AND evaluation_id IN (
                    SELECT DISTINCT evaluation_id FROM factor_evaluation_metric
                    WHERE factor_id='turnover_mean_20d' AND factor_version=2
                )
                """
            )

    def _create_schema(self) -> None:
        with self.store.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS factor_evaluation_run (
                    evaluation_id TEXT PRIMARY KEY,
                    input_hash TEXT NOT NULL UNIQUE,
                    evaluation_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_start_date TEXT NOT NULL,
                    requested_end_date TEXT NOT NULL,
                    effective_start_date TEXT,
                    effective_end_date TEXT,
                    universe TEXT NOT NULL,
                    adjustment TEXT NOT NULL,
                    source_db TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL,
                    source_contract_hash TEXT NOT NULL,
                    factor_contract_hash TEXT NOT NULL,
                    rebalance_step INTEGER NOT NULL,
                    horizons_json TEXT NOT NULL,
                    layer_count INTEGER NOT NULL,
                    winsor_lower REAL NOT NULL,
                    winsor_upper REAL NOT NULL,
                    factor_count INTEGER NOT NULL DEFAULT 0,
                    rebalance_count INTEGER NOT NULL DEFAULT 0,
                    period_count INTEGER NOT NULL DEFAULT 0,
                    correlation_count INTEGER NOT NULL DEFAULT 0,
                    result_hash TEXT,
                    limitations_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS factor_evaluation_period (
                    evaluation_id TEXT NOT NULL,
                    factor_id TEXT NOT NULL,
                    factor_version INTEGER NOT NULL,
                    as_of TEXT NOT NULL,
                    horizon INTEGER NOT NULL,
                    target_date TEXT NOT NULL,
                    security_count INTEGER NOT NULL,
                    raw_rank_ic REAL,
                    directional_rank_ic REAL,
                    layer_returns_json TEXT NOT NULL,
                    top_members_json TEXT NOT NULL,
                    PRIMARY KEY (evaluation_id, factor_id, factor_version, as_of, horizon),
                    FOREIGN KEY (evaluation_id) REFERENCES factor_evaluation_run(evaluation_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (factor_id, factor_version)
                        REFERENCES factor_version(factor_id, version)
                );

                CREATE TABLE IF NOT EXISTS factor_evaluation_metric (
                    evaluation_id TEXT NOT NULL,
                    factor_id TEXT NOT NULL,
                    factor_version INTEGER NOT NULL,
                    factor_name TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    horizon INTEGER NOT NULL,
                    observation_count INTEGER NOT NULL,
                    average_security_count REAL NOT NULL,
                    mean_rank_ic REAL,
                    rank_ic_std REAL,
                    rank_icir REAL,
                    positive_ic_ratio REAL,
                    top_layer_turnover REAL,
                    mean_top_return REAL,
                    mean_bottom_return REAL,
                    mean_layer_spread REAL,
                    cumulative_layer_spread REAL,
                    layer_monotonicity REAL,
                    layer_returns_json TEXT NOT NULL,
                    layer_cumulative_returns_json TEXT NOT NULL,
                    PRIMARY KEY (evaluation_id, factor_id, factor_version, horizon),
                    FOREIGN KEY (evaluation_id) REFERENCES factor_evaluation_run(evaluation_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (factor_id, factor_version)
                        REFERENCES factor_version(factor_id, version)
                );

                CREATE TABLE IF NOT EXISTS factor_evaluation_correlation (
                    evaluation_id TEXT NOT NULL,
                    left_factor_id TEXT NOT NULL,
                    left_factor_version INTEGER NOT NULL,
                    right_factor_id TEXT NOT NULL,
                    right_factor_version INTEGER NOT NULL,
                    mean_rank_correlation REAL NOT NULL,
                    observation_count INTEGER NOT NULL,
                    PRIMARY KEY (
                        evaluation_id, left_factor_id, left_factor_version,
                        right_factor_id, right_factor_version
                    ),
                    FOREIGN KEY (evaluation_id) REFERENCES factor_evaluation_run(evaluation_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_factor_evaluation_run_finished
                ON factor_evaluation_run(status, finished_at DESC);

                CREATE INDEX IF NOT EXISTS idx_factor_evaluation_metric_factor
                ON factor_evaluation_metric(factor_id, factor_version, horizon);
                """
            )

    def _source_contract(self) -> tuple[str, str]:
        fingerprint, size, mtime_ns = source_fingerprint(
            self.repository.db_path, self.repository.security_count
        )
        metadata: dict[str, Any] = {}
        with self.repository.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='price_database_metadata'"
            ).fetchone()
            if exists:
                row = conn.execute("SELECT * FROM price_database_metadata").fetchone()
                metadata = dict(row) if row else {}
        contract_hash = canonical_hash(
            {
                "file_fingerprint": fingerprint,
                "file_size": size,
                "file_mtime_ns": mtime_ns,
                "metadata": metadata,
            }
        )
        return fingerprint, contract_hash

    def _load_histories(self) -> tuple[dict[str, dict], list[str]]:
        histories: dict[str, dict] = {}
        calendar: set[str] = set()
        for code, rows in self.repository.iter_histories(end="9999-12-31", limit=3000):
            if not rows:
                continue
            by_date = {row["time"]: row for row in rows}
            histories[code] = {
                "rows": rows,
                "by_date": by_date,
                "position_by_date": {row["time"]: index for index, row in enumerate(rows)},
            }
            calendar.update(by_date)
        return histories, sorted(calendar)

    @staticmethod
    def _evaluation_dates(
        calendar: list[str], start_date: str, end_date: str, max_horizon: int,
        rebalance_step: int,
    ) -> list[tuple[int, str]]:
        eligible = [
            (index, value)
            for index, value in enumerate(calendar)
            if start_date <= value <= end_date and index + max_horizon < len(calendar)
        ]
        return eligible[::rebalance_step]

    @staticmethod
    def _assign_layers(frame: pd.DataFrame, layer_count: int) -> pd.DataFrame:
        ranked = frame.copy()
        lower = ranked["raw_value"].quantile(0.01)
        upper = ranked["raw_value"].quantile(0.99)
        ranked["winsorized_value"] = ranked["raw_value"].clip(lower, upper)
        ranked["directional_value"] = ranked["winsorized_value"] * ranked["direction_sign"]
        order = ranked["directional_value"].rank(method="first", ascending=True)
        ranked["layer"] = (((order - 1) * layer_count / len(ranked)).astype(int) + 1)
        return ranked

    def run(
        self,
        *,
        start_date: str,
        end_date: str,
        rebalance_step: int = 20,
        horizons: tuple[int, ...] = DEFAULT_HORIZONS,
        layer_count: int = 5,
    ) -> dict:
        with self._lock:
            return self._run(
                start_date=start_date,
                end_date=end_date,
                rebalance_step=rebalance_step,
                horizons=horizons,
                layer_count=layer_count,
            )

    def _run(
        self,
        *,
        start_date: str,
        end_date: str,
        rebalance_step: int,
        horizons: tuple[int, ...],
        layer_count: int,
    ) -> dict:
        date.fromisoformat(start_date)
        date.fromisoformat(end_date)
        if start_date > end_date:
            raise ValueError("评价开始日期不能晚于结束日期")
        if not 1 <= rebalance_step <= 120:
            raise ValueError("调仓采样间隔必须在 1 至 120 个交易日之间")
        normalized_horizons = tuple(sorted(set(int(value) for value in horizons)))
        if not normalized_horizons or normalized_horizons[0] < 1 or normalized_horizons[-1] > 250:
            raise ValueError("未来收益周期必须在 1 至 250 个交易日之间")
        if not 3 <= layer_count <= 10:
            raise ValueError("分层数量必须在 3 至 10 之间")

        factors = self.factor_lab.evaluation_factors()
        factors = [
            item for item in factors
            if item["adjustment"] == self.factor_lab.adjustment
            and item["universe"] == self.factor_lab.universe
        ]
        if not factors:
            raise ValueError("当前前复权合同没有可评价因子")
        factor_contract = [
            {
                "factor_id": item["factor_id"],
                "version": item["version"],
                "formula_hash": item["formula_hash"],
            }
            for item in factors
        ]
        factor_contract_hash = canonical_hash(factor_contract)
        source_fingerprint_value, source_contract_hash = self._source_contract()
        input_payload = {
            "evaluation_version": FACTOR_EVALUATION_VERSION,
            "start_date": start_date,
            "end_date": end_date,
            "universe": self.factor_lab.universe,
            "adjustment": self.factor_lab.adjustment,
            "source_contract_hash": source_contract_hash,
            "factor_contract_hash": factor_contract_hash,
            "rebalance_step": rebalance_step,
            "horizons": normalized_horizons,
            "layer_count": layer_count,
            "winsor": [0.01, 0.99],
        }
        input_hash = canonical_hash(input_payload)
        evaluation_id = canonical_hash({"input": input_hash})
        with self.store.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM factor_evaluation_run WHERE input_hash=?",
                (input_hash,),
            ).fetchone()
        if existing and existing["status"] == "completed":
            return {**self.evaluation(evaluation_id), "reused": True}

        limitations = [
            "股票池是2026-07-26查询时点的当前沪深300，历史评价存在幸存者偏差。",
            "分层收益未计交易成本、冲击成本、停牌成交约束和涨跌停限制。",
            "评价结果只用于因子筛选，不自动进入模型、候选池、决策或交易。",
        ]
        started_at = utc_now()
        with self.store.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO factor_evaluation_run (
                    evaluation_id, input_hash, evaluation_version, status,
                    requested_start_date, requested_end_date, universe, adjustment,
                    source_db, source_fingerprint, source_contract_hash,
                    factor_contract_hash, rebalance_step, horizons_json, layer_count,
                    winsor_lower, winsor_upper, limitations_json, started_at
                ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.01, 0.99, ?, ?)
                """,
                (
                    evaluation_id, input_hash, FACTOR_EVALUATION_VERSION,
                    start_date, end_date, self.factor_lab.universe,
                    self.factor_lab.adjustment, str(self.repository.db_path.resolve()),
                    source_fingerprint_value, source_contract_hash, factor_contract_hash,
                    rebalance_step, _json(normalized_horizons), layer_count,
                    _json(limitations), started_at,
                ),
            )

        try:
            result = self._compute(
                evaluation_id=evaluation_id,
                factors=factors,
                start_date=start_date,
                end_date=end_date,
                rebalance_step=rebalance_step,
                horizons=normalized_horizons,
                layer_count=layer_count,
            )
            with self.store.connect() as conn:
                conn.execute("DELETE FROM factor_evaluation_period WHERE evaluation_id=?", (evaluation_id,))
                conn.execute("DELETE FROM factor_evaluation_metric WHERE evaluation_id=?", (evaluation_id,))
                conn.execute("DELETE FROM factor_evaluation_correlation WHERE evaluation_id=?", (evaluation_id,))
                conn.executemany(
                    """
                    INSERT INTO factor_evaluation_period VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    result["period_rows"],
                )
                conn.executemany(
                    """
                    INSERT INTO factor_evaluation_metric VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    result["metric_rows"],
                )
                conn.executemany(
                    """
                    INSERT INTO factor_evaluation_correlation VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    result["correlation_rows"],
                )
                finished_at = utc_now()
                conn.execute(
                    """
                    UPDATE factor_evaluation_run SET status='completed',
                        effective_start_date=?, effective_end_date=?, factor_count=?,
                        rebalance_count=?, period_count=?, correlation_count=?,
                        result_hash=?, finished_at=?, error=NULL WHERE evaluation_id=?
                    """,
                    (
                        result["effective_start_date"], result["effective_end_date"],
                        len(factors), result["rebalance_count"], len(result["period_rows"]),
                        len(result["correlation_rows"]), result["result_hash"], finished_at,
                        evaluation_id,
                    ),
                )
        except Exception as exc:
            with self.store.connect() as conn:
                conn.execute(
                    "UPDATE factor_evaluation_run SET status='failed', error=?, finished_at=? "
                    "WHERE evaluation_id=?",
                    (str(exc), utc_now(), evaluation_id),
                )
            raise
        return {**self.evaluation(evaluation_id), "reused": False}

    def _compute(
        self,
        *,
        evaluation_id: str,
        factors: list[dict],
        start_date: str,
        end_date: str,
        rebalance_step: int,
        horizons: tuple[int, ...],
        layer_count: int,
    ) -> dict:
        histories, calendar = self._load_histories()
        if not calendar:
            raise ValueError("前复权数据库没有行情")
        if end_date > calendar[-1]:
            end_date = calendar[-1]
        evaluation_dates = self._evaluation_dates(
            calendar, start_date, end_date, max(horizons), rebalance_step
        )
        if len(evaluation_dates) < 2:
            raise ValueError("评价区间不足，至少需要两个有效调仓截面")

        period_items: list[dict] = []
        correlation_values: dict[tuple[str, int, str, int], list[float]] = defaultdict(list)
        top_members_by_factor: dict[tuple[str, int], list[set[str]]] = defaultdict(list)

        for calendar_index, as_of in evaluation_dates:
            factor_frames: dict[tuple[str, int], pd.DataFrame] = {}
            for factor in factors:
                records: list[dict] = []
                lookback = int(factor["lookback"])
                for code, history in histories.items():
                    row = history["by_date"].get(as_of)
                    if row is None:
                        continue
                    rows = history["rows"]
                    position = history["position_by_date"].get(as_of)
                    if position is None:
                        continue
                    tail = rows[max(0, position - lookback - 1): position + 1]
                    raw_value = evaluate_template(factor["template_id"], lookback, tail)
                    if raw_value is None:
                        continue
                    record = {
                        "security_code": code,
                        "raw_value": raw_value,
                        "direction_sign": 1.0 if factor["direction"] == "positive" else -1.0,
                    }
                    current_close = float(row["close"])
                    for horizon in horizons:
                        target_date = calendar[calendar_index + horizon]
                        target = history["by_date"].get(target_date)
                        record[f"return_{horizon}"] = (
                            float(target["close"]) / current_close - 1
                            if target is not None and current_close > 0 else None
                        )
                    records.append(record)
                if len(records) < MIN_CROSS_SECTION:
                    continue
                key = (factor["factor_id"], int(factor["version"]))
                factor_frames[key] = self._assign_layers(pd.DataFrame(records), layer_count)
                top_members_by_factor[key].append(
                    set(factor_frames[key].loc[
                        factor_frames[key]["layer"] == layer_count, "security_code"
                    ])
                )

            keys = sorted(factor_frames)
            for left_index, left_key in enumerate(keys):
                left = factor_frames[left_key].set_index("security_code")["directional_value"]
                for right_key in keys[left_index:]:
                    right = factor_frames[right_key].set_index("security_code")["directional_value"]
                    correlation = _spearman(left, right)
                    if correlation is not None:
                        correlation_values[(*left_key, *right_key)].append(correlation)

            for factor in factors:
                key = (factor["factor_id"], int(factor["version"]))
                frame = factor_frames.get(key)
                if frame is None:
                    continue
                for horizon in horizons:
                    return_column = f"return_{horizon}"
                    valid = frame.dropna(subset=[return_column]).copy()
                    if len(valid) < MIN_CROSS_SECTION:
                        continue
                    raw_ic = _spearman(valid["winsorized_value"], valid[return_column])
                    directional_ic = _spearman(valid["directional_value"], valid[return_column])
                    layer_returns = {
                        str(layer): float(valid.loc[valid["layer"] == layer, return_column].mean())
                        for layer in range(1, layer_count + 1)
                        if not valid.loc[valid["layer"] == layer, return_column].empty
                    }
                    period_items.append(
                        {
                            "factor_id": factor["factor_id"],
                            "factor_version": int(factor["version"]),
                            "factor_name": factor["name"],
                            "direction": factor["direction"],
                            "as_of": as_of,
                            "horizon": horizon,
                            "target_date": calendar[calendar_index + horizon],
                            "security_count": len(valid),
                            "raw_rank_ic": raw_ic,
                            "directional_rank_ic": directional_ic,
                            "layer_returns": layer_returns,
                            "top_members": sorted(
                                valid.loc[valid["layer"] == layer_count, "security_code"]
                            ),
                        }
                    )

        if not period_items:
            raise ValueError("没有形成满足最小横截面数量的评价观察")

        turnover: dict[tuple[str, int], float | None] = {}
        for key, memberships in top_members_by_factor.items():
            values = []
            for previous, current in zip(memberships, memberships[1:]):
                if current:
                    values.append(1 - len(previous.intersection(current)) / len(current))
            turnover[key] = _mean(values)

        metric_rows: list[tuple] = []
        for factor in factors:
            key = (factor["factor_id"], int(factor["version"]))
            for horizon in horizons:
                items = [
                    item for item in period_items
                    if (item["factor_id"], item["factor_version"]) == key
                    and item["horizon"] == horizon
                ]
                if not items:
                    continue
                ic_values = [
                    item["directional_rank_ic"] for item in items
                    if item["directional_rank_ic"] is not None
                ]
                mean_ic = _mean(ic_values)
                ic_std = statistics.stdev(ic_values) if len(ic_values) > 1 else None
                icir = (
                    mean_ic / ic_std * math.sqrt(252 / rebalance_step)
                    if mean_ic is not None and ic_std not in {None, 0} else None
                )
                layer_series = {
                    layer: [
                        item["layer_returns"][str(layer)] for item in items
                        if str(layer) in item["layer_returns"]
                    ]
                    for layer in range(1, layer_count + 1)
                }
                mean_layers = {str(layer): _mean(values) for layer, values in layer_series.items()}
                cumulative_layers = {
                    str(layer): _cumulative_return(values) for layer, values in layer_series.items()
                }
                top_values = layer_series[layer_count]
                bottom_values = layer_series[1]
                spreads = [top - bottom for top, bottom in zip(top_values, bottom_values)]
                relative_spreads = [
                    (1 + top) / (1 + bottom) - 1
                    for top, bottom in zip(top_values, bottom_values)
                    if 1 + bottom > 0
                ]
                monotonicity = _spearman(
                    pd.Series(range(1, layer_count + 1), dtype=float),
                    pd.Series([mean_layers[str(layer)] for layer in range(1, layer_count + 1)]),
                )
                metric_rows.append(
                    (
                        evaluation_id, factor["factor_id"], int(factor["version"]),
                        factor["name"], factor["direction"], horizon, len(ic_values),
                        statistics.fmean(item["security_count"] for item in items),
                        mean_ic, ic_std, icir,
                        sum(value > 0 for value in ic_values) / len(ic_values) if ic_values else None,
                        turnover.get(key), _mean(top_values), _mean(bottom_values),
                        _mean(spreads), _cumulative_return(relative_spreads), monotonicity,
                        _json(mean_layers), _json(cumulative_layers),
                    )
                )

        correlation_rows = [
            (
                evaluation_id, left_id, left_version, right_id, right_version,
                statistics.fmean(values), len(values),
            )
            for (left_id, left_version, right_id, right_version), values
            in sorted(correlation_values.items())
        ]
        period_rows = [
            (
                evaluation_id, item["factor_id"], item["factor_version"], item["as_of"],
                item["horizon"], item["target_date"], item["security_count"],
                item["raw_rank_ic"], item["directional_rank_ic"],
                _json(item["layer_returns"]), _json(item["top_members"]),
            )
            for item in period_items
        ]
        result_hash = canonical_hash(
            {
                "periods": period_rows,
                "metrics": metric_rows,
                "correlations": correlation_rows,
            }
        )
        return {
            "effective_start_date": evaluation_dates[0][1],
            "effective_end_date": evaluation_dates[-1][1],
            "rebalance_count": len(evaluation_dates),
            "period_rows": period_rows,
            "metric_rows": metric_rows,
            "correlation_rows": correlation_rows,
            "result_hash": result_hash,
        }

    def evaluation(self, evaluation_id: str) -> dict | None:
        with self.store.connect() as conn:
            run = conn.execute(
                "SELECT * FROM factor_evaluation_run WHERE evaluation_id=?",
                (evaluation_id,),
            ).fetchone()
            if run is None:
                return None
            metrics = [dict(row) for row in conn.execute(
                "SELECT * FROM factor_evaluation_metric WHERE evaluation_id=? "
                "ORDER BY factor_id, horizon",
                (evaluation_id,),
            )]
            correlations = [dict(row) for row in conn.execute(
                "SELECT * FROM factor_evaluation_correlation WHERE evaluation_id=? "
                "ORDER BY left_factor_id, right_factor_id",
                (evaluation_id,),
            )]
        for metric in metrics:
            metric["layer_returns"] = json.loads(metric.pop("layer_returns_json"))
            metric["layer_cumulative_returns"] = json.loads(
                metric.pop("layer_cumulative_returns_json")
            )
        return {
            "run": _decode_run(run),
            "metrics": metrics,
            "correlations": correlations,
        }

    def latest(self) -> dict | None:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT evaluation_id FROM factor_evaluation_run "
                "WHERE status='completed' ORDER BY finished_at DESC LIMIT 1"
            ).fetchone()
        return self.evaluation(row[0]) if row else None
