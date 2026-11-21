"""Frameless companion with a speech bubble and hover controls."""
from pathlib import Path

from PyQt6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QCursor, QGuiApplication, QLinearGradient, QMovie, QPainter, QPainterPath, QPainterPathStroker, QPen,
    QTransform,
)
from PyQt6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QWidget


ART = Path(__file__).resolve().parent.parent / "assets" / "companion"
SNAP_DISTANCE = 22
RESTORE_GAP = 30


def fit_position(position, size, area):
    return QPoint(
        min(max(position.x(), area.left()), max(area.left(), area.right() - size.width() + 1)),
        min(max(position.y(), area.top()), max(area.top(), area.bottom() - size.height() + 1)),
    )


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


class EdgePeek(QWidget):
    restore_requested = pyqtSignal(object)

    def __init__(self, sprite):
        super().__init__(flags=Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAccessibleName("展开屏幕伴学")
        self.setToolTip("点击或向屏幕内拖动，展开伴学")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sprite = sprite
        self.edge = None
        self.press_at = None

    def dock(self, edge, anchor, area):
        self.edge = edge
        self.area = QRect(area)
        if edge in (Qt.Edge.LeftEdge, Qt.Edge.RightEdge):
            self.setFixedSize(38, 62)
        else:
            self.setFixedSize(62, 38)
        self.move(self.edge_position(anchor - QPoint(self.width() // 2, self.height() // 2)))
        self.show()
        self.update()

    def edge_position(self, position):
        position = fit_position(position, self.size(), self.area)
        if self.edge == Qt.Edge.LeftEdge:
            position.setX(self.area.left())
        elif self.edge == Qt.Edge.RightEdge:
            position.setX(self.area.right() - self.width() + 1)
        elif self.edge == Qt.Edge.TopEdge:
            position.setY(self.area.top())
        else:
            position.setY(self.area.bottom() - self.height() + 1)
        return position

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self.press_at = event.globalPosition().toPoint()
        self.press_position = self.pos()
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self.press_at is not None and event.buttons() & Qt.MouseButton.LeftButton:
            delta = event.globalPosition().toPoint() - self.press_at
            if delta.manhattanLength() >= QApplication.startDragDistance():
                self.move(self.edge_position(self.press_position + delta))

    def mouseReleaseEvent(self, event):
        if self.press_at is None or event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        cursor = event.globalPosition().toPoint()
        delta = cursor - self.press_at
        self.reset_press()
        if delta.manhattanLength() < QApplication.startDragDistance():
            self.restore_requested.emit(None)
            return
        inward = {
            Qt.Edge.LeftEdge: delta.x(),
            Qt.Edge.RightEdge: -delta.x(),
            Qt.Edge.TopEdge: delta.y(),
            Qt.Edge.BottomEdge: -delta.y(),
        }[self.edge]
        if inward >= max(14, QApplication.startDragDistance()):
            self.restore_requested.emit(cursor)
        else:
            self.move(self.edge_position(self.press_position + delta))

    def reset_press(self):
        self.press_at = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def event(self, event):
        if event.type() == QEvent.Type.UngrabMouse:
            self.reset_press()
        elif event.type() in (QEvent.Type.Enter, QEvent.Type.Leave):
            self.update()
        return super().event(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        bounds = QRectF(self.rect()).adjusted(.75, .75, -.75, -.75)
        if self.edge == Qt.Edge.LeftEdge:
            bounds.adjust(-13, 0, 0, 0)
            portrait = QRectF(-31, 0, 82, 82)
            grip = QRectF(32, 23, 2, 16)
        elif self.edge == Qt.Edge.RightEdge:
            bounds.adjust(0, 0, 13, 0)
            portrait = QRectF(-14, 0, 82, 82)
            grip = QRectF(4, 23, 2, 16)
        elif self.edge == Qt.Edge.TopEdge:
            bounds.adjust(0, -13, 0, 0)
            portrait = QRectF(-10, -25, 82, 82)
            grip = QRectF(23, 32, 16, 2)
        else:
            bounds.adjust(0, 0, 0, 13)
            portrait = QRectF(-10, -8, 82, 82)
            grip = QRectF(23, 4, 16, 2)

        active = self.underMouse() or self.press_at is not None
        tab = QPainterPath()
        tab.addRoundedRect(bounds, 12, 12)
        painter.fillPath(tab, QColor(53, 40, 80, 232) if active else QColor(39, 30, 60, 214))
        painter.save()
        painter.setClipPath(tab)
        movie = self.sprite.movie()
        frame = movie.currentPixmap() if movie is not None else self.sprite.pixmap()
        if frame is not None and not frame.isNull():
            painter.drawPixmap(portrait, frame, QRectF(frame.rect()))
        else:
            painter.setPen(QColor("#f2edff"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "学")
        painter.restore()
        painter.setPen(QPen(QColor(211, 190, 253, 224 if active else 156), 1))
        painter.drawPath(tab)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(227, 212, 255, 240 if active else 180))
        painter.drawRoundedRect(grip, 1, 1)


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
        self.sprite.setAccessibleName("小龙学伴")
        self.sprite.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sprite.setFixedSize(138, 138)
        self.drag_handles = set()
        self.drag_handle = None
        self.drag_offset = None
        self.drag_start = None
        self.peek = EdgePeek(self.sprite)
        self.peek.restore_requested.connect(self.restore_from_edge)
        self.destroyed.connect(self.peek.deleteLater)
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
            movie.frameChanged.connect(self.refresh_peek)
        self.state = None
        self.ready_timer = QTimer(self)
        self.ready_timer.setSingleShot(True)
        self.ready_timer.setInterval(2650)
        self.ready_timer.timeout.connect(lambda: self.set_pose("idle"))
        self.sprite.setToolTip("拖到屏幕边缘可收起伴学")
        self.add_drag_handle(self.sprite)

    def set_pose(self, state):
        if self.state == state:
            if state == "ready":
                self.ready_timer.start()
            return
        movie = self.anims[state]
        if not movie.isValid():
            movie = self.anims["idle"]
            if not movie.isValid():
                self.sprite.setText("学伴")
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
            self.drag_start = event.globalPosition().toPoint()
            self.drag_offset = self.drag_start - self.pos()
            handle.setCursor(Qt.CursorShape.ClosedHandCursor)
            return True
        if self.drag_handle is not handle:
            return False
        if kind == QEvent.Type.MouseMove and event.buttons() & Qt.MouseButton.LeftButton:
            cursor = event.globalPosition().toPoint()
            position = cursor - self.drag_offset
            screen = QGuiApplication.screenAt(cursor) or self.screen()
            self.move(fit_position(position, self.size(), screen.availableGeometry()))
            return True
        released = kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton
        if released or kind == QEvent.Type.UngrabMouse:
            cursor = event.globalPosition().toPoint() if released else self.drag_start
            dragged = (cursor - self.drag_start).manhattanLength() >= QApplication.startDragDistance()
            position = cursor - self.drag_offset
            self.drag_handle = None
            self.drag_offset = None
            self.drag_start = None
            handle.setCursor(Qt.CursorShape.OpenHandCursor)
            if released and dragged:
                screen = QGuiApplication.screenAt(cursor) or self.screen()
                area = screen.availableGeometry()
                self.move(fit_position(position, self.size(), area))
                self.snap_to_edge(area)
            return released
        return False

    def snap_to_edge(self, area):
        bounds = self.geometry()
        distances = {
            Qt.Edge.LeftEdge: abs(bounds.left() - area.left()),
            Qt.Edge.RightEdge: abs(area.right() - bounds.right()),
            Qt.Edge.TopEdge: abs(bounds.top() - area.top()),
            Qt.Edge.BottomEdge: abs(area.bottom() - bounds.bottom()),
        }
        edge = min(distances, key=distances.get)
        if distances[edge] <= SNAP_DISTANCE:
            self.peek.dock(edge, self.sprite.mapToGlobal(self.sprite.rect().center()), area)
            self.hover_timer.stop()
            self.controls.hide()
            self.hide()

    def restore_from_edge(self, cursor):
        anchor = self.peek.geometry().center() if cursor is None else cursor
        screen = QGuiApplication.screenAt(anchor) or self.peek.screen()
        area = screen.availableGeometry()
        self.adjustSize()
        sprite_center = self.sprite.mapTo(self, self.sprite.rect().center())
        position = anchor - sprite_center
        if cursor is None:
            if self.peek.edge == Qt.Edge.LeftEdge:
                position.setX(area.left() + RESTORE_GAP)
            elif self.peek.edge == Qt.Edge.RightEdge:
                position.setX(area.right() - self.width() + 1 - RESTORE_GAP)
            elif self.peek.edge == Qt.Edge.TopEdge:
                position.setY(area.top() + RESTORE_GAP)
            else:
                position.setY(area.bottom() - self.height() + 1 - RESTORE_GAP)
        self.move(fit_position(position, self.size(), area))
        self.peek.hide()
        self.show()

    def refresh_peek(self):
        if self.peek.isVisible():
            self.peek.update()

    def hide_controls_if_outside(self):
        at = QCursor.pos()
        if not any(widget.isVisible() and widget.rect().contains(widget.mapFromGlobal(at))
                   for widget in (self.sprite, self.controls)):
            self.controls.hide()

    def closeEvent(self, event):
        super().closeEvent(event)
        if event.isAccepted():
            self.peek.close()
            self.ready_timer.stop()
            for movie in self.anims.values():
                movie.stop()
            self.closed.emit()
