"""Сборка короткого скрипта из профиля. Модель по сети не вызывается.

Скрипт только показывается, пока пользователь сам не сохранит файл
и отдельно не подтвердит запуск. Проверка синтаксиса смотрит,
что текст является программой Python. Это не проверка безопасности.
"""

from __future__ import annotations

import ast
import copy
import json
import subprocess
import sys
from pathlib import Path

from parsedock.errors import ProfileError


def generate_script(document: dict) -> str:
    """Собрать читаемый скрипт. Заголовки запроса в текст не попадают."""

    if not isinstance(document, dict):
        raise ProfileError("Для генерации нужен профиль-объект.")
    body = copy.deepcopy(document)
    source = body.get("source")
    if isinstance(source, dict):
        source.pop("headers", None)
    profile_json = json.dumps(body, ensure_ascii=False, indent=2)
    return (
        '"""Скрипт ParseDock, собранный из профиля в окне.\n'
        "\n"
        "Модель по сети не вызывается. Заголовки запроса сюда не копируются.\n"
        "Пути файлов считаются от папки этого скрипта.\n"
        '"""\n'
        "\n"
        "import json\n"
        "from pathlib import Path\n"
        "\n"
        "from parsedock import parse_profile, run_profile\n"
        "\n"
        f"PROFILE = json.loads({profile_json!r})\n"
        "\n"
        "\n"
        "def main() -> None:\n"
        "    profile = parse_profile(PROFILE, base_dir=Path(__file__).resolve().parent)\n"
        "    result = run_profile(profile)\n"
        '    print(f"Остановка: {result.stop_reason}")\n'
        '    print(f"Записей: {len(result.records)}")\n'
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    main()\n"
    )


def syntax_status(source: str) -> str:
    """Сообщение о синтаксисе. В тексте прямо сказано, что это не безопасность."""

    try:
        ast.parse(source)
    except SyntaxError as exc:
        line = exc.lineno or 0
        return (
            f"Синтаксическая ошибка, строка {line}: {exc.msg}. "
            "Это не проверка безопасности."
        )
    return "Синтаксис в порядке. Это не проверка безопасности."


def run_saved_script(path: Path) -> subprocess.CompletedProcess[str]:
    """Запустить уже сохранённый файл текущим интерпретатором.

    Вызов сам по себе не решает, можно ли это делать. Окно спрашивает
    подтверждение до этого вызова.
    """

    return subprocess.run(
        [sys.executable, str(path)],
        cwd=str(path.parent),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
