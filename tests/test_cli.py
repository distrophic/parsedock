import json
import locale
import sys
from pathlib import Path

import pytest

from parsedock.__main__ import main

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "cards"


def test_cli_writes_records_from_a_saved_page(tmp_path, capsys):
    page = (EXAMPLE / "page.html").read_text(encoding="utf-8")
    (tmp_path / "page.html").write_text(page, encoding="utf-8")
    profile = json.loads((EXAMPLE / "profile.json").read_text(encoding="utf-8"))
    (tmp_path / "profile.json").write_text(json.dumps(profile), encoding="utf-8")

    assert main([str(tmp_path / "profile.json")]) == 0

    lines = (tmp_path / "cards.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert "Чайник" in lines[0]
    output = capsys.readouterr().out
    assert "Записей: 3" in output
    assert "999" not in output


def test_cli_reports_a_missing_profile(tmp_path, capsys):
    assert main([str(tmp_path / "missing.json")]) == 1
    assert "не найден" in capsys.readouterr().err


def test_cli_asks_for_a_profile_path(capsys):
    assert main([]) == 2
    assert "profile.json" in capsys.readouterr().err


def test_cli_exits_when_a_card_has_an_error(tmp_path, capsys):
    (tmp_path / "page.html").write_text(
        "<article class='card'><h2>Один</h2></article><article class='card'></article>",
        encoding="utf-8",
    )
    profile = {
        "schema_version": 1,
        "name": "partial",
        "source": {"type": "file", "path": "page.html"},
        "extraction": {
            "format": "html",
            "container": "article.card",
            "fields": [{"name": "title", "selector": "h2", "kind": "text", "required": True}],
        },
        "export": {"format": "jsonl", "path": "out.jsonl", "mode": "create"},
    }
    (tmp_path / "profile.json").write_text(json.dumps(profile), encoding="utf-8")

    assert main([str(tmp_path / "profile.json")]) == 3

    assert len((tmp_path / "out.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert "Ошибок: 1" in capsys.readouterr().out


def test_module_command_runs_the_saved_page(tmp_path):
    page = (EXAMPLE / "page.html").read_text(encoding="utf-8")
    (tmp_path / "page.html").write_text(page, encoding="utf-8")
    profile = json.loads((EXAMPLE / "profile.json").read_text(encoding="utf-8"))
    (tmp_path / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
    completed = pytest.importorskip("subprocess").run(
        [sys.executable, "-m", "parsedock", str(tmp_path / "profile.json")],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
    )
    output = completed.stdout.decode(locale.getpreferredencoding(False), errors="replace")
    assert completed.returncode == 0
    assert "Записей: 3" in output
    assert (tmp_path / "cards.jsonl").exists()
