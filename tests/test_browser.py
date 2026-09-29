import sys

import pytest

from parsedock.clients.browser_client import open_browser_session
from parsedock.errors import HttpStatusError, NetworkError, ProfileError
from parsedock.profile import load_profile, parse_profile

EXAMPLE = __import__("pathlib").Path(__file__).resolve().parents[1] / "examples" / "cards" / "profile-browser.json"


def test_profile_selects_playwright_without_opening_a_browser():
    profile = load_profile(EXAMPLE)
    assert profile.source.client == "playwright"
    assert profile.source.impersonate is None


def test_impersonate_is_not_a_browser_switch(tmp_path):
    with pytest.raises(ProfileError, match="curl_cffi"):
        parse_profile(
            {
                "schema_version": 1,
                "name": "browser",
                "source": {
                    "type": "url",
                    "url": "https://example.invalid/catalog",
                    "client": "playwright",
                    "impersonate": "chrome",
                },
                "extraction": {
                    "format": "html",
                    "container": "article.card",
                    "fields": [{"name": "title", "selector": "h2", "kind": "text", "required": True}],
                },
                "export": {"format": "jsonl", "path": "out.jsonl"},
            },
            base_dir=tmp_path,
        )


def test_missing_playwright_does_not_fall_back_to_requests(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(NetworkError, match="parsedock\\[browser\\]") as caught:
        open_browser_session()
    assert caught.value.retryable is False


def test_http_error_does_not_open_the_browser(tmp_path, monkeypatch):
    def forbidden():
        raise AssertionError("браузер не должен запускаться после ошибки requests")

    monkeypatch.setattr("parsedock.clients.browser_client.open_browser_session", forbidden)
    from parsedock.run import run_profile

    profile = parse_profile(
        {
            "schema_version": 1,
            "name": "requests-only",
            "source": {
                "type": "url",
                "url": "http://127.0.0.1:9/catalog",
                "timeout_seconds": 1,
                "trust_private_networks": True,
            },
            "extraction": {
                "format": "html",
                "container": "article.card",
                "fields": [{"name": "title", "selector": "h2", "kind": "text", "required": True}],
            },
            "export": {"format": "jsonl", "path": "out.jsonl"},
        },
        base_dir=tmp_path,
    )
    with pytest.raises((NetworkError, HttpStatusError)):
        run_profile(profile)
