"""Запись записей в файл SQLite.

В таблице records отсутствуют значение и пустая строка различаются:
None становится NULL, пустая строка остаётся пустой строкой, ноль остаётся 0.
Список сохраняется текстом JSON в одной ячейке, как в CSV.

Режимы create, replace и append те же, что у JSONL и CSV.
create не перезаписывает существующий файл, но проверка и запись
не являются одной атомарной операцией файловой системы.
replace сначала собирает новую базу рядом со старой и только потом
подменяет имя файла. Если процесс прервётся во время подмены,
на диске может остаться старый файл или новый.
append дописывает строки в ту же таблицу. Другой набор столбцов
или другая версия схемы отклоняются, прежние строки не удаляются.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from parsedock.errors import ExportError
from parsedock.export.target import refuse_symlink

_MODES = frozenset({"create", "replace", "append"})
_SCHEMA_VERSION = "1"


def write_sqlite(
    path: Path | str,
    records: Iterable[Mapping],
    columns: Sequence[str],
    *,
    mode: str = "create",
) -> None:
    """Записать строки в таблицу records в заданном порядке столбцов."""

    if mode not in _MODES:
        raise ExportError(
            f"Режим экспорта {mode!r} не поддерживается. Доступны create, replace и append."
        )
    if not columns:
        raise ExportError("SQLite: нужен хотя бы один столбец")
    destination = Path(path)
    refuse_symlink(destination)
    temporary = destination.with_name(destination.name + ".new") if mode == "replace" else None
    if temporary is not None:
        refuse_symlink(temporary)
    target = temporary if temporary is not None else destination
    remove_target = False
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if mode == "create" and destination.exists():
            raise ExportError(
                f"Файл уже существует: {destination}. Режим create не перезаписывает его."
            )
        if temporary is not None and temporary.exists():
            raise ExportError(f"Временный файл уже существует: {temporary}")
        if mode == "create":
            remove_target = True
        _write_database(target, records, columns, mode=mode)
        remove_target = temporary is not None
        if temporary is not None:
            os.replace(temporary, destination)
            remove_target = False
    except ExportError:
        if remove_target:
            _remove_partial(target)
        raise
    except sqlite3.Error as exc:
        if remove_target:
            _remove_partial(target)
        raise ExportError(f"Не удалось записать файл: {destination}") from exc
    except OSError as exc:
        if remove_target:
            _remove_partial(target)
        raise ExportError(f"Не удалось записать файл: {destination}") from exc


def _write_database(
    destination: Path,
    records: Iterable[Mapping],
    columns: Sequence[str],
    *,
    mode: str,
) -> None:
    connection = sqlite3.connect(destination)
    try:
        connection.execute("BEGIN")
        if mode == "append" and destination.exists() and _has_records_table(connection):
            _require_same_table(connection, columns)
        else:
            _create_schema(connection, columns)
        quoted = ", ".join(_quote(name) for name in columns)
        placeholders = ", ".join("?" for _ in columns)
        statement = f"INSERT INTO records ({quoted}) VALUES ({placeholders})"
        for record in records:
            if not isinstance(record, Mapping):
                raise ExportError("Запись SQLite должна быть объектом")
            connection.execute(statement, [_value(record.get(column), column) for column in columns])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _create_schema(connection: sqlite3.Connection, columns: Sequence[str]) -> None:
    column_sql = ", ".join(_quote(name) for name in columns)
    connection.execute(f"CREATE TABLE records ({column_sql})")
    connection.execute("CREATE TABLE parsedock_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute(
        "INSERT INTO parsedock_meta (key, value) VALUES ('schema_version', ?)",
        (_SCHEMA_VERSION,),
    )


def _has_records_table(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'records'"
    ).fetchone()
    return row is not None


def _require_same_table(connection: sqlite3.Connection, columns: Sequence[str]) -> None:
    try:
        version = connection.execute(
            "SELECT value FROM parsedock_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.Error as exc:
        raise ExportError("SQLite: в файле нет таблицы parsedock_meta версии 1") from exc
    if version is None or version[0] != _SCHEMA_VERSION:
        raise ExportError("SQLite: файл записан другой версией схемы")
    existing = [
        str(row[1]) for row in connection.execute("PRAGMA table_info(records)").fetchall()
    ]
    if existing != list(columns):
        raise ExportError("SQLite: набор столбцов не совпадает с таблицей records")


def _value(value: object, column: str) -> object:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    raise ExportError(f"SQLite: значение столбца {column!r} нельзя записать")


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _remove_partial(path: Path | None) -> None:
    if path is None or not path.exists():
        return
    try:
        path.unlink()
    except OSError:
        return
