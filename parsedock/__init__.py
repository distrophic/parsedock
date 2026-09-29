"""ParseDock: извлечение записей из HTML-файла или HTTP-страниц и запись JSONL или CSV."""

from parsedock.errors import (
    Cancelled,
    ExportError,
    ExtractionError,
    HttpStatusError,
    NetworkError,
    ParseDockError,
    ProfileError,
    RunError,
)
from parsedock.jobs import Job
from parsedock.export.csv import write_csv
from parsedock.export.jsonl import write_jsonl
from parsedock.export.sqlite import write_sqlite
from parsedock.extract.html import ExtractResult, FieldError, extract_html
from parsedock.preview import Preview, preview_html, preview_profile
from parsedock.profile import FileSource, Profile, UrlSource, load_profile, parse_profile
from parsedock.run import run_file_profile, run_profile

__version__ = "0.1.0"

__all__ = [
    "Cancelled",
    "ExportError",
    "ExtractResult",
    "ExtractionError",
    "FieldError",
    "FileSource",
    "HttpStatusError",
    "Job",
    "NetworkError",
    "ParseDockError",
    "Preview",
    "Profile",
    "ProfileError",
    "RunError",
    "UrlSource",
    "extract_html",
    "load_profile",
    "parse_profile",
    "preview_html",
    "preview_profile",
    "run_file_profile",
    "run_profile",
    "write_csv",
    "write_jsonl",
    "write_sqlite",
]