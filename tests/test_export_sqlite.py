import sqlite3
from pathlib import Path

import pytest

from parsedock.errors import ExportError
from parsedock.export.sqlite import write_sqlite
from parsedock.profile import load_profile
from parsedock.run import run_file_profile

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "cards"


def test_sqlite_keeps_null_empty_string_and_zero_distinct(tmp_path: Path):
    destination = tmp_path / "nested" / "cards.sqlite"
    write_sqlite(
        destination,
        [{"title": "Чайник", "price": None, "count": 0, "note": "", "tags": ["a", "b"]}],
        ["title", "price", "count", "note", "tags"],
        mode="create",
    )

    with sqlite3.connect(destination) as connection:
        row = connection.execute(
            "SELECT title, price, count, note, tags, typeof(price), typeof(count), typeof(note) FROM records"
        ).fetchone()
    assert row[:5] == ("Чайник", None, 0, "", '["a", "b"]')
    assert row[5:] == ("null", "integer", "text")

    with pytest.raises(ExportError, match="уже существует"):
        write_sqlite(destination, [{"title": "ещё"}], ["title"], mode="create")


def test_replace_and_append_follow_the_same_columns(tmp_path: Path):
    destination = tmp_path / "cards.sqlite"
    write_sqlite(destination, [{"title": "один"}], ["title"], mode="create")
    write_sqlite(destination, [{"title": "два"}], ["title"], mode="replace")
    write_sqlite(destination, [{"title": "три"}], ["title"], mode="append")

    with sqlite3.connect(destination) as connection:
        rows = connection.execute("SELECT title FROM records ORDER BY rowid").fetchall()
    assert rows == [("два",), ("три",)]
    assert not destination.with_name("cards.sqlite.new").exists()

    with pytest.raises(ExportError, match="столбцов"):
        write_sqlite(destination, [{"title": "четыре", "price": "1"}], ["title", "price"], mode="append")
    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 2


def test_failed_value_does_not_leave_a_create_file(tmp_path: Path):
    destination = tmp_path / "cards.sqlite"
    with pytest.raises(ExportError, match="нельзя записать"):
        write_sqlite(destination, [{"title": {"bad": 1}}], ["title"], mode="create")
    assert not destination.exists()


def test_saved_page_is_written_to_sqlite(tmp_path: Path):
    page = (EXAMPLE / "page.html").read_text(encoding="utf-8")
    (tmp_path / "page.html").write_text(page, encoding="utf-8")
    profile_text = (EXAMPLE / "profile-sqlite.json").read_text(encoding="utf-8")
    (tmp_path / "profile.json").write_text(profile_text, encoding="utf-8")

    result = run_file_profile(load_profile(tmp_path / "profile.json"))

    assert len(result.records) == 3
    with sqlite3.connect(tmp_path / "cards.sqlite") as connection:
        titles = [row[0] for row in connection.execute("SELECT title FROM records ORDER BY rowid")]
        prices = [row[0] for row in connection.execute("SELECT price FROM records ORDER BY rowid")]
    assert titles == ["Чайник", "Кружка", "Ложка"]
    assert prices[0] == "1200"
    assert prices[2] == ""
    assert "999" not in prices
    assert "999" not in titles
