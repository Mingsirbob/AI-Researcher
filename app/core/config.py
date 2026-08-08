from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env(ROOT / ".env")


def _positive_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class Settings:
    stock_db: Path = ROOT / "data" / "stock_data.db"
    stock_pool_db: Path = ROOT / "data" / "stock_pool.db"
    stock_qfq_db: Path = ROOT / "data" / "stock_data_qfq.db"
    stock_hfq_db: Path = ROOT / "data" / "stock_data_hfq.db"
    state_db: Path = ROOT / "data" / "research_state.db"
    paper_db: Path = ROOT / "data" / "paper_trading.db"
    quant_db: Path = ROOT / "data" / "quant_research.db"
    document_root: Path = ROOT / "data" / "documents"
    model_artifact_root: Path = ROOT / "data" / "model_artifacts"
    current_shadow_root: Path = ROOT / "data" / "current_shadow"
    runtime_temp_root: Path = ROOT / "data" / "runtime_tmp"
    strategy_run_root: Path = ROOT / "data" / "strategy_runs"
    llm_base_url: str = os.getenv("TRADINGAGENTS_LLM_BACKEND_URL", "")
    llm_api_key: str = os.getenv("OPENAI_COMPATIBLE_API_KEY", "")
    quick_model: str = os.getenv("TRADINGAGENTS_QUICK_THINK_LLM", "")
    deep_model: str = os.getenv("TRADINGAGENTS_DEEP_THINK_LLM", "")
    ifind_username: str = os.getenv("IFIND_USER", "")
    ifind_password: str = os.getenv("IFIND_PASSWORD", "")
    ifind_timeout_seconds: float = _positive_float("IFIND_TIMEOUT_SECONDS", 30.0)
    ifind_max_attempts: int = _positive_int("IFIND_MAX_ATTEMPTS", 3)
    ifind_backoff_seconds: float = _positive_float("IFIND_BACKOFF_SECONDS", 0.5)
    ifind_circuit_failure_threshold: int = _positive_int("IFIND_CIRCUIT_FAILURE_THRESHOLD", 3)
    ifind_circuit_recovery_seconds: float = _positive_float("IFIND_CIRCUIT_RECOVERY_SECONDS", 60.0)
    llm_connect_timeout_seconds: float = _positive_float("LLM_CONNECT_TIMEOUT_SECONDS", 10.0)
    llm_read_timeout_seconds: float = _positive_float("LLM_READ_TIMEOUT_SECONDS", 90.0)
    llm_max_attempts: int = _positive_int("LLM_MAX_ATTEMPTS", 3)
    llm_backoff_seconds: float = _positive_float("LLM_BACKOFF_SECONDS", 0.75)
    llm_circuit_failure_threshold: int = _positive_int("LLM_CIRCUIT_FAILURE_THRESHOLD", 3)
    llm_circuit_recovery_seconds: float = _positive_float("LLM_CIRCUIT_RECOVERY_SECONDS", 60.0)
    strategy_execution_timeout_seconds: float = _positive_float(
        "STRATEGY_EXECUTION_TIMEOUT_SECONDS", 3.0
    )
    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.quick_model)

    @property
    def ifind_credentials_configured(self) -> bool:
        return bool(self.ifind_username and self.ifind_password)


settings = Settings()
