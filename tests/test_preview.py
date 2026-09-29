import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from parsedock.errors import ExtractionError
from parsedock.jobs import Job
from parsedock.preview import preview_html, preview_profile
from parsedock.profile import load_profile, parse_profile

FETCHED_AT = datetime(2026, 9, 22, 17, 0, tzinfo=timezone.utc)
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "cards"
PAGE = (EXAMPLE / "page.html").read_text(encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        self.server.seen.append(path)
        if path == "/next":
            body = "<article class='card'><h2>Вторая</h2></article>"
        else:
            body = "<article class='card'><h2>Первая</h2></article><a class='next' href='/next'>дальше</a>"
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

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


def test_saved_page_shows_matches_without_writing_or_guessing_selectors():
    profile = load_profile(EXAMPLE / "profile.json")
    export = profile.export_path
    existed = export.exists()
    preview = preview_profile(profile, fetched_at=FETCHED_AT, limit=2)
    assert preview.container_matches == 3
    assert preview.total_records == 3
    assert preview.truncated
    assert [row["title"] for row in preview.records] == ["Чайник", "Кружка"]
    assert all(row["price"] != "999" for row in preview.records)
    assert preview.records[0]["url"] == "/kettle"
    assert export.exists() == existed

    links = preview_html(
        PAGE,
        parse_profile(
            {
                "schema_version": 1,
                "name": "links",
                "source": {"type": "file", "path": "page.html"},
                "extraction": {
                    "format": "html",
                "container": "a",
                "fields": [{"name": "label", "selector": "a", "kind": "text"}],
                },
                "export": {"format": "jsonl", "path": "links.jsonl"},
            },
            base_dir=EXAMPLE,
        ),
        source_url="file://page",
        fetched_at=FETCHED_AT,
    )
    assert links.container_matches == 3
    assert links.total_records == 3
    assert links.records[0]["label"] is None
    texts = preview_html(
        PAGE,
        parse_profile(
            {
                "schema_version": 1,
                "name": "link-text",
                "source": {"type": "file", "path": "page.html"},
                "extraction": {
                    "format": "html",
                    "container": "article.card",
                    "fields": [{"name": "label", "selector": "a", "kind": "text"}],
                },
                "export": {"format": "jsonl", "path": "links.jsonl"},
            },
            base_dir=EXAMPLE,
        ),
        source_url="file://page",
        fetched_at=FETCHED_AT,
        limit=1,
    )
    assert texts.records[0]["label"] == "открыть"
    assert texts.truncated


def test_missing_container_and_bad_selector_stay_distinct(tmp_path):
    profile = parse_profile(
        {
            "schema_version": 1,
            "name": "empty",
            "source": {"type": "file", "path": "page.html"},
            "extraction": {
                "format": "html",
                "container": "article.missing",
                "fields": [{"name": "title", "selector": "h2", "kind": "text", "required": True}],
            },
            "export": {"format": "jsonl", "path": "out.jsonl"},
        },
        base_dir=tmp_path,
    )
    preview = preview_html("<p>пусто</p>", profile, source_url="memory://page", fetched_at=FETCHED_AT)
    assert preview.container_matches == 0
    assert preview.total_records == 0
    assert preview.errors == []
    assert any("не нашёл" in warning for warning in preview.warnings)

    broken = parse_profile(
        {
            "schema_version": 1,
            "name": "broken",
            "source": {"type": "file", "path": "page.html"},
            "extraction": {
                "format": "html",
                "container": "article.card",
                "fields": [{"name": "title", "selector": "h2", "kind": "text", "required": True}],
            },
            "export": {"format": "jsonl", "path": "out.jsonl"},
        },
        base_dir=tmp_path,
    )
    missed = preview_html(
        "<article class='card'><p>без названия</p></article>",
        broken,
        source_url="memory://page",
        fetched_at=FETCHED_AT,
    )
    assert missed.container_matches == 1
    assert missed.total_records == 0
    assert missed.errors[0].field_name == "title"
    with pytest.raises(ExtractionError, match="limit"):
        preview_html("<p>x</p>", broken, source_url="memory://page", fetched_at=FETCHED_AT, limit=0)
    invalid = parse_profile(
        {
            "schema_version": 1,
            "name": "invalid",
            "source": {"type": "file", "path": "page.html"},
            "extraction": {
                "format": "html",
                "container": "a[",
                "fields": [{"name": "title", "selector": "h2", "kind": "text"}],
            },
            "export": {"format": "jsonl", "path": "out.jsonl"},
        },
        base_dir=tmp_path,
    )
    with pytest.raises(ExtractionError, match="селектор"):
        preview_html("<p>x</p>", invalid, source_url="memory://page", fetched_at=FETCHED_AT)


def test_url_preview_reads_one_page_and_skips_the_crawl(tmp_path, serve):
    server = serve()
    host, port = server.server_address[:2]
    profile = parse_profile(
        {
            "schema_version": 1,
            "name": "remote",
            "source": {
                "type": "url",
                "url": f"http://{host}:{port}/page",
                "timeout_seconds": 2,
                "trust_private_networks": True,
            },
            "crawl": {
                "mode": "next_link",
                "next_link": {"selector": "a.next"},
                "obey_robots": True,
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
    preview = preview_profile(profile, fetched_at=FETCHED_AT)
    assert preview.records[0]["title"] == "Первая"
    assert any("не начинает обход" in warning for warning in preview.warnings)
    assert server.seen == ["/page"]
    assert not (tmp_path / "out.jsonl").exists()

    job = Job()
    job.cancel()
    cancelled = preview_profile(profile, fetched_at=FETCHED_AT, job=job)
    assert cancelled.records == []
    assert server.seen == ["/page"]
