import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from parsedock.errors import ExportError, RunError
from parsedock.profile import FileSource, load_profile
from parsedock.run import run_file_profile
from parsedock.export.jsonl import write_jsonl
from parsedock.export.target import write_text_refusing_symlink

EXAMPLE_PROFILE = Path(__file__).resolve().parents[1] / "examples" / "cards" / "profile.json"
FETCHED_AT = datetime(2026, 9, 22, 11, 0, tzinfo=timezone.utc)


def test_create_writes_utf8_jsonl_and_refuses_to_overwrite(tmp_path: Path):
    destination = tmp_path / "nested" / "cards.jsonl"
    records = [{"title": "Чайник", "note": "строка\nвторая"}]

    write_jsonl(destination, records, mode="create")

    raw = destination.read_bytes()
    assert b"\r\n" not in raw
    assert json.loads(raw.decode("utf-8")) == records[0]

    with pytest.raises(ExportError, match="уже существует"):
        write_jsonl(destination, records, mode="create")
    assert destination.read_bytes() == raw


def test_replace_and_append_are_explicit(tmp_path: Path):
    destination = tmp_path / "cards.jsonl"
    write_jsonl(destination, [{"n": 1}], mode="create")
    write_jsonl(destination, [{"n": 2}], mode="replace")
    write_jsonl(destination, [{"n": 3}], mode="append")

    lines = destination.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [{"n": 2}, {"n": 3}]


def test_unknown_export_mode_is_rejected(tmp_path: Path):
    with pytest.raises(ExportError, match="не поддерживается"):
        write_jsonl(tmp_path / "cards.jsonl", [], mode="overwrite")


def test_example_profile_runs_without_network(tmp_path: Path):
    profile = load_profile(EXAMPLE_PROFILE)
    destination = tmp_path / "cards.jsonl"

    result = run_file_profile(profile, fetched_at=FETCHED_AT, output_path=destination)

    assert result.errors == []
    lines = [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()]
    assert [(line["title"], line["price"], line["url"]) for line in lines] == [
        ("Чайник", "1200", "/kettle"),
        ("Кружка", "350", "/mug"),
        ("Ложка", "", "/spoon"),
    ]
    assert {line["source_url"] for line in lines} == {profile.source_url}
    assert {line["fetched_at"] for line in lines} == {"2026-09-22T11:00:00+00:00"}
    assert "999" not in destination.read_text(encoding="utf-8")


def test_missing_page_is_a_run_error_and_creates_no_output(tmp_path: Path):
    profile = load_profile(EXAMPLE_PROFILE)
    missing = profile.__class__(
        schema_version=profile.schema_version,
        name=profile.name,
        source=FileSource(tmp_path / "absent.html"),
        export_path=tmp_path / "out.jsonl",
        export_mode="create",
        extraction=profile.extraction,
    )

    with pytest.raises(RunError, match="не найден"):
        run_file_profile(missing, fetched_at=FETCHED_AT)

    assert not missing.export_path.exists()


def test_replace_does_not_write_through_a_symlink(tmp_path: Path):
    target = tmp_path / "real.txt"
    target.write_text("keep", encoding="utf-8")
    link = tmp_path / "cards.jsonl"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("символические ссылки на этой системе недоступны")
    with pytest.raises(ExportError, match="ссылк"):
        write_jsonl(link, [{"title": "Чайник"}], mode="replace")
    with pytest.raises(ExportError, match="ссылк"):
        write_text_refusing_symlink(link, "замена")
    assert target.read_text(encoding="utf-8") == "keep"
