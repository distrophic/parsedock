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

        # Без явных типов ctypes портит стек на 64-bit, и процесс падает
        # ещё до выхода. TerminateProcess не выгружает DLL Qt.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        kernel32.TerminateProcess.restype = ctypes.c_int
        kernel32.TerminateProcess(kernel32.GetCurrentProcess(), code & 0xFFFFFFFF)
    os._exit(code)
