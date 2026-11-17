"""Frameless companion with a speech bubble and hover controls."""
from pathlib import Path

from PyQt6.QtCore import QEvent, QPoint, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QCursor, QGuiApplication, QLinearGradient, QMovie, QPainter, QPainterPath, QPainterPathStroker, QPen,
    QTransform,
)
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget


ART = Path(__file__).resolve().parent.parent / "assets" / "companion"


class CompanionAction(QPushButton):
    labels = {"pause": "暂停", "resume": "继续", "help": "答疑", "stop": "停止"}

    def __init__(self, action):
        super().__init__()
        self.setFixedSize(34, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            "QPushButton,QPushButton:hover,QPushButton:focus,QPushButton:pressed"
            "{background:transparent;border:0;padding:0;}"
        )
        self.set_action(action)

    def set_action(self, action):
        self.action = action
        self.setAccessibleName(self.labels[action])
        self.setToolTip(self.labels[action])
        self.update()

    def glyph(self):
        mark = QPainterPath()
        if self.action == "pause":
            for left in (0, 6.5):
                mark.addRoundedRect(QRectF(left, 0, 3.4, 12), 1.7, 1.7)
        elif self.action == "resume":
            mark.moveTo(0, 0)
            mark.lineTo(10, 6)
            mark.lineTo(0, 12)
            mark.closeSubpath()
        elif self.action == "help":
            curve = QPainterPath()
            curve.moveTo(0, 3.2)
            curve.cubicTo(.55, .2, 6.1, -.45, 6.4, 2.95)
            curve.cubicTo(6.65, 5.2, 3.4, 5.15, 3.35, 7.35)
            stroke = QPainterPathStroker()
            stroke.setWidth(2.25)
            stroke.setCapStyle(Qt.PenCapStyle.RoundCap)
            mark = stroke.createStroke(curve)
            mark.addEllipse(QRectF(2.05, 9.5, 2.6, 2.6))
        else:
            mark.addRoundedRect(QRectF(0, 0, 12, 12), 1.6, 1.6)
        scale = 12 / mark.boundingRect().height()
        mark = QTransform.fromScale(scale, scale).map(mark)
        center = mark.boundingRect().center()
        mark.translate(self.width() / 2 - center.x(), self.height() / 2 - center.y())
        return mark

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        mark = self.glyph()
        bounds = mark.boundingRect()
        active = self.underMouse() or self.hasFocus()
        painter.fillPath(mark.translated(0, 0.65), QColor(58, 42, 87, 105))
        frost = QLinearGradient(bounds.topLeft(), bounds.bottomLeft())
        frost.setColorAt(0, QColor(255, 252, 255, 244 if active else 232))
        frost.setColorAt(.45, QColor(237, 226, 255, 205 if active else 157))
        frost.setColorAt(1, QColor(219, 204, 245, 221 if active else 191))
        painter.setPen(QPen(QColor(252, 244, 255, 225 if active else 190), .65))
        painter.setBrush(QBrush(frost))
        painter.drawPath(mark)


class CompanionPanel(QWidget):
    closed = pyqtSignal()

    def __init__(self):
        super().__init__(flags=Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("屏幕伴学 · 当前对话")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        self.sprite = QLabel()
        self.sprite.setAccessibleName("龙娘学伴")
        self.sprite.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sprite.setFixedSize(138, 138)
        self.drag_handles = set()
        self.drag_handle = None
        self.drag_offset = None
        self.add_drag_handle(self.sprite)
        self.controls = QWidget()
        self.controls.setObjectName("companionControls")
        layout = QHBoxLayout(self.controls)
        layout.setSpacing(6)
        layout.setContentsMargins(0, 0, 0, 0)
        policy = self.controls.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.controls.setSizePolicy(policy)
        self.controls.hide()

        self.hover_timer = QTimer(self)
        self.hover_timer.setSingleShot(True)
        self.hover_timer.setInterval(220)
        self.hover_timer.timeout.connect(self.hide_controls_if_outside)
        self.controls.installEventFilter(self)

        self.anims = {state: QMovie(str(ART / f"{state}.gif"), parent=self)
                      for state in ("idle", "reading", "thinking", "ready")}
        for movie in self.anims.values():
            movie.setScaledSize(QSize(138, 138))
        self.state = None
        self.ready_timer = QTimer(self)
        self.ready_timer.setSingleShot(True)
        self.ready_timer.setInterval(2650)
        self.ready_timer.timeout.connect(lambda: self.set_pose("idle"))

    def set_pose(self, state):
        movie = self.anims[state]
        if not movie.isValid():
            state = "idle"
            movie = self.anims[state]
            if not movie.isValid():
                self.sprite.setText("学伴")
                self.sprite.setStyleSheet(
                    "color:#f5edff;background:#30264b;border:1px solid #937ab5;"
                    "border-radius:69px;font-size:17px;font-weight:600;"
                )
                self.state = "idle"
                return
        if self.state == state:
            if state == "ready":
                self.ready_timer.start()
            return
        self.ready_timer.stop()
        if self.state is not None:
            self.anims[self.state].stop()
        self.state = state
        self.sprite.setMovie(movie)
        movie.jumpToFrame(0)
        movie.start()
        if state == "ready":
            self.ready_timer.start()

    def eventFilter(self, target, event):
        if target in self.drag_handles and self.drag_window(target, event):
            return True
        if target in (self.sprite, self.controls):
            if event.type() == QEvent.Type.Enter:
                self.hover_timer.stop()
                self.controls.show()
            elif event.type() == QEvent.Type.Leave:
                self.hover_timer.start()
        return super().eventFilter(target, event)

    def add_drag_handle(self, handle):
        self.drag_handles.add(handle)
        handle.setCursor(Qt.CursorShape.OpenHandCursor)
        handle.installEventFilter(self)

    def drag_window(self, handle, event):
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self.drag_handle = handle
            self.drag_offset = event.globalPosition().toPoint() - self.pos()
            handle.setCursor(Qt.CursorShape.ClosedHandCursor)
            return True
        if self.drag_handle is not handle:
            return False
        if kind == QEvent.Type.MouseMove and event.buttons() & Qt.MouseButton.LeftButton:
            cursor = event.globalPosition().toPoint()
            position = cursor - self.drag_offset
            screen = QGuiApplication.screenAt(cursor) or self.screen()
            area = screen.availableGeometry()
            x = min(max(position.x(), area.left()), max(area.left(), area.right() - self.width() + 1))
            y = min(max(position.y(), area.top()), max(area.top(), area.bottom() - self.height() + 1))
            self.move(QPoint(x, y))
            return True
        if kind in (QEvent.Type.MouseButtonRelease, QEvent.Type.UngrabMouse):
            self.drag_handle = None
            self.drag_offset = None
            handle.setCursor(Qt.CursorShape.OpenHandCursor)
        return False

    def hide_controls_if_outside(self):
        at = QCursor.pos()
        if not any(widget.isVisible() and widget.rect().contains(widget.mapFromGlobal(at))
                   for widget in (self.sprite, self.controls)):
            self.controls.hide()

    def closeEvent(self, event):
        super().closeEvent(event)
        if event.isAccepted():
            self.ready_timer.stop()
            for movie in self.anims.values():
                movie.stop()
            self.closed.emit()
