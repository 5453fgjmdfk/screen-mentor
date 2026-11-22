"""Observe practice changes while the originating Codex chat is using the tool."""
from collections import deque
from io import BytesIO
from pathlib import PureWindowsPath
from threading import Event, Thread
import time

from PIL import Image, ImageChops, ImageStat
from PyQt6.QtCore import QBuffer, QIODevice, QObject, QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFontMetrics, QIcon, QImage, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

import native
from companion import CompanionAction, CompanionPanel
from window_reader import WindowReader


CHAT_LEASE_SECONDS = 120
foreground = native.bind(native.user, "GetForegroundWindow", native.w.HWND)
owner_of = native.bind(native.user, "GetWindow", native.w.HWND, native.w.HWND, native.w.UINT)


class WindowScope:
    """Follow approved apps across replacement windows and process restarts."""

    shared_hosts = {"explorer.exe", "applicationframehost.exe", "chrome.exe", "msedge.exe",
                    "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe"}

    def __init__(self, selected):
        self.roots = {}
        for hwnd in selected:
            pid = native.window_state(hwnd)[4]
            self.roots[hwnd] = pid, native.executable_for(pid)
        self.apps = {exe for _, exe in self.roots.values()
                     if exe and PureWindowsPath(exe).name not in self.shared_hosts}

    def accepts(self, hwnd):
        owner = hwnd
        while owner:
            pid = native.window_pid(owner)
            if owner in self.roots and pid == self.roots[owner][0]:
                return True
            if pid and self.apps and native.executable_for(pid) in self.apps:
                return True
            owner = owner_of(owner, 4)  # GW_OWNER
        return False

    def active(self):
        hwnd = foreground()
        return hwnd if self.accepts(hwnd) else None


def changed(a, b):
    if a is None or b is None or a[:2] != b[:2]:
        return a is not b
    diff = ImageChops.difference(a[3], b[3])
    return ImageStat.Stat(diff).mean[0] >= 1.6 or sum(diff.histogram()[45:]) >= 80


class FeedbackEditor(QPlainTextEdit):
    image_pasted = pyqtSignal(QImage)

    def insertFromMimeData(self, source):
        if source.hasImage():
            data = source.imageData()
            image = data.toImage() if isinstance(data, QPixmap) else data
            if isinstance(image, QImage) and not image.isNull():
                self.image_pasted.emit(image)
                return
        super().insertFromMimeData(source)


class Practice(QObject):
    sampled = pyqtSignal(int, object)
    capture_failed = pyqtSignal(int, str)
    wake = pyqtSignal()
    idle_chat = pyqtSignal()
    appearance_requested = pyqtSignal()
    session_started = pyqtSignal()
    session_ended = pyqtSignal()

    def __init__(self, scope, app, *, persistent=False):
        super().__init__()
        self.persistent = persistent
        self.scope = None
        self.running = Event()
        self.capture_generation = 0
        self.session_serial = 0
        self.ending = Event()
        self.ending.set()
        self.shutdown = Event()
        self.reset_capture()
        self.progress_since = None
        self.progress_text = ""
        self.progress_label = ""

        self.setup_panel(app)
        if persistent:
            self.set_idle()
        else:
            self.begin(scope)
        self.sampled.connect(self.accept_sample)
        self.capture_failed.connect(self.note_capture_failure)
        Thread(target=self.watch, daemon=True).start()

    def reset_capture(self):
        self.last_poll = time.monotonic()
        self.chat_suspended = False
        self.pending = self.last_sent = self.current = None
        self.stable_frame = None
        self.issues = deque()
        self.last_image = None
        self.force = False
        self.recheck_reason = None
        self.last_sample = None
        self.capture_error = None
        self.sample_after = 0.0

    def setup_panel(self, app):
        self.panel = CompanionPanel()
        self.panel.closed.connect(self.on_panel_closed)
        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(1)
        bubble = QFrame()
        bubble.setObjectName("companionBubble")
        bubble_layout = QVBoxLayout(bubble)
        bubble_layout.setContentsMargins(12, 10, 12, 9)
        bubble_layout.setSpacing(3)
        layout.addWidget(bubble)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setFixedWidth(278)
        self.status.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.status.setContentsMargins(0, 3, 0, 3)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bubble_layout.addWidget(self.status)
        self.progress = QLabel()
        self.progress.setFixedWidth(278)
        self.progress.setTextFormat(Qt.TextFormat.PlainText)
        self.progress.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.progress.setWordWrap(True)
        self.progress.setObjectName("companionProgress")
        self.progress.hide()
        bubble_layout.addWidget(self.progress)
        self.progress_timer = QTimer(self)
        self.progress_timer.setInterval(500)
        self.progress_timer.timeout.connect(self.render_progress)
        self.copy_area = QWidget()
        copy_layout = QVBoxLayout(self.copy_area)
        copy_layout.setContentsMargins(0, 0, 0, 0)
        copy_header = QHBoxLayout()
        copy_title = QLabel("要输入的内容")
        copy_title.setToolTip("按住这里可移动伴学浮窗")
        self.panel.add_drag_handle(copy_title)
        copy_header.addWidget(copy_title, 1)
        self.copy_button = QPushButton("复制")
        self.copy_button.clicked.connect(self.copy_input)
        copy_header.addWidget(self.copy_button)
        copy_layout.addLayout(copy_header)
        self.input_value = QPlainTextEdit()
        self.input_value.setReadOnly(True)
        self.input_value.setFixedHeight(53)
        self.input_value.setObjectName("companionInput")
        copy_layout.addWidget(self.input_value)
        bubble_layout.addWidget(self.copy_area)
        self.copy_area.hide()
        tail = QLabel("▼")
        tail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tail.setObjectName("companionTail")
        tail.setFixedHeight(12)
        layout.addWidget(tail)
        layout.addWidget(self.panel.sprite, alignment=Qt.AlignmentFlag.AlignHCenter)
        actions = self.panel.controls.layout()
        self.pause_button = CompanionAction("pause")
        self.pause_button.clicked.connect(self.toggle)
        issue = CompanionAction("help")
        issue.clicked.connect(self.report_issue)
        appearance = CompanionAction("appearance")
        appearance.clicked.connect(self.appearance_requested)
        end = CompanionAction("stop")
        end.clicked.connect(self.close if self.persistent else self.panel.close)
        self.chat_button = CompanionAction("chat")
        self.chat_button.clicked.connect(self.idle_chat)
        actions.addWidget(self.chat_button)
        self.practice_controls = (self.pause_button, issue, end)
        for button in (self.pause_button, issue, appearance, end):
            actions.addWidget(button)
        layout.addWidget(self.panel.controls, alignment=Qt.AlignmentFlag.AlignHCenter)
        screen = app.primaryScreen().availableGeometry()
        self.set_status("桌面学伴")
        self.panel.move(screen.right() - self.panel.width() - 18, screen.bottom() - self.panel.height() - 38)
        self.panel.show()
        self.panel.set_pose("idle")

    def begin(self, scope):
        self.capture_generation += 1
        self.session_serial += 1
        self.reset_capture()
        self.scope = scope
        self.ending.clear()
        self.running.set()
        self.chat_button.hide()
        self.pause_button.set_action("pause")
        for control in self.practice_controls:
            control.show()
        self.set_status("切回练习软件，提示会跟着画面更新。")
        self.session_started.emit()

    def set_idle(self):
        for control in self.practice_controls:
            control.hide()
        self.chat_button.show()
        self.set_progress()
        self.copy_area.hide()
        self.input_value.clear()
        self.set_status("桌面学伴 · 悬停后点聊天图标")

    def watch(self):
        previous = None
        with WindowReader() as reader:
            while not self.shutdown.wait(.4):
                generation = self.capture_generation
                scope = self.scope
                if scope is None or self.ending.is_set():
                    reader.close()
                    previous = None
                    continue
                hwnd = None
                if self.running.is_set() and time.monotonic() - self.last_poll < CHAT_LEASE_SECONDS:
                    hwnd = scope.active()
                if hwnd is None:
                    if previous is not None:
                        self.sampled.emit(generation, None)
                        previous = None
                    continue
                previous = hwnd
                try:
                    bounds = native.window_state(hwnd)
                    raw = reader.read(hwnd, *bounds[2:4])
                    if generation != self.capture_generation or not self.running.is_set() or scope.active() != hwnd:
                        continue
                    preview = raw.copy()
                    preview.thumbnail((1440, 1000), Image.Resampling.LANCZOS)
                    key = preview.convert("L").resize((144, 90), Image.Resampling.BILINEAR)
                    self.sampled.emit(generation, (hwnd, bounds, preview, key, raw))
                except native.WindowError as exc:
                    self.capture_failed.emit(generation, str(exc))
                    if self.shutdown.wait(.6):
                        break

    def accept_sample(self, generation, frame):
        if generation == self.capture_generation and not self.ending.is_set():
            if self.running.is_set() and time.monotonic() - self.last_poll < CHAT_LEASE_SECONDS:
                if frame is None:
                    self.capture_error = None
                    self.set_progress()
                    self.set_status("等待练习窗口回到前台。")
                elif self.capture_error:
                    self.capture_error = None
                    self.set_status("画面已恢复，正在查看下一步。")
            self.update(frame)

    def note_capture_failure(self, generation, error):
        if (generation != self.capture_generation or not self.running.is_set() or self.ending.is_set()
                or time.monotonic() - self.last_poll >= CHAT_LEASE_SECONDS):
            return
        if self.capture_error != error:
            self.capture_error = error
            self.set_progress()
            self.set_status(error)
            self.update(None)

    def update(self, frame):
        if frame is None or not self.running.is_set() or self.ending.is_set():
            self.current = self.stable_frame = self.pending = None
            self.force = True
        else:
            self.last_sample = time.monotonic()
            self.last_image = frame[2]
            self.current = frame
            if changed(self.stable_frame, frame):
                self.stable_frame, self.pending = frame, time.monotonic()
        self.wake.emit()

    def take(self):
        if not self.running.is_set() or self.ending.is_set() or self.current is None or self.pending is None or time.monotonic() - self.pending < .55:
            return None
        if self.last_sample < self.sample_after:
            return None
        if not self.force and not changed(self.current, self.last_sent):
            return None
        self.last_sent = self.current
        self.force = False
        self.sample_after = 0.0
        return self.current

    def toggle(self):
        self.capture_generation += 1
        if self.running.is_set():
            self.running.clear()
            self.set_progress()
            self.pause_button.set_action("resume")
            self.set_status("已暂停伴学。点「继续」恢复。")
            self.update(None)
            return
        else:
            self.running.set()
            self.force = True
            self.pause_button.set_action("pause")
            self.set_status("切回练习软件继续")
        self.wake.emit()

    def set_status(self, text, *, bottom=None):
        if bottom is None:
            bottom = self.panel.y() + self.panel.height()
        self.status.setText(text)
        self.fit_text(self.status, text, 8)
        self.fit_panel(bottom)

    @staticmethod
    def fit_text(label, text, margin):
        label.ensurePolished()
        metrics = QFontMetrics(label.font())
        measured = metrics.boundingRect(
            QRect(0, 0, label.width(), 32767), int(Qt.TextFlag.TextWordWrap), text,
        ).height()
        label.setFixedHeight(measured + margin)

    def fit_panel(self, bottom):
        self.panel.layout().invalidate()
        self.panel.layout().activate()
        self.panel.adjustSize()
        self.sync_edge_thought()
        if self.panel.isVisible():
            top = self.panel.screen().availableGeometry().top()
            self.panel.move(self.panel.x(), max(top, bottom - self.panel.height()))

    def set_progress(self, text="", *, pose="thinking", label=""):
        self.panel.set_pose(pose if text else "idle")
        if text == self.progress_text and label == self.progress_label:
            return
        bottom = self.panel.y() + self.panel.height()
        self.progress_text = text
        self.progress_label = label
        self.progress_since = time.monotonic() if text and not label else None
        self.progress_timer.stop()
        if text:
            self.render_progress()
            if self.progress_since is not None:
                self.progress_timer.start()
        self.progress.setVisible(bool(text))
        self.fit_panel(bottom)

    def sync_edge_thought(self):
        text = self.progress.text() if self.progress_text else self.status.text()
        self.panel.peek.set_thought(text)

    def render_progress(self):
        if self.progress_label:
            text = f"{self.progress_label} · {self.progress_text}"
        elif self.progress_since is not None:
            elapsed = int(time.monotonic() - self.progress_since)
            text = f"{self.progress_text} · {elapsed} 秒"
        else:
            return
        before = self.progress.height()
        bottom = self.panel.y() + self.panel.height()
        self.progress.setText(text)
        self.fit_text(self.progress, text, 4)
        if self.progress.isVisible() and before != self.progress.height():
            self.fit_panel(bottom)
        else:
            self.sync_edge_thought()

    def show_instruction(self, text, text_to_type=""):
        self.set_progress()
        bottom = self.panel.y() + self.panel.height()
        if self.input_value.toPlainText() != text_to_type:
            self.input_value.setPlainText(text_to_type)
            self.copy_button.setText("复制")
        self.copy_area.setVisible(bool(text_to_type))
        self.set_status(text, bottom=bottom)
        self.panel.set_pose("ready")

    def copy_input(self):
        QApplication.clipboard().setText(self.input_value.toPlainText())
        self.copy_button.setText("已复制")

    def report_issue(self):
        serial = self.session_serial
        image = self.last_image
        dialog = QDialog(self.panel, Qt.WindowType.Dialog | Qt.WindowType.WindowStaysOnTopHint)
        dialog.setStyleSheet("QDialog{background:#251d3b;} QPlainTextEdit,QListWidget{background:#342851;}")
        dialog.setWindowTitle("反馈到当前对话")
        dialog.setMinimumWidth(400)
        layout = QVBoxLayout(dialog)
        problem = FeedbackEditor()
        problem.setPlaceholderText("描述问题。也可以 Ctrl+V 粘贴图片。")
        problem.setFixedHeight(110)
        layout.addWidget(problem)
        pasted_images = []
        attachments = QListWidget()
        attachments.setFixedHeight(62)
        attachments.setIconSize(QSize(36, 36))
        attachments.hide()
        layout.addWidget(attachments)
        image_actions = QHBoxLayout()
        paste = QPushButton("粘贴图片")
        remove = QPushButton("移除选中图片")
        remove.setEnabled(False)
        image_actions.addWidget(paste)
        image_actions.addWidget(remove)
        layout.addLayout(image_actions)
        attach = QCheckBox("附上最近一次练习窗口截图")
        attach.setEnabled(image is not None)
        if image is None:
            attach.setToolTip("切回练习窗口，让伴学先看一眼画面。")
        layout.addWidget(attach)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        send = buttons.button(QDialogButtonBox.StandardButton.Ok)
        send.setText("发到当前对话")
        send.setEnabled(False)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        def update_send():
            send.setEnabled(bool(pasted_images or attach.isChecked() or problem.toPlainText().strip()))
            attachments.setVisible(bool(pasted_images))
            remove.setEnabled(attachments.currentRow() >= 0)
            paste.setEnabled(len(pasted_images) < 3)

        def add_image(image):
            if len(pasted_images) >= 3 or image.isNull():
                return
            image = image.scaled(1600, 1200, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation) if image.width() > 1600 or image.height() > 1200 else image
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            if not image.save(buffer, "JPEG", 90):
                return
            pasted_images.append(Image.open(BytesIO(bytes(buffer.data()))).convert("RGB"))
            attachments.addItem(QListWidgetItem(QIcon(QPixmap.fromImage(image)),
                                                f"图片 · {image.width()} × {image.height()}"))
            attachments.setCurrentRow(len(pasted_images) - 1)
            update_send()

        def remove_image():
            index = attachments.currentRow()
            if index >= 0:
                del pasted_images[index]
                attachments.takeItem(index)
                update_send()

        problem.image_pasted.connect(add_image)
        paste.clicked.connect(lambda: add_image(QApplication.clipboard().image()))
        remove.clicked.connect(remove_image)
        attachments.currentRowChanged.connect(update_send)
        problem.textChanged.connect(update_send)
        attach.toggled.connect(update_send)
        layout.addWidget(buttons)
        dialog.adjustSize()
        center = self.panel.screen().availableGeometry().center()
        dialog.move(center.x() - dialog.width() // 2, center.y() - dialog.height() // 2)
        problem.setFocus()
        if (dialog.exec() == QDialog.DialogCode.Accepted and serial == self.session_serial
                and not self.ending.is_set()):
            if attach.isChecked() and image is not None:
                pasted_images.append(image)
            self.issues.append((problem.toPlainText().strip()[:800], pasted_images))
            self.force = True
            self.set_status("反馈已提交。切回练习软件查看下一步。" if self.running.is_set()
                            else "反馈已暂存。点「继续」后发到原聊天。")
            self.wake.emit()

    def close(self):
        if self.ending.is_set():
            return
        self.progress_timer.stop()
        self.capture_generation += 1
        self.session_serial += 1
        self.ending.set()
        self.running.clear()
        self.scope = None
        self.reset_capture()
        if self.persistent and not self.shutdown.is_set():
            self.set_idle()
        self.wake.emit()
        self.session_ended.emit()

    def on_panel_closed(self):
        self.shutdown.set()
        self.close()
