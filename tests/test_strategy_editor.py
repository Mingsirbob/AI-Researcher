import sqlite3
import asyncio
import json

import pytest

from app.core.migrations import applied_migrations
from app.core.sqlite_store import SQLiteStore
from app.market.repository import StockRepository
from app.paper.service import PaperExecutionService
from app.quant.store import QuantStore
from app.research.store import ResearchStore
from app.strategy.service import StrategyService
from app.strategy.code_runtime import CodeStrategyRuntime, validate_strategy_source


CODE_SOURCE = """def generate_signals(context):
    rows = []
    for item in context["factors"]:
        momentum = item.get("return_20d")
        if momentum is not None:
            rows.append({"security_code": item["security_code"], "score": float(momentum), "reason": "20日动量"})
    return rows
"""


class FakeStrategyGenerator:
    async def generate(self, **kwargs):
        return {
            "name": kwargs["name"],
            "description": kwargs["requirement"],
            "source_code": CODE_SOURCE,
            "generation_meta": {"provider": "fake", "model": "test"},
        }


def _services(tmp_path):
    price_db = tmp_path / "prices.db"
    with sqlite3.connect(price_db):
        pass
    research = ResearchStore(tmp_path / "state.db", tmp_path / "documents")
    quant = QuantStore(tmp_path / "quant.db", research)
    with quant.connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS factor_evaluation_run "
            "(evaluation_id TEXT PRIMARY KEY, status TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS factor_backtest_run "
            "(backtest_id TEXT PRIMARY KEY, status TEXT NOT NULL)"
        )
    strategy = StrategyService(quant, strategy_run_root=tmp_path / "strategy_runs")
    paper = PaperExecutionService(
        StockRepository(price_db), research,
        paper_store=SQLiteStore(tmp_path / "paper.db"),
        quant_store=quant, strategy_service=strategy,
    )
    return quant, strategy, paper


def test_draft_compile_and_immutable_publish_without_evidence_gate(tmp_path):
    quant, service, _ = _services(tmp_path)
    draft = service.create_draft(
        name="线性 Top30", description="结构化策略", template_id="linear_top30"
    )
    assert service.compile(draft["draft_id"])["status"] == "valid"
    version = service.publish(draft["draft_id"])
    assert version["status"] == "published"
    runtime = service.runtime_strategy(version["strategy_version_id"])
    assert runtime["signal_source"] == "multifactor_linear"
    assert runtime["config"]["top_n"] == 30
    with pytest.raises(ValueError, match="不可修改"):
        service.update_draft(draft["draft_id"], draft["definition"])
    assert "0033_compact_quant_strategy" in applied_migrations(quant.connect)


def test_account_strategy_name_resolves_published_hash(tmp_path):
    quant, service, paper = _services(tmp_path)
    draft = service.create_draft(
        name="模型与公告", description="研究过滤", template_id="lightgbm_research"
    )
    version = service.publish(draft["draft_id"])
    account = paper.create_account("策略账户", 1_000_000, version["name"])
    resolved = paper.account(account["account_id"])
    assert resolved["strategy_name"] == version["name"]
    assert resolved["strategy"]["compiled_hash"] == version["compiled_hash"]
    assert resolved["strategy"]["research_enabled"] is True


def test_system_lightgbm_strategy_is_stored_and_auto_deployed(tmp_path):
    _, service, paper = _services(tmp_path)
    version = service.version(service.system_lightgbm_version_id)
    account = paper.create_account("自动策略账户", 1_000_000, version["name"])

    resolved = paper.account(account["account_id"])
    strategy = resolved["strategy"]
    artifact_path = (
        tmp_path / "strategy_runs" / version["draft_id"] / "v1" / "strategy.json"
    )
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))

    assert resolved["strategy_name"] == version["name"]
    assert version["source_path"] == str(artifact_path)
    assert artifact["strategy_version_id"] == version["strategy_version_id"]
    assert artifact["compiled_hash"] == version["compiled_hash"]
    assert artifact["compiled_plan"] == version["compiled_plan"]
    assert strategy["signal_source"] == "current_shadow"
    assert strategy["config"]["hold_rank_buffer"] == 30
    assert strategy["config"]["exposure_mode"] == "dynamic"
    assert strategy["config"]["auto_run"] is True
    assert strategy["config"]["market"] == "CN_A"


def test_structured_strategy_rejects_corrupted_artifact(tmp_path):
    _, service, _ = _services(tmp_path)
    version = service.version(service.system_lightgbm_version_id)
    artifact_path = (
        tmp_path / "strategy_runs" / version["draft_id"] / "v1" / "strategy.json"
    )
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["compiled_plan"]["name"] = "tampered"
    artifact_path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="编译哈希校验失败"):
        service.runtime_strategy(version["strategy_version_id"])


def test_service_restart_backfills_structured_strategy_artifact(tmp_path):
    research = ResearchStore(tmp_path / "state.db", tmp_path / "documents")
    quant = QuantStore(tmp_path / "quant.db", research)
    original = StrategyService(quant)
    version_id = original.system_lightgbm_version_id
    assert original.version(version_id)["source_path"] is None

    restarted = StrategyService(quant, strategy_run_root=tmp_path / "strategy_runs")
    version = restarted.version(version_id)

    assert version["source_path"] is not None
    assert (
        tmp_path / "strategy_runs" / version["draft_id"] / "v1" / "strategy.json"
    ).is_file()
    assert restarted.runtime_strategy(version_id)["strategy_id"] == version_id


def test_code_strategy_validation_and_restricted_execution():
    assert validate_strategy_source(CODE_SOURCE)["status"] == "valid"
    invalid = validate_strategy_source("import os\ndef generate_signals(context):\n    return []")
    assert invalid["status"] == "invalid"
    assert any(item["code"] == "forbidden_syntax" for item in invalid["errors"])

    result = CodeStrategyRuntime().execute(CODE_SOURCE, {
        "universe": [{"security_code": "000001.SZ"}],
        "factors": [{"security_code": "000001.SZ", "return_20d": 0.12}],
    })
    assert result == [{"security_code": "000001.SZ", "score": 0.12, "reason": "20日动量"}]


def test_generated_code_strategy_is_versioned_and_written_to_run_dir(tmp_path):
    price_db = tmp_path / "prices.db"
    with sqlite3.connect(price_db):
        pass
    research = ResearchStore(tmp_path / "state.db", tmp_path / "documents")
    quant = QuantStore(tmp_path / "quant.db", research)
    service = StrategyService(
        quant,
        strategy_run_root=tmp_path / "strategy_runs",
        generator=FakeStrategyGenerator(),
    )
    draft = asyncio.run(service.generate_draft(
        requirement="按20日动量从高到低选择股票",
        name="动量代码策略",
        pool_id="csi300",
        filter_pipeline_id="factor_quality_passed",
        params={"lookback": 20},
    ))
    assert draft["strategy_kind"] == "python_code"
    assert draft["source_code"] == CODE_SOURCE
    assert service.compile(draft["draft_id"])["status"] == "valid"

    version = service.publish(draft["draft_id"])
    source_path = tmp_path / "strategy_runs" / draft["draft_id"] / "v1" / "strategy.py"
    assert source_path.read_text(encoding="utf-8").strip() == CODE_SOURCE.strip()
    assert version["source_path"] == str(source_path)
    runtime = service.runtime_strategy(version["strategy_version_id"])
    assert runtime["signal_source"] == "python_code"
    assert runtime["config"]["pool_id"] == "csi300"
    assert runtime["config"]["filter_pipeline_id"] == "factor_quality_passed"


def test_account_can_select_published_strategy_by_name_at_creation(tmp_path):
    _, service, paper = _services(tmp_path)
    draft = service.create_draft(
        name="创建即绑定", description="账户版本选择", template_id="linear_top30"
    )
    version = service.publish(draft["draft_id"])
    account = paper.create_account("版本账户", 1_000_000, version["name"])
    assert account["strategy_name"] == version["name"]
    assert account["strategy"]["strategy_id"] == version["strategy_version_id"]
