"""Запуск профиля: локальный файл, HTTP-запрос или обход страниц, затем JSONL или CSV.

Успешные карточки записываются даже тогда, когда часть карточек попала
в ошибки извлечения или преобразования. Сетевая ошибка и ошибочный статус
HTTP на первой странице файл результата не создают. Код 200 сам по себе
не означает, что поля найдены или что преобразование прошло.

Отмена останавливает следующие запросы и сохраняет уже полученные записи.
Если к моменту отмены записей нет, файл результата не создаётся.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from parsedock.clients.requests_client import fetch_url
from parsedock.crawl import crawl_pages
from parsedock.errors import Cancelled, RunError
from parsedock.export.csv import write_csv
from parsedock.export.jsonl import write_jsonl
from parsedock.export.sqlite import write_sqlite
from parsedock.extract import extract_document
from parsedock.extract.html import ExtractResult
from parsedock.jobs import FetchControl, Job, sleep_for
from parsedock.profile import FileSource, Profile, UrlSource
from parsedock.resume import Checkpoint, load_checkpoint, profile_stamp, save_checkpoint
from parsedock.transform import _duplicate_marker, apply_transforms, deduplicate, note_absent_fields


def run_profile(
    profile: Profile,
    *,
    fetched_at: datetime | None = None,
    output_path: Path | str | None = None,
    clock: Callable[[], float] | None = None,
    job: Job | None = None,
    sleeper: Callable[[float], None] | None = None,
    rng: Callable[[], float] | None = None,
) -> ExtractResult:
    """Прочитать источник профиля, преобразовать записи и сохранить результат.

    output_path заменяет путь экспорта из профиля. Для URL в записи попадает
    адрес последнего ответа, уже после перенаправлений. clock нужен для
    проверки лимита времени обхода и паузы между запросами; без него берётся
    обычный монотонный таймер. job позволяет смотреть состояние и отменить
    запуск. sleeper и rng подменяют паузу и случайный разброс в тестах.
    """

    moment = fetched_at if fetched_at is not None else datetime.now(timezone.utc)
    destination = Path(output_path) if output_path is not None else profile.export_path
    active = job if job is not None else Job()
    if not active.begin(destination):
        result = ExtractResult(stop_reason="cancelled")
        result.warnings.append("Задание отменено до запуска.")
        return result
    control = FetchControl(
        policy=profile.reliability,
        job=active,
        clock=clock or time.monotonic,
        sleeper=sleeper if sleeper is not None else sleep_for(active),
        rng=rng or random.random,
    )
    try:
        if isinstance(profile.source, UrlSource) and profile.crawl is not None and profile.resume is not None:
            if output_path is not None and Path(output_path).resolve() != profile.export_path:
                raise RunError("При продолжении путь результата берётся из профиля.")
            active.set_stage("fetch")
            result, already_written = _crawl_resumed(profile, moment, control, destination)
            note_absent_fields(result, profile.extraction.fields)
        elif isinstance(profile.source, UrlSource) and profile.crawl is not None:
            active.set_stage("fetch")
            result = crawl_pages(profile, fetched_at=moment, clock=control.clock, control=control)
            note_absent_fields(result, profile.extraction.fields)
            already_written = False
        else:
            active.set_stage("fetch")
            html, source_url = _load_html(profile, control)
            active.set_stage("extract")
            result = extract_document(
                html,
                profile.extraction,
                source_url=source_url,
                fetched_at=moment,
            )
            apply_transforms(result, profile.extraction.fields)
            result.pages_fetched = 1
            result.stop_reason = "finished"
            already_written = False
        if not already_written:
            deduplicate(result, profile.deduplicate_keys)
        active.observe(result)
        if result.stop_reason == "cancelled" and result.pages_fetched == 0 and not result.records:
            active.finish_cancelled()
            return result
        active.set_stage("export")
        if not already_written:
            _write_output(profile, result, destination)
        if profile.report_path is not None:
            write_jsonl(profile.report_path, _report_rows(result), mode=profile.report_mode)
        active.finish(result)
        return result
    except Cancelled:
        active.finish_cancelled()
        result = ExtractResult(stop_reason="cancelled")
        result.warnings.append("Задание отменено. Новых данных нет.")
        return result
    except Exception as exc:
        active.fail(exc)
        raise


def _crawl_resumed(profile: Profile, fetched_at: datetime, control: FetchControl, destination: Path):
    """Обойти страницы с контрольной точкой и записать каждую страницу сразу."""

    assert profile.resume is not None
    stamp = profile_stamp(profile)
    prior = load_checkpoint(profile.resume.path, stamp)
    if prior is not None and prior.output_started and not destination.exists():
        raise RunError("Файл результата из контрольной точки не найден. Продолжение остановлено.")
    if prior is not None and prior.complete:
        result = ExtractResult(stop_reason=prior.stop_reason or "finished", pages_fetched=prior.pages_fetched)
        result.warnings.append(
            f"Обход уже завершён ({result.stop_reason}), повторная запись не выполняется."
        )
        return result, True
    writer = _ResumeWriter(profile, destination, prior, stamp)
    result = crawl_pages(
        profile,
        fetched_at=fetched_at,
        clock=control.clock,
        control=control,
        resume=prior,
        on_page=writer.save,
    )
    result.records = writer.written
    result.duplicates_removed = writer.removed
    return result, True


class _ResumeWriter:
    """Пишет новые записи страницы и затем обновляет контрольную точку."""

    def __init__(self, profile: Profile, destination: Path, prior: Checkpoint | None, stamp: str) -> None:
        assert profile.resume is not None
        self._profile = profile
        self._destination = destination
        self._path = profile.resume.path
        self._stamp = stamp
        self._mode = profile.export_mode if prior is None else "append"
        self._dedup = set(prior.dedup_markers if prior is not None else ())
        self._output_started = prior.output_started if prior is not None else False
        self.written: list[dict] = []
        self.removed = 0

    def save(self, snapshot: object) -> None:
        fresh: list[dict] = []
        for record in snapshot.page_records:
            marker = None
            if self._profile.deduplicate_keys:
                marker = _duplicate_marker(record, self._profile.deduplicate_keys)
            if marker is not None and marker in self._dedup:
                self.removed += 1
                continue
            if marker is not None:
                self._dedup.add(marker)
            fresh.append(record)
        if fresh:
            _write_output(self._profile, ExtractResult(records=fresh), self._destination, mode=self._mode)
            self._mode = "append"
            self._output_started = True
            self.written.extend(fresh)
        save_checkpoint(
            self._path,
            Checkpoint(
                profile_stamp=self._stamp,
                complete=snapshot.complete,
                stop_reason=snapshot.stop_reason,
                visited=snapshot.visited,
                pending=snapshot.pending,
                pages_fetched=snapshot.pages_fetched,
                record_markers=snapshot.record_markers,
                dedup_markers=tuple(self._dedup),
                output_started=self._output_started,
            ),
        )


def run_file_profile(
    profile: Profile,
    *,
    fetched_at: datetime | None = None,
    output_path: Path | str | None = None,
    job: Job | None = None,
) -> ExtractResult:
    """Запустить профиль, у которого источник — локальный файл."""

    if not isinstance(profile.source, FileSource):
        raise RunError("run_file_profile читает только локальный файл. Для URL используйте run_profile.")
    return run_profile(profile, fetched_at=fetched_at, output_path=output_path, job=job)


def _load_html(profile: Profile, control: FetchControl) -> tuple[str, str]:
    source = profile.source
    if isinstance(source, FileSource):
        return _read_file(source.path), source.path.as_uri()
    if isinstance(source, UrlSource):
        page = fetch_url(source, control)
        return page.text, page.url
    raise RunError("У профиля нет поддерживаемого источника")

def _write_output(
    profile: Profile,
    result: ExtractResult,
    destination: Path,
    mode: str | None = None,
) -> None:
    export_mode = profile.export_mode if mode is None else mode
    if profile.export_format == "jsonl":
        write_jsonl(destination, result.records, mode=export_mode)
        return
    if profile.export_format == "sqlite":
        write_sqlite(destination, result.records, _columns(profile), mode=export_mode)
        return
    if profile.export_format == "csv":
        write_csv(
            destination,
            result.records,
            _columns(profile),
            mode=export_mode,
            encoding=profile.encoding,
            delimiter=profile.delimiter,
            spreadsheet_safe=profile.spreadsheet_safe,
            bom=profile.bom,
        )
        return
    raise RunError(f"Формат экспорта {profile.export_format!r} не поддерживается")


def _columns(profile: Profile) -> list[str]:
    columns: list[str] = []
    for field in profile.extraction.fields:
        columns.append(field.name)
        if field.keep_original:
            columns.append(f"{field.name}_original")
    columns.extend(["source_url", "fetched_at"])
    return columns


def _report_rows(result: ExtractResult) -> list[dict]:
    rows = [
        {
            "kind": "error",
            "card_number": error.card_number,
            "field_name": error.field_name,
            "message": error.message,
            "value": error.value,
        }
        for error in result.errors
    ]
    rows.extend({"kind": "warning", "message": warning} for warning in result.warnings)
    return rows


def _read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RunError(f"Файл страницы не найден: {path}") from exc
    except UnicodeDecodeError as exc:
        raise RunError(f"Файл страницы не в кодировке UTF-8: {path}") from exc
    except OSError as exc:
        raise RunError(f"Не удалось прочитать файл страницы: {path}") from exc
