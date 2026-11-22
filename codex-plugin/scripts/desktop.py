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
from manager import ManagerWindow, TeamPet, show_centered
from resident_agent import Archive, MAX_INPUT, PROFILE, ask_model, excerpt
from server import MentorServer
from team import PRIMARY, TeamCoordinator, TeamStore


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

    def __init__(self, archive, member=None):
        super().__init__()
        self.archive = archive
        self.member = member
        self.settings = QSettings(str(PROFILE / "agent.ini"), QSettings.Format.IniFormat)
        self.busy = False
        self.taken = False
        self.setWindowTitle("屏幕伴学 · " + (member.name if member else "桌面学伴"))
        self.setStyleSheet(STYLE)
        self.resize(560, 490)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 17, 20, 17)
        layout.setSpacing(10)
        self.heading = QLabel(member.name if member else "桌面学伴")
        self.heading.setObjectName("heading")
        layout.addWidget(self.heading)
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
        name = self.member.name if self.member else "学伴"
        self.history.setPlainText("\n\n".join(
            f"{'你' if row['role'] == 'user' else name}\n{row['body']}"
            for row in self.archive.transcript()
        ))
        scrollbar = self.history.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def set_member(self, member):
        renamed = self.member and self.member.name != member.name
        self.member = member
        if member:
            self.heading.setText(member.name)
            self.setWindowTitle("屏幕伴学 · " + member.name)
        if renamed:
            self.refresh()

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
        self.settings.sync()
        endpoint, model, key = (self.settings.value(field, "", type=str)
                                for field in ("endpoint", "model", "key_name"))
        member = self.member
        config = (endpoint, (member.model if member else "") or model, key)
        self.busy = True
        self.send_button.setEnabled(False)
        self.notice.setText("正在回应…")
        Thread(target=self.answer, args=(config, prompt, source, member), daemon=True).start()

    def answer(self, config, prompt, source, member):
        try:
            def context_for(*queries):
                messages = self.archive.request_messages(prompt, queries)
                if member:
                    messages[0]["content"] += f"你的名字是{member.name}，你的分工是：{member.role}"
                return messages

            context = context_for()
            answer, learn, recall = ask_model(*config, context)
            if 1 <= len(recall) <= 70 and recall != prompt:
                expanded = context_for(recall)
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
        self.member_chats = {}
        self.manager = None
        self.pets = {}
        self.agent_settings = QSettings(str(PROFILE / "agent.ini"), QSettings.Format.IniFormat)
        self.team = TeamCoordinator(TeamStore(PROFILE / "team.sqlite3"), PROFILE / "team.ini",
                                    self.model_settings, self.quiet_ready, self)
        self.team.line.connect(self.show_team_line)
        self.main_line = ""
        self.main_bubble_timer = QTimer(self)
        self.main_bubble_timer.setSingleShot(True)
        self.main_bubble_timer.setInterval(12_000)
        self.main_bubble_timer.timeout.connect(self.reset_main_bubble)
        self.engine.practice.idle_chat.connect(self.show_chat)
        self.engine.practice.session_started.connect(self.takeover)
        self.engine.practice.session_ended.connect(self.after_session)
        self.engine.practice.panel.closed.connect(app.quit)
        self.engine.management_requested.connect(self.show_manager)
        self.listener = QLocalServer(self)
        self.listener.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.listener.newConnection.connect(self.accept)
        self.tray = QSystemTrayIcon(QIcon(str(APP_ICON)), self)
        self.tray.setToolTip("屏幕伴学")
        self.menu = QMenu()
        self.menu.addAction("管理面板", self.show_manager)
        self.menu.addAction("日常对话", self.show_chat)
        self.menu.addAction("外观管理", self.engine.practice.panel.open_appearance)
        self.menu.addAction("退出桌面学伴", self.app.quit)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.tray_clicked)
        self.tray.show()
        self.sync_pets()

    def show_chat(self):
        self.show_member_chat(PRIMARY)

    def show_manager(self):
        if self.manager is None:
            self.manager = ManagerWindow(self.team, self)
        self.manager.refresh()
        show_centered(self.manager)

    def model_settings(self):
        self.agent_settings.sync()
        return tuple(self.agent_settings.value(name, "", type=str) for name in ("endpoint", "model", "key_name"))

    def quiet_ready(self):
        return not any(chat.busy for chat in self.member_chats.values())

    def member_archive(self, key):
        return self.archive if key == PRIMARY else Archive(PROFILE / "members" / key / "agent.sqlite3")

    def show_member_chat(self, key):
        member = next((entry for entry in self.team.store.members() if entry.key == key), None)
        if member is None:
            return
        if key not in self.member_chats:
            self.member_chats[key] = ChatWindow(self.member_archive(key), member)
        chat = self.member_chats[key]
        chat.set_member(member)
        if key == PRIMARY:
            self.chat = chat
        if self.team.blocked:
            chat.set_taken(True)
        show_centered(chat)

    def show_member_memory(self, key):
        dialog = MemoryDialog(self.manager, self.member_archive(key))
        dialog.setStyleSheet(STYLE)
        dialog.exec()
        dialog.deleteLater()

    def sync_pets(self):
        members = [member for member in self.team.store.members()
                   if member.key != PRIMARY and member.active and member.visible]
        visible = {member.key for member in members}
        for key in set(self.pets) - visible:
            self.pets.pop(key).close()
        occupied = {pet.slot for pet in self.pets.values()}
        slots = iter(slot for slot in range(len(members) + len(occupied)) if slot not in occupied)
        for member in members:
            if member.key not in self.pets:
                pet = TeamPet(member, self.show_member_chat, next(slots))
                self.pets[member.key] = pet
                if not self.team.blocked:
                    pet.show()
            self.pets[member.key].update_member(member)
            self.pets[member.key].suspend(self.team.blocked)

    def show_team_line(self, key, name, body):
        if self.team.blocked or self.team.token is None or self.team.token.is_set():
            return
        if key == PRIMARY:
            self.main_line = f"{name} · {excerpt(' '.join(body.split()), 250)}"
            self.engine.practice.set_status(self.main_line)
            self.engine.practice.panel.set_pose("ready")
            self.main_bubble_timer.start()
        elif key in self.pets:
            self.pets[key].say(body)

    def reset_main_bubble(self):
        practice = self.engine.practice
        if not self.team.blocked and practice.status.text() == self.main_line:
            practice.set_idle()

    def takeover(self):
        self.team.set_blocked(True)
        self.main_bubble_timer.stop()
        for pet in self.pets.values():
            pet.suspend(True)
        for chat in self.member_chats.values():
            chat.set_taken(True)

    def tray_clicked(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_manager()

    def after_session(self):
        self.team.set_blocked(False)
        for pet in self.pets.values():
            pet.suspend(False)
        for chat in self.member_chats.values():
            chat.set_taken(False)

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
            if request.get("method") == "show_desktop":
                self.show_manager()
                result = {"shown": True}
            else:
                result = self.engine.respond(request, owner)
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
        desktop.show_manager()
    try:
        return app.exec()
    finally:
        desktop.listener.close()
        lock.unlock()


if __name__ == "__main__":
    sys.exit(main())
