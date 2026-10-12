"""Общий конец прогона.

Окно Qt на сервере GitHub после уже успешных тестов роняет процесс:
на Ubuntu это segmentation fault, на Windows код выхода 1.
Тесты к этому моменту завершены. Закрываем окна и выходим сразу,
не дожидаясь разрушения Qt при завершении интерпретатора.
"""

from __future__ import annotations

import os
import sys

_exit_code: int | None = None


def pytest_sessionfinish(session, exitstatus: int) -> None:
    global _exit_code
    _exit_code = int(exitstatus)


def pytest_unconfigure(config) -> None:
    if _exit_code is None:
        return
    try:
        from PySide6.QtWidgets import QApplication
    except Exception:
        return
    application = QApplication.instance()
    if application is None:
        return
    application.closeAllWindows()
    application.processEvents()
    application.quit()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(_exit_code)
