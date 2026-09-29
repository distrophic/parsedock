"""Окно ParseDock в компоновке тёмной панели запроса.

Вкладки повторяют каркас старого окна. Запрос, разбор HTML, XML, JSONL, CSV,
сессия оболочки и сборка скрипта вызывают ядро. Скрипт не запускается сам.
"""

from __future__ import annotations

import json
import queue
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from parsedock.clients.requests_client import fetch_url
from parsedock.codegen import generate_script, run_saved_script, syntax_status
from parsedock.command_run import ShellSession, available_shells
from parsedock.desktop.icons import tab_icon
from parsedock.desktop.style import STYLE
from parsedock.desktop.form_data import (
    PREVIEW_LIMIT,
    argument_text,
    build_profile,
    crawl_from_form,
    dedup_keys_from_form,
    file_source,
    number_text,
    profile_document,
    reliability_extra,
    reliability_from_form,
    resume_document,
    transform_document,
    transform_from_cells,
    url_source,
)
from parsedock.errors import ExportError, ParseDockError, ProfileError, RunError
from parsedock.export.target import write_text_refusing_symlink
from parsedock.jobs import FetchControl, Job
from parsedock.preview import preview_profile
from parsedock.profile import FileSource, UrlSource, load_profile
from parsedock.run import run_profile

_KINDS = ("text", "attribute", "html")
_TRANSFORM_OPS = (
    ("", "нет"),
    ("normalize_whitespace", "пробелы"),
    ("absolute_url", "адрес"),
    ("parse_number", "число"),
    ("parse_currency", "валюта"),
    ("parse_date", "дата"),
)
_DEFAULT_FIELDS = (
    {"name": "title", "selector": "h2", "kind": "text", "attribute": "", "required": False},
    {"name": "price", "selector": ".price", "kind": "text", "attribute": "", "required": False},
    {"name": "url", "selector": "a", "kind": "attribute", "attribute": "href", "required": False},
)


class LibraryWorker(QThread):
    """Вызов ядра вне потока окна."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, action, parent=None) -> None:
        super().__init__(parent)
        self._action = action

    def run(self) -> None:
        try:
            self.succeeded.emit(self._action())
        except ParseDockError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(f"Неожиданная ошибка: {exc}")


class SessionWorker(QThread):
    """Сессия оболочки вне потока окна."""

    chunk = Signal(str)
    ready = Signal(object)
    failed = Signal(str)
    ended = Signal(str)

    def __init__(self, shell, parent=None) -> None:
        super().__init__(parent)
        self._shell = shell
        self.session: ShellSession | None = None

    def stop(self) -> None:
        if self.session is not None:
            self.session.close()

    def run(self) -> None:
        session = ShellSession(self._shell)
        self.session = session
        try:
            session.start(self.chunk.emit)
        except ParseDockError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:
            self.failed.emit(f"Неожиданная ошибка: {exc}")
            return
        self.ready.emit(session)
        if session.process is not None:
            session.process.wait()
        self.ended.emit("Сессия завершена.")


class MainWindow(QMainWindow):
    """Тёмное окно: запрос, разбор HTML и сохранение результата."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ParseDock")
        self.resize(1100, 760)
        self.setStyleSheet(STYLE)
        self._html_path: Path | None = None
        self._xml_path: Path | None = None
        self._crawl_urls: tuple[str, ...] = ()
        self._reliability_extra: dict = {}
        self._active_client = "requests"
        self._job: Job | None = None
        self._worker: LibraryWorker | None = None
        self._mode = ""
        self._export_format = "jsonl"
        self._extraction_format = "html"
        self._timer = QTimer(self)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self._refresh_status)
        self._terminal_timer = QTimer(self)
        self._terminal_timer.setInterval(50)
        self._terminal_timer.timeout.connect(self._drain_terminal)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSize(10)

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(0)

        self.tabs = QTabWidget()
        self.tabs.setIconSize(QSize(16, 16))
        self.tabs.tabBar().setDrawBase(False)
        self.tabs.addTab(self._build_requests(mono), tab_icon("requests"), "Requests")
        self._curl_tab = self.tabs.addTab(self._build_curl(), tab_icon("curl"), "curl_cffi")
        self._html_tab = self.tabs.addTab(self._build_html(), tab_icon("html"), "HTML")
        self.tabs.addTab(self._build_json(mono), tab_icon("json"), "JSON")
        self.tabs.addTab(self._build_csv(), tab_icon("csv"), "CSV")
        self._xml_tab = self.tabs.addTab(self._build_xml(), tab_icon("xml"), "XML")
        self._code_tab = self.tabs.addTab(self._build_code(), tab_icon("code"), "Code")
        self._code_path: Path | None = None
        self._code_saved_text = ""
        self._terminal_tab = self.tabs.addTab(self._build_terminal(), tab_icon("terminal"), "Terminal")
        self._session_worker: SessionWorker | None = None
        self._shell_session: ShellSession | None = None
        self._pending_command = ""
        layout.addWidget(self.tabs)
        self.status = QLabel("")
        layout.addWidget(self.status)

    def preview_file(self, path: str | None = None) -> None:
        """Открыть локальный HTML. path передают тесты, без диалога."""

        if not path:
            chosen, _ = QFileDialog.getOpenFileName(self, "HTML-страница", "", "HTML (*.html *.htm)")
            if not chosen:
                return
            path = chosen
        self._html_path = Path(path)
        self._active_client = "requests"
        self.tabs.setCurrentIndex(self._html_tab)
        self._start(lambda profile: preview_profile(profile, limit=PREVIEW_LIMIT), file=True, mode="preview")

    def preview_xml(self, path: str | None = None) -> None:
        """Открыть локальный XML. path передают тесты, без диалога."""

        if not path:
            chosen, _ = QFileDialog.getOpenFileName(self, "XML", "", "XML (*.xml)")
            if not chosen:
                return
            path = chosen
        self._xml_path = Path(path)
        self._html_path = None
        self.tabs.setCurrentIndex(self._xml_tab)
        self._start(
            lambda profile: preview_profile(profile, limit=PREVIEW_LIMIT),
            file=True,
            mode="preview",
            extraction_format="xml",
        )

    def _build_code(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        note = QLabel(
            "Код собирается из вкладок Requests и HTML, без модели по сети. "
            "Сначала просмотр. Сохранение и запуск спрашиваются отдельно и сами не происходят. "
            "Строка о синтаксисе не означает, что скрипт безопасен. Заголовки запроса в код не копируются."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QHBoxLayout()
        compose = QPushButton("Собрать")
        compose.clicked.connect(self._compose_code)
        save = QPushButton("Сохранить")
        save.clicked.connect(self._save_code)
        run = QPushButton("Запустить")
        run.clicked.connect(self._run_code)
        buttons.addWidget(compose)
        buttons.addWidget(save)
        buttons.addWidget(run)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.code_status = QLabel("Код ещё не собран.")
        self.code_status.setWordWrap(True)
        layout.addWidget(self.code_status)
        self.code_view = QPlainTextEdit()
        self.code_view.setFont(QFont("Consolas"))
        self.code_view.setPlaceholderText("Здесь появится скрипт после кнопки Собрать.")
        layout.addWidget(self.code_view, 1)
        return page

    def _compose_code(self) -> None:
        if not self.url.text().strip() and self._html_path is None:
            self.code_status.setText(
                "Укажите адрес на вкладке Requests или откройте сохранённый HTML на вкладке HTML."
            )
            return
        try:
            use_file = self._html_path is not None and not self.url.text().strip()
            profile = self._profile(file=use_file)
            document = profile_document(
                source=_source_document(profile),
                container=profile.extraction.container,
                fields=[_field_document(field) for field in profile.extraction.fields],
                output_path=self.output_path.text().strip() or "parsedock-result.jsonl",
                replace_output=self.replace_output.isChecked(),
                extraction_format=profile.extraction.format,
                crawl=self._crawl_document(),
                deduplicate_keys=list(profile.deduplicate_keys),
                resume=None if profile.resume is None else {"path": self.resume_path.text().strip()},
                reliability=reliability_from_form(
                    number_text(profile.reliability.max_attempts),
                    number_text(profile.reliability.min_interval_seconds),
                    number_text(profile.reliability.backoff_seconds),
                    extra=reliability_extra(profile.reliability),
                ),
            )
            source = generate_script(document)
        except ParseDockError as exc:
            self.code_status.setText(_plain_profile_error(str(exc)))
            return
        self.code_view.setPlainText(source)
        self.code_status.setText(syntax_status(source))

    def _save_code(self) -> None:
        text = self.code_view.toPlainText()
        if not text.strip():
            self.code_status.setText("Сначала соберите код. Пустой текст не сохраняется.")
            return
        agreed = QMessageBox.question(
            self,
            "Сохранить код",
            "Сохранить этот скрипт на диск?",
        )
        if agreed != QMessageBox.StandardButton.Yes:
            self.code_status.setText("Сохранение отменено.")
            return
        chosen, _ = QFileDialog.getSaveFileName(self, "Скрипт", "parsedock_job.py", "Python (*.py)")
        if not chosen:
            self.code_status.setText("Сохранение отменено.")
            return
        path = Path(chosen)
        if path.exists():
            replace = QMessageBox.question(
                self,
                "Заменить файл",
                f"Файл уже есть:\n{path}\nЗаменить его?",
            )
            if replace != QMessageBox.StandardButton.Yes:
                self.code_status.setText("Файл не заменён.")
                return
        try:
            write_text_refusing_symlink(path, text)
        except ExportError as exc:
            self.code_status.setText(str(exc))
            return
        self._code_path = path
        self._code_saved_text = text
        self.code_status.setText(f"Сохранено: {path}. Запуск ещё не выполнялся.")

    def _run_code(self) -> None:
        text = self.code_view.toPlainText()
        if self._code_path is None or text != self._code_saved_text:
            self.code_status.setText("Сначала сохраните текущий текст. Несохранённый код не запускается.")
            return
        agreed = QMessageBox.question(
            self,
            "Запустить код",
            "Запустить сохранённый файл на этом компьютере?\n"
            "Это отдельное действие, не часть сохранения.",
        )
        if agreed != QMessageBox.StandardButton.Yes:
            self.code_status.setText("Запуск отменён.")
            return
        self.code_status.setText("Скрипт выполняется.")
        worker = LibraryWorker(lambda: run_saved_script(self._code_path), self)
        worker.succeeded.connect(self._show_code_run)
        worker.failed.connect(self.code_status.setText)
        self._worker = worker
        worker.start()

    def _show_code_run(self, completed) -> None:
        output = ((completed.stdout or "") + (completed.stderr or "")).strip()
        text = f"Код выхода: {completed.returncode}."
        if output:
            text += "\n" + output[:2000]
        self.code_status.setText(text)

    def _build_terminal(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        note = QLabel(
            "Команды выполняются на этом компьютере с вашими правами через системную оболочку. "
            "ParseDock их не изолирует. Введите команду сами и нажмите Run: "
            "из адреса, HTML, профиля или сгенерированного кода ничего не запускается само."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        command_row = QHBoxLayout()
        command_row.setSpacing(8)
        self.terminal_shell = QComboBox()
        self.terminal_shell.blockSignals(True)
        for shell in available_shells():
            self.terminal_shell.addItem(shell.label, shell)
        self.terminal_shell.blockSignals(False)
        self.terminal_shell.currentIndexChanged.connect(self._restart_terminal)
        self.terminal_shell.setFixedWidth(120)
        command_row.addWidget(self.terminal_shell)
        prompt = QLabel("$")
        prompt.setFont(QFont("Consolas"))
        command_row.addWidget(prompt)
        self.terminal_command = QLineEdit()
        self.terminal_command.setPlaceholderText("Команда для системной оболочки")
        self.terminal_command.returnPressed.connect(self._send_terminal)
        command_row.addWidget(self.terminal_command, 1)
        send = QPushButton("Run")
        send.clicked.connect(self._send_terminal)
        command_row.addWidget(send)
        self.terminal_stop_button = QPushButton("Stop")
        self.terminal_stop_button.clicked.connect(self._stop_terminal)
        command_row.addWidget(self.terminal_stop_button)
        layout.addLayout(command_row)
        self.terminal_output = QPlainTextEdit()
        self.terminal_output.setReadOnly(True)
        self.terminal_output.setPlaceholderText("Вывод сессии")
        self.terminal_output.setFont(QFont("Consolas"))
        layout.addWidget(self.terminal_output, 1)
        return page

    def _send_terminal(self) -> None:
        text = self.terminal_command.text()
        if not text.strip():
            return
        self.terminal_command.clear()
        if self._shell_session is None or not self._shell_session.running:
            self._pending_command = text
            self._start_shell_session()
            return
        try:
            self._shell_session.send(text)
        except ParseDockError as exc:
            self.terminal_output.appendPlainText(str(exc))

    def _start_shell_session(self) -> None:
        if self._session_worker is not None and self._session_worker.isRunning():
            return
        shell = self.terminal_shell.currentData()
        if shell is None:
            self.terminal_output.setPlainText("На этой системе не найдена поддерживаемая оболочка.")
            return
        worker = SessionWorker(shell)
        worker.ready.connect(self._shell_ready)
        worker.failed.connect(self.terminal_output.appendPlainText)
        worker.ended.connect(self._shell_ended)
        self._session_worker = worker
        self._terminal_timer.start()
        worker.start()

    def _shell_ready(self, session) -> None:
        self._shell_session = session
        pending = self._pending_command
        self._pending_command = ""
        if pending and session.running:
            session.send(pending)

    def _shell_ended(self, message: str) -> None:
        self._drain_terminal()
        self._terminal_timer.stop()
        self._shell_session = None
        self.terminal_output.appendPlainText(message)

    def _drain_terminal(self) -> None:
        worker = self._session_worker
        session = None if worker is None else worker.session
        if session is None:
            return
        while True:
            try:
                text = session.output.get_nowait()
            except queue.Empty:
                break
            cleaned = text.replace("\r", "").strip()
            if cleaned:
                self.terminal_output.appendPlainText(cleaned)

    def _stop_terminal(self) -> None:
        if self._session_worker is not None:
            self._session_worker.stop()

    def _restart_terminal(self) -> None:
        self._pending_command = ""
        self._stop_terminal()
        self._shell_session = None
        self.terminal_output.appendPlainText("Сессия будет запущена заново со следующей команды.")

    def preview_url(self) -> None:
        self._html_path = None
        self._active_client = "requests"
        self.tabs.setCurrentIndex(self._html_tab)
        self._start(
            lambda profile: preview_profile(profile, job=self._job, limit=PREVIEW_LIMIT),
            file=False,
            mode="preview",
        )

    def preview_curl(self) -> None:
        self._html_path = None
        self._active_client = "curl_cffi"
        selector = self.curl_selector.text().strip()
        if selector:
            self.container.setText(selector)
        self.tabs.setCurrentIndex(self._html_tab)
        self._start(
            lambda profile: preview_profile(profile, job=self._job, limit=PREVIEW_LIMIT),
            file=False,
            mode="preview",
            client="curl_cffi",
        )

    def send_curl(self) -> None:
        self._html_path = None
        self._active_client = "curl_cffi"
        self._start(self._fetch_page, file=False, mode="fetch", client="curl_cffi")

    def send_request(self) -> None:
        if self.method.currentText() != "GET":
            self._show_failure("На этом этапе доступен только GET.")
            return
        if self.body.toPlainText().strip():
            self._show_failure("Тело запроса не отправляется. Сейчас доступен только GET без тела.")
            return
        self._html_path = None
        self._active_client = "requests"
        self._start(self._fetch_page, file=False, mode="fetch", client="requests")

    def save_result(self) -> None:
        use_file = self._html_path is not None and not self.url.text().strip()
        self._start(lambda profile: run_profile(profile, job=self._job), file=use_file, mode="save")

    def save_sqlite(self) -> None:
        use_file = self._html_path is not None and not self.url.text().strip()
        self._start(
            lambda profile: run_profile(profile, job=self._job),
            file=use_file,
            mode="save",
            export_format="sqlite",
        )

    def save_csv(self) -> None:
        use_file = self._html_path is not None and not self.url.text().strip()
        self._start(
            lambda profile: run_profile(profile, job=self._job),
            file=use_file,
            mode="save",
            export_format="csv",
        )

    def cancel_work(self) -> None:
        if self._job is not None:
            self._job.cancel()
        self.response.setPlainText("Отмена запрошена. Текущий запрос прерывается.")

    def closeEvent(self, event) -> None:
        self._terminal_timer.stop()
        worker = self._session_worker
        if worker is not None:
            worker.blockSignals(True)
            worker.stop()
            worker.wait(5000)
        if self._job is not None:
            self._job.cancel()
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(3000)
        event.accept()

    def _build_requests(self, mono: QFont) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        request_row = QHBoxLayout()
        request_row.addWidget(QLabel("Method:"))
        self.method = QComboBox()
        self.method.addItems(["GET", "POST", "PUT", "PATCH", "DELETE"])
        self.method.setFixedWidth(96)
        request_row.addWidget(self.method)
        request_row.addWidget(QLabel("URL:"))
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://example.com")
        request_row.addWidget(self.url, 1)
        self.send_button = QPushButton("Send")
        self.send_button.clicked.connect(self._on_send_clicked)
        request_row.addWidget(self.send_button)
        layout.addLayout(request_row)

        buttons = QHBoxLayout()
        self.save_profile_button = QPushButton("Save Profile")
        self.save_profile_button.clicked.connect(self._save_profile)
        self.load_profile_button = QPushButton("Load Profile")
        self.load_profile_button.clicked.connect(self._load_profile)
        buttons.addWidget(self.save_profile_button)
        buttons.addWidget(self.load_profile_button)
        buttons.addStretch(1)
        self.trust_private = QCheckBox("Разрешить localhost и частные сети")
        buttons.addWidget(self.trust_private)
        layout.addLayout(buttons)

        retry_row = QHBoxLayout()
        retry_row.addWidget(QLabel("Повторов:"))
        self.attempts = QLineEdit("1")
        self.attempts.setFixedWidth(48)
        self.attempts.setToolTip(
            "Сколько раз отправить тот же GET, если оборвалось соединение или сервер ответил 429 или 5xx. "
            "1 означает один запрос без повтора. Больше 5 нельзя."
        )
        retry_row.addWidget(self.attempts)
        retry_row.addWidget(QLabel("Пауза между запросами, с:"))
        self.min_interval = QLineEdit("0")
        self.min_interval.setFixedWidth(64)
        self.min_interval.setToolTip("Минимальная пауза между запросами к одному сайту, даже когда ошибок нет.")
        retry_row.addWidget(self.min_interval)
        retry_row.addWidget(QLabel("Пауза перед повтором, с:"))
        self.backoff = QLineEdit("0.5")
        self.backoff.setFixedWidth(64)
        self.backoff.setToolTip("Пауза после неудачи. Следующая пауза растёт. Заголовок Retry-After тоже учитывается.")
        retry_row.addWidget(self.backoff)
        retry_row.addStretch(1)
        layout.addLayout(retry_row)

        layout.addWidget(_section("Headers:"))
        self.headers_edit = QPlainTextEdit()
        self.headers_edit.setFont(mono)
        self.headers_edit.setPlaceholderText('Headers JSON, например:\n{\n  "Accept": "text/html"\n}')
        self.headers_edit.setFixedHeight(110)
        layout.addWidget(self.headers_edit)

        layout.addWidget(_section("Body:"))
        self.body = QPlainTextEdit()
        self.body.setFont(mono)
        self.body.setPlaceholderText("POST Body JSON, например:")
        self.body.setFixedHeight(90)
        layout.addWidget(self.body)

        layout.addWidget(_section("Response:"))
        self.response = QPlainTextEdit()
        self.response.setFont(mono)
        self.response.setReadOnly(True)
        layout.addWidget(self.response, 1)
        return page

    def _build_curl(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        row = QHBoxLayout()
        row.addWidget(QLabel("URL:"))
        self.curl_url = QLineEdit()
        self.curl_url.setPlaceholderText("https://example.com")
        row.addWidget(self.curl_url, 1)
        self.curl_send_button = QPushButton("Открыть")
        self.curl_send_button.clicked.connect(self._on_curl_send_clicked)
        row.addWidget(self.curl_send_button)
        layout.addLayout(row)

        search = QHBoxLayout()
        search.addWidget(QLabel("Селектор:"))
        self.curl_selector = QLineEdit("article.card")
        self.curl_selector.setPlaceholderText("article.card или a")
        search.addWidget(self.curl_selector, 1)
        search.addWidget(QLabel("Отпечаток:"))
        self.curl_impersonate = QComboBox()
        self.curl_impersonate.setEditable(True)
        self.curl_impersonate.addItems(["", "chrome", "firefox", "safari", "edge"])
        self.curl_impersonate.setFixedWidth(140)
        search.addWidget(self.curl_impersonate)
        self.curl_find_button = QPushButton("Найти")
        self.curl_find_button.clicked.connect(self.preview_curl)
        search.addWidget(self.curl_find_button)
        layout.addLayout(search)

        note = QLabel(
            "curl_cffi только скачивает страницу и подстраивает TLS под выбранный отпечаток. "
            "Это не открытый браузер и не обход защиты. Поиск тегов после загрузки тот же, что на вкладке HTML."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        self.curl_response = QPlainTextEdit()
        self.curl_response.setReadOnly(True)
        self.curl_response.setPlaceholderText("Ответ страницы")
        layout.addWidget(self.curl_response, 1)
        return page

    def _build_html(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        selector_row = QHBoxLayout()
        selector_row.addWidget(QLabel("Селектор блока:"))
        self.container = QLineEdit("article.card")
        selector_row.addWidget(self.container, 1)
        layout.addLayout(selector_row)

        self.fields = QTableWidget(0, 8)
        self.fields.setHorizontalHeaderLabels(
            ["Имя", "Селектор", "Вид", "Атрибут", "Обязательное", "Операция", "Параметр", "Исходное"]
        )
        for item in _DEFAULT_FIELDS:
            self._add_field_row(item)
        self.fields.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.fields)

        dedup_row = QHBoxLayout()
        dedup_row.addWidget(QLabel("Дубли по полям:"))
        self.dedup_keys = QLineEdit()
        self.dedup_keys.setPlaceholderText("title, url")
        self.dedup_keys.setToolTip(
            "Записи с одинаковыми значениями этих полей схлопываются, остаётся первая. "
            "Пустое значение и отсутствие значения не объединяются."
        )
        dedup_row.addWidget(self.dedup_keys, 1)
        layout.addLayout(dedup_row)

        actions = QHBoxLayout()
        add_field = QPushButton("Добавить поле")
        add_field.clicked.connect(lambda: self._add_field_row())
        remove_field = QPushButton("Удалить поле")
        remove_field.clicked.connect(self._remove_field_row)
        self.preview_url_button = QPushButton("Разобрать адрес")
        self.preview_url_button.clicked.connect(self.preview_url)
        self.preview_file_button = QPushButton("Открыть сохранённый HTML")
        self.preview_file_button.clicked.connect(self.preview_file)
        actions.addWidget(add_field)
        actions.addWidget(remove_field)
        actions.addWidget(self.preview_url_button)
        actions.addWidget(self.preview_file_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        crawl_row = QHBoxLayout()
        crawl_row.addWidget(QLabel("Обход:"))
        self.crawl_mode = QComboBox()
        self.crawl_mode.addItem("одна страница", "off")
        self.crawl_mode.addItem("параметр страницы", "page_param")
        self.crawl_mode.addItem("ссылка далее", "next_link")
        self.crawl_mode.setMinimumWidth(180)
        self.crawl_mode.currentIndexChanged.connect(self._toggle_crawl)
        crawl_row.addWidget(self.crawl_mode)
        self.crawl_target = QLineEdit()
        crawl_row.addWidget(self.crawl_target, 1)
        self.crawl_pages_label = QLabel("Страниц:")
        crawl_row.addWidget(self.crawl_pages_label)
        self.crawl_pages = QLineEdit("10")
        self.crawl_pages.setFixedWidth(64)
        crawl_row.addWidget(self.crawl_pages)
        self.crawl_robots = QCheckBox("Учитывать robots.txt")
        self.crawl_robots.setChecked(True)
        self.crawl_robots.setToolTip(
            "Сайт публикует правила для автоматических клиентов. Это не юридическое разрешение на сбор."
        )
        crawl_row.addWidget(self.crawl_robots)
        layout.addLayout(crawl_row)
        resume_row = QHBoxLayout()
        self.resume_label = QLabel("Продолжить с файла:")
        resume_row.addWidget(self.resume_label)
        self.resume_path = QLineEdit()
        self.resume_path.setPlaceholderText("state.json")
        self.resume_path.setToolTip(
            "Контрольная точка прерванного обхода. Пустое поле начинает обход сначала. "
            "Уже записанные страницы второй раз не скачиваются."
        )
        resume_row.addWidget(self.resume_path, 1)
        layout.addLayout(resume_row)
        self._toggle_crawl()

        self.results = QTableWidget(0, 0)
        layout.addWidget(self.results, 1)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("Совпадения, предупреждения и ошибки")
        self.details.setFixedHeight(120)
        layout.addWidget(self.details)
        return page

    def _build_xml(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Путь записи:"))
        self.xml_container = QLineEdit("catalog/item")
        self.xml_container.setPlaceholderText("catalog/item")
        path_row.addWidget(self.xml_container, 1)
        layout.addLayout(path_row)
        note = QLabel(
            "Путь — имена элементов через /. Первый шаг это корень документа, дальше только прямые потомки. "
            "Это не CSS и не XPath. Поле «.» означает сам элемент записи. "
            "DTD и сущности в файле отклоняются."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.xml_fields = QTableWidget(0, 5)
        self.xml_fields.setHorizontalHeaderLabels(["Имя", "Путь", "Вид", "Атрибут", "Обязательное"])
        self._add_xml_row({"name": "title", "selector": "title", "kind": "text", "attribute": "", "required": False})
        self._add_xml_row({"name": "price", "selector": "price", "kind": "text", "attribute": "", "required": False})
        self.xml_fields.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.xml_fields)
        actions = QHBoxLayout()
        add_field = QPushButton("Добавить поле")
        add_field.clicked.connect(lambda: self._add_xml_row())
        remove_field = QPushButton("Удалить поле")
        remove_field.clicked.connect(self._remove_xml_row)
        self.xml_open_button = QPushButton("Открыть сохранённый XML")
        self.xml_open_button.clicked.connect(self.preview_xml)
        actions.addWidget(add_field)
        actions.addWidget(remove_field)
        actions.addWidget(self.xml_open_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.xml_results = QTableWidget(0, 0)
        layout.addWidget(self.xml_results, 1)
        self.xml_details = QTextEdit()
        self.xml_details.setReadOnly(True)
        self.xml_details.setPlaceholderText("Записи, предупреждения и ошибки")
        self.xml_details.setFixedHeight(120)
        layout.addWidget(self.xml_details)
        return page

    def _build_json(self, mono: QFont) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        row = QHBoxLayout()
        self.output_path = QLineEdit()
        self.output_path.setPlaceholderText("cards.jsonl")
        browse = QPushButton("Файл…")
        browse.clicked.connect(self._choose_output)
        self.run_button = QPushButton("Сохранить JSONL")
        self.run_button.clicked.connect(self.save_result)
        self.replace_output = QCheckBox("Заменить файл, если он уже есть")
        row.addWidget(self.output_path, 1)
        row.addWidget(browse)
        row.addWidget(self.run_button)
        layout.addLayout(row)
        layout.addWidget(self.replace_output)
        sqlite_row = QHBoxLayout()
        self.sqlite_path = QLineEdit()
        self.sqlite_path.setPlaceholderText("cards.sqlite")
        sqlite_browse = QPushButton("Файл…")
        sqlite_browse.clicked.connect(self._choose_sqlite)
        self.sqlite_button = QPushButton("Сохранить SQLite")
        self.sqlite_button.clicked.connect(self.save_sqlite)
        sqlite_row.addWidget(self.sqlite_path, 1)
        sqlite_row.addWidget(sqlite_browse)
        sqlite_row.addWidget(self.sqlite_button)
        layout.addLayout(sqlite_row)
        sqlite_note = QLabel(
            "В SQLite пустая строка и отсутствие значения различаются: пустое остаётся пустым, "
            "отсутствие записывается как NULL. Галочка замены выше действует и на эту базу."
        )
        sqlite_note.setWordWrap(True)
        layout.addWidget(sqlite_note)
        self.json_view = QPlainTextEdit()
        self.json_view.setFont(mono)
        self.json_view.setReadOnly(True)
        self.json_view.setPlaceholderText("Записи последней страницы в JSON")
        layout.addWidget(self.json_view, 1)
        return page

    def _build_csv(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        row = QHBoxLayout()
        row.addWidget(QLabel("Файл:"))
        self.csv_path = QLineEdit()
        self.csv_path.setPlaceholderText("cards.csv")
        row.addWidget(self.csv_path, 1)
        row.addWidget(QLabel("Разделитель:"))
        self.csv_delimiter = QLineEdit(",")
        self.csv_delimiter.setFixedWidth(48)
        row.addWidget(self.csv_delimiter)
        row.addWidget(QLabel("Кодировка:"))
        self.csv_encoding = QLineEdit("utf-8")
        self.csv_encoding.setFixedWidth(100)
        row.addWidget(self.csv_encoding)
        save = QPushButton("Сохранить CSV")
        save.clicked.connect(self.save_csv)
        self.csv_button = save
        row.addWidget(save)
        layout.addLayout(row)
        note = QLabel(
            "В CSV пустая ячейка не отличается от отсутствующего значения. "
            "Исходное различие остаётся в JSONL и в тексте ошибок на вкладке HTML."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    def _on_curl_send_clicked(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self.cancel_work()
            return
        self.send_curl()

    def _on_send_clicked(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self.cancel_work()
            return
        self.send_request()

    def _fetch_page(self, profile):
        if not isinstance(profile.source, UrlSource):
            raise RunError("Send читает адрес URL. Для файла откройте вкладку HTML.")
        return fetch_url(
            profile.source,
            FetchControl(policy=profile.reliability, job=self._job),
        )

    def _start(
        self,
        action,
        *,
        file: bool,
        mode: str,
        export_format: str = "jsonl",
        client: str | None = None,
        extraction_format: str = "html",
    ) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        try:
            profile = self._profile(
                file=file,
                export_format=export_format,
                client=client,
                extraction_format=extraction_format,
            )
        except ParseDockError as exc:
            self._show_failure(str(exc))
            return
        self._mode = mode
        self._export_format = export_format
        self._extraction_format = extraction_format
        self._job = Job()
        self._set_busy(True)
        worker = LibraryWorker(lambda: action(profile), self)
        worker.succeeded.connect(self._show_success)
        worker.failed.connect(self._show_failure)
        worker.finished.connect(self._set_idle)
        self._worker = worker
        self._timer.start()
        worker.start()

    def _profile(
        self,
        *,
        file: bool,
        export_format: str = "jsonl",
        client: str | None = None,
        extraction_format: str = "html",
    ):
        if export_format == "csv":
            output = self.csv_path.text().strip() or "cards.csv"
            delimiter = self.csv_delimiter.text()
            encoding = self.csv_encoding.text().strip() or "utf-8"
        elif export_format == "sqlite":
            output = self.sqlite_path.text().strip() or "cards.sqlite"
            delimiter = ","
            encoding = "utf-8"
        else:
            output = self.output_path.text().strip() or "parsedock-result.jsonl"
            delimiter = ","
            encoding = "utf-8"
        if extraction_format == "xml":
            container = self.xml_container.text().strip()
            fields = self._xml_rows()
            crawl = None
            resume = None
            dedup: list[str] = []
        else:
            container = self.container.text().strip()
            fields = self._field_rows()
            crawl = self._crawl_document()
            resume = resume_document(
                self.resume_path.text(),
                crawl_enabled=self.crawl_mode.currentData() not in {None, "off"},
            )
            dedup = dedup_keys_from_form(self.dedup_keys.text())
        if file:
            page_path = self._xml_path if extraction_format == "xml" else self._html_path
            if page_path is None:
                kind = "XML" if extraction_format == "xml" else "HTML"
                raise RunError(f"Сначала выберите сохранённый {kind}.")
            source = file_source(page_path)
            base_dir = page_path.parent
        else:
            chosen = client or self._active_client
            if chosen == "curl_cffi":
                source = url_source(
                    self.curl_url.text().strip(),
                    "curl_cffi",
                    trust_private_networks=self.trust_private.isChecked(),
                    impersonate=self.curl_impersonate.currentText(),
                    headers=self._header_map(),
                )
            else:
                source = url_source(
                    self.url.text().strip(),
                    "requests",
                    trust_private_networks=self.trust_private.isChecked(),
                    impersonate="",
                    headers=self._header_map(),
                )
            base_dir = Path.cwd()
        return build_profile(
            base_dir=base_dir,
            source=source,
            container=container,
            fields=fields,
            output_path=output,
            replace_output=self.replace_output.isChecked(),
            export_format=export_format,
            delimiter=delimiter,
            encoding=encoding,
            crawl=crawl,
            deduplicate_keys=dedup,
            reliability=self._reliability_document(),
            resume=resume,
            extraction_format=extraction_format,
        )

    def _header_map(self) -> dict[str, str]:
        raw = self.headers_edit.toPlainText().strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProfileError(f"Headers: неверный JSON ({exc.msg})") from exc
        if not isinstance(data, dict):
            raise ProfileError("Headers: ожидался JSON-объект")
        headers: dict[str, str] = {}
        for key, value in data.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ProfileError("Headers: ключи и значения должны быть строками")
            headers[key] = value
        return headers

    def _xml_rows(self) -> list[dict]:
        rows = []
        for row in range(self.xml_fields.rowCount()):
            kind = self.xml_fields.cellWidget(row, 2)
            required = self.xml_fields.cellWidget(row, 4)
            name_item = self.xml_fields.item(row, 0)
            kept = name_item.data(Qt.ItemDataRole.UserRole) if name_item is not None else None
            extra = kept if isinstance(kept, dict) else {}
            rows.append(
                {
                    "name": _cell(self.xml_fields, row, 0),
                    "selector": _cell(self.xml_fields, row, 1),
                    "kind": kind.currentText() if kind is not None else "text",
                    "attribute": _cell(self.xml_fields, row, 3),
                    "required": required.isChecked() if required is not None else False,
                    "transforms": list(extra.get("transforms") or []),
                    "keep_original": bool(extra.get("keep_original")),
                    "multiple": bool(extra.get("multiple")),
                }
            )
        return rows

    def _add_xml_row(self, item: dict | None = None) -> None:
        values = item or {
            "name": "",
            "selector": "",
            "kind": "text",
            "attribute": "",
            "required": False,
            "transforms": [],
            "keep_original": False,
            "multiple": False,
        }
        row = self.xml_fields.rowCount()
        self.xml_fields.insertRow(row)
        name_item = QTableWidgetItem(values["name"])
        name_item.setData(
            Qt.ItemDataRole.UserRole,
            {
                "transforms": list(values.get("transforms") or []),
                "keep_original": bool(values.get("keep_original")),
                "multiple": bool(values.get("multiple")),
            },
        )
        self.xml_fields.setItem(row, 0, name_item)
        self.xml_fields.setItem(row, 1, QTableWidgetItem(values["selector"]))
        kind = QComboBox()
        kind.addItems(["text", "attribute", "xml"])
        kind.setCurrentText(values["kind"] if values["kind"] in {"text", "attribute", "xml"} else "text")
        self.xml_fields.setCellWidget(row, 2, kind)
        self.xml_fields.setItem(row, 3, QTableWidgetItem(values["attribute"]))
        required = QCheckBox()
        required.setChecked(bool(values["required"]))
        self.xml_fields.setCellWidget(row, 4, required)

    def _remove_xml_row(self) -> None:
        row = self.xml_fields.currentRow()
        if row < 0:
            row = self.xml_fields.rowCount() - 1
        if row >= 0:
            self.xml_fields.removeRow(row)

    def _field_rows(self) -> list[dict]:
        rows = []
        for row in range(self.fields.rowCount()):
            kind = self.fields.cellWidget(row, 2)
            required = self.fields.cellWidget(row, 4)
            operation = self.fields.cellWidget(row, 5)
            original = self.fields.cellWidget(row, 7)
            op_name = str(operation.currentData() or "") if operation is not None else ""
            argument_item = self.fields.item(row, 6)
            if op_name == "__kept__" and argument_item is not None:
                transforms = list(argument_item.data(Qt.ItemDataRole.UserRole) or [])
            else:
                transforms = transform_from_cells(op_name, "" if argument_item is None else argument_item.text())
            rows.append(
                {
                    "name": _cell(self.fields, row, 0),
                    "selector": _cell(self.fields, row, 1),
                    "kind": kind.currentText() if kind is not None else "text",
                    "attribute": _cell(self.fields, row, 3),
                    "required": required.isChecked() if required is not None else False,
                    "transforms": transforms,
                    "keep_original": original.isChecked() if original is not None else False,
                    "multiple": bool(original.property("multiple")) if original is not None else False,
                }
            )
        return rows

    def _add_field_row(self, item: dict | None = None) -> None:
        values = item or {
            "name": "",
            "selector": "",
            "kind": "text",
            "attribute": "",
            "required": False,
            "op": "",
            "argument": "",
            "transforms": [],
            "keep_original": False,
            "multiple": False,
        }
        row = self.fields.rowCount()
        self.fields.insertRow(row)
        self.fields.setItem(row, 0, QTableWidgetItem(values["name"]))
        self.fields.setItem(row, 1, QTableWidgetItem(values["selector"]))
        kind = QComboBox()
        kind.addItems(list(_KINDS))
        kind.setCurrentText(values["kind"])
        self.fields.setCellWidget(row, 2, kind)
        self.fields.setItem(row, 3, QTableWidgetItem(values["attribute"]))
        required = QCheckBox()
        required.setChecked(bool(values["required"]))
        self.fields.setCellWidget(row, 4, required)
        operation = QComboBox()
        for code, label in _TRANSFORM_OPS:
            operation.addItem(label, code)
        op_name = str(values.get("op") or "")
        if op_name == "__kept__":
            operation.addItem("как в профиле", "__kept__")
        index = operation.findData(op_name)
        operation.setCurrentIndex(0 if index < 0 else index)
        operation.setToolTip(
            "Пробелы убирают лишние пробелы. Число ждёт один символ, например . или ,. "
            "Валюта: symbol=₽ decimal=. или code=RUB decimal=. "
            "Дата: %d.%m.%Y. Адрес можно оставить пустым: тогда берётся адрес страницы."
        )
        self.fields.setCellWidget(row, 5, operation)
        argument_item = QTableWidgetItem(str(values.get("argument") or ""))
        if op_name == "__kept__":
            argument_item.setData(Qt.ItemDataRole.UserRole, list(values.get("transforms") or []))
        self.fields.setItem(row, 6, argument_item)
        original = QCheckBox()
        original.setChecked(bool(values.get("keep_original")))
        original.setProperty("multiple", bool(values.get("multiple")))
        original.setToolTip("Рядом записывается поле с суффиксом _original, до преобразования.")
        self.fields.setCellWidget(row, 7, original)

    def _remove_field_row(self) -> None:
        row = self.fields.currentRow()
        if row < 0:
            row = self.fields.rowCount() - 1
        if row >= 0:
            self.fields.removeRow(row)

    def _choose_output(self) -> None:
        chosen, _ = QFileDialog.getSaveFileName(self, "JSONL", "cards.jsonl", "JSONL (*.jsonl)")
        if chosen:
            self.output_path.setText(chosen)
            self.replace_output.setChecked(True)

    def _choose_sqlite(self) -> None:
        chosen, _ = QFileDialog.getSaveFileName(self, "SQLite", "cards.sqlite", "SQLite (*.sqlite *.db)")
        if chosen:
            self.sqlite_path.setText(chosen)
            self.replace_output.setChecked(True)

    def _save_profile(self) -> None:
        chosen, _ = QFileDialog.getSaveFileName(self, "Профиль", "profile.json", "JSON (*.json)")
        if not chosen:
            return
        try:
            on_xml = self.tabs.currentIndex() == self._xml_tab
            page_path = self._xml_path if on_xml else self._html_path
            use_file = page_path is not None and not self.url.text().strip()
            profile = self._profile(file=use_file, extraction_format="xml" if on_xml else "html")
        except ParseDockError as exc:
            self._show_failure(str(exc))
            return
        document = profile_document(
            source=_source_document(profile),
            container=profile.extraction.container,
            fields=[_field_document(field) for field in profile.extraction.fields],
            output_path=self.output_path.text().strip() or "parsedock-result.jsonl",
            replace_output=self.replace_output.isChecked(),
            extraction_format=profile.extraction.format,
            crawl=None if profile.extraction.format == "xml" else self._crawl_document(),
            deduplicate_keys=[] if profile.extraction.format == "xml" else list(profile.deduplicate_keys),
            resume=None if profile.resume is None or profile.extraction.format == "xml" else {"path": self.resume_path.text().strip()},
            reliability=reliability_from_form(
                number_text(profile.reliability.max_attempts),
                number_text(profile.reliability.min_interval_seconds),
                number_text(profile.reliability.backoff_seconds),
                extra=reliability_extra(profile.reliability),
            ),
        )
        try:
            write_text_refusing_symlink(
                Path(chosen),
                json.dumps(document, ensure_ascii=False, indent=2),
            )
        except ExportError as exc:
            self._show_failure(str(exc))
            return
        self.response.setPlainText(f"Профиль сохранён: {chosen}")

    def _load_profile(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, "Профиль", "", "JSON (*.json)")
        if not chosen:
            return
        try:
            profile = load_profile(chosen)
        except ParseDockError as exc:
            self._show_failure(str(exc))
            return
        self._apply_profile(profile)
        note = ""
        if isinstance(profile.source, UrlSource) and profile.source.client == "playwright":
            note = (
                "\n\nВ профиле указан браузер playwright. Эта вкладка его не открывает. "
                "Сохранение отсюда запишет requests или curl_cffi."
            )
        elif isinstance(profile.source, UrlSource) and profile.source.client == "curl_cffi":
            self.tabs.setCurrentIndex(self._curl_tab)
        self.response.setPlainText(f"Профиль загружен: {chosen}{note}")

    def _apply_profile(self, profile) -> None:
        source = profile.source
        if isinstance(source, UrlSource):
            self._html_path = None
            self.url.setText(source.url)
            self.curl_url.setText(source.url)
            self._active_client = "curl_cffi" if source.client == "curl_cffi" else "requests"
            self.curl_impersonate.setCurrentText(source.impersonate or "" if source.client == "curl_cffi" else "")
            self.trust_private.setChecked(source.trust_private_networks)
            self.headers_edit.setPlainText(
                json.dumps(dict(source.headers), ensure_ascii=False, indent=2) if source.headers else ""
            )
        elif isinstance(source, FileSource):
            self.url.clear()
            if profile.extraction.format == "xml":
                self._xml_path = source.path
                self._html_path = None
            else:
                self._html_path = source.path
                self._xml_path = None
        if profile.extraction.format == "xml":
            self.xml_container.setText(profile.extraction.container)
            self.xml_fields.setRowCount(0)
            for field in profile.extraction.fields:
                self._add_xml_row(_field_document(field))
            self.tabs.setCurrentIndex(self._xml_tab)
        else:
            self.container.setText(profile.extraction.container)
            self.curl_selector.setText(profile.extraction.container)
            self.fields.setRowCount(0)
            for field in profile.extraction.fields:
                transforms = [transform_document(item) for item in field.transforms]
                if len(field.transforms) == 1 and _argument_fits(field.transforms[0]):
                    op_name = field.transforms[0].op
                    argument = argument_text(field.transforms[0])
                elif field.transforms:
                    op_name = "__kept__"
                    argument = "несколько операций"
                else:
                    op_name = ""
                    argument = ""
                self._add_field_row(
                {
                    "name": field.name,
                    "selector": field.selector,
                    "kind": field.kind,
                    "attribute": field.attribute or "",
                    "required": field.required,
                    "op": op_name,
                    "argument": argument,
                    "transforms": transforms,
                    "keep_original": field.keep_original,
                    "multiple": field.multiple,
                }
            )
        self.output_path.setText(str(profile.export_path))
        self.replace_output.setChecked(profile.export_mode == "replace")
        self.dedup_keys.setText(", ".join(profile.deduplicate_keys))
        self._apply_crawl(profile.crawl)
        self.resume_path.setText("" if profile.resume is None else str(profile.resume.path))
        self._apply_reliability(profile.reliability)

    def _show_success(self, result) -> None:
        if hasattr(result, "status_code") and hasattr(result, "text"):
            text = f"HTTP {result.status_code}\n{result.url}\n\n{result.text}"
            self.status.setText(f"HTTP {result.status_code}")
            if self._active_client == "curl_cffi":
                self.tabs.setCurrentIndex(self._curl_tab)
                self.curl_response.setPlainText(text)
            else:
                self.tabs.setCurrentIndex(0)
                self.response.setPlainText(text)
            return
        records = getattr(result, "records", [])
        xml_view = self._extraction_format == "xml"
        self._fill_table(records[:PREVIEW_LIMIT], self.xml_results if xml_view else self.results)
        self.json_view.setPlainText(json.dumps(records[:PREVIEW_LIMIT], ensure_ascii=False, indent=2))
        warnings = list(getattr(result, "warnings", []))
        errors = list(getattr(result, "errors", []))
        lines = []
        matches = getattr(result, "container_matches", None)
        if matches is not None:
            if xml_view:
                lines.append(f"Записей по пути: {matches}.")
            else:
                lines.append(f"Блоков по селектору: {matches}.")
        total = getattr(result, "total_records", len(records))
        lines.append(f"Записей: {total}. В таблице первые {min(len(records), PREVIEW_LIMIT)}.")
        if getattr(result, "truncated", False):
            lines.append("Показана только часть записей.")
        removed = getattr(result, "duplicates_removed", 0)
        if removed:
            lines.append(f"Снято дублей: {removed}.")
        stop = getattr(result, "stop_reason", "")
        pages = getattr(result, "pages_fetched", 0)
        if pages:
            lines.append(f"Страниц: {pages}.")
        if stop:
            lines.append(f"Остановка: {stop}.")
        if self._mode == "save":
            if self._export_format == "sqlite":
                saved = self.sqlite_path.text().strip() or "cards.sqlite"
            elif self._export_format == "csv":
                saved = self.csv_path.text().strip() or "cards.csv"
            else:
                saved = self.output_path.text().strip() or "parsedock-result.jsonl"
            lines.append(f"Файл: {saved}")
        for warning in warnings:
            lines.append(f"Предупреждение: {warning}")
        for error in errors:
            lines.append(f"Ошибка, карточка {error.card_number}, поле {error.field_name}: {error.message}")
        details = self.xml_details if xml_view else self.details
        details.setPlainText("\n".join(lines))
        if self._mode == "preview":
            self.tabs.setCurrentIndex(self._xml_tab if xml_view else self._html_tab)
        self.status.setText("Готово.")

    def _show_failure(self, message: str) -> None:
        self.status.setText(message)
        if self._mode == "fetch" or self.tabs.currentIndex() == 0:
            self.response.setPlainText(message)
        self.details.setPlainText(message)
        if self._extraction_format == "xml":
            self.xml_details.setPlainText(message)

    def _toggle_crawl(self) -> None:
        mode = self.crawl_mode.currentData()
        active = mode not in {None, "off"}
        self.crawl_target.setVisible(active)
        self.crawl_pages_label.setVisible(active)
        self.crawl_pages.setVisible(active)
        self.crawl_robots.setVisible(active)
        self.resume_label.setVisible(active)
        self.resume_path.setVisible(active)
        self.crawl_target.setReadOnly(mode == "urls")
        if mode == "page_param":
            self.crawl_target.setPlaceholderText("имя параметра, например page")
        elif mode == "next_link":
            self.crawl_target.setPlaceholderText("селектор ссылки, например a.next")
        elif mode == "urls":
            self.crawl_target.setPlaceholderText("список адресов из профиля")

    def _reliability_document(self) -> dict | None:
        return reliability_from_form(
            self.attempts.text(),
            self.min_interval.text(),
            self.backoff.text(),
            extra=self._reliability_extra,
        )

    def _apply_reliability(self, spec) -> None:
        self.attempts.setText(str(spec.max_attempts))
        self.min_interval.setText(number_text(spec.min_interval_seconds))
        self.backoff.setText(number_text(spec.backoff_seconds))
        self._reliability_extra = reliability_extra(spec)

    def _crawl_document(self) -> dict | None:
        return crawl_from_form(
            str(self.crawl_mode.currentData() or "off"),
            self.crawl_target.text(),
            self.crawl_pages.text(),
            obey_robots=self.crawl_robots.isChecked(),
            urls=self._crawl_urls,
        )

    def _apply_crawl(self, crawl) -> None:
        self._crawl_urls = ()
        if crawl is None:
            self._set_crawl_mode("off")
            return
        if crawl.mode == "urls" and self.crawl_mode.findData("urls") < 0:
            self.crawl_mode.addItem("список адресов", "urls")
        self._set_crawl_mode(crawl.mode)
        self.crawl_pages.setText(str(crawl.max_pages))
        self.crawl_robots.setChecked(crawl.obey_robots)
        if crawl.mode == "page_param" and crawl.page_param is not None:
            self.crawl_target.setText(crawl.page_param.name)
        elif crawl.mode == "next_link" and crawl.next_link is not None:
            self.crawl_target.setText(crawl.next_link.selector)
        elif crawl.mode == "urls":
            self._crawl_urls = crawl.urls
            self.crawl_target.setText(" ".join(crawl.urls))

    def _set_crawl_mode(self, mode: str) -> None:
        index = self.crawl_mode.findData(mode)
        self.crawl_mode.setCurrentIndex(0 if index < 0 else index)

    def _refresh_status(self) -> None:
        if self._job is None or self._job.state not in {"running", "cancelling"}:
            return
        text = self._job.status()
        line = (
            f"{text['state_label']}. {text['stage_label']}. "
            f"Страниц: {text['pages_fetched']}. Записей: {text['record_count']}. "
            f"Дублей: {text['duplicates_removed']}. Ошибок: {text['error_count']}."
        )
        self.status.setText(line)
        if self._mode == "fetch":
            view = self.curl_response if self._active_client == "curl_cffi" else self.response
            view.setPlainText(line)

    def _set_busy(self, busy: bool) -> None:
        self.send_button.setText("Отмена" if busy else "Send")
        for button in (
            self.save_profile_button,
            self.load_profile_button,
            self.preview_url_button,
            self.preview_file_button,
            self.run_button,
            self.csv_button,
            self.curl_send_button,
            self.curl_find_button,
            self.sqlite_button,
            self.xml_open_button,
        ):
            button.setEnabled(not busy)

    def _set_idle(self) -> None:
        self._timer.stop()
        self._set_busy(False)

    def _fill_table(self, records: list[dict], table: QTableWidget | None = None) -> None:
        target = self.results if table is None else table
        columns: list[str] = []
        for record in records:
            for key in record:
                if key not in columns:
                    columns.append(key)
        target.clear()
        target.setColumnCount(len(columns))
        target.setHorizontalHeaderLabels(columns)
        target.setRowCount(len(records))
        for row, record in enumerate(records):
            for column, key in enumerate(columns):
                value = record.get(key)
                target.setItem(row, column, QTableWidgetItem("" if value is None else str(value)))


def _section(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("section")
    return label


def _plain_profile_error(message: str) -> str:
    """Короткое объяснение пустого поля вместо пути в схеме профиля."""

    if "profile.source.url" in message:
        return "Укажите адрес на вкладке Requests. Поле URL пустое."
    if "profile.extraction.container" in message:
        return "Укажите селектор блока на вкладке HTML."
    if "profile.extraction.fields" in message and ".name" in message:
        return "На вкладке HTML у одного из полей пустое имя."
    if "profile.extraction.fields" in message and ".selector" in message:
        return "На вкладке HTML у одного из полей пустой селектор."
    if "profile.extraction.fields" in message and ".attribute" in message:
        return "На вкладке HTML для вида attribute укажите имя атрибута."
    if "next_link.selector" in message or "page_param.name" in message:
        return "Для обхода нескольких страниц укажите параметр или селектор ссылки на вкладке HTML."
    return message


def _later(text: str) -> QWidget:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(12, 12, 12, 12)
    label = QLabel(text)
    label.setWordWrap(True)
    layout.addWidget(label)
    layout.addStretch(1)
    return page


def _source_document(profile) -> dict:
    source = profile.source
    if isinstance(source, FileSource):
        return file_source(source.path)
    return url_source(
        source.url,
        source.client,
        trust_private_networks=source.trust_private_networks,
        impersonate=source.impersonate or "",
        headers=dict(source.headers),
    )


def _field_document(field) -> dict:
    transforms = [transform_document(item) for item in field.transforms]
    if len(field.transforms) == 1 and _argument_fits(field.transforms[0]):
        op_name = field.transforms[0].op
        argument = argument_text(field.transforms[0])
    elif field.transforms:
        op_name = "__kept__"
        argument = ""
    else:
        op_name = ""
        argument = ""
    return {
        "name": field.name,
        "selector": field.selector,
        "kind": field.kind,
        "attribute": field.attribute or "",
        "required": field.required,
        "op": op_name,
        "argument": argument,
        "transforms": transforms,
        "keep_original": field.keep_original,
        "multiple": field.multiple,
    }


def _argument_fits(item) -> bool:
    """Пробел как разделитель тысяч в одну ячейку не записывается."""

    return item.thousands_separator != " "


def _cell(table: QTableWidget, row: int, column: int) -> str:
    item = table.item(row, column)
    if item is None:
        return ""
    return item.text().strip()
