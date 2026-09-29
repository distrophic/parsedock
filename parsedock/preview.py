"""Предпросмотр одной страницы без записи файла.

Показывает, сколько блоков нашёл селектор, первые записи и ошибки полей.
Селектор программа не подбирает: его задаёт профиль или вызов.
Обход остальных страниц, robots.txt и экспорт здесь не запускаются.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from parsedock.clients.requests_client import fetch_url
from parsedock.errors import ExtractionError
from parsedock.extract import extract_document
from parsedock.extract.html import FieldError
from parsedock.jobs import FetchControl, Job, sleep_for
from parsedock.profile import FileSource, Profile, UrlSource
from parsedock.run import _read_file
from parsedock.transform import apply_transforms, deduplicate

DEFAULT_PREVIEW_LIMIT = 5


@dataclass
class Preview:
    """Короткий результат одной страницы для показа до полного запуска."""

    source_url: str
    container_matches: int
    records: list[dict]
    total_records: int
    errors: list[FieldError]
    warnings: list[str]
    truncated: bool
    duplicates_removed: int


def preview_html(
    html: str,
    profile: Profile,
    *,
    source_url: str,
    fetched_at: datetime | None = None,
    limit: int = DEFAULT_PREVIEW_LIMIT,
) -> Preview:
    """Разобрать уже загруженный HTML. Сеть и файл результата не используются."""

    shown = _limit(limit)
    moment = fetched_at if fetched_at is not None else datetime.now(timezone.utc)
    extracted = extract_document(
        html,
        profile.extraction,
        source_url=source_url,
        fetched_at=moment,
    )
    return _finish(profile, extracted, shown, source_url)


def preview_profile(
    profile: Profile,
    *,
    fetched_at: datetime | None = None,
    limit: int = DEFAULT_PREVIEW_LIMIT,
    job: Job | None = None,
    clock: Callable[[], float] | None = None,
    sleeper: Callable[[float], None] | None = None,
    rng: Callable[[], float] | None = None,
) -> Preview:
    """Открыть источник профиля и показать первую страницу.

    Для файла сеть не нужна. Для URL выполняется один запрос тем клиентом,
    который указан в профиле. Если в профиле есть обход, остальные страницы
    не читаются.
    """

    shown = _limit(limit)
    moment = fetched_at if fetched_at is not None else datetime.now(timezone.utc)
    if job is not None and job.cancel_requested:
        return Preview(
            source_url=profile.source_url,
            container_matches=0,
            records=[],
            total_records=0,
            errors=[],
            warnings=["Предпросмотр отменён до загрузки страницы."],
            truncated=False,
            duplicates_removed=0,
        )
    html, source_url = _load(profile, job=job, clock=clock, sleeper=sleeper, rng=rng)
    extracted = extract_document(
        html,
        profile.extraction,
        source_url=source_url,
        fetched_at=moment,
    )
    if isinstance(profile.source, UrlSource) and profile.crawl is not None:
        extracted.warnings.append("Предпросмотр читает одну страницу и не начинает обход.")
    return _finish(profile, extracted, shown, source_url)


def _finish(profile: Profile, extracted, limit: int, source_url: str) -> Preview:
    apply_transforms(extracted, profile.extraction.fields)
    deduplicate(extracted, profile.deduplicate_keys)
    if extracted.container_matches == 0:
        if profile.extraction.format == "xml":
            extracted.warnings.append("Путь элементов не нашёл ни одной записи.")
        else:
            extracted.warnings.append("Селектор контейнера не нашёл ни одного элемента.")
    total = len(extracted.records)
    return Preview(
        source_url=source_url,
        container_matches=extracted.container_matches,
        records=extracted.records[:limit],
        total_records=total,
        errors=list(extracted.errors),
        warnings=list(extracted.warnings),
        truncated=total > limit,
        duplicates_removed=extracted.duplicates_removed,
    )


def _load(
    profile: Profile,
    *,
    job: Job | None,
    clock: Callable[[], float] | None,
    sleeper: Callable[[float], None] | None,
    rng: Callable[[], float] | None,
) -> tuple[str, str]:
    source = profile.source
    if isinstance(source, FileSource):
        return _read_file(source.path), source.path.as_uri()
    if isinstance(source, UrlSource):
        active = job if job is not None else Job()
        page = fetch_url(
            source,
            FetchControl(
                policy=profile.reliability,
                job=active,
                clock=clock or time.monotonic,
                sleeper=sleeper if sleeper is not None else sleep_for(active),
                rng=rng or random.random,
            ),
        )
        return page.text, page.url
    raise ExtractionError("У профиля нет поддерживаемого источника")


def _limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ExtractionError("limit предпросмотра: ожидалось целое число от 1")
    return limit
