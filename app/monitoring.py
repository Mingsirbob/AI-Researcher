from __future__ import annotations

from .primitives import canonical_hash


VERDICTS = ("增强", "维持", "减弱", "证伪", "无法判断")


def run_artifacts(run: dict) -> tuple[dict, dict]:
    artifacts = {item["artifact_type"]: item["payload"] for item in run.get("artifacts", [])}
    evidence_pack = artifacts.get("evidence_pack")
    company_snapshot = artifacts.get("company_snapshot")
    if evidence_pack is None or company_snapshot is None:
        raise ValueError("研究运行缺少 Evidence Pack 或 Company Snapshot")
    return evidence_pack, company_snapshot


def evidence_business_key(item: dict) -> str:
    financial_fact = item.get("financial_fact")
    if financial_fact:
        return f"financial:{financial_fact['metric_code']}"
    if item.get("category") in {"market_fact", "calculation"}:
        return f"market:{item['id']}"
    citation = item.get("citation", {})
    announcement_id = citation.get("announcement_id")
    if announcement_id:
        return f"announcement:{announcement_id}:{citation.get('page_start', '')}"
    return f"evidence:{item['id']}"


def evidence_fingerprint(item: dict) -> str:
    financial_fact = item.get("financial_fact")
    if financial_fact:
        payload = {
            key: financial_fact.get(key)
            for key in (
                "metric_code",
                "period_end",
                "comparison_period_end",
                "current_value",
                "comparison_value",
                "change_pct",
                "extraction_version",
            )
        }
    else:
        citation = item.get("citation", {})
        payload = {
            "category": item.get("category"),
            "label": item.get("label"),
            "value": item.get("value"),
            "as_of": item.get("as_of"),
            "document_sha256": citation.get("sha256"),
            "page_start": citation.get("page_start"),
        }
    return canonical_hash(payload)


def _evidence_summary(item: dict | None) -> dict | None:
    if item is None:
        return None
    fact = item.get("financial_fact")
    return {
        "evidence_id": item["id"],
        "label": item.get("label"),
        "value": item.get("value"),
        "as_of": item.get("as_of"),
        "source": item.get("source"),
        "metric_code": fact.get("metric_code") if fact else None,
        "period_end": fact.get("period_end") if fact else None,
    }


def compare_claim(claim: dict, current_snapshot: dict, current_evidence: list[dict]) -> dict:
    current_claim = next(
        (
            item
            for item in current_snapshot.get("claims", [])
            if item.get("id") == claim["source_claim_id"]
        ),
        None,
    )
    current_by_id = {item["id"]: item for item in current_evidence}
    current_items = [
        current_by_id[evidence_id]
        for evidence_id in (current_claim or {}).get("evidence_ids", [])
        if evidence_id in current_by_id
    ]
    baseline_by_key = {
        evidence_business_key(item): item for item in claim["baseline_evidence"]
    }
    current_by_key = {evidence_business_key(item): item for item in current_items}
    changes = []
    for key in sorted(set(baseline_by_key) | set(current_by_key)):
        baseline = baseline_by_key.get(key)
        current = current_by_key.get(key)
        if baseline is None:
            change_type = "added"
        elif current is None:
            change_type = "removed"
        elif evidence_fingerprint(baseline) != evidence_fingerprint(current):
            change_type = "changed"
        else:
            continue
        changes.append(
            {
                "business_key": key,
                "change_type": change_type,
                "baseline": _evidence_summary(baseline),
                "current": _evidence_summary(current),
            }
        )

    if current_claim is None:
        suggested_verdict = "无法判断"
        rationale = "当前运行中没有同一来源 Claim，不能自动判断论点方向。"
    elif changes:
        suggested_verdict = "无法判断"
        rationale = "相关证据发生变化，程序只报告差异，不推断增强、减弱或证伪。"
    elif current_claim["statement"] == claim["statement"]:
        suggested_verdict = "维持"
        rationale = "当前确定性 Claim 与基线表述及相关证据一致。"
    else:
        suggested_verdict = "无法判断"
        rationale = "当前确定性 Claim 的表述发生变化，程序只报告差异，不推断增强、减弱或证伪。"
    return {
        "claim_id": claim["claim_id"],
        "source_claim_id": claim["source_claim_id"],
        "baseline_run_id": claim["baseline_run_id"],
        "suggested_verdict": suggested_verdict,
        "rationale": rationale,
        "changes": changes,
    }


def build_monitor_evaluations(
    *,
    thesis: dict,
    baseline_run: dict,
    current_run: dict,
    claims: list[dict],
) -> list[dict]:
    if baseline_run["security_code"] != thesis["security_code"]:
        raise ValueError("基线运行与 Thesis 证券不一致")
    if current_run["security_code"] != thesis["security_code"]:
        raise ValueError("当前运行与 Thesis 证券不一致")
    if baseline_run["workflow_version"] != current_run["workflow_version"]:
        raise ValueError("工作流版本不一致，不能直接比较")
    if current_run["status"] not in {"completed", "completed_with_gaps"}:
        raise ValueError("当前研究运行尚未成功完成")
    if current_run["as_of"] < baseline_run["as_of"]:
        raise ValueError("当前运行的截止日早于基线")
    current_pack, current_snapshot = run_artifacts(current_run)
    return [
        compare_claim(claim, current_snapshot, current_pack["items"]) for claim in claims
    ]
