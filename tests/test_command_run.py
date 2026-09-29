import sys
from pathlib import Path

from parsedock.command_run import (
    ShellSession,
    available_home,
    available_shells,
    decode_output,
    session_argv,
)
from parsedock.errors import RunError

EXE = sys.executable


def test_shells_on_this_machine_point_at_real_programs():
    shells = available_shells()
    assert shells
    for shell in shells:
        assert Path(shell.executable).is_file()
    if sys.platform == "win32":
        assert shells[0].shell_id == "cmd"
    else:
        assert shells[0].shell_id == "sh"


def test_session_starts_without_putting_the_command_in_argv():
    shell = available_shells()[0]
    argv = session_argv(shell)
    assert argv[0] == shell.executable
    assert "example.invalid" not in " ".join(argv)


def test_session_starts_at_home_and_keeps_cd(tmp_path: Path):
    shell = available_shells()[0]
    session = ShellSession(shell)
    session.start(lambda _text: None)
    try:
        session.send(_cwd_command())
        home_text = session.wait_result()
        assert str(available_home()).casefold() in home_text.casefold()
        session.send(_cd_command(shell.shell_id, tmp_path))
        session.wait_result()
        session.send(_cwd_command())
        moved = session.wait_result()
        assert str(tmp_path).casefold() in moved.casefold()
    finally:
        session.close()


def test_typed_command_is_visible(tmp_path: Path):
    shell = available_shells()[0]
    session = ShellSession(shell)
    session.start(lambda _text: None)
    try:
        session.send(f'"{EXE}" -c "print(12345)"')
        assert "12345" in session.wait_result()
    finally:
        session.close()


def test_stderr_reaches_the_session():
    shell = available_shells()[0]
    session = ShellSession(shell)
    session.start(lambda _text: None)
    try:
        session.send(f'"{EXE}" -c "import sys; sys.stderr.write(\'err-marker\\n\')"')
        assert "err-marker" in session.wait_result()
    finally:
        session.close()


def test_empty_command_is_rejected():
    shell = available_shells()[0]
    session = ShellSession(shell)
    session.start(lambda _text: None)
    try:
        try:
            session.send("   ")
            raised = False
        except RunError as exc:
            raised = "пустая" in str(exc)
        assert raised
    finally:
        session.close()


def test_close_stops_a_long_command():
    shell = available_shells()[0]
    session = ShellSession(shell)
    session.start(lambda _text: None)
    session.send(f'"{EXE}" -c "import time; time.sleep(30)"')
    session.close()
    assert session.process is not None
    assert session.process.wait(timeout=8) is not None


def test_decode_output_keeps_utf8_text():
    assert decode_output("Чайник".encode("utf-8")) == "Чайник"


def _cwd_command() -> str:
    return f'"{EXE}" -c "import os; print(os.getcwd())"'


def _cd_command(shell_id: str, path: Path) -> str:
    if shell_id == "cmd":
        return f'cd /d "{path}"'
    return f'cd "{path}"'
