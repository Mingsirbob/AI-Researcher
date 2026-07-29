from app.research.analysis import analyze_stock
from app.research.financials import build_financial_change_template
from app.integrations.llm import deterministic_report
from app.research.workflow import build_company_snapshot, build_evidence_pack


def _analysis():
    rows = [
        {
            "time": f"2025-{(index // 28) + 1:02d}-{(index % 28) + 1:02d}",
            "open": 10 + index * 0.02,
            "high": 10.2 + index * 0.02,
            "low": 9.8 + index * 0.02,
            "close": 10 + index * 0.02,
            "vwap": 10 + index * 0.02,
            "volume": 1_000_000 + index * 1000,
        }
        for index in range(300)
    ]
    return analyze_stock("300750.SZ", "宁德时代", rows)


def _financial_evidence():
    fact = {
        "fact_id": "fact-1",
        "metric_code": "operating_revenue",
        "metric_label": "营业收入",
        "statement_type": "flow",
        "period_start": "2025-01-01",
        "period_end": "2025-12-31",
        "comparison_period_start": "2024-01-01",
        "comparison_period_end": "2024-12-31",
        "normalized_unit": "元",
        "current_value": "120000000",
        "comparison_value": "100000000",
        "absolute_change": "20000000",
        "change_pct": "20",
        "source_page": 1,
        "source_text": "营业收入 12000 10000",
        "extraction_version": "financial-regex-v1",
        "confidence": "high",
        "period_label": "2025年年度",
    }
    return [
        {
            "id": "ev-fin-fact-1",
            "category": "financial_fact",
            "label": "营业收入",
            "value": "营业收入：1.20亿元，比较期1.00亿元，变化20.00%",
            "as_of": "2026-03-20",
            "source": "/api/documents/doc-1/file#page=1",
            "method": "程序化抽取",
            "citation": {"document_id": "doc-1", "page_start": 1, "sha256": "a" * 64},
            "financial_fact": fact,
        }
    ]


def _quant_context():
    return {
        "status": "supported",
        "security": {"code": "300750.SZ", "name": "宁德时代"},
        "as_of": "2026-07-20",
        "snapshot_id": "factor-1",
        "factor_version": "price-liquidity-v1",
        "source": "candidate_pool",
        "quality_status": "passed",
        "quality_reasons": [],
        "universe": {"total": 4897, "passed": 4118, "excluded": 779},
        "factors": {"return_60d": 0.12},
        "ranks": {"return_60d": {"rank": 412, "universe": 4118, "percentile": 90.02}},
        "limitations": [],
    }


def test_company_snapshot_combines_market_document_and_financial_evidence():
    analysis = _analysis()
    financial_evidence = _financial_evidence()
    document_evidence = [
        {
            "id": "ev-doc-1",
            "category": "external_document",
            "label": "公告原文 p.1",
            "value": "公司披露年度报告。",
            "as_of": "2026-03-20",
            "source": "/api/documents/doc-1/file#page=1",
            "method": "PDF 按页提取",
            "citation": {"document_id": "doc-1", "page_start": 1, "sha256": "a" * 64},
        }
    ]
    announcements = [
        {
            "announcement_id": "ann-1",
            "title": "2025年年度报告",
            "published_at": "2026-03-20",
            "report_date": "2026-03-20",
            "status": "parsed",
            "current_document_id": "doc-1",
            "text_layer_status": "ok",
        }
    ]
    pack = build_evidence_pack(
        analysis=analysis,
        financial_evidence=financial_evidence,
        document_evidence=document_evidence,
        announcements=announcements,
        extraction={"documents_seen": 1, "reports_identified": 1},
    )
    template = build_financial_change_template(financial_evidence)
    narrative = deterministic_report({**analysis, "evidence": pack["items"]})
    snapshot = build_company_snapshot(
        analysis=analysis,
        evidence_pack=pack,
        announcements=announcements,
        financial_template=template,
        narrative=narrative,
        generation_mode="deterministic_fallback",
        generation_meta={},
        quant_context=_quant_context(),
    )

    assert snapshot["status"] == "complete"
    assert snapshot["coverage"]["financials"]["status"] == "supported"
    assert snapshot["coverage"]["quant"]["status"] == "supported"
    assert snapshot["quant_context"]["snapshot_id"] == "factor-1"
    assert snapshot["financial_change"]["sections"][0]["status"] == "supported"
    assert "ev-fin-fact-1" in snapshot["evidence_ids"]
    assert any(claim["id"] == "cl-fin-operating_scale" for claim in snapshot["claims"])


def test_company_snapshot_marks_missing_sources_without_inventing_content():
    analysis = _analysis()
    pack = build_evidence_pack(
        analysis=analysis,
        financial_evidence=[],
        document_evidence=[],
        announcements=[],
        extraction={"documents_seen": 0, "reports_identified": 0},
    )
    empty_template = {
        "status": "insufficient_evidence",
        "period_summary": "未形成可比较期间",
        "overall_assessment": "没有财务事实",
        "sections": [],
        "limitations": ["财务证据不足"],
        "next_checks": [],
    }
    narrative = deterministic_report(analysis)
    snapshot = build_company_snapshot(
        analysis=analysis,
        evidence_pack=pack,
        announcements=[],
        financial_template=empty_template,
        narrative=narrative,
        generation_mode="deterministic_fallback",
        generation_meta={},
    )

    assert snapshot["status"] == "partial"
    assert snapshot["coverage"]["announcements"]["status"] == "not_covered"
    assert snapshot["coverage"]["financials"]["status"] == "not_covered"
    assert snapshot["coverage"]["quant"]["status"] == "not_covered"
    assert any("公告元数据不能支持内容结论" in item for item in snapshot["limitations"])
