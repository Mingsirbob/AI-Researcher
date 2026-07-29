import sqlite3
from datetime import date, timedelta

import pytest

from app.market.repository import StockRepository
from app.quant.factor_lab import FactorLabService, evaluate_template
from app.research.store import ResearchStore


def create_price_db(path):
    with sqlite3.connect(path) as conn:
        for number, growth in (("000001_SZ", 0.001), ("000002_SZ", 0.002), ("600000_SH", -0.0005)):
            table = f"stock_{number}"
            conn.execute(
                f"""
                CREATE TABLE "{table}" (
                    time TEXT PRIMARY KEY, open REAL, high REAL, low REAL,
                    close REAL, vwap REAL, volume REAL
                )
                """
            )
            price = 10.0
            rows = []
            start = date(2025, 9, 1)
            for index in range(300):
                price *= 1 + growth
                rows.append(
                    (
                        (start + timedelta(days=index)).isoformat(), price * 0.995,
                        price * 1.01, price * 0.99, price, price,
                        1_000_000 + index * 1_000,
                    )
                )
            conn.executemany(f'INSERT INTO "{table}" VALUES (?, ?, ?, ?, ?, ?, ?)', rows)


def create_service(tmp_path):
    price_db = tmp_path / "prices.db"
    create_price_db(price_db)
    repository = StockRepository(price_db)
    store = ResearchStore(
        tmp_path / "state.db", tmp_path / "documents", retain_split_domains=True
    )
    store.bootstrap_securities(price_db)
    return FactorLabService(store, repository), store


def test_factor_lab_seeds_templates_versions_and_current_model_contract(tmp_path):
    service, _ = create_service(tmp_path)

    templates = service.templates()
    factors = service.list_factors()
    overview = service.overview()

    assert len(templates) == 9
    assert len(factors) == 11
    assert overview["status_counts"]["testing"] == 10
    assert overview["status_counts"]["approved"] == 1
    alpha = next(item for item in factors if item["factor_id"] == "alpha158_bundle")
    assert alpha["lifecycle_status"] == "approved"
    assert alpha["model_used"] == 0  # no imported model in this isolated store


def test_factor_versions_are_immutable_and_lifecycle_is_gated(tmp_path):
    service, _ = create_service(tmp_path)
    first = service.create_factor(
        factor_id="student_momentum",
        name="学生动量实验",
        description="验证不同窗口下的动量效果。",
        template_id="momentum",
        window=15,
        direction=None,
        owner="tester",
    )
    second = service.create_factor(
        factor_id="student_momentum",
        name="学生动量实验",
        description="保留旧版并创建新窗口。",
        template_id="momentum",
        window=30,
        direction=None,
        owner="tester",
    )

    assert first["version"] == 1
    assert second["version"] == 2
    assert first["formula_hash"] != second["formula_hash"]
    with pytest.raises(ValueError, match="完全相同"):
        service.create_factor(
            factor_id="student_momentum",
            name="学生动量实验",
            description="重复版本不应写入。",
            template_id="momentum",
            window=30,
            direction=None,
            owner="tester",
        )
    with pytest.raises(ValueError, match="不允许"):
        service.change_status(
            "student_momentum", 2, "approved", "tester", "不能跳过测试和Shadow"
        )
    testing = service.change_status(
        "student_momentum", 2, "testing", "tester", "开始历史测试"
    )
    assert testing["lifecycle_status"] == "testing"


def test_factor_snapshot_is_ranked_and_reused_for_same_contract(tmp_path):
    service, _ = create_service(tmp_path)

    first = service.generate_snapshot("2026-06-27")
    second = service.generate_snapshot("2026-06-27")
    values = service.snapshot_values(first["snapshot_id"], "momentum_20d")

    assert first["status"] == "completed"
    assert first["factor_count"] == 10
    assert first["security_count"] == 3
    assert first["value_count"] == 30
    assert first["coverage"] == 1
    assert second["snapshot_id"] == first["snapshot_id"]
    assert second["reused"] is True
    assert values["total"] == 3
    assert values["items"][0]["security_code"] == "000002.SZ"
    assert values["items"][0]["cross_section_rank"] == 1


def test_formula_engine_only_executes_registered_templates():
    rows = [
        {"close": 10 + index, "volume": 100 + index, "vwap": 10 + index}
        for index in range(30)
    ]
    assert evaluate_template("momentum", 20, rows) > 0
    with pytest.raises(KeyError):
        evaluate_template("arbitrary_python", 20, rows)
