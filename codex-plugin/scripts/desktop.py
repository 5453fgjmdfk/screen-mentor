"""Resident Windows application for chat and external-agent practice."""
from functools import partial
import json
import sqlite3
import sys
from threading import Thread

from capture import enable_dpi_awareness

enable_dpi_awareness()

from PyQt6 import sip
from PyQt6.QtCore import QLockFile, QObject, QSettings, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QIcon, QKeySequence, QShortcut
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QApplication, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMenu,
    QPlainTextEdit, QPushButton, QSystemTrayIcon, QVBoxLayout,
)

from appearance import APP_ICON
from bridge import CHANNEL, MAX_REQUEST, connect, roundtrip
from resident_agent import Archive, MAX_INPUT, PROFILE, ask_model, excerpt
from server import MentorServer


STYLE = """
QDialog { background: #191724; }
QLabel, QPlainTextEdit, QLineEdit, QListWidget, QPushButton {
    color: #f2edff;
    font: 13px 'Microsoft YaHei UI';
}
QPlainTextEdit, QLineEdit, QListWidget {
    background: #211e30; border: 1px solid #4d4161; border-radius: 9px; padding: 8px;
}
QPushButton { background: #302642; border: 1px solid #685684; border-radius: 8px; padding: 7px 11px; }
QPushButton:hover { border-color: #cbb6fa; }
QPushButton:disabled { color: #9388a4; }
QPushButton#primary { color: #241834; background: #c1aae9; font-weight: 600; }
QLabel#heading { font-size: 21px; font-weight: 600; }
QLabel#notice { color: #cbb6fa; }
"""


class MemoryDialog(QDialog):
    def __init__(self, parent, archive):
        super().__init__(parent)
        self.archive = archive
        self.setWindowTitle("长期记忆")
        self.resize(555, 365)
        layout = QVBoxLayout(self)
        self.items = QListWidget()
        layout.addWidget(self.items)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(110)
        layout.addWidget(self.detail)
        actions = QHBoxLayout()
        layout.addLayout(actions)
        self.confirm = QPushButton("确认记住")
        self.remove = QPushButton("移出长期记忆")
        self.confirm.clicked.connect(lambda: self.review("confirmed"))
        self.remove.clicked.connect(lambda: self.review("forgotten"))
        actions.addWidget(self.confirm)
        actions.addWidget(self.remove)
        actions.addStretch()
        done = QPushButton("完成")
        done.clicked.connect(self.accept)
        actions.addWidget(done)
        self.items.currentRowChanged.connect(self.show_item)
        self.refresh()

    def refresh(self):
        self.rows = self.archive.memories()
        self.items.clear()
        for row in self.rows:
            label = {"confirmed": "已确认", "candidate": "待确认"}.get(row["state"], "练习小结")
            self.items.addItem(f"{label}　{excerpt(row['body'].replace(chr(10), ' '), 92)}")
        self.items.setCurrentRow(0)
        self.show_item(self.items.currentRow())

    def show_item(self, index):
        row = self.rows[index] if 0 <= index < len(self.rows) else None
        self.detail.setPlainText(row["body"] if row else "还没有长期记忆。")
        self.confirm.setEnabled(bool(row and row["state"] == "candidate"))
        self.remove.setEnabled(bool(row and row["role"] == "memory"))

    def review(self, state):
        index = self.items.currentRow()
        if index >= 0:
            self.archive.review(self.rows[index]["id"], state)
            self.refresh()


class ChatWindow(QDialog):
    completed = pyqtSignal(str, str)

    def __init__(self, archive):
        super().__init__()
        self.archive = archive
        self.settings = QSettings(str(PROFILE / "agent.ini"), QSettings.Format.IniFormat)
        self.busy = False
        self.taken = False
        self.setWindowTitle("屏幕伴学 · 桌面学伴")
        self.setStyleSheet(STYLE)
        self.resize(560, 490)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 17, 20, 17)
        layout.setSpacing(10)
        title = QLabel("桌面学伴")
        title.setObjectName("heading")
        layout.addWidget(title)
        self.notice = QLabel("日常对话保存在本机。")
        self.notice.setObjectName("notice")
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.history = QPlainTextEdit()
        self.history.setAccessibleName("对话记录")
        self.history.setReadOnly(True)
        layout.addWidget(self.history, 1)
        self.input = QPlainTextEdit()
        self.input.setFixedHeight(76)
        self.input.setPlaceholderText("输入消息。用「记住：…」保存一条记忆，用「回忆：…」检索。")
        layout.addWidget(self.input)
        actions = QHBoxLayout()
        layout.addLayout(actions)
        settings = QPushButton("设置模型")
        settings.clicked.connect(self.configure)
        actions.addWidget(settings)
        memory = QPushButton("长期记忆")
        memory.clicked.connect(self.open_memory)
        actions.addWidget(memory)
        actions.addStretch()
        self.send_button = QPushButton("发送")
        self.send_button.setObjectName("primary")
        self.send_button.clicked.connect(self.send)
        actions.addWidget(self.send_button)
        shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        shortcut.activated.connect(self.send)
        self.completed.connect(self.receive)
        self.refresh()

    def refresh(self):
        self.history.setPlainText("\n\n".join(
            f"{'你' if row['role'] == 'user' else '学伴'}\n{row['body']}"
            for row in self.archive.transcript()
        ))
        scrollbar = self.history.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def configure(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("常驻模型")
        form = QFormLayout(dialog)
        endpoint = QLineEdit(self.settings.value("endpoint", "", type=str))
        endpoint.setPlaceholderText("例如 http://127.0.0.1:11434/v1")
        model = QLineEdit(self.settings.value("model", "", type=str))
        key_name = QLineEdit(self.settings.value("key_name", "", type=str))
        key_name.setPlaceholderText("例如 SCREEN_MENTOR_MODEL_KEY，可留空")
        form.addRow("接口地址", endpoint)
        form.addRow("模型名", model)
        form.addRow("密钥环境变量", key_name)
        buttons = QHBoxLayout()
        cancel = QPushButton("取消")
        cancel.clicked.connect(dialog.reject)
        save = QPushButton("保存")
        save.setObjectName("primary")
        save.clicked.connect(dialog.accept)
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        form.addRow(buttons)
        dialog.setMinimumWidth(520)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            for name, field in (("endpoint", endpoint), ("model", model), ("key_name", key_name)):
                self.settings.setValue(name, field.text().strip())
            self.settings.sync()
            self.notice.setText("模型设置已保存。")
        dialog.deleteLater()

    def open_memory(self):
        dialog = MemoryDialog(self, self.archive)
        dialog.exec()
        dialog.deleteLater()

    def set_taken(self, taken):
        self.taken = taken
        self.input.setEnabled(not taken)
        self.send_button.setEnabled(not taken and not self.busy)
        if taken:
            self.notice.setText("当前由外部对话伴学。结束后会回到日常对话。")
            self.hide()
        else:
            self.notice.setText("已回到日常对话。")

    def send(self):
        if self.busy or self.taken:
            return
        prompt = self.input.toPlainText().strip()
        if not prompt:
            return
        if len(prompt) > MAX_INPUT:
            self.notice.setText(f"每条消息最多 {MAX_INPUT} 字，请拆成几条发送。")
            return
        source = self.archive.write("user", prompt)
        self.input.clear()
        self.refresh()
        marker, raw_value = prompt[:3], prompt[3:].strip()
        if marker in {"记住：", "记住:"}:
            try:
                self.archive.remember(raw_value, source_id=source)
            except ValueError as error:
                self.notice.setText(str(error))
            else:
                self.archive.write("assistant", "已经存入长期记忆。")
                self.refresh()
            return
        if marker in {"回忆：", "回忆:"}:
            hits = self.archive.search(raw_value, exclude=(source,), limit=5)
            answer = "\n\n".join(f"档案 #{row['id']} · {row['created'][:10]}\n{excerpt(row['body'], 1800)}"
                                 for row in hits)
            self.notice.setText(answer or "没有查到相关记录。可以换成更具体的关键词。")
            return
        config = tuple(self.settings.value(field, "", type=str) for field in ("endpoint", "model", "key_name"))
        self.busy = True
        self.send_button.setEnabled(False)
        self.notice.setText("正在回应…")
        Thread(target=self.answer, args=(config, prompt, source), daemon=True).start()

    def answer(self, config, prompt, source):
        try:
            context = self.archive.request_messages(prompt)
            answer, learn, recall = ask_model(*config, context)
            if 1 <= len(recall) <= 70 and recall != prompt:
                expanded = self.archive.request_messages(prompt, (recall,))
                if expanded != context:
                    expanded[0]["content"] += "本轮已重新检索，请依据现有证据回复；recall 留空。"
                    answer, learn, _ = ask_model(*config, expanded)
            if not answer:
                answer = "这次没有找到足够的资料。可以换一个更具体的关键词。"
            self.archive.write("assistant", answer)
            if 1 <= len(learn) <= 240 and learn in prompt:
                self.archive.remember(learn, source_id=source, candidate=True)
                self.completed.emit("回复已保存，可在「长期记忆」确认新提取的内容。", "")
            else:
                self.completed.emit("", "")
        except (ValueError, sqlite3.Error) as error:
            self.completed.emit("", str(error))

    def receive(self, note, error):
        self.busy = False
        self.send_button.setEnabled(not self.taken)
        self.refresh()
        if not self.taken:
            self.notice.setText(error or note or "回复已保存在本机。")


class Desktop(QObject):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.archive = Archive(PROFILE / "agent.sqlite3")
        self.engine = MentorServer(app, self.archive)
        self.chat = None
        self.engine.practice.idle_chat.connect(self.show_chat)
        self.engine.practice.session_ended.connect(self.after_session)
        self.engine.practice.panel.closed.connect(app.quit)
        self.listener = QLocalServer(self)
        self.listener.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.listener.newConnection.connect(self.accept)
        self.tray = QSystemTrayIcon(QIcon(str(APP_ICON)), self)
        self.tray.setToolTip("屏幕伴学")
        self.menu = QMenu()
        self.menu.addAction("日常对话", self.show_chat)
        self.menu.addAction("外观管理", self.engine.practice.panel.open_appearance)
        self.menu.addAction("退出桌面学伴", self.app.quit)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.tray_clicked)
        self.tray.show()

    def show_chat(self):
        if self.chat is None:
            self.chat = ChatWindow(self.archive)
        if not self.engine.practice.ending.is_set():
            self.chat.set_taken(True)
        self.chat.show()
        self.chat.raise_()
        self.chat.activateWindow()

    def tray_clicked(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_chat()

    def after_session(self):
        if self.chat is not None:
            self.chat.set_taken(False)

    def accept(self):
        while self.listener.hasPendingConnections():
            socket = self.listener.nextPendingConnection()
            socket.disconnected.connect(socket.deleteLater)
            socket.readyRead.connect(partial(self.receive, socket))
            QTimer.singleShot(0, partial(self.receive, socket))

    def receive(self, socket):
        if sip.isdeleted(socket) or socket.property("handled"):
            return
        if socket.bytesAvailable() > MAX_REQUEST:
            socket.abort()
            return
        if not socket.canReadLine():
            return
        socket.setProperty("handled", True)
        try:
            envelope = json.loads(bytes(socket.readLine()))
            request = envelope["request"]
            owner = envelope["owner"]
            if not isinstance(owner, str) or not 1 <= len(owner) <= 64:
                raise ValueError
            active = not self.engine.practice.ending.is_set()
            if request.get("method") == "show_desktop":
                self.show_chat()
                result = {"shown": True}
            else:
                result = self.engine.respond(request, owner)
            if self.chat is not None and not active and not self.engine.practice.ending.is_set():
                self.chat.set_taken(True)
        except (KeyError, AttributeError, TypeError, ValueError):
            result = {"error": {"code": -32600, "message": "Invalid request"}}
        if not sip.isdeleted(socket) and socket.state() == QLocalSocket.LocalSocketState.ConnectedState:
            socket.write(json.dumps(result, ensure_ascii=False).encode() + b"\n")
            socket.disconnectFromServer()


def main():
    app = QApplication(sys.argv[:1])
    app.setWindowIcon(QIcon(str(APP_ICON)))
    app.setQuitOnLastWindowClosed(False)
    PROFILE.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(PROFILE / "desktop.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        if "--background" not in sys.argv:
            socket = connect()
            if socket:
                roundtrip(socket, {"owner": "desktop", "request": {"method": "show_desktop"}})
        return 0
    desktop = Desktop(app)
    QLocalServer.removeServer(CHANNEL)
    if not desktop.listener.listen(CHANNEL):
        return 1
    if "--background" not in sys.argv:
        desktop.show_chat()
    try:
        return app.exec()
    finally:
        desktop.listener.close()
        lock.unlock()


if __name__ == "__main__":
    sys.exit(main())
