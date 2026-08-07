from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backtest.contracts import ScoreSignal, equal_weight_top_n
from app.backtest.engine import DailyBacktestEngine
from app.backtest.market_rules import ChinaAMarketRules
from app.core.config import settings
from app.core.primitives import code_to_table, qlib_instrument_to_code
from app.data.ifind import IFindDataLayer
from app.market.stock_pool import StockPoolStore
from app.quant.current_shadow import write_qlib_provider


FACTOR_FAMILIES = ("ROC", "MA", "BETA", "RSQR", "RESI")
FACTOR_WINDOWS = (5, 10, 20, 30, 60)
FACTOR_DIRECTIONS = {
    "ROC": "lower",
    "MA": "lower",
    "BETA": "higher",
    "RSQR": "higher",
    "RESI": "higher",
}
BENCHMARKS = {
    "000001.SH": "上证指数",
    "000300.SH": "沪深300",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000688.SH": "科创50",
    "000510.CSI": "中证A500",
}
NAV_COLUMNS = (
    "backtest_id", "trading_date", "nav", "cash", "holdings_value",
    "daily_return", "cumulative_return", "drawdown", "benchmark_nav",
    "benchmark_daily_return", "benchmark_cumulative_return",
    "excess_cumulative_return", "cash_weight", "holdings_count",
)
TRADE_COLUMNS = (
    "trade_id", "backtest_id", "signal_date", "execution_date",
    "security_code", "side", "quantity", "market_price", "execution_price",
    "gross_notional", "explicit_fee", "slippage_cost", "reason",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Alpha158 动量等权多因子 Top20 回测")
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2026, 7, 23))
    parser.add_argument("--end-date", type=date.fromisoformat, default=date(2026, 7, 31))
    parser.add_argument("--capital", type=float, default=500_000)
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--pool-id", default="csi500")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data" / "backtests" / "alpha158_momentum_csi500_20260723_20260731",
    )
    return parser.parse_args()


def load_local_prices(codes: list[str], start: date, end: date) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    with sqlite3.connect(settings.stock_qfq_db) as conn:
        for code in codes:
            table = code_to_table(code)
            frame = pd.read_sql_query(
                f'SELECT time, open, high, low, close, vwap, volume FROM "{table}" '
                "WHERE time BETWEEN ? AND ? ORDER BY time",
                conn,
                params=(start.isoformat(), end.isoformat()),
            )
            if not frame.empty:
                frame["thscode"] = code
                rows.append(frame)
    if not rows:
        raise RuntimeError("前复权数据库没有返回中证500行情")
    return pd.concat(rows, ignore_index=True)


def fetch_missing_prices(
    codes: list[str], start: date, end: date, *, batch_size: int = 50
) -> pd.DataFrame:
    if start > end:
        return pd.DataFrame()
    data = IFindDataLayer(settings)
    frames: list[pd.DataFrame] = []
    try:
        for offset in range(0, len(codes), batch_size):
            frame = data.get_daily_prices(
                codes[offset : offset + batch_size],
                start,
                end,
                adjustment="forward",
                max_attempts=min(settings.ifind_max_attempts, 2),
            )
            if not frame.empty:
                frames.append(frame)
    finally:
        data.close()
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def feature_frame(prices: pd.DataFrame, provider_path: Path, start: date, end: date) -> pd.DataFrame:
    write_qlib_provider(prices, provider_path)
    import qlib
    from qlib.constant import REG_CN
    from qlib.contrib.data.handler import Alpha158DL
    from qlib.data import D

    qlib.init(provider_uri=str(provider_path), region=REG_CN)
    fields, names = Alpha158DL.get_feature_config({
        "rolling": {
            "windows": list(FACTOR_WINDOWS),
            "include": list(FACTOR_FAMILIES),
        }
    })
    result = D.features(
        instruments=D.instruments(market="csi300"),
        fields=fields,
        start_time=start.isoformat(),
        end_time=end.isoformat(),
        freq="day",
    )
    result.columns = names
    result = result.reset_index()
    result["datetime"] = pd.to_datetime(result["datetime"]).dt.strftime("%Y-%m-%d")
    result["security_code"] = result["instrument"].map(qlib_instrument_to_code)
    return result.drop(columns=["instrument"])


def score_signals(features: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    factor_names = [f"{family}{window}" for family in FACTOR_FAMILIES for window in FACTOR_WINDOWS]
    scored: list[pd.DataFrame] = []
    for signal_date, group in features.groupby("datetime", sort=True):
        group = group.copy()
        rank_columns: list[str] = []
        for factor_name in factor_names:
            family = next(item for item in FACTOR_FAMILIES if factor_name.startswith(item))
            rank_name = f"rank_{factor_name}"
            group[rank_name] = group[factor_name].rank(
                pct=True,
                method="average",
                ascending=FACTOR_DIRECTIONS[family] == "higher",
            )
            rank_columns.append(rank_name)
        group["valid_factor_count"] = group[rank_columns].notna().sum(axis=1)
        group["score"] = group[rank_columns].mean(axis=1, skipna=True)
        group = group.loc[
            (group["valid_factor_count"] == len(factor_names))
            & np.isfinite(group["score"])
        ].copy()
        group["rank"] = group["score"].rank(
            method="first", ascending=False
        ).astype(int)
        scored.append(group[[
            "datetime", "security_code", "score", "rank", "valid_factor_count"
        ]])
    if not scored:
        raise RuntimeError("没有形成有效的多因子截面")
    return pd.concat(scored, ignore_index=True), factor_names


def build_histories(prices: pd.DataFrame, trading_dates: list[str]) -> dict[str, dict]:
    histories: dict[str, dict] = {}
    selected = prices.loc[prices["time"].isin(trading_dates)].copy()
    for code, group in selected.groupby("thscode", sort=True):
        records = group.sort_values("time").to_dict(orient="records")
        histories[code] = {
            "rows": records,
            "by_date": {row["time"]: row for row in records},
            "position": {row["time"]: index for index, row in enumerate(records)},
        }
    return histories


def benchmark_curves(start: date, end: date, baseline: date) -> pd.DataFrame:
    store = StockPoolStore(settings.stock_pool_db)
    frame = pd.DataFrame([
        {
            "benchmark_code": code,
            "trading_date": row["time"],
            "close": row["close"],
        }
        for code in BENCHMARKS
        for row in store.index_prices(code, end_date=end.isoformat())
        if row["time"] >= baseline.isoformat()
    ])
    pivot = frame.pivot(index="trading_date", columns="benchmark_code", values="close")
    missing = set(BENCHMARKS) - set(pivot.columns)
    if missing or baseline.isoformat() not in pivot.index:
        raise RuntimeError(f"六大指数数据不完整：missing={sorted(missing)}")
    normalized = pivot.div(pivot.loc[baseline.isoformat()]).loc[start.isoformat() : end.isoformat()]
    return normalized.rename(columns=BENCHMARKS)


def save_chart(comparison: pd.DataFrame, path: Path) -> None:
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(13, 7), dpi=160)
    for column in comparison.columns:
        strategy = column == "Alpha158动量Top20"
        ax.plot(
            comparison.index,
            (comparison[column] - 1) * 100,
            marker="o" if strategy else None,
            linewidth=3 if strategy else 1.4,
            alpha=1 if strategy else 0.78,
            label=column,
        )
    ax.axhline(0, color="#777777", linewidth=0.8)
    ax.set_title("Alpha158 动量多因子 Top20 与六大指数收益对比")
    ax.set_xlabel("交易日")
    ax.set_ylabel("区间累计收益（%）")
    ax.grid(axis="y", alpha=0.22)
    ax.legend(ncol=4, frameon=False, loc="best")
    fig.autofmt_xdate(rotation=25)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def json_value(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not math.isfinite(float(value)) else float(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(type(value).__name__)


def main() -> int:
    args = parse_args()
    if args.start_date >= args.end_date or args.capital <= 0 or args.top_n < 1:
        raise ValueError("日期、资金或 Top-N 参数无效")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    pool = StockPoolStore(settings.stock_pool_db).resolve(args.pool_id, args.end_date)
    codes = [item["security_code"] for item in pool["members"]]
    history_start = args.start_date - timedelta(days=150)
    local_end = min(args.end_date, date(2026, 7, 30))
    prices = load_local_prices(codes, history_start, local_end)
    missing = fetch_missing_prices(codes, local_end + timedelta(days=1), args.end_date)
    if not missing.empty:
        prices = pd.concat([prices, missing], ignore_index=True)
    prices = prices.drop_duplicates(["thscode", "time"], keep="last")
    prices = prices.sort_values(["thscode", "time"]).reset_index(drop=True)

    trading_dates = sorted(
        prices.loc[
            prices["time"].between(args.start_date.isoformat(), args.end_date.isoformat()),
            "time",
        ].unique()
    )
    if not trading_dates or trading_dates[-1] != args.end_date.isoformat():
        raise RuntimeError(f"行情未覆盖回测截止日：latest={trading_dates[-1] if trading_dates else None}")
    all_dates = sorted(prices["time"].unique())
    first_index = all_dates.index(trading_dates[0])
    signal_dates = all_dates[first_index - 1 : first_index - 1 + len(trading_dates)]
    if len(signal_dates) != len(trading_dates):
        raise RuntimeError("无法为每个执行日匹配前一交易日信号")

    features = feature_frame(
        prices,
        args.output_dir / "qlib_provider",
        date.fromisoformat(signal_dates[0]),
        date.fromisoformat(signal_dates[-1]),
    )
    scores, factor_names = score_signals(features)
    targets = []
    selected_rows: list[dict] = []
    for signal_date, execution_date in zip(signal_dates, trading_dates, strict=True):
        daily = scores.loc[scores["datetime"] == signal_date].sort_values(
            ["rank", "security_code"]
        )
        if len(daily) < args.top_n:
            raise RuntimeError(f"{signal_date} 有效股票不足 Top{args.top_n}")
        signals = [
            ScoreSignal(
                "factor_set",
                "alpha158_trend_momentum_equal_rank",
                signal_date,
                row.security_code,
                float(row.score),
                int(row.rank),
            )
            for row in daily.itertuples(index=False)
        ]
        targets.append(equal_weight_top_n(signals, execution_date=execution_date, top_n=args.top_n))
        for row in daily.head(args.top_n).itertuples(index=False):
            selected_rows.append({
                "signal_date": signal_date,
                "execution_date": execution_date,
                "rank": int(row.rank),
                "security_code": row.security_code,
                "score": float(row.score),
                "valid_factor_count": int(row.valid_factor_count),
            })

    result = DailyBacktestEngine(ChinaAMarketRules()).run(
        backtest_id="alpha158-momentum-csi500-20260723-20260731",
        histories=build_histories(prices, trading_dates),
        trading_dates=trading_dates,
        targets=targets,
        initial_capital=args.capital,
    )
    nav = pd.DataFrame(result.nav_rows, columns=NAV_COLUMNS).set_index("trading_date")
    comparison = benchmark_curves(
        args.start_date,
        args.end_date,
        date.fromisoformat(signal_dates[0]),
    )
    comparison.insert(0, "Alpha158动量Top20", nav["nav"] / args.capital)

    comparison.to_csv(args.output_dir / "nav_comparison.csv", encoding="utf-8-sig")
    pd.DataFrame(selected_rows).to_csv(
        args.output_dir / "daily_top20.csv", index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(result.trade_rows, columns=TRADE_COLUMNS).to_csv(
        args.output_dir / "trades.csv", index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(result.blocked_orders).to_csv(
        args.output_dir / "blocked_orders.csv", index=False, encoding="utf-8-sig"
    )
    save_chart(comparison, args.output_dir / "return_comparison.png")

    final_returns = {
        column: float(comparison[column].iloc[-1] - 1) for column in comparison.columns
    }
    summary = {
        "strategy": "Alpha158趋势与动量25因子等权排名 Top20",
        "requested_period": [args.start_date.isoformat(), args.end_date.isoformat()],
        "effective_period": [trading_dates[0], trading_dates[-1]],
        "signal_period": [signal_dates[0], signal_dates[-1]],
        "universe": {
            "pool_id": pool["pool_id"],
            "name": pool["name"],
            "snapshot_date": pool["as_of"],
            "member_count": pool["member_count"],
        },
        "capital": args.capital,
        "top_n": args.top_n,
        "factor_count": len(factor_names),
        "factors": factor_names,
        "factor_directions": FACTOR_DIRECTIONS,
        "score_method": "每个因子按方向做横截面百分位排名，25个排名等权平均",
        "portfolio_method": "每日目标 Top20 等权；卖出退出 Top20 的持仓",
        "execution": "前一交易日收盘生成信号，下一交易日开盘执行；A股T+1及默认交易成本",
        "metrics": result.metrics,
        "comparison_final_returns": final_returns,
        "blocked_order_count": len(result.blocked_orders),
        "files": {
            "curve": "return_comparison.png",
            "nav": "nav_comparison.csv",
            "selection": "daily_top20.csv",
            "trades": "trades.csv",
            "blocked_orders": "blocked_orders.csv",
        },
        "limitations": [
            "中证500使用2026-07-31成分股快照，短区间结果存在当前成分股口径偏差。",
            "回测只有7个交易日，年化收益、夏普等年化指标不具备统计意义。",
            "六大指数以2026-07-22收盘为共同基点，策略在下一交易日开盘开始建仓。",
        ],
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=json_value),
        encoding="utf-8",
    )
    print(json.dumps({
        "output_dir": str(args.output_dir.resolve()),
        "final_nav": result.metrics["final_nav"],
        "cumulative_return": result.metrics["cumulative_return"],
        "max_drawdown": result.metrics["max_drawdown"],
        "trade_count": result.metrics["trade_count"],
        "blocked_order_count": len(result.blocked_orders),
        "comparison_final_returns": final_returns,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
