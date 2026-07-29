from __future__ import annotations

from .analysis import analyze_stock
from .config import Settings
from .data_access import StockRepository, normalize_code
from .financial_extraction import build_financial_change_template
from .llm import (
    deterministic_report,
    generate_ai_report,
    generate_research_assessment_draft,
)
from .research_assessment import (
    RESEARCH_ASSESSMENT_SCHEMA_VERSION,
    build_research_assessment,
    deterministic_assessment_fallback,
)
from .research_store import ResearchStore
from .research_workflow import (
    COMPANY_SNAPSHOT_SCHEMA_VERSION,
    EVIDENCE_PACK_SCHEMA_VERSION,
    QUANT_CONTEXT_SCHEMA_VERSION,
    WORKFLOW_VERSION,
    build_company_snapshot,
    build_evidence_pack,
    empty_financial_template,
)


class CompanyResearchNotFoundError(ValueError):
    pass


class CompanyResearchContextError(ValueError):
    pass


class CompanyResearchRunError(RuntimeError):
    def __init__(self, run_id: str, message: str) -> None:
        super().__init__(message)
        self.run_id = run_id


class CompanyResearchService:
    def __init__(
        self,
        repository: StockRepository,
        store: ResearchStore,
        settings: Settings,
        quant_store: ResearchStore | None = None,
    ) -> None:
        self.repository = repository
        self.store = store
        self.quant_store = quant_store or store
        self.settings = settings

    def _build_analysis(self, code: str, as_of: str | None) -> dict:
        try:
            normalized = normalize_code(code)
            rows = self.repository.get_history(normalized, end=as_of, limit=1800)
            master = self.store.security(normalized)
            display_name = master.get("security_name") if master else None
            return analyze_stock(
                normalized,
                display_name or self.repository.display_name(normalized),
                rows,
            )
        except (ValueError, KeyError) as exc:
            raise CompanyResearchNotFoundError(str(exc)) from exc

    async def _create_assessment_artifact(
        self,
        *,
        run_id: str,
        evidence_pack: dict,
        evidence_artifact: dict,
        snapshot: dict,
        snapshot_artifact: dict,
        depth: str,
    ) -> tuple[dict, dict]:
        try:
            draft, meta = await generate_research_assessment_draft(
                evidence_pack=evidence_pack,
                company_snapshot=snapshot,
                settings=self.settings,
                depth=depth,
            )
            mode = "ai"
        except Exception as exc:
            draft = deterministic_assessment_fallback(str(exc))
            meta = {"warning": str(exc)}
            mode = "deterministic_fallback"
        assessment = build_research_assessment(
            run_id=run_id,
            evidence_pack=evidence_pack,
            evidence_artifact=evidence_artifact,
            company_snapshot=snapshot,
            company_artifact=snapshot_artifact,
            draft=draft,
            generation_mode=mode,
            generation_meta=meta,
        )
        artifact = self.store.save_research_artifact(
            run_id=run_id,
            artifact_type="research_assessment",
            schema_version=RESEARCH_ASSESSMENT_SCHEMA_VERSION,
            status=assessment["signal"],
            payload=assessment,
        )
        return assessment, artifact

    async def run(
        self,
        *,
        code: str,
        as_of: str | None,
        depth: str,
        factor_snapshot_id: str | None,
    ) -> dict:
        analysis = self._build_analysis(code, as_of)
        try:
            quant_context = self.quant_store.quant_context_for_security(
                analysis["security"]["code"],
                as_of=analysis["as_of"],
                snapshot_id=factor_snapshot_id,
            )
        except ValueError as exc:
            raise CompanyResearchContextError(str(exc)) from exc

        run_id = self.store.start_research_run(
            code=analysis["security"]["code"],
            as_of=analysis["as_of"],
            workflow_version=WORKFLOW_VERSION,
        )
        try:
            extraction = self.store.refresh_financial_facts(
                analysis["security"]["code"], analysis["as_of"]
            )
            financial_evidence = self.store.financial_fact_evidence(
                analysis["security"]["code"], analysis["as_of"]
            )
            financial_template = (
                build_financial_change_template(financial_evidence)
                if financial_evidence
                else empty_financial_template(
                    "当前没有通过严格规则提取出可比较的财务数字。",
                    "未归档财报或现有财报不满足严格同一行双值抽取规则。",
                )
            )
            document_evidence = self.store.evidence_for_security(
                analysis["security"]["code"], as_of=analysis["as_of"], limit=8
            )
            announcements = self.store.list_announcements(
                analysis["security"]["code"], as_of=analysis["as_of"], limit=20
            )
            evidence_pack = build_evidence_pack(
                analysis=analysis,
                financial_evidence=financial_evidence,
                document_evidence=document_evidence,
                announcements=announcements,
                extraction=extraction,
            )
            evidence_artifact = self.store.save_research_artifact(
                run_id=run_id,
                artifact_type="evidence_pack",
                schema_version=EVIDENCE_PACK_SCHEMA_VERSION,
                status=(
                    "complete"
                    if all(
                        item["status"] == "supported"
                        for item in evidence_pack["coverage"].values()
                    )
                    else "partial"
                ),
                payload=evidence_pack,
            )
            quant_artifact = self.store.save_research_artifact(
                run_id=run_id,
                artifact_type="quant_context",
                schema_version=QUANT_CONTEXT_SCHEMA_VERSION,
                status=quant_context["status"],
                payload=quant_context,
            )

            controlled_analysis = dict(analysis)
            controlled_analysis["evidence"] = evidence_pack["items"]
            controlled_analysis["uncertainties"] = [
                item
                for item in analysis["uncertainties"]
                if "当前结论不包含财务、公告" not in item
            ]
            if not financial_evidence:
                controlled_analysis["uncertainties"].append(
                    "当前没有通过严格规则提取的财务事实。"
                )
            if not document_evidence:
                controlled_analysis["uncertainties"].append(
                    "当前没有可引用的已解析公告正文。"
                )

            fallback = deterministic_report(controlled_analysis)
            try:
                narrative, generation_meta = await generate_ai_report(
                    controlled_analysis, self.settings, depth
                )
                generation_mode = "ai"
            except Exception as exc:
                narrative = fallback
                generation_meta = {"warning": str(exc)}
                generation_mode = "deterministic_fallback"

            snapshot = build_company_snapshot(
                analysis=controlled_analysis,
                evidence_pack=evidence_pack,
                announcements=announcements,
                financial_template=financial_template,
                narrative=narrative,
                generation_mode=generation_mode,
                generation_meta=generation_meta,
                quant_context=quant_context,
            )
            snapshot_artifact = self.store.save_research_artifact(
                run_id=run_id,
                artifact_type="company_snapshot",
                schema_version=COMPANY_SNAPSHOT_SCHEMA_VERSION,
                status=snapshot["status"],
                payload=snapshot,
            )
            assessment, assessment_artifact = await self._create_assessment_artifact(
                run_id=run_id,
                evidence_pack=evidence_pack,
                evidence_artifact=evidence_artifact,
                snapshot=snapshot,
                snapshot_artifact=snapshot_artifact,
                depth=depth,
            )
            run_status = (
                "completed" if snapshot["status"] == "complete" else "completed_with_gaps"
            )
            self.store.finish_research_run(
                run_id,
                status=run_status,
                evidence_snapshot_hash=evidence_artifact["snapshot_hash"],
                evidence_count=len(evidence_pack["items"]),
            )
            return {
                "run_id": run_id,
                "status": run_status,
                "workflow_version": WORKFLOW_VERSION,
                "mode": generation_mode,
                "snapshot": snapshot,
                "assessment": assessment,
                "evidence": evidence_pack["items"],
                "analysis": controlled_analysis,
                "artifacts": [
                    evidence_artifact,
                    quant_artifact,
                    snapshot_artifact,
                    assessment_artifact,
                ],
            }
        except Exception as exc:
            self.store.finish_research_run(run_id, status="failed", error=str(exc))
            raise CompanyResearchRunError(run_id, str(exc)) from exc
