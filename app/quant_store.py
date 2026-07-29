from __future__ import annotations

from pathlib import Path

from .migrations import apply_migration
from .research_store import ResearchStore
from .sqlite_store import migrate_legacy_tables


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


class QuantStore(ResearchStore):
    """Quant-only state store with a synchronized security metadata projection."""

    def __init__(self, db_path: Path, master_store: ResearchStore):
        self.db_path = Path(db_path)
        self.master_store = master_store
        self.document_root = master_store.document_root
        self.fts_available = False
        self._initialize_quant()

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
