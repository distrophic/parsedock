from datetime import datetime, timezone

import pytest

from parsedock.errors import ExtractionError
from parsedock.extract.html import extract_html
from parsedock.profile import ExtractionSpec, FieldSpec, parse_profile

FETCHED_AT = datetime(2026, 9, 22, 11, 0, tzinfo=timezone.utc)


def fields(*specs: FieldSpec) -> ExtractionSpec:
    return ExtractionSpec(container="article.card", fields=specs)


def title_field(*, required: bool = True, multiple: bool = False) -> FieldSpec:
    return FieldSpec("title", "h2", "text", required=required, multiple=multiple)


def price_field(*, required: bool = False) -> FieldSpec:
    return FieldSpec("price", ".price", "text", required=required)


def link_field(*, required: bool = True, multiple: bool = False) -> FieldSpec:
    return FieldSpec("url", "a", "attribute", attribute="href", required=required, multiple=multiple)


def test_fields_of_one_card_stay_together():
    html = """
    <div class="noise"><h2>Чужое</h2><span class="price">1</span></div>
    <article class="card"><h2>Чайник</h2><span class="price">1200</span><a href="/kettle"></a></article>
    <article class="card"><h2>Кружка</h2><span class="price">350</span><a href="/mug"></a></article>
    """

    result = extract_html(
        html,
        fields(title_field(), price_field(), link_field()),
        source_url="file:///page.html",
        fetched_at=FETCHED_AT,
    )

    assert result.errors == []
    assert [record["title"] for record in result.records] == ["Чайник", "Кружка"]
    assert [record["price"] for record in result.records] == ["1200", "350"]
    assert [record["url"] for record in result.records] == ["/kettle", "/mug"]
    assert result.records[0]["source_url"] == "file:///page.html"
    assert result.records[0]["fetched_at"] == "2026-09-22T11:00:00+00:00"
    assert "Чужое" not in {record["title"] for record in result.records}


def test_missing_value_empty_string_and_required_failure_are_distinct():
    html = """
    <article class="card"><h2>Полная</h2><span class="price">10</span></article>
    <article class="card"><h2>Пустая цена</h2><span class="price"></span></article>
    <article class="card"><h2>Без цены</h2></article>
    <article class="card"><span class="price">7</span></article>
    <article class="card"><h2>Следующая</h2><span class="price">8</span></article>
    """

    result = extract_html(
        html,
        fields(title_field(), price_field(required=False)),
        source_url="file:///page.html",
        fetched_at=FETCHED_AT,
    )

    assert [record["title"] for record in result.records] == ["Полная", "Пустая цена", "Без цены", "Следующая"]
    assert [record["price"] for record in result.records] == ["10", "", None, "8"]
    assert len(result.errors) == 1
    assert result.errors[0].card_number == 4
    assert result.errors[0].field_name == "title"


def test_text_keeps_surrounding_whitespace():
    html = '<article class="card"><h2>  Чайник  </h2></article>'

    result = extract_html(
        html,
        fields(title_field(required=False)),
        source_url="file:///page.html",
        fetched_at=FETCHED_AT,
    )

    assert result.records[0]["title"] == "  Чайник  "


def test_attribute_missing_and_empty_attribute_differ():
    html = """
    <article class="card"><a href="/ok">да</a></article>
    <article class="card"><a href="">пусто</a></article>
    <article class="card"><a>нет адреса</a></article>
    """

    result = extract_html(
        html,
        fields(link_field(required=False)),
        source_url="file:///page.html",
        fetched_at=FETCHED_AT,
    )

    assert [record["url"] for record in result.records] == ["/ok", "", None]
    assert result.errors == []


def test_several_matches_become_a_list_or_an_error():
    html = """
    <article class="card"><h2>Одна</h2><a href="/one"></a><a href="/two"></a></article>
    """

    many = extract_html(
        html,
        fields(title_field(), link_field(multiple=True)),
        source_url="file:///page.html",
        fetched_at=FETCHED_AT,
    )
    assert many.errors == []
    assert many.records[0]["url"] == ["/one", "/two"]

    ambiguous = extract_html(
        html,
        fields(title_field(), link_field(multiple=False)),
        source_url="file:///page.html",
        fetched_at=FETCHED_AT,
    )
    assert ambiguous.records == []
    assert ambiguous.errors[0].field_name == "url"
    assert "найдено 2" in ambiguous.errors[0].message


def test_optional_multiple_field_without_matches_is_an_empty_list():
    html = '<article class="card"><h2>Одна</h2></article>'

    result = extract_html(
        html,
        fields(title_field(), link_field(required=False, multiple=True)),
        source_url="file:///page.html",
        fetched_at=FETCHED_AT,
    )

    assert result.errors == []
    assert result.records[0]["url"] == []


def test_html_kind_returns_inner_markup():
    html = '<article class="card"><h2>Имя <em>товара</em></h2></article>'
    spec = fields(FieldSpec("title", "h2", "html"))

    result = extract_html(html, spec, source_url="file:///page.html", fetched_at=FETCHED_AT)

    assert result.records[0]["title"] == "Имя <em>товара</em>"


def test_no_containers_produce_no_records_and_no_errors():
    result = extract_html(
        "<p>пусто</p>",
        fields(title_field()),
        source_url="file:///page.html",
        fetched_at=FETCHED_AT,
    )

    assert result.records == []
    assert result.errors == []


def test_invalid_selector_and_naive_time_are_errors():
    with pytest.raises(ExtractionError, match="часовой пояс"):
        extract_html(
            "<article class='card'></article>",
            fields(title_field(required=False)),
            source_url="file:///page.html",
            fetched_at=datetime(2026, 9, 22, 11, 0),
        )

    profile = parse_profile(
        {
            "schema_version": 1,
            "name": "bad-selector",
            "source": {"type": "file", "path": "page.html"},
            "extraction": {
                "format": "html",
                "container": "article.card",
                "fields": [{"name": "title", "selector": "h2[", "kind": "text"}],
            },
            "export": {"format": "jsonl", "path": "out.jsonl"},
        },
        base_dir=".",
    )
    with pytest.raises(ExtractionError, match="неверный CSS-селектор"):
        extract_html(
            "<article class='card'><h2>A</h2></article>",
            profile.extraction,
            source_url="file:///page.html",
            fetched_at=FETCHED_AT,
        )
