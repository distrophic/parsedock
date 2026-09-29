import socket
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from parsedock.errors import HttpStatusError, NetworkError, ProfileError, RunError
from parsedock.jobs import Job
from parsedock.profile import load_profile, parse_profile
from parsedock.reliability import parse_retry_after, retry_delay
from parsedock.run import run_profile

FETCHED_AT = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)
CARD = "<article class='card'><h2>Один</h2></article>"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        self.server.seen.append(path)
        if path == "/hold":
            self.server.started.set()
            self.server.release.wait(3)
        route = self.server.routes.get(path)
        if isinstance(route, list):
            status, headers, body = route.pop(0) if route else (404, {}, "")
        elif route is None:
            status, headers, body = 404, {}, ""
        else:
            status, headers, body = route
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        try:
            self.end_headers()
            if data:
                self.wfile.write(data)
        except OSError:
            return

    def log_message(self, fmt: str, *args) -> None:
        return


@pytest.fixture
def serve():
    servers: list[ThreadingHTTPServer] = []

    def start(routes: dict) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.routes = routes
        server.seen = []
        server.started = threading.Event()
        server.release = threading.Event()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.release.set()
        server.shutdown()
        server.server_close()


def origin(server: ThreadingHTTPServer) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}"


def url_profile(tmp_path: Path, url: str, *, source_updates: dict | None = None, **extra):
    source = {
        "type": "url",
        "url": url,
        "timeout_seconds": 2,
        "trust_private_networks": True,
    }
    if source_updates:
        source.update(source_updates)
    data = {
        "schema_version": 1,
        "name": "job",
        "source": source,
        "extraction": {
            "format": "html",
            "container": "article.card",
            "fields": [{"name": "title", "selector": "h2", "kind": "text", "required": True}],
        },
        "export": {"format": "jsonl", "path": "out.jsonl"},
    }
    data.update(extra)
    return parse_profile(data, base_dir=tmp_path)


def test_retry_after_and_backoff_are_bounded(tmp_path, serve):
    policy = url_profile(
        tmp_path,
        "https://example.invalid/x",
        reliability={
            "max_attempts": 3,
            "backoff_seconds": 0.5,
            "backoff_multiplier": 2,
            "max_backoff_seconds": 1.5,
            "jitter": False,
            "max_retry_after_seconds": 4,
        },
    ).reliability
    assert retry_delay(1, policy, lambda: 0, None) == 0.5
    assert retry_delay(2, policy, lambda: 0, None) == 1.0
    assert retry_delay(3, policy, lambda: 1, None) == 1.5
    assert retry_delay(1, policy, lambda: 0, 10) == 4
    jittered = url_profile(
        tmp_path,
        "https://example.invalid/x",
        reliability={"jitter": True, "backoff_seconds": 0.4, "max_backoff_seconds": 2},
    ).reliability
    assert retry_delay(1, jittered, lambda: 0, None) == 0.2
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    assert parse_retry_after("2.5") == 2.5
    assert parse_retry_after("-3") == 0
    assert parse_retry_after("нет") is None
    assert parse_retry_after("Tue, 22 Sep 2026 12:00:05 GMT", now=now) == pytest.approx(5)

    server = serve({"/page": [(503, {"Retry-After": "2"}, ""), (200, {}, CARD)]})
    slept: list[float] = []
    job = Job()
    profile = url_profile(
        tmp_path,
        f"{origin(server)}/page",
        reliability={
            "max_attempts": 2,
            "backoff_seconds": 0.1,
            "jitter": False,
            "max_retry_after_seconds": 30,
        },
    )
    result = run_profile(
        profile,
        fetched_at=FETCHED_AT,
        output_path=tmp_path / "ok.jsonl",
        job=job,
        sleeper=slept.append,
    )
    assert slept == [2]
    assert job.retries == 1
    assert job.state == "completed"
    assert result.records[0]["title"] == "Один"
    assert server.seen == ["/page", "/page"]


def test_client_errors_and_closed_addresses_are_not_retried(tmp_path, serve):
    server = serve({"/missing": (404, {}, "")})
    slept: list[float] = []
    job = Job()
    with pytest.raises(HttpStatusError):
        run_profile(
            url_profile(
                tmp_path,
                f"{origin(server)}/missing",
                reliability={"max_attempts": 3, "jitter": False, "backoff_seconds": 0.2},
            ),
            fetched_at=FETCHED_AT,
            output_path=tmp_path / "missing.jsonl",
            job=job,
            sleeper=slept.append,
        )
    assert server.seen == ["/missing"]
    assert slept == []
    assert job.state == "failed"
    assert not (tmp_path / "missing.jsonl").exists()

    blocked = Job()
    with pytest.raises(NetworkError, match="закрыт"):
        run_profile(
            url_profile(
                tmp_path,
                "http://169.254.169.254/latest",
                reliability={"max_attempts": 3},
            ),
            fetched_at=FETCHED_AT,
            job=blocked,
            sleeper=slept.append,
        )
    assert blocked.state == "failed"
    assert slept == []


def test_connection_failure_retries_with_growing_pause(tmp_path):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    slept: list[float] = []
    job = Job()
    with pytest.raises(NetworkError) as caught:
        run_profile(
            url_profile(
                tmp_path,
                f"http://127.0.0.1:{port}/page",
                source_updates={"timeout_seconds": 0.4},
                reliability={"max_attempts": 2, "backoff_seconds": 0.2, "jitter": False},
            ),
            fetched_at=FETCHED_AT,
            job=job,
            sleeper=slept.append,
        )
    assert caught.value.retryable
    assert slept == [0.2]
    assert job.retries == 1
    assert job.state == "failed"


def test_min_interval_and_cancel_keep_the_first_page(tmp_path, serve):
    server = serve(
        {
            "/p1": (200, {}, "<article class='card'><h2>Первая</h2></article><a class='next' href='/p2'>дальше</a>"),
            "/p2": (200, {}, "<article class='card'><h2>Вторая</h2></article>"),
        }
    )
    job = Job()
    clock_now = {"value": 0.0}
    slept: list[float] = []

    def clock() -> float:
        return clock_now["value"]

    def sleeper(seconds: float) -> None:
        slept.append(seconds)
        clock_now["value"] += seconds
        job.cancel()

    result = run_profile(
        url_profile(
            tmp_path,
            f"{origin(server)}/p1",
            crawl={
                "mode": "next_link",
                "next_link": {"selector": "a.next"},
                "obey_robots": False,
            },
            reliability={"min_interval_seconds": 1, "jitter": False},
        ),
        fetched_at=FETCHED_AT,
        output_path=tmp_path / "kept.jsonl",
        job=job,
        clock=clock,
        sleeper=sleeper,
    )
    assert slept == [1]
    assert server.seen == ["/p1"]
    assert [row["title"] for row in result.records] == ["Первая"]
    assert result.stop_reason == "cancelled"
    assert job.state == "cancelled"
    assert job.status()["state_label"] == "отменено"
    assert (tmp_path / "kept.jsonl").read_text(encoding="utf-8").count("Первая") == 1


def test_cancel_before_start_and_during_retry_does_not_write(tmp_path, serve):
    server = serve({"/page": [(503, {"Retry-After": "5"}, ""), (200, {}, CARD)]})
    early = Job()
    early.cancel()
    result = run_profile(
        url_profile(tmp_path, f"{origin(server)}/page"),
        fetched_at=FETCHED_AT,
        output_path=tmp_path / "early.jsonl",
        job=early,
    )
    assert result.stop_reason == "cancelled"
    assert early.state == "cancelled"
    assert server.seen == []
    assert not (tmp_path / "early.jsonl").exists()

    job = Job()

    def sleeper(seconds: float) -> None:
        job.cancel()

    stopped = run_profile(
        url_profile(
            tmp_path,
            f"{origin(server)}/page",
            reliability={"max_attempts": 3, "jitter": False, "backoff_seconds": 0.2},
        ),
        fetched_at=FETCHED_AT,
        output_path=tmp_path / "retry.jsonl",
        job=job,
        sleeper=sleeper,
    )
    assert stopped.stop_reason == "cancelled"
    assert job.state == "cancelled"
    assert server.seen == ["/page"]
    assert not (tmp_path / "retry.jsonl").exists()
    with pytest.raises(RunError, match="уже запускалось"):
        run_profile(
            url_profile(tmp_path, f"{origin(server)}/page"),
            fetched_at=FETCHED_AT,
            job=job,
        )


def test_cancel_closes_the_current_request(tmp_path, serve):
    server = serve({"/hold": (200, {}, CARD)})
    job = Job()
    outcome: dict = {}

    def worker():
        outcome["result"] = run_profile(
            url_profile(tmp_path, f"{origin(server)}/hold"),
            fetched_at=FETCHED_AT,
            output_path=tmp_path / "hold.jsonl",
            job=job,
        )

    thread = threading.Thread(target=worker)
    thread.start()
    assert server.started.wait(2)
    job.cancel()
    thread.join(3)
    server.release.set()
    assert not thread.is_alive()
    assert outcome["result"].stop_reason == "cancelled"
    assert job.state == "cancelled"
    assert not (tmp_path / "hold.jsonl").exists()


def test_later_http_error_finishes_with_errors_and_keeps_records(tmp_path, serve):
    server = serve(
        {
            "/p1": (200, {}, "<article class='card'><h2>Первая</h2></article><a class='next' href='/p2'>дальше</a>"),
            "/p2": (404, {}, ""),
        }
    )
    job = Job()
    result = run_profile(
        url_profile(
            tmp_path,
            f"{origin(server)}/p1",
            crawl={"mode": "next_link", "next_link": {"selector": "a.next"}, "obey_robots": False},
        ),
        fetched_at=FETCHED_AT,
        output_path=tmp_path / "partial.jsonl",
        job=job,
    )
    assert [row["title"] for row in result.records] == ["Первая"]
    assert result.stop_reason == "http_status"
    assert job.state == "completed_with_errors"
    assert job.status()["stage_label"] == "завершено"
    assert (tmp_path / "partial.jsonl").exists()


def test_file_profile_job_reports_completion_and_field_errors(tmp_path):
    page = tmp_path / "page.html"
    page.write_text(CARD, encoding="utf-8")
    profile = parse_profile(
        {
            "schema_version": 1,
            "name": "file",
            "source": {"type": "file", "path": "page.html"},
            "extraction": {
                "format": "html",
                "container": "article.card",
                "fields": [{"name": "title", "selector": "h2", "kind": "text", "required": True}],
            },
            "export": {"format": "jsonl", "path": "file.jsonl"},
        },
        base_dir=tmp_path,
    )
    job = Job()
    result = run_profile(profile, fetched_at=FETCHED_AT, job=job)
    assert result.records[0]["title"] == "Один"
    assert job.state == "completed"
    assert job.pages_fetched == 1
    assert Path(job.status()["output_path"]).name == "file.jsonl"

    empty = tmp_path / "empty.html"
    empty.write_text("<article class='card'><p>пусто</p></article>", encoding="utf-8")
    broken = parse_profile(
        {
            "schema_version": 1,
            "name": "broken",
            "source": {"type": "file", "path": "empty.html"},
            "extraction": {
                "format": "html",
                "container": "article.card",
                "fields": [{"name": "title", "selector": "h2", "kind": "text", "required": True}],
            },
            "export": {"format": "jsonl", "path": "broken.jsonl"},
        },
        base_dir=tmp_path,
    )
    failed = Job()
    run_profile(broken, fetched_at=FETCHED_AT, job=failed)
    assert failed.state == "completed_with_errors"
    assert failed.error_count == 1


def test_reliability_profile_rules_and_example():
    example = load_profile(Path(__file__).resolve().parents[1] / "examples" / "cards" / "profile-retry.json")
    assert example.reliability.max_attempts == 3
    assert example.reliability.min_interval_seconds == 1
    bare = parse_profile(
        {
            "schema_version": 1,
            "name": "bare",
            "source": {"type": "file", "path": "page.html"},
            "extraction": {
                "format": "html",
                "container": "article.card",
                "fields": [{"name": "title", "selector": "h2", "kind": "text"}],
            },
            "export": {"format": "jsonl", "path": "out.jsonl"},
        },
        base_dir=Path("."),
    )
    assert bare.reliability.max_attempts == 1
    assert bare.reliability.min_interval_seconds == 0

    def bad(reliability):
        return {
            "schema_version": 1,
            "name": "bad",
            "source": {"type": "file", "path": "page.html"},
            "reliability": reliability,
            "extraction": {
                "format": "html",
                "container": "article.card",
                "fields": [{"name": "title", "selector": "h2", "kind": "text"}],
            },
            "export": {"format": "jsonl", "path": "out.jsonl"},
        }

    with pytest.raises(ProfileError, match="не больше 5"):
        parse_profile(bad({"max_attempts": 6}), base_dir=Path("."))
    with pytest.raises(ProfileError, match="404"):
        parse_profile(bad({"retry_statuses": [404]}), base_dir=Path("."))
    with pytest.raises(ProfileError, match="не больше 60"):
        parse_profile(bad({"min_interval_seconds": 61}), base_dir=Path("."))
    with pytest.raises(ProfileError, match="неизвестный параметр"):
        parse_profile(bad({"forever": True}), base_dir=Path("."))
