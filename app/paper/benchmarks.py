from __future__ import annotations

from .context import *  # noqa: F403
from .context import _normalize_paper_quote_code


class PaperBenchmarkMixin:
    def benchmark_sync_range(
        self, account_id: str | None = None, as_of: str | None = None
    ) -> dict:
        account = self.account(account_id) if account_id else self.default_account()
        end = as_of or self.store.market_data_end()
        if end is None:
            raise ValueError("本地行情库没有可用截止日")
        date.fromisoformat(end)
        with self.paper_store.connect() as conn:
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
            code = _normalize_paper_quote_code(str(row.get("thscode") or ""))
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
        with self.paper_store.connect() as conn:
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
        *,
        realtime_quotes: dict[str, dict] | None = None,
        current_nav: float | None = None,
    ) -> dict:
        realtime_quotes = realtime_quotes or {}
        live_quotes = {
            code: item for code, item in realtime_quotes.items()
            if code in PAPER_BENCHMARKS and item.get("trading_date")
        }
        live_date = max(
            (item["trading_date"] for item in live_quotes.values()), default=None
        )
        live_quote_time = max(
            (item.get("quote_time") for item in live_quotes.values() if item.get("quote_time")),
            default=None,
        )
        comparison_end = max(as_of, live_date) if live_date else as_of
        with self.paper_store.connect() as conn:
            first_fill_row = conn.execute(
                """SELECT MIN(fill_date) FROM paper_order
                   WHERE account_id=? AND status='filled' AND fill_date<=?""",
                (account["account_id"], comparison_end),
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
            if inception <= row["trading_date"] <= comparison_end
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
        if live_date and live_date >= inception and live_date not in dates:
            dates.append(live_date)
            dates.sort()
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
        if live_date and live_date >= inception and current_nav is not None:
            live_portfolio_point = {
                "date": live_date,
                "cumulative_return": float(current_nav) / baseline_nav - 1,
                "valuation_mode": "intraday",
            }
            portfolio_points = [
                item for item in portfolio_points if item["date"] != live_date
            ]
            portfolio_points.append(live_portfolio_point)
            portfolio_points.sort(key=lambda item: item["date"])
        portfolio_latest = portfolio_points[-1]["cumulative_return"]
        with self.paper_store.connect() as conn:
            price_rows = [dict(row) for row in conn.execute(
                """SELECT benchmark_code, trading_date, close, source, snapshot_hash
                   FROM paper_benchmark_price
                   WHERE account_id=? AND trading_date<=?
                   ORDER BY benchmark_code, trading_date""",
                (account["account_id"], comparison_end),
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
            live_quote = live_quotes.get(code)
            if (
                live_quote
                and live_date
                and live_quote["trading_date"] == live_date
                and live_date >= inception
                and float(live_quote.get("latest") or 0) > 0
            ):
                points = [item for item in points if item["date"] != live_date]
                points.append({
                    "date": live_date,
                    "cumulative_return": (
                        float(live_quote["latest"]) / float(baseline["close"]) - 1
                    ),
                    "valuation_mode": "intraday",
                })
                points.sort(key=lambda item: item["date"])
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
                "latest_source": live_quote.get("source") if live_quote else baseline["source"],
            })
        live_complete = bool(live_date and current_nav is not None) and all(
            item["status"] == "complete" and item.get("latest_source") == "iFinD THS_RQ"
            for item in benchmarks
        )
        return {
            "status": "live" if live_complete else "complete" if not missing else "partial",
            "as_of": live_date or eligible_nav[-1]["trading_date"],
            "inception_date": inception,
            "valuation_mode": "intraday" if live_date and current_nav is not None else "official_close",
            "latest_quote_time": live_quote_time,
            "portfolio": {
                "name": account["name"],
                "latest_return": portfolio_latest,
                "points": portfolio_points,
                "baseline_nav_date": baseline_nav_date,
            },
            "benchmarks": benchmarks,
            "limitations": missing + [
                "收益期从首笔成交日开始；盘中点使用 iFinD THS_RQ 估算，收盘后由 THS_HD 日线固化正式绩效。"
            ],
        }
