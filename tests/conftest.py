"""Общий конец прогона.

Окно Qt на сервере GitHub после уже успешных тестов роняет процесс
при выгрузке библиотек: на Ubuntu это segmentation fault, на Windows
access violation. Итог тестов к этому моменту уже напечатан.
Выходим, не давая Qt разобрать свои объекты.
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
    if QApplication.instance() is None:
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    _leave(_exit_code)


def _leave(code: int) -> None:
    if sys.platform == "win32":
        import ctypes

        # os._exit на Windows всё равно вызывает выгрузку DLL, и Qt падает.
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.kernel32.TerminateProcess(handle, code & 0xFFFFFFFF)
    os._exit(code)
