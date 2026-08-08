from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from app.core.migrations import apply_migration
from app.market.repository import normalize_code
from app.research.store import ResearchStore, utc_now
from app.core.sqlite_store import migrate_legacy_tables


QUANT_CORE_TABLES = (
    "factor_snapshot",
    "security_factor_snapshot",
    "research_candidate",
    "model_run",
    "model_artifact",
    "prediction_snapshot",
    "shadow_signal",
    "model_validation_run",
    "current_shadow_snapshot",
    "current_shadow_signal",
)


class QuantStore:
    """Quant-only state store with a synchronized security metadata projection."""

    def __init__(self, db_path: Path, master_store: ResearchStore):
        self.db_path = Path(db_path)
        self.master_store = master_store
        self.document_root = master_store.document_root
        self.fts_available = False
        self._initialize_quant()


    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _initialize_quant(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        apply_migration(self.connect, "0001_quant_core", self._create_quant_schema)
        self.sync_security_projection()
        migrate_legacy_tables(
            target=self,
            source=self.master_store,
            migration_id="0017_split_quant_core_database",
            tables=QUANT_CORE_TABLES,
        )

    def _create_quant_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS security_master (
                    security_code TEXT PRIMARY KEY,
                    exchange TEXT NOT NULL,
                    security_name TEXT,
                    listing_date TEXT,
                    listing_date_source TEXT,
                    board TEXT,
                    industry_l1 TEXT,
                    industry_l2 TEXT,
                    status TEXT NOT NULL DEFAULT 'unknown',
                    first_price_date TEXT,
                    last_price_date TEXT,
                    source TEXT NOT NULL,
                    source_updated_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS factor_snapshot (
                    snapshot_id TEXT PRIMARY KEY,
                    as_of TEXT NOT NULL,
                    factor_version TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL,
                    source_db_size INTEGER NOT NULL,
                    source_db_mtime_ns INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    total_securities INTEGER NOT NULL DEFAULT 0,
                    passed_securities INTEGER NOT NULL DEFAULT 0,
                    excluded_securities INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    error TEXT,
                    UNIQUE(as_of, factor_version, source_fingerprint)
                );
                CREATE INDEX IF NOT EXISTS idx_factor_snapshot_completed
                ON factor_snapshot(status, as_of DESC, finished_at DESC);

                CREATE TABLE IF NOT EXISTS security_factor_snapshot (
                    snapshot_id TEXT NOT NULL,
                    security_code TEXT NOT NULL,
                    latest_trade_date TEXT,
                    observations INTEGER NOT NULL,
                    quality_status TEXT NOT NULL,
                    quality_reasons_json TEXT NOT NULL,
                    close REAL,
                    return_20d REAL,
                    return_60d REAL,
                    volatility_60d REAL,
                    avg_traded_value_20d REAL,
                    volume_ratio_20d REAL,
                    max_drawdown_250d REAL,
                    range_position_52w REAL,
                    PRIMARY KEY (snapshot_id, security_code),
                    FOREIGN KEY (snapshot_id) REFERENCES factor_snapshot(snapshot_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (security_code) REFERENCES security_master(security_code)
                );
                CREATE INDEX IF NOT EXISTS idx_security_factor_quality_return60
                ON security_factor_snapshot(snapshot_id, quality_status, return_60d DESC);

                CREATE TABLE IF NOT EXISTS research_candidate (
                    security_code TEXT PRIMARY KEY,
                    source_snapshot_id TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    added_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (security_code) REFERENCES security_master(security_code),
                    FOREIGN KEY (source_snapshot_id) REFERENCES factor_snapshot(snapshot_id)
                );

                CREATE TABLE IF NOT EXISTS model_run (
                    model_run_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    framework TEXT NOT NULL,
                    model_class TEXT NOT NULL,
                    feature_set TEXT NOT NULL,
                    universe TEXT NOT NULL,
                    train_start TEXT NOT NULL,
                    train_end TEXT NOT NULL,
                    valid_start TEXT NOT NULL,
                    valid_end TEXT NOT NULL,
                    test_start TEXT NOT NULL,
                    test_end TEXT NOT NULL,
                    status TEXT NOT NULL,
                    intended_use TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    limitations_json TEXT NOT NULL,
                    imported_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_artifact (
                    model_run_id TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    PRIMARY KEY (model_run_id, artifact_type),
                    FOREIGN KEY (model_run_id) REFERENCES model_run(model_run_id)
                );
                CREATE TABLE IF NOT EXISTS prediction_snapshot (
                    snapshot_id TEXT PRIMARY KEY,
                    model_run_id TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    instrument_count INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    UNIQUE(model_run_id, source_fingerprint),
                    FOREIGN KEY (model_run_id) REFERENCES model_run(model_run_id)
                );
                CREATE TABLE IF NOT EXISTS shadow_signal (
                    snapshot_id TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    security_code TEXT NOT NULL,
                    source_instrument TEXT NOT NULL,
                    score REAL NOT NULL,
                    realized_label REAL,
                    cross_section_rank INTEGER NOT NULL,
                    cross_section_size INTEGER NOT NULL,
                    percentile REAL NOT NULL,
                    PRIMARY KEY (snapshot_id, trading_date, security_code),
                    FOREIGN KEY (snapshot_id) REFERENCES prediction_snapshot(snapshot_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_shadow_signal_date_rank
                ON shadow_signal(snapshot_id, trading_date DESC, cross_section_rank ASC);

                CREATE TABLE IF NOT EXISTS model_validation_run (
                    validation_id TEXT PRIMARY KEY,
                    model_run_id TEXT NOT NULL,
                    validation_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    gates_json TEXT NOT NULL,
                    windows_json TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(model_run_id, validation_version, source_fingerprint),
                    FOREIGN KEY (model_run_id) REFERENCES model_run(model_run_id)
                );
                CREATE TABLE IF NOT EXISTS current_shadow_snapshot (
                    snapshot_id TEXT PRIMARY KEY,
                    model_run_id TEXT NOT NULL,
                    validation_id TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    universe TEXT NOT NULL,
                    adjustment TEXT NOT NULL,
                    ifind_params TEXT NOT NULL,
                    data_source TEXT NOT NULL,
                    data_start TEXT NOT NULL,
                    data_end TEXT NOT NULL,
                    data_fingerprint TEXT NOT NULL,
                    provider_path TEXT NOT NULL,
                    model_sha256 TEXT NOT NULL,
                    feature_count INTEGER NOT NULL,
                    universe_size INTEGER NOT NULL,
                    signal_count INTEGER NOT NULL,
                    coverage REAL NOT NULL,
                    status TEXT NOT NULL,
                    gates_json TEXT NOT NULL,
                    provider_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(model_run_id, as_of, data_fingerprint, validation_id),
                    FOREIGN KEY (model_run_id) REFERENCES model_run(model_run_id),
                    FOREIGN KEY (validation_id) REFERENCES model_validation_run(validation_id)
                );
                CREATE TABLE IF NOT EXISTS current_shadow_signal (
                    snapshot_id TEXT NOT NULL,
                    security_code TEXT NOT NULL,
                    source_instrument TEXT NOT NULL,
                    score REAL NOT NULL,
                    cross_section_rank INTEGER NOT NULL,
                    cross_section_size INTEGER NOT NULL,
                    percentile REAL NOT NULL,
                    PRIMARY KEY (snapshot_id, security_code),
                    FOREIGN KEY (snapshot_id) REFERENCES current_shadow_snapshot(snapshot_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_current_shadow_asof
                ON current_shadow_snapshot(as_of DESC, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_current_shadow_rank
                ON current_shadow_signal(snapshot_id, cross_section_rank ASC);
                """
            )

    def sync_security_projection(self, codes: list[str] | None = None) -> None:
        where = ""
        params: list[str] = []
        if codes:
            normalized = sorted(set(codes))
            where = f" WHERE security_code IN ({','.join('?' for _ in normalized)})"
            params = normalized
        with self.master_store.connect() as source, self.connect() as target:
            rows = source.execute(f"SELECT * FROM security_master{where}", params).fetchall()
            if not rows:
                return
            columns = tuple(rows[0].keys())
            quoted = ", ".join(f'"{column}"' for column in columns)
            placeholders = ", ".join("?" for _ in columns)
            updates = ", ".join(
                f'"{column}"=excluded."{column}"'
                for column in columns
                if column != "security_code"
            )
            target.executemany(
                f"INSERT INTO security_master ({quoted}) VALUES ({placeholders}) "
                f"ON CONFLICT(security_code) DO UPDATE SET {updates}",
                [tuple(row[column] for column in columns) for row in rows],
            )

    def migrate_legacy(self, migration_id: str, tables: tuple[str, ...]) -> bool:
        return migrate_legacy_tables(
            target=self,
            source=self.master_store,
            migration_id=migration_id,
            tables=tables,
        )

    def upsert_security_name(self, code: str, name: str, source: str = "iFinD") -> None:
        self.master_store.upsert_security_name(code, name, source)
        self.sync_security_projection([code])

    def stats(self) -> dict:
        with self.connect() as conn:
            existing = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }

            def count(table: str, where: str = "") -> int:
                if table not in existing:
                    return 0
                return conn.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0]

            return {
                "factor_snapshots": count("factor_snapshot", "WHERE status='completed'"),
                "factor_lab_values": count("factor_lab_value"),
                "factor_evaluations": count("factor_evaluation_run"),
                "factor_backtests": count("factor_backtest_run"),
                "model_runs": count("model_run"),
                "shadow_signals": count("shadow_signal"),
                "current_shadow_signals": count("current_shadow_signal"),
                "research_candidates": count("research_candidate"),
            }

    def register_model_run(
        self,
        *,
        model_run: dict,
        artifacts: list[dict],
        snapshot: dict,
        signals: list[dict],
    ) -> dict:
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT source_fingerprint FROM model_run WHERE model_run_id=?",
                (model_run["model_run_id"],),
            ).fetchone()
            if existing:
                if existing["source_fingerprint"] != model_run["source_fingerprint"]:
                    raise ValueError("同一模型运行 ID 已注册为不同产物，拒绝覆盖")
                item = self.model_run(model_run["model_run_id"])
                return {**item, "reused": True}

            conn.execute(
                """
                INSERT INTO model_run (
                    model_run_id, experiment_id, framework, model_class, feature_set,
                    universe, train_start, train_end, valid_start, valid_end,
                    test_start, test_end, status, intended_use, source_path,
                    source_fingerprint, config_json, metrics_json, limitations_json,
                    imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    model_run["model_run_id"], model_run["experiment_id"],
                    model_run["framework"], model_run["model_class"],
                    model_run["feature_set"], model_run["universe"],
                    model_run["train_start"], model_run["train_end"],
                    model_run["valid_start"], model_run["valid_end"],
                    model_run["test_start"], model_run["test_end"],
                    model_run["status"], model_run["intended_use"],
                    model_run["source_path"], model_run["source_fingerprint"],
                    json.dumps(model_run["config"], ensure_ascii=False, sort_keys=True),
                    json.dumps(model_run["metrics"], ensure_ascii=False, sort_keys=True),
                    json.dumps(model_run["limitations"], ensure_ascii=False),
                    model_run["imported_at"],
                ),
            )
            conn.executemany(
                """
                INSERT INTO model_artifact (
                    model_run_id, artifact_type, file_path, sha256, byte_size
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        model_run["model_run_id"], item["artifact_type"], item["file_path"],
                        item["sha256"], item["byte_size"],
                    )
                    for item in artifacts
                ],
            )
            conn.execute(
                """
                INSERT INTO prediction_snapshot (
                    snapshot_id, model_run_id, start_date, end_date, row_count,
                    instrument_count, status, source_fingerprint, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot["snapshot_id"], model_run["model_run_id"],
                    snapshot["start_date"], snapshot["end_date"], snapshot["row_count"],
                    snapshot["instrument_count"], snapshot["status"],
                    snapshot["source_fingerprint"], snapshot["imported_at"],
                ),
            )
            conn.executemany(
                """
                INSERT INTO shadow_signal (
                    snapshot_id, trading_date, security_code, source_instrument,
                    score, realized_label, cross_section_rank, cross_section_size, percentile
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot["snapshot_id"], item["trading_date"], item["security_code"],
                        item["source_instrument"], item["score"], item["realized_label"],
                        item["cross_section_rank"], item["cross_section_size"], item["percentile"],
                    )
                    for item in signals
                ],
            )
        return {**self.model_run(model_run["model_run_id"]), "reused": False}

    def model_run(self, model_run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM model_run WHERE model_run_id=?", (model_run_id,)
            ).fetchone()
            if row is None:
                return None
            artifacts = conn.execute(
                "SELECT artifact_type, file_path, sha256, byte_size FROM model_artifact "
                "WHERE model_run_id=? ORDER BY artifact_type",
                (model_run_id,),
            ).fetchall()
            snapshot = conn.execute(
                "SELECT * FROM prediction_snapshot WHERE model_run_id=?",
                (model_run_id,),
            ).fetchone()
        item = dict(row)
        item["config"] = json.loads(item.pop("config_json"))
        item["metrics"] = json.loads(item.pop("metrics_json"))
        item["limitations"] = json.loads(item.pop("limitations_json"))
        item["artifacts"] = [dict(artifact) for artifact in artifacts]
        item["prediction_snapshot"] = dict(snapshot) if snapshot else None
        return item

    def latest_model_run(self) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT model_run_id FROM model_run ORDER BY imported_at DESC LIMIT 1"
            ).fetchone()
        return self.model_run(row["model_run_id"]) if row else None

    def list_shadow_signals(
        self,
        model_run_id: str,
        *,
        as_of: str | None = None,
        query: str = "",
        sort: str = "rank",
        direction: str = "asc",
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        model = self.model_run(model_run_id)
        if model is None or model["prediction_snapshot"] is None:
            raise ValueError("模型运行或预测快照不存在")
        snapshot = model["prediction_snapshot"]
        selected_date = as_of or snapshot["end_date"]
        sort_columns = {
            "rank": "s.cross_section_rank",
            "score": "s.score",
            "label": "s.realized_label",
            "security_code": "s.security_code",
        }
        sort_column = sort_columns.get(sort, sort_columns["rank"])
        sort_direction = "DESC" if direction == "desc" else "ASC"
        conditions = ["s.snapshot_id=?", "s.trading_date=?"]
        params: list[object] = [snapshot["snapshot_id"], selected_date]
        if query.strip():
            conditions.append("(s.security_code LIKE ? OR s.source_instrument LIKE ?)")
            needle = f"%{query.strip().upper()}%"
            params.extend([needle, needle])
        where = " AND ".join(conditions)
        with self.connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM shadow_signal s WHERE {where}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT s.*, COALESCE(m.security_name, s.security_code) AS security_name
                FROM shadow_signal s
                LEFT JOIN security_master m ON m.security_code=s.security_code
                WHERE {where}
                ORDER BY {sort_column} {sort_direction}, s.security_code ASC
                LIMIT ? OFFSET ?
                """,
                [*params, max(1, min(limit, 500)), max(0, offset)],
            ).fetchall()
        return {
            "model_run_id": model_run_id,
            "as_of": selected_date,
            "items": [dict(row) for row in rows],
            "total": total,
            "snapshot": snapshot,
        }

    @staticmethod
    def _decode_model_validation(row: sqlite3.Row | dict) -> dict:
        item = dict(row)
        item["metrics"] = json.loads(item.pop("metrics_json"))
        item["gates"] = json.loads(item.pop("gates_json"))
        item["windows"] = json.loads(item.pop("windows_json"))
        return item

    def register_model_validation(self, validation: dict) -> dict:
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM model_validation_run WHERE validation_id=?",
                (validation["validation_id"],),
            ).fetchone()
            if existing:
                item = self._decode_model_validation(existing)
                if item["source_fingerprint"] != validation["source_fingerprint"]:
                    raise ValueError("模型验证 ID 对应不同数据指纹，拒绝覆盖")
                return {**item, "reused": True}
            conn.execute(
                """
                INSERT INTO model_validation_run (
                    validation_id, model_run_id, validation_version, status,
                    metrics_json, gates_json, windows_json, source_fingerprint, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    validation["validation_id"], validation["model_run_id"],
                    validation["validation_version"], validation["status"],
                    json.dumps(validation["metrics"], ensure_ascii=False, sort_keys=True),
                    json.dumps(validation["gates"], ensure_ascii=False, sort_keys=True),
                    json.dumps(validation["windows"], ensure_ascii=False, sort_keys=True),
                    validation["source_fingerprint"], validation["created_at"],
                ),
            )
        return {**self.model_validation(validation["validation_id"]), "reused": False}

    def model_validation(self, validation_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM model_validation_run WHERE validation_id=?", (validation_id,)
            ).fetchone()
        return self._decode_model_validation(row) if row else None

    def latest_model_validation(self, model_run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM model_validation_run
                WHERE model_run_id=? ORDER BY created_at DESC LIMIT 1
                """,
                (model_run_id,),
            ).fetchone()
        return self._decode_model_validation(row) if row else None

    @staticmethod
    def _decode_current_shadow(row: sqlite3.Row | dict) -> dict:
        item = dict(row)
        item["gates"] = json.loads(item.pop("gates_json"))
        item["provider"] = json.loads(item.pop("provider_json"))
        return item

    def register_current_shadow(self, snapshot: dict, signals: list[dict]) -> dict:
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM current_shadow_snapshot WHERE snapshot_id=?",
                (snapshot["snapshot_id"],),
            ).fetchone()
            if existing:
                item = self._decode_current_shadow(existing)
                if item["data_fingerprint"] != snapshot["data_fingerprint"]:
                    raise ValueError("当前 Shadow 快照 ID 对应不同数据指纹，拒绝覆盖")
                return {**item, "reused": True}
            conn.execute(
                """
                INSERT INTO current_shadow_snapshot (
                    snapshot_id, model_run_id, validation_id, as_of, universe,
                    adjustment, ifind_params, data_source, data_start, data_end,
                    data_fingerprint, provider_path, model_sha256, feature_count,
                    universe_size, signal_count, coverage, status, gates_json,
                    provider_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot["snapshot_id"], snapshot["model_run_id"],
                    snapshot["validation_id"], snapshot["as_of"], snapshot["universe"],
                    snapshot["adjustment"], snapshot["ifind_params"],
                    snapshot["data_source"], snapshot["data_start"], snapshot["data_end"],
                    snapshot["data_fingerprint"], snapshot["provider_path"],
                    snapshot["model_sha256"], snapshot["feature_count"],
                    snapshot["universe_size"], snapshot["signal_count"], snapshot["coverage"],
                    snapshot["status"],
                    json.dumps(snapshot["gates"], ensure_ascii=False, sort_keys=True),
                    json.dumps(snapshot.get("provider", {}), ensure_ascii=False, sort_keys=True),
                    snapshot["created_at"],
                ),
            )
            conn.executemany(
                """
                INSERT INTO current_shadow_signal (
                    snapshot_id, security_code, source_instrument, score,
                    cross_section_rank, cross_section_size, percentile
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot["snapshot_id"], item["security_code"],
                        item["source_instrument"], item["score"],
                        item["cross_section_rank"], item["cross_section_size"],
                        item["percentile"],
                    )
                    for item in signals
                ],
            )
        return {**self.current_shadow_snapshot(snapshot["snapshot_id"]), "reused": False}

    def current_shadow_snapshot(self, snapshot_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM current_shadow_snapshot WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
        return self._decode_current_shadow(row) if row else None

    def latest_current_shadow(self) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM current_shadow_snapshot ORDER BY as_of DESC, created_at DESC LIMIT 1"
            ).fetchone()
        return self._decode_current_shadow(row) if row else None

    def clear_daily_runtime(self, as_of: str) -> None:
        """Delete batch-only factors and model scores after proposals are stored."""
        with self.connect() as conn:
            shadow_ids = [
                row[0] for row in conn.execute(
                    "SELECT snapshot_id FROM current_shadow_snapshot WHERE as_of=?", (as_of,)
                )
            ]
            for snapshot_id in shadow_ids:
                conn.execute(
                    "DELETE FROM current_shadow_signal WHERE snapshot_id=?", (snapshot_id,)
                )
            conn.execute("DELETE FROM current_shadow_snapshot WHERE as_of=?", (as_of,))

            factor_ids = [
                row[0] for row in conn.execute(
                    "SELECT snapshot_id FROM factor_snapshot WHERE as_of=?", (as_of,)
                )
            ]
            for snapshot_id in factor_ids:
                conn.execute(
                    "DELETE FROM research_candidate WHERE source_snapshot_id=?", (snapshot_id,)
                )
                conn.execute(
                    "DELETE FROM security_factor_snapshot WHERE snapshot_id=?", (snapshot_id,)
                )
            conn.execute("DELETE FROM factor_snapshot WHERE as_of=?", (as_of,))

    def current_shadow_for_date(self, as_of: str, model_run_id: str | None = None) -> dict | None:
        conditions = ["as_of=?"]
        params: list[object] = [as_of]
        if model_run_id:
            conditions.append("model_run_id=?")
            params.append(model_run_id)
        with self.connect() as conn:
            row = conn.execute(
                f"""SELECT * FROM current_shadow_snapshot
                    WHERE {' AND '.join(conditions)}
                    ORDER BY created_at DESC LIMIT 1""",
                params,
            ).fetchone()
        return self._decode_current_shadow(row) if row else None

    def list_current_shadow_signals(
        self,
        snapshot_id: str,
        *,
        query: str = "",
        sort: str = "rank",
        direction: str = "asc",
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        snapshot = self.current_shadow_snapshot(snapshot_id)
        if snapshot is None:
            raise ValueError("当前 Shadow 快照不存在")
        sort_columns = {
            "rank": "s.cross_section_rank",
            "score": "s.score",
            "security_code": "s.security_code",
        }
        sort_column = sort_columns.get(sort, sort_columns["rank"])
        sort_direction = "DESC" if direction == "desc" else "ASC"
        conditions = ["s.snapshot_id=?"]
        params: list[object] = [snapshot_id]
        if query.strip():
            conditions.append("(s.security_code LIKE ? OR s.source_instrument LIKE ? OR m.security_name LIKE ?)")
            needle = f"%{query.strip().upper()}%"
            params.extend([needle, needle, f"%{query.strip()}%"])
        where = " AND ".join(conditions)
        with self.connect() as conn:
            total = conn.execute(
                f"""
                SELECT COUNT(*) FROM current_shadow_signal s
                LEFT JOIN security_master m ON m.security_code=s.security_code
                WHERE {where}
                """,
                params,
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT s.*, COALESCE(m.security_name, s.security_code) AS security_name
                FROM current_shadow_signal s
                LEFT JOIN security_master m ON m.security_code=s.security_code
                WHERE {where}
                ORDER BY {sort_column} {sort_direction}, s.security_code ASC
                LIMIT ? OFFSET ?
                """,
                [*params, max(1, min(limit, 500)), max(0, offset)],
            ).fetchall()
        return {
            "snapshot": snapshot,
            "items": [dict(row) for row in rows],
            "total": total,
        }

    def current_shadow_signal(self, snapshot_id: str, code: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT s.*, COALESCE(m.security_name, s.security_code) AS security_name
                FROM current_shadow_signal s
                LEFT JOIN security_master m ON m.security_code=s.security_code
                WHERE s.snapshot_id=? AND s.security_code=?
                """,
                (snapshot_id, normalize_code(code)),
            ).fetchone()
        return dict(row) if row else None

    def factor_snapshot_by_input(
        self, *, as_of: str, factor_version: str, source_fingerprint: str
    ) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM factor_snapshot
                WHERE as_of=? AND factor_version=? AND source_fingerprint=?
                """,
                (as_of, factor_version, source_fingerprint),
            ).fetchone()
        return dict(row) if row else None

    def start_factor_snapshot(
        self,
        *,
        as_of: str,
        factor_version: str,
        source_fingerprint: str,
        source_db_size: int,
        source_db_mtime_ns: int,
    ) -> str:
        existing = self.factor_snapshot_by_input(
            as_of=as_of,
            factor_version=factor_version,
            source_fingerprint=source_fingerprint,
        )
        snapshot_id = existing["snapshot_id"] if existing else str(uuid.uuid4())
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO factor_snapshot (
                    snapshot_id, as_of, factor_version, source_fingerprint,
                    source_db_size, source_db_mtime_ns, status, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?)
                ON CONFLICT(as_of, factor_version, source_fingerprint) DO UPDATE SET
                    status='running', started_at=excluded.started_at, finished_at=NULL,
                    error=NULL, total_securities=0, passed_securities=0,
                    excluded_securities=0
                """,
                (
                    snapshot_id,
                    as_of,
                    factor_version,
                    source_fingerprint,
                    source_db_size,
                    source_db_mtime_ns,
                    now,
                ),
            )
        return snapshot_id

    def finish_factor_snapshot(self, snapshot_id: str, rows: list[dict]) -> None:
        passed = sum(item["quality_status"] == "passed" for item in rows)
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM security_factor_snapshot WHERE snapshot_id=?", (snapshot_id,)
            )
            conn.executemany(
                """
                INSERT INTO security_factor_snapshot (
                    snapshot_id, security_code, latest_trade_date, observations,
                    quality_status, quality_reasons_json, close, return_20d,
                    return_60d, volatility_60d, avg_traded_value_20d,
                    volume_ratio_20d, max_drawdown_250d, range_position_52w
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot_id,
                        item["security_code"],
                        item.get("latest_trade_date"),
                        item["observations"],
                        item["quality_status"],
                        json.dumps(item["quality_reasons"], ensure_ascii=False),
                        item.get("close"),
                        item.get("return_20d"),
                        item.get("return_60d"),
                        item.get("volatility_60d"),
                        item.get("avg_traded_value_20d"),
                        item.get("volume_ratio_20d"),
                        item.get("max_drawdown_250d"),
                        item.get("range_position_52w"),
                    )
                    for item in rows
                ],
            )
            conn.execute(
                """
                UPDATE factor_snapshot SET status='completed', total_securities=?,
                    passed_securities=?, excluded_securities=?, finished_at=?, error=NULL
                WHERE snapshot_id=?
                """,
                (len(rows), passed, len(rows) - passed, utc_now(), snapshot_id),
            )

    def fail_factor_snapshot(self, snapshot_id: str, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE factor_snapshot SET status='failed', finished_at=?, error=?
                WHERE snapshot_id=?
                """,
                (utc_now(), error[:1000], snapshot_id),
            )

    def factor_snapshot(self, snapshot_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM factor_snapshot WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
        return dict(row) if row else None

    def latest_factor_snapshot(self) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM factor_snapshot WHERE status='completed'
                ORDER BY as_of DESC, finished_at DESC LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None

    def latest_compatible_factor_snapshot(self, as_of: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM factor_snapshot
                WHERE status='completed' AND as_of <= ?
                ORDER BY as_of DESC, finished_at DESC LIMIT 1
                """,
                (as_of,),
            ).fetchone()
        return dict(row) if row else None

    def quant_context_for_security(
        self,
        code: str,
        *,
        as_of: str,
        snapshot_id: str | None = None,
    ) -> dict:
        normalized = normalize_code(code)
        explicit_snapshot = snapshot_id is not None
        snapshot = (
            self.factor_snapshot(snapshot_id)
            if explicit_snapshot
            else self.latest_compatible_factor_snapshot(as_of)
        )
        if explicit_snapshot and snapshot is None:
            raise ValueError("候选来源因子快照不存在")
        if snapshot and snapshot["status"] != "completed":
            raise ValueError("候选来源因子快照尚未完成")
        if snapshot and snapshot["as_of"] > as_of:
            raise ValueError("候选来源因子快照晚于研究截止日")

        if snapshot is None:
            return {
                "status": "not_covered",
                "security": {"code": normalized, "name": normalized},
                "as_of": as_of,
                "snapshot_id": None,
                "factor_version": None,
                "source": "none",
                "quality_status": None,
                "quality_reasons": [],
                "universe": {"total": 0, "passed": 0, "excluded": 0},
                "factors": {},
                "ranks": {},
                "limitations": ["截止日内没有已完成的全市场因子快照。"],
            }

        factor_columns = (
            "return_20d",
            "return_60d",
            "volatility_60d",
            "avg_traded_value_20d",
            "volume_ratio_20d",
            "max_drawdown_250d",
            "range_position_52w",
        )
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT f.*, COALESCE(s.security_name, f.security_code) AS security_name
                FROM security_factor_snapshot f
                JOIN security_master s ON s.security_code=f.security_code
                WHERE f.snapshot_id=? AND f.security_code=?
                """,
                (snapshot["snapshot_id"], normalized),
            ).fetchone()
            if row is None:
                name_row = conn.execute(
                    "SELECT COALESCE(security_name, security_code) FROM security_master WHERE security_code=?",
                    (normalized,),
                ).fetchone()
                return {
                    "status": "not_covered",
                    "security": {
                        "code": normalized,
                        "name": name_row[0] if name_row else normalized,
                    },
                    "as_of": snapshot["as_of"],
                    "snapshot_id": snapshot["snapshot_id"],
                    "factor_version": snapshot["factor_version"],
                    "source": "candidate_pool" if explicit_snapshot else "latest_compatible_snapshot",
                    "quality_status": None,
                    "quality_reasons": [],
                    "universe": {
                        "total": snapshot["total_securities"],
                        "passed": snapshot["passed_securities"],
                        "excluded": snapshot["excluded_securities"],
                    },
                    "factors": {},
                    "ranks": {},
                    "limitations": ["该证券不在所选全市场因子快照中。"],
                }

            item = dict(row)
            ranks: dict[str, dict] = {}
            if item["quality_status"] == "passed":
                for column in factor_columns:
                    value = item[column]
                    if value is None:
                        continue
                    rank, universe = conn.execute(
                        f"""
                        SELECT 1 + SUM(CASE WHEN {column} > ? THEN 1 ELSE 0 END),
                               COUNT({column})
                        FROM security_factor_snapshot
                        WHERE snapshot_id=? AND quality_status='passed'
                        """,
                        (value, snapshot["snapshot_id"]),
                    ).fetchone()
                    percentile = 100.0 if universe <= 1 else 100.0 * (universe - rank) / (universe - 1)
                    ranks[column] = {
                        "rank": rank,
                        "universe": universe,
                        "percentile": round(percentile, 2),
                    }

        quality_reasons = json.loads(item["quality_reasons_json"])
        excluded = item["quality_status"] != "passed"
        return {
            "status": "excluded" if excluded else "supported",
            "security": {"code": normalized, "name": item["security_name"]},
            "as_of": snapshot["as_of"],
            "snapshot_id": snapshot["snapshot_id"],
            "factor_version": snapshot["factor_version"],
            "source": "candidate_pool" if explicit_snapshot else "latest_compatible_snapshot",
            "quality_status": item["quality_status"],
            "quality_reasons": quality_reasons,
            "universe": {
                "total": snapshot["total_securities"],
                "passed": snapshot["passed_securities"],
                "excluded": snapshot["excluded_securities"],
            },
            "factors": {column: item[column] for column in factor_columns},
            "ranks": ranks,
            "limitations": (
                ["该证券未通过行情质量门禁，不参与横截面排名。"] if excluded else []
            ),
        }

    def list_factor_rows(
        self,
        snapshot_id: str,
        *,
        quality_status: str = "passed",
        query: str = "",
        exchange: str = "all",
        sort: str = "return_60d",
        direction: str = "desc",
        min_return_20d: float | None = None,
        min_return_60d: float | None = None,
        max_volatility_60d: float | None = None,
        min_avg_traded_value_20d: float | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        sort_columns = {
            "security_code": "f.security_code",
            "return_20d": "f.return_20d",
            "return_60d": "f.return_60d",
            "volatility_60d": "f.volatility_60d",
            "avg_traded_value_20d": "f.avg_traded_value_20d",
            "max_drawdown_250d": "f.max_drawdown_250d",
            "range_position_52w": "f.range_position_52w",
        }
        sort_column = sort_columns.get(sort, sort_columns["return_60d"])
        sort_direction = "ASC" if direction == "asc" else "DESC"
        conditions = ["f.snapshot_id=?"]
        params: list[object] = [snapshot_id]
        if quality_status != "all":
            conditions.append("f.quality_status=?")
            params.append(quality_status)
        if query.strip():
            conditions.append("(f.security_code LIKE ? OR COALESCE(s.security_name, '') LIKE ?)")
            needle = f"%{query.strip()}%"
            params.extend([needle, needle])
        if exchange != "all":
            conditions.append("s.exchange=?")
            params.append(exchange)
        for column, value in (
            ("f.return_20d >= ?", min_return_20d),
            ("f.return_60d >= ?", min_return_60d),
            ("f.volatility_60d <= ?", max_volatility_60d),
            ("f.avg_traded_value_20d >= ?", min_avg_traded_value_20d),
        ):
            if value is not None:
                conditions.append(column)
                params.append(value)
        where = " AND ".join(conditions)
        with self.connect() as conn:
            total = conn.execute(
                f"""
                SELECT COUNT(*) FROM security_factor_snapshot f
                JOIN security_master s ON s.security_code=f.security_code WHERE {where}
                """,
                params,
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT f.*, COALESCE(s.security_name, f.security_code) AS security_name,
                       s.exchange, c.security_code IS NOT NULL AS in_candidate_pool
                FROM security_factor_snapshot f
                JOIN security_master s ON s.security_code=f.security_code
                LEFT JOIN research_candidate c ON c.security_code=f.security_code
                WHERE {where}
                ORDER BY {sort_column} {sort_direction}, f.security_code ASC
                LIMIT ? OFFSET ?
                """,
                [*params, max(1, min(limit, 500)), max(0, offset)],
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["quality_reasons"] = json.loads(item.pop("quality_reasons_json"))
            item["in_candidate_pool"] = bool(item["in_candidate_pool"])
            items.append(item)
        return {"items": items, "total": total}

    def add_research_candidate(self, code: str, snapshot_id: str, note: str = "") -> dict:
        normalized = normalize_code(code)
        now = utc_now()
        with self.connect() as conn:
            factor = conn.execute(
                """
                SELECT 1 FROM security_factor_snapshot
                WHERE snapshot_id=? AND security_code=? AND quality_status='passed'
                """,
                (snapshot_id, normalized),
            ).fetchone()
            if not factor:
                raise ValueError("证券不在该快照的正常排名范围内")
            conn.execute(
                """
                INSERT INTO research_candidate (
                    security_code, source_snapshot_id, note, added_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(security_code) DO UPDATE SET
                    source_snapshot_id=excluded.source_snapshot_id,
                    note=excluded.note, updated_at=excluded.updated_at
                """,
                (normalized, snapshot_id, note, now, now),
            )
        return next(item for item in self.list_research_candidates() if item["security_code"] == normalized)

    def list_research_candidates(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT c.*, COALESCE(s.security_name, c.security_code) AS security_name,
                       f.as_of, q.return_20d, q.return_60d, q.volatility_60d,
                       q.avg_traded_value_20d, q.max_drawdown_250d,
                       q.range_position_52w
                FROM research_candidate c
                JOIN security_master s ON s.security_code=c.security_code
                JOIN factor_snapshot f ON f.snapshot_id=c.source_snapshot_id
                JOIN security_factor_snapshot q ON q.snapshot_id=c.source_snapshot_id
                    AND q.security_code=c.security_code
                ORDER BY c.updated_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def remove_research_candidate(self, code: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM research_candidate WHERE security_code=?", (normalize_code(code),)
            )
        return cursor.rowcount > 0
