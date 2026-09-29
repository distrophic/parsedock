"""Запись записей в CSV.

Отсутствующее значение и пустая строка в CSV выглядят одинаково: оба
становятся пустой ячейкой. Различие сохраняется в JSONL и в отчёте об ошибках.
Ноль записывается как 0.

spreadsheet_safe=false пишет исходный текст. spreadsheet_safe=true добавляет
апостроф перед текстом, который табличный редактор может принять за формулу.
Такой апостроф меняет значение и в файл попадает уже не исходная строка.
Числа этот режим не меняет.

Строки файла заканчиваются на \\r\\n: так их читают и Python на Linux, и
табличные редакторы на Windows. Создающий режим не перезаписывает файл,
но не является атомарной транзакцией.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from parsedock.errors import ExportError
from parsedock.export.target import refuse_symlink

_OPEN_MODE = {"create": "x", "replace": "w", "append": "a"}
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def write_csv(
    path: Path | str,
    records: Iterable[Mapping],
    columns: Sequence[str],
    *,
    mode: str = "create",
    encoding: str = "utf-8",
    delimiter: str = ",",
    spreadsheet_safe: bool = False,
    bom: bool = False,
) -> None:
    """Записать строки CSV в заданном порядке столбцов."""

    if mode not in _OPEN_MODE:
        raise ExportError(
            f"Режим экспорта {mode!r} не поддерживается. Доступны create, replace и append."
        )
    destination = Path(path)
    refuse_symlink(destination)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        existing_size = destination.stat().st_size if destination.exists() else 0
        skip_header = mode == "append" and existing_size > 0
        needs_break = skip_header and not _ends_with_newline(destination)
        file_encoding = encoding
        if bom and existing_size == 0 and encoding.lower().replace("-", "") == "utf8":
            file_encoding = "utf-8-sig"
        with destination.open(_OPEN_MODE[mode], encoding=file_encoding, newline="") as handle:
            if needs_break:
                handle.write("\n")
            writer = csv.writer(
                handle,
                delimiter=delimiter,
                quoting=csv.QUOTE_MINIMAL,
                lineterminator="\r\n",
            )
            if not skip_header:
                writer.writerow(list(columns))
            for record in records:
                if not isinstance(record, Mapping):
                    raise ExportError("Запись CSV должна быть объектом")
                writer.writerow(
                    [_cell(record.get(column), spreadsheet_safe=spreadsheet_safe) for column in columns]
                )
    except FileExistsError as exc:
        raise ExportError(
            f"Файл уже существует: {destination}. Режим create не перезаписывает его."
        ) from exc
    except ExportError:
        raise
    except UnicodeEncodeError as exc:
        raise ExportError(f"Символ не помещается в кодировку {encoding}: {destination}") from exc
    except OSError as exc:
        raise ExportError(f"Не удалось записать файл: {destination}") from exc


def _cell(value: object, *, spreadsheet_safe: bool) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        text = json.dumps(value, ensure_ascii=False)
    elif isinstance(value, str):
        text = value
    else:
        text = str(value)
    if spreadsheet_safe and text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def _ends_with_newline(path: Path) -> bool:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        if size == 0:
            return True
        handle.seek(size - 1)
        return handle.read(1) == b"\n"
