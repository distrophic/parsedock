"""Контрольная точка прерванного обхода.

Файл рядом с заданием запоминает открытые адреса, очередь оставшихся
и отпечаток профиля. Если профиль изменился, продолжение отклоняется:
старые строки и новые правила не смешиваются.

Повторная запись уже сохранённых страниц не выполняется. Это не гарантия
«ровно один раз» при обрыве между записью результата и записью точки.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from parsedock.errors import ExportError, RunError
from parsedock.export.target import refuse_symlink
from parsedock.profile import CrawlSpec, Profile, UrlSource

_COMPLETE_STOPS = frozenset(
    {
        "finished",
        "max_pages",
        "max_depth",
        "no_new_records",
        "cycle",
        "domain",
        "robots",
        "unsupported",
    }
)
_CHECKPOINT_KEYS = frozenset(
    {
        "schema_version",
        "profile_stamp",
        "complete",
        "stop_reason",
        "visited",
        "pending",
        "pages_fetched",
        "record_markers",
        "dedup_markers",
        "output_started",
    }
)


@dataclass
class Checkpoint:
    """Состояние одного обхода, которое можно прочитать при следующем запуске."""

    profile_stamp: str
    complete: bool = False
    stop_reason: str = ""
    visited: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    pages_fetched: int = 0
    record_markers: tuple[tuple[str, ...], ...] = ()
    dedup_markers: tuple[tuple[str, ...], ...] = ()
    output_started: bool = False


@dataclass
class ResumeSnapshot:
    """Снимок после одной страницы или при окончательной остановке."""

    visited: tuple[str, ...]
    pending: tuple[str, ...]
    pages_fetched: int
    record_markers: tuple[tuple[str, ...], ...]
    page_records: list[dict] = field(default_factory=list)
    stop_reason: str = ""
    complete: bool = False


def is_complete_stop(reason: str) -> bool:
    """Остановка, после которой очередь больше не нужна."""

    return reason in _COMPLETE_STOPS


def profile_stamp(profile: Profile) -> str:
    """Отпечаток правил, при которых записан результат.

    Лимиты страниц, глубины и времени сюда не входят: их можно поднять
    и продолжить тот же обход. Смена адреса, селекторов или файла
    результата даёт другой отпечаток.
    """

    source = profile.source
    if not isinstance(source, UrlSource) or profile.crawl is None:
        raise RunError("Продолжение доступно только для URL-профиля с обходом")
    payload = {
        "url": source.url,
        "client": source.client,
        "impersonate": source.impersonate,
        "crawl": _crawl_payload(profile.crawl),
        "extraction_format": profile.extraction.format,
        "container": profile.extraction.container,
        "fields": [
            {
                "name": item.name,
                "selector": item.selector,
                "kind": item.kind,
                "attribute": item.attribute,
                "required": item.required,
                "multiple": item.multiple,
            }
            for item in profile.extraction.fields
        ],
        "deduplicate": list(profile.deduplicate_keys),
        "export_path": str(profile.export_path),
        "export_format": profile.export_format,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_checkpoint(path: Path, stamp: str) -> Checkpoint | None:
    """Прочитать точку. Отсутствие файла означает первый запуск."""

    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunError(f"Контрольная точка повреждена: {path}") from exc
    if not isinstance(data, dict):
        raise RunError(f"Контрольная точка повреждена: {path}")
    unknown = set(data) - _CHECKPOINT_KEYS
    if unknown:
        names = ", ".join(sorted(unknown))
        raise RunError(f"Контрольная точка содержит неизвестные поля: {names}")
    if data.get("schema_version") != 1:
        raise RunError(f"Контрольная точка {path} записана другой версией")
    saved = data.get("profile_stamp")
    if saved != stamp:
        raise RunError(
            "Контрольная точка относится к другому профилю. "
            "Продолжение остановлено, файл результата не изменён."
        )
    return Checkpoint(
        profile_stamp=stamp,
        complete=bool(data.get("complete")),
        stop_reason=str(data.get("stop_reason") or ""),
        visited=tuple(_string_list(data.get("visited"), "visited")),
        pending=tuple(_string_list(data.get("pending"), "pending")),
        pages_fetched=_whole(data.get("pages_fetched", 0)),
        record_markers=tuple(_marker_list(data.get("record_markers"), "record_markers")),
        dedup_markers=tuple(_marker_list(data.get("dedup_markers"), "dedup_markers")),
        output_started=bool(data.get("output_started")),
    )


def save_checkpoint(path: Path, checkpoint: Checkpoint) -> None:
    """Записать точку после успешной записи страницы."""

    document = {
        "schema_version": 1,
        "profile_stamp": checkpoint.profile_stamp,
        "complete": checkpoint.complete,
        "stop_reason": checkpoint.stop_reason,
        "visited": list(checkpoint.visited),
        "pending": list(checkpoint.pending),
        "pages_fetched": checkpoint.pages_fetched,
        "record_markers": [list(item) for item in checkpoint.record_markers],
        "dedup_markers": [list(item) for item in checkpoint.dedup_markers],
        "output_started": checkpoint.output_started,
    }
    try:
        refuse_symlink(path)
    except ExportError as exc:
        raise RunError(str(exc)) from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")


def _crawl_payload(crawl: CrawlSpec) -> dict:
    payload: dict = {
        "mode": crawl.mode,
        "urls": list(crawl.urls),
        "allowed_domains": list(crawl.allowed_domains),
        "stop_when_no_new_records": crawl.stop_when_no_new_records,
        "obey_robots": crawl.obey_robots,
        "robots_on_unavailable": crawl.robots_on_unavailable,
    }
    if crawl.next_link is not None:
        payload["next_link"] = {
            "selector": crawl.next_link.selector,
            "attribute": crawl.next_link.attribute,
        }
    if crawl.page_param is not None:
        payload["page_param"] = {
            "name": crawl.page_param.name,
            "start": crawl.page_param.start,
            "step": crawl.page_param.step,
        }
    return payload


def _string_list(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RunError(f"Контрольная точка: поле {name} должно быть списком строк")
    return list(value)


def _marker_list(value: object, name: str) -> list[tuple[str, ...]]:
    if not isinstance(value, list):
        raise RunError(f"Контрольная точка: поле {name} должно быть списком")
    markers: list[tuple[str, ...]] = []
    for item in value:
        if not isinstance(item, list) or not all(isinstance(part, str) for part in item):
            raise RunError(f"Контрольная точка: поле {name} повреждено")
        markers.append(tuple(item))
    return markers


def _whole(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RunError("Контрольная точка: pages_fetched должно быть целым числом от нуля")
    return value
