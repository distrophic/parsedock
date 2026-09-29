"""Извлечение данных из сохранённых документов."""

from datetime import datetime

from parsedock.extract.html import ExtractResult, FieldError, extract_html
from parsedock.extract.xml import extract_xml
from parsedock.profile import ExtractionSpec

__all__ = ["ExtractResult", "FieldError", "extract_document", "extract_html", "extract_xml"]


def extract_document(
    text: str,
    extraction: ExtractionSpec,
    *,
    source_url: str,
    fetched_at: datetime,
) -> ExtractResult:
    """Разобрать страницу тем правилом, которое записано в профиле."""

    if extraction.format == "xml":
        return extract_xml(text, extraction, source_url=source_url, fetched_at=fetched_at)
    return extract_html(text, extraction, source_url=source_url, fetched_at=fetched_at)
