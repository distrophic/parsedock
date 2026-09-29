"""Сохранение результата задания."""

from parsedock.export.csv import write_csv
from parsedock.export.jsonl import write_jsonl
from parsedock.export.sqlite import write_sqlite

__all__ = ["write_csv", "write_jsonl", "write_sqlite"]
