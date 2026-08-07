from __future__ import annotations

import json
from typing import Any

from app.core.migrations import apply_migration
from app.research.store import utc_now


SIMPLE_RESEARCH_TABLES = (
    "research_factor_set",
    "research_factor",
    "research_model",
    "research_experiment",
    "research_experiment_series",
)


class SimpleResearchCatalog:
    """Small canonical catalog layered over legacy quant tables during cutover."""

    def __init__(self, store) -> None:
        self.store = store
        apply_migration(store.connect, "0027_simple_quant_research", self._create_schema)

    def _create_schema(self) -> None:
        with self.store.connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS research_factor_set (
                factor_set_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                source TEXT NOT NULL,
                factor_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                config_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS research_factor (
                factor_set_id TEXT NOT NULL,
                factor_id TEXT NOT NULL,
                name TEXT NOT NULL,
                expression TEXT NOT NULL,
                parameters_json TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (factor_set_id, factor_id),
                FOREIGN KEY (factor_set_id) REFERENCES research_factor_set(factor_set_id)
            );
            CREATE TABLE IF NOT EXISTS research_model (
                model_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                model_type TEXT NOT NULL,
                feature_set_id TEXT NOT NULL,
                artifact_path TEXT NOT NULL,
                parameters_json TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS research_experiment (
                experiment_id TEXT PRIMARY KEY,
                experiment_type TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                universe TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                config_json TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS research_experiment_series (
                experiment_id TEXT NOT NULL,
                series_name TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                value REAL,
                PRIMARY KEY (experiment_id, series_name, trading_date),
                FOREIGN KEY (experiment_id) REFERENCES research_experiment(experiment_id)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_research_experiment_target
            ON research_experiment(target_type, target_id, created_at DESC);
            """)

    def sync_from_legacy(self) -> dict[str, int]:
        now = utc_now()
        with self.store.connect() as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self._seed_sets(conn, now)
            if {"factor_definition", "factor_version"} <= tables:
                self._sync_factors(conn, now)
            if "model_run" in tables:
                self._sync_models(conn, now)
            if "factor_evaluation_run" in tables:
                self._sync_evaluations(conn)
            if "factor_backtest_run" in tables:
                self._sync_backtests(conn)
            self._refresh_counts(conn, now)
            return {
                "factor_sets": _count(conn, "research_factor_set"),
                "factors": _count(conn, "research_factor"),
                "models": _count(conn, "research_model"),
                "experiments": _count(conn, "research_experiment"),
                "series": _count(conn, "research_experiment_series"),
            }

    def overview(self) -> dict[str, Any]:
        with self.store.connect() as conn:
            latest = conn.execute(
                "SELECT * FROM research_experiment ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            return {
                "counts": {
                    "factor_sets": _count(conn, "research_factor_set"),
                    "factors": _count(conn, "research_factor"),
                    "models": _count(conn, "research_model"),
                    "experiments": _count(conn, "research_experiment"),
                },
                "latest_experiment": _decode_experiment(latest),
            }

    def factor_sets(self) -> list[dict[str, Any]]:
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM research_factor_set ORDER BY name, factor_set_id"
            ).fetchall()
        return [_decode_json_columns(row, "config_json") for row in rows]

    def factors(
        self,
        *,
        factor_set_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        parameters: list[str] = []
        if factor_set_id:
            where.append("factor_set_id=?")
            parameters.append(factor_set_id)
        if status:
            where.append("status=?")
            parameters.append(status)
        sql = "SELECT * FROM research_factor"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY factor_set_id, name, factor_id"
        with self.store.connect() as conn:
            rows = conn.execute(sql, parameters).fetchall()
        return [
            _decode_json_columns(row, "parameters_json", "metrics_json")
            for row in rows
        ]

    def models(self, *, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM research_model"
        parameters: list[str] = []
        if status:
            sql += " WHERE status=?"
            parameters.append(status)
        sql += " ORDER BY updated_at DESC, model_id"
        with self.store.connect() as conn:
            rows = conn.execute(sql, parameters).fetchall()
        return [
            _decode_json_columns(row, "parameters_json", "metrics_json")
            for row in rows
        ]

    def experiments(
        self,
        *,
        experiment_type: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        parameters: list[Any] = []
        for column, value in (
            ("experiment_type", experiment_type),
            ("target_type", target_type),
            ("target_id", target_id),
        ):
            if value:
                where.append(f"{column}=?")
                parameters.append(value)
        sql = "SELECT * FROM research_experiment"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, experiment_id LIMIT ?"
        parameters.append(limit)
        with self.store.connect() as conn:
            rows = conn.execute(sql, parameters).fetchall()
        return [_decode_experiment(row) for row in rows]

    def experiment(self, experiment_id: str) -> dict[str, Any] | None:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM research_experiment WHERE experiment_id=?",
                (experiment_id,),
            ).fetchone()
            if row is None:
                return None
            series_rows = conn.execute(
                "SELECT series_name, trading_date, value "
                "FROM research_experiment_series WHERE experiment_id=? "
                "ORDER BY trading_date, series_name",
                (experiment_id,),
            ).fetchall()
        return {
            "item": _decode_experiment(row),
            "series": [dict(item) for item in series_rows],
        }

    @staticmethod
    def _seed_sets(conn, now: str) -> None:
        rows = (
            ("alpha158", "Alpha158", "Qlib Alpha158 价量特征集", "qlib", 0, "active", "{}", now),
            ("custom", "自定义因子", "项目内定义和计算的可解释因子", "local", 0, "active", "{}", now),
        )
        conn.executemany(
            "INSERT INTO research_factor_set VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(factor_set_id) DO UPDATE SET name=excluded.name, "
            "description=excluded.description, source=excluded.source, updated_at=excluded.updated_at",
            rows,
        )

    @staticmethod
    def _sync_factors(conn, now: str) -> None:
        rows = conn.execute("""
            SELECT d.factor_id, d.name, d.description, v.template_id, v.expression,
                   v.parameters_json, v.lifecycle_status
            FROM factor_definition d
            JOIN factor_version v ON v.factor_id=d.factor_id
            JOIN (SELECT factor_id, MAX(version) AS version FROM factor_version GROUP BY factor_id) x
              ON x.factor_id=v.factor_id AND x.version=v.version
        """).fetchall()
        for row in rows:
            factor_set_id = "alpha158" if row["template_id"] == "alpha158_bundle" else "custom"
            status = _simple_status(row["lifecycle_status"])
            metrics = conn.execute(
                "SELECT * FROM factor_evaluation_metric WHERE factor_id=? "
                "ORDER BY CASE WHEN horizon=20 THEN 0 ELSE 1 END LIMIT 1",
                (row["factor_id"],),
            ).fetchone() if "factor_evaluation_metric" in {
                item[0] for item in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            } else None
            conn.execute("""
                INSERT INTO research_factor VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(factor_set_id, factor_id) DO UPDATE SET
                    name=excluded.name, expression=excluded.expression,
                    parameters_json=excluded.parameters_json, description=excluded.description,
                    status=excluded.status, metrics_json=excluded.metrics_json,
                    updated_at=excluded.updated_at
            """, (
                factor_set_id, row["factor_id"], row["name"], row["expression"],
                row["parameters_json"], row["description"], status,
                _json(dict(metrics) if metrics else {}), now,
            ))

    @staticmethod
    def _sync_models(conn, now: str) -> None:
        rows = conn.execute("SELECT * FROM model_run").fetchall()
        for row in rows:
            artifact = conn.execute(
                "SELECT file_path FROM model_artifact WHERE model_run_id=? ORDER BY artifact_type LIMIT 1",
                (row["model_run_id"],),
            ).fetchone()
            conn.execute("""
                INSERT INTO research_model VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(model_id) DO UPDATE SET artifact_path=excluded.artifact_path,
                    parameters_json=excluded.parameters_json, metrics_json=excluded.metrics_json,
                    status=excluded.status, updated_at=excluded.updated_at
            """, (
                row["model_run_id"], row["experiment_id"], row["model_class"],
                row["feature_set"], artifact["file_path"] if artifact else row["source_path"],
                row["config_json"], row["metrics_json"],
                "active" if row["status"] in {"completed", "registered"} else row["status"], now,
            ))

    @staticmethod
    def _sync_evaluations(conn) -> None:
        for row in conn.execute("SELECT * FROM factor_evaluation_run WHERE status='completed'"):
            metrics = [dict(item) for item in conn.execute(
                "SELECT * FROM factor_evaluation_metric WHERE evaluation_id=?",
                (row["evaluation_id"],),
            )]
            conn.execute("""
                INSERT OR REPLACE INTO research_experiment VALUES
                (?, 'factor_evaluation', 'factor_set', 'evaluation_contract', ?, ?, ?, ?, ?, ?, ?)
            """, (
                row["evaluation_id"], row["universe"], row["requested_start_date"],
                row["requested_end_date"], _json({
                    "rebalance_step": row["rebalance_step"],
                    "horizons": json.loads(row["horizons_json"]),
                    "layer_count": row["layer_count"],
                }), _json(metrics), row["status"], row["finished_at"] or row["started_at"],
            ))

    @staticmethod
    def _sync_backtests(conn) -> None:
        for row in conn.execute("SELECT * FROM factor_backtest_run WHERE status='completed'"):
            conn.execute("""
                INSERT OR REPLACE INTO research_experiment VALUES
                (?, 'backtest', 'factor', ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                row["backtest_id"], f"{row['factor_id']}@{row['factor_version']}",
                row["universe"], row["requested_start_date"], row["requested_end_date"],
                _json({
                    "top_n": row["top_n"], "rebalance_step": row["rebalance_step"],
                    "initial_capital": row["initial_capital"],
                }), row["metrics_json"], row["status"], row["finished_at"] or row["started_at"],
            ))
            conn.execute("DELETE FROM research_experiment_series WHERE experiment_id=?", (row["backtest_id"],))
            nav = conn.execute(
                "SELECT trading_date, nav, benchmark_nav, drawdown FROM factor_backtest_nav "
                "WHERE backtest_id=? ORDER BY trading_date",
                (row["backtest_id"],),
            ).fetchall()
            conn.executemany(
                "INSERT INTO research_experiment_series VALUES (?, ?, ?, ?)",
                [
                    (row["backtest_id"], name, item["trading_date"], item[name])
                    for item in nav for name in ("nav", "benchmark_nav", "drawdown")
                ],
            )

    @staticmethod
    def _refresh_counts(conn, now: str) -> None:
        conn.execute("""
            UPDATE research_factor_set SET factor_count=(
                SELECT COUNT(*) FROM research_factor f
                WHERE f.factor_set_id=research_factor_set.factor_set_id
            ), updated_at=?
        """, (now,))


def _simple_status(value: str) -> str:
    if value in {"approved", "shadow"}:
        return "active"
    if value == "deprecated":
        return "disabled"
    return "draft"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _count(conn, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _decode_json_columns(row, *columns: str) -> dict[str, Any]:
    item = dict(row)
    for column in columns:
        item[column.removesuffix("_json")] = json.loads(item.pop(column) or "{}")
    return item


def _decode_experiment(row) -> dict[str, Any] | None:
    if row is None:
        return None
    return _decode_json_columns(row, "config_json", "metrics_json")
