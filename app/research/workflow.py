from __future__ import annotations

from typing import Any


WORKFLOW_VERSION = "company-research-v2"
EVIDENCE_PACK_SCHEMA_VERSION = "evidence-pack-v1"
QUANT_CONTEXT_SCHEMA_VERSION = "quant-context-v1"
COMPANY_SNAPSHOT_SCHEMA_VERSION = "company-snapshot-v2"


def empty_financial_template(message: str, limitation: str) -> dict:
    labels = {
        "operating_scale": "经营规模",
        "profitability": "盈利质量",
        "cash_and_capex": "现金流与资本开支",
        "balance_and_working_capital": "资产负债与营运",
        "shareholder_returns": "股东回报",
    }
    return {
        "status": "insufficient_evidence",
        "period_summary": "未形成可比较期间",
        "overall_assessment": message,
        "sections": [
            {
                "key": key,
                "label": label,
                "status": "not_covered",
                "direction": "unknown",
                "summary": "当前证据未覆盖该栏目。",
                "evidence_ids": [],
            }
            for key, label in labels.items()
        ],
        "limitations": [limitation],
        "next_checks": [],
    }


def _unique_evidence(*groups: list[dict]) -> list[dict]:
    selected: list[dict] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            evidence_id = item["id"]
            if evidence_id in seen:
                continue
            seen.add(evidence_id)
            selected.append(item)
    return selected


def build_evidence_pack(
    *,
    analysis: dict,
    financial_evidence: list[dict],
    document_evidence: list[dict],
    announcements: list[dict],
    extraction: dict,
) -> dict:
    evidence = _unique_evidence(
        analysis["evidence"], financial_evidence, document_evidence
    )
    parsed_document_ids = {
        item.get("citation", {}).get("document_id")
        for item in document_evidence
        if item.get("citation", {}).get("document_id")
    }
    coverage = {
        "market": {
            **analysis["coverage"],
            "status": "supported",
        },
        "announcements": {
            "recent_items": len(announcements),
            "cited_documents": len(parsed_document_ids),
            "status": "supported" if document_evidence else "not_covered",
        },
        "financials": {
            "documents_seen": extraction.get("documents_seen", 0),
            "reports_identified": extraction.get("reports_identified", 0),
            "facts_extracted": len(financial_evidence),
            "status": "supported" if financial_evidence else "not_covered",
        },
    }
    return {
        "security": analysis["security"],
        "as_of": analysis["as_of"],
        "coverage": coverage,
        "items": evidence,
    }


def _announcement_items(announcements: list[dict]) -> list[dict]:
    items = []
    for item in announcements[:10]:
        items.append(
            {
                "announcement_id": item["announcement_id"],
                "title": item["title"],
                "published_at": item.get("published_at") or item.get("report_date"),
                "status": item["status"],
                "document_id": item.get("current_document_id"),
                "text_layer_status": item.get("text_layer_status"),
            }
        )
    return items


def _financial_claims(financial_template: dict) -> list[dict]:
    claims = []
    for section in financial_template["sections"]:
        if section["status"] != "supported" or not section["evidence_ids"]:
            continue
        claims.append(
            {
                "id": f"cl-fin-{section['key']}",
                "statement": section["summary"],
                "claim_type": "fact",
                "confidence": 0.95,
                "evidence_ids": section["evidence_ids"],
                "counter_evidence": [],
                "invalidating_conditions": [
                    "后续披露修订报告、会计差错更正或调整比较口径"
                ],
            }
        )
    return claims


def build_company_snapshot(
    *,
    analysis: dict,
    evidence_pack: dict,
    announcements: list[dict],
    financial_template: dict,
    narrative: dict,
    generation_mode: str,
    generation_meta: dict[str, Any],
    quant_context: dict | None = None,
) -> dict:
    quant_context = quant_context or {
        "status": "not_covered",
        "snapshot_id": None,
        "limitations": ["未提供全市场因子上下文。"],
    }
    coverage = {
        **evidence_pack["coverage"],
        "quant": {
            "status": quant_context["status"],
            "snapshot_id": quant_context.get("snapshot_id"),
            "as_of": quant_context.get("as_of"),
        },
    }
    gaps = [name for name, item in coverage.items() if item["status"] != "supported"]
    status = "complete" if not gaps else "partial"
    limitations = list(dict.fromkeys([
        *narrative.get("uncertainties", []),
        *financial_template.get("limitations", []),
        *quant_context.get("limitations", []),
    ]))
    next_checks = list(dict.fromkeys([
        *narrative.get("next_checks", []),
        *financial_template.get("next_checks", []),
    ]))
    if "announcements" in gaps:
        limitations.append("当前没有可引用的公告正文；公告元数据不能支持内容结论。")
        next_checks.append("手工同步并归档与研究问题相关的公告 PDF。")
    if "financials" in gaps:
        next_checks.append("归档定期报告并检查程序化财务事实提取结果。")

    claims = [*analysis["claims"], *_financial_claims(financial_template)]
    valid_evidence_ids = {item["id"] for item in evidence_pack["items"]}
    for claim in [*claims, *narrative.get("claims", [])]:
        if not set(claim.get("evidence_ids", [])).issubset(valid_evidence_ids):
            raise ValueError("公司研究快照包含无效 Evidence ID")

    return {
        "status": status,
        "security": analysis["security"],
        "as_of": analysis["as_of"],
        "workflow_version": WORKFLOW_VERSION,
        "generation": {
            "mode": generation_mode,
            "meta": generation_meta,
        },
        "executive_summary": narrative["executive_summary"],
        "observed_changes": narrative.get("observed_changes", []),
        "coverage": coverage,
        "market_state": {
            "metrics": analysis["metrics"],
            "claims": analysis["claims"],
        },
        "quant_context": quant_context,
        "financial_change": financial_template,
        "recent_announcements": _announcement_items(announcements),
        "claims": claims,
        "narrative_claims": narrative.get("claims", []),
        "counter_view": narrative.get("counter_view", []),
        "limitations": limitations,
        "next_checks": next_checks,
        "evidence_ids": [item["id"] for item in evidence_pack["items"]],
    }
