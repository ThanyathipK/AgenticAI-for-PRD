"""Upload validation and conversion independent of the HTTP route module."""

import io
import logging
from typing import Dict

from fastapi import HTTPException, UploadFile, status

from app.config import settings

logger = logging.getLogger("app.services.document_ingestion")

# DOC-UPLOAD 3.0 — Upload validation/conversion service used by route 2.1.
# 3.1/3.2 define accepted extension/MIME pairs; 3.3 is the OCR-only sentinel;
# 3.4 validates metadata, 3.4.1 checks binary signatures, 3.5 bounds bytes, and
# 3.6 converts supported content to canonical Markdown.
ALLOWED_FORMATS = {
    "docx": {
        "original_format": "docx",
        "mime_types": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    },
    "pdf": {"original_format": "pdf", "mime_types": {"application/pdf"}},
    "md": {
        "original_format": "md",
        "mime_types": {"text/markdown", "text/x-markdown", "text/plain"},
    },
    "txt": {"original_format": "txt", "mime_types": {"text/plain"}},
}
GENERIC_MIME_TYPES = {"", "application/octet-stream", "binary/octet-stream"}
NEEDS_OCR_MESSAGE = (
    "This PDF contains no extractable text and appears to be a scanned document. "
    "OCR is out of scope. Please upload a text-based PDF or a markdown/plain-text "
    "rendition instead."
)


class DocumentIngestionService:
    """Validate uploaded files and convert supported formats to Markdown."""

    @staticmethod
    def resolve_upload_format(filename: str, content_type: str) -> Dict:
        lower_name = (filename or "").lower()
        extension = lower_name.rsplit(".", 1)[-1] if "." in lower_name else ""
        entry = ALLOWED_FORMATS.get(extension)
        if not entry:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Unsupported file type '.{extension}'. Supported formats: docx, pdf, md, txt."
                ),
            )
        mime = (content_type or "").strip().lower()
        if mime and mime not in entry["mime_types"] and mime not in GENERIC_MIME_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"MIME type '{content_type}' does not match the file extension "
                    f"'.{extension}'. Expected one of: {', '.join(sorted(entry['mime_types']))}."
                ),
            )
        return {"original_format": entry["original_format"], "mime_type": mime or None}

    @staticmethod
    # DOC-UPLOAD 3.4.1 — PDF/DOCX magic-byte check after extension/MIME validation.
    # Text formats remain governed by UTF-8 replacement decoding in 3.6.
    def validate_binary_signature(raw: bytes, original_format: str) -> None:
        if original_format == "pdf" and not raw.startswith(b"%PDF-"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File contents do not match the declared PDF format.",
            )
        if original_format == "docx" and not raw.startswith(
            (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File contents do not match the declared DOCX format.",
            )

    @staticmethod
    async def read_upload_with_size_cap(file: UploadFile) -> bytes:
        max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
        buffer = bytearray()
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            buffer.extend(chunk)
            if len(buffer) > max_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail=f"File exceeds the maximum upload size of {settings.MAX_UPLOAD_MB}MB.",
                )
        return bytes(buffer)

    @staticmethod
    def convert_to_markdown(raw: bytes, original_format: str) -> str:
        if not raw:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The uploaded file is empty.",
            )
        if original_format in ("md", "txt"):
            return raw.decode("utf-8", errors="replace")
        if original_format == "docx":
            try:
                import mammoth
            except ImportError as exc:  # pragma: no cover
                raise HTTPException(
                    status_code=status.HTTP_501_NOT_IMPLEMENTED,
                    detail="DOCX conversion is unavailable on this server (mammoth missing).",
                ) from exc
            try:
                return mammoth.convert_to_markdown(io.BytesIO(raw)).value or ""
            except Exception as exc:
                logger.error("DOCX conversion failed: %s", exc)
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"Could not read this DOCX file: {exc}",
                ) from exc
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail="PDF extraction is unavailable on this server (pypdf missing).",
            ) from exc
        try:
            pages = [
                (page.extract_text() or "").strip() for page in PdfReader(io.BytesIO(raw)).pages
            ]
        except Exception as exc:
            logger.error("PDF extraction failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Could not read this PDF file: {exc}",
            ) from exc
        text = "\n\n".join(page for page in pages if page)
        if not text.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=NEEDS_OCR_MESSAGE,
            )
        return text
