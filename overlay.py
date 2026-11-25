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

ACCENT = QColor(162, 131, 231)
WARM = QColor(233, 171, 215)
CARD_BG = QColor(37, 29, 59, 239)
CARD_LINE = QColor(183, 154, 236, 125)
TXT = QColor(249, 243, 255)
SUB = QColor(205, 190, 229)

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
        pw, ph = max(1, pw), max(1, ph)
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
            self._reveal(0)

    def hideEvent(self, event) -> None:
        self._anim.stop()
        super().hideEvent(event)

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
        if self.has_guide and self._rect is not None:
            region = (self._rect.united(QRectF(self._drag_to.x(), self._drag_to.y(), 1, 1))
                      if self._drag_to else self._rect)
            self.update(region.adjusted(-30, -30, 56, 56).toAlignedRect())
        elif self._mode:
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
        badge_extra = 28 if self._mode == "guide" and self._num > 0 else 0
        preferred = max(120, metrics.horizontalAdvance(self._say) + 40 + badge_extra)
        if self._detail:
            w = max(w, 420)
            preferred = max(preferred, min(w, body_metrics.horizontalAdvance(self._detail) + 32))
        cw = max(100, min(w, self.width() - 32, preferred))
        inner = max(40, cw - 32)
        head_inner = max(40, inner - badge_extra)
        hh = QFontMetrics(head_font).boundingRect(
            QRect(0, 0, head_inner, 4000),
            int(Qt.TextFlag.TextWordWrap),
            self._say or " ",
        ).height()
        if badge_extra:
            hh = max(hh, 22)
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

        # 卡片位置：优先放目标上方，放不下放下方；仍遮挡目标时放左右两侧
        if target is not None:
            occupied = target.united(QRectF(self._drag_to.x() - 8, self._drag_to.y() - 8, 16, 16)) if self._drag_to else target
            cx = min(max(center.x() - card.width() / 2, 16), max(16.0, self.width() - card.width() - 16))
            above_y = occupied.top() - card.height() - 16
            below_y = occupied.bottom() + 16
            if above_y >= 16:
                card.moveTo(cx, above_y)
            elif below_y + card.height() <= self.height() - 16:
                card.moveTo(cx, below_y)
            else:
                cy = min(max(center.y() - card.height() / 2, 16), max(16.0, self.height() - card.height() - 16))
                right_x = occupied.right() + 16
                left_x = occupied.left() - card.width() - 16
                if right_x + card.width() <= self.width() - 16:
                    card.moveTo(right_x, cy)
                elif left_x >= 16:
                    card.moveTo(left_x, cy)
                else:
                    card.moveTo(cx, min(below_y, max(16.0, self.height() - card.height() - 16)))
        else:
            where = self._hint_at or QPointF(self.width() / 2, self.height() * .66)
            card.moveTo(min(max(where.x() - card.width() / 2, 16), max(16.0, self.width() - card.width() - 16)),
                        min(max(where.y(), 16), max(16.0, self.height() - card.height() - 16)))

        if target is not None:
            self._paint_target(p, target)
            if self._drag_to is not None:
                p.setPen(QPen(ACCENT.lighter(140), 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(self._drag_to, 8, 8)
                self._paint_mouse_demo(p, center, self._drag_to)
            elif self._mark_target:
                self._paint_mouse_demo(p, center)

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

    def _paint_target(self, p: QPainter, target: QRectF) -> None:
        radius = min(4.0, target.width() / 4, target.height() / 4)
        p.setPen(QPen(QColor(15, 24, 40, 165), 3.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(target, radius, radius)
        outline = QPen(ACCENT.lighter(130), 1.7)
        outline.setDashPattern([7, 5])
        p.setPen(outline)
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

        text_x = x
        head_inner = inner
        if self._mode == "guide" and self._num > 0:
            badge = QRectF(x, y + 1, 21, 21)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(ACCENT)
            p.drawEllipse(badge)
            badge_font = QFont(FONT_FAMILY, 9)
            badge_font.setBold(True)
            p.setFont(badge_font)
            p.setPen(QColor(25, 18, 42))
            p.drawText(badge, int(Qt.AlignmentFlag.AlignCenter), str(self._num))
            text_x += 28
            head_inner = max(40, inner - 28)

        head_font = QFont(FONT_FAMILY, 12)
        head_font.setBold(True)
        p.setFont(head_font)
        p.setPen(TXT)
        hh = QFontMetrics(head_font).boundingRect(
            QRect(0, 0, head_inner, 4000), int(Qt.TextFlag.TextWordWrap), self._say or " "
        )
        head_h = max(hh.height(), 22 if self._mode == "guide" and self._num > 0 else 0)
        p.drawText(
            QRectF(text_x, y, head_inner, head_h + 4),
            int(Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignLeft),
            self._say,
        )
        if self._detail:
            p.setFont(QFont(FONT_FAMILY, 10))
            p.setPen(SUB)
            detail_y = y + head_h + 6
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
