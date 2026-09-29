"""Тёмная тема окна. Здесь только таблица стилей Qt, без виджетов и действий."""

STYLE = """
QMainWindow, QWidget {
    background: #2b2b2b;
    color: #d0d0d0;
    font-size: 13px;
}
QTabWidget::pane {
    border: none;
    background: #2b2b2b;
    top: 0px;
}
QTabBar::tab {
    background: #2a2a2a;
    color: #c8c8c8;
    padding: 7px 16px 7px 10px;
    border: 1px solid #6a6a6a;
    border-radius: 12px;
    margin: 4px 5px 8px 0px;
    min-height: 22px;
}
QTabBar::tab:hover {
    background: #343434;
    border-color: #8a8a8a;
}
QTabBar::tab:selected {
    background: #3c3c3c;
    color: #f2f2f2;
    border-color: #b0b0b0;
}
QLineEdit, QComboBox {
    background: #1f1f1f;
    color: #e0e0e0;
    border: 1px solid #454545;
    border-radius: 12px;
    selection-background-color: #3d4f66;
    padding: 6px 12px;
    min-height: 18px;
}
QPlainTextEdit, QTextEdit {
    background: #1f1f1f;
    color: #e0e0e0;
    border: 1px solid #454545;
    border-radius: 16px;
    selection-background-color: #3d4f66;
    padding: 10px 12px;
}
QTableWidget {
    background: #1f1f1f;
    color: #e0e0e0;
    border: 1px solid #454545;
    border-radius: 14px;
    selection-background-color: #3d4f66;
    padding: 4px;
    gridline-color: #343434;
}
QComboBox::drop-down {
    border: none;
    width: 24px;
    border-top-right-radius: 12px;
    border-bottom-right-radius: 12px;
}
QComboBox QAbstractItemView {
    background: #1f1f1f;
    color: #e0e0e0;
    border: 1px solid #454545;
    border-radius: 10px;
    padding: 4px;
    outline: none;
    selection-background-color: #3d4f66;
}
QPushButton {
    background: #3a3a3a;
    color: #e6e6e6;
    border: 1px solid #555555;
    border-radius: 12px;
    padding: 7px 16px;
}
QPushButton:hover {
    background: #474747;
}
QPushButton:disabled {
    color: #777777;
    background: #2f2f2f;
}
QHeaderView::section {
    background: #333333;
    color: #d0d0d0;
    border: none;
    border-bottom: 1px solid #3f3f3f;
    padding: 6px;
}
QHeaderView::section:first {
    border-top-left-radius: 12px;
}
QHeaderView::section:last {
    border-top-right-radius: 12px;
}
QCheckBox {
    spacing: 8px;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border-radius: 5px;
    border: 1px solid #555555;
    background: #1f1f1f;
}
QCheckBox::indicator:checked {
    background: #4d6d8c;
    border-color: #6d8eae;
}
QScrollBar:vertical {
    background: transparent;
    width: 12px;
    margin: 6px 2px 6px 0px;
}
QScrollBar::handle:vertical {
    background: #4a4a4a;
    border-radius: 5px;
    min-height: 28px;
}
QScrollBar:horizontal {
    background: transparent;
    height: 12px;
    margin: 0px 6px 2px 6px;
}
QScrollBar::handle:horizontal {
    background: #4a4a4a;
    border-radius: 5px;
    min-width: 28px;
}
QScrollBar::add-line, QScrollBar::sub-line,
QScrollBar::add-page, QScrollBar::sub-page {
    background: transparent;
    height: 0px;
    width: 0px;
}
QLabel#section {
    color: #c8c8c8;
    padding-top: 6px;
}
"""
