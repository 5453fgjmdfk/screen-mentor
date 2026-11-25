"""Resident Windows application for chat and external-agent practice."""
from functools import partial
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sqlite3
import sys
import threading
from threading import Event, Thread
import traceback
from uuid import uuid4

from capture import enable_dpi_awareness

enable_dpi_awareness()

from PyQt6 import sip
from PyQt6.QtCore import QPoint, QLockFile, QObject, QSettings, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QIcon, QKeySequence, QShortcut, QTextCursor, QTextDocument
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QMenu,
    QMessageBox, QPlainTextEdit, QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from appearance import APP_ICON, SkinLibrary
from bridge import CHANNEL, MAX_REQUEST, connect, roundtrip
from companion import fit_position, style_row_views
from manager import PANEL_STYLE, ManagerWindow, TeamPet, panel_style, show_centered
from resident_agent import Archive, MAX_INPUT, PROFILE, RECALL_MARKERS, REMEMBER_MARKERS, ask_model, excerpt
from scheduled import TaskScheduler
from server import MentorServer
from team import PRIMARY, Member, TeamCoordinator, TeamStore


STYLE = """
QWidget { outline: 0; }
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
    def __init__(self, parent, archive, *, member_name=""):
        super().__init__(parent)
        self.archive = archive
        self.all_rows = []
        self.rows = []
        self.setWindowTitle(f"长期记忆 · {member_name}" if member_name else "长期记忆")
        self.resize(580, 410)
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("按关键词筛选记忆…")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self.apply_filter)
        top.addWidget(self.search_input, 1)
        self.count_label = QLabel()
        self.count_label.setObjectName("quiet")
        top.addWidget(self.count_label)
        layout.addLayout(top)
        self.items = QListWidget()
        layout.addWidget(self.items)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(110)
        layout.addWidget(self.detail)
        self.notice = QLabel()
        self.notice.setObjectName("notice")
        self.notice.setWordWrap(True)
        self.notice.hide()
        layout.addWidget(self.notice)
        actions = QHBoxLayout()
        layout.addLayout(actions)
        add_btn = QPushButton("新增记忆…")
        add_btn.clicked.connect(self.add_memory)
        actions.addWidget(add_btn)
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
        style_row_views(self)
        self.refresh()

    def refresh(self):
        self.all_rows = self.archive.memories()
        self.apply_filter()

    def apply_filter(self):
        needle = self.search_input.text().strip().casefold()
        self.rows = [row for row in self.all_rows if not needle or needle in row["body"].casefold()]
        self.count_label.setText(f"{len(self.rows)} / {len(self.all_rows)} 条" if needle else f"共 {len(self.all_rows)} 条")
        self.items.clear()
        for row in self.rows:
            label = {"confirmed": "已确认", "candidate": "待确认"}.get(row["state"], "练习小结")
            created = (row["created"] or "")[:10]
            prefix = f"{label} · {created}" if created else label
            self.items.addItem(f"{prefix}　{excerpt(row['body'].replace(chr(10), ' '), 84)}")
        self.items.setCurrentRow(0)
        self.show_item(self.items.currentRow())

    def show_item(self, index):
        row = self.rows[index] if 0 <= index < len(self.rows) else None
        if row is not None:
            self.detail.setPlainText(row["body"])
        elif self.search_input.text().strip():
            self.detail.setPlainText("没有匹配的长期记忆。")
        else:
            self.detail.setPlainText("还没有长期记忆。")
        self.confirm.setEnabled(bool(row and row["state"] == "candidate"))
        self.remove.setEnabled(bool(row))

    def add_memory(self):
        text, accepted = QInputDialog.getMultiLineText(self, "新增长期记忆", "填写希望学伴长期记住的内容（1–1200 字）：")
        if not accepted:
            return
        try:
            self.archive.remember(text)
        except (ValueError, sqlite3.Error) as error:
            self.notice.setText(str(error))
            self.notice.show()
            return
        self.notice.setText("已存入长期记忆。")
        self.notice.show()
        self.refresh()

    def review(self, state):
        index = self.items.currentRow()
        if index >= 0:
            try:
                self.archive.review(self.rows[index]["id"], state)
            except (ValueError, sqlite3.Error) as error:
                self.notice.setText(str(error))
                self.notice.show()
                return
            self.notice.hide()
            self.refresh()


class ChatPane(QWidget):
    completed = pyqtSignal(object, str, str, str)
    chunk = pyqtSignal(object, str)
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
        self.last_prompt = ""
        self.last_source_id = None
        self.preview = ""
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(65)
        self.preview_timer.timeout.connect(self.render_preview)
        self.draft_position = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        top_bar = QHBoxLayout()
        self.notice = QLabel("日常对话保存在本机。")
        self.notice.setObjectName("notice")
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        top_bar.addWidget(self.notice, 1)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索当前会话…")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setMaximumWidth(200)
        self.search_input.returnPressed.connect(self.find_next)
        top_bar.addWidget(self.search_input)
        find_btn = QPushButton("查找")
        find_btn.setToolTip("在当前会话记录中查找下一个匹配")
        find_btn.clicked.connect(self.find_next)
        top_bar.addWidget(find_btn)
        layout.addLayout(top_bar)
        self.history = QPlainTextEdit()
        self.history.setAccessibleName("对话记录")
        self.history.setReadOnly(True)
        layout.addWidget(self.history, 1)
        self.input = QPlainTextEdit()
        self.input.setFixedHeight(76)
        self.input.setPlaceholderText("输入消息。用「记住：…」保存一条记忆，用「回忆：…」检索。（Ctrl+Enter 发送）")
        layout.addWidget(self.input)
        actions = QHBoxLayout()
        layout.addLayout(actions)
        settings = QPushButton("设置模型")
        settings.clicked.connect(open_model)
        actions.addWidget(settings)
        memory = QPushButton("长期记忆")
        memory.clicked.connect(self.open_memory)
        actions.addWidget(memory)
        self.clear_button = QPushButton("清空")
        self.clear_button.setToolTip("清空当前私聊记录（保留长期记忆）")
        self.clear_button.clicked.connect(self.clear_history)
        actions.addWidget(self.clear_button)
        actions.addStretch()
        self.retry_button = QPushButton("重试")
        self.retry_button.setToolTip("重新向模型发送上一条消息")
        self.retry_button.clicked.connect(self.retry_last)
        self.retry_button.setEnabled(False)
        actions.addWidget(self.retry_button)
        self.stop_button = QPushButton("停止")
        self.stop_button.setToolTip("停止正在生成的回复")
        self.stop_button.clicked.connect(self.stop_reply)
        self.stop_button.setEnabled(False)
        actions.addWidget(self.stop_button)
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
        for key in ("Ctrl+Return", "Ctrl+Enter"):
            shortcut = QShortcut(QKeySequence(key), self.input)
            shortcut.activated.connect(self.send)
        self.chunk.connect(self.receive_chunk)
        self.completed.connect(self.receive)
        self.refresh()

    def refresh(self):
        name = self.member.name
        rows = self.archive.transcript()
        self.history.setPlainText("\n\n".join(
            f"{'你' if row['role'] == 'user' else name}\n{row['body']}"
            for row in rows
        ))
        scrollbar = self.history.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        self.draft_position = self.history.document().characterCount() - 1
        last_user = next((row for row in reversed(rows)
                          if row["role"] == "user"
                          and not row["body"].startswith(RECALL_MARKERS + REMEMBER_MARKERS)), None)
        if last_user is not None:
            self.last_prompt = last_user["body"]
            self.last_source_id = last_user["id"]
        else:
            self.last_prompt = ""
            self.last_source_id = None
        self.update_controls()

    def update_controls(self):
        self.send_button.setEnabled(not self.taken and not self.busy)
        self.stop_button.setEnabled(self.busy and not self.taken)
        self.retry_button.setEnabled(bool(self.last_prompt) and not self.busy and not self.taken)
        self.clear_button.setEnabled(not self.busy and not self.taken)

    def find_next(self):
        query = self.search_input.text().strip()
        if not query:
            return
        if not self.history.find(query):
            cursor = self.history.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            self.history.setTextCursor(cursor)
            if not self.history.find(query):
                self.notice.setText(f"当前显示记录中未找到「{query}」，可用「回忆：{query}」检索全部档案。")
                return
        self.history.ensureCursorVisible()
        self.notice.setText(f"已定位到「{query}」。")

    def clear_history(self):
        if self.busy or self.taken:
            return
        if not self.history.toPlainText().strip():
            self.notice.setText("当前会话记录已经为空。")
            return
        reply = QMessageBox.question(
            self, "清空会话记录",
            f"清空与「{self.member.name}」的全部私聊记录？已保存的长期记忆与练习小结会保留。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self.archive.clear_transcript()
        except sqlite3.Error:
            self.notice.setText("无法清空会话记录，请检查本机档案。")
            return
        self.refresh()
        self.notice.setText("已清空私聊记录，长期记忆已保留。")

    def set_member(self, member):
        renamed = self.member.name != member.name
        self.member = member
        if renamed and not self.busy:
            self.refresh()

    def receive_chunk(self, token, text):
        if token is not self.cancel or token.is_set():
            return
        self.preview = text
        if not self.preview_timer.isActive():
            self.preview_timer.start()

    def render_preview(self):
        if not self.busy or self.cancel.is_set():
            return
        scrollbar = self.history.verticalScrollBar()
        following = scrollbar.value() >= scrollbar.maximum() - 12
        cursor = QTextCursor(self.history.document())
        cursor.setPosition(min(self.draft_position, self.history.document().characterCount() - 1))
        cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        if self.preview:
            cursor.insertText(f"\n\n{self.member.name}\n{self.preview}")
        if following:
            scrollbar.setValue(scrollbar.maximum())
        self.notice.setText("正在回复…" if self.preview else "正在思考…")
        self.response.emit("stream" if self.preview else "think", self.preview)

    def open_memory(self):
        dialog = MemoryDialog(self, self.archive, member_name=self.member.name)
        dialog.exec()
        dialog.deleteLater()

    def stop_reply(self):
        if not self.busy:
            return
        self.cancel.set()
        self.preview_timer.stop()
        self.busy = False
        self.preview = ""
        self.refresh()
        self.notice.setText("已停止本次回复。")
        self.response.emit("error", "已停止本次回复。")

    def retry_last(self):
        if self.busy or self.taken or not self.last_prompt:
            return
        self._dispatch_model(self.last_prompt, self.last_source_id)

    def set_taken(self, taken):
        self.taken = taken
        self.input.setEnabled(not taken)
        if self.team_button:
            self.team_button.setEnabled(not taken)
        if taken:
            self.cancel.set()
            self.preview_timer.stop()
            self.busy = False
            self.preview = ""
            self.refresh()
            self.notice.setText("当前由外部对话伴学。结束后会回到日常对话。")
        else:
            self.update_controls()
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
        marker, raw_value = prompt[:3], prompt[3:].strip()
        if marker in REMEMBER_MARKERS and not 1 <= len(raw_value) <= 1200:
            self.notice.setText("每条记忆需要 1–1200 个字符。")
            return False
        try:
            source = self.archive.write("user", prompt)
        except sqlite3.Error:
            self.notice.setText("无法保存消息，请检查本机档案。")
            return False
        self.refresh()
        if marker in REMEMBER_MARKERS:
            try:
                self.archive.remember(raw_value, source_id=source)
            except (ValueError, sqlite3.Error) as error:
                self.notice.setText(str(error))
            else:
                reply = "已经存入长期记忆。"
                self.archive.write("assistant", reply)
                self.refresh()
                self.notice.setText("已存入长期记忆。")
                self.response.emit("done", reply)
            return True
        if marker in RECALL_MARKERS:
            try:
                hits = self.archive.search(raw_value, exclude=(source,), limit=5)
            except sqlite3.Error:
                self.notice.setText("无法检索本机档案，请检查存储状态。")
                return True
            if hits:
                answer = "\n\n".join(f"档案 #{row['id']} · {(row['created'] or '')[:10]}\n{excerpt(row['body'], 1800)}"
                                     for row in hits)
                cursor = QTextCursor(self.history.document())
                cursor.movePosition(QTextCursor.MoveOperation.End)
                cursor.insertText("\n\n")
                result_start = cursor.position()
                cursor.insertText(f"回忆结果\n{answer}")
                cursor.setPosition(result_start)
                self.history.setTextCursor(cursor)
                self.history.ensureCursorVisible()
                notice = f"找到 {len(hits)} 条相关记录。"
            else:
                notice = "没有查到相关记录。可以换成更具体的关键词。"
            self.notice.setText(notice)
            self.response.emit("done", notice)
            return True
        self._dispatch_model(prompt, source)
        return True

    def _dispatch_model(self, prompt, source):
        self.settings.sync()
        endpoint, model, key = (self.settings.value(field, "", type=str)
                                for field in ("endpoint", "model", "key_name"))
        member = self.member
        config = member.connection((endpoint, model, key))
        self.busy = True
        self.cancel = Event()
        token = self.cancel
        self.preview = ""
        self.update_controls()
        self.notice.setText("正在思考…")
        self.started.emit()
        Thread(target=self.answer, args=(config, prompt, source, member, token), daemon=True).start()

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
            publish = lambda text: self.chunk.emit(cancel, text)
            answer, learn, recall = ask_model(*config, context, publish=publish, cancelled=cancel.is_set)
            if cancel.is_set():
                return
            if 1 <= len(recall) <= 70 and recall != prompt:
                expanded = context_for(recall)
                if expanded != context:
                    publish("")
                    expanded[0]["content"] += "本轮已重新检索，请依据现有证据回复；recall 留空。"
                    answer, learn, _ = ask_model(*config, expanded, publish=publish, cancelled=cancel.is_set)
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
            try:
                self.completed.emit(cancel, reply, note, error_message)
            except RuntimeError:
                pass

    def receive(self, token, reply, note, error):
        if token is not self.cancel:
            return
        self.preview_timer.stop()
        self.busy = False
        self.refresh()
        if not token.is_set() and not self.taken:
            self.notice.setText(error or note or "回复已保存在本机。")
            self.response.emit("error" if error else "done", error or reply)


class QuickChat(QFrame):
    def __init__(self, pane, anchor, open_history, *, style=PANEL_STYLE):
        super().__init__(None, Qt.WindowType.Popup)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setObjectName("card")
        self.setStyleSheet(style)
        self.pane = pane
        self.anchor = anchor
        self.show_history = open_history
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        row = QHBoxLayout()
        name = QLabel(pane.member.name)
        name.setTextFormat(Qt.TextFormat.PlainText)
        row.addWidget(name, 1)
        history = QPushButton("打开会话")
        history.clicked.connect(self.open_history)
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
        self._place()

    def _place(self):
        self.adjustSize()
        screen = self.anchor.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        point = self.anchor.mapToGlobal(QPoint(self.anchor.width() // 2, self.anchor.height()))
        position = point + QPoint(-self.width() // 2, 8)
        if position.y() + self.height() > area.bottom():
            position.setY(point.y() - self.anchor.height() - self.height() - 8)
        self.move(fit_position(position, self.size(), area))

    def send(self):
        prompt = self.input.text().strip()
        if self.pane.send_message(prompt):
            self.close()
            if prompt.startswith(RECALL_MARKERS):
                self.show_history()
        elif self.pane.busy or prompt:
            self.status.setText("上一条消息还在回复。" if self.pane.busy else self.pane.notice.text())
            self.status.show()
            self._place()

    def open_history(self):
        draft = self.input.text()
        self.close()
        self.show_history()
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
        self.pets_visible = self.preferences.value("pets_on_startup", True, type=bool)
        self.engine.practice.panel.suspend(not self.pets_visible)
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
        self.engine.practice.panel.layout_changed.connect(lambda: self.save_pet_layout(PRIMARY))
        self.engine.practice.panel.add_desktop_actions(self.show_manager, self.hide_pets)
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
        self.pets_action = self.menu.addAction("", self.toggle_pets)
        self.update_pets_action()
        self.menu.addAction("快速对话", lambda: self.show_quick_chat(PRIMARY) if self.pets_visible else self.show_chat())
        self.menu.addAction("日常对话", self.show_chat)
        self.menu.addAction("桌宠外观", lambda: self.show_appearance())
        self.menu.addAction("重置桌宠位置", self.reset_pet_positions)
        self.menu.addSeparator()
        self.menu.addAction("退出桌面学伴", self.quit)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.tray_clicked)
        self.tray.show()
        self.attach_pet_menu(self.engine.practice.panel)
        self.apply_pet_preferences()
        self.engine.practice.dialog_style = self.current_panel_style()
        self.restore_pet_layout(PRIMARY, self.engine.practice.panel)
        self.sync_pets()

    def current_panel_style(self):
        return panel_style(
            self.preferences.value("theme", "slate", type=str),
            self.preferences.value("theme_color", "#1a232b", type=str),
        )

    def save_pet_layout(self, key):
        panel = self.engine.practice.panel if key == PRIMARY else self.pets.get(key)
        if panel is None:
            return
        self.preferences.setValue(f"pet_layouts/{key}", json.dumps(panel.layout_state()))
        self.preferences.sync()

    def restore_pet_layout(self, key, panel):
        raw = self.preferences.value(f"pet_layouts/{key}", "", type=str)
        if not raw:
            return False
        try:
            state = json.loads(raw)
        except ValueError:
            return False
        return panel.restore_layout(state)

    def reset_pet_positions(self):
        self.preferences.beginGroup("pet_layouts")
        self.preferences.remove("")
        self.preferences.endGroup()
        self.preferences.sync()
        screen = self.app.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            primary_panel = self.engine.practice.panel
            if primary_panel.peek.isVisible():
                primary_panel.peek.hide()
            primary_panel.dock_position = None
            primary_panel.adjustSize()
            primary_panel.move(fit_position(
                QPoint(area.right() - primary_panel.width() - 18, area.bottom() - primary_panel.height() - 38),
                primary_panel.size(), area,
            ))
            if self.pets_visible and not primary_panel.isVisible():
                primary_panel.show()
        for pet in self.pets.values():
            if pet.peek.isVisible():
                pet.peek.hide()
            pet.dock_position = None
            pet.move(pet.default_position())
            if self.pets_visible and not self.team.blocked and pet.member.visible and not pet.isVisible():
                pet.show()

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

    def attach_pet_menu(self, panel):
        for widget in (panel, panel.peek):
            widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            widget.customContextMenuRequested.connect(partial(self.popup_pet_menu, widget))

    def popup_pet_menu(self, widget, point):
        self.menu.popup(widget.mapToGlobal(point))

    def update_pets_action(self):
        self.pets_action.setText("隐藏桌宠" if self.pets_visible else "显示桌宠")

    def hide_pets(self):
        self.set_pets_visible(False)

    def toggle_pets(self):
        self.set_pets_visible(not self.pets_visible)

    def set_pets_visible(self, visible):
        self.pets_visible = visible
        self.sync_pet_visibility()
        self.update_pets_action()
        if self.manager is not None:
            self.manager.update_pet_visibility()

    def sync_pet_visibility(self):
        self.engine.practice.panel.suspend(not self.pets_visible)
        for pet in self.pets.values():
            pet.suspend(not self.pets_visible or self.team.blocked or not pet.member.visible)
        quick = self.quick_chat
        if quick is not None and not sip.isdeleted(quick):
            key = quick.pane.member.key
            if not self.pets_visible or (key != PRIMARY and (key not in self.pets or self.pets[key].suspended)):
                quick.close()

    def apply_pet_preferences(self):
        interval = max(4, min(30, self.preferences.value("bubble_seconds", 12, type=int))) * 1000
        self.main_bubble_timer.setInterval(interval)
        enabled = self.preferences.value("snap_enabled", True, type=bool)
        opacity = max(50, min(100, self.preferences.value("pet_opacity", 100, type=int))) / 100
        self.engine.practice.panel.set_snapping(enabled)
        self.engine.practice.panel.set_pet_opacity(opacity)
        for pet in self.pets.values():
            pet.set_snapping(enabled)
            pet.set_pet_opacity(opacity)
            pet.bubble_timer.setInterval(interval)

    def workspace_directory(self):
        directory = self.preferences.value("workspace_directory", "", type=str)
        return directory if directory and Path(directory).is_dir() else ""

    def quit(self):
        if self.exiting:
            return
        self.exiting = True
        if self.manager is not None:
            self.manager.store_layout()
        self.main_bubble_timer.stop()
        self.engine.hide_guide()
        self.engine.overlay.close()
        self.engine.practice.shutdown.set()
        self.engine.practice.close()
        self.scheduler.stop()
        self.team.stop()
        self.team.timer.stop()
        for chat in self.member_chats.values():
            chat.cancel.set()
        for pet in list(self.pets.values()):
            pet.close()
        self.pets.clear()
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

    def delete_member(self, key):
        if key == PRIMARY:
            raise ValueError("主学伴不能删除。")
        if self.team.running:
            raise ValueError("当前正在群聊或协作，请先停止后再删除成员。")
        try:
            self.team.store.remove_member(key)
        except sqlite3.Error as error:
            raise ValueError("无法删除学伴，请检查本机存储。") from error
        pet = self.pets.pop(key, None)
        if pet is not None:
            pet.close()
        chat = self.member_chats.pop(key, None)
        if chat is not None:
            chat.cancel.set()
            if self.manager is not None:
                self.manager.private_chats.removeWidget(chat)
            chat.deleteLater()
        if self.quick_chat is not None and not sip.isdeleted(self.quick_chat) and self.quick_chat.pane.member.key == key:
            self.quick_chat.close()
        self.preferences.remove(f"pet_layouts/{key}")
        self.preferences.sync()
        self.sync_pets()
        if self.manager is not None:
            self.manager.reload_members(PRIMARY)
            self.manager.reload_rooms()
            self.manager.refresh()

    def on_skin_deleted(self, skin_key):
        try:
            self.team.store.reset_skin(skin_key)
        except sqlite3.Error:
            pass
        self.engine.practice.panel.refresh_skin(force=True)
        self.sync_pets()
        if self.manager is not None:
            self.manager.reload_members()

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
        self.quick_chat = QuickChat(pane, panel.sprite, lambda: self.show_member_chat(key),
                                    style=self.current_panel_style())
        self.quick_chat.show()
        self.quick_chat.input.setFocus()

    def show_member_memory(self, key):
        member = next((entry for entry in self.team.store.members() if entry.key == key), None)
        dialog = MemoryDialog(self.manager, self.member_archive(key), member_name=member.name if member else "")
        if self.manager is None:
            dialog.setStyleSheet(self.current_panel_style())
        dialog.exec()
        dialog.deleteLater()

    def sync_pets(self):
        members = [member for member in self.team.store.members() if member.key != PRIMARY]
        keys = {member.key for member in members}
        for key in set(self.pets) - keys:
            self.pets.pop(key).close()
        occupied = {pet.slot for pet in self.pets.values()}
        slots = iter(slot for slot in range(len(members) + len(occupied)) if slot not in occupied)
        opacity = max(50, min(100, self.preferences.value("pet_opacity", 100, type=int))) / 100
        for member in members:
            if member.key not in self.pets:
                if not member.visible:
                    continue
                pet = TeamPet(member, self, next(slots))
                self.pets[member.key] = pet
                self.attach_pet_menu(pet)
                pet.layout_changed.connect(partial(self.save_pet_layout, member.key))
                pet.set_snapping(self.preferences.value("snap_enabled", True, type=bool))
                pet.set_pet_opacity(opacity)
                pet.bubble_timer.setInterval(self.main_bubble_timer.interval())
                restored_dock = self.restore_pet_layout(member.key, pet)
                if not self.team.blocked and self.pets_visible and not (restored_dock and pet.peek.isVisible()):
                    pet.show()
            self.pets[member.key].update_member(member, self.team.store.palette(member.key))
        self.sync_pet_visibility()

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
        pose = "thinking" if phase == "think" else "ready" if phase == "done" else "idle"
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
            practice.panel.set_pose(pose)
            if complete:
                self.main_bubble_timer.start()
        elif key in self.pets:
            self.pets[key].say(body, pose=pose, transient=complete)
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
        self.sync_pet_visibility()
        for chat in self.member_chats.values():
            chat.set_taken(True)

    def tray_clicked(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_manager()

    def after_session(self):
        if self.exiting:
            return
        self.team.set_blocked(False)
        self.sync_pet_visibility()
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
                def disconnected():
                    return sip.isdeleted(socket) or socket.state() != QLocalSocket.LocalSocketState.ConnectedState

                result = self.engine.respond(request, owner, disconnected=disconnected)
        except (KeyError, AttributeError, TypeError, ValueError):
            result = {"error": {"code": -32600, "message": "Invalid request"}}
        if not sip.isdeleted(socket) and socket.state() == QLocalSocket.LocalSocketState.ConnectedState:
            socket.write(json.dumps(result, ensure_ascii=False).encode() + b"\n")
            socket.flush()
            socket.disconnectFromServer()


def log_unexpected_errors():
    """Keep crash locations without retaining prompts, screenshots, or exception messages."""
    logger = logging.getLogger("screen_mentor")
    try:
        handler = RotatingFileHandler(PROFILE / "desktop-errors.log", maxBytes=65536, backupCount=1,
                                      encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)

    def report(error_type, _error, stack):
        locations = " > ".join(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                               for frame in traceback.extract_tb(stack))
        logger.error("%s %s", error_type.__name__, locations)

    sys.excepthook = report
    threading.excepthook = lambda args: report(args.exc_type, args.exc_value, args.exc_traceback)


def main(*, initial_page=None):
    app = QApplication(sys.argv[:1])
    app.setWindowIcon(QIcon(str(APP_ICON)))
    app.setQuitOnLastWindowClosed(False)
    PROFILE.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(PROFILE / "desktop.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        if "--background" not in sys.argv:
            try:
                socket = connect()
                if socket:
                    roundtrip(socket, {"owner": "desktop", "request": {"method": "show_desktop", "page": initial_page}})
            except (OSError, ValueError):
                pass
        return 0
    log_unexpected_errors()
    desktop = Desktop(app)
    try:
        QLocalServer.removeServer(CHANNEL)
        if not desktop.listener.listen(CHANNEL):
            return 1
        if "--background" not in sys.argv:
            if initial_page == "appearance":
                desktop.show_appearance()
            else:
                desktop.show_manager()
        return app.exec()
    finally:
        desktop.listener.close()
        desktop.engine.practice.stop_worker()
        lock.unlock()


if __name__ == "__main__":
    sys.exit(main())
