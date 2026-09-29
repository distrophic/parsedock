from pathlib import Path

import pytest

from parsedock.errors import ProfileError
from parsedock.profile import load_profile, parse_profile

EXAMPLE_PROFILE = Path(__file__).resolve().parents[1] / "examples" / "cards" / "profile.json"


def sample_profile() -> dict:
    return {
        "schema_version": 1,
        "name": "sample",
        "source": {"type": "file", "path": "page.html"},
        "extraction": {
            "format": "html",
            "container": "article.card",
            "fields": [
                {"name": "title", "selector": "h2", "kind": "text", "required": True},
            ],
        },
        "export": {"format": "jsonl", "path": "out.jsonl", "mode": "create"},
    }


def test_example_profile_loads_paths_next_to_the_file():
    profile = load_profile(EXAMPLE_PROFILE)

    assert profile.name == "example-cards"
    assert profile.schema_version == 1
    assert profile.source_path == EXAMPLE_PROFILE.parent.joinpath("page.html").resolve()
    assert profile.export_path == EXAMPLE_PROFILE.parent.joinpath("cards.jsonl").resolve()
    assert profile.export_mode == "create"
    assert profile.source_url == profile.source_path.as_uri()
    assert [field.name for field in profile.extraction.fields] == ["title", "price", "url"]
    price = profile.extraction.fields[1]
    assert price.required is False
    assert price.multiple is False


def test_missing_optional_keys_use_documented_defaults(tmp_path: Path):
    data = sample_profile()
    data["extraction"]["fields"][0].pop("required")
    data["export"].pop("mode")

    profile = parse_profile(data, base_dir=tmp_path)

    assert profile.extraction.fields[0].required is False
    assert profile.extraction.fields[0].multiple is False
    assert profile.export_mode == "create"


def test_unknown_parameter_names_the_field(tmp_path: Path):
    data = sample_profile()
    data["extraction"]["fields"][0]["script"] = "print(1)"

    with pytest.raises(ProfileError, match=r"fields\[0\]\.script: неизвестный параметр"):
        parse_profile(data, base_dir=tmp_path)


def test_url_source_uses_documented_defaults(tmp_path: Path):
    data = sample_profile()
    data["source"] = {"type": "url", "url": "https://example.invalid/cards"}

    profile = parse_profile(data, base_dir=tmp_path)

    assert profile.source.url == "https://example.invalid/cards"
    assert profile.source.method == "GET"
    assert profile.source.timeout_seconds == 20
    assert profile.source.max_response_bytes == 2_000_000
    assert profile.source.max_redirects == 5
    assert profile.source.trust_private_networks is False
    with pytest.raises(ProfileError, match="нет локального файла"):
        _ = profile.source_path


def test_url_source_rejects_secrets_unsupported_methods_and_unknown_keys(tmp_path: Path):
    data = sample_profile()
    data["source"] = {"type": "url", "url": "https://user:secret@example.invalid/cards"}
    with pytest.raises(ProfileError, match="логин или пароль"):
        parse_profile(data, base_dir=tmp_path)

    data["source"] = {"type": "url", "url": "ftp://example.invalid/cards"}
    with pytest.raises(ProfileError, match="http или https"):
        parse_profile(data, base_dir=tmp_path)

    data["source"] = {"type": "url", "url": "https://example.invalid/cards", "method": "POST"}
    with pytest.raises(ProfileError, match="только GET"):
        parse_profile(data, base_dir=tmp_path)

    data["source"] = {"type": "url", "url": "https://example.invalid/cards", "verify_tls": False}
    with pytest.raises(ProfileError, match="verify_tls: неизвестный параметр"):
        parse_profile(data, base_dir=tmp_path)


def test_unsupported_schema_format_and_export_are_rejected(tmp_path: Path):
    wrong_version = sample_profile()
    wrong_version["schema_version"] = 2
    with pytest.raises(ProfileError, match="schema_version"):
        parse_profile(wrong_version, base_dir=tmp_path)

    wrong_format = sample_profile()
    wrong_format["extraction"]["format"] = "yaml"
    with pytest.raises(ProfileError, match="html и xml"):
        parse_profile(wrong_format, base_dir=tmp_path)

    wrong_export = sample_profile()
    wrong_export["export"]["format"] = "xlsx"
    with pytest.raises(ProfileError, match="jsonl и csv"):
        parse_profile(wrong_export, base_dir=tmp_path)


def test_attribute_is_required_only_for_attribute_kind(tmp_path: Path):
    missing = sample_profile()
    missing["extraction"]["fields"] = [{"name": "url", "selector": "a", "kind": "attribute"}]
    with pytest.raises(ProfileError, match="attribute: параметр обязателен"):
        parse_profile(missing, base_dir=tmp_path)

    extra = sample_profile()
    extra["extraction"]["fields"][0]["attribute"] = "href"
    with pytest.raises(ProfileError, match="только при kind=attribute"):
        parse_profile(extra, base_dir=tmp_path)


def test_field_name_must_be_ascii(tmp_path: Path):
    data = sample_profile()
    data["extraction"]["fields"][0]["name"] = "название"

    with pytest.raises(ProfileError, match="только латиницу"):
        parse_profile(data, base_dir=tmp_path)


def test_duplicate_and_reserved_field_names_are_rejected(tmp_path: Path):
    duplicated = sample_profile()
    duplicated["extraction"]["fields"].append(
        {"name": "title", "selector": "h2", "kind": "text"}
    )
    with pytest.raises(ProfileError, match="повторяется"):
        parse_profile(duplicated, base_dir=tmp_path)

    reserved = sample_profile()
    reserved["extraction"]["fields"][0]["name"] = "source_url"
    with pytest.raises(ProfileError, match="служебным полем"):
        parse_profile(reserved, base_dir=tmp_path)


def test_invalid_json_and_missing_file_are_profile_errors(tmp_path: Path):
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    with pytest.raises(ProfileError, match="неверный JSON"):
        load_profile(broken)

    with pytest.raises(ProfileError, match="не найден"):
        load_profile(tmp_path / "missing.json")


def test_relative_source_path_stays_inside_the_profile_directory(tmp_path: Path):
    page = tmp_path / "nested" / "page.html"
    page.parent.mkdir()
    data = sample_profile()
    data["source"]["path"] = "nested/page.html"

    profile = parse_profile(data, base_dir=tmp_path)

    assert profile.source_path == page.resolve()
