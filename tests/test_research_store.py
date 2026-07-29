import sqlite3

from app.research.store import ResearchStore


def create_price_db(path):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE stock_300750_SZ (
                time TEXT PRIMARY KEY, open REAL, high REAL, low REAL,
                close REAL, vwap REAL, volume REAL
            )
            """
        )
        conn.executemany(
            "INSERT INTO stock_300750_SZ VALUES (?, 1, 1, 1, 1, 1, 1)",
            [("2020-01-02",), ("2026-07-20",)],
        )


def test_security_bootstrap_does_not_invent_listing_date(tmp_path):
    stock_db = tmp_path / "stocks.db"
    create_price_db(stock_db)
    store = ResearchStore(tmp_path / "state.db", tmp_path / "documents")

    result = store.bootstrap_securities(stock_db)
    security = store.security("300750.SZ")

    assert result["inserted"] == 1
    assert security["first_price_date"] == "2020-01-02"
    assert security["listing_date"] is None
    assert security["security_name"] == "宁德时代"


def test_announcement_document_evidence_has_page_and_hash(tmp_path):
    stock_db = tmp_path / "stocks.db"
    create_price_db(stock_db)
    store = ResearchStore(tmp_path / "state.db", tmp_path / "documents")
    store.bootstrap_securities(stock_db)
    announcement_id = store.upsert_announcements(
        "300750.SZ",
        [
            {
                "sequence": "123.0",
                "title": "年度报告公告",
                "date": "2026-07-01",
                "published_at": "2026-07-01 18:00:00",
                "url": "https://ft.10jqka.com.cn/report.pdf",
            }
        ],
    )[0]
    pdf_path = tmp_path / "documents" / "aa" / ("a" * 64 + ".pdf")
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"%PDF-test")
    document_id, created = store.save_document(
        announcement_id=announcement_id,
        sha256="a" * 64,
        file_path=f"documents/aa/{'a' * 64}.pdf",
        source_url="https://ft.10jqka.com.cn/report.pdf",
        content_type="application/pdf",
        byte_size=9,
        pages=["第一页原文", "第二页包含资本开支信息"],
        chunks=[
            {
                "chunk_index": 0,
                "page_start": 1,
                "page_end": 1,
                "section_path": None,
                "text": "第一页原文",
            },
            {
                "chunk_index": 1,
                "page_start": 2,
                "page_end": 2,
                "section_path": None,
                "text": "第二页包含资本开支信息",
            },
        ],
        extraction_method="pypdf",
        text_layer_status="ok",
    )

    evidence = store.evidence_for_security(
        "300750.SZ", query="资本开支", as_of="2026-07-20", limit=5
    )
    assert created is True
    assert evidence[0]["citation"]["document_id"] == document_id
    assert evidence[0]["citation"]["page_start"] == 2
    assert evidence[0]["citation"]["sha256"] == "a" * 64
    assert evidence[0]["source"].endswith(f"/file#page=2")
    assert store.resolve_document_path(document_id) == pdf_path.resolve()
    assert store.stats() == {
        "securities": 1,
        "announcements": 1,
        "documents": 1,
        "parsed_documents": 1,
        "ocr_required_documents": 0,
        "evidence_chunks": 2,
        "research_interactions": 0,
        "evidence_snapshots": 0,
        "financial_reports": 0,
        "financial_facts": 0,
        "research_runs": 0,
        "research_artifacts": 0,
        "factor_snapshots": 0,
            "research_candidates": 0,
            "evidence_acceptance_runs": 0,
            "model_runs": 0,
            "shadow_signals": 0,
            "model_validations": 0,
            "current_shadow_signals": 0,
            "decision_outcomes": 0,
            "fts_available": True,
        }


def test_document_hash_is_idempotent(tmp_path):
    stock_db = tmp_path / "stocks.db"
    create_price_db(stock_db)
    store = ResearchStore(tmp_path / "state.db", tmp_path / "documents")
    store.bootstrap_securities(stock_db)
    announcement_id = store.upsert_announcements(
        "300750.SZ", [{"sequence": "1", "title": "公告", "url": "https://ft.10jqka.com.cn/a.pdf"}]
    )[0]
    kwargs = dict(
        announcement_id=announcement_id,
        sha256="b" * 64,
        file_path=f"documents/bb/{'b' * 64}.pdf",
        source_url="https://ft.10jqka.com.cn/a.pdf",
        content_type="application/pdf",
        byte_size=10,
        pages=["原文内容足够长"],
        chunks=[{"chunk_index": 0, "page_start": 1, "page_end": 1, "text": "原文内容足够长"}],
        extraction_method="pypdf",
        text_layer_status="ok",
    )
    first_id, first_created = store.save_document(**kwargs)
    second_id, second_created = store.save_document(**kwargs)
    assert first_created is True
    assert second_created is False
    assert first_id == second_id


def test_assistant_evidence_ranks_financial_terms_and_deduplicates_documents(tmp_path):
    stock_db = tmp_path / "stocks.db"
    create_price_db(stock_db)
    store = ResearchStore(tmp_path / "state.db", tmp_path / "documents")
    store.bootstrap_securities(stock_db)
    announcement_id = store.upsert_announcements(
        "300750.SZ",
        [
            {
                "sequence": "annual-1",
                "title": "2025年年度报告",
                "date": "2026-03-20",
                "published_at": "2026-03-20 18:00:00",
                "url": "https://ft.10jqka.com.cn/annual.pdf",
            }
        ],
    )[0]
    store.save_document(
        announcement_id=announcement_id,
        sha256="c" * 64,
        file_path=f"documents/cc/{'c' * 64}.pdf",
        source_url="https://ft.10jqka.com.cn/annual.pdf",
        content_type="application/pdf",
        byte_size=10,
        pages=["营业收入增长，净利润增长。", "营业收入分产品情况。", "研发投入情况。"],
        chunks=[
            {"chunk_index": 0, "page_start": 1, "page_end": 1, "text": "营业收入增长，净利润增长。"},
            {"chunk_index": 1, "page_start": 2, "page_end": 2, "text": "营业收入分产品情况。"},
            {"chunk_index": 2, "page_start": 3, "page_end": 3, "text": "研发投入情况。"},
        ],
        extraction_method="pypdf",
        text_layer_status="ok",
    )

    evidence = store.assistant_evidence(
        "300750.SZ",
        "最近财报的营收和利润如何？",
        as_of="2026-07-20",
        scope="financial_reports",
        limit=8,
    )

    assert len(evidence) == 2
    assert evidence[0]["citation"]["title"] == "2025年年度报告"
    assert evidence[0]["citation"]["page_start"] == 1
    assert all(item["citation"]["sha256"] == "c" * 64 for item in evidence)


def test_assistant_evidence_returns_empty_for_unmatched_question(tmp_path):
    stock_db = tmp_path / "stocks.db"
    create_price_db(stock_db)
    store = ResearchStore(tmp_path / "state.db", tmp_path / "documents")
    store.bootstrap_securities(stock_db)

    assert store.assistant_evidence("300750.SZ", "海外市占率是多少？") == []


def test_pending_documents_can_prioritize_financial_reports(tmp_path):
    stock_db = tmp_path / "stocks.db"
    create_price_db(stock_db)
    store = ResearchStore(tmp_path / "state.db", tmp_path / "documents")
    store.bootstrap_securities(stock_db)
    ids = store.upsert_announcements(
        "300750.SZ",
        [
            {"sequence": "1", "title": "董事会决议公告", "url": "https://ft.10jqka.com.cn/1.pdf"},
            {"sequence": "2", "title": "2025年年度报告", "url": "https://ft.10jqka.com.cn/2.pdf"},
        ],
    )

    pending = store.pending_documents(ids, 5, document_scope="financial_reports")

    assert [item["title"] for item in pending] == ["2025年年度报告"]


def test_interaction_persists_immutable_evidence_snapshot(tmp_path):
    stock_db = tmp_path / "stocks.db"
    create_price_db(stock_db)
    store = ResearchStore(tmp_path / "state.db", tmp_path / "documents")
    store.bootstrap_securities(stock_db)
    evidence = [
        {
            "id": "ev-doc-1",
            "value": "营业收入同比增长10%。",
            "source": "/api/documents/doc-1/file#page=12",
            "citation": {
                "document_id": "doc-1",
                "chunk_id": "chunk-1",
                "page_start": 12,
                "sha256": "d" * 64,
                "title": "2025年年度报告",
            },
        }
    ]
    saved = store.save_interaction(
        code="300750.SZ",
        interaction_type="document_qa",
        question="收入如何？",
        scope="financial_reports",
        as_of="2026-07-20",
        mode="ai",
        status="answered",
        result={"status": "answered", "answer": "收入增长", "claims": []},
        evidence=evidence,
        meta={"model": "test-model"},
    )
    evidence[0]["value"] = "调用方随后修改的文本"

    detail = store.interaction(saved["interaction_id"])
    history = store.list_interactions("300750.SZ")

    assert detail["evidence"][0]["value"] == "营业收入同比增长10%。"
    assert detail["evidence"][0]["citation"]["sha256"] == "d" * 64
    assert detail["evidence_snapshot_hash"] == saved["evidence_snapshot_hash"]
    assert history[0]["evidence_count"] == 1
    assert store.stats()["research_interactions"] == 1
    assert store.stats()["evidence_snapshots"] == 1


def test_research_run_persists_immutable_versioned_artifacts(tmp_path):
    stock_db = tmp_path / "stocks.db"
    create_price_db(stock_db)
    store = ResearchStore(tmp_path / "state.db", tmp_path / "documents")
    store.bootstrap_securities(stock_db)

    run_id = store.start_research_run(
        code="300750.SZ", as_of="2026-07-20", workflow_version="company-research-v1"
    )
    evidence = {"items": [{"id": "ev-price", "value": "100.00"}]}
    artifact = store.save_research_artifact(
        run_id=run_id,
        artifact_type="evidence_pack",
        schema_version="evidence-pack-v1",
        status="partial",
        payload=evidence,
    )
    evidence["items"][0]["value"] = "调用方随后修改"
    store.finish_research_run(
        run_id,
        status="completed_with_gaps",
        evidence_snapshot_hash=artifact["snapshot_hash"],
        evidence_count=1,
    )

    saved = store.research_run(run_id)
    assert saved["status"] == "completed_with_gaps"
    assert saved["evidence_snapshot_hash"] == artifact["snapshot_hash"]
    assert saved["artifacts"][0]["payload"]["items"][0]["value"] == "100.00"
    assert store.list_research_runs("300750.SZ")[0]["run_id"] == run_id
    assert store.stats()["research_runs"] == 1
    assert store.stats()["research_artifacts"] == 1


def test_financial_template_evidence_combines_fixed_topics(tmp_path):
    stock_db = tmp_path / "stocks.db"
    create_price_db(stock_db)
    store = ResearchStore(tmp_path / "state.db", tmp_path / "documents")
    store.bootstrap_securities(stock_db)
    announcement_id = store.upsert_announcements(
        "300750.SZ",
        [{"sequence": "annual", "title": "2025年年度报告", "date": "2026-03-20", "url": "https://ft.10jqka.com.cn/a.pdf"}],
    )[0]
    store.save_document(
        announcement_id=announcement_id,
        sha256="e" * 64,
        file_path=f"documents/ee/{'e' * 64}.pdf",
        source_url="https://ft.10jqka.com.cn/a.pdf",
        content_type="application/pdf",
        byte_size=10,
        pages=["营业收入与净利润增长", "经营现金流改善，资本开支增加", "存货与应收账款变化"],
        chunks=[
            {"chunk_index": 0, "page_start": 1, "page_end": 1, "text": "营业收入与净利润增长"},
            {"chunk_index": 1, "page_start": 2, "page_end": 2, "text": "经营现金流改善，资本开支增加"},
            {"chunk_index": 2, "page_start": 3, "page_end": 3, "text": "存货与应收账款变化"},
        ],
        extraction_method="pypdf",
        text_layer_status="ok",
    )

    evidence = store.financial_template_evidence("300750.SZ", as_of="2026-07-20")

    assert {item["citation"]["page_start"] for item in evidence} == {1, 2, 3}


def test_refresh_financial_facts_persists_period_values_and_source_page(tmp_path):
    stock_db = tmp_path / "stocks.db"
    create_price_db(stock_db)
    store = ResearchStore(tmp_path / "state.db", tmp_path / "documents")
    store.bootstrap_securities(stock_db)
    announcement_id = store.upsert_announcements(
        "300750.SZ",
        [
            {
                "sequence": "annual-facts",
                "title": "2025年年度报告",
                "date": "2026-03-20",
                "url": "https://ft.10jqka.com.cn/facts.pdf",
            }
        ],
    )[0]
    store.save_document(
        announcement_id=announcement_id,
        sha256="f" * 64,
        file_path=f"documents/ff/{'f' * 64}.pdf",
        source_url="https://ft.10jqka.com.cn/facts.pdf",
        content_type="application/pdf",
        byte_size=10,
        pages=["单位：万元\n主要会计数据和财务指标\n营业收入 12,000.00 10,000.00 20.00%"],
        chunks=[
            {
                "chunk_index": 0,
                "page_start": 1,
                "page_end": 1,
                "text": "单位：万元\n营业收入 12,000.00 10,000.00 20.00%",
            }
        ],
        extraction_method="pypdf",
        text_layer_status="ok",
    )

    refreshed = store.refresh_financial_facts("300750.SZ", "2026-07-20")
    evidence = store.financial_fact_evidence("300750.SZ", "2026-07-20")

    assert refreshed["reports_identified"] == 1
    assert refreshed["facts_extracted"] == 1
    assert evidence[0]["category"] == "financial_fact"
    assert evidence[0]["financial_fact"]["current_value"] == "120000000"
    assert evidence[0]["financial_fact"]["comparison_period_end"] == "2024-12-31"
    assert evidence[0]["citation"]["page_start"] == 1
    assert store.stats()["financial_reports"] == 1
    assert store.stats()["financial_facts"] == 1
