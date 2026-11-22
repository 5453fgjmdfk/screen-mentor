"""Frameless companion with a speech bubble and hover controls."""
from PyQt6.QtCore import QElapsedTimer, QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QLinearGradient, QPainter,
    QPainterPath, QPainterPathStroker, QPen, QTransform,
)
from PyQt6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QWidget

from appearance import AppearanceDialog, BUILT_INS, PeekFrames, SkinLibrary, paint_peek
from skin_assets import STATES

SNAP_DISTANCE = 22
RESTORE_GAP = 30


def tinted(hex_color, alpha):
    color = QColor(hex_color)
    color.setAlpha(alpha)
    return color


def css_fill(hex_color, alpha):
    color = QColor(hex_color)
    return f"rgba({color.red()},{color.green()},{color.blue()},{alpha})"


def fit_position(position, size, area):
    return QPoint(
        min(max(position.x(), area.left()), max(area.left(), area.right() - size.width() + 1)),
        min(max(position.y(), area.top()), max(area.top(), area.bottom() - size.height() + 1)),
    )


class CompanionAction(QPushButton):
    labels = {"pause": "暂停", "resume": "继续", "help": "答疑", "stop": "停止", "appearance": "外观",
              "chat": "日常对话"}

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
        elif self.action == "chat":
            mark.addRoundedRect(QRectF(0, 0, 12, 9), 2.3, 2.3)
            tail = QPainterPath()
            tail.moveTo(3, 8)
            tail.lineTo(3, 12)
            tail.lineTo(7, 8)
            tail.closeSubpath()
            mark = mark.united(tail)
        elif self.action == "appearance":
            mark.moveTo(4.3, 0)
            mark.quadTo(6, 2.5, 7.7, 0)
            mark.lineTo(11.8, 2.2)
            mark.lineTo(10.7, 5.6)
            mark.lineTo(9.2, 5)
            mark.lineTo(9.2, 12)
            mark.lineTo(2.8, 12)
            mark.lineTo(2.8, 5)
            mark.lineTo(1.3, 5.6)
            mark.lineTo(.2, 2.2)
            mark.closeSubpath()
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


class EdgeThought(QWidget):
    trail = 17
    padding = 10
    text_flags = int(Qt.TextFlag.TextWordWrap | Qt.TextFlag.TextWrapAnywhere)

    def __init__(self, peek):
        super().__init__(peek, flags=Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus
                         | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        font = QFont("Microsoft YaHei UI")
        font.setPixelSize(13)
        self.setFont(font)
        self.text = ""
        self.body_size = QSize()
        self.skin_colors = BUILT_INS[0].palette

    def set_text(self, text):
        text = " ".join(text.split())
        if text == self.text:
            return
        self.text = text
        self.setAccessibleName(text)
        metrics = QFontMetrics(self.font())
        width = min(198, max(54, metrics.horizontalAdvance(text)))
        measure = QRect(0, 0, width, 32767)
        limit = metrics.height() + 2 * metrics.lineSpacing()

        def height(value):
            return metrics.boundingRect(measure, self.text_flags, value).height()

        self.visible_text = text
        if height(text) > limit:
            start, end = 0, len(text)
            while start < end:
                middle = (start + end + 1) // 2
                if height(text[:middle].rstrip() + "…") <= limit:
                    start = middle
                else:
                    end = middle - 1
            self.visible_text = text[:start].rstrip() + "…"
        self.body_size = QSize(width + 2 * self.padding, height(self.visible_text) + 16)
        self.update()

    def follow(self, pet, edge, area):
        self.edge = edge
        width, height = self.body_size.width(), self.body_size.height()
        if edge == Qt.Edge.BottomEdge:
            size = QSize(width, height + self.trail)
            anchor = QPoint(pet.center().x(), pet.top() + 3)
            origin = anchor - QPoint(width // 2, size.height())
        else:
            size = QSize(width + self.trail, height)
            anchor = QPoint(pet.right() - 4 if edge == Qt.Edge.LeftEdge else pet.left() + 4, pet.top() + 25)
            origin = anchor - QPoint(0 if edge == Qt.Edge.LeftEdge else size.width(), height * 3 // 4)
        position = fit_position(origin, size, area)
        self.dot_axis = anchor - position
        self.setGeometry(QRect(position, size))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = QRectF(1, 1, self.body_size.width() - 2, self.body_size.height() - 2)
        if self.edge == Qt.Edge.LeftEdge:
            body.translate(self.trail, 0)
        palette = self.skin_colors
        painter.setPen(QPen(tinted(palette.border, 220), 1))
        painter.setBrush(tinted(palette.background, 231))
        painter.drawRoundedRect(body, 12, 12)
        if self.edge == Qt.Edge.BottomEdge:
            x = min(max(12, self.dot_axis.x()), self.width() - 12)
            dots = ((QPointF(x + 3, self.height() - 3), 1.8),
                    (QPointF(x, self.height() - 10), 3))
        else:
            y = min(max(10, self.dot_axis.y()), self.height() - 10)
            left = self.edge == Qt.Edge.LeftEdge
            dots = ((QPointF(3 if left else self.width() - 3, y), 1.8),
                    (QPointF(10 if left else self.width() - 10, y - 3), 3))
        painter.setBrush(tinted(palette.surface, 230))
        for center, radius in dots:
            painter.drawEllipse(center, radius, radius)
        painter.setPen(QColor(palette.text))
        painter.drawText(body.adjusted(self.padding - 1, 7, 1 - self.padding, -7),
                         self.text_flags, self.visible_text)


class EdgePeek(QWidget):
    restore_requested = pyqtSignal(object)

    def __init__(self):
        super().__init__(flags=Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAccessibleName("展开屏幕伴学")
        self.setToolTip("点击或向屏幕内拖动，展开伴学")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edge = None
        self.press_at = None
        self.frames = {}
        self.art_path = None
        self.thought = EdgeThought(self)
        self.clock = QElapsedTimer()
        self.animation = QTimer(self)
        self.animation.setInterval(33)
        self.animation.timeout.connect(self.animate)

    def set_skin(self, skin):
        if self.thought.skin_colors != skin.palette:
            self.thought.skin_colors = skin.palette
            self.thought.update()
        if self.art_path != skin.path:
            for frames in self.frames.values():
                frames.dispose()
            self.frames = {bottom: PeekFrames(skin, bottom, self) for bottom in (False, True)}
            self.art_path = skin.path
            if self.isVisible():
                self.frames[self.edge == Qt.Edge.BottomEdge].start()
        self.update()

    def dock(self, edge, anchor, area):
        self.edge = edge
        self.area = QRect(area)
        if edge == Qt.Edge.BottomEdge:
            self.setFixedSize(84, 68)
        else:
            self.setFixedSize(64, 84)
        self.move(self.edge_position(anchor - QPoint(self.width() // 2, self.height() // 2)))
        self.clock.start()
        self.frames[edge == Qt.Edge.BottomEdge].start()
        self.show()
        self.animation.start()
        self.animate()

    def edge_position(self, position):
        position = fit_position(position, self.size(), self.area)
        if self.edge == Qt.Edge.LeftEdge:
            position.setX(self.area.left())
        elif self.edge == Qt.Edge.RightEdge:
            position.setX(self.area.right() - self.width() + 1)
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
        return super().event(event)

    def set_thought(self, text):
        self.thought.set_text(text)
        if self.isVisible():
            self.follow_thought()

    def animate(self):
        self.update()
        if not self.thought.isVisible() and self.thought.text and self.clock.elapsed() >= 200:
            self.follow_thought()

    def follow_thought(self):
        if self.thought.text and self.clock.elapsed() >= 200:
            self.thought.follow(self.geometry(), self.edge, self.area)
            self.thought.show()
        else:
            self.thought.hide()

    def moveEvent(self, event):
        super().moveEvent(event)
        if self.thought.isVisible():
            self.follow_thought()

    def hideEvent(self, event):
        self.animation.stop()
        self.frames[self.edge == Qt.Edge.BottomEdge].stop()
        self.thought.hide()
        self.reset_press()
        super().hideEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        bottom = self.edge == Qt.Edge.BottomEdge
        elapsed = self.clock.elapsed() / 1000
        pose = self.frames[bottom].current(elapsed)
        if pose.isNull():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(tinted(self.thought.skin_colors.background, 230))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(3, 3, -3, -3), 12, 12)
            painter.setPen(QColor(self.thought.skin_colors.text))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "学")
            return

        paint_peek(painter, self.width(), self.height(), pose, bottom, elapsed,
                   mirror=self.edge == Qt.Edge.RightEdge)


class CompanionPanel(QWidget):
    closed = pyqtSignal()

    def __init__(self, *, skin_key=None, sprite_size=138):
        super().__init__(flags=Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("屏幕伴学 · 当前对话")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        self.sprite = QLabel()
        self.sprite.setAccessibleName("小龙学伴")
        self.sprite.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sprite.setFixedSize(sprite_size, sprite_size)
        self.drag_handles = set()
        self.drag_handle = None
        self.drag_offset = None
        self.drag_start = None
        self.peek = EdgePeek()
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

        self.anims = {}
        self.state = None
        self.ready_timer = QTimer(self)
        self.ready_timer.setSingleShot(True)
        self.ready_timer.setInterval(2650)
        self.ready_timer.timeout.connect(lambda: self.set_pose("idle"))
        self.sprite.setToolTip("拖到屏幕两侧或底部，进入探头状态")
        self.add_drag_handle(self.sprite)
        self.library = SkinLibrary()
        self.skin_key = skin_key
        self.skin = None
        self.appearance_dialog = None
        self.refresh_skin()
        self.skin_timer = QTimer(self)
        self.skin_timer.setInterval(1500)
        self.skin_timer.timeout.connect(self.refresh_skin)
        self.skin_timer.start()

    def refresh_skin(self):
        if self.skin_key is None:
            skin = self.library.current()
        else:
            skin = next((item for item in self.library.all() if item.key == self.skin_key), BUILT_INS[0])
        if skin == self.skin:
            return
        previous_skin = self.skin
        self.skin = skin
        palette = skin.palette
        self.setStyleSheet(
            f"QWidget{{color:{palette.text};font:13px 'Microsoft YaHei UI';}}"
            f"QFrame#companionBubble{{background:{css_fill(palette.background, 239)};"
            f"border:1px solid {palette.border};border-radius:14px;}}"
            f"QLabel#companionProgress{{color:{palette.highlight};}}"
            f"QLabel#companionTail{{color:{palette.surface};font-size:13px;}}"
            f"QPlainTextEdit#companionInput{{background:{palette.surface};padding:4px;border-radius:7px;}}"
            f"QPushButton{{color:{palette.text};background:{css_fill(palette.surface, 219)};"
            f"border:1px solid {palette.border};border-radius:10px;padding:5px 10px;}}"
            f"QPushButton:hover{{background:{palette.border};}}"
        )
        if previous_skin is None or previous_skin.path != skin.path:
            pose = self.state or "idle"
            self.sprite.clear()
            for movie in self.anims.values():
                movie.stop()
                movie.deleteLater()
            self.anims = {state: skin.movie(state, self, self.sprite.size()) for state in STATES}
            self.state = None
            self.set_pose(pose)
        self.peek.set_skin(skin)

    def open_appearance(self):
        if self.appearance_dialog is None:
            dialog = AppearanceDialog(self if self.isVisible() else None, self.library, self.library.current())
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dialog.accepted.connect(self.refresh_skin)
            dialog.finished.connect(self.forget_appearance)
            self.appearance_dialog = dialog
        self.appearance_dialog.show_centered()

    def forget_appearance(self):
        self.appearance_dialog = None

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
            else:
                position.setY(area.bottom() - self.height() + 1 - RESTORE_GAP)
        self.move(fit_position(position, self.size(), area))
        self.peek.hide()
        self.show()

    def hide_controls_if_outside(self):
        at = QCursor.pos()
        if not any(widget.isVisible() and widget.rect().contains(widget.mapFromGlobal(at))
                   for widget in (self.sprite, self.controls)):
            self.controls.hide()

    def closeEvent(self, event):
        super().closeEvent(event)
        if event.isAccepted():
            self.peek.close()
            self.skin_timer.stop()
            if self.appearance_dialog is not None:
                self.appearance_dialog.close()
            self.ready_timer.stop()
            for movie in self.anims.values():
                movie.stop()
            self.closed.emit()
