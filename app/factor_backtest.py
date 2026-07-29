from __future__ import annotations

import json
import math
import sqlite3
import statistics
import threading
from collections import defaultdict
from datetime import date
from typing import Any

from .data_access import StockRepository
from .factor_evaluation import FactorEvaluationService, MIN_CROSS_SECTION
from .factor_lab import FactorLabService, evaluate_template
from .migrations import apply_migration
from .primitives import canonical_hash
from .quant import source_fingerprint
from .research_store import ResearchStore, utc_now


FACTOR_BACKTEST_VERSION = "factor-backtest-v1.1"
BENCHMARK_CODE = "CSI300_CURRENT_EQUAL_WEIGHT"
BENCHMARK_NAME = "当前沪深300等权代理"
FACTOR_BACKTEST_TABLES = (
    "factor_backtest_run",
    "factor_backtest_nav",
    "factor_backtest_rebalance",
    "factor_backtest_trade",
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _decode_run(row: sqlite3.Row | dict | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    item["metrics"] = json.loads(item.pop("metrics_json"))
    item["limitations"] = json.loads(item.pop("limitations_json"))
    return item


class FactorBacktestService:
    def __init__(
        self,
        store: ResearchStore,
        repository: StockRepository,
        factor_lab: FactorLabService,
        factor_evaluation: FactorEvaluationService,
    ):
        self.store = store
        self.repository = repository
        self.factor_lab = factor_lab
        self.factor_evaluation = factor_evaluation
        self._lock = threading.Lock()
        apply_migration(store.connect, "0011_factor_backtest", self._create_schema)
        if hasattr(store, "migrate_legacy"):
            store.migrate_legacy(
                "0020_split_factor_backtest_database", FACTOR_BACKTEST_TABLES
            )

    def _create_schema(self) -> None:
        with self.store.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS factor_backtest_run (
                    backtest_id TEXT PRIMARY KEY,
                    input_hash TEXT NOT NULL UNIQUE,
                    backtest_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    evaluation_id TEXT NOT NULL,
                    factor_id TEXT NOT NULL,
                    factor_version INTEGER NOT NULL,
                    requested_start_date TEXT NOT NULL,
                    requested_end_date TEXT NOT NULL,
                    effective_start_date TEXT,
                    effective_end_date TEXT,
                    universe TEXT NOT NULL,
                    adjustment TEXT NOT NULL,
                    source_db TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL,
                    source_contract_hash TEXT NOT NULL,
                    benchmark_code TEXT NOT NULL,
                    benchmark_name TEXT NOT NULL,
                    top_n INTEGER NOT NULL,
                    rebalance_step INTEGER NOT NULL,
                    initial_capital REAL NOT NULL,
                    commission_rate REAL NOT NULL,
                    stamp_duty_rate REAL NOT NULL,
                    slippage_rate REAL NOT NULL,
                    trading_day_count INTEGER NOT NULL DEFAULT 0,
                    rebalance_count INTEGER NOT NULL DEFAULT 0,
                    trade_count INTEGER NOT NULL DEFAULT 0,
                    result_hash TEXT,
                    metrics_json TEXT NOT NULL,
                    limitations_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    error TEXT,
                    FOREIGN KEY (evaluation_id) REFERENCES factor_evaluation_run(evaluation_id),
                    FOREIGN KEY (factor_id, factor_version)
                        REFERENCES factor_version(factor_id, version)
                );

                CREATE TABLE IF NOT EXISTS factor_backtest_nav (
                    backtest_id TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    nav REAL NOT NULL,
                    cash REAL NOT NULL,
                    holdings_value REAL NOT NULL,
                    daily_return REAL,
                    cumulative_return REAL NOT NULL,
                    drawdown REAL NOT NULL,
                    benchmark_nav REAL NOT NULL,
                    benchmark_daily_return REAL,
                    benchmark_cumulative_return REAL NOT NULL,
                    excess_cumulative_return REAL NOT NULL,
                    cash_weight REAL NOT NULL,
                    holdings_count INTEGER NOT NULL,
                    PRIMARY KEY (backtest_id, trading_date),
                    FOREIGN KEY (backtest_id) REFERENCES factor_backtest_run(backtest_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS factor_backtest_rebalance (
                    backtest_id TEXT NOT NULL,
                    signal_date TEXT NOT NULL,
                    execution_date TEXT NOT NULL,
                    target_count INTEGER NOT NULL,
                    executed_buy_count INTEGER NOT NULL,
                    executed_sell_count INTEGER NOT NULL,
                    blocked_buy_count INTEGER NOT NULL,
                    blocked_sell_count INTEGER NOT NULL,
                    turnover REAL NOT NULL,
                    explicit_cost REAL NOT NULL,
                    slippage_cost REAL NOT NULL,
                    selected_json TEXT NOT NULL,
                    PRIMARY KEY (backtest_id, signal_date),
                    FOREIGN KEY (backtest_id) REFERENCES factor_backtest_run(backtest_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS factor_backtest_trade (
                    trade_id TEXT PRIMARY KEY,
                    backtest_id TEXT NOT NULL,
                    signal_date TEXT NOT NULL,
                    execution_date TEXT NOT NULL,
                    security_code TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    market_price REAL NOT NULL,
                    execution_price REAL NOT NULL,
                    gross_notional REAL NOT NULL,
                    explicit_fee REAL NOT NULL,
                    slippage_cost REAL NOT NULL,
                    reason TEXT NOT NULL,
                    FOREIGN KEY (backtest_id) REFERENCES factor_backtest_run(backtest_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_factor_backtest_run_finished
                ON factor_backtest_run(status, finished_at DESC);
                CREATE INDEX IF NOT EXISTS idx_factor_backtest_trade_run
                ON factor_backtest_trade(backtest_id, execution_date);
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

    def run(
        self,
        *,
        evaluation_id: str,
        factor_id: str,
        start_date: str | None = None,
        end_date: str | None = None,
        top_n: int = 30,
        rebalance_step: int = 20,
        initial_capital: float = 1_000_000,
        commission_rate: float = 0.0003,
        stamp_duty_rate: float = 0.0005,
        slippage_rate: float = 0.001,
    ) -> dict:
        with self._lock:
            return self._run(
                evaluation_id=evaluation_id,
                factor_id=factor_id,
                start_date=start_date,
                end_date=end_date,
                top_n=top_n,
                rebalance_step=rebalance_step,
                initial_capital=initial_capital,
                commission_rate=commission_rate,
                stamp_duty_rate=stamp_duty_rate,
                slippage_rate=slippage_rate,
            )

    def _run(
        self,
        *,
        evaluation_id: str,
        factor_id: str,
        start_date: str | None,
        end_date: str | None,
        top_n: int,
        rebalance_step: int,
        initial_capital: float,
        commission_rate: float,
        stamp_duty_rate: float,
        slippage_rate: float,
    ) -> dict:
        evaluation = self.factor_evaluation.evaluation(evaluation_id)
        if evaluation is None or evaluation["run"]["status"] != "completed":
            raise ValueError("回测必须绑定一个已完成的正式因子评价")
        evaluation_run = evaluation["run"]
        factor_metrics = [
            item for item in evaluation["metrics"] if item["factor_id"] == factor_id
        ]
        if not factor_metrics:
            raise ValueError("所选因子不属于该评价合同")
        factor_version = int(factor_metrics[0]["factor_version"])
        factors = [
            item for item in self.factor_lab.evaluation_factors()
            if item["factor_id"] == factor_id and int(item["version"]) == factor_version
        ]
        if not factors:
            raise ValueError("所选因子版本已不可用于回测")
        factor = factors[0]
        resolved_start = start_date or evaluation_run["requested_start_date"]
        resolved_end = end_date or evaluation_run["requested_end_date"]
        date.fromisoformat(resolved_start)
        date.fromisoformat(resolved_end)
        if resolved_start >= resolved_end:
            raise ValueError("回测开始日期必须早于结束日期")
        if not 5 <= top_n <= 100:
            raise ValueError("Top-N 必须在 5 至 100 之间")
        if not 5 <= rebalance_step <= 120:
            raise ValueError("调仓间隔必须在 5 至 120 个交易日之间")
        if initial_capital <= 0:
            raise ValueError("初始资金必须为正数")
        for value, name in (
            (commission_rate, "佣金"),
            (stamp_duty_rate, "印花税"),
            (slippage_rate, "滑点"),
        ):
            if value < 0 or value > 0.02:
                raise ValueError(f"{name}费率必须在 0 至 2% 之间")

        fingerprint, source_contract_hash = self._source_contract()
        if source_contract_hash != evaluation_run["source_contract_hash"]:
            raise ValueError("前复权行情已变化，请先重新运行因子评价再回测")
        input_payload = {
            "backtest_version": FACTOR_BACKTEST_VERSION,
            "evaluation_id": evaluation_id,
            "factor_id": factor_id,
            "factor_version": factor_version,
            "start_date": resolved_start,
            "end_date": resolved_end,
            "source_contract_hash": source_contract_hash,
            "top_n": top_n,
            "rebalance_step": rebalance_step,
            "initial_capital": initial_capital,
            "commission_rate": commission_rate,
            "stamp_duty_rate": stamp_duty_rate,
            "slippage_rate": slippage_rate,
            "benchmark": BENCHMARK_CODE,
            "execution": "signal close, next calendar open",
        }
        input_hash = canonical_hash(input_payload)
        backtest_id = canonical_hash({"input": input_hash})
        with self.store.connect() as conn:
            existing = conn.execute(
                "SELECT status FROM factor_backtest_run WHERE input_hash=?", (input_hash,)
            ).fetchone()
        if existing and existing["status"] == "completed":
            return {**self.backtest(backtest_id), "reused": True}

        limitations = [
            "股票池是2026-07-26查询时点的当前沪深300，历史回测存在幸存者偏差。",
            "基准是当前沪深300成分的每日等权代理，不是官方沪深300指数。",
            "已模拟佣金、卖出印花税和固定滑点；未模拟最低佣金、100股整手和冲击成本。",
            "停牌或缺少有效开盘价时不成交；尚未按板块和历史规则完整识别涨跌停。",
            "回测只验证单因子Top-N组合，不自动发布因子、重训模型或进入模拟盘。",
        ]
        started_at = utc_now()
        with self.store.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO factor_backtest_run (
                    backtest_id, input_hash, backtest_version, status, evaluation_id,
                    factor_id, factor_version, requested_start_date, requested_end_date,
                    universe, adjustment, source_db, source_fingerprint,
                    source_contract_hash, benchmark_code, benchmark_name, top_n,
                    rebalance_step, initial_capital, commission_rate, stamp_duty_rate,
                    slippage_rate, metrics_json, limitations_json, started_at
                ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, '{}', ?, ?)
                """,
                (
                    backtest_id, input_hash, FACTOR_BACKTEST_VERSION, evaluation_id,
                    factor_id, factor_version, resolved_start, resolved_end,
                    evaluation_run["universe"], evaluation_run["adjustment"],
                    str(self.repository.db_path.resolve()), fingerprint,
                    source_contract_hash, BENCHMARK_CODE, BENCHMARK_NAME, top_n,
                    rebalance_step, initial_capital, commission_rate, stamp_duty_rate,
                    slippage_rate, _json(limitations), started_at,
                ),
            )
        try:
            result = self._compute(
                backtest_id=backtest_id,
                factor=factor,
                start_date=resolved_start,
                end_date=resolved_end,
                top_n=top_n,
                rebalance_step=rebalance_step,
                initial_capital=initial_capital,
                commission_rate=commission_rate,
                stamp_duty_rate=stamp_duty_rate,
                slippage_rate=slippage_rate,
            )
            with self.store.connect() as conn:
                conn.execute("DELETE FROM factor_backtest_nav WHERE backtest_id=?", (backtest_id,))
                conn.execute("DELETE FROM factor_backtest_rebalance WHERE backtest_id=?", (backtest_id,))
                conn.execute("DELETE FROM factor_backtest_trade WHERE backtest_id=?", (backtest_id,))
                conn.executemany(
                    "INSERT INTO factor_backtest_nav VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    result["nav_rows"],
                )
                conn.executemany(
                    "INSERT INTO factor_backtest_rebalance VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    result["rebalance_rows"],
                )
                conn.executemany(
                    "INSERT INTO factor_backtest_trade VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    result["trade_rows"],
                )
                conn.execute(
                    """
                    UPDATE factor_backtest_run SET status='completed', effective_start_date=?,
                        effective_end_date=?, trading_day_count=?, rebalance_count=?,
                        trade_count=?, result_hash=?, metrics_json=?, finished_at=?, error=NULL
                    WHERE backtest_id=?
                    """,
                    (
                        result["effective_start_date"], result["effective_end_date"],
                        len(result["nav_rows"]), len(result["rebalance_rows"]),
                        len(result["trade_rows"]), result["result_hash"],
                        _json(result["metrics"]), utc_now(), backtest_id,
                    ),
                )
        except Exception as exc:
            with self.store.connect() as conn:
                conn.execute(
                    "UPDATE factor_backtest_run SET status='failed', error=?, finished_at=? "
                    "WHERE backtest_id=?", (str(exc), utc_now(), backtest_id),
                )
            raise
        return {**self.backtest(backtest_id), "reused": False}

    @staticmethod
    def _annualized_return(total_return: float, periods: int) -> float | None:
        if periods < 2 or total_return <= -1:
            return None
        return (1 + total_return) ** (252 / (periods - 1)) - 1

    @staticmethod
    def _annualized_ratio(values: list[float]) -> float | None:
        if len(values) < 2:
            return None
        deviation = statistics.stdev(values)
        return statistics.fmean(values) / deviation * math.sqrt(252) if deviation else None

    def _compute(
        self,
        *,
        backtest_id: str,
        factor: dict,
        start_date: str,
        end_date: str,
        top_n: int,
        rebalance_step: int,
        initial_capital: float,
        commission_rate: float,
        stamp_duty_rate: float,
        slippage_rate: float,
    ) -> dict:
        histories: dict[str, dict] = {}
        calendar: set[str] = set()
        for code, rows in self.repository.iter_histories(end=end_date, limit=3000):
            if not rows:
                continue
            by_date = {row["time"]: row for row in rows}
            histories[code] = {
                "rows": rows,
                "by_date": by_date,
                "position": {row["time"]: index for index, row in enumerate(rows)},
            }
            calendar.update(by_date)
        trading_dates = [value for value in sorted(calendar) if start_date <= value <= end_date]
        if len(trading_dates) < rebalance_step + 2:
            raise ValueError("回测区间不足以形成两次净值观察")

        schedules: dict[str, dict] = {}
        lookback = int(factor["lookback"])
        signal_dates = trading_dates[:-1:rebalance_step]
        for signal_date in signal_dates:
            records: list[tuple[str, float]] = []
            for code, history in histories.items():
                position = history["position"].get(signal_date)
                if position is None:
                    continue
                tail = history["rows"][max(0, position - lookback - 1): position + 1]
                raw_value = evaluate_template(factor["template_id"], lookback, tail)
                if raw_value is None or not math.isfinite(raw_value):
                    continue
                directional = raw_value if factor["direction"] == "positive" else -raw_value
                records.append((code, directional))
            if len(records) < max(MIN_CROSS_SECTION, top_n):
                continue
            records.sort(key=lambda item: (-item[1], item[0]))
            execution_index = trading_dates.index(signal_date) + 1
            schedules[trading_dates[execution_index]] = {
                "signal_date": signal_date,
                "members": [code for code, _ in records[:top_n]],
            }
        if not schedules:
            raise ValueError("没有形成满足最小横截面数量的可执行调仓")

        cash = initial_capital
        positions: dict[str, float] = {}
        last_close: dict[str, float] = {}
        previous_benchmark_marks: dict[str, float] = {}
        benchmark_nav = initial_capital
        previous_nav: float | None = None
        nav_peak = initial_capital
        nav_rows: list[tuple] = []
        rebalance_rows: list[tuple] = []
        trade_rows: list[tuple] = []
        total_explicit_cost = 0.0
        total_slippage_cost = 0.0
        total_turnover = 0.0

        for trading_date in trading_dates:
            rows_today = {
                code: history["by_date"][trading_date]
                for code, history in histories.items()
                if trading_date in history["by_date"]
            }
            schedule = schedules.get(trading_date)
            if schedule:
                marks_at_open = {
                    code: (
                        _finite(rows_today.get(code, {}).get("open"))
                        or last_close.get(code)
                    )
                    for code in positions
                }
                equity_at_open = cash + sum(
                    quantity * (marks_at_open.get(code) or 0)
                    for code, quantity in positions.items()
                )
                members = schedule["members"]
                desired_value = equity_at_open / len(members)
                buys = sells = blocked_buys = blocked_sells = 0
                rebalance_notional = explicit_cost = slippage_cost = 0.0

                for code in sorted(list(positions)):
                    row = rows_today.get(code)
                    market_price = _finite(row.get("open")) if row else None
                    volume = _finite(row.get("volume")) if row else None
                    tradable = market_price is not None and market_price > 0 and volume is not None and volume > 0
                    current_value = positions[code] * (market_price or last_close.get(code, 0))
                    target_value = desired_value if code in members else 0.0
                    if current_value <= target_value + 1e-8:
                        continue
                    if not tradable:
                        blocked_sells += 1
                        continue
                    quantity = min(positions[code], (current_value - target_value) / market_price)
                    execution_price = market_price * (1 - slippage_rate)
                    gross = quantity * execution_price
                    fee = gross * (commission_rate + stamp_duty_rate)
                    slip = quantity * market_price * slippage_rate
                    cash += gross - fee
                    positions[code] -= quantity
                    if positions[code] <= 1e-10:
                        positions.pop(code, None)
                    sells += 1
                    rebalance_notional += quantity * market_price
                    explicit_cost += fee
                    slippage_cost += slip
                    trade_rows.append((
                        canonical_hash({"backtest_id": backtest_id, "signal": schedule["signal_date"], "code": code, "side": "sell"}),
                        backtest_id, schedule["signal_date"], trading_date, code, "sell",
                        quantity, market_price, execution_price, gross, fee, slip,
                        "target_rebalance",
                    ))

                for code in members:
                    row = rows_today.get(code)
                    market_price = _finite(row.get("open")) if row else None
                    volume = _finite(row.get("volume")) if row else None
                    if market_price is None or market_price <= 0 or volume is None or volume <= 0:
                        current_value = positions.get(code, 0.0) * last_close.get(code, 0.0)
                        if current_value < desired_value - 1e-8:
                            blocked_buys += 1
                        continue
                    current_value = positions.get(code, 0.0) * market_price
                    required = desired_value - current_value
                    if required <= 1e-8:
                        continue
                    execution_price = market_price * (1 + slippage_rate)
                    affordable = cash / (1 + commission_rate)
                    gross = min(required * (1 + slippage_rate), affordable)
                    if gross <= 1e-8:
                        continue
                    quantity = gross / execution_price
                    fee = gross * commission_rate
                    slip = quantity * market_price * slippage_rate
                    cash -= gross + fee
                    positions[code] = positions.get(code, 0.0) + quantity
                    buys += 1
                    rebalance_notional += quantity * market_price
                    explicit_cost += fee
                    slippage_cost += slip
                    trade_rows.append((
                        canonical_hash({"backtest_id": backtest_id, "signal": schedule["signal_date"], "code": code, "side": "buy"}),
                        backtest_id, schedule["signal_date"], trading_date, code, "buy",
                        quantity, market_price, execution_price, gross, fee, slip,
                        "top_n_entry_or_resize",
                    ))

                turnover = rebalance_notional / equity_at_open if equity_at_open > 0 else 0.0
                total_turnover += turnover
                total_explicit_cost += explicit_cost
                total_slippage_cost += slippage_cost
                rebalance_rows.append((
                    backtest_id, schedule["signal_date"], trading_date, len(members),
                    buys, sells, blocked_buys, blocked_sells, turnover,
                    explicit_cost, slippage_cost, _json(members),
                ))

            for code, row in rows_today.items():
                close = _finite(row.get("close"))
                if close is not None and close > 0:
                    last_close[code] = close
            holdings_value = sum(
                quantity * last_close.get(code, 0) for code, quantity in positions.items()
            )
            nav = cash + holdings_value
            daily_return = nav / previous_nav - 1 if previous_nav else None
            cumulative_return = nav / initial_capital - 1
            nav_peak = max(nav_peak, nav)
            drawdown = nav / nav_peak - 1 if nav_peak else 0.0

            current_benchmark_marks = {
                code: last_close[code] for code in histories if code in last_close
            }
            benchmark_returns = [
                current_benchmark_marks[code] / previous_benchmark_marks[code] - 1
                for code in current_benchmark_marks.keys() & previous_benchmark_marks.keys()
                if previous_benchmark_marks[code] > 0
            ]
            benchmark_daily = statistics.fmean(benchmark_returns) if previous_nav and benchmark_returns else None
            if benchmark_daily is not None:
                benchmark_nav *= 1 + benchmark_daily
            benchmark_cumulative = benchmark_nav / initial_capital - 1
            excess_cumulative = (
                (1 + cumulative_return) / (1 + benchmark_cumulative) - 1
                if benchmark_cumulative > -1 else 0.0
            )
            nav_rows.append((
                backtest_id, trading_date, nav, cash, holdings_value, daily_return,
                cumulative_return, drawdown, benchmark_nav, benchmark_daily,
                benchmark_cumulative, excess_cumulative, cash / nav if nav else 0.0,
                len(positions),
            ))
            previous_nav = nav
            previous_benchmark_marks = current_benchmark_marks

        if len(nav_rows) < 2:
            raise ValueError("没有形成足够的逐日净值")
        daily_returns = [row[5] for row in nav_rows if row[5] is not None]
        paired_excess = [
            row[5] - row[9] for row in nav_rows
            if row[5] is not None and row[9] is not None
        ]
        strategy_return = nav_rows[-1][6]
        benchmark_return = nav_rows[-1][10]
        daily_volatility = (
            statistics.stdev(daily_returns) * math.sqrt(252)
            if len(daily_returns) > 1 else None
        )
        tracking_error = (
            statistics.stdev(paired_excess) * math.sqrt(252)
            if len(paired_excess) > 1 else None
        )
        metrics = {
            "cumulative_return": strategy_return,
            "annualized_return": self._annualized_return(strategy_return, len(nav_rows)),
            "annualized_volatility": daily_volatility,
            "sharpe_ratio": self._annualized_ratio(daily_returns),
            "max_drawdown": min(row[7] for row in nav_rows),
            "benchmark_cumulative_return": benchmark_return,
            "benchmark_annualized_return": self._annualized_return(benchmark_return, len(nav_rows)),
            "excess_cumulative_return": (1 + strategy_return) / (1 + benchmark_return) - 1,
            "tracking_error": tracking_error,
            "information_ratio": self._annualized_ratio(paired_excess),
            "excess_positive_day_ratio": (
                sum(value > 0 for value in paired_excess) / len(paired_excess)
                if paired_excess else None
            ),
            "average_turnover": total_turnover / len(rebalance_rows) if rebalance_rows else 0.0,
            "total_turnover": total_turnover,
            "explicit_cost": total_explicit_cost,
            "slippage_cost": total_slippage_cost,
            "total_cost": total_explicit_cost + total_slippage_cost,
            "total_cost_rate": (total_explicit_cost + total_slippage_cost) / initial_capital,
            "final_nav": nav_rows[-1][2],
        }
        result_hash = canonical_hash(
            {"nav": nav_rows, "rebalances": rebalance_rows, "trades": trade_rows, "metrics": metrics}
        )
        return {
            "effective_start_date": nav_rows[0][1],
            "effective_end_date": nav_rows[-1][1],
            "nav_rows": nav_rows,
            "rebalance_rows": rebalance_rows,
            "trade_rows": trade_rows,
            "metrics": metrics,
            "result_hash": result_hash,
        }

    def backtest(self, backtest_id: str) -> dict | None:
        with self.store.connect() as conn:
            run = conn.execute(
                "SELECT * FROM factor_backtest_run WHERE backtest_id=?", (backtest_id,)
            ).fetchone()
            if run is None:
                return None
            nav = [dict(row) for row in conn.execute(
                "SELECT * FROM factor_backtest_nav WHERE backtest_id=? ORDER BY trading_date",
                (backtest_id,),
            )]
            rebalances = [dict(row) for row in conn.execute(
                "SELECT * FROM factor_backtest_rebalance WHERE backtest_id=? ORDER BY execution_date",
                (backtest_id,),
            )]
        for item in rebalances:
            item["selected"] = json.loads(item.pop("selected_json"))
        return {"run": _decode_run(run), "nav": nav, "rebalances": rebalances}

    def latest(self) -> dict | None:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT backtest_id FROM factor_backtest_run "
                "WHERE status='completed' ORDER BY finished_at DESC LIMIT 1"
            ).fetchone()
        return self.backtest(row[0]) if row else None
