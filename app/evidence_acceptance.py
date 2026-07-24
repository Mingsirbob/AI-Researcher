from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .financial_extraction import extract_financial_facts
from .primitives import canonical_hash as _canonical_hash, sha256_file, sha256_text
from .research_store import ResearchStore


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 1.0


class EvidenceAcceptanceService:
    def __init__(self, store: ResearchStore, manifest_path: Path):
        self.store = store
        self.manifest_path = manifest_path

    def _load_manifest(self) -> tuple[dict, str]:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        return manifest, _canonical_hash(manifest)

    @staticmethod
    def _add_metric(
        metrics: list[dict],
        thresholds: dict,
        key: str,
        label: str,
        numerator: int,
        denominator: int,
    ) -> None:
        value = _ratio(numerator, denominator)
        threshold = float(thresholds[key])
        metrics.append(
            {
                "key": key,
                "label": label,
                "value": value,
                "threshold": threshold,
                "numerator": numerator,
                "denominator": denominator,
                "passed": value >= threshold,
            }
        )

    def evaluate(self) -> dict:
        started_at = datetime.now(timezone.utc).isoformat()
        manifest, manifest_hash = self._load_manifest()
        thresholds = manifest["thresholds"]
        with self.store.connect() as conn:
            documents = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT d.*, a.security_code, a.title, a.published_at, a.report_date
                    FROM announcement_document d
                    JOIN announcement a ON a.announcement_id=d.announcement_id
                    ORDER BY d.sha256
                    """
                )
            ]
            pages = [
                dict(row)
                for row in conn.execute(
                    "SELECT document_id, page_number, text, text_sha256 FROM document_page ORDER BY document_id, page_number"
                )
            ]
            chunks = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT c.*, d.text_layer_status, d.page_count
                    FROM document_chunk c
                    JOIN announcement_document d ON d.document_id=c.document_id
                    ORDER BY c.document_id, c.chunk_index
                    """
                )
            ]
            facts = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT f.*, d.sha256 AS stored_document_sha256, d.page_count,
                           a.published_at, a.report_date, a.title
                    FROM financial_metric_fact f
                    JOIN announcement_document d ON d.document_id=f.document_id
                    JOIN announcement a ON a.announcement_id=d.announcement_id
                    ORDER BY f.document_id, f.metric_code
                    """
                )
            ]
            financial_documents = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT d.document_id, d.sha256, a.title
                    FROM financial_report_period r
                    JOIN announcement_document d ON d.document_id=r.document_id
                    JOIN announcement a ON a.announcement_id=d.announcement_id
                    ORDER BY d.document_id
                    """
                )
            ]
            artifacts = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT a.payload_json, r.as_of
                    FROM research_artifact a
                    JOIN research_run r ON r.run_id=a.run_id
                    WHERE a.artifact_type='evidence_pack'
                    ORDER BY a.created_at
                    """
                )
            ]

        document_by_sha = {item["sha256"]: item for item in documents}
        document_by_id = {item["document_id"]: item for item in documents}
        pages_by_document: dict[str, list[dict]] = {}
        page_by_key = {}
        for page in pages:
            pages_by_document.setdefault(page["document_id"], []).append(page)
            page_by_key[(page["document_id"], page["page_number"])] = page

        file_checks = []
        for document in documents:
            path = (self.store.document_root.parent / document["file_path"]).resolve()
            root = self.store.document_root.resolve()
            actual_hash = (
                sha256_file(path)
                if path.is_relative_to(root) and path.is_file()
                else None
            )
            file_checks.append(
                {
                    "document_id": document["document_id"],
                    "expected_sha256": document["sha256"],
                    "actual_sha256": actual_hash,
                    "passed": actual_hash == document["sha256"],
                }
            )

        document_samples = []
        required_terms_total = 0
        required_terms_found = 0
        ocr_total = 0
        ocr_passed = 0
        for sample in manifest["document_samples"]:
            document = document_by_sha.get(sample["sha256"])
            page_items = pages_by_document.get(document["document_id"], []) if document else []
            combined_text = _normalized_text("\n".join(item["text"] for item in page_items))
            term_results = []
            for term in sample.get("required_terms", []):
                found = _normalized_text(term) in combined_text
                term_results.append({"term": term, "found": found})
                required_terms_total += 1
                required_terms_found += int(found)
            status_matches = bool(
                document
                and document["text_layer_status"] == sample["expected_text_layer_status"]
            )
            pages_match = bool(document and document["page_count"] >= sample["min_pages"])
            no_ocr_chunks = True
            if sample["expected_text_layer_status"] == "ocr_required":
                ocr_total += 1
                no_ocr_chunks = not any(
                    chunk["document_id"] == document["document_id"] for chunk in chunks
                ) if document else False
                ocr_passed += int(status_matches and no_ocr_chunks)
            document_samples.append(
                {
                    "label": sample["label"],
                    "sha256": sample["sha256"],
                    "document_id": document["document_id"] if document else None,
                    "security_code": document["security_code"] if document else None,
                    "title": document["title"] if document else None,
                    "expected_status": sample["expected_text_layer_status"],
                    "actual_status": document["text_layer_status"] if document else "missing",
                    "status_matches": status_matches,
                    "pages_match": pages_match,
                    "terms": term_results,
                    "passed": status_matches
                    and pages_match
                    and no_ocr_chunks
                    and all(item["found"] for item in term_results),
                }
            )

        extraction_cache = {}
        golden_results = []
        golden_found = 0
        golden_exact = 0
        for expected in manifest["golden_facts"]:
            document = document_by_sha.get(expected["document_sha256"])
            if document and document["document_id"] not in extraction_cache:
                extraction_cache[document["document_id"]] = extract_financial_facts(
                    document_id=document["document_id"],
                    title=document["title"],
                    sha256=document["sha256"],
                    pages=[item["text"] for item in pages_by_document[document["document_id"]]],
                )
            actual = None
            if document:
                actual = next(
                    (
                        item
                        for item in extraction_cache[document["document_id"]]["facts"]
                        if item["metric_code"] == expected["metric_code"]
                    ),
                    None,
                )
            found = actual is not None
            exact = bool(
                actual
                and all(actual.get(key) == value for key, value in expected.items() if key != "document_sha256")
            )
            golden_found += int(found)
            golden_exact += int(exact)
            golden_results.append(
                {
                    "document_sha256": expected["document_sha256"],
                    "metric_code": expected["metric_code"],
                    "expected": {key: value for key, value in expected.items() if key != "document_sha256"},
                    "actual": (
                        {key: actual.get(key) for key in expected if key != "document_sha256"}
                        if actual
                        else None
                    ),
                    "found": found,
                    "exact": exact,
                }
            )

        fact_failures = []
        for fact in facts:
            page = page_by_key.get((fact["document_id"], fact["source_page"]))
            valid = bool(
                page
                and fact["document_sha256"] == fact["stored_document_sha256"]
                and sha256_text(fact["source_text"])
                == fact["source_text_sha256"]
                and _normalized_text(fact["source_text"]) in _normalized_text(page["text"])
            )
            if not valid:
                fact_failures.append(
                    {"document_id": fact["document_id"], "metric_code": fact["metric_code"]}
                )

        chunk_failures = []
        for chunk in chunks:
            valid = (
                chunk["text_layer_status"] in {"ok", "partial"}
                and 1 <= chunk["page_start"] <= chunk["page_end"] <= chunk["page_count"]
                and sha256_text(chunk["text"])
                == chunk["text_sha256"]
            )
            if not valid:
                chunk_failures.append({"chunk_id": chunk["chunk_id"], "document_id": chunk["document_id"]})

        citation_total = 0
        citation_passed = 0
        point_total = 0
        point_passed = 0
        research_failures = []
        for artifact in artifacts:
            payload = json.loads(artifact["payload_json"])
            for evidence in payload.get("items", []):
                evidence_as_of = str(evidence.get("as_of") or "")[:10]
                if evidence_as_of:
                    point_total += 1
                    point_passed += int(evidence_as_of <= artifact["as_of"])
                citation = evidence.get("citation") or {}
                document_id = citation.get("document_id")
                if not document_id:
                    continue
                citation_total += 1
                document = document_by_id.get(document_id)
                valid = bool(
                    document
                    and citation.get("sha256") == document["sha256"]
                    and 1 <= int(citation.get("page_start") or 0)
                    <= int(citation.get("page_end") or citation.get("page_start") or 0)
                    <= document["page_count"]
                )
                citation_passed += int(valid)
                if not valid:
                    research_failures.append(
                        {"evidence_id": evidence.get("id"), "document_id": document_id}
                    )

        fact_point_total = len(facts)
        fact_point_passed = sum(
            str(fact["period_end"]) <= str(fact["published_at"] or fact["report_date"] or "")[:10]
            for fact in facts
        )
        point_total += fact_point_total
        point_passed += fact_point_passed

        consistency_total = 0
        consistency_passed = 0
        consistency_failures = []
        persisted_by_document: dict[str, dict[str, dict]] = {}
        for fact in facts:
            persisted_by_document.setdefault(fact["document_id"], {})[fact["metric_code"]] = fact
        for document in financial_documents:
            generated = extract_financial_facts(
                document_id=document["document_id"],
                title=document["title"],
                sha256=document["sha256"],
                pages=[item["text"] for item in pages_by_document.get(document["document_id"], [])],
            )
            generated_map = {item["metric_code"]: item for item in generated["facts"]}
            persisted_map = persisted_by_document.get(document["document_id"], {})
            keys = set(generated_map) | set(persisted_map)
            for key in keys:
                consistency_total += 1
                generated_fact = generated_map.get(key)
                persisted_fact = persisted_map.get(key)
                valid = bool(
                    generated_fact
                    and persisted_fact
                    and all(
                        generated_fact.get(field) == persisted_fact.get(field)
                        for field in (
                            "period_end",
                            "comparison_period_end",
                            "current_value",
                            "comparison_value",
                            "source_page",
                            "source_text_sha256",
                            "extraction_version",
                        )
                    )
                )
                consistency_passed += int(valid)
                if not valid:
                    consistency_failures.append(
                        {"document_id": document["document_id"], "metric_code": key}
                    )

        metrics = []
        parsed_documents = sum(
            item["text_layer_status"] in {"ok", "partial"} for item in documents
        )
        self._add_metric(metrics, thresholds, "global_parse_coverage", "PDF 可解析覆盖率", parsed_documents, len(documents))
        self._add_metric(metrics, thresholds, "pdf_file_hash_integrity", "PDF 文件哈希完整率", sum(item["passed"] for item in file_checks), len(file_checks))
        self._add_metric(metrics, thresholds, "document_sample_accuracy", "金标文档状态准确率", sum(item["passed"] for item in document_samples), len(document_samples))
        self._add_metric(metrics, thresholds, "required_term_recall", "金标原文片段召回率", required_terms_found, required_terms_total)
        self._add_metric(metrics, thresholds, "ocr_isolation_accuracy", "OCR 异常隔离准确率", ocr_passed, ocr_total)
        self._add_metric(metrics, thresholds, "golden_fact_recall", "金标财务事实召回率", golden_found, len(golden_results))
        self._add_metric(metrics, thresholds, "golden_fact_exact_accuracy", "金标财务数值精确率", golden_exact, len(golden_results))
        self._add_metric(metrics, thresholds, "financial_fact_citation_integrity", "财务事实原文绑定完整率", len(facts) - len(fact_failures), len(facts))
        self._add_metric(metrics, thresholds, "chunk_integrity", "公告切块完整率", len(chunks) - len(chunk_failures), len(chunks))
        self._add_metric(metrics, thresholds, "research_evidence_citation_integrity", "研究证据引用完整率", citation_passed, citation_total)
        self._add_metric(metrics, thresholds, "point_in_time_integrity", "时点边界完整率", point_passed, point_total)
        self._add_metric(metrics, thresholds, "persisted_extraction_consistency", "持久化提取版本一致率", consistency_passed, consistency_total)

        fingerprint_source = {
            "documents": [
                [item["sha256"], item["text_layer_status"], item["page_count"], item["text_char_count"]]
                for item in documents
            ],
            "facts": [
                [
                    item["document_id"], item["metric_code"], item["current_value"],
                    item["comparison_value"], item["source_text_sha256"], item["extraction_version"],
                ]
                for item in facts
            ],
            "artifacts": len(artifacts),
            "chunks": len(chunks),
            "file_hashes": [item["actual_sha256"] for item in file_checks],
        }
        failed_metrics = [item["key"] for item in metrics if not item["passed"]]
        result = {
            "manifest_version": manifest["manifest_version"],
            "manifest_hash": manifest_hash,
            "data_fingerprint": _canonical_hash(fingerprint_source),
            "started_at": started_at,
            "status": "passed" if not failed_metrics else "failed",
            "summary": {
                "documents": len(documents),
                "parsed_documents": parsed_documents,
                "ocr_required_documents": len(documents) - parsed_documents,
                "document_samples": len(document_samples),
                "golden_facts": len(golden_results),
                "persisted_financial_facts": len(facts),
                "research_evidence_items": point_total - fact_point_total,
                "failed_metrics": len(failed_metrics),
            },
            "metrics": metrics,
            "failed_metrics": failed_metrics,
            "document_samples": document_samples,
            "golden_facts": golden_results,
            "failures": {
                "pdf_files": [item for item in file_checks if not item["passed"]],
                "financial_citations": fact_failures,
                "chunks": chunk_failures,
                "research_citations": research_failures,
                "extraction_consistency": consistency_failures,
            },
            "known_gaps": manifest.get("known_gaps", []),
        }
        return result

    def run(self) -> dict:
        return self.store.save_evidence_acceptance_run(self.evaluate())
