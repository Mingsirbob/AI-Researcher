from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.market.repository import KNOWN_NAMES, normalize_code
from app.research.financials import extract_financial_facts, format_fact_change
from app.core.migrations import apply_migration
from app.core.primitives import canonical_hash, sha256_text


FINANCIAL_REPORT_TITLE_TERMS = (
    "年度报告",
    "半年度报告",
    "季度报告",
    "业绩预告",
    "业绩快报",
)

QUERY_CONCEPTS = {
    "收入": ("营业收入", "收入"),
    "营收": ("营业收入", "营收"),
    "利润": ("净利润", "利润总额", "利润"),
    "盈利": ("净利润", "毛利率", "盈利"),
    "现金流": ("经营活动产生的现金流量净额", "现金流"),
    "毛利率": ("毛利率", "毛利"),
    "研发": ("研发投入", "研发费用", "研发"),
    "资本开支": ("资本性支出", "资本开支", "在建工程"),
    "分红": ("现金分红", "利润分配", "分红"),
    "回购": ("回购股份", "回购"),
    "减持": ("减持",),
    "增持": ("增持",),
    "担保": ("担保",),
    "诉讼": ("诉讼", "仲裁"),
    "存货": ("存货", "存货周转"),
    "应收": ("应收账款", "应收款项"),
    "负债": ("资产负债率", "负债合计", "负债"),
    "减值": ("资产减值", "信用减值", "减值损失"),
    "风险": ("风险", "减值"),
}

FINANCIAL_TEMPLATE_QUESTIONS = (
    "营业收入和净利润发生了什么变化？",
    "毛利率和盈利能力发生了什么变化？",
    "经营现金流和资本开支发生了什么变化？",
    "存货、应收账款和负债发生了什么变化？",
    "现金分红和股东回报发生了什么变化？",
)

RETIRED_SPLIT_DOMAIN_TABLES = (
    "paper_daily_batch_step",
    "paper_order",
    "paper_position",
    "paper_nav_snapshot",
    "paper_realtime_quote",
    "paper_benchmark_price",
    "paper_daily_run",
    "paper_daily_batch",
    "paper_account",
    "paper_strategy",
    "factor_release_decision",
    "factor_release_gate",
    "factor_release_candidate",
    "factor_backtest_trade",
    "factor_backtest_rebalance",
    "factor_backtest_nav",
    "factor_backtest_run",
    "factor_evaluation_correlation",
    "factor_evaluation_metric",
    "factor_evaluation_period",
    "factor_evaluation_run",
    "factor_lab_value",
    "factor_lab_snapshot",
    "model_factor_binding",
    "factor_set_member",
    "factor_set",
    "factor_release_review",
    "factor_version",
    "factor_definition",
    "factor_formula_template",
    "research_candidate",
    "security_factor_snapshot",
    "factor_snapshot",
    "current_shadow_signal",
    "current_shadow_snapshot",
    "model_validation_run",
    "shadow_signal",
    "prediction_snapshot",
    "model_artifact",
    "model_run",
)


def assistant_query_terms(question: str) -> list[str]:
    text = question.strip().lower()
    terms: list[str] = []
    for trigger, expansions in QUERY_CONCEPTS.items():
        if trigger in text:
            terms.extend(expansions)
    terms.extend(re.findall(r"[a-z][a-z0-9_.-]{1,30}|\d{2,}", text))
    quoted = re.findall(r"[\"“]([^\"”]{2,20})[\"”]", question)
    terms.extend(quoted)
    return list(dict.fromkeys(term for term in terms if term))[:20]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_sequence(value: str | None) -> str:
    text = str(value or "").strip()
    return text[:-2] if text.endswith(".0") and text[:-2].isdigit() else text


class ResearchStore:
    def __init__(
        self,
        db_path: Path,
        document_root: Path,
        *,
        retain_split_domains: bool = False,
    ):
        self.db_path = db_path
        self.document_root = document_root
        self.retain_split_domains = retain_split_domains
        self.document_root.mkdir(parents=True, exist_ok=True)
        self.fts_available = False
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        apply_migration(self.connect, "0001_research_core", self._create_schema)
        apply_migration(
            self.connect,
            "0016_quant_reference_boundary",
            self._remove_decision_shadow_foreign_key,
        )
        if not self.retain_split_domains:
            apply_migration(
                self.connect,
                "0022_retire_split_domain_tables",
                self._retire_split_domain_tables,
            )
        with self.connect() as conn:
            try:
                conn.execute("SELECT 1 FROM document_chunk_fts LIMIT 1")
                self.fts_available = True
            except sqlite3.OperationalError:
                self.fts_available = False

    def _retire_split_domain_tables(self) -> None:
        conn = self.connect()
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")
            for table in RETIRED_SPLIT_DOMAIN_TABLES:
                conn.execute(f'DROP TABLE IF EXISTS "{table}"')
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.close()

    def _remove_decision_shadow_foreign_key(self) -> None:
        with self.connect() as conn:
            has_external_reference = any(
                row[2] == "current_shadow_snapshot"
                for row in conn.execute("PRAGMA foreign_key_list(decision_case)")
            )
        if not has_external_reference:
            return

        conn = self.connect()
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")
            conn.executescript(
                """
                CREATE TABLE decision_case_quant_boundary (
                    case_id TEXT PRIMARY KEY,
                    security_code TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    decision_horizon_days INTEGER NOT NULL,
                    decision_horizon_label TEXT NOT NULL,
                    benchmark_code TEXT NOT NULL,
                    benchmark_name TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    rule_status TEXT NOT NULL,
                    research_run_id TEXT,
                    thesis_id TEXT,
                    shadow_snapshot_id TEXT,
                    scores_json TEXT NOT NULL,
                    gates_json TEXT NOT NULL,
                    risk_boundaries_json TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (security_code) REFERENCES security_master(security_code),
                    FOREIGN KEY (research_run_id) REFERENCES research_run(run_id)
                );
                INSERT INTO decision_case_quant_boundary
                SELECT * FROM decision_case;
                DROP TABLE decision_case;
                ALTER TABLE decision_case_quant_boundary RENAME TO decision_case;
                CREATE INDEX idx_decision_case_asof
                ON decision_case(as_of DESC, created_at DESC);
                """
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.close()

    def _create_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
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

                CREATE TABLE IF NOT EXISTS security_attribute_history (
                    id TEXT PRIMARY KEY,
                    security_code TEXT NOT NULL,
                    attribute_name TEXT NOT NULL,
                    attribute_value TEXT NOT NULL,
                    effective_from TEXT,
                    effective_to TEXT,
                    observed_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    UNIQUE(security_code, attribute_name, attribute_value, observed_at),
                    FOREIGN KEY (security_code) REFERENCES security_master(security_code)
                );

                CREATE TABLE IF NOT EXISTS announcement (
                    announcement_id TEXT PRIMARY KEY,
                    security_code TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_sequence TEXT,
                    title TEXT NOT NULL,
                    report_date TEXT,
                    published_at TEXT,
                    source_url TEXT,
                    metadata_hash TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'metadata_only',
                    current_document_id TEXT,
                    error TEXT,
                    UNIQUE(source, source_sequence),
                    FOREIGN KEY (security_code) REFERENCES security_master(security_code)
                );

                CREATE INDEX IF NOT EXISTS idx_announcement_security_date
                ON announcement(security_code, published_at DESC, report_date DESC);

                CREATE TABLE IF NOT EXISTS announcement_document (
                    document_id TEXT PRIMARY KEY,
                    announcement_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    content_type TEXT,
                    byte_size INTEGER NOT NULL,
                    fetched_at TEXT NOT NULL,
                    page_count INTEGER NOT NULL,
                    text_char_count INTEGER NOT NULL,
                    extraction_method TEXT NOT NULL,
                    text_layer_status TEXT NOT NULL,
                    parse_error TEXT,
                    UNIQUE(announcement_id, sha256),
                    FOREIGN KEY (announcement_id) REFERENCES announcement(announcement_id)
                );

                CREATE TABLE IF NOT EXISTS document_page (
                    document_id TEXT NOT NULL,
                    page_number INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    text_sha256 TEXT NOT NULL,
                    char_count INTEGER NOT NULL,
                    PRIMARY KEY (document_id, page_number),
                    FOREIGN KEY (document_id) REFERENCES announcement_document(document_id)
                );

                CREATE TABLE IF NOT EXISTS document_chunk (
                    chunk_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    page_start INTEGER NOT NULL,
                    page_end INTEGER NOT NULL,
                    section_path TEXT,
                    text TEXT NOT NULL,
                    text_sha256 TEXT NOT NULL,
                    char_count INTEGER NOT NULL,
                    UNIQUE(document_id, chunk_index),
                    FOREIGN KEY (document_id) REFERENCES announcement_document(document_id)
                );

                CREATE INDEX IF NOT EXISTS idx_document_chunk_document_page
                ON document_chunk(document_id, page_start, chunk_index);

                CREATE TABLE IF NOT EXISTS announcement_sync_run (
                    run_id TEXT PRIMARY KEY,
                    security_code TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    metadata_received INTEGER NOT NULL DEFAULT 0,
                    metadata_upserted INTEGER NOT NULL DEFAULT 0,
                    documents_downloaded INTEGER NOT NULL DEFAULT 0,
                    documents_parsed INTEGER NOT NULL DEFAULT 0,
                    documents_skipped INTEGER NOT NULL DEFAULT 0,
                    failed_documents TEXT NOT NULL DEFAULT '{}',
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS research_interaction (
                    interaction_id TEXT PRIMARY KEY,
                    security_code TEXT NOT NULL,
                    interaction_type TEXT NOT NULL,
                    question TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    evidence_snapshot_hash TEXT NOT NULL,
                    evidence_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (security_code) REFERENCES security_master(security_code)
                );

                CREATE INDEX IF NOT EXISTS idx_research_interaction_security_created
                ON research_interaction(security_code, created_at DESC);

                CREATE TABLE IF NOT EXISTS research_interaction_evidence (
                    interaction_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    evidence_id TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    PRIMARY KEY (interaction_id, position),
                    FOREIGN KEY (interaction_id) REFERENCES research_interaction(interaction_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS financial_report_period (
                    document_id TEXT PRIMARY KEY,
                    security_code TEXT NOT NULL,
                    report_type TEXT NOT NULL,
                    fiscal_year INTEGER NOT NULL,
                    period_label TEXT NOT NULL,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    comparison_period_start TEXT NOT NULL,
                    comparison_flow_period_end TEXT NOT NULL,
                    comparison_stock_period_end TEXT NOT NULL,
                    identification_method TEXT NOT NULL,
                    extraction_version TEXT NOT NULL,
                    identified_at TEXT NOT NULL,
                    FOREIGN KEY (document_id) REFERENCES announcement_document(document_id)
                );

                CREATE INDEX IF NOT EXISTS idx_financial_report_security_period
                ON financial_report_period(security_code, period_end DESC);

                CREATE TABLE IF NOT EXISTS financial_metric_fact (
                    fact_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    metric_code TEXT NOT NULL,
                    metric_label TEXT NOT NULL,
                    statement_type TEXT NOT NULL,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    comparison_period_start TEXT NOT NULL,
                    comparison_period_end TEXT NOT NULL,
                    raw_current_value TEXT NOT NULL,
                    raw_comparison_value TEXT NOT NULL,
                    display_unit TEXT NOT NULL,
                    unit_scale TEXT NOT NULL,
                    normalized_unit TEXT NOT NULL,
                    current_value TEXT NOT NULL,
                    comparison_value TEXT NOT NULL,
                    absolute_change TEXT NOT NULL,
                    change_pct TEXT,
                    source_page INTEGER NOT NULL,
                    source_text TEXT NOT NULL,
                    source_text_sha256 TEXT NOT NULL,
                    document_sha256 TEXT NOT NULL,
                    extraction_method TEXT NOT NULL,
                    extraction_version TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    extracted_at TEXT NOT NULL,
                    UNIQUE(document_id, metric_code, period_end, comparison_period_end),
                    FOREIGN KEY (document_id) REFERENCES announcement_document(document_id)
                );

                CREATE INDEX IF NOT EXISTS idx_financial_fact_metric_period
                ON financial_metric_fact(metric_code, period_end DESC);

                CREATE TABLE IF NOT EXISTS research_run (
                    run_id TEXT PRIMARY KEY,
                    security_code TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    workflow_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    evidence_snapshot_hash TEXT,
                    evidence_count INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    error TEXT,
                    FOREIGN KEY (security_code) REFERENCES security_master(security_code)
                );

                CREATE INDEX IF NOT EXISTS idx_research_run_security_started
                ON research_run(security_code, started_at DESC);

                CREATE TABLE IF NOT EXISTS research_artifact (
                    artifact_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, artifact_type),
                    FOREIGN KEY (run_id) REFERENCES research_run(run_id) ON DELETE CASCADE
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
                    FOREIGN KEY (snapshot_id) REFERENCES factor_snapshot(snapshot_id) ON DELETE CASCADE,
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

                CREATE TABLE IF NOT EXISTS evidence_acceptance_run (
                    run_id TEXT PRIMARY KEY,
                    manifest_version TEXT NOT NULL,
                    manifest_hash TEXT NOT NULL,
                    data_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    UNIQUE(manifest_hash, data_fingerprint)
                );

                CREATE INDEX IF NOT EXISTS idx_evidence_acceptance_finished
                ON evidence_acceptance_run(finished_at DESC);

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

                CREATE TABLE IF NOT EXISTS decision_case (
                    case_id TEXT PRIMARY KEY,
                    security_code TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    decision_horizon_days INTEGER NOT NULL,
                    decision_horizon_label TEXT NOT NULL,
                    benchmark_code TEXT NOT NULL,
                    benchmark_name TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    rule_status TEXT NOT NULL,
                    research_run_id TEXT,
                    thesis_id TEXT,
                    shadow_snapshot_id TEXT,
                    scores_json TEXT NOT NULL,
                    gates_json TEXT NOT NULL,
                    risk_boundaries_json TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (security_code) REFERENCES security_master(security_code),
                    FOREIGN KEY (research_run_id) REFERENCES research_run(run_id),
                    FOREIGN KEY (shadow_snapshot_id) REFERENCES current_shadow_snapshot(snapshot_id)
                );

                CREATE INDEX IF NOT EXISTS idx_decision_case_asof
                ON decision_case(as_of DESC, created_at DESC);

                CREATE TABLE IF NOT EXISTS decision_case_review (
                    review_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    decision TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(case_id, sequence),
                    FOREIGN KEY (case_id) REFERENCES decision_case(case_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_decision_review_case
                ON decision_case_review(case_id, sequence DESC);

                CREATE TABLE IF NOT EXISTS decision_outcome (
                    outcome_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    evaluation_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    entry_date TEXT,
                    entry_price REAL,
                    horizon_trading_days INTEGER NOT NULL,
                    target_date TEXT,
                    exit_date TEXT,
                    exit_price REAL,
                    security_return REAL,
                    benchmark_return REAL,
                    excess_return REAL,
                    maximum_adverse_excursion REAL,
                    security_data_source TEXT NOT NULL,
                    benchmark_data_source TEXT NOT NULL,
                    adjustment TEXT NOT NULL,
                    ifind_params TEXT NOT NULL,
                    data_fingerprint TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    gates_json TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    UNIQUE(case_id, evaluation_version, data_fingerprint),
                    FOREIGN KEY (case_id) REFERENCES decision_case(case_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_decision_outcome_case
                ON decision_outcome(case_id, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_decision_outcome_status
                ON decision_outcome(status, created_at DESC);
                """
            )
            try:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS document_chunk_fts USING fts5(
                        chunk_id UNINDEXED,
                        security_code UNINDEXED,
                        title,
                        text,
                        tokenize='unicode61'
                    )
                    """
                )
                self.fts_available = True
            except sqlite3.OperationalError:
                self.fts_available = False

    def bootstrap_securities(self, stock_db: Path) -> dict:
        now = utc_now()
        inserted = 0
        updated = 0
        with sqlite3.connect(stock_db, timeout=30) as source, self.connect() as target:
            tables = [
                row[0]
                for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'stock_%' ORDER BY name"
                )
            ]
            for table in tables:
                code = table.removeprefix("stock_").replace("_", ".")
                first_date, last_date = source.execute(
                    f'SELECT MIN(time), MAX(time) FROM "{table}"'
                ).fetchone()
                existing = target.execute(
                    "SELECT 1 FROM security_master WHERE security_code = ?", (code,)
                ).fetchone()
                target.execute(
                    """
                    INSERT INTO security_master (
                        security_code, exchange, security_name, listing_date,
                        listing_date_source, status, first_price_date, last_price_date,
                        source, source_updated_at, created_at, updated_at
                    ) VALUES (?, ?, ?, NULL, NULL, 'covered', ?, ?, 'local_stock_db', ?, ?, ?)
                    ON CONFLICT(security_code) DO UPDATE SET
                        first_price_date=excluded.first_price_date,
                        last_price_date=excluded.last_price_date,
                        security_name=COALESCE(security_master.security_name, excluded.security_name),
                        source_updated_at=excluded.source_updated_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        code,
                        code.split(".")[1],
                        KNOWN_NAMES.get(code),
                        first_date,
                        last_date,
                        now,
                        now,
                        now,
                    ),
                )
                inserted += 0 if existing else 1
                updated += 1 if existing else 0
        return {"securities": len(tables), "inserted": inserted, "updated": updated}

    def upsert_security_name(self, code: str, name: str, source: str = "iFinD") -> None:
        normalized = normalize_code(code)
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO security_master (
                    security_code, exchange, security_name, status, source,
                    source_updated_at, created_at, updated_at
                ) VALUES (?, ?, ?, 'ifind_observed', ?, ?, ?, ?)
                ON CONFLICT(security_code) DO UPDATE SET
                    security_name=excluded.security_name,
                    source_updated_at=excluded.source_updated_at,
                    updated_at=excluded.updated_at
                """,
                (normalized, normalized.split(".")[1], name, source, now, now, now),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO security_attribute_history (
                    id, security_code, attribute_name, attribute_value, observed_at, source
                ) VALUES (?, ?, 'security_name', ?, ?, ?)
                """,
                (str(uuid.uuid4()), normalized, name, now, source),
            )

    def security(self, code: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM security_master WHERE security_code = ?",
                (normalize_code(code),),
            ).fetchone()
        return dict(row) if row else None

    def security_at(self, code: str, as_of: str) -> dict | None:
        item = self.security(code)
        if item is None:
            return None
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT attribute_name, attribute_value, effective_from, effective_to,
                          observed_at, source
                   FROM security_attribute_history
                   WHERE security_code=?
                     AND COALESCE(effective_from, substr(observed_at, 1, 10))<=?
                     AND (effective_to IS NULL OR effective_to>=?)
                   ORDER BY attribute_name, COALESCE(effective_from, observed_at) DESC,
                            observed_at DESC""",
                (normalize_code(code), as_of, as_of),
            ).fetchall()
        attributes: dict[str, dict] = {}
        for row in rows:
            if row["attribute_name"] not in attributes:
                attributes[row["attribute_name"]] = dict(row)
        for name, value in attributes.items():
            item[name] = value["attribute_value"]
        item["as_of"] = as_of
        item["attribute_history"] = attributes
        return item

    def search_document_chunks(
        self, code: str, query: str, *, as_of: str | None = None, limit: int = 20
    ) -> dict:
        terms = assistant_query_terms(query)
        if not terms:
            return {"items": [], "query": query, "retrieval_method": "empty_query"}
        normalized = normalize_code(code)
        capped = max(1, min(limit, 100))
        date_sql = "AND COALESCE(a.published_at, a.report_date)<=?" if as_of else ""
        date_params = [as_of + " 23:59:59" if as_of and len(as_of) == 10 else as_of] if as_of else []
        rows = []
        method = "like_fallback"
        if self.fts_available:
            expression = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms[:12])
            try:
                with self.connect() as conn:
                    rows = conn.execute(
                        f"""SELECT c.chunk_id, c.document_id, c.page_start, c.page_end,
                                   c.text, a.title, a.published_at, a.report_date,
                                   bm25(document_chunk_fts, 0.0, 0.0, 6.0, 1.0) AS rank
                            FROM document_chunk_fts
                            JOIN document_chunk c ON c.chunk_id=document_chunk_fts.chunk_id
                            JOIN announcement_document d ON d.document_id=c.document_id
                            JOIN announcement a ON a.announcement_id=d.announcement_id
                            WHERE document_chunk_fts MATCH ?
                              AND document_chunk_fts.security_code=? {date_sql}
                            ORDER BY rank, COALESCE(a.published_at, a.report_date) DESC
                            LIMIT ?""",
                        [expression, normalized, *date_params, capped],
                    ).fetchall()
                method = "fts5_bm25"
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            likes = " OR ".join("c.text LIKE ? OR a.title LIKE ?" for _ in terms)
            params: list[object] = [normalized]
            for term in terms:
                params.extend([f"%{term}%", f"%{term}%"])
            with self.connect() as conn:
                rows = conn.execute(
                    f"""SELECT c.chunk_id, c.document_id, c.page_start, c.page_end,
                               c.text, a.title, a.published_at, a.report_date, 0.0 AS rank
                        FROM document_chunk c
                        JOIN announcement_document d ON d.document_id=c.document_id
                        JOIN announcement a ON a.announcement_id=d.announcement_id
                        WHERE a.security_code=? AND ({likes}) {date_sql}
                        ORDER BY COALESCE(a.published_at, a.report_date) DESC LIMIT ?""",
                    [*params, *date_params, capped],
                ).fetchall()
        return {
            "query": query,
            "retrieval_method": method,
            "items": [{**dict(row), "retrieval_score": -float(row["rank"] or 0)} for row in rows],
        }

    def market_data_end(self) -> str | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT MAX(last_price_date) FROM security_master"
            ).fetchone()[0]

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
                "securities": conn.execute("SELECT COUNT(*) FROM security_master").fetchone()[0],
                "announcements": conn.execute("SELECT COUNT(*) FROM announcement").fetchone()[0],
                "documents": conn.execute("SELECT COUNT(*) FROM announcement_document").fetchone()[0],
                "parsed_documents": conn.execute(
                    "SELECT COUNT(*) FROM announcement_document WHERE text_layer_status IN ('ok', 'partial')"
                ).fetchone()[0],
                "ocr_required_documents": conn.execute(
                    "SELECT COUNT(*) FROM announcement_document WHERE text_layer_status='ocr_required'"
                ).fetchone()[0],
                "evidence_chunks": conn.execute("SELECT COUNT(*) FROM document_chunk").fetchone()[0],
                "research_interactions": conn.execute(
                    "SELECT COUNT(*) FROM research_interaction"
                ).fetchone()[0],
                "evidence_snapshots": conn.execute(
                    "SELECT COUNT(*) FROM research_interaction_evidence"
                ).fetchone()[0],
                "financial_reports": conn.execute(
                    "SELECT COUNT(*) FROM financial_report_period"
                ).fetchone()[0],
                "financial_facts": conn.execute(
                    "SELECT COUNT(*) FROM financial_metric_fact"
                ).fetchone()[0],
                "research_runs": conn.execute("SELECT COUNT(*) FROM research_run").fetchone()[0],
                "research_artifacts": conn.execute(
                    "SELECT COUNT(*) FROM research_artifact"
                ).fetchone()[0],
                "factor_snapshots": count("factor_snapshot", "WHERE status='completed'"),
                "research_candidates": count("research_candidate"),
                "evidence_acceptance_runs": conn.execute(
                    "SELECT COUNT(*) FROM evidence_acceptance_run"
                ).fetchone()[0],
                "model_runs": count("model_run"),
                "shadow_signals": count("shadow_signal"),
                "model_validations": count("model_validation_run"),
                "current_shadow_signals": count("current_shadow_signal"),
                "decision_outcomes": conn.execute(
                    "SELECT COUNT(*) FROM decision_outcome"
                ).fetchone()[0],
                "fts_available": self.fts_available,
            }


    @staticmethod
    def _decode_decision_case(row: sqlite3.Row | dict) -> dict:
        item = dict(row)
        for key in ("scores", "gates", "risk_boundaries", "snapshot"):
            item[key] = json.loads(item.pop(f"{key}_json"))
        return item

    def save_decision_case(self, item: dict) -> dict:
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM decision_case WHERE snapshot_hash=?", (item["snapshot_hash"],)
            ).fetchone()
            if existing:
                return {**self.decision_case(existing["case_id"]), "reused": True}
            conn.execute(
                """
                INSERT INTO decision_case (
                    case_id, security_code, as_of, decision_horizon_days,
                    decision_horizon_label, benchmark_code, benchmark_name,
                    policy_version, rule_status, research_run_id, thesis_id,
                    shadow_snapshot_id, scores_json, gates_json,
                    risk_boundaries_json, snapshot_json, snapshot_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["case_id"], item["security_code"], item["as_of"],
                    item["decision_horizon_days"], item["decision_horizon_label"],
                    item["benchmark_code"], item["benchmark_name"], item["policy_version"],
                    item["rule_status"], item.get("research_run_id"), item.get("thesis_id"),
                    item.get("shadow_snapshot_id"),
                    json.dumps(item["scores"], ensure_ascii=False, sort_keys=True),
                    json.dumps(item["gates"], ensure_ascii=False, sort_keys=True),
                    json.dumps(item["risk_boundaries"], ensure_ascii=False, sort_keys=True),
                    json.dumps(item["snapshot"], ensure_ascii=False, sort_keys=True),
                    item["snapshot_hash"], item["created_at"],
                ),
            )
        return {**self.decision_case(item["case_id"]), "reused": False}

    def decision_case(self, case_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT c.*, COALESCE(m.security_name, c.security_code) AS security_name
                FROM decision_case c
                LEFT JOIN security_master m ON m.security_code=c.security_code
                WHERE c.case_id=?
                """,
                (case_id,),
            ).fetchone()
            if not row:
                return None
            reviews = conn.execute(
                "SELECT * FROM decision_case_review WHERE case_id=? ORDER BY sequence",
                (case_id,),
            ).fetchall()
        item = self._decode_decision_case(row)
        item["reviews"] = [dict(review) for review in reviews]
        item["review_status"] = reviews[-1]["decision"] if reviews else "pending"
        return item

    def list_decision_cases(
        self,
        *,
        code: str | None = None,
        rule_status: str | None = None,
        review_status: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        conditions: list[str] = []
        params: list[object] = []
        if code:
            conditions.append("security_code=?")
            params.append(normalize_code(code))
        if rule_status:
            conditions.append("rule_status=?")
            params.append(rule_status)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT case_id FROM decision_case {where} ORDER BY created_at DESC LIMIT ?",
                [*params, max(1, min(limit, 500))],
            ).fetchall()
        items = [self.decision_case(row["case_id"]) for row in rows]
        items = [item for item in items if item is not None]
        if review_status:
            items = [item for item in items if item["review_status"] == review_status]
        return items[: max(1, min(limit, 100))]

    def append_decision_review(
        self,
        *,
        case_id: str,
        decision: str,
        reviewer: str,
        note: str,
    ) -> dict:
        now = utc_now()
        with self.connect() as conn:
            case = conn.execute(
                "SELECT rule_status FROM decision_case WHERE case_id=?", (case_id,)
            ).fetchone()
            if not case:
                raise KeyError(case_id)
            previous = conn.execute(
                "SELECT decision FROM decision_case_review WHERE case_id=? ORDER BY sequence DESC LIMIT 1",
                (case_id,),
            ).fetchone()
            if previous:
                raise ValueError("该决策案例已经完成审批；研究变化后应创建新案例")
            if decision == "approve_for_tracking" and case["rule_status"] != "eligible_for_review":
                raise ValueError("只有 eligible_for_review 案例可以批准进入 Shadow 跟踪")
            review_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO decision_case_review (
                    review_id, case_id, sequence, decision, reviewer, note, created_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?)
                """,
                (review_id, case_id, decision, reviewer, note, now),
            )
        return self.decision_case(case_id)

    @staticmethod
    def _decode_decision_outcome(row: sqlite3.Row | dict) -> dict:
        item = dict(row)
        for key in ("metrics", "gates", "snapshot"):
            item[key] = json.loads(item.pop(f"{key}_json"))
        return item

    def save_decision_outcome(self, item: dict) -> dict:
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT outcome_id FROM decision_outcome WHERE case_id=? AND evaluation_version=? AND data_fingerprint=?",
                (item["case_id"], item["evaluation_version"], item["data_fingerprint"]),
            ).fetchone()
            if existing:
                return {**self.decision_outcome(existing["outcome_id"]), "reused": True}
            conn.execute(
                """
                INSERT INTO decision_outcome (
                    outcome_id, case_id, evaluation_version, status, entry_date,
                    entry_price, horizon_trading_days, target_date, exit_date,
                    exit_price, security_return, benchmark_return, excess_return,
                    maximum_adverse_excursion, security_data_source,
                    benchmark_data_source, adjustment, ifind_params,
                    data_fingerprint, metrics_json, gates_json, snapshot_json,
                    snapshot_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["outcome_id"], item["case_id"], item["evaluation_version"],
                    item["status"], item.get("entry_date"), item.get("entry_price"),
                    item["horizon_trading_days"], item.get("target_date"),
                    item.get("exit_date"), item.get("exit_price"),
                    item.get("security_return"), item.get("benchmark_return"),
                    item.get("excess_return"), item.get("maximum_adverse_excursion"),
                    item["security_data_source"], item["benchmark_data_source"],
                    item["adjustment"], item["ifind_params"], item["data_fingerprint"],
                    json.dumps(item["metrics"], ensure_ascii=False, sort_keys=True),
                    json.dumps(item["gates"], ensure_ascii=False, sort_keys=True),
                    json.dumps(item["snapshot"], ensure_ascii=False, sort_keys=True),
                    item["snapshot_hash"], item["created_at"],
                ),
            )
        return {**self.decision_outcome(item["outcome_id"]), "reused": False}

    def decision_outcome(self, outcome_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM decision_outcome WHERE outcome_id=?", (outcome_id,)
            ).fetchone()
        return self._decode_decision_outcome(row) if row else None

    def decision_case_outcome(self, case_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM decision_outcome WHERE case_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (case_id,),
            ).fetchone()
        return self._decode_decision_outcome(row) if row else None

    def list_decision_outcomes(self, *, status: str | None = None, limit: int = 100) -> list[dict]:
        where = "WHERE status=?" if status else ""
        params: list[object] = [status] if status else []
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM decision_outcome {where} ORDER BY created_at DESC, rowid DESC LIMIT ?",
                [*params, max(1, min(limit, 500))],
            ).fetchall()
        return [self._decode_decision_outcome(row) for row in rows]

    def latest_decision_outcomes(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT o.*, c.security_code, c.benchmark_code, c.decision_horizon_days,
                       c.rule_status, c.policy_version, c.scores_json,
                       COALESCE((SELECT r.decision FROM decision_case_review r
                                 WHERE r.case_id=c.case_id ORDER BY r.sequence DESC LIMIT 1), 'pending') AS review_status
                FROM decision_outcome o
                JOIN decision_case c ON c.case_id=o.case_id
                WHERE o.rowid=(SELECT o2.rowid FROM decision_outcome o2
                               WHERE o2.case_id=o.case_id ORDER BY o2.created_at DESC, o2.rowid DESC LIMIT 1)
                ORDER BY o.created_at DESC
                """
            ).fetchall()
        items = []
        for row in rows:
            item = self._decode_decision_outcome(row)
            item["scores"] = json.loads(item.pop("scores_json"))
            items.append(item)
        return items


    def save_evidence_acceptance_run(self, result: dict) -> dict:
        existing = None
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT * FROM evidence_acceptance_run
                WHERE manifest_hash=? AND data_fingerprint=?
                """,
                (result["manifest_hash"], result["data_fingerprint"]),
            ).fetchone()
            if existing is None:
                run_id = str(uuid.uuid4())
                payload_json = json.dumps(
                    result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                snapshot_hash = sha256_text(payload_json)
                finished_at = utc_now()
                conn.execute(
                    """
                    INSERT INTO evidence_acceptance_run (
                        run_id, manifest_version, manifest_hash, data_fingerprint,
                        status, result_json, snapshot_hash, started_at, finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        result["manifest_version"],
                        result["manifest_hash"],
                        result["data_fingerprint"],
                        result["status"],
                        payload_json,
                        snapshot_hash,
                        result["started_at"],
                        finished_at,
                    ),
                )
                return {
                    **result,
                    "run_id": run_id,
                    "snapshot_hash": snapshot_hash,
                    "finished_at": finished_at,
                    "reused": False,
                }
        item = dict(existing)
        payload = json.loads(item.pop("result_json"))
        return {**payload, **item, "reused": True}

    def evidence_acceptance_run(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM evidence_acceptance_run WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        payload = json.loads(item.pop("result_json"))
        return {**payload, **item}

    def latest_evidence_acceptance_run(self) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT run_id FROM evidence_acceptance_run ORDER BY finished_at DESC LIMIT 1"
            ).fetchone()
        return self.evidence_acceptance_run(row["run_id"]) if row else None

    def list_evidence_acceptance_runs(self, limit: int = 20) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, manifest_version, manifest_hash, data_fingerprint,
                       status, snapshot_hash, started_at, finished_at
                FROM evidence_acceptance_run
                ORDER BY finished_at DESC LIMIT ?
                """,
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def start_research_run(
        self,
        *,
        code: str,
        as_of: str,
        workflow_version: str,
    ) -> str:
        run_id = str(uuid.uuid4())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO research_run (
                    run_id, security_code, as_of, workflow_version, status, started_at
                ) VALUES (?, ?, ?, ?, 'running', ?)
                """,
                (run_id, normalize_code(code), as_of, workflow_version, utc_now()),
            )
        return run_id

    def save_research_artifact(
        self,
        *,
        run_id: str,
        artifact_type: str,
        schema_version: str,
        status: str,
        payload: dict,
    ) -> dict:
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        snapshot_hash = sha256_text(payload_json)
        artifact_id = str(uuid.uuid4())
        created_at = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO research_artifact (
                    artifact_id, run_id, artifact_type, schema_version, status,
                    payload_json, snapshot_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    run_id,
                    artifact_type,
                    schema_version,
                    status,
                    payload_json,
                    snapshot_hash,
                    created_at,
                ),
            )
        return {
            "artifact_id": artifact_id,
            "artifact_type": artifact_type,
            "schema_version": schema_version,
            "status": status,
            "snapshot_hash": snapshot_hash,
            "created_at": created_at,
        }

    def finish_research_run(
        self,
        run_id: str,
        *,
        status: str,
        evidence_snapshot_hash: str | None = None,
        evidence_count: int = 0,
        error: str | None = None,
    ) -> None:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE research_run
                SET status=?, evidence_snapshot_hash=?, evidence_count=?,
                    finished_at=?, error=?
                WHERE run_id=? AND status='running'
                """,
                (
                    status,
                    evidence_snapshot_hash,
                    evidence_count,
                    utc_now(),
                    error[:1000] if error else None,
                    run_id,
                ),
            )
        if cursor.rowcount != 1:
            raise ValueError("研究任务不存在或已经结束")

    def list_research_runs(self, code: str, limit: int = 20) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, security_code, as_of, workflow_version, status,
                       evidence_snapshot_hash, evidence_count, started_at, finished_at, error
                FROM research_run
                WHERE security_code=?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (normalize_code(code), max(1, min(limit, 100))),
            ).fetchall()
        return [dict(row) for row in rows]

    def research_run(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM research_run WHERE run_id=?", (run_id,)
            ).fetchone()
            if not row:
                return None
            artifacts = conn.execute(
                """
                SELECT artifact_id, artifact_type, schema_version, status,
                       payload_json, snapshot_hash, created_at
                FROM research_artifact
                WHERE run_id=? ORDER BY created_at, artifact_type
                """,
                (run_id,),
            ).fetchall()
        item = dict(row)
        item["artifacts"] = [
            {
                **{key: artifact[key] for key in artifact.keys() if key != "payload_json"},
                "payload": json.loads(artifact["payload_json"]),
            }
            for artifact in artifacts
        ]
        return item

    def latest_research_assessment(self, code: str, as_of: str | None = None) -> dict | None:
        params: list = [normalize_code(code)]
        date_filter = ""
        if as_of:
            date_filter = " AND r.as_of<=?"
            params.append(as_of)
        with self.connect() as conn:
            row = conn.execute(
                f"""SELECT a.artifact_id, a.run_id, a.schema_version, a.status,
                           a.payload_json, a.snapshot_hash, a.created_at,
                           r.security_code, r.as_of, r.finished_at
                    FROM research_artifact a
                    JOIN research_run r ON r.run_id=a.run_id
                    WHERE r.security_code=? {date_filter}
                      AND r.status IN ('completed','completed_with_gaps')
                      AND a.artifact_type='research_assessment'
                    ORDER BY r.as_of DESC, r.finished_at DESC, a.created_at DESC
                    LIMIT 1""",
                params,
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        return item

    def list_securities(self, query: str = "", limit: int = 12) -> list[dict]:
        needle = f"%{query.strip()}%"
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT security_code AS code,
                       COALESCE(security_name, security_code) AS name,
                       security_name IS NOT NULL AS has_name,
                       exchange, board, industry_l1, first_price_date, last_price_date
                FROM security_master
                WHERE ? = '%%' OR security_code LIKE ? OR security_name LIKE ?
                ORDER BY security_code LIMIT ?
                """,
                (needle, needle, needle, max(1, min(limit, 50))),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _announcement_id(code: str, sequence: str, metadata: dict) -> str:
        if sequence:
            return f"ifind:{sequence}"
        return f"ifind-hash:{canonical_hash({'code': code, **metadata}, compact=False)}"

    def upsert_announcements(self, code: str, announcements: list[dict]) -> list[str]:
        normalized = normalize_code(code)
        now = utc_now()
        ids: list[str] = []
        with self.connect() as conn:
            for item in announcements:
                sequence = normalize_sequence(item.get("sequence"))
                metadata = {
                    "title": str(item.get("title") or "").strip(),
                    "report_date": str(item.get("date") or "").strip(),
                    "published_at": str(item.get("published_at") or "").strip(),
                    "source_url": str(item.get("url") or "").strip(),
                }
                if not metadata["title"]:
                    continue
                announcement_id = self._announcement_id(normalized, sequence, metadata)
                metadata_hash = canonical_hash(metadata, compact=False)
                conn.execute(
                    """
                    INSERT INTO announcement (
                        announcement_id, security_code, source, source_sequence, title,
                        report_date, published_at, source_url, metadata_hash,
                        first_seen_at, last_seen_at
                    ) VALUES (?, ?, 'iFinD', ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(announcement_id) DO UPDATE SET
                        title=excluded.title,
                        report_date=excluded.report_date,
                        published_at=excluded.published_at,
                        source_url=excluded.source_url,
                        metadata_hash=excluded.metadata_hash,
                        last_seen_at=excluded.last_seen_at
                    """,
                    (
                        announcement_id,
                        normalized,
                        sequence or None,
                        metadata["title"],
                        metadata["report_date"] or None,
                        metadata["published_at"] or None,
                        metadata["source_url"] or None,
                        metadata_hash,
                        now,
                        now,
                    ),
                )
                ids.append(announcement_id)
        return ids

    def announcement(self, announcement_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT a.*, d.sha256, d.file_path, d.page_count, d.text_char_count,
                       d.text_layer_status, d.version AS document_version
                FROM announcement a
                LEFT JOIN announcement_document d ON d.document_id = a.current_document_id
                WHERE a.announcement_id = ?
                """,
                (announcement_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_announcements(
        self,
        code: str,
        *,
        as_of: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        conditions = ["a.security_code = ?"]
        params: list[object] = [normalize_code(code)]
        if as_of:
            conditions.append("COALESCE(a.published_at, a.report_date) <= ?")
            params.append(as_of + " 23:59:59" if len(as_of) == 10 else as_of)
        params.append(max(1, min(limit, 100)))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT a.*, d.sha256, d.page_count, d.text_char_count,
                       d.text_layer_status, d.version AS document_version
                FROM announcement a
                LEFT JOIN announcement_document d ON d.document_id = a.current_document_id
                WHERE {' AND '.join(conditions)}
                ORDER BY COALESCE(a.published_at, a.report_date) DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_documents(
        self,
        announcement_ids: list[str],
        limit: int,
        document_scope: str = "all",
    ) -> list[dict]:
        if not announcement_ids or limit < 1:
            return []
        placeholders = ",".join("?" for _ in announcement_ids)
        scope_condition = ""
        params: list[object] = [*announcement_ids]
        if document_scope == "financial_reports":
            scope_condition = "AND (" + " OR ".join(
                "title LIKE ?" for _ in FINANCIAL_REPORT_TITLE_TERMS
            ) + ")"
            params.extend(f"%{term}%" for term in FINANCIAL_REPORT_TITLE_TERMS)
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM announcement
                WHERE announcement_id IN ({placeholders})
                  AND source_url IS NOT NULL AND source_url != ''
                  AND status IN ('metadata_only', 'download_failed', 'parse_failed')
                  {scope_condition}
                ORDER BY COALESCE(published_at, report_date) DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def save_document(
        self,
        *,
        announcement_id: str,
        sha256: str,
        file_path: str,
        source_url: str,
        content_type: str,
        byte_size: int,
        pages: list[str],
        chunks: list[dict],
        extraction_method: str,
        text_layer_status: str,
        parse_error: str | None = None,
    ) -> tuple[str, bool]:
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT document_id, text_layer_status, parse_error FROM announcement_document "
                "WHERE announcement_id=? AND sha256=?",
                (announcement_id, sha256),
            ).fetchone()
            if existing:
                status = (
                    "parse_failed"
                    if existing["parse_error"]
                    else "ocr_required"
                    if existing["text_layer_status"] == "ocr_required"
                    else "parsed"
                )
                conn.execute(
                    "UPDATE announcement SET current_document_id=?, status=?, error=? WHERE announcement_id=?",
                    (existing["document_id"], status, existing["parse_error"], announcement_id),
                )
                return existing["document_id"], False
            version = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM announcement_document WHERE announcement_id=?",
                (announcement_id,),
            ).fetchone()[0]
            document_id = str(uuid.uuid4())
            text_char_count = sum(len(page) for page in pages)
            conn.execute(
                """
                INSERT INTO announcement_document (
                    document_id, announcement_id, version, sha256, file_path, source_url,
                    content_type, byte_size, fetched_at, page_count, text_char_count,
                    extraction_method, text_layer_status, parse_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    announcement_id,
                    version,
                    sha256,
                    file_path,
                    source_url,
                    content_type,
                    byte_size,
                    utc_now(),
                    len(pages),
                    text_char_count,
                    extraction_method,
                    text_layer_status,
                    parse_error,
                ),
            )
            for page_number, text in enumerate(pages, start=1):
                conn.execute(
                    "INSERT INTO document_page VALUES (?, ?, ?, ?, ?)",
                    (
                        document_id,
                        page_number,
                        text,
                        sha256_text(text),
                        len(text),
                    ),
                )
            security_code, title = conn.execute(
                "SELECT security_code, title FROM announcement WHERE announcement_id=?",
                (announcement_id,),
            ).fetchone()
            for chunk in chunks:
                chunk_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO document_chunk (
                        chunk_id, document_id, chunk_index, page_start, page_end,
                        section_path, text, text_sha256, char_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_id,
                        document_id,
                        chunk["chunk_index"],
                        chunk["page_start"],
                        chunk["page_end"],
                        chunk.get("section_path"),
                        chunk["text"],
                        sha256_text(chunk["text"]),
                        len(chunk["text"]),
                    ),
                )
                if self.fts_available:
                    conn.execute(
                        "INSERT INTO document_chunk_fts VALUES (?, ?, ?, ?)",
                        (chunk_id, security_code, title, chunk["text"]),
                    )
            status = (
                "parse_failed"
                if parse_error
                else "ocr_required"
                if text_layer_status == "ocr_required"
                else "parsed"
            )
            conn.execute(
                """
                UPDATE announcement
                SET current_document_id=?, status=?, error=? WHERE announcement_id=?
                """,
                (document_id, status, parse_error, announcement_id),
            )
        return document_id, True

    def mark_announcement_error(self, announcement_id: str, status: str, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE announcement SET status=?, error=? WHERE announcement_id=?",
                (status, error[:500], announcement_id),
            )

    def current_documents(self, code: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT d.*, a.security_code, a.title
                FROM announcement_document d
                JOIN announcement a ON a.current_document_id=d.document_id
                WHERE a.security_code=? ORDER BY a.published_at DESC
                """,
                (normalize_code(code),),
            ).fetchall()
        return [dict(row) for row in rows]

    def replace_document_extraction(
        self,
        document_id: str,
        *,
        pages: list[str],
        chunks: list[dict],
        extraction_method: str,
        text_layer_status: str,
        parse_error: str | None,
    ) -> None:
        with self.connect() as conn:
            document = conn.execute(
                """
                SELECT d.document_id, d.announcement_id, a.security_code, a.title
                FROM announcement_document d
                JOIN announcement a ON a.announcement_id=d.announcement_id
                WHERE d.document_id=?
                """,
                (document_id,),
            ).fetchone()
            if not document:
                raise KeyError(document_id)
            chunk_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT chunk_id FROM document_chunk WHERE document_id=?", (document_id,)
                )
            ]
            if self.fts_available:
                conn.executemany(
                    "DELETE FROM document_chunk_fts WHERE chunk_id=?",
                    [(chunk_id,) for chunk_id in chunk_ids],
                )
            conn.execute("DELETE FROM document_chunk WHERE document_id=?", (document_id,))
            conn.execute("DELETE FROM document_page WHERE document_id=?", (document_id,))
            for page_number, text in enumerate(pages, start=1):
                conn.execute(
                    "INSERT INTO document_page VALUES (?, ?, ?, ?, ?)",
                    (
                        document_id,
                        page_number,
                        text,
                        sha256_text(text),
                        len(text),
                    ),
                )
            for chunk in chunks:
                chunk_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO document_chunk (
                        chunk_id, document_id, chunk_index, page_start, page_end,
                        section_path, text, text_sha256, char_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_id,
                        document_id,
                        chunk["chunk_index"],
                        chunk["page_start"],
                        chunk["page_end"],
                        chunk.get("section_path"),
                        chunk["text"],
                        sha256_text(chunk["text"]),
                        len(chunk["text"]),
                    ),
                )
                if self.fts_available:
                    conn.execute(
                        "INSERT INTO document_chunk_fts VALUES (?, ?, ?, ?)",
                        (chunk_id, document["security_code"], document["title"], chunk["text"]),
                    )
            conn.execute(
                """
                UPDATE announcement_document
                SET page_count=?, text_char_count=?, extraction_method=?,
                    text_layer_status=?, parse_error=? WHERE document_id=?
                """,
                (
                    len(pages),
                    sum(len(page) for page in pages),
                    extraction_method,
                    text_layer_status,
                    parse_error,
                    document_id,
                ),
            )
            status = "ocr_required" if text_layer_status == "ocr_required" else "parsed"
            conn.execute(
                "UPDATE announcement SET status=?, error=? WHERE announcement_id=?",
                (status, parse_error, document["announcement_id"]),
            )

    def evidence_for_security(
        self,
        code: str,
        *,
        as_of: str | None = None,
        query: str = "",
        limit: int = 5,
    ) -> list[dict]:
        conditions = ["a.security_code = ?"]
        params: list[object] = [normalize_code(code)]
        if as_of:
            conditions.append("COALESCE(a.published_at, a.report_date) <= ?")
            params.append(as_of + " 23:59:59" if len(as_of) == 10 else as_of)
        if query:
            conditions.append("(c.text LIKE ? OR a.title LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])
        else:
            conditions.append("c.chunk_index = 0")
        params.append(max(1, min(limit, 20)))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT c.chunk_id, c.document_id, c.page_start, c.page_end, c.text,
                       d.sha256, a.announcement_id, a.title, a.published_at, a.report_date
                FROM document_chunk c
                JOIN announcement_document d ON d.document_id = c.document_id
                JOIN announcement a ON a.announcement_id = d.announcement_id
                WHERE {' AND '.join(conditions)}
                ORDER BY COALESCE(a.published_at, a.report_date) DESC, c.chunk_index ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        evidence = []
        for row in rows:
            item = dict(row)
            page_label = (
                str(item["page_start"])
                if item["page_start"] == item["page_end"]
                else f"{item['page_start']}-{item['page_end']}"
            )
            evidence.append(
                {
                    "id": f"ev-doc-{item['chunk_id']}",
                    "category": "external_document",
                    "label": f"公告原文 p.{page_label}",
                    "value": item["text"][:1200],
                    "as_of": item["published_at"] or item["report_date"],
                    "source": f"/api/documents/{item['document_id']}/file#page={item['page_start']}",
                    "method": "本地持久化公告 PDF 按页提取；引用绑定文档 SHA-256 与页码",
                    "citation": {
                        "announcement_id": item["announcement_id"],
                        "document_id": item["document_id"],
                        "chunk_id": item["chunk_id"],
                        "page_start": item["page_start"],
                        "page_end": item["page_end"],
                        "sha256": item["sha256"],
                        "title": item["title"],
                    },
                }
            )
        return evidence

    def assistant_evidence(
        self,
        code: str,
        question: str,
        *,
        as_of: str | None = None,
        scope: str = "all",
        limit: int = 8,
    ) -> list[dict]:
        conditions = ["a.security_code = ?"]
        params: list[object] = [normalize_code(code)]
        if as_of:
            conditions.append("COALESCE(a.published_at, a.report_date) <= ?")
            params.append(as_of + " 23:59:59" if len(as_of) == 10 else as_of)
        if scope == "financial_reports":
            title_conditions = " OR ".join("a.title LIKE ?" for _ in FINANCIAL_REPORT_TITLE_TERMS)
            conditions.append(f"({title_conditions})")
            params.extend(f"%{term}%" for term in FINANCIAL_REPORT_TITLE_TERMS)
        elif scope == "announcements":
            title_conditions = " OR ".join("a.title LIKE ?" for _ in FINANCIAL_REPORT_TITLE_TERMS)
            conditions.append(f"NOT ({title_conditions})")
            params.extend(f"%{term}%" for term in FINANCIAL_REPORT_TITLE_TERMS)

        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT c.chunk_id, c.document_id, c.chunk_index, c.page_start, c.page_end,
                       c.text, c.text_sha256, d.sha256, a.announcement_id, a.title,
                       a.published_at, a.report_date
                FROM document_chunk c
                JOIN announcement_document d ON d.document_id = c.document_id
                JOIN announcement a ON a.announcement_id = d.announcement_id
                WHERE {' AND '.join(conditions)}
                ORDER BY COALESCE(a.published_at, a.report_date) DESC, c.chunk_index ASC
                LIMIT 600
                """,
                params,
            ).fetchall()

        terms = assistant_query_terms(question)
        overview = any(
            phrase in question
            for phrase in ("最新公告", "近期公告", "有哪些公告", "公告摘要", "财报摘要", "总结")
        )
        scored: list[tuple[float, dict]] = []
        for row in rows:
            item = dict(row)
            title = item["title"].lower()
            text = item["text"].lower()
            score = sum(6 for term in terms if term.lower() in title)
            score += sum(min(text.count(term.lower()), 3) for term in terms)
            if not terms and (overview or scope == "financial_reports") and item["chunk_index"] == 0:
                score = 1
            if score:
                scored.append((score, item))
        scored.sort(
            key=lambda pair: (
                pair[0],
                pair[1]["published_at"] or pair[1]["report_date"] or "",
                -pair[1]["chunk_index"],
            ),
            reverse=True,
        )

        selected: list[dict] = []
        per_document: dict[str, int] = {}
        seen_text: set[str] = set()
        for score, item in scored:
            if per_document.get(item["document_id"], 0) >= 2 or item["text_sha256"] in seen_text:
                continue
            per_document[item["document_id"]] = per_document.get(item["document_id"], 0) + 1
            seen_text.add(item["text_sha256"])
            page_label = (
                str(item["page_start"])
                if item["page_start"] == item["page_end"]
                else f"{item['page_start']}-{item['page_end']}"
            )
            selected.append(
                {
                    "id": f"ev-doc-{item['chunk_id']}",
                    "category": "external_document",
                    "label": f"公告原文 p.{page_label}",
                    "value": item["text"][:1800],
                    "as_of": item["published_at"] or item["report_date"],
                    "source": f"/api/documents/{item['document_id']}/file#page={item['page_start']}",
                    "method": "本地归档 PDF 页内文本块；问答引用绑定文档 SHA-256 与页码",
                    "retrieval_score": score,
                    "citation": {
                        "announcement_id": item["announcement_id"],
                        "document_id": item["document_id"],
                        "chunk_id": item["chunk_id"],
                        "page_start": item["page_start"],
                        "page_end": item["page_end"],
                        "sha256": item["sha256"],
                        "title": item["title"],
                    },
                }
            )
            if len(selected) >= max(1, min(limit, 12)):
                break
        return selected

    def financial_template_evidence(
        self,
        code: str,
        *,
        as_of: str | None = None,
        limit: int = 16,
    ) -> list[dict]:
        selected: list[dict] = []
        seen: set[str] = set()
        for question in FINANCIAL_TEMPLATE_QUESTIONS:
            for item in self.assistant_evidence(
                code,
                question,
                as_of=as_of,
                scope="financial_reports",
                limit=6,
            ):
                if item["id"] in seen:
                    continue
                seen.add(item["id"])
                selected.append(item)
                if len(selected) >= max(1, min(limit, 24)):
                    return selected
        return selected

    def document_pages(self, document_id: str) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT text FROM document_page WHERE document_id=? ORDER BY page_number",
                (document_id,),
            ).fetchall()
        return [row["text"] for row in rows]

    def save_financial_extraction(self, document_id: str, extraction: dict) -> dict:
        report = extraction.get("report")
        if report is None:
            return {"document_id": document_id, "report_identified": False, "facts": 0}
        document = self.document(document_id)
        if document is None:
            raise KeyError(document_id)
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO financial_report_period (
                    document_id, security_code, report_type, fiscal_year, period_label,
                    period_start, period_end, comparison_period_start,
                    comparison_flow_period_end, comparison_stock_period_end,
                    identification_method, extraction_version, identified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    report_type=excluded.report_type,
                    fiscal_year=excluded.fiscal_year,
                    period_label=excluded.period_label,
                    period_start=excluded.period_start,
                    period_end=excluded.period_end,
                    comparison_period_start=excluded.comparison_period_start,
                    comparison_flow_period_end=excluded.comparison_flow_period_end,
                    comparison_stock_period_end=excluded.comparison_stock_period_end,
                    identification_method=excluded.identification_method,
                    extraction_version=excluded.extraction_version,
                    identified_at=excluded.identified_at
                """,
                (
                    document_id,
                    document["security_code"],
                    report["report_type"],
                    report["fiscal_year"],
                    report["period_label"],
                    report["period_start"],
                    report["period_end"],
                    report["comparison_period_start"],
                    report["comparison_flow_period_end"],
                    report["comparison_stock_period_end"],
                    report["identification_method"],
                    report["extraction_version"],
                    now,
                ),
            )
            conn.execute("DELETE FROM financial_metric_fact WHERE document_id=?", (document_id,))
            for fact in extraction.get("facts", []):
                conn.execute(
                    """
                    INSERT INTO financial_metric_fact (
                        fact_id, document_id, metric_code, metric_label, statement_type,
                        period_start, period_end, comparison_period_start,
                        comparison_period_end, raw_current_value, raw_comparison_value,
                        display_unit, unit_scale, normalized_unit, current_value,
                        comparison_value, absolute_change, change_pct, source_page,
                        source_text, source_text_sha256, document_sha256,
                        extraction_method, extraction_version, confidence, extracted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fact["fact_id"],
                        document_id,
                        fact["metric_code"],
                        fact["metric_label"],
                        fact["statement_type"],
                        fact["period_start"],
                        fact["period_end"],
                        fact["comparison_period_start"],
                        fact["comparison_period_end"],
                        fact["raw_current_value"],
                        fact["raw_comparison_value"],
                        fact["display_unit"],
                        fact["unit_scale"],
                        fact["normalized_unit"],
                        fact["current_value"],
                        fact["comparison_value"],
                        fact["absolute_change"],
                        fact["change_pct"],
                        fact["source_page"],
                        fact["source_text"],
                        fact["source_text_sha256"],
                        fact["document_sha256"],
                        fact["extraction_method"],
                        fact["extraction_version"],
                        fact["confidence"],
                        now,
                    ),
                )
        return {
            "document_id": document_id,
            "report_identified": True,
            "period_end": report["period_end"],
            "facts": len(extraction.get("facts", [])),
            "issues": extraction.get("issues", []),
        }

    def refresh_financial_facts(self, code: str, as_of: str | None = None) -> dict:
        conditions = ["a.security_code=?", "d.text_layer_status IN ('ok', 'partial')"]
        params: list[object] = [normalize_code(code)]
        title_conditions = " OR ".join("a.title LIKE ?" for _ in FINANCIAL_REPORT_TITLE_TERMS)
        conditions.append(f"({title_conditions})")
        params.extend(f"%{term}%" for term in FINANCIAL_REPORT_TITLE_TERMS)
        if as_of:
            conditions.append("COALESCE(a.published_at, a.report_date) <= ?")
            params.append(as_of + " 23:59:59" if len(as_of) == 10 else as_of)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT d.document_id, d.sha256, a.title
                FROM announcement_document d
                JOIN announcement a ON a.current_document_id=d.document_id
                WHERE {' AND '.join(conditions)}
                ORDER BY COALESCE(a.published_at, a.report_date) DESC
                """,
                params,
            ).fetchall()
        results = []
        for row in rows:
            extraction = extract_financial_facts(
                document_id=row["document_id"],
                title=row["title"],
                sha256=row["sha256"],
                pages=self.document_pages(row["document_id"]),
            )
            results.append(self.save_financial_extraction(row["document_id"], extraction))
        return {
            "documents_seen": len(rows),
            "reports_identified": sum(1 for item in results if item["report_identified"]),
            "facts_extracted": sum(item["facts"] for item in results),
            "documents": results,
        }

    def financial_facts(self, code: str, as_of: str | None = None) -> list[dict]:
        conditions = ["r.security_code=?"]
        params: list[object] = [normalize_code(code)]
        if as_of:
            conditions.append("COALESCE(a.published_at, a.report_date) <= ?")
            params.append(as_of + " 23:59:59" if len(as_of) == 10 else as_of)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT f.*, r.report_type, r.fiscal_year, r.period_label,
                       a.title, a.published_at, a.report_date
                FROM financial_metric_fact f
                JOIN financial_report_period r ON r.document_id=f.document_id
                JOIN announcement_document d ON d.document_id=f.document_id
                JOIN announcement a ON a.announcement_id=d.announcement_id
                WHERE {' AND '.join(conditions)}
                ORDER BY f.period_end DESC,
                         CASE WHEN a.title LIKE '%摘要%' THEN 1 ELSE 0 END,
                         f.source_page ASC
                """,
                params,
            ).fetchall()
        if not rows:
            return []
        latest_period = rows[0]["period_end"]
        selected: dict[str, dict] = {}
        for row in rows:
            item = dict(row)
            if item["period_end"] != latest_period or item["metric_code"] in selected:
                continue
            selected[item["metric_code"]] = item
        return list(selected.values())

    def financial_fact_evidence(self, code: str, as_of: str | None = None) -> list[dict]:
        evidence = []
        for fact in self.financial_facts(code, as_of):
            evidence.append(
                {
                    "id": f"ev-fin-{fact['fact_id']}",
                    "category": "financial_fact",
                    "label": fact["metric_label"],
                    "value": format_fact_change(fact),
                    "as_of": fact["published_at"] or fact["report_date"],
                    "source": f"/api/documents/{fact['document_id']}/file#page={fact['source_page']}",
                    "method": (
                        "程序化同表同行双值抽取；Decimal 单位归一化与变化率计算；"
                        f"版本 {fact['extraction_version']}"
                    ),
                    "citation": {
                        "document_id": fact["document_id"],
                        "page_start": fact["source_page"],
                        "page_end": fact["source_page"],
                        "sha256": fact["document_sha256"],
                        "title": fact["title"],
                    },
                    "financial_fact": {
                        key: fact[key]
                        for key in (
                            "fact_id",
                            "metric_code",
                            "metric_label",
                            "statement_type",
                            "period_start",
                            "period_end",
                            "comparison_period_start",
                            "comparison_period_end",
                            "normalized_unit",
                            "current_value",
                            "comparison_value",
                            "absolute_change",
                            "change_pct",
                            "source_page",
                            "source_text",
                            "extraction_version",
                            "confidence",
                            "period_label",
                        )
                    },
                }
            )
        return evidence

    def save_interaction(
        self,
        *,
        code: str,
        interaction_type: str,
        question: str,
        scope: str,
        as_of: str,
        mode: str,
        status: str,
        result: dict,
        evidence: list[dict],
        meta: dict | None = None,
    ) -> dict:
        if interaction_type not in {"document_qa", "financial_change_template"}:
            raise ValueError("不支持的研究交互类型")
        interaction_id = str(uuid.uuid4())
        created_at = utc_now()
        evidence_json = json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        snapshot_hash = sha256_text(evidence_json)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO research_interaction (
                    interaction_id, security_code, interaction_type, question, scope,
                    as_of, mode, status, result_json, meta_json,
                    evidence_snapshot_hash, evidence_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    interaction_id,
                    normalize_code(code),
                    interaction_type,
                    question,
                    scope,
                    as_of,
                    mode,
                    status,
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    json.dumps(meta or {}, ensure_ascii=False, sort_keys=True),
                    snapshot_hash,
                    len(evidence),
                    created_at,
                ),
            )
            conn.executemany(
                """
                INSERT INTO research_interaction_evidence (
                    interaction_id, position, evidence_id, snapshot_json
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        interaction_id,
                        position,
                        item["id"],
                        json.dumps(item, ensure_ascii=False, sort_keys=True),
                    )
                    for position, item in enumerate(evidence)
                ],
            )
        return {
            "interaction_id": interaction_id,
            "evidence_snapshot_hash": snapshot_hash,
            "created_at": created_at,
        }

    def list_interactions(self, code: str, limit: int = 20) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT interaction_id, interaction_type, question, scope, as_of,
                       mode, status, evidence_snapshot_hash, evidence_count, created_at
                FROM research_interaction
                WHERE security_code=?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (normalize_code(code), max(1, min(limit, 100))),
            ).fetchall()
        return [dict(row) for row in rows]

    def interaction(self, interaction_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM research_interaction WHERE interaction_id=?",
                (interaction_id,),
            ).fetchone()
            if not row:
                return None
            snapshots = conn.execute(
                """
                SELECT snapshot_json FROM research_interaction_evidence
                WHERE interaction_id=? ORDER BY position
                """,
                (interaction_id,),
            ).fetchall()
        item = dict(row)
        item["result"] = json.loads(item.pop("result_json"))
        item["meta"] = json.loads(item.pop("meta_json"))
        item["evidence"] = [json.loads(snapshot["snapshot_json"]) for snapshot in snapshots]
        return item

    def document(self, document_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT d.*, a.security_code, a.title, a.published_at, a.report_date
                FROM announcement_document d
                JOIN announcement a ON a.announcement_id=d.announcement_id
                WHERE d.document_id=?
                """,
                (document_id,),
            ).fetchone()
        return dict(row) if row else None

    def resolve_document_path(self, document_id: str) -> Path | None:
        item = self.document(document_id)
        if not item:
            return None
        path = (self.document_root.parent / item["file_path"]).resolve()
        root = self.document_root.resolve()
        return path if path.is_relative_to(root) and path.exists() else None

    def start_sync_run(self, code: str, start_date: str, end_date: str) -> str:
        run_id = str(uuid.uuid4())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO announcement_sync_run (
                    run_id, security_code, started_at, start_date, end_date, status
                ) VALUES (?, ?, ?, ?, ?, 'running')
                """,
                (run_id, normalize_code(code), utc_now(), start_date, end_date),
            )
        return run_id

    def finish_sync_run(self, run_id: str, result: dict, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE announcement_sync_run
                SET finished_at=?, status=?, metadata_received=?, metadata_upserted=?,
                    documents_downloaded=?, documents_parsed=?, documents_skipped=?,
                    failed_documents=?, error=? WHERE run_id=?
                """,
                (
                    utc_now(),
                    "failed" if error else ("partial" if result.get("failed_documents") else "success"),
                    result.get("metadata_received", 0),
                    result.get("metadata_upserted", 0),
                    result.get("documents_downloaded", 0),
                    result.get("documents_parsed", 0),
                    result.get("documents_skipped", 0),
                    json.dumps(result.get("failed_documents", {}), ensure_ascii=False),
                    error[:500] if error else None,
                    run_id,
                ),
            )
