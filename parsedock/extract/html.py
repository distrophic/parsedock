"""Извлечение записей из HTML по CSS-селекторам.

Каждая карточка — один контейнер. Поля ищутся внутри него, а не по всей
странице, поэтому название, цена и ссылка остаются одной записью.

Текст и значение атрибута возвращаются без очистки пробелов. Пустая строка
и отсутствующий элемент — разные результаты. Разбор чисел, дат и лишних
пробелов сюда не входит.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from bs4 import BeautifulSoup, Tag

from parsedock.errors import ExtractionError
from parsedock.profile import ExtractionSpec, FieldSpec

try:
    from soupsieve import SelectorSyntaxError
except ImportError:  # soupsieve ставится вместе с Beautiful Soup.
    SelectorSyntaxError = None  # type: ignore[misc, assignment]


@dataclass(frozen=True)
class FieldError:
    """Ошибка одного поля в одной карточке. Нумерация карточек с 1."""

    card_number: int
    field_name: str
    message: str
    value: object = None


@dataclass
class ExtractResult:
    """Успешные записи, ошибки, предупреждения и число снятых дублей.

    card_numbers хранит номер карточки в HTML для каждой успешной записи.
    В файл результата этот список не пишется.
    """

    records: list[dict] = field(default_factory=list)
    errors: list[FieldError] = field(default_factory=list)
    card_numbers: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duplicates_removed: int = 0
    pages_fetched: int = 0
    stop_reason: str = ""
    container_matches: int = 0


def extract_html(
    html: str,
    extraction: ExtractionSpec,
    *,
    source_url: str,
    fetched_at: datetime,
) -> ExtractResult:
    """Собрать записи из HTML-текста.

    fetched_at обязан содержать часовой пояс. В запись он попадает строкой
    ISO 8601, например 2026-09-22T11:00:00+00:00.
    """

    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ExtractionError("fetched_at должен содержать часовой пояс")

    soup = BeautifulSoup(html, "html.parser")
    containers = _select(soup, extraction.container, "селектор контейнера")
    fetched_text = fetched_at.isoformat()
    result = ExtractResult(container_matches=len(containers))

    for card_number, container in enumerate(containers, start=1):
        record, errors = _extract_card(container, extraction.fields, card_number)
        if errors:
            result.errors.extend(errors)
            continue
        record["source_url"] = source_url
        record["fetched_at"] = fetched_text
        result.records.append(record)
        result.card_numbers.append(card_number)

    return result


def _extract_card(
    container: Tag,
    fields: tuple[FieldSpec, ...],
    card_number: int,
) -> tuple[dict, list[FieldError]]:
    record: dict = {}
    errors: list[FieldError] = []
    for spec in fields:
        value, error = _extract_field(container, spec, card_number)
        if error is not None:
            errors.append(error)
            continue
        record[spec.name] = value
    if errors:
        return {}, errors
    return record, []


def _extract_field(
    container: Tag,
    spec: FieldSpec,
    card_number: int,
) -> tuple[object, FieldError | None]:
    matches = _select(container, spec.selector, f"поле {spec.name}")
    if spec.multiple:
        if not matches:
            if spec.required:
                return None, _missing(spec, card_number, "совпадений нет")
            return [], None
        values: list[str] = []
        for match in matches:
            value, missing = _read_tag(match, spec)
            if missing:
                if spec.required:
                    return None, _missing(spec, card_number, "атрибут отсутствует")
                return None, None
            values.append(value)
        return values, None

    if not matches:
        if spec.required:
            return None, _missing(spec, card_number, "совпадений нет")
        return None, None
    if len(matches) > 1:
        return None, FieldError(
            card_number=card_number,
            field_name=spec.name,
            message=(
                f"Поле {spec.name}: ожидалось одно совпадение, найдено {len(matches)} "
                f"(карточка {card_number})"
            ),
        )
    value, missing = _read_tag(matches[0], spec)
    if missing:
        if spec.required:
            return None, _missing(spec, card_number, "атрибут отсутствует")
        return None, None
    return value, None


def _read_tag(tag: Tag, spec: FieldSpec) -> tuple[str, bool]:
    """Вернуть значение и признак «данные отсутствуют».

    Второй элемент True означает отсутствие, а не пустую строку.
    """

    if spec.kind == "text":
        return tag.get_text(), False
    if spec.kind == "html":
        return tag.decode_contents(), False
    attribute = spec.attribute or ""
    if attribute not in tag.attrs:
        return "", True
    raw = tag.attrs[attribute]
    if isinstance(raw, list):
        return " ".join(raw), False
    return str(raw), False


def _missing(spec: FieldSpec, card_number: int, reason: str) -> FieldError:
    return FieldError(
        card_number=card_number,
        field_name=spec.name,
        message=f"Поле {spec.name}: {reason} (карточка {card_number})",
    )


def _select(node: BeautifulSoup | Tag, selector: str, label: str) -> list[Tag]:
    try:
        found = node.select(selector)
    except Exception as exc:
        if SelectorSyntaxError is not None and isinstance(exc, SelectorSyntaxError):
            raise ExtractionError(f"{label}: неверный CSS-селектор {selector!r}") from exc
        raise
    return [item for item in found if isinstance(item, Tag)]
