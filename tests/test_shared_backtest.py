from __future__ import annotations

import pytest

from app.backtest.constraints import PortfolioConstraints, apply_constraints
from app.backtest.contracts import PositionLot, TargetPortfolio
from app.backtest.engine import DailyBacktestEngine
from app.backtest.market_rules import ChinaAConfig, ChinaAMarketRules
from app.backtest.contracts import ScoreSignal
from app.backtest.signals import combine_factor_signals, ranked_rows_to_target, strategy_weights_to_target


def _history(prices: list[tuple[str, float, float]]) -> dict:
    rows = [
        {
            "time": trading_date,
            "open": open_price,
            "high": max(open_price, close_price),
            "low": min(open_price, close_price),
            "close": close_price,
            "vwap": close_price,
            "volume": 1_000_000,
        }
        for trading_date, open_price, close_price in prices
    ]
    return {"rows": rows, "by_date": {row["time"]: row for row in rows}}


def test_china_a_rules_cover_fees_lots_and_board_limits():
    rules = ChinaAMarketRules(ChinaAConfig(commission_rate=0.00025, slippage_rate=0))

    assert rules.fee("buy", 1000, 100) == pytest.approx(26)
    assert rules.fee("sell", 1000, 100) == pytest.approx(76)
    assert rules.round_buy_quantity(399) == 300
    assert rules.price_limit("300001.SZ", trading_date="2026-01-01") == 0.20
    assert rules.price_limit("688001.SH", trading_date="2026-01-01") == 0.20
    assert rules.price_limit("920001.BJ", trading_date="2026-01-01") == 0.30
    assert rules.price_limit("000001.SZ", trading_date="2026-01-01", is_st=True) == 0.05


def test_constraints_cap_names_and_groups():
    result = apply_constraints(
        {"A": 0.7, "B": 0.2, "C": 0.1},
        PortfolioConstraints(max_weight=0.5, max_group_weight=0.6),
        groups={"A": "tech", "B": "tech", "C": "finance"},
    )

    assert max(result.values()) <= 0.5
    assert result["A"] + result["B"] <= 0.6 + 1e-12


def test_engine_resizes_existing_positions_with_a_share_rules():
    dates = ["2026-01-05", "2026-01-06", "2026-01-07"]
    histories = {
        "000001.SZ": _history([(day, 10.0, 10.0) for day in dates]),
        "000002.SZ": _history([(day, 10.0, 10.0) for day in dates]),
    }
    targets = [
        TargetPortfolio("factor", "momentum", dates[0], dates[0], {
            "000001.SZ": 0.5, "000002.SZ": 0.5,
        }),
        TargetPortfolio("factor", "momentum", dates[1], dates[1], {
            "000001.SZ": 0.8, "000002.SZ": 0.2,
        }),
    ]
    result = DailyBacktestEngine(
        ChinaAMarketRules(ChinaAConfig(slippage_rate=0))
    ).run(
        backtest_id="shared-engine-test",
        histories=histories,
        trading_dates=dates,
        targets=targets,
        initial_capital=100_000,
    )

    second_day = [row for row in result.trade_rows if row[3] == dates[1]]
    assert any(row[4] == "000001.SZ" and row[5] == "buy" for row in second_day)
    assert any(row[4] == "000002.SZ" and row[5] == "sell" for row in second_day)
    assert all(row[6] % 100 == 0 for row in result.trade_rows)
    assert result.metrics["explicit_cost"] > 0
    assert result.nav_rows[-1][13] == 2


def test_position_lots_only_release_after_trade_date():
    lots = [PositionLot("000001.SZ", "2026-01-05", 500, 10.0)]

    with pytest.raises(RuntimeError, match="可卖数量"):
        DailyBacktestEngine._consume_lots(lots, 100, "2026-01-05")
    assert DailyBacktestEngine._consume_lots(lots, 100, "2026-01-06") == 1000
    assert lots[0].quantity == 400


def test_limit_check_uses_execution_open_not_same_day_close():
    rules = ChinaAMarketRules()
    row = {"open": 10.0, "close": 11.0, "volume": 1_000_000}

    assert rules.tradability_reason(
        security_code="000001.SZ",
        side="buy",
        row=row,
        previous_close=10.0,
        trading_date="2026-01-06",
    ) is None


def test_factor_model_and_strategy_adapters_share_target_contract():
    model = ranked_rows_to_target(
        [
            {"security_code": "000001.SZ", "score": 0.9, "rank": 1},
            {"security_code": "000002.SZ", "score": 0.8, "rank": 2},
        ],
        source_type="model",
        source_id="lightgbm-v1",
        signal_date="2026-01-05",
        execution_date="2026-01-06",
        top_n=1,
    )
    combined = combine_factor_signals(
        {
            "momentum": [
                ScoreSignal("factor", "momentum", "2026-01-05", "000001.SZ", 1.0, 1),
                ScoreSignal("factor", "momentum", "2026-01-05", "000002.SZ", 0.0, 2),
            ],
            "low_vol": [
                ScoreSignal("factor", "low_vol", "2026-01-05", "000002.SZ", 1.0, 1),
                ScoreSignal("factor", "low_vol", "2026-01-05", "000001.SZ", 0.0, 2),
            ],
        },
        factor_weights={"momentum": 0.7, "low_vol": 0.3},
        source_id="linear-v1",
        execution_date="2026-01-06",
        top_n=1,
    )
    strategy = strategy_weights_to_target(
        {"000001.SZ": 0.6, "000002.SZ": 0.4},
        strategy_id="strategy-v1",
        signal_date="2026-01-05",
        execution_date="2026-01-06",
    )

    assert model.weights == {"000001.SZ": 1.0}
    assert combined.weights == {"000001.SZ": 1.0}
    assert strategy.source_type == "strategy"
    assert sum(strategy.weights.values()) == pytest.approx(1.0)
