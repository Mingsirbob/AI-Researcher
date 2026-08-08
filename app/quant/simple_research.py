from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.core.migrations import apply_migration
from app.research.store import utc_now


@dataclass(frozen=True)
class FormulaTemplate:
    template_id: str
    name: str
    category: str
    description: str
    expression_template: str
    default_window: int
    min_window: int
    max_window: int
    direction: str

    def render(self, window: int) -> str:
        return self.expression_template.format(window=window)


FORMULA_TEMPLATES = {
    item.template_id: item
    for item in (
        FormulaTemplate("momentum", "区间动量", "momentum", "观察过去一段时间的累计涨跌幅。", "close / Ref(close, {window}) - 1", 20, 2, 250, "positive"),
        FormulaTemplate("reversal", "短期反转", "reversal", "把短期涨跌幅取反，观察均值回归。", "-(close / Ref(close, {window}) - 1)", 5, 2, 60, "positive"),
        FormulaTemplate("volatility", "历史波动率", "risk", "衡量日收益的年化波动，通常越低越稳。", "Std(Return(close), {window}) * Sqrt(252)", 60, 10, 250, "negative"),
        FormulaTemplate("volume_ratio", "成交量比率", "liquidity", "最新成交量相对过去均量的倍数。", "volume / Mean(volume, {window})", 20, 5, 120, "positive"),
        FormulaTemplate("turnover_mean", "平均成交额", "liquidity", "过去一段时间的平均成交金额。", "Mean(volume * vwap, {window})", 20, 5, 120, "positive"),
        FormulaTemplate("range_position", "价格区间位置", "momentum", "当前价格在历史最高与最低之间的位置。", "(close - Min(close, {window})) / (Max(close, {window}) - Min(close, {window}))", 250, 20, 500, "positive"),
        FormulaTemplate("max_drawdown", "最大回撤", "risk", "窗口内从高点到低点的最大跌幅。", "MaxDrawdown(close, {window})", 250, 20, 500, "positive"),
        FormulaTemplate("ma_gap", "均线偏离", "momentum", "当前价格相对移动均线的偏离。", "close / Mean(close, {window}) - 1", 20, 5, 250, "positive"),
    )
}


class SimpleResearchCatalog:
    """Canonical factor and backtest catalog backed by the compact four-table DB."""

    def __init__(self, store) -> None:
        self.store = store
        apply_migration(store.connect, "0032_compact_quant_research", self._create_schema)

    def _create_schema(self) -> None:
        with self.store.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS quant_factor (
                    factor_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    expression TEXT NOT NULL,
                    parameters_json TEXT NOT NULL DEFAULT '{}',
                    description TEXT NOT NULL DEFAULT '',
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'active',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quant_backtest (
                    backtest_id TEXT PRIMARY KEY,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    universe TEXT NOT NULL DEFAULT '',
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    series_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_quant_backtest_target
                ON quant_backtest(target_type, target_id, created_at DESC);
                """
            )

    def overview(self) -> dict[str, Any]:
        with self.store.connect() as conn:
            latest = conn.execute(
                "SELECT * FROM quant_backtest ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            latest_item = _decode_backtest(latest)
            if latest_item:
                latest_item.pop("series", None)
            return {
                "counts": {
                    "factor_sets": 1,
                    "factors": _count(conn, "quant_factor"),
                    "models": 0,
                    "experiments": _count(conn, "quant_backtest"),
                },
                "latest_experiment": latest_item,
            }

    def factor_sets(self) -> list[dict[str, Any]]:
        with self.store.connect() as conn:
            count = _count(conn, "quant_factor")
        return [{
            "factor_set_id": "default", "name": "因子库",
            "description": "当前有效因子", "source": "local",
            "factor_count": count, "status": "active", "config": {},
        }]

    @staticmethod
    def templates() -> list[dict[str, Any]]:
        return [
            {
                "template_id": item.template_id,
                "name": item.name,
                "category": item.category,
                "description": item.description,
                "expression_template": item.expression_template,
                "default_window": item.default_window,
                "min_window": item.min_window,
                "max_window": item.max_window,
                "direction": item.direction,
            }
            for item in FORMULA_TEMPLATES.values()
        ]

    def create_factor(
        self, *, factor_id: str, name: str, description: str,
        template_id: str, window: int, direction: str | None, owner: str,
    ) -> dict[str, Any]:
        factor_id = factor_id.strip().lower()
        if not re.fullmatch(r"[a-z0-9_]+", factor_id):
            raise ValueError("因子 ID 只能包含小写字母、数字和下划线")
        template = FORMULA_TEMPLATES.get(template_id)
        if template is None:
            raise ValueError("未知公式模板")
        if not template.min_window <= window <= template.max_window:
            raise ValueError(f"窗口必须在 {template.min_window} 至 {template.max_window} 之间")
        resolved_direction = direction or template.direction
        return self.save_factor({
            "factor_id": factor_id,
            "name": name.strip(),
            "description": description.strip(),
            "expression": template.render(window),
            "parameters": {
                "template_id": template_id,
                "window": window,
                "direction": resolved_direction,
                "owner": owner.strip(),
            },
        })

    def factors(self, *, factor_set_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        if factor_set_id and factor_set_id != "default":
            return []
        sql, params = "SELECT * FROM quant_factor", []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY name, factor_id"
        with self.store.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {"factor_set_id": "default", **_decode_json_columns(row, "parameters_json", "metrics_json")}
            for row in rows
        ]

    def models(self, *, status: str | None = None) -> list[dict[str, Any]]:
        return []

    def save_factor(self, item: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self.store.connect() as conn:
            conn.execute(
                """
                INSERT INTO quant_factor VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
                ON CONFLICT(factor_id) DO UPDATE SET
                    name=excluded.name, expression=excluded.expression,
                    parameters_json=excluded.parameters_json,
                    description=excluded.description, status='active',
                    updated_at=excluded.updated_at
                """,
                (
                    item["factor_id"], item["name"], item["expression"],
                    _json(item.get("parameters") or {}), item.get("description") or "",
                    _json(item.get("metrics") or {}), now,
                ),
            )
        return next(row for row in self.factors() if row["factor_id"] == item["factor_id"])

    def save_backtest(self, result: dict[str, Any]) -> None:
        run = result["run"]
        series = [
            {"series_name": name, "trading_date": row["trading_date"], "value": row.get(name)}
            for row in result.get("nav", [])
            for name in ("nav", "benchmark_nav", "drawdown")
        ]
        with self.store.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO quant_backtest VALUES
                (?, 'factor', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run["backtest_id"], run["factor_id"], run["universe"],
                    run["requested_start_date"], run["requested_end_date"],
                    _json({
                        "top_n": run["top_n"],
                        "rebalance_step": run["rebalance_step"],
                    }),
                    _json(run.get("metrics") or {}), _json(series), run["status"],
                    run.get("finished_at") or run["started_at"],
                ),
            )

    def experiments(self, *, experiment_type: str | None = None, target_type: str | None = None,
                    target_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        where, params = [], []
        if experiment_type and experiment_type != "backtest":
            return []
        for column, value in (("target_type", target_type), ("target_id", target_id)):
            if value:
                where.append(f"{column}=?")
                params.append(value)
        sql = "SELECT * FROM quant_backtest"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.store.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_decode_backtest(row) for row in rows]

    def experiment(self, experiment_id: str) -> dict[str, Any] | None:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM quant_backtest WHERE backtest_id=?", (experiment_id,)
            ).fetchone()
        if row is None:
            return None
        item = _decode_backtest(row)
        return {"item": item, "series": item.pop("series")}

    def latest_backtest(self) -> dict[str, Any] | None:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM quant_backtest ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        item = _decode_backtest(row)
        grouped: dict[str, dict[str, Any]] = {}
        for point in item.pop("series"):
            day = grouped.setdefault(point["trading_date"], {"trading_date": point["trading_date"]})
            day[point["series_name"]] = point["value"]
        return {
            "run": {
                "backtest_id": item["backtest_id"],
                "target_type": item["target_type"],
                "target_id": item["target_id"],
                "metrics": item["metrics"],
                "status": item["status"],
                "limitations": [],
            },
            "nav": list(grouped.values()),
            "rebalances": [],
        }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _count(conn, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _decode_json_columns(row, *columns: str) -> dict[str, Any]:
    item = dict(row)
    for column in columns:
        item[column.removesuffix("_json")] = json.loads(item.pop(column) or "{}")
    return item


def _decode_backtest(row) -> dict[str, Any] | None:
    if row is None:
        return None
    item = _decode_json_columns(row, "config_json", "metrics_json", "series_json")
    item["experiment_id"] = item["backtest_id"]
    item["experiment_type"] = "backtest"
    return item
