from __future__ import annotations

import hashlib
import os
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from pypdf import PdfReader

from app.core.resilience import backoff_seconds


ALLOWED_DOCUMENT_HOST_SUFFIXES = (".10jqka.com.cn", ".51ifind.com")


class DocumentPipelineError(RuntimeError):
    pass


@dataclass(frozen=True)
class DownloadedDocument:
    sha256: str
    path: Path
    relative_path: str
    content_type: str
    byte_size: int


@dataclass(frozen=True)
class ParsedDocument:
    pages: list[str]
    chunks: list[dict]
    extraction_method: str
    text_layer_status: str
    parse_error: str | None = None


def validate_document_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        raise DocumentPipelineError("公告文件 URL 只允许 http/https")
    if not host or not any(host.endswith(suffix) for suffix in ALLOWED_DOCUMENT_HOST_SUFFIXES):
        raise DocumentPipelineError(f"公告文件主机不在允许列表：{host or 'missing'}")


def download_pdf(
    url: str,
    document_root: Path,
    *,
    timeout_seconds: float = 30.0,
    max_attempts: int = 3,
    max_bytes: int = 50 * 1024 * 1024,
) -> DownloadedDocument:
    validate_document_url(url)
    document_root.mkdir(parents=True, exist_ok=True)
    temp_path = document_root / f".{uuid.uuid4()}.part"
    last_error: Exception | None = None
    try:
        for attempt in range(1, max_attempts + 1):
            digest = hashlib.sha256()
            byte_size = 0
            try:
                with httpx.Client(
                    timeout=httpx.Timeout(timeout_seconds),
                    follow_redirects=True,
                    headers={"User-Agent": "AI-Researcher/0.1 announcement-archiver"},
                ) as client:
                    with client.stream("GET", url) as response:
                        response.raise_for_status()
                        validate_document_url(str(response.url))
                        content_type = response.headers.get("content-type", "").split(";", 1)[0]
                        declared_size = int(response.headers.get("content-length", "0") or 0)
                        if declared_size > max_bytes:
                            raise DocumentPipelineError(
                                f"公告 PDF 超过大小限制：{declared_size} > {max_bytes}"
                            )
                        with temp_path.open("wb") as output:
                            for block in response.iter_bytes(64 * 1024):
                                byte_size += len(block)
                                if byte_size > max_bytes:
                                    raise DocumentPipelineError(
                                        f"公告 PDF 超过大小限制：>{max_bytes}"
                                    )
                                digest.update(block)
                                output.write(block)
                with temp_path.open("rb") as stream:
                    if stream.read(5) != b"%PDF-":
                        raise DocumentPipelineError("下载内容不是 PDF 文件")
                sha256 = digest.hexdigest()
                target_dir = document_root / sha256[:2]
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / f"{sha256}.pdf"
                if target.exists():
                    temp_path.unlink(missing_ok=True)
                else:
                    os.replace(temp_path, target)
                return DownloadedDocument(
                    sha256=sha256,
                    path=target,
                    relative_path=target.relative_to(document_root.parent).as_posix(),
                    content_type=content_type or "application/pdf",
                    byte_size=byte_size,
                )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                last_error = exc
                temp_path.unlink(missing_ok=True)
                retryable_status = not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code in {
                    429,
                    500,
                    502,
                    503,
                    504,
                }
                if not retryable_status or attempt >= max_attempts:
                    break
                time.sleep(backoff_seconds(attempt, 0.5, 4.0))
            except Exception:
                temp_path.unlink(missing_ok=True)
                raise
    finally:
        temp_path.unlink(missing_ok=True)
    if isinstance(last_error, httpx.HTTPStatusError):
        reason = f"HTTP {last_error.response.status_code}"
    elif isinstance(last_error, httpx.TimeoutException):
        reason = "timeout"
    else:
        reason = type(last_error).__name__ if last_error else "unknown"
    raise DocumentPipelineError(f"公告 PDF 下载失败：{reason}") from last_error


def normalize_page_text(value: str) -> str:
    lines = []
    for raw_line in value.replace("\x00", "").splitlines():
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _section_hint(text: str) -> str | None:
    first_line = text.splitlines()[0].strip() if text.strip() else ""
    if 2 <= len(first_line) <= 80 and not first_line.endswith(("。", "；", ";", ".")):
        return first_line
    return None


def chunk_pages(pages: list[str], max_chars: int = 1600, overlap: int = 200) -> list[dict]:
    if max_chars < 200 or overlap < 0 or overlap >= max_chars:
        raise ValueError("无效的切块参数")
    chunks: list[dict] = []
    chunk_index = 0
    for page_number, page_text in enumerate(pages, start=1):
        text = page_text.strip()
        if not text:
            continue
        cursor = 0
        while cursor < len(text):
            end = min(len(text), cursor + max_chars)
            if end < len(text):
                candidates = [
                    text.rfind(marker, cursor + max_chars // 2, end)
                    for marker in ("\n", "。", "；", ";")
                ]
                split_at = max(candidates)
                if split_at > cursor:
                    end = split_at + 1
            chunk_text = text[cursor:end].strip()
            if chunk_text:
                chunks.append(
                    {
                        "chunk_index": chunk_index,
                        "page_start": page_number,
                        "page_end": page_number,
                        "section_path": _section_hint(chunk_text),
                        "text": chunk_text,
                    }
                )
                chunk_index += 1
            if end >= len(text):
                break
            cursor = max(cursor + 1, end - overlap)
    return chunks


def text_layer_is_garbled(pages: list[str]) -> bool:
    text = "".join(pages)
    characters = [char for char in text if not char.isspace()]
    if not characters:
        return False
    controls = sum(1 for char in characters if unicodedata.category(char) == "Cc")
    recognized = sum(
        1
        for char in characters
        if char.isascii()
        or "\u3400" <= char <= "\u9fff"
        or "\u3000" <= char <= "\u303f"
        or "\uff00" <= char <= "\uffef"
    )
    return controls / len(characters) > 0.005 or recognized / len(characters) < 0.72


def parse_pdf(path: Path) -> ParsedDocument:
    try:
        reader = PdfReader(path)
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise DocumentPipelineError("公告 PDF 已加密且无法解密")
        pages = [normalize_page_text(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:
        raise DocumentPipelineError(f"公告 PDF 解析失败：{exc}") from exc
    text_pages = sum(1 for page in pages if len(page) >= 20)
    total_chars = sum(len(page) for page in pages)
    parse_error = None
    if text_layer_is_garbled(pages):
        status = "ocr_required"
        parse_error = "PDF 文本层疑似乱码，需要 OCR"
    elif total_chars < 50:
        status = "ocr_required"
    elif pages and text_pages / len(pages) < 0.7:
        status = "partial"
    else:
        status = "ok"
    return ParsedDocument(
        pages=pages,
        chunks=[] if status == "ocr_required" else chunk_pages(pages),
        extraction_method="pypdf",
        text_layer_status=status,
        parse_error=parse_error,
    )
