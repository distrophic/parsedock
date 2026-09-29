from datetime import datetime, timezone
from pathlib import Path

import pytest

from parsedock.errors import ExtractionError, ProfileError
from parsedock.extract.xml import extract_xml, next_link_from_xml
from parsedock.preview import preview_profile
from parsedock.profile import load_profile, parse_profile
from parsedock.run import run_file_profile

FETCHED_AT = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "cards"


def test_saved_xml_example_does_not_write_beside_the_example(tmp_path: Path):
    profile = load_profile(EXAMPLE / "profile-xml.json")
    destination = tmp_path / "cards.jsonl"
    example_output = EXAMPLE / "cards-xml.jsonl"
    before = example_output.read_bytes() if example_output.exists() else None
    result = run_file_profile(profile, fetched_at=FETCHED_AT, output_path=destination)
    assert [row["title"] for row in result.records] == ["Чайник", "Кружка", "Ложка"]
    assert result.records[0]["price"] == "1200"
    assert result.records[2]["price"] == ""
    assert result.records[0]["url"] == "/kettle"
    assert len(result.records) == 3
    assert destination.read_text(encoding="utf-8").count("Чайник") == 1
    assert "999" not in destination.read_text(encoding="utf-8")
    after = example_output.read_bytes() if example_output.exists() else None
    assert after == before


def test_missing_element_is_null_and_empty_element_is_blank(tmp_path: Path):
    page = tmp_path / "one.xml"
    page.write_text("<catalog><item><title>Одна</title></item></catalog>", encoding="utf-8")
    profile = parse_profile(
        {
            "schema_version": 1,
            "name": "xml",
            "source": {"type": "file", "path": str(page)},
            "extraction": {
                "format": "xml",
                "container": "catalog/item",
                "fields": [
                    {"name": "title", "selector": "title", "kind": "text", "required": True},
                    {"name": "price", "selector": "price", "kind": "text"},
                ],
            },
            "export": {"format": "jsonl", "path": "out.jsonl", "mode": "create"},
        },
        base_dir=tmp_path,
    )
    shown = preview_profile(profile, fetched_at=FETCHED_AT)
    assert shown.records[0]["price"] is None
    assert shown.container_matches == 1
    assert not (tmp_path / "out.jsonl").exists()


def test_css_selector_is_rejected_for_xml(tmp_path: Path):
    with pytest.raises(ProfileError, match="CSS-селектор"):
        parse_profile(
            {
                "schema_version": 1,
                "name": "xml",
                "source": {"type": "file", "path": "page.xml"},
                "extraction": {
                    "format": "xml",
                    "container": "article.card",
                    "fields": [{"name": "title", "selector": "h2", "kind": "text"}],
                },
                "export": {"format": "jsonl", "path": "out.jsonl", "mode": "create"},
            },
            base_dir=tmp_path,
        )


def test_html_kind_is_rejected_for_xml_and_xml_kind_for_html(tmp_path: Path):
    xml_profile = {
        "schema_version": 1,
        "name": "xml",
        "source": {"type": "file", "path": "page.xml"},
        "extraction": {
            "format": "xml",
            "container": "catalog/item",
            "fields": [{"name": "body", "selector": "title", "kind": "html"}],
        },
        "export": {"format": "jsonl", "path": "out.jsonl", "mode": "create"},
    }
    with pytest.raises(ProfileError, match="для XML"):
        parse_profile(xml_profile, base_dir=tmp_path)
    html_profile = {
        "schema_version": 1,
        "name": "html",
        "source": {"type": "file", "path": "page.html"},
        "extraction": {
            "format": "html",
            "container": "article",
            "fields": [{"name": "body", "selector": "h2", "kind": "xml"}],
        },
        "export": {"format": "jsonl", "path": "out.jsonl", "mode": "create"},
    }
    with pytest.raises(ProfileError, match="text, attribute или html"):
        parse_profile(html_profile, base_dir=tmp_path)


def test_doctype_and_entities_are_rejected():
    xml = """<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>
<catalog><item><title>&lol2;</title></item></catalog>
"""
    profile_xml = xml
    with pytest.raises(ExtractionError, match="DTD|сущност"):
        extract_xml(
            profile_xml,
            _spec(),
            source_url="file:///catalog.xml",
            fetched_at=FETCHED_AT,
        )


def test_namespaced_local_name_matches_and_attribute_on_the_record():
    xml = """<catalog xmlns="urn:cards"><item id="1"><title>Чайник</title></item></catalog>"""
    result = extract_xml(xml, _spec(extra=True), source_url="file:///catalog.xml", fetched_at=FETCHED_AT)
    assert result.records[0]["title"] == "Чайник"
    assert result.records[0]["id"] == "1"


def test_next_link_uses_an_element_path():
    xml = "<catalog><item><title>Одна</title></item><next href='https://shop.example/p2'/></catalog>"
    url, warning = next_link_from_xml(xml, "catalog/next", "href", "https://shop.example/p1")
    assert url == "https://shop.example/p2"
    assert warning is None


def _spec(extra: bool = False):
    from parsedock.profile import ExtractionSpec, FieldSpec

    fields = [
        FieldSpec(name="title", selector="title", kind="text", required=True),
        FieldSpec(name="price", selector="price", kind="text"),
    ]
    if extra:
        fields.append(FieldSpec(name="id", selector=".", kind="attribute", attribute="id"))
    return ExtractionSpec(container="catalog/item", fields=tuple(fields), format="xml")
