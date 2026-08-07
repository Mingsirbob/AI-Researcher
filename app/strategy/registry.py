from __future__ import annotations

from copy import deepcopy


COMPONENTS = (
    {"component_id": "universe.csi300", "slot": "universe", "name": "沪深300 股票池", "input": None, "output": "UniverseSnapshot", "config_schema": {"pool_id": "string"}},
    {"component_id": "signal.lightgbm_shadow", "slot": "signal", "name": "LightGBM Current Shadow", "input": "UniverseSnapshot", "output": "SignalSnapshot", "config_schema": {"model_run_id": "string?"}},
    {"component_id": "signal.multifactor_linear", "slot": "signal", "name": "线性多因子", "input": "UniverseSnapshot", "output": "SignalSnapshot", "config_schema": {"factor_weights": "object"}},
    {"component_id": "signal.python_code", "slot": "signal", "name": "LLM Python 代码", "input": "UniverseSnapshot", "output": "SignalSnapshot", "config_schema": {"strategy_params": "object"}},
    {"component_id": "research.announcements", "slot": "research", "name": "公告与财报研究", "input": "SignalSnapshot", "output": "ResearchAssessment", "config_schema": {"top_k": "integer"}},
    {"component_id": "macro.regime_placeholder", "slot": "macro", "name": "宏观状态（待接数据）", "input": "SignalSnapshot", "output": "MacroRegimeSnapshot", "config_schema": {}, "available": False},
    {"component_id": "portfolio.top_n_equal_weight", "slot": "portfolio", "name": "Top-N 风险权重组合", "input": "SignalSnapshot", "output": "TargetPortfolio", "config_schema": {"top_n": "integer", "hold_rank_buffer": "integer", "exposure_mode": "fixed|dynamic", "target_gross_exposure": "number", "minimum_exposure": "number", "neutral_exposure": "number", "maximum_exposure": "number"}},
    {"component_id": "risk.portfolio_limits", "slot": "risk", "name": "组合约束风控", "input": "TargetPortfolio", "output": "RiskDecision", "config_schema": {"max_position_weight": "number", "max_industry_weight": "number", "max_pair_correlation": "number"}},
    {"component_id": "execution.paper_review", "slot": "execution", "name": "A股模拟交易执行", "input": "RiskDecision", "output": "OrderPlan", "config_schema": {"schedule": "daily", "run_time": "HH:MM", "auto_run": "boolean", "approval_mode": "manual", "market": "CN_A"}},
)

_DEFAULT_LIMITS = {
    "max_position_weight": 0.12,
    "max_industry_weight": 0.2,
    "max_pair_correlation": 0.85,
}

_DYNAMIC_EXPOSURE = {
    "exposure_mode": "dynamic",
    "target_gross_exposure": 0.5,
    "minimum_exposure": 0.2,
    "neutral_exposure": 0.5,
    "maximum_exposure": 0.8,
    "bullish_breadth_threshold": 0.6,
    "bearish_breadth_threshold": 0.4,
    "high_volatility_threshold": 0.5,
}

_PAPER_EXECUTION = {
    "schedule": "daily",
    "run_time": "18:10",
    "auto_run": True,
    "approval_mode": "manual",
    "market": "CN_A",
    "commission_rate": 0.0003,
    "minimum_commission": 5.0,
    "stamp_duty_rate": 0.0005,
    "transfer_fee_rate": 0.00001,
    "slippage_rate": 0.0005,
    "lot_size": 100,
    "max_volume_participation": 0.1,
}


def _template(template_id: str, name: str, signal: str, research: bool, exposure: float) -> dict:
    dynamic = signal == "signal.lightgbm_shadow"
    portfolio = {
        "top_n": 5 if dynamic else 30,
        "hold_rank_buffer": 30 if dynamic else 60,
        **(
            _DYNAMIC_EXPOSURE
            if dynamic
            else {"exposure_mode": "fixed", "target_gross_exposure": exposure}
        ),
    }
    signal_config = ({
        "factor_weights": {
            "return_20d": 0.25, "return_60d": 0.30,
            "volatility_60d": -0.20, "max_drawdown_250d": 0.15,
            "range_position_52w": 0.10,
        }
    } if signal == "signal.multifactor_linear" else {})
    return {
        "template_id": template_id, "name": name,
        "description": "结构化白名单策略，可编译、验证并发布为不可变版本。",
        "modules": {
            "universe": {"component_id": "universe.csi300", "enabled": True, "config": {"pool_id": "csi300"}},
            "signal": {"component_id": signal, "enabled": True, "config": signal_config},
            "research": {"component_id": "research.announcements", "enabled": research, "config": {"top_k": 5}},
            "macro": {"component_id": "macro.regime_placeholder", "enabled": False, "config": {}},
            "portfolio": {"component_id": "portfolio.top_n_equal_weight", "enabled": True, "config": portfolio},
            "risk": {"component_id": "risk.portfolio_limits", "enabled": True, "config": dict(_DEFAULT_LIMITS)},
            "execution": {"component_id": "execution.paper_review", "enabled": True, "config": dict(_PAPER_EXECUTION)},
        },
    }


TEMPLATES = (
    _template("lightgbm_top30", "LightGBM 纯 Top-N", "signal.lightgbm_shadow", False, 0.5),
    _template("lightgbm_research", "LightGBM + 公告研究", "signal.lightgbm_shadow", True, 0.5),
    _template("linear_top30", "线性多因子纯策略", "signal.multifactor_linear", False, 0.8),
    _template("linear_research", "线性多因子 + 公告研究", "signal.multifactor_linear", True, 0.8),
    _template("python_code", "LLM Python 代码策略", "signal.python_code", False, 0.8),
)


class ComponentRegistry:
    def __init__(self) -> None:
        self._items = {item["component_id"]: item for item in COMPONENTS}

    def get(self, component_id: str) -> dict | None:
        item = self._items.get(component_id)
        return deepcopy(item) if item else None

    def list(self) -> list[dict]:
        return deepcopy(list(COMPONENTS))

    def templates(self) -> list[dict]:
        return deepcopy([
            item for item in TEMPLATES if item["template_id"] != "python_code"
        ])

    def template(self, template_id: str) -> dict:
        item = next((item for item in TEMPLATES if item["template_id"] == template_id), None)
        if item is None:
            raise KeyError("策略模板不存在")
        return deepcopy(item)
