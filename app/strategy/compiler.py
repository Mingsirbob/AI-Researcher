from __future__ import annotations

from app.core.primitives import canonical_hash

from .registry import ComponentRegistry


REQUIRED_SLOTS = ("universe", "signal", "portfolio", "risk", "execution")
SLOT_ORDER = ("universe", "signal", "research", "macro", "portfolio", "risk", "execution")


class StrategyCompiler:
    def __init__(self, registry: ComponentRegistry) -> None:
        self.registry = registry

    def compile(self, definition: dict) -> dict:
        errors: list[dict] = []
        modules = definition.get("modules") or {}
        plan = []
        for slot in SLOT_ORDER:
            module = modules.get(slot)
            if slot in REQUIRED_SLOTS and (not module or not module.get("enabled", True)):
                errors.append({"slot": slot, "message": "必需模块未启用"})
                continue
            if not module or not module.get("enabled", True):
                continue
            component = self.registry.get(str(module.get("component_id", "")))
            if component is None:
                errors.append({"slot": slot, "message": "组件不在注册表白名单中"})
                continue
            if component["slot"] != slot:
                errors.append({"slot": slot, "message": "组件与模块槽位不匹配"})
            if component.get("available", True) is False:
                errors.append({"slot": slot, "message": "组件的数据合同尚未就绪"})
            config = module.get("config") or {}
            self._validate_config(slot, config, errors)
            plan.append({"slot": slot, "component_id": component["component_id"], "config": config, "input": component["input"], "output": component["output"]})
        compiled = {"compiler_version": "strategy-compiler-v1", "name": definition.get("name"), "description": definition.get("description", ""), "plan": plan}
        return {"status": "valid" if not errors else "invalid", "errors": errors, "compiled_plan": compiled, "compiled_hash": canonical_hash(compiled)}

    @staticmethod
    def _validate_config(slot: str, config: dict, errors: list[dict]) -> None:
        def error(message: str) -> None:
            errors.append({"slot": slot, "message": message})

        if slot == "portfolio":
            top_n = int(config.get("top_n", 0) or 0)
            buffer_size = int(config.get("hold_rank_buffer", 0) or 0)
            if top_n < 1:
                error("Top-N 必须为正数")
            if buffer_size < top_n:
                error("持仓排名缓冲必须大于等于 Top-N")
            mode = config.get("exposure_mode", "fixed")
            if mode not in {"fixed", "dynamic"}:
                error("仓位模式必须是 fixed 或 dynamic")
            if mode == "fixed":
                exposure = float(config.get("target_gross_exposure", 0))
                if not 0 < exposure <= 1:
                    error("固定仓位必须在 0 到 1 之间")
            else:
                values = [
                    float(config.get(name, -1))
                    for name in ("minimum_exposure", "neutral_exposure", "maximum_exposure")
                ]
                if not (0 <= values[0] <= values[1] <= values[2] <= 1):
                    error("动态仓位必须满足 0 <= 最低 <= 中性 <= 最高 <= 1")
        if slot == "execution":
            if config.get("schedule") != "daily":
                error("当前模拟盘只支持 daily 调度")
            if config.get("approval_mode", "manual") != "manual":
                error("模拟盘订单必须使用 manual 人工审批模式")
            run_time = str(config.get("run_time", ""))
            try:
                hours, minutes = (int(value) for value in run_time.split(":"))
                valid_time = 0 <= hours <= 23 and 0 <= minutes <= 59
            except (TypeError, ValueError):
                valid_time = False
            if not valid_time:
                error("自动运行时间必须使用 HH:MM")
