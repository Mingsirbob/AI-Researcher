from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


EXTRACTION_VERSION = "financial-regex-v2"


@dataclass(frozen=True)
class MetricDefinition:
    code: str
    label: str
    aliases: tuple[str, ...]
    statement_type: str
    default_unit: str | None = None


METRICS = (
    MetricDefinition("operating_revenue", "营业收入", ("营业收入",), "flow"),
    MetricDefinition(
        "net_profit_parent",
        "归母净利润",
        ("归属于上市公司股东的净利润", "归属于母公司所有者的净利润"),
        "flow",
    ),
    MetricDefinition(
        "operating_cash_flow",
        "经营活动现金流量净额",
        ("经营活动产生的现金流量净额",),
        "flow",
    ),
    MetricDefinition(
        "capital_expenditure_cash",
        "购建长期资产支付的现金",
        ("购建固定资产、无形资产和其他长期资产支付的现金",),
        "flow",
    ),
    MetricDefinition("research_and_development_expense", "研发费用", ("研发费用",), "flow"),
    MetricDefinition("total_assets", "总资产", ("资产总计", "总资产"), "stock"),
    MetricDefinition("total_liabilities", "负债合计", ("负债合计",), "stock"),
    MetricDefinition("inventory", "存货", ("存货",), "stock"),
    MetricDefinition("accounts_receivable", "应收账款", ("应收账款",), "stock"),
    MetricDefinition(
        "equity_parent",
        "归母净资产",
        ("归属于上市公司股东的净资产", "归属于母公司所有者权益合计"),
        "stock",
    ),
    MetricDefinition("basic_eps", "基本每股收益", ("基本每股收益",), "flow", "元/股"),
)

UNIT_SCALES = {
    "元": Decimal("1"),
    "千元": Decimal("1000"),
    "万元": Decimal("10000"),
    "亿元": Decimal("100000000"),
    "元/股": Decimal("1"),
}

NUMBER_TOKEN = re.compile(
    r"(?<![\dA-Za-z])(?:[-−]?\d[\d,]*(?:\.\d+)?|[（(][-−]?\d[\d,]*(?:\.\d+)?[）)])(?![\dA-Za-z])"
)
UNIT_PATTERN = re.compile(r"单位\s*[:：]\s*(?:人民币)?\s*(亿元|万元|千元|元)")


def _iso(year: int, month: int, day: int) -> str:
    return date(year, month, day).isoformat()


def identify_report_period(title: str, text_sample: str = "") -> dict | None:
    normalized = re.sub(r"\s+", "", f"{title}\n{text_sample[:2000]}")
    patterns = (
        (r"(20\d{2})年(?:第一季度|一季度)报告", "q1", 3, 31, "第一季度"),
        (r"(20\d{2})年半年度报告", "semiannual", 6, 30, "半年度"),
        (r"(20\d{2})年(?:第三季度|三季度)报告", "q3", 9, 30, "前三季度"),
        (r"(20\d{2})年年度报告", "annual", 12, 31, "年度"),
        (r"(20\d{2})年度(?:业绩快报|业绩预告)", "annual_performance", 12, 31, "年度"),
    )
    for pattern, report_type, month, day, label in patterns:
        match = re.search(pattern, normalized)
        if not match:
            continue
        year = int(match.group(1))
        return {
            "report_type": report_type,
            "fiscal_year": year,
            "period_label": f"{year}年{label}",
            "period_start": _iso(year, 1, 1),
            "period_end": _iso(year, month, day),
            "comparison_period_start": _iso(year - 1, 1, 1),
            "comparison_flow_period_end": _iso(year - 1, month, day),
            "comparison_stock_period_end": _iso(year - 1, 12, 31),
            "identification_method": "title_regex",
            "extraction_version": EXTRACTION_VERSION,
        }
    return None


def _page_unit(page_text: str) -> str | None:
    match = UNIT_PATTERN.search(page_text[:2500])
    return match.group(1) if match else None


def _decimal_token(token: str) -> Decimal | None:
    raw = token.strip().replace(",", "").replace("−", "-")
    negative = raw.startswith(("(", "（")) and raw.endswith((")", "）"))
    raw = raw.strip("()（）")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return -value if negative else value


def _decimal_string(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _row_values(line: str, alias: str) -> tuple[str, str] | None:
    match = re.match(
        rf"^(?:其中\s*[:：]?\s*)?{re.escape(alias)}(?:\s*[（(][^\d）)]{{0,16}}[）)])?\s+(.+)$",
        line,
    )
    if not match:
        return None
    remainder = match.group(1)
    # Q3 headline tables can contain quarter value/growth and YTD value/growth
    # on one row. Treating the two non-percent numbers as a comparable pair
    # silently mixes periods, so only a single percentage column is allowed.
    if len(re.findall(r"[-−]?\d[\d,]*(?:\.\d+)?\s*[%％]", remainder)) > 1:
        return None
    tokens = []
    for token_match in NUMBER_TOKEN.finditer(remainder):
        suffix = remainder[token_match.end() : token_match.end() + 1]
        if suffix in {"%", "％"}:
            continue
        tokens.append(token_match.group(0))
    if len(tokens) != 2:
        return None
    return tokens[0], tokens[1]


def _strip_row_prefix(line: str) -> str:
    """Remove common statement row enumerators without changing cited text."""
    return re.sub(
        r"^(?:[（(][一二三四五六七八九十0-9]+[）)]|[一二三四五六七八九十]+[、.．])\s*",
        "",
        line,
    )


def extract_financial_facts(
    *,
    document_id: str,
    title: str,
    sha256: str,
    pages: list[str],
) -> dict:
    report = identify_report_period(title, "\n".join(pages[:2]))
    if report is None:
        return {"report": None, "facts": [], "issues": ["无法识别报告期"]}

    candidates: dict[str, tuple[int, dict]] = {}
    issues: list[str] = []
    for page_number, page_text in enumerate(pages, start=1):
        page_unit = _page_unit(page_text)
        page_priority = 5 if "主要会计数据和财务指标" in page_text else 3 if page_number <= 20 else 1
        for raw_line in page_text.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            if not line or len(line) > 600:
                continue
            match_line = _strip_row_prefix(line)
            for metric in METRICS:
                matched_alias = next(
                    (alias for alias in metric.aliases if match_line.startswith(alias)), None
                )
                if matched_alias is None:
                    continue
                values = _row_values(match_line, matched_alias)
                if values is None:
                    continue
                unit = metric.default_unit or page_unit
                if unit not in UNIT_SCALES:
                    issues.append(f"p.{page_number} {metric.label}：无法确认单位")
                    continue
                current_raw, comparison_raw = values
                current = _decimal_token(current_raw)
                comparison = _decimal_token(comparison_raw)
                if current is None or comparison is None:
                    continue
                scale = UNIT_SCALES[unit]
                current_normalized = current * scale
                comparison_normalized = comparison * scale
                delta = current_normalized - comparison_normalized
                change_pct = None
                if comparison_normalized != 0:
                    change_pct = (
                        delta / abs(comparison_normalized) * Decimal("100")
                    ).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
                comparison_period_end = (
                    report["comparison_stock_period_end"]
                    if metric.statement_type == "stock"
                    else report["comparison_flow_period_end"]
                )
                source_hash = sha256_text(line)
                fact_id = sha256_text(
                    f"{document_id}|{metric.code}|{report['period_end']}|{comparison_period_end}"
                )[:32]
                fact = {
                    "fact_id": fact_id,
                    "document_id": document_id,
                    "metric_code": metric.code,
                    "metric_label": metric.label,
                    "statement_type": metric.statement_type,
                    "period_start": report["period_start"],
                    "period_end": report["period_end"],
                    "comparison_period_start": report["comparison_period_start"],
                    "comparison_period_end": comparison_period_end,
                    "raw_current_value": current_raw,
                    "raw_comparison_value": comparison_raw,
                    "display_unit": unit,
                    "unit_scale": _decimal_string(scale),
                    "normalized_unit": "元/股" if unit == "元/股" else "元",
                    "current_value": _decimal_string(current_normalized),
                    "comparison_value": _decimal_string(comparison_normalized),
                    "absolute_change": _decimal_string(delta),
                    "change_pct": _decimal_string(change_pct) if change_pct is not None else None,
                    "source_page": page_number,
                    "source_text": line,
                    "source_text_sha256": source_hash,
                    "document_sha256": sha256,
                    "extraction_method": "strict_same_line_two_value_regex",
                    "extraction_version": EXTRACTION_VERSION,
                    "confidence": "high",
                }
                previous = candidates.get(metric.code)
                if previous is None or page_priority > previous[0]:
                    candidates[metric.code] = (page_priority, fact)

    facts = [item[1] for item in candidates.values()]
    facts.sort(key=lambda item: item["metric_code"])
    return {"report": report, "facts": facts, "issues": list(dict.fromkeys(issues))[:50]}


def format_fact_change(fact: dict) -> str:
    current = Decimal(fact["current_value"])
    comparison = Decimal(fact["comparison_value"])
    unit = fact["normalized_unit"]
    divisor = Decimal("100000000") if unit == "元" else Decimal("1")
    display_unit = "亿元" if unit == "元" else unit
    current_display = current / divisor
    comparison_display = comparison / divisor
    change = fact.get("change_pct")
    change_text = f"，变化 {Decimal(change).quantize(Decimal('0.01'))}%" if change is not None else ""
    return (
        f"{fact['metric_label']}：{_decimal_string(current_display.quantize(Decimal('0.01')))}{display_unit}；"
        f"比较期 {_decimal_string(comparison_display.quantize(Decimal('0.01')))}{display_unit}{change_text}"
    )


def build_financial_change_template(evidence: list[dict]) -> dict:
    facts_by_code = {
        item["financial_fact"]["metric_code"]: item for item in evidence if item.get("financial_fact")
    }
    section_specs = (
        ("operating_scale", "经营规模", ("operating_revenue",), True),
        (
            "profitability",
            "盈利质量",
            ("net_profit_parent", "basic_eps", "research_and_development_expense"),
            True,
        ),
        (
            "cash_and_capex",
            "现金流与资本开支",
            ("operating_cash_flow", "capital_expenditure_cash"),
            True,
        ),
        (
            "balance_and_working_capital",
            "资产负债与营运",
            ("total_assets", "total_liabilities", "inventory", "accounts_receivable", "equity_parent"),
            False,
        ),
        ("shareholder_returns", "股东回报", (), False),
    )
    sections = []
    for key, label, metric_codes, directional in section_specs:
        items = [facts_by_code[code] for code in metric_codes if code in facts_by_code]
        if not items:
            sections.append(
                {
                    "key": key,
                    "label": label,
                    "status": "not_covered",
                    "direction": "unknown",
                    "summary": "严格抽取规则未获得可比较数值。",
                    "evidence_ids": [],
                }
            )
            continue
        direction = "unknown"
        if directional:
            changes = [
                Decimal(item["financial_fact"]["change_pct"])
                for item in items
                if item["financial_fact"].get("change_pct") is not None
                and item["financial_fact"]["metric_code"] != "capital_expenditure_cash"
            ]
            if changes and all(value > 0 for value in changes):
                direction = "improved"
            elif changes and all(value < 0 for value in changes):
                direction = "weakened"
            elif changes and all(value == 0 for value in changes):
                direction = "stable"
            elif changes:
                direction = "mixed"
        sections.append(
            {
                "key": key,
                "label": label,
                "status": "supported",
                "direction": direction,
                "summary": "；".join(item["value"] for item in items),
                "evidence_ids": [item["id"] for item in items],
            }
        )

    facts = [item["financial_fact"] for item in evidence if item.get("financial_fact")]
    if not facts:
        raise ValueError("没有可用于模板的程序化财务事实")
    first = facts[0]
    flow_comparisons = sorted(
        {fact["comparison_period_end"] for fact in facts if fact["statement_type"] == "flow"}
    )
    stock_comparisons = sorted(
        {fact["comparison_period_end"] for fact in facts if fact["statement_type"] == "stock"}
    )
    comparisons = []
    if flow_comparisons:
        comparisons.append(f"流量比较期截至 {flow_comparisons[-1]}")
    if stock_comparisons:
        comparisons.append(f"时点比较期截至 {stock_comparisons[-1]}")
    missing = [section["label"] for section in sections if section["status"] == "not_covered"]
    return {
        "status": "answered",
        "period_summary": f"{first['period_label']}，{'；'.join(comparisons)}",
        "overall_assessment": f"程序从同表同行双值中提取并比较 {len(facts)} 项标准财务指标。",
        "sections": sections,
        "limitations": [
            "仅使用能够确认报告期、比较期、单位且同行恰有两组数值的披露；未覆盖跨行表格和调整前后多列口径。"
        ],
        "next_checks": [f"补充核验：{'、'.join(missing)}"] if missing else [],
    }
from app.core.primitives import sha256_text
