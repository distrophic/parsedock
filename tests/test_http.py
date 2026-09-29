import ipaddress
import json
import socket
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests

from parsedock.errors import HttpStatusError, NetworkError, RunError
from parsedock.profile import parse_profile
from parsedock.run import run_file_profile, run_profile

FETCHED_AT = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
PAGE = Path(__file__).resolve().parents[1].joinpath("examples", "cards", "page.html").read_bytes()
CARD = b"<article class='card'><h2>\xd0\x9e\xd0\xb4\xd0\xb8\xd0\xbd</h2></article>"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        self.server.seen.append(
            {
                "path": self.path,
                "cookie": self.headers.get("Cookie"),
                "authorization": self.headers.get("Authorization"),
                "accept": self.headers.get("Accept"),
                "user_agent": self.headers.get("User-Agent"),
            }
        )
        if path == "/flood":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"x" * 100)
            return
        route = self.server.routes.get(path)
        if route is None:
            self._reply(404, {}, b"missing")
            return
        status, headers, body = route
        self._reply(status, headers, body)

    def _reply(self, status: int, headers: dict, body: bytes) -> None:
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        return


@pytest.fixture
def serve():
    servers: list[ThreadingHTTPServer] = []

    def start(routes: dict) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.routes = routes
        server.seen = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        return server

    yield start

    for server in servers:
        server.shutdown()
        server.server_close()


def url_profile(tmp_path: Path, url: str, **source):
    data = {
        "schema_version": 1,
        "name": "remote-cards",
        "source": {
            "type": "url",
            "url": url,
            "timeout_seconds": 2,
            "trust_private_networks": True,
            **source,
        },
        "extraction": {
            "format": "html",
            "container": "article.card",
            "fields": [
                {"name": "title", "selector": "h2", "kind": "text", "required": True},
                {"name": "price", "selector": ".price", "kind": "text"},
            ],
        },
        "export": {"format": "jsonl", "path": "out.jsonl", "mode": "create"},
    }
    return parse_profile(data, base_dir=tmp_path)


def root(server: ThreadingHTTPServer) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}"


def test_local_server_page_is_saved_and_http_200_does_not_hide_field_errors(tmp_path: Path, serve, monkeypatch):
    calls = []
    original = requests.Session.request

    def wrapped(self, method, url, **kwargs):
        calls.append({"trust_env": self.trust_env, **kwargs})
        return original(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", wrapped)
    server = serve({"/page": (200, {"Content-Type": "text/html; charset=utf-8"}, PAGE)})
    destination = tmp_path / "cards.jsonl"
    profile = url_profile(
        tmp_path,
        f"{root(server)}/page",
        headers={"Accept": "text/html"},
        params={"lang": "ru"},
    )

    result = run_profile(profile, fetched_at=FETCHED_AT, output_path=destination)

    assert result.errors == []
    rows = [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()]
    assert [row["title"] for row in rows] == ["Чайник", "Кружка", "Ложка"]
    assert rows[0]["source_url"] == f"{root(server)}/page?lang=ru"
    assert rows[0]["fetched_at"] == "2026-09-22T12:00:00+00:00"
    assert "999" not in destination.read_text(encoding="utf-8")
    assert server.seen[0]["accept"] == "text/html"
    assert server.seen[0]["user_agent"] == "ParseDock/0.1"
    assert calls[0]["verify"] is True
    assert calls[0]["allow_redirects"] is False
    assert calls[0]["timeout"] == (2, 2)
    assert calls[0]["trust_env"] is False

    broken = serve(
        {
            "/broken": (
                200,
                {"Content-Type": "text/html; charset=utf-8"},
                b"<article class='card'><span class='price'>1</span></article>",
            )
        }
    )
    broken_output = tmp_path / "broken.jsonl"
    broken_result = run_profile(
        url_profile(tmp_path, f"{root(broken)}/broken"),
        fetched_at=FETCHED_AT,
        output_path=broken_output,
    )
    assert broken_result.records == []
    assert broken_result.errors[0].field_name == "title"
    assert broken_output.read_text(encoding="utf-8") == ""


def test_http_status_network_failure_and_size_limit_do_not_write_output(tmp_path: Path, serve):
    missing = serve({})
    missing_output = tmp_path / "missing.jsonl"
    with pytest.raises(HttpStatusError, match="404") as status_error:
        run_profile(
            url_profile(tmp_path, f"{root(missing)}/absent"),
            fetched_at=FETCHED_AT,
            output_path=missing_output,
        )
    assert status_error.value.status_code == 404
    assert not missing_output.exists()

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    closed_port = sock.getsockname()[1]
    sock.close()
    refused_output = tmp_path / "refused.jsonl"
    with pytest.raises(NetworkError):
        run_profile(
            url_profile(
                tmp_path,
                f"http://127.0.0.1:{closed_port}/page",
                timeout_seconds=0.5,
            ),
            fetched_at=FETCHED_AT,
            output_path=refused_output,
        )
    assert not refused_output.exists()

    huge = serve({"/big": (200, {"Content-Type": "text/html; charset=utf-8"}, b"x" * 50)})
    huge_output = tmp_path / "huge.jsonl"
    with pytest.raises(NetworkError, match="больше лимита"):
        run_profile(
            url_profile(tmp_path, f"{root(huge)}/big", max_response_bytes=10),
            fetched_at=FETCHED_AT,
            output_path=huge_output,
        )
    assert not huge_output.exists()

    flood = serve({})
    flood_output = tmp_path / "flood.jsonl"
    with pytest.raises(NetworkError, match="больше лимита"):
        run_profile(
            url_profile(tmp_path, f"{root(flood)}/flood", max_response_bytes=10),
            fetched_at=FETCHED_AT,
            output_path=flood_output,
        )
    assert not flood_output.exists()


def test_secret_headers_stay_on_the_same_origin(tmp_path: Path, serve):
    other = serve({"/landed": (200, {"Content-Type": "text/html; charset=utf-8"}, PAGE)})
    start = serve({"/go": (302, {"Location": f"{root(other)}/landed"}, b"")})
    same = serve(
        {
            "/go": (302, {"Location": "/page"}, b""),
            "/page": (200, {"Content-Type": "text/html; charset=utf-8"}, PAGE),
        }
    )
    headers = {"Authorization": "Bearer secret-token", "Cookie": "session=1", "Accept": "text/html"}
    run_profile(
        url_profile(tmp_path, f"{root(start)}/go", headers=headers),
        fetched_at=FETCHED_AT,
        output_path=tmp_path / "cross.jsonl",
    )
    assert start.seen[0]["authorization"] == "Bearer secret-token"
    assert start.seen[0]["cookie"] == "session=1"
    assert other.seen[0]["authorization"] is None
    assert other.seen[0]["cookie"] is None
    assert other.seen[0]["accept"] == "text/html"

    run_profile(
        url_profile(tmp_path, f"{root(same)}/go", headers=headers),
        fetched_at=FETCHED_AT,
        output_path=tmp_path / "same.jsonl",
    )
    assert same.seen[1]["authorization"] == "Bearer secret-token"
    assert same.seen[1]["cookie"] == "session=1"


def test_redirects_are_checked_one_by_one(tmp_path: Path, serve):
    server = serve(
        {
            "/start": (302, {"Location": "/page"}, b""),
            "/page": (200, {"Content-Type": "text/html; charset=utf-8"}, PAGE),
            "/loop": (302, {"Location": "/loop"}, b""),
            "/meta": (302, {"Location": "http://169.254.169.254/latest/"}, b""),
        }
    )
    base = root(server)
    destination = tmp_path / "cards.jsonl"

    result = run_profile(
        url_profile(tmp_path, f"{base}/start"),
        fetched_at=FETCHED_AT,
        output_path=destination,
    )

    assert result.errors == []
    assert json.loads(destination.read_text(encoding="utf-8").splitlines()[0])["source_url"] == f"{base}/page"

    with pytest.raises(NetworkError, match="Цикл перенаправлений"):
        run_profile(url_profile(tmp_path, f"{base}/loop"), fetched_at=FETCHED_AT, output_path=tmp_path / "loop.jsonl")
    assert not (tmp_path / "loop.jsonl").exists()

    with pytest.raises(NetworkError, match="169.254.169.254"):
        run_profile(url_profile(tmp_path, f"{base}/meta"), fetched_at=FETCHED_AT, output_path=tmp_path / "meta.jsonl")
    assert [item["path"] for item in server.seen if item["path"].startswith("/meta")] == ["/meta"]
    assert not (tmp_path / "meta.jsonl").exists()


def test_localhost_is_blocked_until_the_profile_trusts_private_networks(tmp_path: Path, serve):
    server = serve({"/page": (200, {"Content-Type": "text/html; charset=utf-8"}, PAGE)})
    with pytest.raises(NetworkError, match="закрыт"):
        run_profile(
            url_profile(tmp_path, f"{root(server)}/page", trust_private_networks=False),
            fetched_at=FETCHED_AT,
            output_path=tmp_path / "blocked.jsonl",
        )
    assert server.seen == []
    assert not (tmp_path / "blocked.jsonl").exists()


def test_each_run_uses_a_fresh_session(tmp_path: Path, serve):
    server = serve(
        {
            "/first": (200, {"Content-Type": "text/html; charset=utf-8", "Set-Cookie": "id=1"}, CARD),
            "/second": (200, {"Content-Type": "text/html; charset=utf-8"}, CARD),
        }
    )
    base = root(server)
    run_profile(url_profile(tmp_path, f"{base}/first"), fetched_at=FETCHED_AT, output_path=tmp_path / "first.jsonl")
    run_profile(url_profile(tmp_path, f"{base}/second"), fetched_at=FETCHED_AT, output_path=tmp_path / "second.jsonl")

    assert [item["cookie"] for item in server.seen] == [None, None]


def test_undecodable_body_is_a_network_error(tmp_path: Path, serve):
    server = serve({"/bad": (200, {"Content-Type": "text/html; charset=utf-8"}, b"\xff")})
    with pytest.raises(NetworkError, match="не удалось прочитать"):
        run_profile(
            url_profile(tmp_path, f"{root(server)}/bad"),
            fetched_at=FETCHED_AT,
            output_path=tmp_path / "bad.jsonl",
        )
    assert not (tmp_path / "bad.jsonl").exists()


def test_request_uses_the_checked_address_when_the_name_does_not_resolve(tmp_path: Path, serve, monkeypatch):
    server = serve({"/page": (200, {"Content-Type": "text/html; charset=utf-8"}, CARD)})
    port = server.server_address[1]

    def resolve(host: str, checked_port: int):
        assert host == "shop.test"
        assert checked_port == port
        return (ipaddress.ip_address("127.0.0.1"),)

    monkeypatch.setattr("parsedock.clients.addresses.resolve_ips", resolve)
    result = run_profile(
        url_profile(tmp_path, f"http://shop.test:{port}/page"),
        fetched_at=FETCHED_AT,
        output_path=tmp_path / "pinned.jsonl",
    )

    assert result.records[0]["title"] == "Один"
    assert server.seen[0]["path"].startswith("/page")


def test_run_file_profile_rejects_a_url_profile(tmp_path: Path):
    profile = url_profile(tmp_path, "https://example.invalid/cards", trust_private_networks=False)
    with pytest.raises(RunError, match="только локальный файл"):
        run_file_profile(profile, fetched_at=FETCHED_AT, output_path=tmp_path / "out.jsonl")
