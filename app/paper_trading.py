from __future__ import annotations

import json
import math
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from .data_access import StockRepository, normalize_code
from .migrations import apply_migration
from .primitives import canonical_hash
from .portfolio_decision import (
    COMMISSION_RATE,
    DEFAULT_MAX_INDUSTRY_WEIGHT,
    DEFAULT_MAX_PAIR_CORRELATION,
    DEFAULT_MAX_POSITION_WEIGHT,
    DEFAULT_TARGET_GROSS_EXPOSURE,
    MIN_COMMISSION,
    SELL_STAMP_DUTY_RATE,
    SLIPPAGE_RATE,
    STRATEGY_VERSION,
    PortfolioDecisionService,
)
from .research_store import ResearchStore, utc_now


PAPER_BENCHMARKS = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000300.SH": "沪深300",
}


class _PaperExecutionCore:
    def __init__(self, repository: StockRepository, store: ResearchStore, quote_provider: Any | None = None):
        self.repository = repository
        self.store = store
        self.quote_provider = quote_provider
        self._initialize()

    def _initialize(self) -> None:
        apply_migration(self.store.connect, "0003_paper_trading", self._create_schema)
        apply_migration(self.store.connect, "0007_paper_benchmarks", self._create_benchmark_schema)
        self._recover_run_statuses()

    def _create_benchmark_schema(self) -> None:
        with self.store.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_benchmark_price (
                    account_id TEXT NOT NULL,
                    benchmark_code TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    close REAL NOT NULL,
                    source TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, benchmark_code, trading_date),
                    FOREIGN KEY (account_id) REFERENCES paper_account(account_id)
                );
                CREATE INDEX IF NOT EXISTS idx_paper_benchmark_account_date
                ON paper_benchmark_price(account_id, trading_date, benchmark_code);
                """
            )

    def _create_schema(self) -> None:
        with self.store.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_account (
                    account_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    initial_cash REAL NOT NULL,
                    cash REAL NOT NULL,
                    benchmark_code TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_daily_run (
                    run_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    factor_snapshot_id TEXT NOT NULL,
                    shadow_snapshot_id TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    market_summary_json TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    UNIQUE(account_id, as_of, strategy_version),
                    FOREIGN KEY (account_id) REFERENCES paper_account(account_id)
                );
                CREATE TABLE IF NOT EXISTS paper_order (
                    order_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    security_code TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    reference_price REAL NOT NULL,
                    target_weight REAL NOT NULL,
                    reason_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reviewer TEXT,
                    review_note TEXT,
                    reviewed_at TEXT,
                    fill_date TEXT,
                    fill_price REAL,
                    gross_amount REAL,
                    fees REAL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES paper_daily_run(run_id),
                    FOREIGN KEY (account_id) REFERENCES paper_account(account_id)
                );
                CREATE INDEX IF NOT EXISTS idx_paper_order_account_status
                ON paper_order(account_id, status, created_at DESC);
                CREATE TABLE IF NOT EXISTS paper_position (
                    account_id TEXT NOT NULL,
                    security_code TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    available_quantity INTEGER NOT NULL,
                    average_cost REAL NOT NULL,
                    realized_pnl REAL NOT NULL,
                    last_buy_date TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, security_code),
                    FOREIGN KEY (account_id) REFERENCES paper_account(account_id)
                );
                CREATE TABLE IF NOT EXISTS paper_nav_snapshot (
                    account_id TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    cash REAL NOT NULL,
                    market_value REAL NOT NULL,
                    nav REAL NOT NULL,
                    daily_return REAL,
                    cumulative_return REAL NOT NULL,
                    benchmark_return REAL,
                    excess_return REAL,
                    drawdown REAL NOT NULL,
                    gross_exposure REAL NOT NULL,
                    turnover REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, trading_date),
                    FOREIGN KEY (account_id) REFERENCES paper_account(account_id)
                );
                CREATE TABLE IF NOT EXISTS paper_realtime_quote (
                    snapshot_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    security_code TEXT NOT NULL,
                    quote_time TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    open REAL NOT NULL,
                    latest REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    volume REAL NOT NULL,
                    amount REAL NOT NULL,
                    previous_close REAL NOT NULL,
                    source TEXT NOT NULL,
                    purpose_json TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL UNIQUE,
                    raw_json TEXT NOT NULL,
                    FOREIGN KEY (account_id) REFERENCES paper_account(account_id)
                );
                CREATE INDEX IF NOT EXISTS idx_paper_realtime_quote_account_code_time
                ON paper_realtime_quote(account_id, security_code, quote_time DESC);
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(paper_order)")}
            if "execution_quote_id" not in columns:
                conn.execute("ALTER TABLE paper_order ADD COLUMN execution_quote_id TEXT")
            conn.execute(
                """UPDATE paper_daily_run SET status=CASE
                    WHEN EXISTS (SELECT 1 FROM paper_order o WHERE o.run_id=paper_daily_run.run_id AND o.status='proposed')
                        THEN 'awaiting_review'
                    WHEN EXISTS (SELECT 1 FROM paper_order o WHERE o.run_id=paper_daily_run.run_id AND o.status='approved')
                        THEN 'approved_waiting_execution'
                    ELSE 'completed' END
                WHERE status<>'superseded'"""
            )

    def _recover_run_statuses(self) -> None:
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE paper_daily_run SET status=CASE
                    WHEN EXISTS (SELECT 1 FROM paper_order o WHERE o.run_id=paper_daily_run.run_id AND o.status='proposed')
                        THEN 'awaiting_review'
                    WHEN EXISTS (SELECT 1 FROM paper_order o WHERE o.run_id=paper_daily_run.run_id AND o.status='approved')
                        THEN 'approved_waiting_execution'
                    ELSE 'completed' END
                WHERE status<>'superseded'"""
            )

    @staticmethod
    def _decode_json(item: dict, *keys: str) -> dict:
        for key in keys:
            item[key] = json.loads(item.pop(f"{key}_json"))
        return item

    def create_account(self, name: str, initial_cash: float, benchmark_code: str) -> dict:
        now = utc_now()
        account_id = str(uuid.uuid4())
        with self.store.connect() as conn:
            conn.execute(
                """INSERT INTO paper_account
                (account_id, name, initial_cash, cash, benchmark_code, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'active', ?, ?)""",
                (account_id, name, initial_cash, initial_cash, benchmark_code, now, now),
            )
        return self.account(account_id)

    def default_account(self) -> dict:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM paper_account WHERE status='active' ORDER BY created_at LIMIT 1"
            ).fetchone()
        return dict(row) if row else self.create_account("每日模拟组合", 1_000_000, "000300.SH")

    def account(self, account_id: str) -> dict:
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM paper_account WHERE account_id=?", (account_id,)).fetchone()
        if row is None:
            raise KeyError("模拟账户不存在")
        return dict(row)

    def benchmark_sync_range(
        self, account_id: str | None = None, as_of: str | None = None
    ) -> dict:
        account = self.account(account_id) if account_id else self.default_account()
        end = as_of or self.store.market_data_end()
        if end is None:
            raise ValueError("本地行情库没有可用截止日")
        date.fromisoformat(end)
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT MIN(fill_date) FROM paper_order
                   WHERE account_id=? AND status='filled' AND fill_date<=?""",
                (account["account_id"], end),
            ).fetchone()
        start = (
            (date.fromisoformat(row[0]) - timedelta(days=10)).isoformat()
            if row[0] else end
        )
        return {
            "account_id": account["account_id"],
            "start_date": start,
            "end_date": end,
            "benchmark_codes": list(PAPER_BENCHMARKS),
        }

    def save_benchmark_prices(
        self,
        account_id: str,
        rows: list[dict],
        *,
        source: str = "iFinD THS_HD",
    ) -> dict:
        self.account(account_id)
        normalized: list[tuple] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            code = normalize_code(str(row.get("thscode") or ""))
            if code not in PAPER_BENCHMARKS:
                raise ValueError(f"不支持的模拟盘比较基准：{code}")
            trading_date = str(row.get("time") or "")[:10]
            date.fromisoformat(trading_date)
            close = float(row.get("close"))
            if not math.isfinite(close) or close <= 0:
                raise ValueError(f"指数 {code} 在 {trading_date} 的收盘价无效")
            key = (code, trading_date)
            if key in seen:
                raise ValueError(f"指数基准包含重复日期：{code} {trading_date}")
            seen.add(key)
            payload = {
                "account_id": account_id,
                "benchmark_code": code,
                "trading_date": trading_date,
                "close": close,
                "source": source,
            }
            normalized.append(
                (account_id, code, trading_date, close, source, canonical_hash(payload), utc_now())
            )
        with self.store.connect() as conn:
            conn.executemany(
                """INSERT INTO paper_benchmark_price
                   (account_id, benchmark_code, trading_date, close, source, snapshot_hash, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(account_id, benchmark_code, trading_date) DO UPDATE SET
                   close=excluded.close, source=excluded.source,
                   snapshot_hash=excluded.snapshot_hash, fetched_at=excluded.fetched_at""",
                normalized,
            )
        return {
            "account_id": account_id,
            "rows_written": len(normalized),
            "benchmark_count": len({item[1] for item in normalized}),
            "source": source,
        }

    def benchmark_comparison(
        self,
        account: dict,
        nav_rows: list[dict],
        as_of: str,
    ) -> dict:
        with self.store.connect() as conn:
            first_fill_row = conn.execute(
                """SELECT MIN(fill_date) FROM paper_order
                   WHERE account_id=? AND status='filled' AND fill_date<=?""",
                (account["account_id"], as_of),
            ).fetchone()
        inception = first_fill_row[0] if first_fill_row else None
        if inception is None:
            return {
                "status": "not_available",
                "as_of": as_of,
                "inception_date": None,
                "portfolio": None,
                "benchmarks": [],
                "limitations": ["尚无已成交订单，累计收益对比将在首笔模拟成交后开始。"],
            }
        eligible_nav = [
            row for row in nav_rows
            if inception <= row["trading_date"] <= as_of
        ]
        if not eligible_nav:
            return {
                "status": "not_available",
                "as_of": as_of,
                "inception_date": inception,
                "portfolio": None,
                "benchmarks": [],
                "limitations": [f"首笔成交日 {inception} 尚无模拟组合日终净值快照。"],
            }
        if eligible_nav[0]["trading_date"] != inception:
            return {
                "status": "not_available",
                "as_of": as_of,
                "inception_date": inception,
                "portfolio": None,
                "benchmarks": [],
                "limitations": [f"缺少首笔成交日 {inception} 的组合日终净值，不能后移比较起点。"],
            }
        dates = [row["trading_date"] for row in eligible_nav]
        prior_nav = [row for row in nav_rows if row["trading_date"] < inception]
        baseline_nav = float(prior_nav[-1]["nav"]) if prior_nav else float(account["initial_cash"])
        baseline_nav_date = prior_nav[-1]["trading_date"] if prior_nav else None
        portfolio_points = [
            {
                "date": row["trading_date"],
                "cumulative_return": float(row["nav"]) / baseline_nav - 1,
            }
            for row in eligible_nav
        ]
        portfolio_latest = portfolio_points[-1]["cumulative_return"]
        with self.store.connect() as conn:
            price_rows = [dict(row) for row in conn.execute(
                """SELECT benchmark_code, trading_date, close, source, snapshot_hash
                   FROM paper_benchmark_price
                   WHERE account_id=? AND trading_date<=?
                   ORDER BY benchmark_code, trading_date""",
                (account["account_id"], as_of),
            ).fetchall()]
        by_code: dict[str, dict[str, dict]] = {code: {} for code in PAPER_BENCHMARKS}
        for row in price_rows:
            if row["benchmark_code"] in by_code:
                by_code[row["benchmark_code"]][row["trading_date"]] = row
        benchmarks = []
        missing: list[str] = []
        for code, name in PAPER_BENCHMARKS.items():
            prices = by_code[code]
            prior_dates = [trading_date for trading_date in prices if trading_date < inception]
            baseline = prices[max(prior_dates)] if prior_dates else None
            if baseline is None:
                missing.append(f"{name}缺少收益期起点 {inception} 之前的最近收盘价")
                benchmarks.append({
                    "code": code, "name": name, "status": "missing_baseline",
                    "latest_return": None, "excess_return": None, "coverage": 0.0,
                    "points": [], "source": None,
                })
                continue
            points = []
            for trading_date in dates:
                price = prices.get(trading_date)
                if price is None:
                    continue
                points.append({
                    "date": trading_date,
                    "cumulative_return": float(price["close"]) / float(baseline["close"]) - 1,
                })
            latest = points[-1]["cumulative_return"] if points else None
            coverage = len(points) / len(dates) if dates else 0.0
            if coverage < 1:
                missing.append(f"{name}仅覆盖 {len(points)}/{len(dates)} 个组合净值日")
            benchmarks.append({
                "code": code,
                "name": name,
                "status": "complete" if coverage == 1 else "partial",
                "latest_return": latest,
                "excess_return": portfolio_latest - latest if latest is not None else None,
                "coverage": coverage,
                "points": points,
                "source": baseline["source"],
            })
        return {
            "status": "complete" if not missing else "partial",
            "as_of": eligible_nav[-1]["trading_date"],
            "inception_date": inception,
            "portfolio": {
                "name": account["name"],
                "latest_return": portfolio_latest,
                "points": portfolio_points,
                "baseline_nav_date": baseline_nav_date,
            },
            "benchmarks": benchmarks,
            "limitations": missing + [
                "收益期从首笔成交日开始：组合使用成交前最近日终净值，指数使用该交易日前最近收盘价；不纳入盘中 THS_RQ 浮动。"
            ],
        }

    def position_codes(self, account_id: str | None = None) -> list[str]:
        account = self.account(account_id) if account_id else self.default_account()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT security_code FROM paper_position
                   WHERE account_id=? AND quantity>0 ORDER BY security_code""",
                (account["account_id"],),
            ).fetchall()
        return [row["security_code"] for row in rows]

    def _price(self, code: str, as_of: str, field: str = "close", *, strictly_after: str | None = None) -> tuple[str, float] | None:
        rows = self.repository.get_history(code, start=strictly_after, end=as_of, limit=3000)
        for row in reversed(rows):
            if strictly_after and row["time"] <= strictly_after:
                continue
            value = row.get(field)
            if value is not None and float(value) > 0:
                return row["time"], float(value)
        return None

    def _execution_quote(self, code: str, proposal_as_of: str, execution_date: str, side: str) -> tuple[tuple[str, float] | None, str | None]:
        try:
            rows = self.repository.get_history(code, start=proposal_as_of, end=execution_date, limit=3000)
        except KeyError:
            return None, "missing_local_price_table"
        previous_close = None
        last_reason = "no_later_trading_day"
        for row in rows:
            close = float(row["close"]) if row.get("close") is not None else None
            if row["time"] <= proposal_as_of:
                if close and close > 0:
                    previous_close = close
                continue
            try:
                open_price = float(row["open"])
                high = float(row["high"])
                low = float(row["low"])
                volume = float(row["volume"])
            except (TypeError, ValueError):
                last_reason = "suspended_or_incomplete_quote"
                if close and close > 0:
                    previous_close = close
                continue
            if min(open_price, high, low, close or 0) <= 0 or volume <= 0:
                last_reason = "suspended_or_incomplete_quote"
                if close and close > 0:
                    previous_close = close
                continue
            one_price = abs(high - low) <= max(abs(close or open_price), 1.0) * 1e-8
            if one_price and previous_close:
                if side == "buy" and open_price > previous_close:
                    last_reason = "one_price_limit_up"
                    previous_close = close
                    continue
                if side == "sell" and open_price < previous_close:
                    last_reason = "one_price_limit_down"
                    previous_close = close
                    continue
            return (row["time"], open_price), None
        return None, last_reason

    def _realtime_scope(self, account: dict) -> dict[str, list[str]]:
        scope: dict[str, list[str]] = {account["benchmark_code"]: ["benchmark"]}
        with self.store.connect() as conn:
            positions = conn.execute(
                "SELECT security_code FROM paper_position WHERE account_id=? AND quantity>0",
                (account["account_id"],),
            ).fetchall()
            pending = conn.execute(
                "SELECT DISTINCT security_code FROM paper_order WHERE account_id=? AND status='approved'",
                (account["account_id"],),
            ).fetchall()
        for row in positions:
            scope.setdefault(row["security_code"], []).append("position")
        for row in pending:
            scope.setdefault(row["security_code"], []).append("approved_order")
        return scope

    @staticmethod
    def _validate_realtime_quote(quote: dict) -> tuple[dict | None, list[str]]:
        issues = []
        quote_time = quote.get("quote_time")
        try:
            parsed_time = datetime.fromisoformat(str(quote_time))
        except (TypeError, ValueError):
            parsed_time = None
            issues.append("invalid_quote_time")
        numeric: dict[str, float | None] = {}
        for field in ("open", "latest", "high", "low", "volume", "amount", "previous_close"):
            try:
                value = float(quote.get(field))
                numeric[field] = value if math.isfinite(value) else None
            except (TypeError, ValueError):
                numeric[field] = None
        for field in ("open", "latest", "high", "low", "previous_close"):
            if numeric[field] is None or numeric[field] <= 0:
                issues.append(f"invalid_{field}")
        for field in ("volume", "amount"):
            if numeric[field] is None or numeric[field] < 0:
                issues.append(f"invalid_{field}")
        if numeric["high"] and numeric["low"] and numeric["high"] < numeric["low"]:
            issues.append("high_below_low")
        if issues:
            return None, issues
        normalized = {
            "security_code": normalize_code(quote["security_code"]),
            "quote_time": parsed_time.isoformat(sep=" "),
            "trading_date": parsed_time.date().isoformat(),
            **{field: numeric[field] for field in numeric},
            "source": "iFinD THS_RQ",
        }
        return normalized, []

    def refresh_realtime_quotes(self, account_id: str | None = None) -> dict:
        if self.quote_provider is None:
            raise RuntimeError("实时行情服务未配置")
        account = self.account(account_id) if account_id else self.default_account()
        scope = self._realtime_scope(account)
        received_at = utc_now()
        raw_quotes = self.quote_provider.get_realtime_quotes(list(scope))
        saved, invalid = [], []
        returned_codes = set()
        for raw_quote in raw_quotes:
            code = normalize_code(raw_quote.get("security_code", ""))
            returned_codes.add(code)
            quote, issues = self._validate_realtime_quote(raw_quote)
            if quote is None:
                invalid.append({"security_code": code, "issues": issues})
                continue
            purposes = sorted(set(scope.get(code, ["unknown"])))
            payload = {**quote, "account_id": account["account_id"], "purpose": purposes}
            snapshot_hash = canonical_hash(payload, compact=False)
            snapshot_id = snapshot_hash
            with self.store.connect() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO paper_realtime_quote
                    (snapshot_id, account_id, security_code, quote_time, trading_date, received_at,
                     open, latest, high, low, volume, amount, previous_close, source,
                     purpose_json, snapshot_hash, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (snapshot_id, account["account_id"], code, quote["quote_time"], quote["trading_date"],
                     received_at, quote["open"], quote["latest"], quote["high"], quote["low"],
                     quote["volume"], quote["amount"], quote["previous_close"], quote["source"],
                     json.dumps(purposes, ensure_ascii=False), snapshot_hash,
                     json.dumps(raw_quote, ensure_ascii=False, sort_keys=True)),
                )
            saved.append({**quote, "snapshot_id": snapshot_id, "purpose": purposes})
        missing = sorted(set(scope) - returned_codes)
        return {
            "account_id": account["account_id"],
            "received_at": received_at,
            "requested_codes": list(scope),
            "quotes": saved,
            "missing_codes": missing,
            "invalid_quotes": invalid,
            "source": "iFinD THS_RQ",
        }

    def _latest_realtime_quotes(self, account_id: str, codes: list[str] | None = None) -> list[dict]:
        params: list[Any] = [account_id]
        code_filter = ""
        if codes:
            code_filter = f" AND q.security_code IN ({','.join('?' for _ in codes)})"
            params.extend(codes)
        with self.store.connect() as conn:
            rows = conn.execute(
                f"""SELECT q.* FROM paper_realtime_quote q
                WHERE q.account_id=? {code_filter}
                  AND q.received_at=(SELECT MAX(q2.received_at) FROM paper_realtime_quote q2
                                     WHERE q2.account_id=q.account_id AND q2.security_code=q.security_code)
                ORDER BY q.security_code""",
                params,
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["purpose"] = json.loads(item.pop("purpose_json"))
            item.pop("raw_json", None)
            items.append(item)
        return items

    def _positions(self, account_id: str, as_of: str, shadow_snapshot_id: str | None = None,
                   price_overrides: dict[str, dict] | None = None) -> list[dict]:
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT p.*, COALESCE(m.security_name, p.security_code) security_name,
                          m.industry_l1
                FROM paper_position p LEFT JOIN security_master m ON m.security_code=p.security_code
                WHERE p.account_id=? AND p.quantity>0 ORDER BY p.security_code""",
                (account_id,),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            realtime = (price_overrides or {}).get(item["security_code"])
            price = None if realtime else self._price(item["security_code"], as_of)
            close = realtime["latest"] if realtime else (price[1] if price else None)
            item["close"] = close
            item["price_source"] = realtime["source"] if realtime else "local_daily_close"
            item["quote_time"] = realtime["quote_time"] if realtime else (price[0] if price else None)
            item["market_value"] = close * item["quantity"] if close else None
            item["unrealized_pnl"] = (close - item["average_cost"]) * item["quantity"] if close else None
            signal = self.store.current_shadow_signal(shadow_snapshot_id, item["security_code"]) if shadow_snapshot_id else None
            item["shadow_rank"] = signal["cross_section_rank"] if signal else None
            items.append(item)
        return items

    def _mark_nav(self, account: dict, trading_date: str, turnover: float = 0.0,
                  price_overrides: dict[str, dict] | None = None) -> dict:
        positions = self._positions(account["account_id"], trading_date, price_overrides=price_overrides)
        market_value = sum(item["market_value"] or 0 for item in positions)
        nav = account["cash"] + market_value
        with self.store.connect() as conn:
            prior = conn.execute(
                "SELECT * FROM paper_nav_snapshot WHERE account_id=? AND trading_date<? ORDER BY trading_date DESC LIMIT 1",
                (account["account_id"], trading_date),
            ).fetchone()
            peak_row = conn.execute(
                "SELECT MAX(nav) FROM paper_nav_snapshot WHERE account_id=? AND trading_date<?",
                (account["account_id"], trading_date),
            ).fetchone()
            previous_nav = prior["nav"] if prior else account["initial_cash"]
            peak = max(float(peak_row[0] or account["initial_cash"]), nav)
            daily_return = nav / previous_nav - 1 if previous_nav else None
            cumulative_return = nav / account["initial_cash"] - 1
            drawdown = nav / peak - 1 if peak else 0.0
            conn.execute(
                """INSERT INTO paper_nav_snapshot
                (account_id, trading_date, cash, market_value, nav, daily_return,
                 cumulative_return, benchmark_return, excess_return, drawdown,
                 gross_exposure, turnover, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
                ON CONFLICT(account_id, trading_date) DO UPDATE SET
                cash=excluded.cash, market_value=excluded.market_value, nav=excluded.nav,
                daily_return=excluded.daily_return, cumulative_return=excluded.cumulative_return,
                drawdown=excluded.drawdown, gross_exposure=excluded.gross_exposure,
                turnover=paper_nav_snapshot.turnover + excluded.turnover,
                created_at=excluded.created_at""",
                (account["account_id"], trading_date, account["cash"], market_value, nav,
                 daily_return, cumulative_return, drawdown, market_value / nav if nav else 0,
                 turnover, utc_now()),
            )
        return {"trading_date": trading_date, "cash": account["cash"], "market_value": market_value,
                "nav": nav, "daily_return": daily_return, "cumulative_return": cumulative_return,
                "drawdown": drawdown, "gross_exposure": market_value / nav if nav else 0,
                "turnover": turnover, "benchmark_return": None, "excess_return": None}

    def _run_for_date(self, account_id: str, as_of: str) -> dict | None:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT run_id FROM paper_daily_run WHERE account_id=? AND as_of=? AND strategy_version=?",
                (account_id, as_of, STRATEGY_VERSION),
            ).fetchone()
        return self.run(row["run_id"]) if row else None

    def _supersede_prior_runs(self, account_id: str, as_of: str, active_run_id: str) -> None:
        now = utc_now()
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE paper_order SET status='cancelled',
                   review_note=COALESCE(review_note || '；', '') || ?,
                   reviewed_at=COALESCE(reviewed_at, ?)
                   WHERE account_id=? AND run_id<>? AND status IN ('proposed','approved')
                     AND fill_date IS NULL
                     AND run_id IN (SELECT run_id FROM paper_daily_run WHERE account_id=? AND as_of=?)""",
                (f'已由 {STRATEGY_VERSION} 同日批次替代', now, account_id, active_run_id, account_id, as_of),
            )
            conn.execute(
                """UPDATE paper_daily_run SET status='superseded'
                   WHERE account_id=? AND as_of=? AND run_id<>? AND status<>'completed'""",
                (account_id, as_of, active_run_id),
            )

    def run(self, run_id: str) -> dict:
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM paper_daily_run WHERE run_id=?", (run_id,)).fetchone()
            orders = conn.execute(
                """SELECT o.*, COALESCE(m.security_name, o.security_code) security_name
                FROM paper_order o LEFT JOIN security_master m ON m.security_code=o.security_code
                WHERE o.run_id=? ORDER BY CASE o.side WHEN 'sell' THEN 0 ELSE 1 END, o.created_at""", (run_id,)
            ).fetchall()
        if row is None:
            raise KeyError("模拟研究批次不存在")
        item = self._decode_json(dict(row), "config", "market_summary")
        item["orders"] = [self._decode_json(dict(order), "reason") for order in orders]
        return item

    def review_order(self, order_id: str, decision: str, reviewer: str, note: str) -> dict:
        status = "approved" if decision == "approve" else "rejected"
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM paper_order WHERE order_id=?", (order_id,)).fetchone()
            if row is None:
                raise KeyError("模拟订单不存在")
            if row["status"] != "proposed":
                raise ValueError("该订单已经完成审批")
            conn.execute(
                "UPDATE paper_order SET status=?, reviewer=?, review_note=?, reviewed_at=? WHERE order_id=?",
                (status, reviewer, note, utc_now(), order_id),
            )
        self._refresh_run_status(row["run_id"])
        return self.run(row["run_id"])

    def _refresh_run_status(self, run_id: str) -> None:
        with self.store.connect() as conn:
            run = conn.execute("SELECT status FROM paper_daily_run WHERE run_id=?", (run_id,)).fetchone()
            if run is None or run["status"] == "superseded":
                return
            statuses = [row[0] for row in conn.execute(
                "SELECT status FROM paper_order WHERE run_id=?", (run_id,)
            ).fetchall()]
            if "proposed" in statuses:
                status = "awaiting_review"
            elif "approved" in statuses:
                status = "approved_waiting_execution"
            else:
                status = "completed"
            conn.execute("UPDATE paper_daily_run SET status=? WHERE run_id=?", (status, run_id))

    def _fill_order(self, account_id: str, order: dict, fill_date: str, open_price: float,
                    execution_quote_id: str | None = None) -> tuple[dict | None, str | None]:
        fill_price = open_price * (1 + SLIPPAGE_RATE if order["side"] == "buy" else 1 - SLIPPAGE_RATE)
        quantity = int(order["quantity"])
        gross = fill_price * quantity
        fees = max(MIN_COMMISSION, gross * COMMISSION_RATE)
        if order["side"] == "sell":
            fees += gross * SELL_STAMP_DUTY_RATE
        with self.store.connect() as conn:
            current = conn.execute(
                "SELECT * FROM paper_position WHERE account_id=? AND security_code=?",
                (account_id, order["security_code"]),
            ).fetchone()
            fresh_account = conn.execute(
                "SELECT * FROM paper_account WHERE account_id=?", (account_id,)
            ).fetchone()
            if order["side"] == "buy":
                config_row = conn.execute(
                    "SELECT config_json FROM paper_daily_run WHERE run_id=?", (order["run_id"],)
                ).fetchone()
                config = json.loads(config_row["config_json"]) if config_row else {}
                max_positions = int(config.get("max_positions") or 0)
                is_new_position = current is None or int(current["quantity"] or 0) <= 0
                position_count = conn.execute(
                    "SELECT COUNT(*) FROM paper_position WHERE account_id=? AND quantity>0",
                    (account_id,),
                ).fetchone()[0]
                if is_new_position and max_positions and position_count >= max_positions:
                    return None, "持仓数量上限尚未通过卖出释放"
                total = gross + fees
                if fresh_account["cash"] + 1e-8 < total:
                    return None, "可用现金不足"
                old_qty = current["quantity"] if current else 0
                old_cost = current["average_cost"] if current else 0
                new_qty = old_qty + quantity
                avg_cost = (old_qty * old_cost + total) / new_qty
                conn.execute(
                    "UPDATE paper_account SET cash=cash-?, updated_at=? WHERE account_id=?",
                    (total, utc_now(), account_id),
                )
                conn.execute(
                    """INSERT INTO paper_position
                    (account_id, security_code, quantity, available_quantity, average_cost,
                     realized_pnl, last_buy_date, updated_at)
                    VALUES (?, ?, ?, 0, ?, 0, ?, ?)
                    ON CONFLICT(account_id, security_code) DO UPDATE SET
                    quantity=?, average_cost=?, last_buy_date=?, updated_at=?""",
                    (account_id, order["security_code"], quantity, avg_cost, fill_date, utc_now(),
                     new_qty, avg_cost, fill_date, utc_now()),
                )
            else:
                if current is None or current["available_quantity"] < quantity:
                    return None, "可卖数量不足"
                proceeds = gross - fees
                realized = (fill_price - current["average_cost"]) * quantity - fees
                conn.execute(
                    "UPDATE paper_account SET cash=cash+?, updated_at=? WHERE account_id=?",
                    (proceeds, utc_now(), account_id),
                )
                conn.execute(
                    """UPDATE paper_position SET quantity=quantity-?,
                    available_quantity=available_quantity-?, realized_pnl=realized_pnl+?,
                    updated_at=? WHERE account_id=? AND security_code=?""",
                    (quantity, quantity, realized, utc_now(), account_id, order["security_code"]),
                )
            conn.execute(
                """UPDATE paper_order SET status='filled', fill_date=?, fill_price=?,
                gross_amount=?, fees=?, execution_quote_id=? WHERE order_id=?""",
                (fill_date, fill_price, gross, fees, execution_quote_id, order["order_id"]),
            )
        self._refresh_run_status(order["run_id"])
        return {
            "order_id": order["order_id"], "security_code": order["security_code"],
            "side": order["side"], "quantity": quantity, "fill_date": fill_date,
            "fill_price": fill_price, "gross_amount": gross, "fees": fees,
            "execution_quote_id": execution_quote_id,
        }, None

    def settle(self, *, execution_date: str, account_id: str | None = None) -> dict:
        date.fromisoformat(execution_date)
        account = self.account(account_id) if account_id else self.default_account()
        filled, skipped, turnover = [], [], 0.0
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE paper_position SET available_quantity=quantity, updated_at=? WHERE account_id=? AND COALESCE(last_buy_date, '')<?",
                (utc_now(), account["account_id"], execution_date),
            )
            rows = conn.execute(
                """SELECT o.*, r.as_of FROM paper_order o JOIN paper_daily_run r ON r.run_id=o.run_id
                WHERE o.account_id=? AND o.status='approved' AND r.as_of<?
                ORDER BY CASE o.side WHEN 'sell' THEN 0 ELSE 1 END, o.created_at""",
                (account["account_id"], execution_date),
            ).fetchall()
        for raw in rows:
            order = dict(raw)
            price_item, execution_reason = self._execution_quote(
                order["security_code"], order["as_of"], execution_date, order["side"]
            )
            if not price_item:
                skipped.append({"order_id": order["order_id"], "reason": execution_reason})
                continue
            fill_date, open_price = price_item
            fill, reason = self._fill_order(account["account_id"], order, fill_date, open_price)
            if reason:
                skipped.append({"order_id": order["order_id"], "reason": reason})
                continue
            turnover += fill["gross_amount"]
            filled.append(fill)
        account = self.account(account["account_id"])
        nav = self._mark_nav(account, execution_date, turnover)
        return {"account": account, "filled": filled, "skipped": skipped, "nav": nav}

    def settle_realtime(self, account_id: str | None = None) -> dict:
        refresh = self.refresh_realtime_quotes(account_id)
        account = self.account(refresh["account_id"])
        quotes = {item["security_code"]: item for item in refresh["quotes"]}
        trading_dates = [quote["trading_date"] for quote in quotes.values()]
        execution_date = max(trading_dates) if trading_dates else date.today().isoformat()
        filled, skipped, turnover = [], [], 0.0
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE paper_position SET available_quantity=quantity, updated_at=? "
                "WHERE account_id=? AND COALESCE(last_buy_date, '')<?",
                (utc_now(), account["account_id"], execution_date),
            )
            rows = conn.execute(
                """SELECT o.*, r.as_of FROM paper_order o
                JOIN paper_daily_run r ON r.run_id=o.run_id
                WHERE o.account_id=? AND o.status='approved'
                ORDER BY CASE o.side WHEN 'sell' THEN 0 ELSE 1 END, o.created_at""",
                (account["account_id"],),
            ).fetchall()
        for raw in rows:
            order = dict(raw)
            quote = quotes.get(order["security_code"])
            if not quote:
                skipped.append({"order_id": order["order_id"], "reason": "missing_realtime_quote"})
                continue
            fill_date = quote["trading_date"]
            if fill_date <= order["as_of"]:
                skipped.append({"order_id": order["order_id"], "reason": "no_later_trading_day"})
                continue
            market_open = datetime.fromisoformat(f"{fill_date}T09:30:00+08:00")
            reviewed_at = datetime.fromisoformat(order["reviewed_at"])
            if reviewed_at >= market_open:
                skipped.append({"order_id": order["order_id"], "reason": "approved_after_market_open"})
                continue
            if quote["volume"] <= 0:
                skipped.append({"order_id": order["order_id"], "reason": "suspended_or_preopen_quote"})
                continue
            one_price = abs(quote["high"] - quote["low"]) <= max(abs(quote["latest"]), 1.0) * 1e-8
            if one_price and order["side"] == "buy" and quote["open"] > quote["previous_close"]:
                skipped.append({"order_id": order["order_id"], "reason": "one_price_limit_up"})
                continue
            if one_price and order["side"] == "sell" and quote["open"] < quote["previous_close"]:
                skipped.append({"order_id": order["order_id"], "reason": "one_price_limit_down"})
                continue
            fill, reason = self._fill_order(
                account["account_id"], order, fill_date, quote["open"], quote["snapshot_id"]
            )
            if reason:
                skipped.append({"order_id": order["order_id"], "reason": reason})
                continue
            turnover += fill["gross_amount"]
            filled.append(fill)
        account = self.account(account["account_id"])
        nav = self._mark_nav(account, execution_date, turnover, quotes)
        return {"account": account, "filled": filled, "skipped": skipped, "nav": nav, "realtime": refresh}

    def dashboard(self, account_id: str | None = None, as_of: str | None = None) -> dict:
        account = self.account(account_id) if account_id else self.default_account()
        as_of = as_of or self.store.market_data_end()
        latest_shadow = self.store.latest_current_shadow()
        realtime_quotes = self._latest_realtime_quotes(account["account_id"])
        realtime_by_code = {
            item["security_code"]: item for item in realtime_quotes
            if item["trading_date"] >= as_of
        }
        positions = self._positions(
            account["account_id"], as_of,
            latest_shadow["snapshot_id"] if latest_shadow else None,
            realtime_by_code,
        )
        with self.store.connect() as conn:
            run_rows = conn.execute(
                "SELECT run_id FROM paper_daily_run WHERE account_id=? ORDER BY as_of DESC, created_at DESC LIMIT 20",
                (account["account_id"],),
            ).fetchall()
            nav_rows = [dict(row) for row in conn.execute(
                "SELECT * FROM paper_nav_snapshot WHERE account_id=? ORDER BY trading_date",
                (account["account_id"],),
            ).fetchall()]
        runs = [self.run(row["run_id"]) for row in run_rows]
        current_nav = account["cash"] + sum(item["market_value"] or 0 for item in positions)
        for item in positions:
            item["weight"] = (item["market_value"] or 0) / current_nav if current_nav else 0
        performance_as_of = nav_rows[-1]["trading_date"] if nav_rows else as_of
        benchmark_comparison = self.benchmark_comparison(account, nav_rows, performance_as_of)
        return {"paper_only": True, "account": account, "as_of": as_of, "nav": current_nav,
                "cumulative_return": current_nav / account["initial_cash"] - 1,
                "cash_weight": account["cash"] / current_nav if current_nav else 0,
                "positions": positions, "runs": runs, "nav_history": nav_rows,
                "benchmark_comparison": benchmark_comparison,
                "latest_shadow": latest_shadow, "realtime_quotes": realtime_quotes,
                "realtime": {
                    "source": "iFinD THS_RQ",
                    "quote_count": len(realtime_quotes),
                    "latest_quote_time": max(
                        (item["quote_time"] for item in realtime_quotes), default=None
                    ),
                    "high_frequency_used": False,
                },
                 "limitations": ["仅为模拟盘，不连接真实券商。", "订单在研究日后的首个可用开盘价撮合，包含固定滑点与费用假设。",
                                 "几日收益只能验证流程与短期表现，不能证明策略具有稳定盈利能力。"]}


class PaperExecutionService(_PaperExecutionCore):
    """Owns paper-account mutations, approvals, fills, positions and NAV."""

    def __init__(
        self,
        repository: StockRepository,
        store: ResearchStore,
        quote_provider: Any | None = None,
        portfolio_decision: PortfolioDecisionService | None = None,
    ) -> None:
        super().__init__(repository, store, quote_provider)
        self.portfolio_decision = portfolio_decision or PortfolioDecisionService(repository, store)

    def research_targets(self, *, as_of: str, limit: int = 5, hold_rank_buffer: int = 30) -> dict:
        return self.portfolio_decision.research_targets(
            as_of=as_of,
            limit=limit,
            hold_rank_buffer=hold_rank_buffer,
        )

    def review_holdings(self, *, account_id: str | None, as_of: str) -> dict:
        account = self.account(account_id) if account_id else self.default_account()
        _, shadow = self.portfolio_decision.sources(as_of)
        positions = self._positions(account["account_id"], as_of, shadow["snapshot_id"])
        return self.portfolio_decision.review_holdings(
            account_id=account["account_id"],
            positions=positions,
            as_of=as_of,
        )

    def create_daily_run(
        self,
        *,
        as_of: str,
        account_id: str | None,
        top_n: int,
        hold_rank_buffer: int,
        target_gross_exposure: float = DEFAULT_TARGET_GROSS_EXPOSURE,
        max_position_weight: float = DEFAULT_MAX_POSITION_WEIGHT,
        max_industry_weight: float = DEFAULT_MAX_INDUSTRY_WEIGHT,
        max_pair_correlation: float = DEFAULT_MAX_PAIR_CORRELATION,
    ) -> dict:
        date.fromisoformat(as_of)
        account = self.account(account_id) if account_id else self.default_account()
        with self.store.connect() as conn:
            latest_fill = conn.execute(
                "SELECT MAX(fill_date) FROM paper_order WHERE account_id=? AND status='filled'",
                (account["account_id"],),
            ).fetchone()[0]
        if latest_fill and as_of < latest_fill:
            raise ValueError(
                f"研究日 {as_of} 早于账户最近成交日 {latest_fill}，禁止回溯重算当前账户"
            )
        _, shadow = self.portfolio_decision.sources(as_of)
        existing = self._run_for_date(account["account_id"], as_of)
        if existing:
            self._supersede_prior_runs(account["account_id"], as_of, existing["run_id"])
            return {**existing, "reused": True}

        positions = self._positions(account["account_id"], as_of, shadow["snapshot_id"])
        run_id, now = str(uuid.uuid4()), utc_now()
        plan = self.portfolio_decision.build_plan(
            account=account,
            positions=positions,
            as_of=as_of,
            run_id=run_id,
            created_at=now,
            top_n=top_n,
            hold_rank_buffer=hold_rank_buffer,
            target_gross_exposure=target_gross_exposure,
            max_position_weight=max_position_weight,
            max_industry_weight=max_industry_weight,
            max_pair_correlation=max_pair_correlation,
        )
        with self.store.connect() as conn:
            conn.execute(
                """INSERT INTO paper_daily_run
                (run_id, account_id, as_of, factor_snapshot_id, shadow_snapshot_id, strategy_version,
                 status, config_json, market_summary_json, snapshot_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'awaiting_review', ?, ?, ?, ?)""",
                (
                    run_id,
                    account["account_id"],
                    as_of,
                    plan["factor_snapshot_id"],
                    plan["shadow_snapshot_id"],
                    STRATEGY_VERSION,
                    json.dumps(plan["config"], ensure_ascii=False, sort_keys=True),
                    json.dumps(plan["market_summary"], ensure_ascii=False, sort_keys=True),
                    plan["snapshot_hash"],
                    now,
                ),
            )
            conn.executemany(
                """INSERT INTO paper_order
                (order_id, run_id, account_id, security_code, side, quantity, reference_price,
                 target_weight, reason_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?)""",
                [
                    (
                        order["order_id"],
                        run_id,
                        account["account_id"],
                        order["security_code"],
                        order["side"],
                        order["quantity"],
                        order["reference_price"],
                        order["target_weight"],
                        json.dumps(order["reason"], ensure_ascii=False, sort_keys=True),
                        now,
                    )
                    for order in plan["orders"]
                ],
            )
        self._supersede_prior_runs(account["account_id"], as_of, run_id)
        self._refresh_run_status(run_id)
        self._mark_nav(account, as_of)
        return {**self.run(run_id), "reused": False}


class PaperTradingService(PaperExecutionService):
    """Backward-compatible facade used by the existing API and scripts."""

    pass
