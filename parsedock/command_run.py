"""Постоянная сессия оболочки, которую ведёт пользователь.

Сессия не привязана к папке проекта. Она начинается в домашней папке
пользователя, а cd и переменные сохраняются для следующих команд.
На Windows это cmd или PowerShell. На Linux это sh или bash, если он есть.

Это ввод команд в одну оболочку, не отдельное окно с полным экраном:
программы вроде vim из этой панели не управляются. Текст команды
не собирается из адреса страницы. Сетевой порт не открывается.
shell=True не используется.
"""

from __future__ import annotations

import locale
import os
import queue
import shutil
import signal
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from parsedock.errors import RunError


@dataclass(frozen=True)
class ShellSpec:
    """Оболочка для долгой сессии."""

    shell_id: str
    label: str
    executable: str
    flags: tuple[str, ...]


def available_shells() -> tuple[ShellSpec, ...]:
    """Оболочки, которые реально найдены на этой машине."""

    if os.name == "nt":
        found: list[ShellSpec] = []
        cmd = os.environ.get("ComSpec") or shutil.which("cmd.exe") or shutil.which("cmd")
        if cmd:
            found.append(ShellSpec("cmd", "cmd", cmd, ("/d", "/q", "/k")))
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if powershell:
            found.append(
                ShellSpec(
                    "powershell",
                    "PowerShell",
                    powershell,
                    ("-NoLogo", "-NoProfile", "-NoExit"),
                )
            )
        return tuple(found)

    found = []
    sh = shutil.which("sh")
    if sh is None and Path("/bin/sh").is_file():
        sh = "/bin/sh"
    if sh:
        found.append(ShellSpec("sh", "sh", sh, ()))
    bash = shutil.which("bash")
    if bash:
        found.append(ShellSpec("bash", "bash", bash, ("--noprofile", "--norc")))
    return tuple(found)


def session_argv(shell: ShellSpec) -> list[str]:
    """Аргументы процесса оболочки. Текста команды здесь нет."""

    return [shell.executable, *shell.flags]


def decode_output(data: bytes) -> str:
    """Прочитать байты вывода. Сначала UTF-8, затем кодировка системы."""

    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False), errors="replace")


class ShellSession:
    """Одна открытая оболочка. Следующая команда видит cd предыдущей."""

    def __init__(self, shell: ShellSpec) -> None:
        self.shell = shell
        self.process: subprocess.Popen | None = None
        self.marker = f"__PARSEDDOCK_{uuid.uuid4().hex}__"
        self._on_text = None
        self._batch: list[str] = []
        self._lock = threading.Lock()
        self._marker_event = threading.Event()
        self._write_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self.output: queue.Queue[str] = queue.Queue()

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, on_text) -> None:
        """Открыть оболочку в домашней папке и дождаться её готовности."""

        if not available_home().is_dir():
            raise RunError("Домашняя папка пользователя не найдена.")
        kwargs: dict = {
            "args": session_argv(self.shell),
            "cwd": available_home(),
            "env": os.environ.copy(),
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "bufsize": 0,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        self._on_text = on_text
        self.process = subprocess.Popen(**kwargs)
        self._reader = threading.Thread(target=self._read_loop)
        self._reader.start()
        self._write(self._startup_command())
        if not self._marker_event.wait(20):
            self.close()
            raise RunError("Оболочка не запустилась.")
        with self._lock:
            self._batch = []
        self._marker_event.clear()

    def send(self, command: str) -> None:
        """Отправить введённую строку в уже открытую оболочку."""

        text = command.strip()
        if not text:
            raise RunError("Команда пустая.")
        if not self.running:
            raise RunError("Сессия оболочки не запущена.")
        with self._lock:
            self._batch = []
            self._marker_event.clear()
        self._write(text)

    def wait_result(self, timeout: float = 20) -> str:
        """Дождаться конца команды и вернуть её вывод."""

        if not self._marker_event.wait(timeout):
            raise RunError("Оболочка не ответила.")
        with self._lock:
            return "".join(self._batch)

    def close(self) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            try:
                if process.stdin is not None:
                    line = b"exit\r\n" if self.shell.shell_id == "cmd" else b"exit\n"
                    process.stdin.write(line)
                    process.stdin.flush()
                    process.stdin.close()
            except Exception:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                stop_command(process)
        if self._reader is not None:
            self._reader.join(timeout=3)

    def _startup_command(self) -> str:
        if self.shell.shell_id == "cmd":
            return "chcp 65001>nul"
        if self.shell.shell_id == "powershell":
            return (
                "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false; "
                "$OutputEncoding = [Console]::OutputEncoding"
            )
        return "true"

    def _write(self, command: str) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise RunError("Сессия оболочки не запущена.")
        newline = "\r\n" if self.shell.shell_id == "cmd" else "\n"
        if self.shell.shell_id == "powershell":
            trailer = f"Write-Output '{self.marker}'"
        elif self.shell.shell_id == "cmd":
            trailer = f"echo {self.marker}"
        else:
            trailer = f"printf '%s\\n' '{self.marker}'"
        payload = f"{command}{newline}{trailer}{newline}"
        with self._write_lock:
            process.stdin.write(payload.encode("utf-8"))
            process.stdin.flush()

    def _read_loop(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        pending = ""
        try:
            while True:
                data = process.stdout.read(256)
                if not data:
                    break
                pending += decode_output(data)
                while self.marker in pending:
                    before, pending = pending.split(self.marker, 1)
                    self._emit(before)
                    self._marker_event.set()
        except Exception:
            return
        finally:
            self._marker_event.set()

    def _emit(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._batch.append(text)
        self.output.put(text)
        if self._on_text is not None:
            self._on_text(text)


def available_home() -> Path:
    return Path.home()


def stop_command(process: subprocess.Popen) -> None:
    """Завершить оболочку и процессы, которые она успела запустить."""

    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
        process.wait(timeout=3)
