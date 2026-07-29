from __future__ import annotations

import json
import math
import re
import statistics
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from .data_access import StockRepository
from .migrations import apply_migration
from .primitives import (
    annualized_volatility,
    canonical_hash,
    maximum_drawdown,
    period_return,
)
from .quant import source_fingerprint
from .research_store import ResearchStore, utc_now


FACTOR_LAB_SCHEMA_VERSION = "factor-lab-v1"
FACTOR_LAB_TABLES = (
    "factor_formula_template",
    "factor_definition",
    "factor_version",
    "factor_release_review",
    "factor_set",
    "factor_set_member",
    "model_factor_binding",
    "factor_lab_snapshot",
    "factor_lab_value",
)
FACTOR_LIFECYCLE = ("draft", "testing", "shadow", "approved", "deprecated")
FACTOR_TRANSITIONS = {
    "draft": {"testing", "deprecated"},
    "testing": {"shadow", "deprecated"},
    "shadow": {"approved", "testing", "deprecated"},
    "approved": {"deprecated"},
    "deprecated": set(),
}


@dataclass(frozen=True)
class FormulaTemplate:
    template_id: str
    name: str
    category: str
    description: str
    expression_template: str
    required_fields: tuple[str, ...]
    default_window: int
    min_window: int
    max_window: int
    direction: str

    def render(self, window: int) -> str:
        return self.expression_template.format(window=window)


FORMULA_TEMPLATES = {
    item.template_id: item
    for item in (
        FormulaTemplate(
            "alpha158_bundle", "Qlib Alpha158 特征包", "model_bundle",
            "用于登记现有模型的外部特征合同，不由实验室公式引擎计算。",
            "qlib:Alpha158", (), 1, 1, 1, "positive",
        ),
        FormulaTemplate(
            "momentum", "区间动量", "momentum", "观察过去一段时间的累计涨跌幅。",
            "close / Ref(close, {window}) - 1", ("close",), 20, 2, 250, "positive",
        ),
        FormulaTemplate(
            "reversal", "短期反转", "reversal", "把短期涨跌幅取反，观察均值回归。",
            "-(close / Ref(close, {window}) - 1)", ("close",), 5, 2, 60, "positive",
        ),
        FormulaTemplate(
            "volatility", "历史波动率", "risk", "衡量日收益的年化波动，通常越低越稳。",
            "Std(Return(close), {window}) * Sqrt(252)", ("close",), 60, 10, 250, "negative",
        ),
        FormulaTemplate(
            "volume_ratio", "成交量比率", "liquidity", "最新成交量相对过去均量的倍数。",
            "volume / Mean(volume, {window})", ("volume",), 20, 5, 120, "positive",
        ),
        FormulaTemplate(
            "turnover_mean", "平均成交额", "liquidity", "过去一段时间的平均成交金额。",
            "Mean(volume * vwap, {window})", ("volume", "vwap"), 20, 5, 120, "positive",
        ),
        FormulaTemplate(
            "range_position", "价格区间位置", "momentum", "当前价格在历史最高与最低之间的位置。",
            "(close - Min(close, {window})) / (Max(close, {window}) - Min(close, {window}))",
            ("close",), 250, 20, 500, "positive",
        ),
        FormulaTemplate(
            "max_drawdown", "最大回撤", "risk", "窗口内从高点到低点的最大跌幅。",
            "MaxDrawdown(close, {window})", ("close",), 250, 20, 500, "positive",
        ),
        FormulaTemplate(
            "ma_gap", "均线偏离", "momentum", "当前价格相对移动均线的偏离。",
            "close / Mean(close, {window}) - 1", ("close",), 20, 5, 250, "positive",
        ),
    )
}


BUILTIN_FACTORS = (
    ("momentum_20d", "20日动量", "momentum", 20, "过去20个交易日涨幅，越高代表近期动量越强。"),
    ("momentum_60d", "60日动量", "momentum", 60, "过去60个交易日涨幅，用于观察中期趋势。"),
    ("reversal_5d", "5日反转", "reversal", 5, "短期涨幅取反，用于观察过度上涨或下跌后的回归。"),
    ("volatility_20d", "20日波动率", "volatility", 20, "短期年化波动率，越低通常代表价格更稳定。"),
    ("volatility_60d", "60日波动率", "volatility", 60, "中期年化波动率，用于识别高风险股票。"),
    ("volume_ratio_20d", "20日量比", "volume_ratio", 20, "当前成交量相对20日平均成交量。"),
    ("turnover_mean_20d", "20日平均成交额", "turnover_mean", 20, "衡量股票近期可交易容量。"),
    ("range_position_250d", "250日价格位置", "range_position", 250, "当前价格在约一年价格区间中的位置。"),
    ("max_drawdown_250d", "250日最大回撤", "max_drawdown", 250, "过去约一年最严重的峰谷跌幅。"),
    ("ma_gap_20d", "20日均线偏离", "ma_gap", 20, "当前价格相对20日均线的距离。"),
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decoded(row: dict | Any | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    for key in ("parameters_json", "required_fields_json", "limitations_json"):
        if key in item:
            item[key.removesuffix("_json")] = json.loads(item.pop(key) or "[]")
    return item


def _finite_series(rows: list[dict], field: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        value = row.get(field)
        try:
            number = float(value)
        except (TypeError, ValueError):
            return []
        if not math.isfinite(number) or number < 0 or (field != "volume" and number <= 0):
            return []
        values.append(number)
    return values


def evaluate_template(template_id: str, window: int, rows: list[dict]) -> float | None:
    template = FORMULA_TEMPLATES[template_id]
    if len(rows) < window + (1 if template_id in {"momentum", "reversal", "volatility"} else 0):
        return None
    closes = _finite_series(rows, "close")
    if not closes:
        return None
    value: float | None
    if template_id == "momentum":
        value = period_return(closes, window)
    elif template_id == "reversal":
        result = period_return(closes, window)
        value = -result if result is not None else None
    elif template_id == "volatility":
        value = annualized_volatility(closes, window)
    elif template_id == "volume_ratio":
        volumes = _finite_series(rows[-window:], "volume")
        mean = statistics.fmean(volumes) if volumes else 0
        value = volumes[-1] / mean if mean else None
    elif template_id == "turnover_mean":
        recent = rows[-window:]
        volumes = _finite_series(recent, "volume")
        vwaps = _finite_series(recent, "vwap")
        value = statistics.fmean(v * p for v, p in zip(volumes, vwaps)) if volumes and vwaps else None
    elif template_id == "range_position":
        recent = closes[-window:]
        low, high = min(recent), max(recent)
        value = 0.5 if high == low else (recent[-1] - low) / (high - low)
    elif template_id == "max_drawdown":
        value = maximum_drawdown(closes, window)
    elif template_id == "ma_gap":
        recent = closes[-window:]
        mean = statistics.fmean(recent)
        value = recent[-1] / mean - 1 if mean else None
    else:  # pragma: no cover - registry and evaluator are changed together
        raise ValueError(f"不支持的公式模板：{template_id}")
    return value if value is not None and math.isfinite(value) else None


class FactorLabService:
    def __init__(
        self,
        store: ResearchStore,
        repository: StockRepository,
        *,
        adjustment: str = "unadjusted",
        universe: str = "A-share local coverage",
    ):
        self.store = store
        self.repository = repository
        self.adjustment = adjustment
        self.universe = universe
        apply_migration(store.connect, "0008_factor_lab", self._create_schema)
        if hasattr(store, "migrate_legacy"):
            store.migrate_legacy("0018_split_factor_lab_database", FACTOR_LAB_TABLES)
        self._seed_catalog()

    def _create_schema(self) -> None:
        with self.store.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS factor_formula_template (
                    template_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    description TEXT NOT NULL,
                    expression_template TEXT NOT NULL,
                    required_fields_json TEXT NOT NULL,
                    default_window INTEGER NOT NULL,
                    min_window INTEGER NOT NULL,
                    max_window INTEGER NOT NULL,
                    default_direction TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS factor_definition (
                    factor_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    description TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS factor_version (
                    factor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    template_id TEXT NOT NULL,
                    expression TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    required_fields_json TEXT NOT NULL,
                    lookback INTEGER NOT NULL,
                    direction TEXT NOT NULL,
                    adjustment TEXT NOT NULL,
                    universe TEXT NOT NULL,
                    availability_rule TEXT NOT NULL,
                    missing_policy TEXT NOT NULL,
                    lifecycle_status TEXT NOT NULL,
                    formula_hash TEXT NOT NULL,
                    limitations_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status_changed_at TEXT NOT NULL,
                    PRIMARY KEY (factor_id, version),
                    UNIQUE (formula_hash),
                    FOREIGN KEY (factor_id) REFERENCES factor_definition(factor_id),
                    FOREIGN KEY (template_id) REFERENCES factor_formula_template(template_id)
                );

                CREATE INDEX IF NOT EXISTS idx_factor_version_status
                ON factor_version(lifecycle_status, factor_id);

                CREATE TABLE IF NOT EXISTS factor_release_review (
                    review_id TEXT PRIMARY KEY,
                    factor_id TEXT NOT NULL,
                    factor_version INTEGER NOT NULL,
                    from_status TEXT NOT NULL,
                    to_status TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (factor_id, factor_version)
                        REFERENCES factor_version(factor_id, version)
                );

                CREATE TABLE IF NOT EXISTS factor_set (
                    factor_set_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    set_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (factor_set_id, version)
                );

                CREATE TABLE IF NOT EXISTS factor_set_member (
                    factor_set_id TEXT NOT NULL,
                    factor_set_version INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    factor_id TEXT NOT NULL,
                    factor_version INTEGER NOT NULL,
                    weight REAL,
                    PRIMARY KEY (factor_set_id, factor_set_version, position),
                    UNIQUE (factor_set_id, factor_set_version, factor_id, factor_version),
                    FOREIGN KEY (factor_set_id, factor_set_version)
                        REFERENCES factor_set(factor_set_id, version),
                    FOREIGN KEY (factor_id, factor_version)
                        REFERENCES factor_version(factor_id, version)
                );

                CREATE TABLE IF NOT EXISTS model_factor_binding (
                    model_run_id TEXT PRIMARY KEY,
                    factor_set_id TEXT NOT NULL,
                    factor_set_version INTEGER NOT NULL,
                    binding_status TEXT NOT NULL,
                    bound_at TEXT NOT NULL,
                    FOREIGN KEY (model_run_id) REFERENCES model_run(model_run_id),
                    FOREIGN KEY (factor_set_id, factor_set_version)
                        REFERENCES factor_set(factor_set_id, version)
                );

                CREATE TABLE IF NOT EXISTS factor_lab_snapshot (
                    snapshot_id TEXT PRIMARY KEY,
                    as_of TEXT NOT NULL,
                    universe TEXT NOT NULL,
                    adjustment TEXT NOT NULL,
                    factor_contract_hash TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    factor_count INTEGER NOT NULL DEFAULT 0,
                    security_count INTEGER NOT NULL DEFAULT 0,
                    value_count INTEGER NOT NULL DEFAULT 0,
                    coverage REAL NOT NULL DEFAULT 0,
                    snapshot_hash TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    error TEXT,
                    UNIQUE(as_of, factor_contract_hash, source_fingerprint)
                );

                CREATE TABLE IF NOT EXISTS factor_lab_value (
                    snapshot_id TEXT NOT NULL,
                    factor_id TEXT NOT NULL,
                    factor_version INTEGER NOT NULL,
                    security_code TEXT NOT NULL,
                    raw_value REAL,
                    quality_status TEXT NOT NULL,
                    quality_reason TEXT,
                    cross_section_rank INTEGER,
                    cross_section_size INTEGER,
                    percentile REAL,
                    PRIMARY KEY (snapshot_id, factor_id, factor_version, security_code),
                    FOREIGN KEY (snapshot_id) REFERENCES factor_lab_snapshot(snapshot_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (factor_id, factor_version)
                        REFERENCES factor_version(factor_id, version),
                    FOREIGN KEY (security_code) REFERENCES security_master(security_code)
                );

                CREATE INDEX IF NOT EXISTS idx_factor_lab_value_rank
                ON factor_lab_value(snapshot_id, factor_id, factor_version, cross_section_rank);
                """
            )

    def _seed_catalog(self) -> None:
        now = utc_now()
        with self.store.connect() as conn:
            for template in FORMULA_TEMPLATES.values():
                conn.execute(
                    """
                    INSERT OR IGNORE INTO factor_formula_template (
                        template_id, name, category, description, expression_template,
                        required_fields_json, default_window, min_window, max_window,
                        default_direction, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        template.template_id, template.name, template.category,
                        template.description, template.expression_template,
                        _json(template.required_fields), template.default_window,
                        template.min_window, template.max_window, template.direction, now,
                    ),
                )

            self._insert_factor(
                conn, "alpha158_bundle", "Qlib Alpha158", "model_bundle",
                "当前 LightGBM 训练时使用的固定 Alpha158 特征集合。",
                "system", "alpha158_bundle", 1, "qlib:Alpha158", {}, (), 1,
                "positive", "CPS:2", "CSI300", "T日收盘后", "由Qlib处理",
                "approved", ["作为整体登记，M11.1不拆分内部158个表达式。"], now,
            )
            alpha_hash = canonical_hash({"members": [["alpha158_bundle", 1]]})
            conn.execute(
                """
                INSERT OR IGNORE INTO factor_set (
                    factor_set_id, version, name, status, set_hash, created_at
                ) VALUES ('alpha158_set', 1, 'Alpha158 当前基线', 'approved', ?, ?)
                """,
                (alpha_hash, now),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO factor_set_member (
                    factor_set_id, factor_set_version, position, factor_id, factor_version, weight
                ) VALUES ('alpha158_set', 1, 1, 'alpha158_bundle', 1, NULL)
                """
            )

            for factor_id, name, template_id, window, description in BUILTIN_FACTORS:
                template = FORMULA_TEMPLATES[template_id]
                self._insert_factor(
                    conn, factor_id, name, template.category, description, "system",
                    template_id, 1, template.render(window), {"window": window},
                    template.required_fields, window, template.direction, "unadjusted",
                    "A-share local coverage", "T日收盘后", "历史不足则缺失",
                    "testing", ["当前使用不复权行情，正式发布前必须完成公司行为处理验证。"], now,
                )
                if self.adjustment != "unadjusted" and template_id != "turnover_mean":
                    self._insert_factor(
                        conn, factor_id, name, template.category, description, "system",
                        template_id, 2, template.render(window), {"window": window},
                        template.required_fields, window, template.direction,
                        self.adjustment, self.universe, "T日收盘后", "历史不足则缺失",
                        "testing",
                        [
                            "股票池是查询时点的当前沪深300，历史评价存在幸存者偏差。",
                            "评价结果只用于因子研究，不自动进入模型、候选池或交易。",
                        ],
                        now,
                    )
            latest_models = conn.execute(
                "SELECT model_run_id FROM model_run ORDER BY imported_at DESC"
            ).fetchall()
            for model in latest_models:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO model_factor_binding (
                        model_run_id, factor_set_id, factor_set_version, binding_status, bound_at
                    ) VALUES (?, 'alpha158_set', 1, 'frozen', ?)
                    """,
                    (model["model_run_id"], now),
                )

    @staticmethod
    def _insert_factor(
        conn, factor_id: str, name: str, category: str, description: str, owner: str,
        template_id: str, version: int, expression: str, parameters: dict,
        required_fields: tuple[str, ...], lookback: int, direction: str,
        adjustment: str, universe: str, availability_rule: str, missing_policy: str,
        status: str, limitations: list[str], now: str,
    ) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO factor_definition (
                factor_id, name, category, description, owner, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (factor_id, name, category, description, owner, now),
        )
        payload = {
            "factor_id": factor_id, "version": version, "template_id": template_id,
            "expression": expression, "parameters": parameters,
            "required_fields": required_fields, "lookback": lookback,
            "direction": direction, "adjustment": adjustment, "universe": universe,
            "availability_rule": availability_rule, "missing_policy": missing_policy,
        }
        conn.execute(
            """
            INSERT OR IGNORE INTO factor_version (
                factor_id, version, template_id, expression, parameters_json,
                required_fields_json, lookback, direction, adjustment, universe,
                availability_rule, missing_policy, lifecycle_status, formula_hash,
                limitations_json, created_at, status_changed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                factor_id, version, template_id, expression, _json(parameters),
                _json(required_fields), lookback, direction, adjustment, universe,
                availability_rule, missing_policy, status, canonical_hash(payload),
                _json(limitations), now, now,
            ),
        )

    def templates(self) -> list[dict]:
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM factor_formula_template ORDER BY category, template_id"
            ).fetchall()
        return [_decoded(row) for row in rows]

    def list_factors(self, status: str | None = None) -> list[dict]:
        params: list[Any] = []
        where = ""
        if status:
            if status not in FACTOR_LIFECYCLE:
                raise ValueError("未知因子状态")
            where = "WHERE v.lifecycle_status=?"
            params.append(status)
        with self.store.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT d.*, v.*, CASE WHEN b.model_run_id IS NULL THEN 0 ELSE 1 END AS model_used
                FROM factor_definition d
                JOIN factor_version v ON v.factor_id=d.factor_id
                JOIN (
                    SELECT factor_id, MAX(version) AS version FROM factor_version GROUP BY factor_id
                ) latest ON latest.factor_id=v.factor_id AND latest.version=v.version
                LEFT JOIN factor_set_member m
                    ON m.factor_id=v.factor_id AND m.factor_version=v.version
                LEFT JOIN model_factor_binding b
                    ON b.factor_set_id=m.factor_set_id
                    AND b.factor_set_version=m.factor_set_version
                {where}
                GROUP BY v.factor_id, v.version
                ORDER BY CASE v.lifecycle_status
                    WHEN 'approved' THEN 1 WHEN 'shadow' THEN 2 WHEN 'testing' THEN 3
                    WHEN 'draft' THEN 4 ELSE 5 END, d.category, d.factor_id
                """,
                params,
            ).fetchall()
        return [_decoded(row) for row in rows]

    def create_factor(
        self, *, factor_id: str, name: str, description: str, template_id: str,
        window: int, direction: str | None, owner: str,
    ) -> dict:
        factor_id = factor_id.strip().lower()
        if not re.fullmatch(r"[a-z0-9_]+", factor_id):
            raise ValueError("因子ID只能包含小写字母、数字和下划线")
        template = FORMULA_TEMPLATES.get(template_id)
        if template is None:
            raise ValueError("未知公式模板")
        if self.adjustment != "unadjusted" and template_id == "turnover_mean":
            raise ValueError("前复权VWAP乘真实成交量不等于历史成交额，暂不允许创建该因子")
        if not template.min_window <= window <= template.max_window:
            raise ValueError(f"窗口必须在 {template.min_window} 至 {template.max_window} 之间")
        resolved_direction = direction or template.direction
        if resolved_direction not in {"positive", "negative"}:
            raise ValueError("方向必须为 positive 或 negative")
        now = utc_now()
        payload = {
            "factor_id": factor_id, "version": 1, "template_id": template_id,
            "expression": template.render(window), "parameters": {"window": window},
            "required_fields": template.required_fields, "lookback": window,
            "direction": resolved_direction, "adjustment": self.adjustment,
            "universe": self.universe, "availability_rule": "T日收盘后",
            "missing_policy": "历史不足则缺失",
        }
        with self.store.connect() as conn:
            latest = conn.execute(
                "SELECT MAX(version) FROM factor_version WHERE factor_id=?", (factor_id,)
            ).fetchone()[0]
            version = int(latest or 0) + 1
            payload["version"] = version
            duplicate = conn.execute(
                """
                SELECT factor_id, version FROM factor_version
                WHERE factor_id=? AND template_id=? AND expression=?
                    AND parameters_json=? AND direction=? AND adjustment=?
                    AND universe=? AND availability_rule=? AND missing_policy=?
                """,
                (
                    factor_id, template_id, payload["expression"],
                    _json(payload["parameters"]), resolved_direction, self.adjustment,
                    self.universe, "T日收盘后", "历史不足则缺失",
                ),
            ).fetchone()
            if duplicate:
                raise ValueError(
                    f"公式与 {duplicate['factor_id']}@v{duplicate['version']} 完全相同，无需重复登记"
                )
            self._insert_factor(
                conn, factor_id, name.strip(), template.category, description.strip(), owner.strip(),
                template_id, version, template.render(window), {"window": window},
                template.required_fields, window, resolved_direction, self.adjustment,
                self.universe, "T日收盘后", "历史不足则缺失", "draft",
                ["评价结果只用于因子研究，不自动进入模型、候选池或交易。"], now,
            )
        return next(item for item in self.list_factors() if item["factor_id"] == factor_id)

    def change_status(
        self, factor_id: str, version: int, to_status: str, reviewer: str, note: str
    ) -> dict:
        if to_status not in FACTOR_LIFECYCLE:
            raise ValueError("未知目标状态")
        now = utc_now()
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT lifecycle_status FROM factor_version WHERE factor_id=? AND version=?",
                (factor_id, version),
            ).fetchone()
            if row is None:
                raise KeyError("因子版本不存在")
            current = row["lifecycle_status"]
            if to_status not in FACTOR_TRANSITIONS[current]:
                raise ValueError(f"不允许从 {current} 直接变更为 {to_status}")
            conn.execute(
                """
                UPDATE factor_version SET lifecycle_status=?, status_changed_at=?
                WHERE factor_id=? AND version=?
                """,
                (to_status, now, factor_id, version),
            )
            conn.execute(
                """
                INSERT INTO factor_release_review (
                    review_id, factor_id, factor_version, from_status, to_status,
                    reviewer, note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), factor_id, version, current, to_status, reviewer, note, now),
            )
        return next(item for item in self.list_factors() if item["factor_id"] == factor_id)

    def _snapshot_factors(self) -> list[dict]:
        return [
            item for item in self.list_factors()
            if item["lifecycle_status"] in {"testing", "shadow", "approved"}
            and item["template_id"] in FORMULA_TEMPLATES
            and item["template_id"] != "alpha158_bundle"
            and item["adjustment"] == self.adjustment
            and item["universe"] == self.universe
        ]

    def evaluation_factors(self) -> list[dict]:
        return self._snapshot_factors()

    def generate_snapshot(self, as_of: str) -> dict:
        date.fromisoformat(as_of)
        factors = self._snapshot_factors()
        if not factors:
            raise ValueError("没有可计算的测试中、Shadow或已批准因子")
        fingerprint, _, _ = source_fingerprint(
            self.repository.db_path, self.repository.security_count
        )
        contract = [
            {"factor_id": item["factor_id"], "version": item["version"], "hash": item["formula_hash"]}
            for item in factors
        ]
        contract_hash = canonical_hash(contract)
        with self.store.connect() as conn:
            existing = conn.execute(
                """
                SELECT * FROM factor_lab_snapshot
                WHERE as_of=? AND factor_contract_hash=? AND source_fingerprint=?
                """,
                (as_of, contract_hash, fingerprint),
            ).fetchone()
        if existing and existing["status"] == "completed":
            return {**dict(existing), "reused": True}

        snapshot_id = canonical_hash(
            {"schema": FACTOR_LAB_SCHEMA_VERSION, "as_of": as_of,
             "factor_contract_hash": contract_hash, "source_fingerprint": fingerprint}
        )
        now = utc_now()
        with self.store.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO factor_lab_snapshot (
                    snapshot_id, as_of, universe, adjustment, factor_contract_hash,
                    source_fingerprint, status, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    snapshot_id, as_of, self.universe, self.adjustment,
                    contract_hash, fingerprint, now,
                ),
            )
        try:
            values: list[dict] = []
            max_lookback = max(int(item["lookback"]) for item in factors) + 1
            security_count = 0
            for security_code, rows in self.repository.iter_histories(
                end=as_of, limit=max(30, max_lookback)
            ):
                security_count += 1
                for factor in factors:
                    raw = evaluate_template(
                        factor["template_id"], int(factor["lookback"]), rows
                    )
                    values.append(
                        {
                            "factor_id": factor["factor_id"], "factor_version": factor["version"],
                            "security_code": security_code, "raw_value": raw,
                            "quality_status": "valid" if raw is not None else "missing",
                            "quality_reason": None if raw is not None else "insufficient_or_invalid_input",
                            "direction": factor["direction"],
                        }
                    )
            for factor in factors:
                group = [
                    item for item in values
                    if item["factor_id"] == factor["factor_id"] and item["raw_value"] is not None
                ]
                group.sort(
                    key=lambda item: item["raw_value"],
                    reverse=factor["direction"] == "positive",
                )
                size = len(group)
                for rank, item in enumerate(group, 1):
                    item["cross_section_rank"] = rank
                    item["cross_section_size"] = size
                    item["percentile"] = round(100 * (size - rank) / max(1, size - 1), 4)
            finished = utc_now()
            snapshot_hash = canonical_hash(
                {
                    "contract": contract, "as_of": as_of,
                    "values": [
                        [item["factor_id"], item["factor_version"], item["security_code"], item["raw_value"]]
                        for item in values
                    ],
                }
            )
            valid_count = sum(item["raw_value"] is not None for item in values)
            with self.store.connect() as conn:
                conn.execute("DELETE FROM factor_lab_value WHERE snapshot_id=?", (snapshot_id,))
                conn.executemany(
                    """
                    INSERT INTO factor_lab_value (
                        snapshot_id, factor_id, factor_version, security_code, raw_value,
                        quality_status, quality_reason, cross_section_rank,
                        cross_section_size, percentile
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            snapshot_id, item["factor_id"], item["factor_version"],
                            item["security_code"], item["raw_value"], item["quality_status"],
                            item["quality_reason"], item.get("cross_section_rank"),
                            item.get("cross_section_size"), item.get("percentile"),
                        )
                        for item in values
                    ],
                )
                conn.execute(
                    """
                    UPDATE factor_lab_snapshot SET status='completed', factor_count=?,
                        security_count=?, value_count=?, coverage=?, snapshot_hash=?, finished_at=?
                    WHERE snapshot_id=?
                    """,
                    (
                        len(factors), security_count, valid_count,
                        valid_count / max(1, len(values)), snapshot_hash, finished, snapshot_id,
                    ),
                )
        except Exception as exc:
            with self.store.connect() as conn:
                conn.execute(
                    """
                    UPDATE factor_lab_snapshot SET status='failed', error=?, finished_at=?
                    WHERE snapshot_id=?
                    """,
                    (str(exc), utc_now(), snapshot_id),
                )
            raise
        return {**self.snapshot(snapshot_id), "reused": False}

    def snapshot(self, snapshot_id: str) -> dict | None:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM factor_lab_snapshot WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
        return dict(row) if row else None

    def latest_snapshot(self) -> dict | None:
        with self.store.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM factor_lab_snapshot WHERE status='completed'
                    AND adjustment=? AND universe=?
                ORDER BY as_of DESC, finished_at DESC LIMIT 1
                """,
                (self.adjustment, self.universe),
            ).fetchone()
        return dict(row) if row else None

    def snapshot_values(
        self, snapshot_id: str, factor_id: str, limit: int = 100
    ) -> dict:
        with self.store.connect() as conn:
            snapshot = conn.execute(
                "SELECT * FROM factor_lab_snapshot WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            if snapshot is None:
                raise KeyError("实验室快照不存在")
            rows = conn.execute(
                """
                SELECT v.*, s.security_name
                FROM factor_lab_value v
                LEFT JOIN security_master s ON s.security_code=v.security_code
                WHERE v.snapshot_id=? AND v.factor_id=? AND v.quality_status='valid'
                ORDER BY v.cross_section_rank LIMIT ?
                """,
                (snapshot_id, factor_id, limit),
            ).fetchall()
            total = conn.execute(
                """
                SELECT COUNT(*) FROM factor_lab_value
                WHERE snapshot_id=? AND factor_id=? AND quality_status='valid'
                """,
                (snapshot_id, factor_id),
            ).fetchone()[0]
        return {"snapshot": dict(snapshot), "items": [dict(row) for row in rows], "total": total}

    def overview(self) -> dict:
        factors = self.list_factors()
        counts = {status: 0 for status in FACTOR_LIFECYCLE}
        for factor in factors:
            counts[factor["lifecycle_status"]] += 1
        with self.store.connect() as conn:
            binding = conn.execute(
                """
                SELECT b.*, m.model_class, m.feature_set, m.imported_at, f.name AS factor_set_name
                FROM model_factor_binding b
                JOIN model_run m ON m.model_run_id=b.model_run_id
                JOIN factor_set f ON f.factor_set_id=b.factor_set_id
                    AND f.version=b.factor_set_version
                ORDER BY m.imported_at DESC LIMIT 1
                """
            ).fetchone()
        return {
            "schema_version": FACTOR_LAB_SCHEMA_VERSION,
            "factor_count": len(factors),
            "status_counts": counts,
            "template_count": len(FORMULA_TEMPLATES),
            "latest_snapshot": self.latest_snapshot(),
            "current_model_binding": dict(binding) if binding else None,
            "data_contract": {
                "source": str(self.repository.db_path),
                "adjustment": self.adjustment,
                "universe": self.universe,
                "availability": "T日收盘后",
            },
        }
