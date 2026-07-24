from pathlib import Path

from pypdf import PdfWriter

from app.document_pipeline import (
    DocumentPipelineError,
    chunk_pages,
    parse_pdf,
    text_layer_is_garbled,
    validate_document_url,
)


def test_chunks_keep_exact_page_boundaries():
    chunks = chunk_pages(["第一章\n" + "甲" * 1900, "第二页内容"], max_chars=800, overlap=100)
    assert len(chunks) >= 4
    assert {chunk["page_start"] for chunk in chunks} == {1, 2}
    assert all(chunk["page_start"] == chunk["page_end"] for chunk in chunks)
    assert [chunk["chunk_index"] for chunk in chunks] == list(range(len(chunks)))


def test_blank_pdf_is_marked_for_ocr(tmp_path):
    path = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with path.open("wb") as output:
        writer.write(output)

    parsed = parse_pdf(path)
    assert parsed.text_layer_status == "ocr_required"
    assert parsed.pages == [""]
    assert parsed.chunks == []


def test_document_url_allowlist():
    validate_document_url("https://ft.10jqka.com.cn/report.pdf")
    try:
        validate_document_url("http://127.0.0.1/private.pdf")
    except DocumentPipelineError as exc:
        assert "允许列表" in str(exc)
    else:
        raise AssertionError("非 iFinD 文档主机必须拒绝")


def test_garbled_text_layer_is_not_citable():
    assert text_layer_is_garbled(["证券代码：300750 公司公告内容正常。"] ) is False
    assert text_layer_is_garbled(["FF301\u0001\u0012⊈ಊṉọᄅḸіඳxĎර௹"] ) is True
