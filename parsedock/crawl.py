"""Обход нескольких страниц одного задания.

Одинаковый адрес страницы и одинаковые извлечённые записи — разные события.
Повтор адреса останавливает обход и не увеличивает счётчик дублей записей.
Параметры URL не удаляются и не переставляются.

robots.txt описывает пожелания сайта к автоматическим клиентам.
Это не разрешение и не запрет с точки зрения закона.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from parsedock.clients.requests_client import fetch_in_session, user_agent_for
from parsedock.clients.sessions import open_session
from parsedock.errors import Cancelled, ExtractionError, HttpStatusError, NetworkError
from parsedock.jobs import FetchControl
from parsedock.extract import extract_document
from parsedock.extract.html import ExtractResult, FieldError
from parsedock.extract.xml import next_link_from_xml
from parsedock.profile import CrawlSpec, Profile, UrlSource
from parsedock.transform import apply_transforms

try:
    from soupsieve import SelectorSyntaxError
except ImportError:
    SelectorSyntaxError = None  # type: ignore[misc, assignment]

_STOP_TEXT = {
    "max_pages": "Достигнут лимит страниц.",
    "max_depth": "Достигнут лимит глубины.",
    "max_duration": "Достигнут лимит времени обхода.",
    "no_new_records": "Новых записей нет, обход остановлен.",
    "cycle": "Обнаружен повтор адреса страницы, обход остановлен.",
    "domain": "Следующий адрес вне разрешённых доменов.",
    "robots": "robots.txt запрещает следующий адрес.",
    "unsupported": "Следующий адрес не является http или https.",
    "http_status": "Обход остановлен из-за статуса HTTP.",
    "network": "Обход остановлен из-за сетевой ошибки.",
    "cancelled": "Задание отменено, уже полученные записи сохранены.",
}


def with_page_param(url: str, name: str, value: int) -> str:
    """Подставить номер страницы, сохранив остальные параметры и их порядок."""

    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    updated: list[tuple[str, str]] = []
    replaced = False
    for key, current in pairs:
        if key == name and not replaced:
            updated.append((key, str(value)))
            replaced = True
        else:
            updated.append((key, current))
    if not replaced:
        updated.append((name, str(value)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(updated), ""))


def strip_fragment(url: str) -> str:
    """Убрать только фрагмент после #. Query-параметры остаются как есть."""

    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def crawl_pages(
    profile: Profile,
    *,
    fetched_at: datetime,
    clock: Callable[[], float] | None = None,
    control: FetchControl | None = None,
    resume: object | None = None,
    on_page: Callable[[object], None] | None = None,
) -> ExtractResult:
    """Обойти страницы профиля и собрать записи до преобразования дублей."""

    crawl = profile.crawl
    source = profile.source
    if crawl is None or not isinstance(source, UrlSource):
        raise NetworkError("Обход доступен только для URL-профиля с блоком crawl")
    timer = clock or (control.clock if control is not None else time.monotonic)
    active = control or FetchControl(policy=profile.reliability, clock=timer)
    session = open_session(source)
    if active.job is not None:
        active.job.attach_session(session)
    try:
        _raise_if_cancelled(active)
        robots = _load_robots(session, source, crawl, source.url, active)
        return _walk(
            profile,
            source,
            crawl,
            session,
            robots,
            fetched_at,
            timer,
            active,
            resume=resume,
            on_page=on_page,
        )
    finally:
        if active.job is not None:
            active.job.attach_session(None)
        session.close()


def _walk(
    profile: Profile,
    source: UrlSource,
    crawl: CrawlSpec,
    session: object,
    robots: RobotFileParser | None,
    fetched_at: datetime,
    clock: Callable[[], float],
    control: FetchControl,
    resume: object | None = None,
    on_page: Callable[[object], None] | None = None,
) -> ExtractResult:
    result = ExtractResult()
    visited: set[str] = set()
    seen_records: set[tuple[str, ...]] = set()
    started = clock()
    depth = 1
    pending = _initial_urls(source.url, crawl)
    if resume is not None:
        visited = set(resume.visited)
        pending = list(resume.pending)
        seen_records = set(resume.record_markers)
        result.pages_fetched = resume.pages_fetched
        depth = resume.pages_fetched + 1
    agent = user_agent_for(source)

    while pending:
        if _cancelled(control):
            if result.pages_fetched == 0:
                raise Cancelled("Задание отменено")
            return _stop(result, "cancelled")
        if result.pages_fetched >= crawl.max_pages:
            _notify(on_page, visited, pending, seen_records, result.pages_fetched, "max_pages")
            return _stop(result, "max_pages")
        if depth > crawl.max_depth:
            _notify(on_page, visited, pending, seen_records, result.pages_fetched, "max_depth")
            return _stop(result, "max_depth")
        if clock() - started >= crawl.max_duration_seconds:
            return _stop(result, "max_duration")

        current = strip_fragment(pending.pop(0))
        if current in visited:
            _notify(on_page, visited, pending, seen_records, result.pages_fetched, "cycle")
            return _stop(result, "cycle")
        if not _http_url(current):
            _notify(on_page, visited, pending, seen_records, result.pages_fetched, "unsupported")
            return _stop(result, "unsupported")
        if not _domain_allowed(current, crawl):
            _notify(on_page, visited, pending, seen_records, result.pages_fetched, "domain")
            return _stop(result, "domain")
        if robots is not None and not robots.can_fetch(agent, current):
            if result.pages_fetched == 0:
                raise NetworkError(f"robots.txt запрещает адрес {current}")
            _notify(on_page, visited, pending, seen_records, result.pages_fetched, "robots")
            return _stop(result, "robots")

        if control.job is not None:
            control.job.set_stage("fetch")
        try:
            page = fetch_in_session(session, source, current, control)
        except Cancelled:
            if result.pages_fetched == 0:
                raise
            return _stop(result, "cancelled")
        except HttpStatusError as exc:
            if result.pages_fetched == 0:
                raise
            result.errors.append(FieldError(0, "page", str(exc), current))
            return _stop(result, "http_status")
        except NetworkError as exc:
            if result.pages_fetched == 0:
                raise
            result.errors.append(FieldError(0, "page", str(exc), current))
            return _stop(result, "network")

        visited.add(current)
        final_url = strip_fragment(page.url)
        visited.add(final_url)
        result.pages_fetched += 1
        if control.job is not None:
            control.job.set_stage("extract")
        page_result = extract_document(
            page.text,
            profile.extraction,
            source_url=final_url,
            fetched_at=fetched_at,
        )
        apply_transforms(page_result, profile.extraction.fields, report_absence=False)
        _merge(result, page_result, final_url)
        if control.job is not None:
            control.job.observe(result)
        new_records = _count_new(page_result.records, profile, seen_records)
        reason = ""
        if crawl.stop_when_no_new_records and new_records == 0:
            reason = "no_new_records"
        else:
            depth += 1
            if crawl.mode == "next_link" and crawl.next_link is not None:
                if profile.extraction.format == "xml":
                    nxt, warning = next_link_from_xml(
                        page.text, crawl.next_link.selector, crawl.next_link.attribute, final_url
                    )
                else:
                    nxt, warning = _find_next(
                        page.text, crawl.next_link.selector, crawl.next_link.attribute, final_url
                    )
                if warning:
                    result.warnings.append(warning)
                if nxt is None:
                    reason = "finished"
                else:
                    pending.append(nxt)
            elif crawl.mode == "page_param" and crawl.page_param is not None:
                number = crawl.page_param.start + result.pages_fetched * crawl.page_param.step
                pending.append(with_page_param(source.url, crawl.page_param.name, number))
        _notify(
            on_page,
            visited,
            pending,
            seen_records,
            result.pages_fetched,
            reason,
            page_records=page_result.records,
        )
        if reason:
            return _stop(result, reason)
    _notify(on_page, visited, pending, seen_records, result.pages_fetched, "finished")
    return _stop(result, "finished")


def _notify(
    on_page: Callable[[object], None] | None,
    visited: set[str],
    pending: list[str],
    seen_records: set[tuple[str, ...]],
    pages_fetched: int,
    reason: str,
    page_records: list[dict] | None = None,
) -> None:
    if on_page is None:
        return
    from parsedock.resume import ResumeSnapshot, is_complete_stop

    on_page(
        ResumeSnapshot(
            visited=tuple(visited),
            pending=tuple(pending),
            pages_fetched=pages_fetched,
            record_markers=tuple(seen_records),
            page_records=list(page_records or []),
            stop_reason=reason,
            complete=is_complete_stop(reason),
        )
    )


def _initial_urls(start_url: str, crawl: CrawlSpec) -> list[str]:
    if crawl.mode == "urls":
        return list(crawl.urls)
    if crawl.mode == "page_param" and crawl.page_param is not None:
        return [with_page_param(start_url, crawl.page_param.name, crawl.page_param.start)]
    return [start_url]


def _load_robots(
    session: object,
    source: UrlSource,
    crawl: CrawlSpec,
    start_url: str,
    control: FetchControl,
) -> RobotFileParser | None:
    if not crawl.obey_robots:
        return None
    parts = urlsplit(start_url)
    robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
    try:
        page = fetch_in_session(session, source, robots_url, control)
    except (HttpStatusError, NetworkError) as exc:
        return _robots_unavailable(crawl, exc)
    parser = RobotFileParser()
    parser.parse(page.text.splitlines())
    return parser


def _robots_unavailable(crawl: CrawlSpec, exc: Exception) -> None:
    if crawl.robots_on_unavailable == "allow":
        return None
    if crawl.robots_on_unavailable == "deny":
        raise NetworkError("robots.txt недоступен, политика профиля запрещает обход") from exc
    raise NetworkError("robots.txt недоступен, обход остановлен") from exc


def _find_next(html: str, selector: str, attribute: str, base_url: str) -> tuple[str | None, str | None]:
    soup = BeautifulSoup(html, "html.parser")
    try:
        matches = soup.select(selector)
    except Exception as exc:
        if SelectorSyntaxError is not None and isinstance(exc, SelectorSyntaxError):
            raise ExtractionError(f"Ссылка следующей страницы: неверный CSS-селектор {selector!r}") from exc
        raise
    if not matches:
        return None, None
    warning = None
    if len(matches) > 1:
        warning = f"Ссылок следующей страницы найдено {len(matches)}, взята первая."
    value = matches[0].get(attribute)
    if value is None or value == "":
        return None, warning or "У ссылки следующей страницы нет адреса."
    return strip_fragment(urljoin(base_url, str(value))), warning


def _merge(total: ExtractResult, page: ExtractResult, page_url: str) -> None:
    total.records.extend(page.records)
    total.card_numbers.extend(page.card_numbers)
    total.warnings.extend(page.warnings)
    for error in page.errors:
        total.errors.append(
            FieldError(
                card_number=error.card_number,
                field_name=error.field_name,
                message=f"{error.message} Страница: {page_url}",
                value=page_url if error.value is None else error.value,
            )
        )


def _count_new(records: list[dict], profile: Profile, seen: set[tuple[str, ...]]) -> int:
    fresh = 0
    names = tuple(field.name for field in profile.extraction.fields)
    for record in records:
        marker = tuple(_marker_part(record.get(name)) for name in names)
        if marker in seen:
            continue
        seen.add(marker)
        fresh += 1
    return fresh


def _marker_part(value: object) -> str:
    if isinstance(value, list):
        return "\x1f".join(str(item) for item in value)
    return "" if value is None else str(value)


def _domain_allowed(url: str, crawl: CrawlSpec) -> bool:
    host = urlsplit(url).hostname
    if host is None:
        return False
    return host.casefold() in crawl.allowed_domains


def _http_url(url: str) -> bool:
    return urlsplit(url).scheme in {"http", "https"}


def _cancelled(control: FetchControl) -> bool:
    return control.job is not None and control.job.cancel_requested


def _raise_if_cancelled(control: FetchControl) -> None:
    if _cancelled(control):
        raise Cancelled("Задание отменено")


def _stop(result: ExtractResult, reason: str) -> ExtractResult:
    result.stop_reason = reason
    text = _STOP_TEXT.get(reason, "")
    if text:
        result.warnings.append(text)
    return result
