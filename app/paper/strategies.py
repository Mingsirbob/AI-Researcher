from __future__ import annotations


LIGHTGBM_SHADOW_STRATEGY_ID = "lightgbm_shadow_v1"
MULTIFACTOR_LINEAR_STRATEGY_ID = "multifactor_linear_v1"
DEFAULT_PAPER_STRATEGY_ID = LIGHTGBM_SHADOW_STRATEGY_ID


PAPER_STRATEGIES = (
    {
        "strategy_id": LIGHTGBM_SHADOW_STRATEGY_ID,
        "version": "1.0.0",
        "name": "LightGBM Shadow",
        "description": "使用已验收 Current Shadow 的 LightGBM 截面排名生成候选。",
        "signal_source": "current_shadow",
        "config": {
            "target_gross_exposure": 0.50,
            "max_position_weight": 0.12,
            "max_industry_weight": 0.20,
            "max_pair_correlation": 0.85,
        },
    },
    {
        "strategy_id": MULTIFACTOR_LINEAR_STRATEGY_ID,
        "version": "1.0.0",
        "name": "线性多因子",
        "description": "对动量、低波动、回撤和价格位置做截面排名后线性合成。",
        "signal_source": "multifactor_linear",
        "config": {
            "target_gross_exposure": 0.80,
            "max_position_weight": 0.12,
            "max_industry_weight": 0.24,
            "max_pair_correlation": 0.85,
            "factor_weights": {
                "return_20d": 0.25,
                "return_60d": 0.30,
                "volatility_60d": -0.20,
                "max_drawdown_250d": 0.15,
                "range_position_52w": 0.10,
            },
        },
    },
)


def strategy_definition(strategy_id: str) -> dict:
    for strategy in PAPER_STRATEGIES:
        if strategy["strategy_id"] == strategy_id:
            return strategy
    raise KeyError("模拟策略不存在")
