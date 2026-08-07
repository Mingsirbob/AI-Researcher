from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Any

from app.core.primitives import canonical_hash

from .contracts import BacktestResult, PositionLot, TargetPortfolio
from .market_rules import ChinaAMarketRules
from .metrics import calculate_metrics


class DailyBacktestEngine:
    """Daily target-weight simulator shared by factors, models and strategies."""

    def __init__(self, rules: ChinaAMarketRules | None = None):
        self.rules = rules or ChinaAMarketRules()

    def run(
        self,
        *,
        backtest_id: str,
        histories: dict[str, dict[str, Any]],
        trading_dates: list[str],
        targets: list[TargetPortfolio],
        initial_capital: float,
    ) -> BacktestResult:
        if initial_capital <= 0:
            raise ValueError("初始资金必须为正数")
        if len(trading_dates) < 2:
            raise ValueError("没有足够的交易日")
        schedules = {target.execution_date: target for target in targets}
        lots: dict[str, list[PositionLot]] = defaultdict(list)
        cash = float(initial_capital)
        last_close: dict[str, float] = {}
        previous_benchmark_marks: dict[str, float] = {}
        previous_nav: float | None = None
        benchmark_nav = float(initial_capital)
        nav_peak = float(initial_capital)
        nav_rows: list[tuple] = []
        rebalance_rows: list[tuple] = []
        trade_rows: list[tuple] = []
        blocked_orders: list[dict[str, Any]] = []
        realized_pnls: list[float] = []

        for trading_date in trading_dates:
            rows_today = {
                code: history["by_date"][trading_date]
                for code, history in histories.items()
                if trading_date in history["by_date"]
            }
            target = schedules.get(trading_date)
            if target is not None:
                rebalance = self._rebalance(
                    backtest_id=backtest_id,
                    target=target,
                    rows_today=rows_today,
                    last_close=last_close,
                    lots=lots,
                    cash=cash,
                )
                cash = rebalance["cash"]
                trade_rows.extend(rebalance["trades"])
                blocked_orders.extend(rebalance["blocked"])
                realized_pnls.extend(rebalance["realized_pnls"])
                rebalance_rows.append(rebalance["row"])

            for code, row in rows_today.items():
                close = _finite(_value(row, "close"))
                if close is not None and close > 0:
                    last_close[code] = close
            quantities = {code: sum(lot.quantity for lot in items) for code, items in lots.items()}
            holdings_value = sum(quantity * last_close.get(code, 0.0) for code, quantity in quantities.items())
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
                sum(quantity > 0 for quantity in quantities.values()),
            ))
            previous_nav = nav
            previous_benchmark_marks = current_benchmark_marks

        metrics = calculate_metrics(
            nav_rows=nav_rows,
            rebalance_rows=rebalance_rows,
            trade_rows=trade_rows,
            initial_capital=initial_capital,
            realized_pnls=realized_pnls,
        )
        return BacktestResult(
            effective_start_date=nav_rows[0][1],
            effective_end_date=nav_rows[-1][1],
            nav_rows=nav_rows,
            rebalance_rows=rebalance_rows,
            trade_rows=trade_rows,
            metrics=metrics,
            blocked_orders=blocked_orders,
        )

    def _rebalance(
        self,
        *,
        backtest_id: str,
        target: TargetPortfolio,
        rows_today: dict[str, Any],
        last_close: dict[str, float],
        lots: dict[str, list[PositionLot]],
        cash: float,
    ) -> dict[str, Any]:
        open_marks = {
            code: _finite(_value(rows_today.get(code), "open")) or last_close.get(code, 0.0)
            for code in set(lots) | set(target.weights)
        }
        quantities = {code: sum(lot.quantity for lot in items) for code, items in lots.items()}
        equity = cash + sum(quantities.get(code, 0) * price for code, price in open_marks.items())
        trades: list[tuple] = []
        blocked: list[dict[str, Any]] = []
        realized_pnls: list[float] = []
        buys = sells = blocked_buys = blocked_sells = 0
        notional = explicit_cost = slippage_cost = 0.0

        for code in sorted(set(lots) | set(target.weights)):
            market_price = open_marks.get(code, 0.0)
            current_quantity = sum(lot.quantity for lot in lots.get(code, []))
            current_value = current_quantity * market_price
            target_value = equity * target.weights.get(code, 0.0)
            if current_value <= target_value + 1e-8:
                continue
            available = sum(
                lot.quantity for lot in lots.get(code, [])
                if lot.acquired_date < target.execution_date
            )
            desired = min(available, (current_value - target_value) / market_price) if market_price > 0 else 0
            liquidating = target.weights.get(code, 0.0) <= 1e-12
            quantity = self.rules.round_sell_quantity(desired, liquidating=liquidating)
            row = rows_today.get(code)
            reason = self._blocked_reason(code, "sell", row, last_close.get(code), target.execution_date)
            quantity = self._apply_volume_cap(quantity, row)
            if available <= 0:
                reason = "t_plus_one"
            elif quantity <= 0 and reason is None:
                reason = "lot_or_volume_limit"
            if reason:
                blocked_sells += 1
                blocked.append(_blocked(target, code, "sell", reason))
                continue
            execution_price = self.rules.execution_price("sell", market_price)
            fee = self.rules.fee("sell", quantity, execution_price)
            gross = quantity * execution_price
            cost_basis = self._consume_lots(lots[code], quantity, target.execution_date)
            cash += gross - fee
            realized_pnls.append(gross - fee - cost_basis)
            sells += 1
            notional += quantity * market_price
            explicit_cost += fee
            slip = quantity * (market_price - execution_price)
            slippage_cost += abs(slip)
            trades.append(_trade_row(
                backtest_id, target, code, "sell", quantity, market_price,
                execution_price, gross, fee, abs(slip), "target_rebalance",
            ))
            if not lots[code]:
                lots.pop(code, None)

        for code, weight in sorted(target.weights.items()):
            market_price = open_marks.get(code, 0.0)
            current_quantity = sum(lot.quantity for lot in lots.get(code, []))
            required = equity * weight - current_quantity * market_price
            if required <= 1e-8:
                continue
            row = rows_today.get(code)
            reason = self._blocked_reason(code, "buy", row, last_close.get(code), target.execution_date)
            execution_price = self.rules.execution_price("buy", market_price) if market_price > 0 else 0.0
            quantity = self.rules.round_buy_quantity(required / execution_price) if execution_price > 0 else 0
            quantity = self._apply_volume_cap(quantity, row)
            quantity = self._affordable_quantity(quantity, execution_price, cash)
            if quantity <= 0 and reason is None:
                reason = "cash_lot_or_volume_limit"
            if reason:
                blocked_buys += 1
                blocked.append(_blocked(target, code, "buy", reason))
                continue
            fee = self.rules.fee("buy", quantity, execution_price)
            gross = quantity * execution_price
            cash -= gross + fee
            lots[code].append(PositionLot(code, target.execution_date, quantity, execution_price))
            buys += 1
            notional += quantity * market_price
            explicit_cost += fee
            slip = quantity * (execution_price - market_price)
            slippage_cost += abs(slip)
            trades.append(_trade_row(
                backtest_id, target, code, "buy", quantity, market_price,
                execution_price, gross, fee, abs(slip), "target_rebalance",
            ))

        turnover = notional / equity if equity > 0 else 0.0
        return {
            "cash": cash,
            "trades": trades,
            "blocked": blocked,
            "realized_pnls": realized_pnls,
            "row": (
                backtest_id, target.signal_date, target.execution_date,
                len(target.weights), buys, sells, blocked_buys, blocked_sells,
                turnover, explicit_cost, slippage_cost,
                _json_codes(target.weights),
            ),
        }

    def _blocked_reason(
        self,
        code: str,
        side: str,
        row: Any,
        previous_close: float | None,
        trading_date: str,
    ) -> str | None:
        if row is None:
            return "missing_bar"
        return self.rules.tradability_reason(
            security_code=code,
            side=side,  # type: ignore[arg-type]
            row=dict(row),
            previous_close=previous_close,
            trading_date=trading_date,
        )

    def _apply_volume_cap(self, quantity: int, row: Any) -> int:
        if row is None:
            return 0
        cap = self.rules.volume_cap(_value(row, "volume"))
        return min(quantity, cap) if cap is not None else 0

    def _affordable_quantity(self, quantity: int, price: float, cash: float) -> int:
        lot = self.rules.config.lot_size
        candidate = quantity
        while candidate > 0:
            if candidate * price + self.rules.fee("buy", candidate, price) <= cash + 1e-9:
                return candidate
            candidate -= lot
        return 0

    @staticmethod
    def _consume_lots(items: list[PositionLot], quantity: int, trading_date: str) -> float:
        remaining = quantity
        cost = 0.0
        for lot in list(items):
            if remaining <= 0:
                break
            if lot.acquired_date >= trading_date:
                continue
            used = min(lot.quantity, remaining)
            lot.quantity -= used
            remaining -= used
            cost += used * lot.cost_price
            if lot.quantity == 0:
                items.remove(lot)
        if remaining:
            raise RuntimeError("可卖数量计算不一致")
        return cost


def _trade_row(
    backtest_id: str,
    target: TargetPortfolio,
    code: str,
    side: str,
    quantity: int,
    market_price: float,
    execution_price: float,
    gross: float,
    fee: float,
    slip: float,
    reason: str,
) -> tuple:
    trade_id = canonical_hash({
        "backtest_id": backtest_id,
        "signal_date": target.signal_date,
        "execution_date": target.execution_date,
        "code": code,
        "side": side,
    })
    return (
        trade_id, backtest_id, target.signal_date, target.execution_date,
        code, side, quantity, market_price, execution_price, gross, fee, slip, reason,
    )


def _blocked(target: TargetPortfolio, code: str, side: str, reason: str) -> dict[str, Any]:
    return {
        "signal_date": target.signal_date,
        "execution_date": target.execution_date,
        "security_code": code,
        "side": side,
        "reason": reason,
    }


def _value(row: Any, key: str) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return None


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _json_codes(weights: dict[str, float]) -> str:
    import json

    return json.dumps(sorted(weights), ensure_ascii=False, separators=(",", ":"))
