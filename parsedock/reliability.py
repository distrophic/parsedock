"""Пауза перед повтором GET.

backoff — пауза, которая растёт после каждой неудачи.
jitter — случайный разброс этой паузы, чтобы запросы не били в одну секунду.
Retry-After — заголовок ответа: сайт сам называет паузу в секундах
или точное время. Дольше предела из профиля программа не ждёт.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from parsedock.profile import ReliabilitySpec


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Сколько секунд просит заголовок Retry-After. Непонятное значение — None."""

    if value is None:
        return None
    text = value.strip()
    if text == "":
        return None
    try:
        number = float(text)
    except ValueError:
        number = None
    else:
        if number != number or number == float("inf") or number == float("-inf"):
            return None
        return max(0.0, number)

    moment = now if now is not None else datetime.now(timezone.utc)
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - moment).total_seconds())


def retry_delay(
    attempt: int,
    policy: ReliabilitySpec,
    rng: Callable[[], float],
    retry_after: float | None,
) -> float:
    """Пауза после неудачной попытки. attempt начинается с 1.

    rng возвращает число от 0 до 1. При jitter пауза попадает в диапазон
    от половины расчётной до полной. Значение Retry-After не разбрасывается:
    если оно длиннее backoff, ждём его, но не дольше предела профиля.
    """

    grown = policy.backoff_seconds * (policy.backoff_multiplier ** (attempt - 1))
    base = min(grown, policy.max_backoff_seconds)
    if policy.jitter:
        sample = float(rng())
        if sample < 0 or sample > 1:
            sample = min(1.0, max(0.0, sample))
        base *= 0.5 + 0.5 * sample
    if retry_after is None:
        return base
    asked = min(max(0.0, retry_after), policy.max_retry_after_seconds)
    return max(base, asked)
