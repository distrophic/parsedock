"""Проверка пути перед записью результата.

Если итоговое имя — символическая ссылка, запись не начинается.
Так содержимое не попадает в другой файл, на который указывает ссылка.
"""

from __future__ import annotations

from pathlib import Path

from parsedock.errors import ExportError


def refuse_symlink(path: Path) -> None:
    """Остановить запись, если путь сам является ссылкой."""

    if path.is_symlink():
        raise ExportError(
            f"Путь является ссылкой на другой файл: {path}. Запись через ссылку не выполняется."
        )


def write_text_refusing_symlink(path: Path, text: str) -> None:
    """Записать текст, если путь не является ссылкой на другой файл."""

    refuse_symlink(path)
    path.write_text(text, encoding="utf-8")
