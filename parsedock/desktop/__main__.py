"""Запуск окна: python -m parsedock.desktop"""

from __future__ import annotations

import sys


def main() -> int:
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        raise SystemExit(
            "Окно не запущено: пакет PySide6 не установлен. "
            "Установите дополнительную зависимость parsedock[desktop]. "
            "Ядро без окна по-прежнему работает."
        ) from exc
    from parsedock.desktop.window import MainWindow

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
