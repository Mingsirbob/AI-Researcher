from __future__ import annotations

from .context import *  # noqa: F403
from .context import _normalize_paper_quote_code


class PaperDashboardMixin:
    def dashboard(self, account_id: str | None = None, as_of: str | None = None) -> dict:
        account = self.account(account_id) if account_id else self.default_account()
        as_of = as_of or self.store.market_data_end()
        latest_shadow = self.quant_store.latest_current_shadow()
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
        with self.paper_store.connect() as conn:
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
        max_position_weight = float(account["strategy"]["config"].get("max_position_weight") or 0)
        for item in positions:
            item["weight"] = (item["market_value"] or 0) / current_nav if current_nav else 0
            risk_flags = []
            if item["close"] is None:
                risk_flags.append("missing_price")
            if max_position_weight and item["weight"] > max_position_weight + 1e-8:
                risk_flags.append("position_weight_limit")
            if item["available_quantity"] < item["quantity"]:
                risk_flags.append("t1_locked")
            item["risk_flags"] = risk_flags
            item["risk_status"] = (
                "行情缺失" if "missing_price" in risk_flags
                else "仓位超限" if "position_weight_limit" in risk_flags
                else "T+1锁定" if "t1_locked" in risk_flags
                else "正常"
            )
        performance_as_of = nav_rows[-1]["trading_date"] if nav_rows else as_of
        benchmark_comparison = self.benchmark_comparison(
            account,
            nav_rows,
            performance_as_of,
            realtime_quotes=realtime_by_code,
            current_nav=current_nav,
        )
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
