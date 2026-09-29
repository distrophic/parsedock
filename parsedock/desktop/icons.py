"""Небольшие светлые значки вкладок. Рисуются кодом, без внешних файлов."""

from __future__ import annotations

from PySide6.QtCore import QLineF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap


def tab_icon(name: str) -> QIcon:
    """Значок 16×16 для вкладки. Неизвестное имя даёт пустой значок."""

    painter_for = _SHAPES.get(name)
    if painter_for is None:
        return QIcon()
    icon = QIcon()
    for size in (16, 32):
        icon.addPixmap(_paint(painter_for, size))
    return icon


def _paint(draw, size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("#d5d5d5"))
    pen.setWidthF(max(1.15, size / 14))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    scale = size / 16
    painter.scale(scale, scale)
    draw(painter)
    painter.end()
    return pixmap


def _requests(painter: QPainter) -> None:
    painter.drawEllipse(QRectF(1.6, 1.6, 12.8, 12.8))
    arrow = QPainterPath()
    arrow.moveTo(4.8, 8)
    arrow.lineTo(10.4, 8)
    arrow.moveTo(7.8, 5.4)
    arrow.lineTo(10.8, 8)
    arrow.lineTo(7.8, 10.6)
    painter.drawPath(arrow)


def _curl(painter: QPainter) -> None:
    painter.drawEllipse(QRectF(2.2, 2.2, 11.6, 11.6))
    painter.drawArc(QRectF(4.6, 4.6, 6.8, 6.8), 40 * 16, 280 * 16)


def _html(painter: QPainter) -> None:
    painter.drawPath(_chevron(2.2, opening=True))
    painter.drawPath(_chevron(8.6, opening=False))
    painter.drawLine(QLineF(9.1, 3.1, 6.9, 12.9))


def _chevron(x: float, *, opening: bool) -> QPainterPath:
    path = QPainterPath()
    if opening:
        path.moveTo(x + 4.2, 2.8)
        path.lineTo(x + 1.2, 7.2)
        path.quadTo(x + 0.5, 8, x + 1.2, 8.8)
        path.lineTo(x + 4.2, 13.2)
    else:
        path.moveTo(x + 0.6, 2.8)
        path.lineTo(x + 3.6, 7.2)
        path.quadTo(x + 4.3, 8, x + 3.6, 8.8)
        path.lineTo(x + 0.6, 13.2)
    return path


def _json(painter: QPainter) -> None:
    painter.drawPath(_brace(2.6, opening=True))
    painter.drawPath(_brace(8.2, opening=False))


def _brace(x: float, *, opening: bool) -> QPainterPath:
    path = QPainterPath()
    if opening:
        path.moveTo(x + 3.6, 2.3)
        path.cubicTo(x + 1.2, 2.3, x + 1.2, 5.2, x + 1.2, 6.4)
        path.cubicTo(x + 1.2, 7.4, x + 0.2, 7.6, x + 0.2, 8)
        path.cubicTo(x + 0.2, 8.4, x + 1.2, 8.6, x + 1.2, 9.6)
        path.cubicTo(x + 1.2, 10.8, x + 1.2, 13.7, x + 3.6, 13.7)
    else:
        path.moveTo(x + 0.4, 2.3)
        path.cubicTo(x + 2.8, 2.3, x + 2.8, 5.2, x + 2.8, 6.4)
        path.cubicTo(x + 2.8, 7.4, x + 3.8, 7.6, x + 3.8, 8)
        path.cubicTo(x + 3.8, 8.4, x + 2.8, 8.6, x + 2.8, 9.6)
        path.cubicTo(x + 2.8, 10.8, x + 2.8, 13.7, x + 0.4, 13.7)
    return path


def _csv(painter: QPainter) -> None:
    painter.drawRoundedRect(QRectF(2.2, 2.4, 11.6, 11.2), 2.4, 2.4)
    painter.drawLine(QLineF(4.2, 6.2, 11.8, 6.2))
    painter.drawLine(QLineF(4.2, 10, 11.8, 10))
    painter.drawLine(QLineF(8, 4.4, 8, 11.6))


def _xml(painter: QPainter) -> None:
    painter.drawPath(_chevron(2.6, opening=True))
    painter.drawPath(_chevron(8.4, opening=False))


def _code(painter: QPainter) -> None:
    painter.drawRoundedRect(QRectF(2.2, 2.6, 11.6, 10.8), 2.8, 2.8)
    mark = QPainterPath()
    mark.moveTo(4.6, 6.2)
    mark.lineTo(6.5, 8)
    mark.lineTo(4.6, 9.8)
    painter.drawPath(mark)
    painter.drawLine(QLineF(7.8, 10.1, 11, 10.1))


def _terminal(painter: QPainter) -> None:
    painter.drawRoundedRect(QRectF(1.8, 2.8, 12.4, 10.4), 2.8, 2.8)
    prompt = QPainterPath()
    prompt.moveTo(4.1, 6.5)
    prompt.lineTo(6.3, 8)
    prompt.lineTo(4.1, 9.5)
    painter.drawPath(prompt)
    painter.drawLine(QLineF(7.6, 9.6, 11.2, 9.6))


_SHAPES = {
    "requests": _requests,
    "curl": _curl,
    "html": _html,
    "json": _json,
    "csv": _csv,
    "xml": _xml,
    "code": _code,
    "terminal": _terminal,
}
