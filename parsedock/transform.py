"""Именованные преобразования значений и снятие дублей.

Пустое значение, ноль и ошибка преобразования остаются разными исходами.
Ошибка не заменяется пустой строкой: вся запись уходит в отчёт, а исходное
значение сохраняется в ошибке.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin

from parsedock.extract.html import ExtractResult, FieldError
from parsedock.profile import FieldSpec, Transform


class _ConversionFailure(Exception):
    def __init__(self, message: str, value: object) -> None:
        super().__init__(message)
        self.message = message
        self.value = value
        self.field_name = ""


def apply_transforms(
    result: ExtractResult,
    fields: tuple[FieldSpec, ...],
    *,
    report_absence: bool = True,
) -> None:
    """Применить операции профиля к уже извлечённым записям."""

    if not any(field.transforms or field.keep_original for field in fields):
        if report_absence:
            result.warnings.extend(_missing_warnings(result.records, fields))
        return

    numbers = result.card_numbers or list(range(1, len(result.records) + 1))
    kept: list[dict] = []
    kept_numbers: list[int] = []
    for record, card_number in zip(result.records, numbers, strict=True):
        try:
            kept.append(_transform_record(record, fields))
        except _ConversionFailure as exc:
            result.errors.append(
                FieldError(
                    card_number=card_number,
                    field_name=exc.field_name,
                    message=exc.message,
                    value=exc.value,
                )
            )
            continue
        kept_numbers.append(card_number)
    result.records = kept
    result.card_numbers = kept_numbers
    if report_absence:
        result.warnings.extend(_missing_warnings(result.records, fields))


def note_absent_fields(result: ExtractResult, fields: tuple[FieldSpec, ...]) -> None:
    """Предупредить, если поле не найдено ни в одной записи этого запуска."""

    result.warnings.extend(_missing_warnings(result.records, fields))


def deduplicate(result: ExtractResult, keys: tuple[str, ...]) -> None:
    """Оставить первую запись для каждого ключа.

    Пустой ключ не объединяет записи: отсутствие значения не считается
    совпадением с другой пустой записью.
    """

    if not keys:
        return
    seen: set[tuple[str, ...]] = set()
    kept: list[dict] = []
    removed = 0
    for record in result.records:
        marker = _duplicate_marker(record, keys)
        if marker is None:
            kept.append(record)
            continue
        if marker in seen:
            removed += 1
            continue
        seen.add(marker)
        kept.append(record)
    result.records = kept
    result.duplicates_removed = removed


def _transform_record(record: dict, fields: tuple[FieldSpec, ...]) -> dict:
    transformed: dict = {}
    for field in fields:
        raw = record.get(field.name)
        try:
            transformed[field.name] = _apply_chain(raw, field.transforms, str(record.get("source_url", "")))
        except _ConversionFailure as exc:
            exc.field_name = field.name
            raise
        if field.keep_original:
            transformed[f"{field.name}_original"] = list(raw) if isinstance(raw, list) else raw
    transformed["source_url"] = record.get("source_url")
    transformed["fetched_at"] = record.get("fetched_at")
    return transformed


def _apply_chain(value: object, transforms: tuple[Transform, ...], source_url: str) -> object:
    current = value
    for transform in transforms:
        current = _apply(current, transform, source_url)
    return current


def _apply(value: object, transform: Transform, source_url: str) -> object:
    if isinstance(value, list):
        return [_apply_scalar(item, transform, source_url) for item in value]
    return _apply_scalar(value, transform, source_url)


def _apply_scalar(value: object, transform: Transform, source_url: str) -> object:
    if value is None:
        return None
    if transform.op == "normalize_whitespace":
        return _normalize_whitespace(value)
    if transform.op == "absolute_url":
        return _absolute_url(value, transform.base_url or source_url)
    if transform.op == "parse_number":
        return _parse_number(value, transform)
    if transform.op == "parse_currency":
        return _parse_currency(value, transform)
    if transform.op == "parse_date":
        return _parse_date(value, transform)
    raise _ConversionFailure(f"Неизвестная операция {transform.op}", value)


def _normalize_whitespace(value: object) -> str:
    if not isinstance(value, str):
        raise _ConversionFailure("Для очистки пробелов ожидалась строка", value)
    return re.sub(r"\s+", " ", value).strip()


def _absolute_url(value: object, base_url: str) -> str:
    if value == "":
        return ""
    if not isinstance(value, str):
        raise _ConversionFailure("Для адреса ожидалась строка", value)
    if base_url == "":
        raise _ConversionFailure("Нет адреса страницы, от которого можно построить ссылку", value)
    return urljoin(base_url, value)


def _parse_number(value: object, transform: Transform) -> object:
    if value == "":
        return ""
    if not isinstance(value, str):
        raise _ConversionFailure("Для числа ожидалась строка", value)
    return _decimal_token(value, transform.decimal_separator or ".", transform.thousands_separator)


def _parse_currency(value: object, transform: Transform) -> object:
    if value == "":
        return ""
    if not isinstance(value, str):
        raise _ConversionFailure("Для денежной суммы ожидалась строка", value)
    marker = transform.symbol if transform.symbol is not None else transform.code or ""
    if value.count(marker) != 1:
        raise _ConversionFailure(f"В значении нет ровно одного обозначения {marker!r}", value)
    remainder = value.replace(marker, "", 1).strip()
    if remainder == "":
        return ""
    return _decimal_token(remainder, transform.decimal_separator or ".", transform.thousands_separator)


def _decimal_token(text: str, decimal_separator: str, thousands_separator: str | None) -> int | str:
    if thousands_separator and thousands_separator in text:
        pieces = text.split(decimal_separator)
        if len(pieces) > 2:
            raise _ConversionFailure("В числе несколько десятичных разделителей", text)
        integer_part = _join_thousands(pieces[0], thousands_separator, text)
        fraction = pieces[1] if len(pieces) == 2 else None
        if fraction is not None and (not fraction.isdigit() or thousands_separator in fraction):
            raise _ConversionFailure("Дробная часть должна состоять из цифр", text)
        normalized = integer_part if fraction is None else f"{integer_part}.{fraction}"
    else:
        if decimal_separator != "." and "." in text:
            raise _ConversionFailure("Точка не задана как разделитель", text)
        if text.count(decimal_separator) > 1:
            raise _ConversionFailure("В числе несколько десятичных разделителей", text)
        normalized = text.replace(decimal_separator, ".", 1)
    if re.fullmatch(r"-?\d+(\.\d+)?", normalized) is None:
        raise _ConversionFailure("Значение не является числом в заданном формате", text)
    try:
        number = Decimal(normalized)
    except InvalidOperation as exc:
        raise _ConversionFailure("Значение не является числом в заданном формате", text) from exc
    integral = number.to_integral_value()
    if number == integral:
        return int(integral)
    return format(number, "f")


def _join_thousands(integer_part: str, separator: str, original: str) -> str:
    sign = ""
    digits = integer_part
    if digits.startswith("-"):
        sign = "-"
        digits = digits[1:]
    groups = digits.split(separator)
    if any(group == "" for group in groups):
        raise _ConversionFailure("Некорректная группировка разрядов", original)
    if not groups[0].isdigit() or not 1 <= len(groups[0]) <= 3:
        raise _ConversionFailure("Некорректная группировка разрядов", original)
    if any((not group.isdigit()) or len(group) != 3 for group in groups[1:]):
        raise _ConversionFailure("Некорректная группировка разрядов", original)
    return sign + "".join(groups)


def _parse_date(value: object, transform: Transform) -> str:
    if value == "":
        return ""
    if not isinstance(value, str):
        raise _ConversionFailure("Для даты ожидалась строка", value)
    fmt = transform.date_format or ""
    try:
        parsed = datetime.strptime(value, fmt)
    except ValueError as exc:
        raise _ConversionFailure("Дата не совпала с заданным форматом", value) from exc
    has_time = re.search(r"%[HIMpSXf]", fmt) is not None
    if not has_time:
        return parsed.date().isoformat()
    if transform.timezone_offset is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=transform.timezone_offset)
    return parsed.isoformat()


def _duplicate_marker(record: dict, keys: tuple[str, ...]) -> tuple[str, ...] | None:
    parts: list[str] = []
    for key in keys:
        value = record.get(key)
        if value is None or value == "" or value == []:
            return None
        parts.append(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return tuple(parts)


def _missing_warnings(records: list[dict], fields: tuple[FieldSpec, ...]) -> list[str]:
    if not records:
        return []
    warnings: list[str] = []
    for field in fields:
        if all(_is_absent(record.get(field.name)) for record in records):
            warnings.append(f"Поле {field.name} не найдено ни в одной записи этого запуска.")
    return warnings


def _is_absent(value: object) -> bool:
    return value is None or value == []

