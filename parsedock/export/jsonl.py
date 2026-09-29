"""Запись записей в JSONL.

JSONL — текстовый файл, в котором каждая строка является отдельным
JSON-объектом. Строки разделяются символом \\n и на Windows тоже,
чтобы один и тот же файл одинаково читался на Linux и Windows.

Режимы:
- create — создать новый файл и отказаться, если он уже есть;
- replace — заменить существующий файл;
- append — дописать строки в конец, создав файл при его отсутствии.

Проверка create уменьшает случайную перезапись, но не является
атомарной транзакцией файловой системы.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path

from parsedock.errors import ExportError
from parsedock.export.target import refuse_symlink

_OPEN_MODE = {"create": "x", "replace": "w", "append": "a"}


def write_jsonl(path: Path | str, records: Iterable[Mapping], *, mode: str = "create") -> None:
    """Записать записи в файл по одной строке на запись."""

    if mode not in _OPEN_MODE:
        raise ExportError(
            f"Режим экспорта {mode!r} не поддерживается. Доступны create, replace и append."
        )

    destination = Path(path)
    refuse_symlink(destination)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open(_OPEN_MODE[mode], encoding="utf-8", newline="\n") as handle:
            for record in records:
                if not isinstance(record, Mapping):
                    raise ExportError("Запись JSONL должна быть объектом")
                try:
                    line = json.dumps(record, ensure_ascii=False)
                except TypeError as exc:
                    raise ExportError("Запись нельзя преобразовать в JSON") from exc
                handle.write(line)
                handle.write("\n")
    except FileExistsError as exc:
        raise ExportError(
            f"Файл уже существует: {destination}. Режим create не перезаписывает его."
        ) from exc
    except ExportError:
        raise
    except OSError as exc:
        raise ExportError(f"Не удалось записать файл: {destination}") from exc
