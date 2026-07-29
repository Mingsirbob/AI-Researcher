from __future__ import annotations

from datetime import date, timedelta

from app.research.documents import DocumentPipelineError, download_pdf, parse_pdf
from app.research.financials import extract_financial_facts
from app.integrations.ifind import IFindService
from app.research.store import ResearchStore


class AnnouncementPipeline:
    def __init__(self, store: ResearchStore, ifind: IFindService):
        self.store = store
        self.ifind = ifind

    def sync(
        self,
        code: str,
        *,
        end_date: date,
        lookback_days: int = 365,
        download_limit: int = 5,
        document_scope: str = "all",
    ) -> dict:
        if lookback_days < 1:
            raise ValueError("lookback_days 必须大于 0")
        if download_limit < 0 or download_limit > 50:
            raise ValueError("download_limit 必须在 0 到 50 之间")
        if document_scope not in {"all", "financial_reports"}:
            raise ValueError("document_scope 仅支持 all 或 financial_reports")
        start_date = end_date - timedelta(days=lookback_days)
        run_id = self.store.start_sync_run(code, start_date.isoformat(), end_date.isoformat())
        result = {
            "run_id": run_id,
            "security_code": code,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "metadata_received": 0,
            "metadata_upserted": 0,
            "documents_downloaded": 0,
            "documents_parsed": 0,
            "documents_skipped": 0,
            "financial_facts_extracted": 0,
            "failed_documents": {},
        }
        try:
            profile = self.ifind.get_security_profile(code)
            if profile.get("name"):
                self.store.upsert_security_name(code, profile["name"])
            announcements = self.ifind.query_announcements(
                code,
                start_date.isoformat(),
                end_date.isoformat(),
            )
            result["metadata_received"] = len(announcements)
            announcement_ids = self.store.upsert_announcements(code, announcements)
            result["metadata_upserted"] = len(announcement_ids)

            pending = self.store.pending_documents(
                announcement_ids,
                download_limit,
                document_scope=document_scope,
            )
            for item in pending:
                announcement_id = item["announcement_id"]
                try:
                    downloaded = download_pdf(item["source_url"], self.store.document_root)
                    result["documents_downloaded"] += 1
                    try:
                        parsed = parse_pdf(downloaded.path)
                    except DocumentPipelineError as exc:
                        self.store.save_document(
                            announcement_id=announcement_id,
                            sha256=downloaded.sha256,
                            file_path=downloaded.relative_path,
                            source_url=item["source_url"],
                            content_type=downloaded.content_type,
                            byte_size=downloaded.byte_size,
                            pages=[],
                            chunks=[],
                            extraction_method="pypdf",
                            text_layer_status="parse_failed",
                            parse_error=str(exc),
                        )
                        raise
                    document_id, created = self.store.save_document(
                        announcement_id=announcement_id,
                        sha256=downloaded.sha256,
                        file_path=downloaded.relative_path,
                        source_url=item["source_url"],
                        content_type=downloaded.content_type,
                        byte_size=downloaded.byte_size,
                        pages=parsed.pages,
                        chunks=parsed.chunks,
                        extraction_method=parsed.extraction_method,
                        text_layer_status=parsed.text_layer_status,
                        parse_error=parsed.parse_error,
                    )
                    if parsed.text_layer_status in {"ok", "partial"}:
                        extraction = extract_financial_facts(
                            document_id=document_id,
                            title=item["title"],
                            sha256=downloaded.sha256,
                            pages=parsed.pages,
                        )
                        extracted = self.store.save_financial_extraction(document_id, extraction)
                        result["financial_facts_extracted"] += extracted["facts"]
                    if created:
                        result["documents_parsed"] += 1
                    else:
                        result["documents_skipped"] += 1
                except DocumentPipelineError as exc:
                    status = "parse_failed" if "解析" in str(exc) else "download_failed"
                    self.store.mark_announcement_error(announcement_id, status, str(exc))
                    result["failed_documents"][announcement_id] = str(exc)[:500]
            self.store.finish_sync_run(run_id, result)
            result["status"] = "partial" if result["failed_documents"] else "success"
            return result
        except Exception as exc:
            self.store.finish_sync_run(run_id, result, error=str(exc))
            raise

    def reprocess(self, code: str) -> dict:
        parsed = 0
        ocr_required = 0
        financial_facts_extracted = 0
        failed: dict[str, str] = {}
        for item in self.store.current_documents(code):
            path = self.store.resolve_document_path(item["document_id"])
            if path is None:
                failed[item["document_id"]] = "本地 PDF 文件不存在"
                continue
            try:
                document = parse_pdf(path)
                self.store.replace_document_extraction(
                    item["document_id"],
                    pages=document.pages,
                    chunks=document.chunks,
                    extraction_method=document.extraction_method,
                    text_layer_status=document.text_layer_status,
                    parse_error=document.parse_error,
                )
                if document.text_layer_status == "ocr_required":
                    ocr_required += 1
                else:
                    parsed += 1
                    extraction = extract_financial_facts(
                        document_id=item["document_id"],
                        title=item["title"],
                        sha256=item["sha256"],
                        pages=document.pages,
                    )
                    saved = self.store.save_financial_extraction(item["document_id"], extraction)
                    financial_facts_extracted += saved["facts"]
            except DocumentPipelineError as exc:
                failed[item["document_id"]] = str(exc)
        return {
            "security_code": code,
            "documents_seen": parsed + ocr_required + len(failed),
            "parsed": parsed,
            "ocr_required": ocr_required,
            "financial_facts_extracted": financial_facts_extracted,
            "failed": failed,
        }
