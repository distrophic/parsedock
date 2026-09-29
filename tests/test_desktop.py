import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from parsedock.desktop.form_data import (
    build_profile,
    crawl_from_form,
    file_source,
    reliability_from_form,
    resume_document,
    transform_from_cells,
    url_source,
)
from parsedock.desktop.window import MainWindow
from parsedock.errors import ProfileError
from parsedock.profile import load_profile

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "cards" / "page.html"


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


def test_file_profile_uses_the_saved_page_and_does_not_set_a_client(tmp_path):
    page = tmp_path / "page.html"
    page.write_text("<article class='card'><h2>Окно</h2></article>", encoding="utf-8")
    profile = build_profile(
        base_dir=page.parent,
        source=file_source(page),
        container="article.card",
        fields=[{"name": "title", "selector": "h2", "kind": "text", "required": True}],
        output_path="out.jsonl",
        replace_output=False,
    )
    assert profile.source.path == page.resolve()
    assert profile.extraction.container == "article.card"
    assert profile.export_mode == "create"
    assert profile.crawl is None


def test_page_param_from_the_form_limits_the_saved_profile(tmp_path):
    profile = build_profile(
        base_dir=tmp_path,
        source=url_source(
            "https://example.invalid/catalog",
            "requests",
            trust_private_networks=False,
            impersonate="",
        ),
        container="article.card",
        fields=[{"name": "title", "selector": "h2", "kind": "text", "required": True}],
        output_path="out.jsonl",
        replace_output=False,
        crawl=crawl_from_form("page_param", "page", "3", obey_robots=True),
    )
    assert profile.crawl is not None
    assert profile.crawl.mode == "page_param"
    assert profile.crawl.page_param.name == "page"
    assert profile.crawl.max_pages == 3
    assert profile.crawl.obey_robots is True


def test_crawl_page_limit_must_be_a_whole_number():
    with pytest.raises(ProfileError, match="Лимит страниц"):
        crawl_from_form("next_link", "a.next", "много", obey_robots=True)


def test_window_keeps_one_page_until_crawl_is_chosen(app):
    window = MainWindow()
    window.url.setText("https://example.invalid/catalog")
    profile = window._profile(file=False)
    assert profile.crawl is None
    window.crawl_mode.setCurrentIndex(window.crawl_mode.findData("page_param"))
    window.crawl_target.setText("page")
    window.crawl_pages.setText("4")
    profile = window._profile(file=False)
    assert profile.crawl is not None
    assert profile.crawl.max_pages == 4
    window.close()


def test_window_loads_page_crawl_from_a_profile(app):
    window = MainWindow()
    loaded = load_profile(EXAMPLE.parent / "profile-pages.json")
    window._apply_profile(loaded)
    assert window.crawl_mode.currentData() == "page_param"
    assert window.crawl_target.text() == "page"
    assert window.crawl_pages.text() == "5"
    built = window._profile(file=False)
    assert built.crawl is not None
    assert built.crawl.mode == "page_param"
    assert built.crawl.max_pages == 5
    window.close()


def test_number_transform_needs_a_decimal_separator():
    assert transform_from_cells("parse_number", ",") == [
        {"op": "parse_number", "decimal_separator": ","}
    ]
    with pytest.raises(ProfileError, match="десятич"):
        transform_from_cells("parse_number", "точка")


def test_window_applies_whitespace_and_deduplicates_by_title(app):
    window = MainWindow()
    window.url.setText("https://example.invalid/catalog")
    window.dedup_keys.setText("title")
    operation = window.fields.cellWidget(1, 5)
    operation.setCurrentIndex(operation.findData("normalize_whitespace"))
    profile = window._profile(file=False)
    assert profile.deduplicate_keys == ("title",)
    assert profile.extraction.fields[1].transforms[0].op == "normalize_whitespace"
    assert profile.extraction.fields[0].transforms == ()
    window.close()


def test_window_keeps_several_transforms_loaded_from_a_profile(app, tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(
        """
        {
          "schema_version": 1,
          "name": "two-transforms",
          "source": {"type": "url", "url": "https://example.invalid/item"},
          "extraction": {
            "format": "html",
            "container": "article.card",
            "fields": [
              {
                "name": "price",
                "selector": ".price",
                "kind": "text",
                "transforms": [
                  {"op": "normalize_whitespace"},
                  {"op": "parse_number", "decimal_separator": ","}
                ]
              }
            ]
          },
          "deduplicate": {"keys": ["price"]},
          "export": {"format": "jsonl", "path": "out.jsonl", "mode": "create"}
        }
        """,
        encoding="utf-8",
    )
    window = MainWindow()
    window._apply_profile(load_profile(path))
    profile = window._profile(file=False)
    operations = [item.op for item in profile.extraction.fields[0].transforms]
    assert operations == ["normalize_whitespace", "parse_number"]
    assert profile.deduplicate_keys == ("price",)
    window.close()


def test_retry_count_stays_between_one_and_five():
    with pytest.raises(ProfileError, match="Повторы"):
        reliability_from_form("6", "0", "0.5")


def test_window_retries_only_when_attempts_are_raised(app):
    window = MainWindow()
    window.url.setText("https://example.invalid/catalog")
    assert window._profile(file=False).reliability.max_attempts == 1
    assert window._profile(file=False).reliability.min_interval_seconds == 0
    window.attempts.setText("3")
    window.min_interval.setText("1")
    profile = window._profile(file=False)
    assert profile.reliability.max_attempts == 3
    assert profile.reliability.min_interval_seconds == 1
    window.close()


def test_window_keeps_retry_limit_from_a_profile(app):
    window = MainWindow()
    window._apply_profile(load_profile(EXAMPLE.parent / "profile-retry.json"))
    assert window.attempts.text() == "3"
    assert window.min_interval.text() == "1"
    profile = window._profile(file=False)
    assert profile.reliability.max_attempts == 3
    assert profile.reliability.min_interval_seconds == 1
    assert profile.reliability.max_backoff_seconds == 8
    window.close()


def test_sqlite_export_and_resume_path_come_from_the_window(app):
    window = MainWindow()
    window.url.setText("https://example.invalid/catalog")
    window.sqlite_path.setText("cards.sqlite")
    sqlite_profile = window._profile(file=False, export_format="sqlite")
    assert sqlite_profile.export_format == "sqlite"
    assert sqlite_profile.export_path.name == "cards.sqlite"
    assert sqlite_profile.resume is None

    window.crawl_mode.setCurrentIndex(window.crawl_mode.findData("page_param"))
    window.crawl_target.setText("page")
    window.resume_path.setText("state.json")
    resumed = window._profile(file=False)
    assert resumed.resume is not None
    assert resumed.resume.path.name == "state.json"
    window.close()


def test_resume_path_without_crawl_is_rejected():
    with pytest.raises(ProfileError, match="Продолжение"):
        resume_document("state.json", crawl_enabled=False)
    assert resume_document("", crawl_enabled=False) is None


def test_curl_tab_builds_a_curl_profile_and_requests_stay_separate(app):
    window = MainWindow()
    window.url.setText("https://example.invalid/requests")
    window.curl_url.setText("https://example.invalid/curl")
    window.curl_impersonate.setCurrentText("chrome")
    window.curl_selector.setText("article.card")
    requests_profile = window._profile(file=False, client="requests")
    curl_profile = window._profile(file=False, client="curl_cffi")
    assert requests_profile.source.client == "requests"
    assert requests_profile.source.impersonate is None
    assert requests_profile.source.url == "https://example.invalid/requests"
    assert curl_profile.source.client == "curl_cffi"
    assert curl_profile.source.impersonate == "chrome"
    assert curl_profile.source.url == "https://example.invalid/curl"
    window.close()


def test_xml_tab_reads_element_paths_and_not_css(app, tmp_path: Path):
    page = tmp_path / "catalog.xml"
    page.write_text(
        "<catalog><price>999</price><item><title>Чайник</title><price></price></item></catalog>",
        encoding="utf-8",
    )
    window = MainWindow()
    window._xml_path = page
    window.xml_container.setText("article.card")
    with pytest.raises(ProfileError, match="CSS-селектор"):
        window._profile(file=True, extraction_format="xml")
    window.xml_container.setText("catalog/item")
    window.xml_fields.setRowCount(0)
    window._add_xml_row({"name": "title", "selector": "title", "kind": "text", "attribute": "", "required": True})
    window._add_xml_row({"name": "price", "selector": "price", "kind": "text", "attribute": "", "required": False})
    profile = window._profile(file=True, extraction_format="xml")
    assert profile.extraction.format == "xml"
    assert profile.extraction.container == "catalog/item"
    assert profile.crawl is None
    window.preview_xml(str(page))
    for _ in range(50):
        if window.xml_results.rowCount() == 1:
            break
        QTest.qWait(100)
    assert window.xml_results.rowCount() == 1
    assert window.xml_results.item(0, 0).text() == "Чайник"
    assert "999" not in window.xml_details.toPlainText()
    window.close()


def test_terminal_tab_runs_only_the_typed_command(app):
    window = MainWindow()
    assert window.tabs.tabText(window._terminal_tab) == "Terminal"
    assert window.terminal_shell.count() >= 1
    assert not hasattr(window, "terminal_cwd")
    window.url.setText("https://example.invalid/should-not-become-a-command")
    window.terminal_command.setText(f'"{sys.executable}" -c "print(12345)"')
    window._send_terminal()
    for _ in range(100):
        if "12345" in window.terminal_output.toPlainText():
            break
        QTest.qWait(100)
    output = window.terminal_output.toPlainText()
    assert "12345" in output
    assert "example.invalid" not in output
    window.close()


def test_save_profile_does_not_write_through_a_symlink(app, tmp_path: Path, monkeypatch):
    target = tmp_path / "real.json"
    target.write_text("keep", encoding="utf-8")
    link = tmp_path / "profile.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("символические ссылки на этой системе недоступны")
    window = MainWindow()
    window.url.setText("https://example.invalid/catalog")
    monkeypatch.setattr(
        "parsedock.desktop.window.QFileDialog.getSaveFileName",
        lambda *args, **kwargs: (str(link), "JSON (*.json)"),
    )
    window._save_profile()
    assert target.read_text(encoding="utf-8") == "keep"
    assert "ссылк" in window.status.text()
    window.close()


def test_new_window_leaves_required_unchecked(app):
    window = MainWindow()
    for row in range(window.fields.rowCount()):
        assert window.fields.cellWidget(row, 4).isChecked() is False
    for row in range(window.xml_fields.rowCount()):
        assert window.xml_fields.cellWidget(row, 4).isChecked() is False
    window.close()


def test_code_tab_explains_an_empty_address(app):
    window = MainWindow()
    window.url.clear()
    window._compose_code()
    assert "Укажите адрес" in window.code_status.text()
    assert window.code_view.toPlainText() == ""
    window.close()


def test_code_tab_previews_a_script_and_does_not_run_it(app, tmp_path: Path):
    window = MainWindow()
    window.url.setText("https://example.invalid/catalog")
    window.headers_edit.setPlainText('{"Authorization": "Bearer secret-token"}')
    window.output_path.setText(str(tmp_path / "out.jsonl"))
    window._compose_code()
    source = window.code_view.toPlainText()
    assert "article.card" in source
    assert "secret-token" not in source
    assert "не проверка безопасности" in window.code_status.text()
    assert window._code_path is None
    assert not list(tmp_path.glob("*.py"))
    window.close()


def test_window_previews_saved_cards_without_writing(app):
    window = MainWindow()
    assert window.tabs.count() == 8
    assert window.tabs.tabText(window._curl_tab) == "curl_cffi"
    assert all(not window.tabs.tabIcon(index).isNull() for index in range(window.tabs.count()))
    window.output_path.setText(str(EXAMPLE.parent / "should-not-appear.jsonl"))
    window.preview_file(str(EXAMPLE))
    for _ in range(50):
        if window.results.rowCount() == 3:
            break
        QTest.qWait(100)
    assert window.results.rowCount() == 3
    assert window.results.item(0, 0).text() == "Чайник"
    assert "999" not in window.details.toPlainText()
    assert not (EXAMPLE.parent / "should-not-appear.jsonl").exists()
    window.close()
