import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from parsedock.crawl import strip_fragment, with_page_param
from parsedock.errors import NetworkError, ProfileError
from parsedock.profile import parse_profile
from parsedock.run import run_profile

FETCHED_AT = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen.append(self.path)
        route = self.server.routes.get(self.path)
        if route is None:
            route = self.server.routes.get(urlsplit(self.path).path)
        if route is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status, body = route
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:
        return


@pytest.fixture
def serve():
    servers: list[ThreadingHTTPServer] = []

    def start(routes: dict) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.routes = routes
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


def card(title: str, nxt: str | None = None) -> str:
    link = f'<a class="next" href="{nxt}">дальше</a>' if nxt else ""
    return f"<article class='card'><h2>{title}</h2></article>{link}"


def crawl_profile(tmp_path, url: str, crawl: dict, **source):
    data = {
        "schema_version": 1,
        "name": "pages",
        "source": {
            "type": "url",
            "url": url,
            "timeout_seconds": 2,
            "trust_private_networks": True,
            **source,
        },
        "crawl": crawl,
        "extraction": {
            "format": "html",
            "container": "article.card",
            "fields": [{"name": "title", "selector": "h2", "kind": "text", "required": True}],
        },
        "export": {"format": "jsonl", "path": "out.jsonl"},
    }
    return parse_profile(data, base_dir=tmp_path)


def test_page_param_keeps_other_query_values_and_drops_only_fragment():
    assert with_page_param("https://shop.example/list?sort=name&x=1#top", "page", 2) == (
        "https://shop.example/list?sort=name&x=1&page=2"
    )
    assert with_page_param("https://shop.example/list?page=1&sort=name", "page", 3) == (
        "https://shop.example/list?page=3&sort=name"
    )
    assert strip_fragment("https://shop.example/list?sort=name#top") == "https://shop.example/list?sort=name"


def test_next_link_collects_pages_and_stops_on_cycle(tmp_path, serve):
    server = serve(
        {
            "/p1": (200, card("Первая", "/p2")),
            "/p2": (200, card("Вторая", "/p1")),
        }
    )
    base = origin(server)
    profile = crawl_profile(
        tmp_path,
        f"{base}/p1",
        {"mode": "next_link", "next_link": {"selector": "a.next"}, "obey_robots": False, "max_pages": 5},
    )

    result = run_profile(profile, fetched_at=FETCHED_AT, output_path=tmp_path / "out.jsonl")

    assert [row["title"] for row in result.records] == ["Первая", "Вторая"]
    assert [row["source_url"] for row in result.records] == [f"{base}/p1", f"{base}/p2"]
    assert result.pages_fetched == 2
    assert result.stop_reason == "cycle"
    assert result.duplicates_removed == 0
    assert server.seen == ["/p1", "/p2"]


def test_page_param_and_url_list_stay_on_the_local_server(tmp_path, serve):
    server = serve(
        {
            "/list?sort=name&page=1": (200, card("Один")),
            "/list?sort=name&page=2": (200, card("Два")),
            "/a": (200, card("А")),
            "/b": (200, card("Б")),
        }
    )
    base = origin(server)
    by_number = crawl_profile(
        tmp_path,
        f"{base}/list?sort=name",
        {
            "mode": "page_param",
            "page_param": {"name": "page", "start": 1, "step": 1},
            "max_pages": 2,
            "obey_robots": False,
            "stop_when_no_new_records": False,
        },
    )
    numbered = run_profile(by_number, fetched_at=FETCHED_AT, output_path=tmp_path / "numbered.jsonl")
    assert [row["title"] for row in numbered.records] == ["Один", "Два"]
    assert numbered.stop_reason == "max_pages"
    assert server.seen == ["/list?sort=name&page=1", "/list?sort=name&page=2"]

    server.seen.clear()
    listed = crawl_profile(
        tmp_path,
        f"{base}/a",
        {"mode": "urls", "urls": [f"{base}/a", f"{base}/b"], "obey_robots": False},
    )
    result = run_profile(listed, fetched_at=FETCHED_AT, output_path=tmp_path / "listed.jsonl")
    assert [row["title"] for row in result.records] == ["А", "Б"]
    assert server.seen == ["/a", "/b"]


def test_repeated_records_do_not_count_as_a_repeated_page(tmp_path, serve):
    server = serve(
        {
            "/p1": (200, card("Одинаковая", "/p2")),
            "/p2": (200, card("Одинаковая", "/p3")),
            "/p3": (200, card("Новая")),
        }
    )
    parsed = parse_profile(_body(origin(server), "/p1"), base_dir=tmp_path)
    result = run_profile(parsed, fetched_at=FETCHED_AT, output_path=tmp_path / "dupes.jsonl")

    assert result.pages_fetched == 2
    assert result.stop_reason == "no_new_records"
    assert result.duplicates_removed == 1
    assert [row["title"] for row in result.records] == ["Одинаковая"]
    assert "/p3" not in server.seen


def _body(base: str, path: str) -> dict:
    return {
        "schema_version": 1,
        "name": "dupes",
        "source": {
            "type": "url",
            "url": f"{base}{path}",
            "timeout_seconds": 2,
            "trust_private_networks": True,
        },
        "crawl": {"mode": "next_link", "next_link": {"selector": "a.next"}, "obey_robots": False},
        "extraction": {
            "format": "html",
            "container": "article.card",
            "fields": [{"name": "title", "selector": "h2", "kind": "text", "required": True}],
        },
        "deduplicate": {"keys": ["title"]},
        "export": {"format": "jsonl", "path": "dupes.jsonl"},
    }


def test_limits_domain_and_http_error_keep_earlier_pages(tmp_path, serve):
    server = serve(
        {
            "/p1": (200, card("Первая", "/p2")),
            "/p2": (200, card("Вторая", "http://evil.test/p3")),
            "/only": (200, card("Только", "/next")),
            "/gone": (404, ""),
        }
    )
    base = origin(server)
    outside = crawl_profile(
        tmp_path,
        f"{base}/p1",
        {"mode": "next_link", "next_link": {"selector": "a.next"}, "obey_robots": False, "max_pages": 5},
    )
    stopped = run_profile(outside, fetched_at=FETCHED_AT, output_path=tmp_path / "domain.jsonl")
    assert [row["title"] for row in stopped.records] == ["Первая", "Вторая"]
    assert stopped.stop_reason == "domain"
    assert "evil.test" not in "".join(server.seen)

    server.seen.clear()
    short = crawl_profile(
        tmp_path,
        f"{base}/only",
        {"mode": "next_link", "next_link": {"selector": "a.next"}, "obey_robots": False, "max_pages": 1},
    )
    limited = run_profile(short, fetched_at=FETCHED_AT, output_path=tmp_path / "limited.jsonl")
    assert limited.pages_fetched == 1
    assert limited.stop_reason == "max_pages"
    assert server.seen == ["/only"]

    def clock() -> float:
        clock.calls += 1
        return 0 if clock.calls < 3 else 100

    clock.calls = 0
    server.seen.clear()
    timed = crawl_profile(
        tmp_path,
        f"{base}/p1",
        {
            "mode": "next_link",
            "next_link": {"selector": "a.next"},
            "obey_robots": False,
            "max_duration_seconds": 5,
            "max_pages": 5,
        },
    )
    expired = run_profile(timed, fetched_at=FETCHED_AT, output_path=tmp_path / "timed.jsonl", clock=clock)
    assert expired.pages_fetched == 1
    assert expired.stop_reason == "max_duration"

    server.seen.clear()
    broken = crawl_profile(
        tmp_path,
        f"{base}/p1",
        {"mode": "next_link", "next_link": {"selector": "a.next"}, "obey_robots": False},
    )
    server.routes["/p2"] = (200, card("Вторая", "/gone"))
    failed = run_profile(broken, fetched_at=FETCHED_AT, output_path=tmp_path / "failed.jsonl")
    assert [row["title"] for row in failed.records] == ["Первая", "Вторая"]
    assert failed.stop_reason == "http_status"
    assert failed.errors[-1].field_name == "page"


def test_robots_policy_is_explicit(tmp_path, serve):
    server = serve(
        {
            "/robots.txt": (200, "User-agent: *\nDisallow: /secret\n"),
            "/ok": (200, card("Можно")),
            "/secret": (200, card("Нельзя")),
        }
    )
    base = origin(server)
    profile = crawl_profile(
        tmp_path,
        f"{base}/ok",
        {
            "mode": "urls",
            "urls": [f"{base}/ok", f"{base}/secret"],
            "obey_robots": True,
        },
    )
    result = run_profile(profile, fetched_at=FETCHED_AT, output_path=tmp_path / "robots.jsonl")
    assert [row["title"] for row in result.records] == ["Можно"]
    assert result.stop_reason == "robots"
    assert "/secret" not in server.seen

    closed = serve({"/page": (200, card("Страница"))})
    with pytest.raises(NetworkError, match="robots.txt недоступен"):
        run_profile(
            crawl_profile(
                tmp_path,
                f"{origin(closed)}/page",
                {"mode": "next_link", "next_link": {"selector": "a.next"}, "robots_on_unavailable": "error"},
            ),
            fetched_at=FETCHED_AT,
            output_path=tmp_path / "closed.jsonl",
        )
    assert closed.seen == ["/robots.txt"]
    assert not (tmp_path / "closed.jsonl").exists()


def test_single_page_profile_does_not_read_robots_txt(tmp_path, serve):
    server = serve({"/page": (200, card("Одна"))})
    profile = parse_profile(
        {
            "schema_version": 1,
            "name": "one",
            "source": {
                "type": "url",
                "url": f"{origin(server)}/page",
                "timeout_seconds": 2,
                "trust_private_networks": True,
            },
            "extraction": {
                "format": "html",
                "container": "article.card",
                "fields": [{"name": "title", "selector": "h2", "kind": "text"}],
            },
            "export": {"format": "jsonl", "path": "one.jsonl"},
        },
        base_dir=tmp_path,
    )
    result = run_profile(profile, fetched_at=FETCHED_AT, output_path=tmp_path / "one.jsonl")
    assert result.records[0]["title"] == "Одна"
    assert result.pages_fetched == 1
    assert server.seen == ["/page"]


def test_crawl_rejects_a_file_source_and_loose_query_params(tmp_path):
    with pytest.raises(ProfileError, match="type=url"):
        parse_profile(
            {
                "schema_version": 1,
                "name": "file",
                "source": {"type": "file", "path": "page.html"},
                "crawl": {"mode": "urls", "urls": ["https://example.invalid/a"]},
                "extraction": {
                    "format": "html",
                    "container": "article.card",
                    "fields": [{"name": "title", "selector": "h2", "kind": "text"}],
                },
                "export": {"format": "jsonl", "path": "out.jsonl"},
            },
            base_dir=tmp_path,
        )
