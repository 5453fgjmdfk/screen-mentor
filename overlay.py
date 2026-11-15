"""全屏透明引导层：画箭头、圈住控件、配一句话。

关键：这层窗口是「鼠标穿透」的，用户照常点他的软件，箭头只是画在上面。
"""
from __future__ import annotations

import math
import time

from PyQt6.QtCore import QPointF, QRect, QRectF, Qt, QTimer
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)
from PyQt6.QtWidgets import QWidget

ACCENT = QColor(64, 132, 255)
WARM = QColor(255, 174, 32)
CARD_BG = QColor(15, 19, 27, 232)
CARD_LINE = QColor(255, 255, 255, 40)
TXT = QColor(242, 246, 253)
SUB = QColor(166, 179, 199)

FONT_FAMILY = "Microsoft YaHei UI"


def virtual_geometry() -> QRect:
    """所有显示器拼起来的矩形（Qt 逻辑坐标）。"""
    from PyQt6.QtGui import QGuiApplication

    geo = QRect()
    for scr in QGuiApplication.screens():
        geo = geo.united(scr.geometry())
    if geo.isNull():
        geo = QRect(0, 0, 1280, 720)
    return geo


def _anchor_on_rect(rect: QRectF, toward: QPointF) -> QPointF:
    """求矩形边框上离 toward 最近的点（用来当箭头起点）。"""
    cx, cy = rect.center().x(), rect.center().y()
    dx, dy = toward.x() - cx, toward.y() - cy
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return QPointF(cx, cy)
    hw, hh = rect.width() / 2, rect.height() / 2
    sx = hw / abs(dx) if abs(dx) > 1e-6 else float("inf")
    sy = hh / abs(dy) if abs(dy) > 1e-6 else float("inf")
    s = min(sx, sy)
    return QPointF(cx + dx * s, cy + dy * s)


class GuideOverlay(QWidget):
    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

        self._phys: tuple[int, int] | None = None  # 截图的物理像素尺寸
        self._mode: str | None = None  # guide / thinking / hint
        self._rect: QRectF | None = None
        self._mark_target = True
        self._drag_to: QPointF | None = None
        self._hint_at: QPointF | None = None
        self._say = ""
        self._detail = ""
        self._num = 0
        self._phase = 0.0
        self._demo_started = 0.0

        self.setGeometry(virtual_geometry())

        self._anim = QTimer(self)
        self._anim.setInterval(33)
        self._anim.timeout.connect(self._tick)

        self._ttl = QTimer(self)
        self._ttl.setSingleShot(True)
        self._ttl.timeout.connect(self.clear)

    # ---------- 对外 ----------

    def set_capture_size(self, width: int, height: int) -> None:
        self._phys = (int(width), int(height))

    def phys_to_local(self, x: float, y: float, w: float, h: float) -> QRectF:
        """截图物理像素坐标 -> 本窗口局部坐标（比例映射，抗 DPI 缩放）。"""
        pw, ph = self._phys or (self.width(), self.height())
        lw, lh = max(1, self.width()), max(1, self.height())
        kx, ky = lw / pw, lh / ph
        return QRectF(x * kx, y * ky, max(2.0, w * kx), max(2.0, h * ky))

    def show_guide(
        self,
        rect_phys: tuple[float, float, float, float] | None,
        say: str,
        detail: str = "",
        number: int = 1,
        seconds: int = 45,
        *,
        mark_target: bool = True,
        drag_to_phys: tuple[float, float] | None = None,
        hint_at_phys: tuple[float, float] | None = None,
    ) -> None:
        self._mode = "guide"
        self._demo_started = time.monotonic()
        self._mark_target = mark_target
        self._say = say or ""
        self._detail = detail or ""
        self._num = number
        self._drag_to = self.phys_to_local(*drag_to_phys, 0, 0).topLeft() if drag_to_phys else None
        self._hint_at = self.phys_to_local(*hint_at_phys, 0, 0).topLeft() if hint_at_phys else None
        if rect_phys:
            x, y, w, h = rect_phys
            r = self.phys_to_local(x, y, w, h)
            pad = min(1.5, min(r.width(), r.height()) * 0.08)
            self._rect = r.adjusted(-pad, -pad, pad, pad)
        else:
            self._rect = None
        self._reveal(seconds)

    def show_thinking(self, text: str = "正在看你的屏幕…") -> None:
        self._mode = "thinking"
        self._say = text
        self._detail = ""
        self._rect = None
        self._reveal(30)

    def show_hint(self, text: str, seconds: int = 12) -> None:
        self._mode = "hint"
        self._say = text
        self._detail = ""
        self._rect = None
        self._reveal(seconds)

    def clear(self) -> None:
        self._mode = None
        self._rect = None
        self._say = ""
        self._detail = ""
        self._ttl.stop()
        self._anim.stop()
        self.update()

    @property
    def has_guide(self) -> bool:
        return self._mode == "guide"

    def restore_guide(self) -> None:
        if self.has_guide:
            self.show()
            self.raise_()
            self.update()

    # ---------- 内部 ----------

    def _reveal(self, seconds: int) -> None:
        animated = self._mode == "thinking" or (self.has_guide and self._rect is not None and self._mark_target)
        if animated:
            self._anim.start()
        else:
            self._anim.stop()
        self.show()
        self.raise_()
        if seconds > 0:
            self._ttl.start(max(3, seconds) * 1000)
        else:
            self._ttl.stop()
        self.update()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.09) % (math.pi * 4)
        if self._mode:
            self.update()

    # ---------- 绘制 ----------

    def paintEvent(self, event) -> None:
        if not self._mode:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        if self._mode == "thinking":
            self._paint_thinking(p)
        elif self._mode == "hint":
            self._paint_hint(p)
        else:
            self._paint_guide(p)
        p.end()

    def _card_rect(self, w: int = 310) -> tuple[QRectF, int]:
        head_font = QFont(FONT_FAMILY, 12)
        head_font.setBold(True)
        metrics = QFontMetrics(head_font)
        body_metrics = QFontMetrics(QFont(FONT_FAMILY, 10))
        preferred = max(120, metrics.horizontalAdvance(self._say) + 40)
        if self._detail:
            w = max(w, 420)
            preferred = max(preferred, min(w, body_metrics.horizontalAdvance(self._detail) + 32))
        cw = min(w, self.width() - 32, preferred)
        inner = cw - 32
        hh = QFontMetrics(head_font).boundingRect(
            QRect(0, 0, inner, 4000),
            int(Qt.TextFlag.TextWordWrap),
            self._say or " ",
        ).height()
        ch = 24 + hh
        if self._detail:
            ch += 6 + body_metrics.boundingRect(
                QRect(0, 0, inner, 4000), int(Qt.TextFlag.TextWordWrap), self._detail
            ).height()
        return QRectF(0, 0, cw, ch), inner

    def _paint_guide(self, p: QPainter) -> None:
        card, inner = self._card_rect()

        if self._rect is not None:
            target = QRectF(self._rect)
            center = target.center()
        else:
            center = QPointF(self.width() / 2, self.height() * 0.62)
            target = None

        # 卡片位置：优先放目标上方，放不下就放下方
        if target is not None:
            occupied = target.united(QRectF(self._drag_to.x() - 8, self._drag_to.y() - 8, 16, 16)) if self._drag_to else target
            cx = min(max(center.x() - card.width() / 2, 16), self.width() - card.width() - 16)
            above_y = occupied.top() - card.height() - 16
            if above_y > 16:
                card.moveTo(cx, above_y)
            else:
                card.moveTo(cx, min(occupied.bottom() + 16, self.height() - card.height() - 16))
        else:
            where = self._hint_at or QPointF(self.width() / 2, self.height() * .66)
            card.moveTo(min(max(where.x() - card.width() / 2, 16), self.width() - card.width() - 16),
                        min(max(where.y(), 16), self.height() - card.height() - 16))

        if target is not None and self._drag_to is not None:
            p.setPen(QPen(ACCENT.lighter(140), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(self._drag_to, 8, 8)
            self._paint_target(p, target)
            self._paint_mouse_demo(p, center, self._drag_to)
        elif target is not None and self._mark_target:
            self._paint_target(p, target)
            self._paint_mouse_demo(p, center)
        elif target is not None:
            start = _anchor_on_rect(QRectF(card), center)
            end = _anchor_on_rect(QRectF(target), start)
            self._paint_arrow(p, start, end)

        self._paint_card(p, card, inner)

    def _paint_mouse_demo(self, p: QPainter, start: QPointF, end: QPointF | None = None) -> None:
        elapsed = (time.monotonic() - self._demo_started) % (3.0 if end is not None else 2.2)
        if end is not None:
            progress = min(1.0, max(0.0, (elapsed - .55) / 1.25))
            eased = progress * progress * (3 - 2 * progress)
            cursor = start + (end - start) * eased
            pressed = .4 <= elapsed < 1.82
            pulse = elapsed - (1.82 if elapsed >= 1.82 else .4)
            pulse_at = end if elapsed >= 1.82 else start
            if elapsed >= 2.4:
                return
        else:
            approach = min(1.0, elapsed / .38)
            cursor = start + QPointF(23, 19) * ((1 - approach) ** 2)
            pressed = .45 <= elapsed < .66
            pulse = elapsed - .45
            pulse_at = start
            if elapsed >= 1.48:
                return
        if 0 <= pulse < .46:
            amount = pulse / .46
            ink = QColor(ACCENT)
            ink.setAlpha(round(220 * (1 - amount)))
            p.setPen(QPen(ink, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(pulse_at, 6 + 15 * amount, 6 + 15 * amount)

        p.save()
        p.translate(cursor)
        pointer = QPolygonF([QPointF(0, 0), QPointF(1, 20), QPointF(6, 15),
                            QPointF(11, 23), QPointF(15, 21), QPointF(10, 13), QPointF(17, 12)])
        p.setPen(QPen(QColor(16, 26, 42), 1.5, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(ACCENT.lighter(140) if pressed else QColor(251, 253, 255))
        p.drawPolygon(pointer)
        p.restore()

    def _paint_arrow(self, p: QPainter, a: QPointF, b: QPointF) -> None:
        dx, dy = b.x() - a.x(), b.y() - a.y()
        dist = max(1.0, math.hypot(dx, dy))
        # 稍微拐个弯，别像一根钢筋
        nx, ny = -dy / dist, dx / dist
        ctrl = QPointF(
            (a.x() + b.x()) / 2 + nx * dist * 0.12,
            (a.y() + b.y()) / 2 + ny * dist * 0.12,
        )
        path = QPainterPath(a)
        path.quadTo(ctrl, b)

        glow = QPen(QColor(10, 18, 34, 100), 4)
        glow.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.strokePath(path, glow)

        pen = QPen(ACCENT, 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.strokePath(path, pen)

        # 箭头尖
        ang = math.atan2(b.y() - ctrl.y(), b.x() - ctrl.x())
        size = 7.5
        head = QPolygonF(
            [
                b,
                QPointF(
                    b.x() - size * math.cos(ang - 0.42),
                    b.y() - size * math.sin(ang - 0.42),
                ),
                QPointF(
                    b.x() - size * math.cos(ang + 0.42),
                    b.y() - size * math.sin(ang + 0.42),
                ),
            ]
        )
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ACCENT)
        p.drawPolygon(head)

    def _paint_target(self, p: QPainter, target: QRectF) -> None:
        radius = min(4.0, target.width() / 4, target.height() / 4)
        p.setPen(QPen(QColor(15, 24, 40, 165), 3.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(target, radius, radius)
        p.setPen(QPen(ACCENT.lighter(130), 1.7))
        p.drawRoundedRect(target, radius, radius)

    def _paint_card(self, p: QPainter, card: QRectF, inner: int) -> None:
        p.setPen(QPen(CARD_LINE, 1))
        p.setBrush(CARD_BG)
        p.drawRoundedRect(card, 12, 12)

        x = card.left() + 16
        y = card.top() + 12
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ACCENT)
        p.drawRoundedRect(QRectF(card.left() + 5, y + 2, 2, 14), 1, 1)

        head_font = QFont(FONT_FAMILY, 12)
        head_font.setBold(True)
        p.setFont(head_font)
        p.setPen(TXT)
        hh = QFontMetrics(head_font).boundingRect(
            QRect(0, 0, inner, 4000), int(Qt.TextFlag.TextWordWrap), self._say or " "
        )
        p.drawText(
            QRectF(x, y, inner, hh.height() + 4),
            int(Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignLeft),
            self._say,
        )
        if self._detail:
            p.setFont(QFont(FONT_FAMILY, 10))
            p.setPen(SUB)
            detail_y = y + hh.height() + 6
            p.drawText(QRectF(x, detail_y, inner, card.bottom() - detail_y - 10),
                       int(Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignLeft), self._detail)

    def _paint_thinking(self, p: QPainter) -> None:
        card, inner = self._card_rect(300)
        card.moveTo((self.width() - card.width()) / 2, self.height() - card.height() - 90)
        p.setPen(QPen(CARD_LINE, 1))
        p.setBrush(CARD_BG)
        p.drawRoundedRect(card, 14, 14)

        # 转圈
        cx, cy, r = card.left() + 30, card.center().y(), 9
        pen = QPen(QColor(ACCENT.red(), ACCENT.green(), ACCENT.blue(), 90), 3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(cx - r, cy - r, r * 2, r * 2), 0, 360 * 16)
        pen.setColor(ACCENT)
        p.setPen(pen)
        start = int(math.degrees(self._phase * 3) % 360)
        p.drawArc(QRectF(cx - r, cy - r, r * 2, r * 2), int(start * 16), int(100 * 16))

        f = QFont(FONT_FAMILY, 11)
        p.setFont(f)
        p.setPen(TXT)
        p.drawText(
            QRectF(cx + r + 12, card.top(), card.width() - r - 30, card.height()),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            self._say,
        )

    def _paint_hint(self, p: QPainter) -> None:
        f = QFont(FONT_FAMILY, 11)
        p.setFont(f)
        fm = QFontMetrics(f)
        tw = min(self.width() - 80, fm.horizontalAdvance(self._say) + 68)
        h = fm.height() + 30
        box = QRectF((self.width() - tw) / 2, self.height() - h - 110, tw, h)
        p.setPen(QPen(CARD_LINE, 1))
        p.setBrush(CARD_BG)
        p.drawRoundedRect(box, 20, 20)
        p.setPen(QPen(WARM, 3))
        p.drawLine(
            QPointF(box.left() + 22, box.center().y() - 7),
            QPointF(box.left() + 22, box.center().y() + 7),
        )
        p.setPen(TXT)
        p.drawText(
            QRectF(box.left() + 38, box.top(), box.width() - 52, box.height()),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            self._say,
        )
