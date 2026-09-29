import ast
from pathlib import Path

from parsedock.codegen import generate_script, run_saved_script, syntax_status


def test_script_keeps_the_profile_and_drops_headers():
    document = {
        "schema_version": 1,
        "name": "desktop",
        "source": {
            "type": "url",
            "url": 'https://shop.example/list?q=a"b',
            "client": "requests",
            "headers": {"Authorization": "Bearer secret-token"},
        },
        "extraction": {
            "format": "html",
            "container": "article.card",
            "fields": [{"name": "title", "selector": "h2", "kind": "text", "required": True}],
        },
        "export": {"format": "jsonl", "path": "cards.jsonl", "mode": "create"},
    }
    source = generate_script(document)
    ast.parse(source)
    assert "article.card" in source
    assert "https://shop.example/list" in source
    assert "secret-token" not in source
    assert "Authorization" not in source
    assert syntax_status(source) == "Синтаксис в порядке. Это не проверка безопасности."
    assert "не проверка безопасности" in syntax_status("def broken(:\n")


def test_saved_script_runs_only_when_called(tmp_path: Path):
    script = tmp_path / "hello.py"
    script.write_text("print('codegen-ok')\n", encoding="utf-8")
    completed = run_saved_script(script)
    assert completed.returncode == 0
    assert "codegen-ok" in completed.stdout
