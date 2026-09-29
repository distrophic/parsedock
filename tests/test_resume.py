import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from parsedock.errors import ProfileError, RunError
from parsedock.profile import parse_profile
from parsedock.run import run_profile


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen.append(self.path)
        route = self.server.routes.get(self.path)
        if route is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status, body = route
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
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


def profile(tmp_path, url: str, *, selector: str = "h2"):
    return parse_profile(
        {
            "schema_version": 1,
            "name": "resume",
            "source": {
                "type": "url",
                "url": url,
                "timeout_seconds": 2,
                "trust_private_networks": True,
            },
            "crawl": {
                "mode": "next_link",
                "next_link": {"selector": "a.next"},
                "max_pages": 5,
                "obey_robots": False,
            },
            "extraction": {
                "format": "html",
                "container": "article.card",
                "fields": [{"name": "title", "selector": selector, "kind": "text", "required": True}],
            },
            "export": {"format": "jsonl", "path": "out.jsonl", "mode": "create"},
            "resume": {"path": "state.json"},
        },
        base_dir=tmp_path,
    )


def titles(path):
    return [json.loads(line)["title"] for line in path.read_text(encoding="utf-8").splitlines()]


def test_resume_keeps_the_first_page_and_fetches_the_failed_one_later(tmp_path, serve):
    server = serve({"/": (200, card("Один", "/two")), "/two": (500, "no")})
    first = run_profile(profile(tmp_path, origin(server) + "/"))
    assert first.pages_fetched == 1
    assert titles(tmp_path / "out.jsonl") == ["Один"]
    assert server.seen.count("/") == 1

    server.routes["/two"] = (200, card("Два"))
    seen_before = len(server.seen)
    second = run_profile(profile(tmp_path, origin(server) + "/"))
    assert second.pages_fetched == 2
    assert titles(tmp_path / "out.jsonl") == ["Один", "Два"]
    assert server.seen[seen_before:] == ["/two"]

    third = run_profile(profile(tmp_path, origin(server) + "/"))
    assert third.records == []
    assert "повторная запись не выполняется" in third.warnings[-1]
    assert titles(tmp_path / "out.jsonl") == ["Один", "Два"]
    assert server.seen[seen_before:] == ["/two"]


def test_changed_profile_does_not_append_to_the_old_result(tmp_path, serve):
    server = serve({"/": (200, card("Один", "/two")), "/two": (500, "no")})
    run_profile(profile(tmp_path, origin(server) + "/"))
    with pytest.raises(RunError, match="другому профилю"):
        run_profile(profile(tmp_path, origin(server) + "/", selector="h1"))
    assert titles(tmp_path / "out.jsonl") == ["Один"]


def test_resume_requires_a_url_crawl(tmp_path):
    with pytest.raises(ProfileError, match="profile.resume"):
        parse_profile(
            {
                "schema_version": 1,
                "name": "file",
                "source": {"type": "file", "path": "page.html"},
                "extraction": {
                    "format": "html",
                    "container": "article",
                    "fields": [{"name": "title", "selector": "h2", "kind": "text"}],
                },
                "export": {"format": "jsonl", "path": "out.jsonl"},
                "resume": {"path": "state.json"},
            },
            base_dir=tmp_path,
        )
