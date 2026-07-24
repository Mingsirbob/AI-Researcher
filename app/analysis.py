from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from typing import Any

from .primitives import annualized_volatility, maximum_drawdown, period_return


def _round(value: float | None, digits: int = 2) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def _mean(values: list[float], periods: int) -> float | None:
    if len(values) < periods:
        return None
    return statistics.fmean(values[-periods:])


def _rsi(values: list[float], periods: int = 14) -> float | None:
    if len(values) <= periods:
        return None
    changes = [values[i] - values[i - 1] for i in range(len(values) - periods, len(values))]
    gains = statistics.fmean(max(change, 0) for change in changes)
    losses = statistics.fmean(max(-change, 0) for change in changes)
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + gains / losses)


def _series(rows: list[dict], key: str) -> list[float]:
    return [float(row[key]) for row in rows if row.get(key) is not None]


def _display_percent(value: float | None) -> str:
    return f"{value:.2f}%" if value is not None else "数据不足"


def analyze_stock(code: str, name: str, rows: list[dict]) -> dict[str, Any]:
    valid = [row for row in rows if row.get("close") is not None]
    traded = [row for row in valid if row.get("volume") is not None]
    if len(valid) < 2:
        raise ValueError("有效行情不足，无法分析")

    closes = _series(valid, "close")
    volumes = _series(traded, "volume")
    latest = valid[-1]
    latest_close = closes[-1]
    as_of = latest["time"]
    ma20 = _mean(closes, 20)
    ma60 = _mean(closes, 60)
    ma120 = _mean(closes, 120)
    high_52w = max(closes[-250:])
    low_52w = min(closes[-250:])
    avg_volume20 = _mean(volumes, 20)
    current_volume = float(latest["volume"]) if latest.get("volume") is not None else None
    volume_ratio = current_volume / avg_volume20 if current_volume is not None and avg_volume20 else None

    returns = {
        period: period_return(closes, period, require_positive_base=False)
        for period in (1, 5, 20, 60, 120, 250)
    }
    volatility = annualized_volatility(
        closes, periods=60, require_full_window=False, ignore_zero_bases=True
    )
    drawdown = maximum_drawdown(closes, periods=250, require_full_window=False)
    rsi14 = _rsi(closes)
    from_high = latest_close / high_52w - 1 if high_52w else None
    range_position = (latest_close - low_52w) / (high_52w - low_52w) if high_52w != low_52w else 0.5

    trend_score = sum(
        [
            1 if ma20 and latest_close > ma20 else -1,
            1 if ma60 and latest_close > ma60 else -1,
            1 if ma20 and ma60 and ma20 > ma60 else -1,
        ]
    )
    trend = "偏强" if trend_score >= 2 else "偏弱" if trend_score <= -2 else "震荡"
    suspended = latest.get("open") is None or latest.get("volume") is None

    metrics = {
        "close": _round(latest_close),
        "daily_return": _round(returns[1] * 100 if returns[1] is not None else None),
        "return_20d": _round(returns[20] * 100 if returns[20] is not None else None),
        "return_60d": _round(returns[60] * 100 if returns[60] is not None else None),
        "return_250d": _round(returns[250] * 100 if returns[250] is not None else None),
        "volatility_60d": _round(volatility * 100 if volatility is not None else None),
        "max_drawdown_250d": _round(drawdown * 100 if drawdown is not None else None),
        "ma20": _round(ma20),
        "ma60": _round(ma60),
        "ma120": _round(ma120),
        "rsi14": _round(rsi14, 1),
        "volume_ratio_20d": _round(volume_ratio),
        "from_52w_high": _round(from_high * 100 if from_high is not None else None),
        "range_position_52w": _round(range_position * 100),
        "trend": trend,
        "suspended": suspended,
    }

    source = f"local://stock_data.db/stock_{code.replace('.', '_')}"
    evidence = [
        {
            "id": "ev-price",
            "category": "market_fact",
            "label": "最新收盘",
            "value": f"{latest_close:.2f}",
            "as_of": as_of,
            "source": source,
            "method": "本地日线 close 字段；价格复权口径以源数据库为准",
        },
        {
            "id": "ev-trend",
            "category": "calculation",
            "label": "趋势结构",
            "value": trend,
            "as_of": as_of,
            "source": source,
            "method": "收盘价与20/60日均线、20日与60日均线相对位置的三项规则",
        },
        {
            "id": "ev-momentum",
            "category": "calculation",
            "label": "20日收益",
            "value": f"{metrics['return_20d']:.2f}%" if metrics["return_20d"] is not None else "数据不足",
            "as_of": as_of,
            "source": source,
            "method": "最新收盘价 / 20个交易日前收盘价 - 1",
        },
        {
            "id": "ev-risk",
            "category": "calculation",
            "label": "250日最大回撤",
            "value": f"{metrics['max_drawdown_250d']:.2f}%" if metrics["max_drawdown_250d"] is not None else "数据不足",
            "as_of": as_of,
            "source": source,
            "method": "最近250条记录内逐日相对历史峰值的最小收益",
        },
        {
            "id": "ev-volume",
            "category": "calculation",
            "label": "量比（20日）",
            "value": f"{metrics['volume_ratio_20d']:.2f}x" if metrics["volume_ratio_20d"] is not None else "停牌或数据不足",
            "as_of": as_of,
            "source": source,
            "method": "当日成交量 / 最近20个有成交日的平均成交量",
        },
    ]

    momentum = returns[20]
    claim_text = (
        f"价格趋势当前{trend}，20日动量为{metrics['return_20d']:.2f}%。"
        if momentum is not None
        else f"价格趋势当前{trend}，20日样本不足。"
    )
    claims = [
        {
            "id": "cl-trend",
            "statement": claim_text,
            "claim_type": "inference",
            "confidence": 0.78 if len(closes) >= 120 else 0.58,
            "evidence_ids": ["ev-price", "ev-trend", "ev-momentum"],
            "counter_evidence": ["单一价格趋势不能证明基本面改善"],
            "invalidating_conditions": ["收盘价跌破60日均线且20日均线转为下行"],
        },
        {
            "id": "cl-risk",
            "statement": (
                f"近250日最大回撤为{_display_percent(metrics['max_drawdown_250d'])}，"
                f"60日年化波动率为{_display_percent(metrics['volatility_60d'])}。"
            ),
            "claim_type": "fact",
            "confidence": 0.98,
            "evidence_ids": ["ev-risk"],
            "counter_evidence": [],
            "invalidating_conditions": ["新增行情将改变滚动窗口统计结果"],
        },
    ]

    chart_rows = valid[-520:]
    generated_at = datetime.now(timezone.utc).isoformat()
    return {
        "security": {"code": code, "name": name},
        "as_of": as_of,
        "generated_at": generated_at,
        "coverage": {"start": valid[0]["time"], "end": as_of, "observations": len(valid)},
        "metrics": metrics,
        "evidence": evidence,
        "claims": claims,
        "uncertainties": [
            "本地库仅含日线价格与成交量，当前结论不包含财务、公告、估值和行业证据。",
            "数据库未声明前复权、后复权或不复权口径，公司行为附近的收益需二次核验。",
            "技术与流动性状态只描述已发生的市场数据，不构成未来价格预测。",
        ],
        "series": {
            "dates": [row["time"] for row in chart_rows],
            "open": [row.get("open") for row in chart_rows],
            "high": [row.get("high") for row in chart_rows],
            "low": [row.get("low") for row in chart_rows],
            "close": [row.get("close") for row in chart_rows],
            "volume": [row.get("volume") for row in chart_rows],
        },
    }
