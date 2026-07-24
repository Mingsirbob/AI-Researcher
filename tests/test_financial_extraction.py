from app.financial_extraction import (
    build_financial_change_template,
    extract_financial_facts,
    identify_report_period,
)


def test_identify_report_period_distinguishes_flow_and_stock_comparisons():
    annual = identify_report_period("某公司2025年年度报告")
    quarter = identify_report_period("某公司 2026 年第三季度报告")

    assert annual["period_end"] == "2025-12-31"
    assert annual["comparison_flow_period_end"] == "2024-12-31"
    assert quarter["period_end"] == "2026-09-30"
    assert quarter["comparison_flow_period_end"] == "2025-09-30"
    assert quarter["comparison_stock_period_end"] == "2025-12-31"


def test_extract_financial_facts_normalizes_units_and_calculates_with_decimal():
    result = extract_financial_facts(
        document_id="doc-1",
        title="某公司2025年年度报告",
        sha256="a" * 64,
        pages=[
            """单位：万元
主要会计数据和财务指标
营业收入 36,201,347.59 40,091,770.46 -9.70%
归属于上市公司股东的净利润 5,000,000.00 4,000,000.00 25.00%
经营活动产生的现金流量净额 (1,200.00) 1,000.00 -220.00%
基本每股收益（元/股） 12.50 10.00 25.00%"""
        ],
    )
    facts = {item["metric_code"]: item for item in result["facts"]}

    assert result["report"]["period_label"] == "2025年年度"
    assert facts["operating_revenue"]["current_value"] == "362013475900"
    assert facts["operating_revenue"]["change_pct"] == "-9.7038"
    assert facts["net_profit_parent"]["change_pct"] == "25"
    assert facts["operating_cash_flow"]["current_value"] == "-12000000"
    assert facts["operating_cash_flow"]["change_pct"] == "-220"
    assert facts["basic_eps"]["normalized_unit"] == "元/股"


def test_extract_rejects_unknown_unit_and_multi_comparison_rows():
    result = extract_financial_facts(
        document_id="doc-2",
        title="某公司2025年年度报告",
        sha256="b" * 64,
        pages=[
            """主要会计数据和财务指标
营业收入 100 90 -10%
归属于上市公司股东的净利润 100 90 80 11%"""
        ],
    )

    assert result["facts"] == []
    assert any("无法确认单位" in issue for issue in result["issues"])


def test_quarterly_extraction_uses_same_period_for_flows_and_year_end_for_stocks():
    result = extract_financial_facts(
        document_id="doc-q3",
        title="某公司2026年第三季度报告",
        sha256="c" * 64,
        pages=["单位：元\n营业收入 120 100 20%\n资产总计 500 450 11.11%"],
    )
    facts = {item["metric_code"]: item for item in result["facts"]}

    assert facts["operating_revenue"]["comparison_period_end"] == "2025-09-30"
    assert facts["total_assets"]["comparison_period_end"] == "2025-12-31"


def test_q3_rejects_quarter_and_ytd_values_mixed_on_one_headline_row():
    result = extract_financial_facts(
        document_id="doc-q3-multi-period",
        title="某公司2025年第三季度报告",
        sha256="d" * 64,
        pages=[
            """单位：元
基本每股收益（元/股） 4.10 37.23% 11.02 34.56%
合并年初到报告期末利润表
基本每股收益 11.02 8.19"""
        ],
    )

    fact = next(item for item in result["facts"] if item["metric_code"] == "basic_eps")
    assert fact["current_value"] == "11.02"
    assert fact["comparison_value"] == "8.19"
    assert fact["extraction_version"] == "financial-regex-v2"


def test_q3_accepts_enumerated_eps_row_in_formal_statement():
    result = extract_financial_facts(
        document_id="doc-q3-enumerated",
        title="某公司2025年第三季度报告",
        sha256="e" * 64,
        pages=["单位：千元\n（一）基本每股收益 11.02 8.19"],
    )

    fact = next(item for item in result["facts"] if item["metric_code"] == "basic_eps")
    assert fact["current_value"] == "11.02"
    assert fact["comparison_value"] == "8.19"
    assert fact["source_text"] == "（一）基本每股收益 11.02 8.19"


def test_build_template_uses_program_facts_and_marks_missing_sections():
    evidence = [
        {
            "id": "ev-fin-1",
            "value": "营业收入：120亿元；比较期 100亿元，变化 20%",
            "financial_fact": {
                "metric_code": "operating_revenue",
                "metric_label": "营业收入",
                "statement_type": "flow",
                "period_label": "2025年年度",
                "period_end": "2025-12-31",
                "comparison_period_end": "2024-12-31",
                "change_pct": "20",
            },
        }
    ]

    template = build_financial_change_template(evidence)

    assert template["status"] == "answered"
    assert template["sections"][0]["direction"] == "improved"
    assert template["sections"][0]["evidence_ids"] == ["ev-fin-1"]
    assert template["sections"][1]["status"] == "not_covered"
