import hashlib
import json
from pathlib import Path

from app.research.acceptance import EvidenceAcceptanceService
from app.research.financials import extract_financial_facts
from app.research.store import ResearchStore


METRIC_KEYS = (
    "global_parse_coverage",
    "pdf_file_hash_integrity",
    "document_sample_accuracy",
    "required_term_recall",
    "ocr_isolation_accuracy",
    "golden_fact_recall",
    "golden_fact_exact_accuracy",
    "financial_fact_citation_integrity",
    "chunk_integrity",
    "research_evidence_citation_integrity",
    "point_in_time_integrity",
    "persisted_extraction_consistency",
)


def build_acceptance_fixture(tmp_path: Path) -> tuple[EvidenceAcceptanceService, Path]:
    document_root = tmp_path / "documents"
    store = ResearchStore(tmp_path / "research.db", document_root)
    pdf_path = document_root / "sample.pdf"
    pdf_path.write_bytes(b"m5-real-file-integrity-fixture")
    pdf_sha256 = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    page_text = "某公司2025年年度报告\n单位：元\n营业收入 120 100 20%"
    text_sha256 = hashlib.sha256(page_text.encode("utf-8")).hexdigest()
    now = "2026-03-01T00:00:00+00:00"

    with store.connect() as conn:
        conn.execute(
            """INSERT INTO security_master (
                security_code, exchange, security_name, status, source,
                source_updated_at, created_at, updated_at
            ) VALUES ('000001.SZ', 'SZ', '某公司', 'listed', 'test', ?, ?, ?)""",
            (now, now, now),
        )
        conn.execute(
            """INSERT INTO announcement (
                announcement_id, security_code, source, source_sequence, title,
                report_date, published_at, source_url, metadata_hash,
                first_seen_at, last_seen_at, status, current_document_id
            ) VALUES ('ann-1', '000001.SZ', 'test', '1', '某公司2025年年度报告',
                '2026-02-28', '2026-02-28 18:00:00', 'https://example.test/sample.pdf',
                'meta', ?, ?, 'parsed', 'doc-1')""",
            (now, now),
        )
        conn.execute(
            """INSERT INTO announcement_document (
                document_id, announcement_id, version, sha256, file_path, source_url,
                content_type, byte_size, fetched_at, page_count, text_char_count,
                extraction_method, text_layer_status
            ) VALUES ('doc-1', 'ann-1', 1, ?, 'documents/sample.pdf',
                'https://example.test/sample.pdf', 'application/pdf', ?, ?, 1, ?,
                'fixture', 'ok')""",
            (pdf_sha256, pdf_path.stat().st_size, now, len(page_text)),
        )
        conn.execute(
            "INSERT INTO document_page VALUES ('doc-1', 1, ?, ?, ?)",
            (page_text, text_sha256, len(page_text)),
        )
        conn.execute(
            "INSERT INTO document_chunk VALUES ('chunk-1', 'doc-1', 0, 1, 1, NULL, ?, ?, ?)",
            (page_text, text_sha256, len(page_text)),
        )

    extraction = extract_financial_facts(
        document_id="doc-1",
        title="某公司2025年年度报告",
        sha256=pdf_sha256,
        pages=[page_text],
    )
    store.save_financial_extraction("doc-1", extraction)
    fact = extraction["facts"][0]
    manifest = {
        "manifest_version": "test-v1",
        "thresholds": {key: 1.0 for key in METRIC_KEYS},
        "document_samples": [
            {
                "label": "annual fixture",
                "sha256": pdf_sha256,
                "expected_text_layer_status": "ok",
                "min_pages": 1,
                "required_terms": ["营业收入120100"],
            }
        ],
        "golden_facts": [
            {
                "document_sha256": pdf_sha256,
                "metric_code": "operating_revenue",
                "period_end": "2025-12-31",
                "comparison_period_end": "2024-12-31",
                "current_value": "120",
                "comparison_value": "100",
                "source_page": fact["source_page"],
            }
        ],
        "known_gaps": ["fixture gap"],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return EvidenceAcceptanceService(store, manifest_path), pdf_path


def metric(result: dict, key: str) -> dict:
    return next(item for item in result["metrics"] if item["key"] == key)


def test_acceptance_passes_golden_document_fact_and_integrity_checks(tmp_path):
    service, _ = build_acceptance_fixture(tmp_path)

    result = service.evaluate()

    assert result["status"] == "passed"
    assert all(item["passed"] for item in result["metrics"])
    assert result["document_samples"][0]["passed"] is True
    assert result["golden_facts"][0]["exact"] is True


def test_acceptance_fails_when_golden_value_changes(tmp_path):
    service, _ = build_acceptance_fixture(tmp_path)
    manifest = json.loads(service.manifest_path.read_text(encoding="utf-8"))
    manifest["golden_facts"][0]["current_value"] = "121"
    service.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = service.evaluate()

    assert result["status"] == "failed"
    assert metric(result, "golden_fact_exact_accuracy")["passed"] is False


def test_acceptance_fails_when_archived_pdf_is_missing(tmp_path):
    service, pdf_path = build_acceptance_fixture(tmp_path)
    pdf_path.unlink()

    result = service.evaluate()

    assert result["status"] == "failed"
    assert metric(result, "pdf_file_hash_integrity")["passed"] is False
    assert result["failures"]["pdf_files"][0]["actual_sha256"] is None


def test_acceptance_run_is_immutable_and_reused_for_same_data(tmp_path):
    service, _ = build_acceptance_fixture(tmp_path)

    first = service.run()
    second = service.run()

    assert first["run_id"] == second["run_id"]
    assert first["snapshot_hash"] == second["snapshot_hash"]
    assert first["reused"] is False
    assert second["reused"] is True
    assert service.store.latest_evidence_acceptance_run()["run_id"] == first["run_id"]
