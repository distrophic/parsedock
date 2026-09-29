"""Извлечение записей из XML по пути элементов.

Путь — имена элементов через /. Это не CSS и не XPath: нет классов,
звёздочек, условий и поиска по всему дереву. Первый шаг — корень
документа, дальше только прямые потомки. Поле `.` означает сам элемент
записи, обычно чтобы снять его атрибут.

Имя без фигурных скобок сравнивается без пространства имён. Запись
`{uri}name` требует точное совпадение. Точка в шаге запрещена, поэтому
селектор вроде article.card сюда не подходит. Документ с DTD или
объявлением сущности отклоняется до разбора: внешний файл и раздувание
через сущности не читаются. Обычные подстановки вроде &amp; остаются.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit, urljoin
from xml.etree.ElementTree import Element

from parsedock.errors import ExtractionError
from parsedock.extract.html import ExtractResult, FieldError
from parsedock.profile import ExtractionSpec, FieldSpec

_LOCAL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_CLARK_NAME = re.compile(r"\{[^{}/]+\}[A-Za-z_][A-Za-z0-9_-]*")
_MAX_ELEMENTS = 100_000


def element_path(value: str, *, allow_self: bool) -> tuple[str, ...]:
    """Проверить путь. ValueError — текст для ошибки профиля или извлечения."""

    if allow_self and value == ".":
        return (".",)
    if not value or value.startswith("/") or value.endswith("/") or "//" in value:
        raise ValueError(_path_message(value))
    parts = value.split("/")
    for part in parts:
        if _LOCAL_NAME.fullmatch(part) or _CLARK_NAME.fullmatch(part):
            continue
        raise ValueError(_path_message(value))
    return tuple(parts)


def extract_xml(
    xml: str,
    extraction: ExtractionSpec,
    *,
    source_url: str,
    fetched_at: datetime,
) -> ExtractResult:
    """Собрать записи из XML-текста.

    fetched_at обязан содержать часовой пояс. Пустой элемент и отсутствующий
    элемент — разные результаты: пустая строка и null.
    """

    if extraction.format != "xml":
        raise ExtractionError("extract_xml читает только extraction.format=xml")
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ExtractionError("fetched_at должен содержать часовой пояс")

    root = _parse(xml)
    try:
        steps = element_path(extraction.container, allow_self=False)
    except ValueError as exc:
        raise ExtractionError(str(exc)) from exc
    containers = _walk([root], steps, from_root=True)
    fetched_text = fetched_at.isoformat()
    result = ExtractResult(container_matches=len(containers))

    for card_number, container in enumerate(containers, start=1):
        record, errors = _extract_record(container, extraction.fields, card_number)
        if errors:
            result.errors.extend(errors)
            continue
        record["source_url"] = source_url
        record["fetched_at"] = fetched_text
        result.records.append(record)
        result.card_numbers.append(card_number)
    return result


def next_link_from_xml(xml: str, path: str, attribute: str, base_url: str) -> tuple[str | None, str | None]:
    """Адрес следующей страницы по пути элемента и имени атрибута."""

    root = _parse(xml)
    try:
        steps = element_path(path, allow_self=False)
    except ValueError as exc:
        raise ExtractionError(str(exc)) from exc
    matches = _walk([root], steps, from_root=True)
    if not matches:
        return None, None
    warning = None
    if len(matches) > 1:
        warning = f"Ссылок следующей страницы найдено {len(matches)}, взята первая."
    value = matches[0].get(attribute)
    if value is None or value == "":
        return None, warning or "У ссылки следующей страницы нет адреса."
    joined = urljoin(base_url, value)
    parts = urlsplit(joined)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, "")), warning


def _path_message(value: str) -> str:
    return (
        "для XML нужен путь из имён элементов через /, например catalog/item. "
        f"CSS-селектор сюда не переносится. Получено {value!r}."
    )


def _parse(xml: str) -> Element:
    lowered = xml.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise ExtractionError("DTD и сущности XML не разбираются.")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ExtractionError(f"XML не разобран: {exc}") from exc
    count = 0
    for _node in root.iter():
        count += 1
        if count > _MAX_ELEMENTS:
            raise ExtractionError("XML слишком большой для разбора.")
    return root


def _walk(nodes: list[Element], steps: tuple[str, ...], *, from_root: bool) -> list[Element]:
    if not steps:
        return []
    current = nodes
    if from_root:
        current = [node for node in nodes if _matches(node.tag, steps[0])]
        steps = steps[1:]
    for step in steps:
        found: list[Element] = []
        for node in current:
            found.extend(child for child in list(node) if _matches(child.tag, step))
        current = found
    return current


def _matches(tag: str, step: str) -> bool:
    if step.startswith("{") and "}" in step:
        return tag == step
    if tag.startswith("{") and "}" in tag:
        return tag[tag.find("}") + 1 :] == step
    return tag == step


def _extract_record(
    container: Element,
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
    container: Element,
    spec: FieldSpec,
    card_number: int,
) -> tuple[object, FieldError | None]:
    try:
        steps = element_path(spec.selector, allow_self=True)
    except ValueError as exc:
        raise ExtractionError(f"поле {spec.name}: {exc}") from exc
    if steps == (".",):
        matches = [container]
    else:
        matches = _walk([container], steps, from_root=False)

    if spec.multiple:
        if not matches:
            if spec.required:
                return None, _missing(spec, card_number, "совпадений нет")
            return [], None
        values: list[str] = []
        for match in matches:
            value, missing = _read_element(match, spec)
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
    value, missing = _read_element(matches[0], spec)
    if missing:
        if spec.required:
            return None, _missing(spec, card_number, "атрибут отсутствует")
        return None, None
    return value, None


def _read_element(element: Element, spec: FieldSpec) -> tuple[str, bool]:
    if spec.kind == "text":
        return "".join(element.itertext()), False
    if spec.kind == "xml":
        return ET.tostring(element, encoding="unicode"), False
    attribute = spec.attribute or ""
    if attribute not in element.attrib:
        return "", True
    return element.attrib[attribute], False


def _missing(spec: FieldSpec, card_number: int, reason: str) -> FieldError:
    return FieldError(
        card_number=card_number,
        field_name=spec.name,
        message=f"Поле {spec.name}: {reason} (карточка {card_number})",
    )
