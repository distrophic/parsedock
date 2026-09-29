import ipaddress
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from parsedock.clients.curl_client import open_curl_session
from parsedock.errors import HttpStatusError, NetworkError, ProfileError
from parsedock.profile import load_profile, parse_profile
from parsedock.run import run_profile
from pathlib import Path

FETCHED_AT = datetime(2026, 9, 22, 16, 0, tzinfo=timezone.utc)
CARD = "<article class='card'><h2>Через curl</h2></article>"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        self.server.seen.append(
            {
                "path": path,
                "cookie": self.headers.get("Cookie"),
                "user_agent": self.headers.get("User-Agent"),
            }
        )
        if path == "/missing":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/start":
            self.send_response(302)
            self.send_header("Location", "/page")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/big":
            body = b"x" * 100
        else:
            body = CARD.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Set-Cookie", "session=abc")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, fmt, *args) -> None:
        return


@pytest.fixture
def serve():
    servers: list[ThreadingHTTPServer] = []

    def start() -> ThreadingHTTPServer:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.seen = []
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def origin(server: ThreadingHTTPServer) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}"


def curl_profile(tmp_path: Path, url: str, **source):
    data = {
        "schema_version": 1,
        "name": "curl",
        "source": {
            "type": "url",
            "url": url,
            "client": "curl_cffi",
            "timeout_seconds": 2,
            "trust_private_networks": True,
            **source,
        },
        "extraction": {
            "format": "html",
            "container": "article.card",
            "fields": [{"name": "title", "selector": "h2", "kind": "text", "required": True}],
        },
        "export": {"format": "jsonl", "path": "out.jsonl"},
    }
    return parse_profile(data, base_dir=tmp_path)


def test_profile_selects_curl_explicitly_and_rejects_a_bad_fingerprint(tmp_path):
    plain = parse_profile(
        {
            "schema_version": 1,
            "name": "plain",
            "source": {"type": "url", "url": "https://example.invalid/a"},
            "extraction": {
                "format": "html",
                "container": "article.card",
                "fields": [{"name": "title", "selector": "h2", "kind": "text"}],
            },
            "export": {"format": "jsonl", "path": "out.jsonl"},
        },
        base_dir=tmp_path,
    )
    assert plain.source.client == "requests"
    assert plain.source.impersonate is None
    with pytest.raises(ProfileError, match="только при client=curl_cffi"):
        parse_profile(
            {
                "schema_version": 1,
                "name": "mixed",
                "source": {
                    "type": "url",
                    "url": "https://example.invalid/a",
                    "impersonate": "chrome",
                },
                "extraction": {
                    "format": "html",
                    "container": "article.card",
                    "fields": [{"name": "title", "selector": "h2", "kind": "text"}],
                },
                "export": {"format": "jsonl", "path": "out.jsonl"},
            },
            base_dir=tmp_path,
        )
    with pytest.raises(ProfileError, match="не поддерживается"):
        curl_profile(tmp_path, "https://example.invalid/a", impersonate="not-a-browser")
    example = load_profile(Path(__file__).resolve().parents[1] / "examples" / "cards" / "profile-curl.json")
    assert example.source.client == "curl_cffi"
    assert example.source.impersonate == "chrome"


def test_curl_fetches_a_page_and_does_not_keep_cookies_for_the_next_job(tmp_path, serve):
    server = serve()
    url = f"{origin(server)}/page"
    first = run_profile(curl_profile(tmp_path, url), fetched_at=FETCHED_AT, output_path=tmp_path / "a.jsonl")
    second = run_profile(curl_profile(tmp_path, url), fetched_at=FETCHED_AT, output_path=tmp_path / "b.jsonl")
    assert first.records[0]["title"] == "Через curl"
    assert second.records[0]["title"] == "Через curl"
    assert [item["user_agent"] for item in server.seen] == ["ParseDock/0.1", "ParseDock/0.1"]
    assert server.seen[1]["cookie"] is None


def test_impersonation_changes_the_header_and_is_not_a_fallback(tmp_path, serve, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("curl_cffi не должен открываться для профиля requests")

    monkeypatch.setattr("parsedock.clients.curl_client.open_curl_session", forbidden)
    server = serve()
    requests_profile = parse_profile(
        {
            "schema_version": 1,
            "name": "requests-only",
            "source": {
                "type": "url",
                "url": f"{origin(server)}/missing",
                "timeout_seconds": 2,
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
    with pytest.raises(HttpStatusError):
        run_profile(requests_profile, fetched_at=FETCHED_AT, output_path=tmp_path / "no.jsonl")

    monkeypatch.undo()
    server.seen.clear()
    result = run_profile(
        curl_profile(tmp_path, f"{origin(server)}/page", impersonate="chrome"),
        fetched_at=FETCHED_AT,
        output_path=tmp_path / "chrome.jsonl",
    )
    assert result.records[0]["title"] == "Через curl"
    agent = server.seen[-1]["user_agent"]
    assert agent.startswith("Mozilla/")
    assert "ParseDock" not in agent


def test_curl_follows_one_redirect_and_stops_on_size(tmp_path, serve):
    server = serve()
    base = origin(server)
    redirected = run_profile(
        curl_profile(tmp_path, f"{base}/start"),
        fetched_at=FETCHED_AT,
        output_path=tmp_path / "go.jsonl",
    )
    assert redirected.records[0]["source_url"] == f"{base}/page"
    assert [item["path"] for item in server.seen] == ["/start", "/page"]
    with pytest.raises(NetworkError, match="лимита"):
        run_profile(
            curl_profile(tmp_path, f"{base}/big", max_response_bytes=10),
            fetched_at=FETCHED_AT,
            output_path=tmp_path / "big.jsonl",
        )


def test_curl_uses_the_checked_address_when_the_name_does_not_resolve(tmp_path, serve, monkeypatch):
    server = serve()
    port = server.server_address[1]

    def resolve(host: str, checked_port: int):
        assert host == "shop.test"
        assert checked_port == port
        return (ipaddress.ip_address("127.0.0.1"),)

    monkeypatch.setattr("parsedock.clients.addresses.resolve_ips", resolve)
    result = run_profile(
        curl_profile(tmp_path, f"http://shop.test:{port}/page"),
        fetched_at=FETCHED_AT,
        output_path=tmp_path / "pinned.jsonl",
    )

    assert result.records[0]["title"] == "Через curl"
    assert server.seen[0]["path"] == "/page"


def test_missing_curl_package_is_reported_and_requests_is_not_used(tmp_path, monkeypatch):
    profile = curl_profile(tmp_path, "http://127.0.0.1:9/page")
    monkeypatch.setitem(sys.modules, "curl_cffi", None)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", None)
    with pytest.raises(NetworkError, match="не подставляется"):
        open_curl_session(profile.source)
