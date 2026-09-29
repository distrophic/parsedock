"""Состояние одного запуска профиля.

Задание проходит ожидание, выполнение и одно конечное состояние.
Отмена останавливает следующие запросы. Уже полученные записи
сохраняются. Возобновление с места остановки сюда не входит:
для него нужно отдельное хранилище очереди и контрольных точек.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from parsedock.errors import Cancelled, RunError
from parsedock.extract.html import ExtractResult
from parsedock.profile import ReliabilitySpec

STATE_TEXT = {
    "pending": "ожидает запуска",
    "running": "выполняется",
    "cancelling": "отменяется",
    "completed": "завершено",
    "completed_with_errors": "завершено с ошибками",
    "cancelled": "отменено",
    "failed": "не удалось выполнить",
}
STAGE_TEXT = {
    "pending": "ожидает запуска",
    "fetch": "получение страницы",
    "extract": "извлечение",
    "export": "сохранение",
    "done": "завершено",
}
_TERMINAL = frozenset({"completed", "completed_with_errors", "cancelled", "failed"})
_ERROR_STOPS = frozenset({"http_status", "network"})


class Job:
    """Один запуск. Повторно тот же объект не запускается."""

    def __init__(self) -> None:
        self.state = "pending"
        self.stage = "pending"
        self.pages_fetched = 0
        self.record_count = 0
        self.duplicates_removed = 0
        self.error_count = 0
        self.retries = 0
        self.output_path: Path | None = None
        self.stop_reason = ""
        self.message = "Задание ожидает запуска."
        self._cancel = False
        self._started = False
        self._session: object | None = None

    @property
    def cancel_requested(self) -> bool:
        return self._cancel

    def cancel(self) -> None:
        """Попросить остановку. Повторный вызов ничего не меняет."""

        if self.state in _TERMINAL:
            return
        self._cancel = True
        session = self._session
        if self.state == "pending":
            self.state = "cancelled"
            self.stage = "done"
            self.stop_reason = "cancelled"
            self.message = "Задание отменено до запуска."
        else:
            self.state = "cancelling"
            self.message = "Задание отменяется."
        _close_session(session)

    def begin(self, output_path: Path) -> bool:
        """Перевести задание в выполнение. False — его уже отменили."""

        if self._started:
            raise RunError("Задание уже запускалось. Для нового запуска создайте новый объект Job.")
        if self.state == "cancelled" or self._cancel:
            self.state = "cancelled"
            self.stage = "done"
            self.stop_reason = "cancelled"
            if self.message == "Задание ожидает запуска.":
                self.message = "Задание отменено до запуска."
            return False
        self._started = True
        self.state = "running"
        self.stage = "fetch"
        self.output_path = output_path
        self.message = "Задание выполняется."
        return True

    def attach_session(self, session: object | None) -> None:
        """Запомнить сессию, чтобы отмена могла закрыть текущий запрос."""

        self._session = session
        if session is not None and self._cancel:
            _close_session(session)

    def set_stage(self, stage: str) -> None:
        if self.state in _TERMINAL:
            return
        self.stage = stage

    def note_retry(self) -> None:
        self.retries += 1

    def observe(self, result: ExtractResult) -> None:
        if self.state in _TERMINAL:
            return
        self.pages_fetched = result.pages_fetched
        self.record_count = len(result.records)
        self.error_count = len(result.errors)
        self.duplicates_removed = result.duplicates_removed
        self.stop_reason = result.stop_reason

    def finish(self, result: ExtractResult) -> None:
        if self.state in _TERMINAL:
            return
        self.observe(result)
        self.stage = "done"
        if result.stop_reason == "cancelled" or self._cancel:
            self.state = "cancelled"
            self.stop_reason = "cancelled"
            self.message = "Задание отменено. Уже полученные записи сохранены."
            return
        if result.errors or result.stop_reason in _ERROR_STOPS:
            self.state = "completed_with_errors"
            self.message = "Задание завершено, есть ошибки."
            return
        self.state = "completed"
        self.message = "Задание завершено."

    def finish_cancelled(self) -> None:
        if self.state in _TERMINAL:
            return
        self.state = "cancelled"
        self.stage = "done"
        self.stop_reason = "cancelled"
        self.message = "Задание отменено. Новых данных нет."

    def fail(self, exc: BaseException) -> None:
        if self.state in _TERMINAL:
            return
        if self._cancel:
            self.finish_cancelled()
            return
        self.state = "failed"
        self.stage = "done"
        self.message = str(exc)

    def status(self) -> dict[str, object]:
        """Снимок для показа: состояние, этап, счётчики и путь результата."""

        return {
            "state": self.state,
            "state_label": STATE_TEXT[self.state],
            "stage": self.stage,
            "stage_label": STAGE_TEXT.get(self.stage, self.stage),
            "pages_fetched": self.pages_fetched,
            "record_count": self.record_count,
            "duplicates_removed": self.duplicates_removed,
            "error_count": self.error_count,
            "retries": self.retries,
            "output_path": None if self.output_path is None else str(self.output_path),
            "stop_reason": self.stop_reason,
            "message": self.message,
        }


def sleep_for(job: Job | None) -> Callable[[float], None]:
    """Пауза короткими шагами, чтобы отмена не ждала всю задержку."""

    def sleeper(seconds: float) -> None:
        if seconds <= 0:
            return
        deadline = time.monotonic() + seconds
        while True:
            if job is not None and job.cancel_requested:
                raise Cancelled("Задание отменено")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.05, remaining))

    return sleeper


def _close_session(session: object | None) -> None:
    close = getattr(session, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:
        return


@dataclass
class FetchControl:
    """Общие для одного задания повторы, пауза, отмена и часы."""

    policy: ReliabilitySpec
    job: Job | None = None
    clock: Callable[[], float] = time.monotonic
    sleeper: Callable[[float], None] | None = None
    rng: Callable[[], float] = random.random
    pace: dict[str, float] = field(default_factory=dict)
