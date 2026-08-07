from __future__ import annotations


# Alpha158 contains three generator blocks (K-bar, normalized price and rolling
# operators).  This semantic taxonomy is more useful to researchers than those
# implementation details and is kept frozen with the Qlib Alpha158 contract.
ALPHA158_TAXONOMY = (
    {"category_id": "candlestick", "name": "K线形态", "count": 9,
     "families": ("KMID", "KLEN", "KMID2", "KUP", "KUP2", "KLOW", "KLOW2", "KSFT", "KSFT2")},
    {"category_id": "normalized_price", "name": "标准化价格", "count": 4,
     "families": ("OPEN", "HIGH", "LOW", "VWAP")},
    {"category_id": "trend_momentum", "name": "趋势与动量", "count": 25,
     "families": ("ROC", "MA", "BETA", "RSQR", "RESI")},
    {"category_id": "volatility", "name": "波动与离散", "count": 10,
     "families": ("STD", "WVMA")},
    {"category_id": "range_position", "name": "极值与区间位置", "count": 45,
     "families": ("MAX", "MIN", "QTLU", "QTLD", "RANK", "RSV", "IMAX", "IMIN", "IMXD")},
    {"category_id": "price_volume", "name": "价量关系", "count": 10,
     "families": ("CORR", "CORD")},
    {"category_id": "direction_strength", "name": "涨跌方向强度", "count": 30,
     "families": ("CNTP", "CNTN", "CNTD", "SUMP", "SUMN", "SUMD")},
    {"category_id": "volume_behavior", "name": "成交量行为", "count": 25,
     "families": ("VMA", "VSTD", "VSUMP", "VSUMN", "VSUMD")},
)


# The factor lab uses research IDs while the daily snapshot and strategy runtime
# use stable storage fields.  Only mappings listed here can be compiled into a
# linear strategy.
STRATEGY_FACTOR_BINDINGS = {
    "momentum_20d": {"runtime_field": "return_20d", "label": "20日动量"},
    "momentum_60d": {"runtime_field": "return_60d", "label": "60日动量"},
    "volatility_60d": {"runtime_field": "volatility_60d", "label": "60日波动"},
    "max_drawdown_250d": {"runtime_field": "max_drawdown_250d", "label": "250日回撤"},
    "range_position_250d": {"runtime_field": "range_position_52w", "label": "年度价格位置"},
}


def alpha158_summary() -> dict:
    categories = [
        {**item, "families": list(item["families"])} for item in ALPHA158_TAXONOMY
    ]
    return {
        "feature_count": sum(item["count"] for item in categories),
        "generator_count": 3,
        "category_count": len(categories),
        "categories": categories,
    }


def decorate_factor(item: dict) -> dict:
    result = dict(item)
    lifecycle = result.get("lifecycle_status")
    binding = STRATEGY_FACTOR_BINDINGS.get(str(result.get("factor_id")))
    result["strategy_compatible"] = binding is not None
    result["strategy_eligible"] = binding is not None and lifecycle == "approved"
    result["strategy_runtime_field"] = binding["runtime_field"] if binding else None
    if result.get("template_id") == "alpha158_bundle":
        result["usage_scope"] = "model_bundle"
    elif lifecycle == "deprecated":
        result["usage_scope"] = "retired"
    elif binding:
        result["usage_scope"] = "strategy_component"
    else:
        result["usage_scope"] = "research_only"
    return result
