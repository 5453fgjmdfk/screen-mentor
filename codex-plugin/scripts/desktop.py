"""Resident Windows application for chat and external-agent practice."""
from functools import partial
import json
from pathlib import Path
import sqlite3
import sys
from threading import Event, Thread
from uuid import uuid4

from capture import enable_dpi_awareness

enable_dpi_awareness()

from PyQt6 import sip
from PyQt6.QtCore import QPoint, QLockFile, QObject, QSettings, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QIcon, QKeySequence, QShortcut, QTextCursor
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMenu,
    QPlainTextEdit, QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from appearance import APP_ICON, SkinLibrary
from bridge import CHANNEL, MAX_REQUEST, connect, roundtrip
from companion import fit_position
from manager import PANEL_STYLE, ManagerWindow, TeamPet, show_centered
from resident_agent import Archive, MAX_INPUT, PROFILE, ask_model, excerpt
from scheduled import TaskScheduler
from server import MentorServer
from team import PRIMARY, Member, TeamCoordinator, TeamStore


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


class ChatPane(QWidget):
    completed = pyqtSignal(str, str, str)
    chunk = pyqtSignal(str)
    started = pyqtSignal()
    response = pyqtSignal(str, str)

    def __init__(self, archive, member, open_team, open_model):
        super().__init__()
        self.archive = archive
        self.member = member
        self.settings = QSettings(str(PROFILE / "agent.ini"), QSettings.Format.IniFormat)
        self.busy = False
        self.taken = False
        self.cancel = Event()
        self.preview = ""
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(65)
        self.preview_timer.timeout.connect(self.render_preview)
        self.draft_position = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.notice = QLabel("日常对话保存在本机。")
        self.notice.setObjectName("notice")
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
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
        settings.clicked.connect(open_model)
        actions.addWidget(settings)
        memory = QPushButton("长期记忆")
        memory.clicked.connect(self.open_memory)
        actions.addWidget(memory)
        actions.addStretch()
        self.team_button = None
        if open_team is not None:
            self.team_button = QPushButton("协作…")
            self.team_button.setToolTip("打开群聊，并把正在输入的任务带过去。")
            self.team_button.clicked.connect(lambda: open_team(self.input.toPlainText()))
            actions.addWidget(self.team_button)
        self.send_button = QPushButton("发送")
        self.send_button.setObjectName("primary")
        self.send_button.clicked.connect(self.send)
        actions.addWidget(self.send_button)
        shortcut = QShortcut(QKeySequence("Ctrl+Return"), self.input)
        shortcut.activated.connect(self.send)
        self.chunk.connect(self.receive_chunk)
        self.completed.connect(self.receive)
        self.refresh()

    def refresh(self):
        name = self.member.name
        self.history.setPlainText("\n\n".join(
            f"{'你' if row['role'] == 'user' else name}\n{row['body']}"
            for row in self.archive.transcript()
        ))
        scrollbar = self.history.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        self.draft_position = self.history.document().characterCount() - 1

    def set_member(self, member):
        renamed = self.member.name != member.name
        self.member = member
        if renamed and not self.busy:
            self.refresh()

    def receive_chunk(self, text):
        if self.cancel.is_set():
            return
        self.preview = text
        if not self.preview_timer.isActive():
            self.preview_timer.start()

    def render_preview(self):
        scrollbar = self.history.verticalScrollBar()
        following = scrollbar.value() >= scrollbar.maximum() - 12
        cursor = QTextCursor(self.history.document())
        cursor.setPosition(self.draft_position)
        cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        if self.preview:
            cursor.insertText(f"\n\n{self.member.name}\n{self.preview}")
        if following:
            scrollbar.setValue(scrollbar.maximum())
        self.notice.setText("正在回复…" if self.preview else "正在思考…")
        self.response.emit("stream" if self.preview else "think", self.preview)

    def open_memory(self):
        dialog = MemoryDialog(self, self.archive)
        dialog.exec()
        dialog.deleteLater()

    def set_taken(self, taken):
        self.taken = taken
        self.input.setEnabled(not taken)
        self.send_button.setEnabled(not taken and not self.busy)
        if self.team_button:
            self.team_button.setEnabled(not taken)
        if taken:
            self.cancel.set()
            self.preview_timer.stop()
            self.preview = ""
            self.refresh()
            self.notice.setText("当前由外部对话伴学。结束后会回到日常对话。")
        else:
            self.notice.setText("已回到日常对话。")

    def send(self):
        if self.send_message(self.input.toPlainText()):
            self.input.clear()

    def send_message(self, prompt):
        if self.busy or self.taken:
            return False
        prompt = prompt.strip()
        if not prompt:
            return False
        if len(prompt) > MAX_INPUT:
            self.notice.setText(f"每条消息最多 {MAX_INPUT} 字，请拆成几条发送。")
            return False
        try:
            source = self.archive.write("user", prompt)
        except sqlite3.Error:
            self.notice.setText("无法保存消息，请检查本机档案。")
            return False
        self.refresh()
        marker, raw_value = prompt[:3], prompt[3:].strip()
        if marker in {"记住：", "记住:"}:
            try:
                self.archive.remember(raw_value, source_id=source)
            except ValueError as error:
                self.notice.setText(str(error))
            else:
                reply = "已经存入长期记忆。"
                self.archive.write("assistant", reply)
                self.refresh()
                self.response.emit("done", reply)
            return True
        if marker in {"回忆：", "回忆:"}:
            hits = self.archive.search(raw_value, exclude=(source,), limit=5)
            answer = "\n\n".join(f"档案 #{row['id']} · {row['created'][:10]}\n{excerpt(row['body'], 1800)}"
                                 for row in hits)
            self.notice.setText(answer or "没有查到相关记录。可以换成更具体的关键词。")
            self.response.emit("done", self.notice.text())
            return True
        self.settings.sync()
        endpoint, model, key = (self.settings.value(field, "", type=str)
                                for field in ("endpoint", "model", "key_name"))
        member = self.member
        config = member.connection((endpoint, model, key))
        self.busy = True
        self.cancel = Event()
        self.preview = ""
        self.send_button.setEnabled(False)
        self.notice.setText("正在思考…")
        self.started.emit()
        Thread(target=self.answer, args=(config, prompt, source, member, self.cancel), daemon=True).start()
        return True

    def answer(self, config, prompt, source, member, cancel):
        reply = note = error_message = ""
        try:
            def context_for(*queries):
                messages = self.archive.request_messages(prompt, queries)
                messages[0]["content"] += (
                    f"\n{member.identity()}\n若需要重新检索，请先返回 recall，其余字段留空。"
                )
                return messages

            if cancel.is_set():
                return
            context = context_for()
            answer, learn, recall = ask_model(*config, context, publish=self.chunk.emit, cancelled=cancel.is_set)
            if cancel.is_set():
                return
            if 1 <= len(recall) <= 70 and recall != prompt:
                expanded = context_for(recall)
                if expanded != context:
                    self.chunk.emit("")
                    expanded[0]["content"] += "本轮已重新检索，请依据现有证据回复；recall 留空。"
                    answer, learn, _ = ask_model(*config, expanded, publish=self.chunk.emit, cancelled=cancel.is_set)
            if cancel.is_set():
                return
            if not answer:
                answer = "这次没有找到足够的资料。可以换一个更具体的关键词。"
            self.archive.write("assistant", answer)
            reply = answer
            if 1 <= len(learn) <= 240 and learn in prompt:
                self.archive.remember(learn, source_id=source, candidate=True)
                note = "回复已保存，可在「长期记忆」确认新提取的内容。"
        except (ValueError, sqlite3.Error) as error:
            error_message = str(error)
        finally:
            self.completed.emit(reply, note, error_message)

    def receive(self, reply, note, error):
        self.preview_timer.stop()
        self.busy = False
        self.send_button.setEnabled(not self.taken)
        self.refresh()
        if not self.cancel.is_set() and not self.taken:
            self.notice.setText(error or note or "回复已保存在本机。")
            self.response.emit("error" if error else "done", error or reply)


class QuickChat(QFrame):
    def __init__(self, pane, anchor, open_history):
        super().__init__(None, Qt.WindowType.Popup)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setObjectName("card")
        self.setStyleSheet(PANEL_STYLE)
        self.pane = pane
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        row = QHBoxLayout()
        name = QLabel(pane.member.name)
        name.setTextFormat(Qt.TextFormat.PlainText)
        row.addWidget(name, 1)
        history = QPushButton("打开会话")
        history.clicked.connect(lambda: self.open_history(open_history))
        row.addWidget(history)
        layout.addLayout(row)
        entry = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("给这位学伴发消息…")
        self.input.setMaxLength(MAX_INPUT)
        self.input.setMinimumWidth(227)
        self.input.returnPressed.connect(self.send)
        entry.addWidget(self.input, 1)
        submit = QPushButton("发送")
        submit.setObjectName("primary")
        submit.clicked.connect(self.send)
        entry.addWidget(submit)
        layout.addLayout(entry)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.hide()
        layout.addWidget(self.status)
        self.adjustSize()
        area = anchor.screen().availableGeometry()
        point = anchor.mapToGlobal(QPoint(anchor.width() // 2, anchor.height()))
        position = point + QPoint(-self.width() // 2, 8)
        if position.y() + self.height() > area.bottom():
            position.setY(point.y() - anchor.height() - self.height() - 8)
        self.move(fit_position(position, self.size(), area))

    def send(self):
        if self.pane.send_message(self.input.text()):
            self.close()
        elif self.pane.busy or self.input.text().strip():
            self.status.setText("上一条消息还在回复。" if self.pane.busy else self.pane.notice.text())
            self.status.show()

    def open_history(self, callback):
        draft = self.input.text()
        self.close()
        callback()
        if draft and not self.pane.input.toPlainText():
            self.pane.input.setPlainText(draft)


class Desktop(QObject):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.archive = Archive(PROFILE / "agent.sqlite3")
        self.engine = MentorServer(app, self.archive, create_member=self.create_member, list_skins=self.member_skins)
        self.chat = None
        self.member_chats = {}
        self.manager = None
        self.quick_chat = None
        self.chat_bubbles = set()
        self.exiting = False
        self.pets = {}
        self.preferences = QSettings(str(PROFILE / "desktop.ini"), QSettings.Format.IniFormat)
        self.agent_settings = QSettings(str(PROFILE / "agent.ini"), QSettings.Format.IniFormat)
        self.team = TeamCoordinator(TeamStore(PROFILE / "team.sqlite3"), PROFILE / "team.ini",
                                    self.model_settings, self.quiet_ready, self)
        self.scheduler = TaskScheduler(self.team, self)
        self.team.line.connect(self.show_team_line)
        self.main_line = ""
        self.main_bubble_timer = QTimer(self)
        self.main_bubble_timer.setSingleShot(True)
        self.main_bubble_timer.setInterval(12_000)
        self.main_bubble_timer.timeout.connect(self.reset_main_bubble)
        self.engine.practice.idle_chat.connect(lambda: self.show_quick_chat(PRIMARY))
        self.engine.practice.panel.clicked.connect(lambda: self.show_quick_chat(PRIMARY))
        self.engine.practice.panel.add_desktop_actions(self.show_manager, self.quit)
        self.engine.practice.appearance_requested.connect(self.show_appearance)
        self.engine.practice.session_started.connect(self.takeover)
        self.engine.practice.session_ended.connect(self.after_session)
        self.engine.practice.panel.closed.connect(self.quit)
        self.engine.management_requested.connect(self.show_manager)
        self.listener = QLocalServer(self)
        self.listener.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.listener.newConnection.connect(self.accept)
        self.tray = QSystemTrayIcon(QIcon(str(APP_ICON)), self)
        self.tray.setToolTip("屏幕伴学")
        self.menu = QMenu()
        self.menu.addAction("管理面板", self.show_manager)
        self.menu.addAction("日常对话", self.show_chat)
        self.menu.addAction("桌宠外观", lambda: self.show_appearance())
        self.menu.addAction("退出桌面学伴", self.quit)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.tray_clicked)
        self.tray.show()
        self.sync_pets()

    def show_chat(self):
        self.show_member_chat(PRIMARY)

    def show_manager(self):
        manager = self.get_manager()
        manager.refresh()
        if manager.isVisible() or (manager.placed and self.preferences.value("remember_geometry", True, type=bool)):
            if manager.isMinimized():
                manager.showNormal()
            else:
                manager.show()
            manager.raise_()
            manager.activateWindow()
        else:
            show_centered(manager)
            manager.placed = True

    def get_manager(self):
        if self.manager is None:
            self.manager = ManagerWindow(self.team, self)
        return self.manager

    def hide_to_tray(self):
        self.manager.store_layout()
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.manager.hide()
        else:
            self.manager.showMinimized()

    def close_manager(self):
        if self.preferences.value("close_action", "tray", type=str) == "quit":
            QTimer.singleShot(0, self.quit)
        else:
            self.hide_to_tray()

    def workspace_directory(self):
        directory = self.preferences.value("workspace_directory", "", type=str)
        return directory if directory and Path(directory).is_dir() else ""

    def quit(self):
        if self.exiting:
            return
        self.exiting = True
        if self.manager is not None:
            self.manager.store_layout()
        self.engine.practice.shutdown.set()
        self.engine.practice.close()
        self.scheduler.stop()
        self.team.stop()
        self.team.timer.stop()
        for chat in self.member_chats.values():
            chat.cancel.set()
        if self.quick_chat is not None and not sip.isdeleted(self.quick_chat):
            self.quick_chat.close()
        self.tray.hide()
        self.app.quit()

    def show_appearance(self, key=PRIMARY):
        self.show_manager()
        self.manager.open_appearance(key)

    def show_team(self, draft=""):
        self.show_manager()
        self.manager.open_group(draft)

    def model_settings(self):
        self.agent_settings.sync()
        return tuple(self.agent_settings.value(name, "", type=str) for name in ("endpoint", "model", "key_name"))

    def quiet_ready(self):
        return not any(chat.busy for chat in self.member_chats.values())

    def member_archive(self, key):
        return self.archive if key == PRIMARY else Archive(PROFILE / "members" / key / "agent.sqlite3")

    def member_skins(self):
        return [{"key": skin.key, "name": skin.name} for skin in SkinLibrary().all(saved_palettes=False)]

    def create_member(self, options):
        portrait = options.get("portrait_path")
        skin_key = options.get("skin_key")
        if portrait and skin_key:
            raise ValueError("请在现有皮肤与 PNG 图片中选择一种。")
        if skin_key and not any(skin["key"] == skin_key for skin in self.member_skins()):
            raise ValueError("找不到这个皮肤。请用 list_member_skins 查看本机已有的皮肤。")
        endpoint = options.get("endpoint", "").strip()
        model = options.get("model", "").strip()
        member = Member(uuid4().hex, options["name"].strip(), options["role"].strip(),
                        model="" if endpoint else model, skin=skin_key or "default",
                        visible=options.get("visible", False), personality=options.get("personality", "").strip(),
                        persona=options.get("persona", "").strip(), endpoint=endpoint,
                        own_model=model if endpoint else "", key_name=options.get("key_name", "").strip())
        saved = self.save_member(member, portrait=portrait)
        return {"created": True, "member_id": saved.key, "name": saved.name, "role": saved.role,
                "skin_key": saved.skin, "visible": saved.visible, "displayed": saved.visible and not self.team.blocked}

    def save_member(self, member, *, portrait=None):
        install = None
        if portrait:
            source = Path(portrait)
            if not source.is_absolute() or source.drive.startswith("\\") or source.suffix.casefold() != ".png":
                raise ValueError("请选择本机的透明 PNG，或提供它的绝对路径。")
            install = partial(SkinLibrary().import_portrait, source, member.name)
        try:
            saved = self.team.store.save_member(member, install_skin=install)
        except sqlite3.Error as error:
            raise ValueError("无法保存学伴，请检查本机存储。") from error
        self.sync_pets()
        chat = self.member_chats.get(saved.key)
        if chat is not None:
            chat.set_member(saved)
        if self.manager is not None:
            self.manager.reload_members(saved.key)
            if self.manager.model_target.currentData() == saved.key:
                self.manager.load_model()
            self.manager.refresh()
        return saved

    def show_member_chat(self, key):
        self.show_manager()
        self.manager.open_chat(key)

    def chat_for(self, key):
        member = next((entry for entry in self.team.store.members() if entry.key == key), None)
        if member is None:
            return None
        if key not in self.member_chats:
            chat = ChatPane(self.member_archive(key), member, self.show_team if key == PRIMARY else None,
                            lambda: self.get_manager().open_model(key))
            self.member_chats[key] = chat
            chat.started.connect(lambda: self.show_chat_response(key, "think", ""))
            chat.response.connect(lambda phase, text: self.show_chat_response(key, phase, text))
        chat = self.member_chats[key]
        chat.set_member(member)
        if key == PRIMARY:
            self.chat = chat
        if self.team.blocked:
            chat.set_taken(True)
        return chat

    def show_quick_chat(self, key):
        if self.team.blocked:
            return
        panel = self.engine.practice.panel if key == PRIMARY else self.pets.get(key)
        if panel is None or not panel.isVisible():
            return
        pane = self.get_manager().private_chat(key)
        if pane is None:
            return
        if self.quick_chat is not None and not sip.isdeleted(self.quick_chat):
            self.quick_chat.close()
        self.quick_chat = QuickChat(pane, panel.sprite, lambda: self.show_member_chat(key))
        self.quick_chat.show()
        self.quick_chat.input.setFocus()

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
                pet = TeamPet(member, self, next(slots))
                self.pets[member.key] = pet
                if not self.team.blocked:
                    pet.show()
            self.pets[member.key].update_member(member, self.team.store.palette(member.key))
            self.pets[member.key].suspend(self.team.blocked)

    def show_team_line(self, key, name, body):
        if self.team.blocked or self.team.token is None or self.team.token.is_set() or key in self.chat_bubbles:
            return
        if key == PRIMARY:
            self.main_line = f"{name} · {excerpt(' '.join(body.split()), 250)}"
            self.engine.practice.set_status(self.main_line)
            self.engine.practice.panel.set_pose("ready")
            self.main_bubble_timer.start()
        elif key in self.pets:
            self.pets[key].say(body)

    def show_chat_response(self, key, phase, body):
        if self.team.blocked:
            return
        complete = phase in {"done", "error"}
        if phase == "think":
            body = "正在思考…"
        else:
            body = " ".join(body.split())
            if len(body) > 105:
                body = body[:104] + "…" if phase == "error" else "…" + body[-104:]
        if key == PRIMARY:
            practice = self.engine.practice
            self.main_line = body
            self.main_bubble_timer.stop()
            practice.set_status(body)
            practice.panel.set_pose("thinking" if phase == "think" else "ready" if complete else "idle")
            if complete:
                self.main_bubble_timer.start()
        elif key in self.pets:
            self.pets[key].say(body, pose="thinking" if phase == "think" else "ready" if complete else "idle",
                               transient=complete)
        if complete:
            self.chat_bubbles.discard(key)
        else:
            self.chat_bubbles.add(key)

    def reset_main_bubble(self):
        practice = self.engine.practice
        if not self.team.blocked and practice.status.text() == self.main_line:
            practice.set_idle()

    def takeover(self):
        self.team.set_blocked(True)
        self.chat_bubbles.clear()
        if self.quick_chat is not None and not sip.isdeleted(self.quick_chat):
            self.quick_chat.close()
        self.main_bubble_timer.stop()
        for pet in self.pets.values():
            pet.suspend(True)
        for chat in self.member_chats.values():
            chat.set_taken(True)

    def tray_clicked(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_manager()

    def after_session(self):
        if self.exiting:
            return
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
                if request.get("page") == "appearance":
                    self.show_appearance()
                else:
                    self.show_manager()
                result = {"shown": True}
            else:
                result = self.engine.respond(request, owner)
        except (KeyError, AttributeError, TypeError, ValueError):
            result = {"error": {"code": -32600, "message": "Invalid request"}}
        if not sip.isdeleted(socket) and socket.state() == QLocalSocket.LocalSocketState.ConnectedState:
            socket.write(json.dumps(result, ensure_ascii=False).encode() + b"\n")
            socket.disconnectFromServer()


def main(*, initial_page=None):
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
                roundtrip(socket, {"owner": "desktop", "request": {"method": "show_desktop", "page": initial_page}})
        return 0
    desktop = Desktop(app)
    QLocalServer.removeServer(CHANNEL)
    if not desktop.listener.listen(CHANNEL):
        return 1
    if "--background" not in sys.argv:
        if initial_page == "appearance":
            desktop.show_appearance()
        else:
            desktop.show_manager()
    try:
        return app.exec()
    finally:
        desktop.listener.close()
        lock.unlock()


if __name__ == "__main__":
    sys.exit(main())
