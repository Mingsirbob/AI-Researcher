import asyncio
import json

import httpx

from app.config import Settings
from app.llm import (
    LLMRuntime,
    generate_ai_report,
    generate_document_answer,
    generate_financial_change_template,
    generate_research_assessment_draft,
)
from app.runtime_events import RunContext, RuntimeEventStore, bind_run_context


def analysis_fixture():
    return {
        "security": {"code": "300750.SZ", "name": "宁德时代"},
        "as_of": "2026-07-20",
        "coverage": {"start": "2020-01-02", "end": "2026-07-20", "observations": 100},
        "metrics": {"trend": "震荡"},
        "evidence": [{"id": "ev-price", "value": "376.43"}],
        "claims": [],
        "uncertainties": ["仅含行情"],
    }


def valid_response(request: httpx.Request) -> httpx.Response:
    report = {
        "executive_summary": "摘要",
        "observed_changes": ["变化"],
        "claims": [
            {
                "statement": "收盘价为376.43",
                "claim_type": "fact",
                "confidence": 1.0,
                "evidence_ids": ["ev-price"],
                "counter_evidence": [],
                "invalidating_conditions": [],
            }
        ],
        "counter_view": ["反方"],
        "uncertainties": ["仅含行情"],
        "next_checks": ["检查公告"],
    }
    return httpx.Response(
        200,
        request=request,
        json={
            "choices": [{"message": {"content": json.dumps(report, ensure_ascii=False)}}],
            "usage": {"total_tokens": 10},
        },
    )


def llm_settings(**overrides):
    values = {
        "llm_base_url": "https://llm.example/v1",
        "llm_api_key": "secret",
        "quick_model": "test-model",
        "llm_max_attempts": 3,
        "llm_backoff_seconds": 0.001,
        "llm_circuit_failure_threshold": 3,
        "llm_circuit_recovery_seconds": 60,
    }
    values.update(overrides)
    return Settings(**values)


def test_llm_retries_503_then_succeeds():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, request=request)
        return valid_response(request)

    settings = llm_settings()
    runtime = LLMRuntime(settings)
    report, meta = asyncio.run(
        generate_ai_report(
            analysis_fixture(),
            settings,
            "quick",
            runtime=runtime,
            transport=httpx.MockTransport(handler),
        )
    )
    assert report["claims"][0]["evidence_ids"] == ["ev-price"]
    assert meta["resilience"] == {"attempts": 2, "retried": True}
    assert runtime.status()["retries"] == 1
    assert runtime.status()["state"] == "closed"


def test_llm_runtime_events_are_correlated_and_do_not_store_prompt_or_key(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, request=request)
        return valid_response(request)

    settings = llm_settings(llm_api_key="key-that-must-not-be-persisted")
    event_store = RuntimeEventStore(tmp_path / "state.db")
    event_store.prepare_run(
        root_run_id="run-llm",
        task_key="test",
        contract_version="v1",
        contract_hash="hash-llm",
        input_payload={},
    )
    context = RunContext(event_store, "run-llm", step_run_id="step-llm")
    with bind_run_context(context):
        asyncio.run(
            generate_ai_report(
                analysis_fixture(),
                settings,
                "quick",
                runtime=LLMRuntime(settings),
                transport=httpx.MockTransport(handler),
            )
        )

    events = [event for event in event_store.events("run-llm") if event["call_id"]]
    assert [event["event_type"] for event in events] == [
        "tool_started", "tool_retrying", "tool_completed"
    ]
    assert len({event["call_id"] for event in events}) == 1
    assert {event["step_run_id"] for event in events} == {"step-llm"}
    serialized = json.dumps(events, ensure_ascii=False)
    assert "key-that-must-not-be-persisted" not in serialized
    assert "请基于以下受控证据" not in serialized


def test_llm_does_not_retry_authentication_error():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, request=request)

    settings = llm_settings()
    try:
        asyncio.run(
            generate_ai_report(
                analysis_fixture(),
                settings,
                "quick",
                runtime=LLMRuntime(settings),
                transport=httpx.MockTransport(handler),
            )
        )
    except RuntimeError as exc:
        assert "authentication_or_permission" in str(exc)
    else:
        raise AssertionError("401 必须失败")
    assert len(calls) == 1


def test_llm_timeouts_open_circuit():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("read timed out", request=request)

    settings = llm_settings(llm_circuit_failure_threshold=2)
    runtime = LLMRuntime(settings)
    try:
        asyncio.run(
            generate_ai_report(
                analysis_fixture(),
                settings,
                "quick",
                runtime=runtime,
                transport=httpx.MockTransport(handler),
            )
        )
    except RuntimeError as exc:
        assert "attempts=2" in str(exc)
    else:
        raise AssertionError("连续超时必须失败")
    assert len(calls) == 2
    assert runtime.status()["state"] == "open"
    assert runtime.status()["timeouts"] == 2


def test_document_assistant_accepts_only_existing_evidence_ids():
    evidence = [
        {
            "id": "ev-doc-1",
            "value": "营业收入为100亿元。",
            "citation": {"page_start": 12, "sha256": "a" * 64},
        }
    ]

    def handler(request):
        answer = {
            "status": "answered",
            "answer": "报告披露营业收入为100亿元。",
            "claims": [{"statement": "营业收入为100亿元。", "evidence_ids": ["ev-doc-1"]}],
            "limitations": [],
            "follow_up_questions": [],
        }
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}}]},
        )

    answer, meta = asyncio.run(
        generate_document_answer(
            security={"code": "300750.SZ", "name": "宁德时代"},
            question="营业收入是多少？",
            as_of="2026-07-20",
            evidence=evidence,
            settings=llm_settings(),
            runtime=LLMRuntime(llm_settings()),
            transport=httpx.MockTransport(handler),
        )
    )

    assert answer["status"] == "answered"
    assert answer["claims"][0]["evidence_ids"] == ["ev-doc-1"]
    assert meta["resilience"]["attempts"] == 1


def test_financial_change_template_requires_fixed_sections_and_valid_citations():
    evidence = [{"id": "ev-doc-1", "value": "营业收入同比增长10%。"}]

    def handler(request):
        template = {
            "status": "answered",
            "period_summary": "2025年度与上年同期",
            "overall_assessment": "经营规模增长，其余栏目证据未覆盖。",
            "sections": [
                {"key": "operating_scale", "label": "经营规模", "status": "supported", "direction": "improved", "summary": "营业收入增长。", "evidence_ids": ["ev-doc-1"]},
                {"key": "profitability", "label": "盈利质量", "status": "not_covered", "direction": "unknown", "summary": "未覆盖", "evidence_ids": []},
                {"key": "cash_and_capex", "label": "现金流与资本开支", "status": "not_covered", "direction": "unknown", "summary": "未覆盖", "evidence_ids": []},
                {"key": "balance_and_working_capital", "label": "资产负债与营运", "status": "not_covered", "direction": "unknown", "summary": "未覆盖", "evidence_ids": []},
                {"key": "shareholder_returns", "label": "股东回报", "status": "not_covered", "direction": "unknown", "summary": "未覆盖", "evidence_ids": []},
            ],
            "limitations": ["只覆盖已提供证据"],
            "next_checks": [],
        }
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": json.dumps(template, ensure_ascii=False)}}]},
        )

    template, _ = asyncio.run(
        generate_financial_change_template(
            security={"code": "300750.SZ", "name": "宁德时代"},
            as_of="2026-07-20",
            evidence=evidence,
            settings=llm_settings(),
            runtime=LLMRuntime(llm_settings()),
            transport=httpx.MockTransport(handler),
        )
    )

    assert len(template["sections"]) == 5
    assert template["sections"][0]["evidence_ids"] == ["ev-doc-1"]


def research_assessment_inputs():
    evidence = {
        "items": [
            {
                "id": "ev-fundamental-1",
                "value": "公司披露营业收入同比增长10%。",
                "citation": {"page_start": 12, "sha256": "a" * 64},
            }
        ]
    }
    snapshot = {
        "security": {"code": "300750.SZ", "name": "宁德时代"},
        "as_of": "2026-07-20",
        "coverage": {"evidence_count": 1},
    }
    return evidence, snapshot


def test_research_assessment_accepts_only_existing_evidence_ids():
    evidence_pack, company_snapshot = research_assessment_inputs()

    def handler(request):
        assessment = {
            "fundamental_outlook": "positive",
            "evidence_confidence": 0.8,
            "fundamental_evidence": [
                {
                    "statement": "公司披露营业收入同比增长10%。",
                    "evidence_ids": ["ev-fundamental-1"],
                }
            ],
            "material_negatives": [],
            "catalysts": [],
            "invalidating_conditions": ["后续定期报告不再维持收入增长"],
            "limitations": ["仅覆盖当前证据包"],
        }
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": json.dumps(assessment, ensure_ascii=False)}}]},
        )

    assessment, meta = asyncio.run(
        generate_research_assessment_draft(
            evidence_pack=evidence_pack,
            company_snapshot=company_snapshot,
            settings=llm_settings(),
            runtime=LLMRuntime(llm_settings()),
            transport=httpx.MockTransport(handler),
        )
    )

    assert assessment["fundamental_evidence"][0]["evidence_ids"] == ["ev-fundamental-1"]
    assert meta["resilience"]["attempts"] == 1


def test_research_assessment_rejects_unknown_evidence_ids():
    evidence_pack, company_snapshot = research_assessment_inputs()

    def handler(request):
        assessment = {
            "fundamental_outlook": "positive",
            "evidence_confidence": 0.8,
            "fundamental_evidence": [
                {
                    "statement": "模型虚构的基本面事实。",
                    "evidence_ids": ["ev-does-not-exist"],
                }
            ],
            "material_negatives": [],
            "catalysts": [],
            "invalidating_conditions": [],
            "limitations": [],
        }
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": json.dumps(assessment, ensure_ascii=False)}}]},
        )

    settings = llm_settings(llm_max_attempts=1)
    try:
        asyncio.run(
            generate_research_assessment_draft(
                evidence_pack=evidence_pack,
                company_snapshot=company_snapshot,
                settings=settings,
                runtime=LLMRuntime(settings),
                transport=httpx.MockTransport(handler),
            )
        )
    except RuntimeError as exc:
        assert "category=contract" in str(exc)
        assert "不存在的证据" in str(exc)
    else:
        raise AssertionError("研究评估引用不存在的 Evidence ID 时必须失败")
