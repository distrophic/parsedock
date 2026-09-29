"""Запуск профиля из командной строки: python -m parsedock profile.json

Это не встроенный терминал и не оболочка. Команда только читает один
локальный профиль и вызывает то же ядро, что и окно. Адрес из профиля
сам в команду оболочки не превращается.
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path

from parsedock.errors import ParseDockError
from parsedock.jobs import Job
from parsedock.profile import load_profile
from parsedock.run import run_profile

_USAGE = """\
Использование: python -m parsedock profile.json

Профиль — локальный JSON. Результат пишется в файл из этого профиля.
Повторы, обход страниц и разбор HTML берутся оттуда же.

Коды выхода:
  0  завершено
  1  профиль или запуск не удались
  2  команда вызвана без пути к профилю
  3  завершено с ошибками, файл результата уже записан
  4  отменено
"""

_EXIT = {
    "completed": 0,
    "failed": 1,
    "completed_with_errors": 3,
    "cancelled": 4,
}


def main(argv: list[str] | None = None) -> int:
    """Выполнить один профиль. argv без имени программы."""

    args = list(sys.argv[1:] if argv is None else argv)
    if args in (["-h"], ["--help"]):
        print(_USAGE, end="")
        return 0
    if len(args) != 1:
        print(_USAGE, end="", file=sys.stderr)
        return 2
    job = Job()
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_args: job.cancel())
    try:
        try:
            profile = load_profile(Path(args[0]))
            run_profile(profile, job=job)
        except ParseDockError as exc:
            print(str(exc), file=sys.stderr)
            if job.state != "pending":
                print(_summary(job), file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            job.cancel()
            print("Отмена запрошена.", file=sys.stderr)
            return 4
    finally:
        signal.signal(signal.SIGINT, previous)
    print(_summary(job))
    return _EXIT.get(job.state, 1)


def _summary(job: Job) -> str:
    text = job.status()
    lines = [
        str(text["state_label"]),
        (
            f"Страниц: {text['pages_fetched']}. Записей: {text['record_count']}. "
            f"Дублей: {text['duplicates_removed']}. Ошибок: {text['error_count']}."
        ),
    ]
    if text["output_path"]:
        lines.append(f"Файл: {text['output_path']}")
    if text["stop_reason"]:
        lines.append(f"Остановка: {text['stop_reason']}.")
    if text["message"]:
        lines.append(str(text["message"]))
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
