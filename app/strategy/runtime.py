from __future__ import annotations


class StrategyRuntime:
    """Adapts a compiled plan to the existing portfolio engine contract."""

    @staticmethod
    def paper_strategy(version: dict) -> dict:
        plan = version["compiled_plan"]["plan"]
        by_slot = {step["slot"]: step for step in plan}
        signal = by_slot["signal"]["component_id"]
        config = {}
        for slot in ("universe", "signal", "research", "macro", "portfolio", "risk", "execution"):
            config.update(by_slot.get(slot, {}).get("config") or {})
        config["components"] = {
            slot: step["component_id"] for slot, step in by_slot.items()
        }
        signal_source = {
            "signal.multifactor_linear": "multifactor_linear",
            "signal.python_code": "python_code",
        }.get(signal, "current_shadow")
        return {
            "strategy_id": version["strategy_version_id"],
            "version": str(version["version"]),
            "name": version["name"],
            "description": version.get("description", ""),
            "signal_source": signal_source,
            "config": config,
            "research_enabled": "research" in by_slot,
            "macro_enabled": "macro" in by_slot,
            "execution_enabled": "execution" in by_slot,
            "compiled_hash": version["compiled_hash"],
            "strategy_kind": version.get("strategy_kind", "structured"),
            "source_code": version.get("source_code"),
            "source_path": version.get("source_path"),
        }
