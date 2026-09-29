import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from parsedock.errors import ExportError
from parsedock.export.csv import write_csv
from parsedock.profile import parse_profile
from parsedock.run import run_file_profile

FETCHED_AT = datetime(2026, 9, 22, 13, 30, tzinfo=timezone.utc)


def test_csv_quotes_newlines_distinguishes_zero_and_can_protect_formulas(tmp_path: Path):
    destination = tmp_path / "cards.csv"
    records = [
        {"title": "Чай\nник", "price": 0, "note": "", "formula": "=1+1", "source_url": "u", "fetched_at": "t"},
        {"title": "Пусто", "price": None, "note": "", "formula": "обычный", "source_url": "u", "fetched_at": "t"},
    ]
    columns = ["title", "price", "note", "formula", "source_url", "fetched_at"]

    write_csv(destination, records, columns, delimiter=";", spreadsheet_safe=True)

    raw = destination.read_bytes()
    assert b"\r\n" in raw
    with destination.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle, delimiter=";"))
    assert rows[0] == columns
    assert rows[1][0] == "Чай\nник"
    assert rows[1][1] == "0"
    assert rows[1][2] == ""
    assert rows[1][3] == "'=1+1"
    assert rows[2][1] == ""

    plain = tmp_path / "plain.csv"
    write_csv(plain, records, columns, spreadsheet_safe=False)
    with plain.open(encoding="utf-8", newline="") as handle:
        plain_rows = list(csv.reader(handle))
    assert plain_rows[1][3] == "=1+1"

    with pytest.raises(ExportError, match="уже существует"):
        write_csv(destination, records, columns, mode="create")


def test_bom_is_written_for_utf8_and_missing_characters_fail(tmp_path: Path):
    destination = tmp_path / "cards.csv"
    write_csv(destination, [{"title": "Чай"}], ["title"], bom=True)
    assert destination.read_bytes().startswith(b"\xef\xbb\xbf")

    blocked = tmp_path / "blocked.csv"
    with pytest.raises(ExportError, match="кодировку"):
        write_csv(blocked, [{"title": "снежинка \u2603"}], ["title"], encoding="cp1251")


def test_profile_run_writes_csv_columns_in_field_order(tmp_path: Path):
    page = tmp_path / "page.html"
    page.write_text(
        '<article class="card"><h2>Чайник</h2><a href="/kettle">открыть</a></article>',
        encoding="utf-8",
    )
    profile = parse_profile(
        {
            "schema_version": 1,
            "name": "csv",
            "source": {"type": "file", "path": "page.html"},
            "extraction": {
                "format": "html",
                "container": "article.card",
                "fields": [
                    {"name": "title", "selector": "h2", "kind": "text"},
                    {
                        "name": "url",
                        "selector": "a",
                        "kind": "attribute",
                        "attribute": "href",
                        "transforms": [{"op": "absolute_url", "base_url": "https://shop.example/"}],
                    },
                ],
            },
            "export": {"format": "csv", "path": "cards.csv", "mode": "create"},
        },
        base_dir=tmp_path,
    )

    result = run_file_profile(profile, fetched_at=FETCHED_AT)

    assert result.errors == []
    with (tmp_path / "cards.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == ["title", "url", "source_url", "fetched_at"]
    assert rows[1][0] == "Чайник"
    assert rows[1][1] == "https://shop.example/kettle"
    assert json.dumps(result.records[0]["url"]) == '"https://shop.example/kettle"'
