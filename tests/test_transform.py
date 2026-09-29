from datetime import datetime, timezone
from pathlib import Path

import pytest

from parsedock.errors import ProfileError
from parsedock.extract.html import extract_html
from parsedock.profile import ExtractionSpec, FieldSpec, Transform, parse_profile
from parsedock.run import run_file_profile
from parsedock.transform import apply_transforms, deduplicate

FETCHED_AT = datetime(2026, 9, 22, 13, 0, tzinfo=timezone.utc)


def profile_with(fields: list[dict], **extra) -> dict:
    data = {
        "schema_version": 1,
        "name": "sample",
        "source": {"type": "file", "path": "page.html"},
        "extraction": {"format": "html", "container": "article.card", "fields": fields},
        "export": {"format": "jsonl", "path": "out.jsonl"},
    }
    data.update(extra)
    return data


def test_whitespace_number_zero_empty_and_failure_stay_distinct(tmp_path: Path):
    html = """
    <article class="card"><h2>  Чай  ник </h2><span class="price">1 200</span></article>
    <article class="card"><h2>Ноль</h2><span class="price">0</span></article>
    <article class="card"><h2>Пусто</h2><span class="price"></span></article>
    <article class="card"><h2>Нет цены</h2></article>
    <article class="card"><h2>Мусор</h2><span class="price">abc</span></article>
    <article class="card"><h2>Точка</h2><span class="price">12.50</span></article>
    <article class="card"><h2>Дробь</h2><span class="price">1,50</span></article>
    """
    fields = [
        {
            "name": "title",
            "selector": "h2",
            "kind": "text",
            "transforms": [{"op": "normalize_whitespace"}],
        },
        {
            "name": "price",
            "selector": ".price",
            "kind": "text",
            "keep_original": True,
            "transforms": [
                {"op": "normalize_whitespace"},
                {"op": "parse_number", "decimal_separator": ",", "thousands_separator": " "},
            ],
        },
    ]
    profile = parse_profile(profile_with(fields), base_dir=tmp_path)
    result = extract_html(html, profile.extraction, source_url="https://shop.example/list", fetched_at=FETCHED_AT)
    apply_transforms(result, profile.extraction.fields)

    assert [(row["title"], row["price"]) for row in result.records] == [
        ("Чай ник", 1200),
        ("Ноль", 0),
        ("Пусто", ""),
        ("Нет цены", None),
        ("Дробь", "1.50"),
    ]
    assert result.records[0]["price_original"] == "1 200"
    assert [error.value for error in result.errors] == ["abc", "12.50"]
    assert result.duplicates_removed == 0


def test_absolute_url_date_and_currency(tmp_path: Path):
    html = """
    <article class="card">
      <a href="/kettle"></a>
      <span class="price">1200 ₽</span>
      <time>02.01.2026 09:30</time>
    </article>
    """
    fields = [
        {
            "name": "url",
            "selector": "a",
            "kind": "attribute",
            "attribute": "href",
            "transforms": [{"op": "absolute_url", "base_url": "https://shop.example/catalog"}],
        },
        {
            "name": "price",
            "selector": ".price",
            "kind": "text",
            "transforms": [
                {
                    "op": "parse_currency",
                    "symbol": "₽",
                    "decimal_separator": ".",
                    "thousands_separator": " ",
                }
            ],
        },
        {
            "name": "posted",
            "selector": "time",
            "kind": "text",
            "transforms": [
                {"op": "parse_date", "format": "%d.%m.%Y %H:%M", "timezone": "+03:00"}
            ],
        },
    ]
    profile = parse_profile(profile_with(fields), base_dir=tmp_path)
    result = extract_html(html, profile.extraction, source_url="file:///page.html", fetched_at=FETCHED_AT)
    apply_transforms(result, profile.extraction.fields)

    assert result.errors == []
    assert result.records[0]["url"] == "https://shop.example/kettle"
    assert result.records[0]["price"] == 1200
    assert result.records[0]["posted"] == "2026-01-02T09:30:00+03:00"


def test_invalid_date_is_not_replaced_with_empty(tmp_path: Path):
    html = '<article class="card"><time></time></article><article class="card"><time>31.02.2026</time></article>'
    fields = [
        {
            "name": "posted",
            "selector": "time",
            "kind": "text",
            "transforms": [{"op": "parse_date", "format": "%d.%m.%Y"}],
        }
    ]
    profile = parse_profile(profile_with(fields), base_dir=tmp_path)
    result = extract_html(html, profile.extraction, source_url="file:///page.html", fetched_at=FETCHED_AT)
    apply_transforms(result, profile.extraction.fields)

    assert [row["posted"] for row in result.records] == [""]
    assert result.errors[0].value == "31.02.2026"


def test_duplicates_keep_the_first_record_and_ignore_empty_keys():
    result = extract_html(
        """
        <article class="card"><h2>Первая</h2><a href="/a"></a></article>
        <article class="card"><h2>Вторая</h2><a href="/a"></a></article>
        <article class="card"><h2>Без ссылки</h2></article>
        <article class="card"><h2>Тоже без ссылки</h2></article>
        """,
        ExtractionSpec(
            "article.card",
            (
                FieldSpec("title", "h2", "text"),
                FieldSpec("url", "a", "attribute", attribute="href", required=False),
            ),
        ),
        source_url="file:///page.html",
        fetched_at=FETCHED_AT,
    )
    deduplicate(result, ("url",))

    assert [row["title"] for row in result.records] == ["Первая", "Без ссылки", "Тоже без ссылки"]
    assert result.duplicates_removed == 1


def test_warning_when_a_field_is_absent_on_every_record():
    result = extract_html(
        '<article class="card"><h2>Одна</h2></article><article class="card"><h2>Другая</h2></article>',
        ExtractionSpec(
            "article.card",
            (FieldSpec("title", "h2", "text"), FieldSpec("price", ".price", "text", required=False)),
        ),
        source_url="file:///page.html",
        fetched_at=FETCHED_AT,
    )
    apply_transforms(result, result_fields())

    assert any("price" in warning for warning in result.warnings)


def result_fields() -> tuple[FieldSpec, ...]:
    return (
        FieldSpec("title", "h2", "text"),
        FieldSpec("price", ".price", "text", required=False),
    )


def test_unknown_transform_and_guessed_date_format_are_rejected(tmp_path: Path):
    data = profile_with(
        [{"name": "title", "selector": "h2", "kind": "text", "transforms": [{"op": "python", "code": "1"}]}]
    )
    with pytest.raises(ProfileError, match="неизвестная операция"):
        parse_profile(data, base_dir=tmp_path)

    dated = profile_with(
        [
            {
                "name": "posted",
                "selector": "time",
                "kind": "text",
                "transforms": [{"op": "parse_date", "format": "%d.%m.%Y", "timezone": "Europe/Moscow"}],
            }
        ]
    )
    with pytest.raises(ProfileError, match="смещение"):
        parse_profile(dated, base_dir=tmp_path)


def test_saved_run_writes_numbers_nulls_and_an_error_report(tmp_path: Path):
    page = tmp_path / "page.html"
    page.write_text(
        """
        <article class="card"><h2>Чайник</h2><span class="price">10</span><a href="/kettle"></a></article>
        <article class="card"><h2>Чайник</h2><span class="price">10</span><a href="/kettle"></a></article>
        <article class="card"><h2>Мусор</h2><span class="price">нет</span><a href="/bad"></a></article>
        """,
        encoding="utf-8",
    )
    profile = parse_profile(
        {
            "schema_version": 1,
            "name": "run",
            "source": {"type": "file", "path": "page.html"},
            "extraction": {
                "format": "html",
                "container": "article.card",
                "fields": [
                    {"name": "title", "selector": "h2", "kind": "text"},
                    {
                        "name": "price",
                        "selector": ".price",
                        "kind": "text",
                        "transforms": [{"op": "parse_number", "decimal_separator": "."}],
                    },
                    {"name": "url", "selector": "a", "kind": "attribute", "attribute": "href"},
                ],
            },
            "deduplicate": {"keys": ["url"]},
            "export": {
                "format": "jsonl",
                "path": "out.jsonl",
                "report_path": "errors.jsonl",
            },
        },
        base_dir=tmp_path,
    )

    result = run_file_profile(profile, fetched_at=FETCHED_AT)

    assert result.duplicates_removed == 1
    assert [row["title"] for row in result.records] == ["Чайник"]
    assert result.records[0]["price"] == 10
    assert result.errors[0].value == "нет"
    report = (tmp_path / "errors.jsonl").read_text(encoding="utf-8")
    assert '"kind": "error"' in report
    assert "нет" in report
    assert "Мусор" not in (tmp_path / "out.jsonl").read_text(encoding="utf-8")


def test_transform_dataclass_is_available_for_direct_calls():
    assert Transform("normalize_whitespace").op == "normalize_whitespace"
