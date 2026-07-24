from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Callable

import httpx
from pydantic import ValidationError

from .config import Settings
from .resilience import CircuitBreaker, CircuitOpenError, backoff_seconds
from .runtime_events import (
    begin_tool_event,
    complete_tool_event,
    fail_tool_event,
    record_tool_retry,
)
from .schemas import AIReport, DocumentAnswer, FinancialChangeTemplate, ResearchAssessmentDraft


SYSTEM_PROMPT = """你是A股证据型研究编辑。你只能使用输入JSON中的受控证据，不得补充未提供的外部事实、公司基本面或新闻。
把事实与推断明确分开；每项claim必须引用输入中真实存在的evidence id；不得给出买入、卖出、目标价或未来价格预测。
公告标题只能证明文件已发布；只有带文档SHA-256和页码的公告原文证据才能支持内容结论。当证据只覆盖价格和成交量时，必须把这个局限写进uncertainties。输出必须是合法JSON，且完全符合给定结构。"""

DOCUMENT_ASSISTANT_PROMPT = """你是A股公告与财报证据助手。只能根据输入中的公告原文证据回答，不能使用记忆、常识或外部资料补充事实。
每项内容结论都必须出现在claims中，并引用一个或多个真实evidence id。不得把没有披露的内容推断为零或没有变化；不得给出买卖建议、目标价或未来价格预测。
如果证据无法直接回答问题，将status设为insufficient_evidence，清楚说明缺少什么，不得勉强作答。回答中的数字、期间和单位必须与证据一致。输出必须是合法JSON并完全符合给定结构。"""

FINANCIAL_CHANGE_PROMPT = """你是A股财报变化审阅助手。只能使用输入中的财报或业绩公告原文证据，按固定五个栏目生成变化模板。
不得自行计算同比、环比、比率或金额差；只有原文明确披露当期与比较期及变化时才能描述变化。每个supported栏目必须引用真实evidence id；证据不够时必须使用not_covered、unknown和空evidence_ids。
sections必须且只能各包含一次以下key：operating_scale、profitability、cash_and_capex、balance_and_working_capital、shareholder_returns。不得给出投资建议、目标价或未来价格预测。输出必须是合法JSON并完全符合给定结构。"""

RESEARCH_ASSESSMENT_PROMPT = """你是A股证据审计员。只能从输入的Evidence Pack和Company Snapshot提取结构化基本面状态，不得使用外部知识，不得输出买卖建议、准入结果、仓位或目标价。
fundamental_evidence、material_negatives和catalysts中的每一项都必须引用真实Evidence ID。公告标题只能证明公告存在，不能支持公告内容；重大负面与催化剂必须引用带原文和页码的证据或程序化财务事实。
severity表示已披露事实的重要程度，不表示价格预测。证据不足时必须使用fundamental_outlook=insufficient并降低evidence_confidence，不得把缺失信息当作没有风险。invalidating_conditions必须是未来可核验的研究失效条件。输出必须是合法JSON并完全符合给定结构。"""


class LLMRuntime:
    def __init__(self, settings: Settings):
        self.circuit = CircuitBreaker(
            "LLM",
            failure_threshold=settings.llm_circuit_failure_threshold,
            recovery_timeout_seconds=settings.llm_circuit_recovery_seconds,
        )
        self._lock = threading.Lock()
        self._metrics = {
            "requests": 0,
            "attempts": 0,
            "retries": 0,
            "timeouts": 0,
            "failures": 0,
            "last_error_category": None,
        }

    def record_attempt(self, *, attempt: int, category: str | None = None) -> None:
        with self._lock:
            self._metrics["attempts"] += 1
            if attempt > 1:
                self._metrics["retries"] += 1
            if category == "timeout":
                self._metrics["timeouts"] += 1
            if category:
                self._metrics["failures"] += 1
                self._metrics["last_error_category"] = category

    def record_request(self) -> None:
        with self._lock:
            self._metrics["requests"] += 1

    def status(self) -> dict:
        with self._lock:
            metrics = dict(self._metrics)
        return {**self.circuit.status(), **metrics}


_runtime_lock = threading.Lock()
_default_runtime: LLMRuntime | None = None


def get_llm_runtime(settings: Settings) -> LLMRuntime:
    global _default_runtime
    with _runtime_lock:
        if _default_runtime is None:
            _default_runtime = LLMRuntime(settings)
        return _default_runtime


def llm_resilience_status(settings: Settings) -> dict:
    return {
        **get_llm_runtime(settings).status(),
        "connect_timeout_seconds": settings.llm_connect_timeout_seconds,
        "read_timeout_seconds": settings.llm_read_timeout_seconds,
        "max_attempts": settings.llm_max_attempts,
        "backoff_seconds": settings.llm_backoff_seconds,
    }


def _payload(analysis: dict[str, Any]) -> dict[str, Any]:
    return {
        "security": analysis["security"],
        "as_of": analysis["as_of"],
        "coverage": analysis["coverage"],
        "metrics": analysis["metrics"],
        "evidence": analysis["evidence"],
        "deterministic_claims": analysis["claims"],
        "known_uncertainties": analysis["uncertainties"],
        "required_output_schema": AIReport.model_json_schema(),
    }


def _llm_error_category(exc: Exception) -> tuple[str, bool, bool]:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout", True, True
    if isinstance(exc, httpx.NetworkError):
        return "network", True, True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            return "rate_limit", True, True
        if status in {500, 502, 503, 504}:
            return "upstream", True, True
        if status in {401, 403}:
            return "authentication_or_permission", False, False
        return "request", False, False
    if isinstance(exc, (ValidationError, json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError)):
        return "contract", True, False
    return "internal", False, False


def _validate_response(raw: dict, analysis: dict[str, Any]) -> AIReport:
    content = raw["choices"][0]["message"]["content"]
    if content.startswith("```"):
        content = content.strip("`").removeprefix("json").strip()
    validated = AIReport.model_validate_json(content)
    valid_ids = {item["id"] for item in analysis["evidence"]}
    for claim in validated.claims:
        if not claim.evidence_ids or not set(claim.evidence_ids).issubset(valid_ids):
            raise ValueError("模型引用了不存在的证据")
    return validated


async def generate_ai_report(
    analysis: dict[str, Any],
    settings: Settings,
    depth: str,
    *,
    runtime: LLMRuntime | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[dict, dict]:
    if not settings.llm_configured:
        raise RuntimeError("LLM尚未配置")
    model = settings.deep_model if depth == "deep" and settings.deep_model else settings.quick_model
    request_body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "请基于以下受控证据生成研究报告：\n" + json.dumps(_payload(analysis), ensure_ascii=False),
            },
        ],
        "temperature": 0.15,
        "response_format": {"type": "json_object"},
    }
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    tool_name = "llm.generate_ai_report"
    runtime_call_id = begin_tool_event(tool_name, {"model": model, "depth": depth})
    active_runtime = runtime or get_llm_runtime(settings)
    active_runtime.record_request()
    try:
        active_runtime.circuit.before_call()
    except CircuitOpenError as exc:
        fail_tool_event(runtime_call_id, tool_name, exc, category="circuit_open")
        raise RuntimeError(str(exc)) from exc

    timeout = httpx.Timeout(
        connect=settings.llm_connect_timeout_seconds,
        read=settings.llm_read_timeout_seconds,
        write=min(30.0, settings.llm_read_timeout_seconds),
        pool=settings.llm_connect_timeout_seconds,
    )
    last_error: Exception | None = None
    attempts_used = 0
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        for attempt in range(1, settings.llm_max_attempts + 1):
            attempts_used = attempt
            try:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                    json=request_body,
                )
                response.raise_for_status()
                raw = response.json()
                validated = _validate_response(raw, analysis)
            except Exception as exc:
                last_error = exc
                category, retryable, counts_for_circuit = _llm_error_category(exc)
                active_runtime.record_attempt(attempt=attempt, category=category)
                if counts_for_circuit or active_runtime.circuit.status()["state"] == "half_open":
                    active_runtime.circuit.record_failure()
                if (
                    not retryable
                    or active_runtime.circuit.status()["state"] == "open"
                    or attempt >= settings.llm_max_attempts
                ):
                    break
                record_tool_retry(runtime_call_id, tool_name, attempt, category)
                await asyncio.sleep(
                    backoff_seconds(
                        attempt,
                        settings.llm_backoff_seconds,
                        settings.llm_backoff_seconds * 8,
                    )
                )
                continue

            active_runtime.record_attempt(attempt=attempt)
            active_runtime.circuit.record_success()
            meta = {
                "model": model,
                "provider": "openai_compatible",
                "usage": raw.get("usage", {}),
                "resilience": {"attempts": attempt, "retried": attempt > 1},
            }
            complete_tool_event(
                runtime_call_id, tool_name,
                {"model": model, "attempts": attempt, "usage": raw.get("usage", {})},
            )
            return validated.model_dump(), meta

    category, _, _ = _llm_error_category(last_error or RuntimeError("unknown"))
    failure = RuntimeError(
        f"LLM 调用失败（category={category}, attempts={attempts_used}）：{last_error}"
    )
    fail_tool_event(
        runtime_call_id, tool_name, last_error or failure,
        category=category, attempt=attempts_used,
    )
    raise failure from last_error


def _validate_document_answer(raw: dict, evidence: list[dict]) -> DocumentAnswer:
    content = raw["choices"][0]["message"]["content"]
    if content.startswith("```"):
        content = content.strip("`").removeprefix("json").strip()
    validated = DocumentAnswer.model_validate_json(content)
    valid_ids = {item["id"] for item in evidence}
    for claim in validated.claims:
        if not claim.evidence_ids or not set(claim.evidence_ids).issubset(valid_ids):
            raise ValueError("模型引用了不存在的公告证据")
    if validated.status == "answered" and not validated.claims:
        raise ValueError("已回答状态必须包含带引用的结论")
    return validated


def _validate_financial_change(raw: dict, evidence: list[dict]) -> FinancialChangeTemplate:
    content = raw["choices"][0]["message"]["content"]
    if content.startswith("```"):
        content = content.strip("`").removeprefix("json").strip()
    validated = FinancialChangeTemplate.model_validate_json(content)
    valid_ids = {item["id"] for item in evidence}
    required_keys = {
        "operating_scale",
        "profitability",
        "cash_and_capex",
        "balance_and_working_capital",
        "shareholder_returns",
    }
    if {section.key for section in validated.sections} != required_keys or len(validated.sections) != 5:
        raise ValueError("财报变化模板栏目不完整或重复")
    for section in validated.sections:
        if section.status == "supported":
            if not section.evidence_ids or not set(section.evidence_ids).issubset(valid_ids):
                raise ValueError("财报变化栏目引用了不存在的证据")
        elif section.evidence_ids:
            raise ValueError("未覆盖栏目不得附加证据引用")
    if validated.status == "answered" and not any(
        section.status == "supported" for section in validated.sections
    ):
        raise ValueError("已回答模板必须至少包含一个有证据栏目")
    return validated


def _validate_research_assessment(raw: dict, evidence: list[dict]) -> ResearchAssessmentDraft:
    content = raw["choices"][0]["message"]["content"]
    if content.startswith("```"):
        content = content.strip("`").removeprefix("json").strip()
    validated = ResearchAssessmentDraft.model_validate_json(content)
    valid_ids = {item["id"] for item in evidence}
    cited_items = [
        *validated.fundamental_evidence,
        *validated.material_negatives,
        *validated.catalysts,
    ]
    for item in cited_items:
        if not item.evidence_ids or not set(item.evidence_ids).issubset(valid_ids):
            raise ValueError("研究评估引用了不存在的证据")
    if validated.fundamental_outlook != "insufficient" and not validated.fundamental_evidence:
        raise ValueError("非证据不足状态必须包含基本面证据")
    return validated


async def _generate_evidence_json(
    request_body: dict,
    settings: Settings,
    validator: Callable[[dict], Any],
    *,
    runtime: LLMRuntime | None,
    transport: httpx.AsyncBaseTransport | None,
) -> tuple[dict, dict]:
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    operation = validator.__name__.removeprefix("_validate_")
    tool_name = f"llm.{operation}"
    runtime_call_id = begin_tool_event(
        tool_name, {"model": request_body.get("model"), "operation": operation}
    )
    active_runtime = runtime or get_llm_runtime(settings)
    active_runtime.record_request()
    try:
        active_runtime.circuit.before_call()
    except CircuitOpenError as exc:
        fail_tool_event(runtime_call_id, tool_name, exc, category="circuit_open")
        raise RuntimeError(str(exc)) from exc

    timeout = httpx.Timeout(
        connect=settings.llm_connect_timeout_seconds,
        read=settings.llm_read_timeout_seconds,
        write=min(30.0, settings.llm_read_timeout_seconds),
        pool=settings.llm_connect_timeout_seconds,
    )
    last_error: Exception | None = None
    attempts_used = 0
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        for attempt in range(1, settings.llm_max_attempts + 1):
            attempts_used = attempt
            try:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                    json=request_body,
                )
                response.raise_for_status()
                raw = response.json()
                validated = validator(raw)
            except Exception as exc:
                last_error = exc
                category, retryable, counts_for_circuit = _llm_error_category(exc)
                active_runtime.record_attempt(attempt=attempt, category=category)
                if counts_for_circuit or active_runtime.circuit.status()["state"] == "half_open":
                    active_runtime.circuit.record_failure()
                if (
                    not retryable
                    or active_runtime.circuit.status()["state"] == "open"
                    or attempt >= settings.llm_max_attempts
                ):
                    break
                record_tool_retry(runtime_call_id, tool_name, attempt, category)
                await asyncio.sleep(
                    backoff_seconds(
                        attempt,
                        settings.llm_backoff_seconds,
                        settings.llm_backoff_seconds * 8,
                    )
                )
                continue
            active_runtime.record_attempt(attempt=attempt)
            active_runtime.circuit.record_success()
            complete_tool_event(
                runtime_call_id, tool_name,
                {"model": request_body["model"], "attempts": attempt,
                 "usage": raw.get("usage", {})},
            )
            return validated.model_dump(), {
                "model": request_body["model"],
                "provider": "openai_compatible",
                "usage": raw.get("usage", {}),
                "resilience": {"attempts": attempt, "retried": attempt > 1},
            }
    category, _, _ = _llm_error_category(last_error or RuntimeError("unknown"))
    failure = RuntimeError(
        f"LLM 调用失败（category={category}, attempts={attempts_used}）：{last_error}"
    )
    fail_tool_event(
        runtime_call_id, tool_name, last_error or failure,
        category=category, attempt=attempts_used,
    )
    raise failure from last_error


async def generate_document_answer(
    *,
    security: dict,
    question: str,
    as_of: str,
    evidence: list[dict],
    settings: Settings,
    runtime: LLMRuntime | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[dict, dict]:
    if not settings.llm_configured:
        raise RuntimeError("LLM尚未配置")
    request_body = {
        "model": settings.quick_model,
        "messages": [
            {"role": "system", "content": DOCUMENT_ASSISTANT_PROMPT},
            {
                "role": "user",
                "content": "请回答问题：\n"
                + json.dumps(
                    {
                        "security": security,
                        "question": question,
                        "as_of": as_of,
                        "evidence": evidence,
                        "required_output_schema": DocumentAnswer.model_json_schema(),
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    return await _generate_evidence_json(
        request_body,
        settings,
        lambda raw: _validate_document_answer(raw, evidence),
        runtime=runtime,
        transport=transport,
    )


async def generate_financial_change_template(
    *,
    security: dict,
    as_of: str,
    evidence: list[dict],
    settings: Settings,
    runtime: LLMRuntime | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[dict, dict]:
    if not settings.llm_configured:
        raise RuntimeError("LLM尚未配置")
    request_body = {
        "model": settings.quick_model,
        "messages": [
            {"role": "system", "content": FINANCIAL_CHANGE_PROMPT},
            {
                "role": "user",
                "content": "请生成财报变化模板：\n"
                + json.dumps(
                    {
                        "security": security,
                        "as_of": as_of,
                        "evidence": evidence,
                        "required_output_schema": FinancialChangeTemplate.model_json_schema(),
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    return await _generate_evidence_json(
        request_body,
        settings,
        lambda raw: _validate_financial_change(raw, evidence),
        runtime=runtime,
        transport=transport,
    )


async def generate_research_assessment_draft(
    *,
    evidence_pack: dict,
    company_snapshot: dict,
    settings: Settings,
    depth: str = "quick",
    runtime: LLMRuntime | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[dict, dict]:
    if not settings.llm_configured:
        raise RuntimeError("LLM尚未配置")
    model = settings.deep_model if depth == "deep" and settings.deep_model else settings.quick_model
    request_body = {
        "model": model,
        "messages": [
            {"role": "system", "content": RESEARCH_ASSESSMENT_PROMPT},
            {
                "role": "user",
                "content": "请提取结构化研究评估：\n" + json.dumps(
                    {
                        "security": company_snapshot["security"],
                        "as_of": company_snapshot["as_of"],
                        "coverage": company_snapshot["coverage"],
                        "evidence": evidence_pack["items"],
                        "company_snapshot": company_snapshot,
                        "required_output_schema": ResearchAssessmentDraft.model_json_schema(),
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "temperature": 0.05,
        "response_format": {"type": "json_object"},
    }
    return await _generate_evidence_json(
        request_body,
        settings,
        lambda raw: _validate_research_assessment(raw, evidence_pack["items"]),
        runtime=runtime,
        transport=transport,
    )


def deterministic_report(analysis: dict[str, Any]) -> dict:
    metrics = analysis["metrics"]
    return {
        "executive_summary": (
            f"截至{analysis['as_of']}，{analysis['security']['name']}（{analysis['security']['code']}）"
            f"的价格趋势为{metrics['trend']}。该结论只基于本地日线价格与成交量，不能替代基本面研究。"
        ),
        "observed_changes": [
            f"20个交易日收益：{metrics['return_20d']}%",
            f"60个交易日收益：{metrics['return_60d']}%",
            f"当前距52周高点：{metrics['from_52w_high']}%",
        ],
        "claims": analysis["claims"],
        "counter_view": [
            "价格趋势可能反映短期风险偏好，而非公司经营变化。",
            "没有公告、财务与行业数据，无法判断价格变化的根本原因。",
        ],
        "uncertainties": analysis["uncertainties"],
        "next_checks": [
            "接入公告和财报后核验近期是否存在可解释价格变化的公开事件。",
            "确认源行情的复权口径，并对公司行为窗口重新计算收益。",
            "使用同行业样本比较相对强弱，避免把市场共振误判为个股变化。",
        ],
    }
