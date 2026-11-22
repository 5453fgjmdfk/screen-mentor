"""Native management panel and desktop views for companion members."""
from dataclasses import replace
from datetime import datetime
import sqlite3
from uuid import uuid4

from PyQt6.QtCore import QDateTime, QPoint, QSignalBlocker, QTimer, Qt
from PyQt6.QtGui import QCursor, QGuiApplication
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDateTimeEdit, QDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from appearance import AppearanceEditor, SkinLibrary, palette_for, palette_key
from companion import CompanionAction, CompanionPanel, fit_position
from resident_agent import MAX_INPUT, excerpt
from scheduled import FREQUENCIES, MODES, ScheduledTask
from team import (
    MAX_MEMBERS, MAX_SPEAKERS, MAX_WORKERS, MAX_INTERVAL, MIN_INTERVAL, CasualRoom, Member, PRIMARY, ROOM_MODES,
)


PANEL_STYLE = """
QDialog, QWidget#panel {background:#1a232b;color:#e5edf1;}
QLabel, QLineEdit, QPlainTextEdit, QListWidget, QPushButton, QCheckBox, QComboBox, QSpinBox, QDateTimeEdit {
    color:#e5edf1;font:13px 'Microsoft YaHei UI';
}
QLabel#title {font-size:23px;font-weight:600;}
QLabel#section {font-size:16px;font-weight:600;}
QLabel#quiet {color:#a7bbc7;}
QLabel#notice {color:#b2ccd9;}
QFrame#rail {background:#202b35;border:1px solid #354752;border-radius:14px;}
QFrame#card {background:#222f3a;border:1px solid #344754;border-radius:13px;}
QListWidget, QPlainTextEdit, QLineEdit, QComboBox, QSpinBox, QDateTimeEdit {
    background:#1d2933;border:1px solid #3f5360;border-radius:8px;padding:7px;selection-background-color:#416272;
}
QListWidget#nav {background:transparent;border:0;}
QListWidget::item {padding:8px;border-radius:7px;}
QListWidget::item:selected {background:#365260;color:#f3f8fa;}
QPushButton {background:#2b3e4a;border:1px solid #455e6d;border-radius:8px;padding:7px 12px;}
QPushButton:hover, QPushButton:focus {border-color:#9abacb;}
QPushButton:disabled {color:#8596a0;border-color:#374b56;}
QPushButton#primary {background:#a1c1d0;color:#1c2b34;font-weight:600;}
"""


def label(text, role=""):
    widget = QLabel(text)
    widget.setObjectName(role)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    return widget


def button(text, callback, *, primary=False):
    widget = QPushButton(text)
    if primary:
        widget.setObjectName("primary")
    widget.clicked.connect(callback)
    return widget


def show_centered(dialog):
    screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
    area = screen.availableGeometry()
    dialog.move(fit_position(area.center() - dialog.rect().center(), dialog.size(), area))
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()


class TeamPet(CompanionPanel):
    def __init__(self, member, desktop, slot):
        super().__init__(skin_key=member.skin, sprite_size=102)
        self.member = member
        self.slot = slot
        self.suspended = False
        self.dock_position = None
        self.setWindowTitle(member.name)
        self.sprite.setAccessibleName(member.name)
        layout = QVBoxLayout(self)
        layout.setSpacing(2)
        layout.setContentsMargins(3, 3, 3, 3)
        bubble = QFrame()
        bubble.setObjectName("companionBubble")
        copy = QVBoxLayout(bubble)
        copy.setContentsMargins(9, 6, 9, 6)
        self.caption = label(member.name)
        self.caption.setFixedWidth(156)
        copy.addWidget(self.caption)
        layout.addWidget(bubble)
        layout.addWidget(self.sprite, alignment=Qt.AlignmentFlag.AlignHCenter)
        chat = CompanionAction("chat")
        chat.clicked.connect(lambda: desktop.show_quick_chat(self.member.key))
        self.clicked.connect(lambda: desktop.show_quick_chat(self.member.key))
        self.controls.layout().addWidget(chat)
        appearance = CompanionAction("appearance")
        appearance.clicked.connect(lambda: desktop.show_appearance(self.member.key))
        self.controls.layout().addWidget(appearance)
        self.add_desktop_actions(desktop.show_manager, desktop.quit)
        layout.addWidget(self.controls, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.bubble_timer = QTimer(self)
        self.bubble_timer.setSingleShot(True)
        self.bubble_timer.setInterval(12_000)
        self.bubble_timer.timeout.connect(lambda: self.say(""))
        self.say("")
        area = QGuiApplication.primaryScreen().availableGeometry()
        width = self.width() + 12
        columns = max(1, (area.width() - 340) // width)
        x = area.right() - 335 - (slot % columns + 1) * width
        y = area.bottom() - 38 - self.height() - slot // columns * (self.height() + 28)
        self.move(fit_position(QPoint(x, y), self.size(), area))

    def update_member(self, member, palette):
        changed = self.member != member
        self.member = member
        self.skin_key = member.skin
        self.skin_palette = palette
        self.refresh_skin()
        if changed:
            self.setWindowTitle(member.name)
            self.sprite.setAccessibleName(member.name)
            self.say("")

    def say(self, text, *, pose="ready", transient=True):
        message = f"{self.member.name}\n{excerpt(' '.join(text.split()), 350)}" if text else self.member.name
        bottom = self.y() + self.height()
        self.caption.setText(message)
        self.layout().activate()
        self.adjustSize()
        if self.isVisible() and self.drag_handle is None:
            area = self.screen().availableGeometry()
            self.move(fit_position(QPoint(self.x(), bottom - self.height()), self.size(), area))
        self.peek.set_thought(message)
        self.bubble_timer.stop()
        if text:
            self.set_pose(pose)
            if transient:
                self.bubble_timer.start()

    def suspend(self, suspend):
        if self.suspended == suspend:
            return
        self.suspended = suspend
        if suspend:
            self.bubble_timer.stop()
            self.say("")
            if self.peek.isVisible():
                self.dock_position = (self.peek.edge, self.peek.geometry().center(), self.peek.area)
                self.peek.hide()
            self.hide()
        elif self.dock_position is not None:
            self.peek.dock(*self.dock_position)
            self.dock_position = None
        else:
            self.show()


class MemberDialog(QDialog):
    def __init__(self, parent, member, persist):
        super().__init__(parent)
        self.persist = persist
        self.member = member or Member(uuid4().hex, "", "")
        self.setWindowTitle("编辑成员" if member else "添加成员")
        self.setMinimumWidth(484)
        self.setStyleSheet(PANEL_STYLE)
        form = QFormLayout(self)
        form.setContentsMargins(18, 16, 18, 16)
        self.name = QLineEdit(self.member.name)
        self.name.setMaxLength(20)
        self.role = QPlainTextEdit(self.member.role)
        self.role.setFixedHeight(80)
        self.role.setPlaceholderText("例如：复核实验步骤，提醒我验证结果。")
        self.model = QLineEdit(self.member.model)
        self.model.setMaxLength(120)
        self.model.setPlaceholderText("留空则使用默认模型")
        self.visible_pet = QCheckBox("在桌面显示这个伙伴")
        self.visible_pet.setChecked(self.member.visible)
        self.active = QCheckBox("可参加群聊和协作")
        self.active.setChecked(self.member.active)
        form.addRow("成员名称", self.name)
        form.addRow("分工", self.role)
        form.addRow("模型覆盖", self.model)
        form.addRow(self.visible_pet)
        form.addRow(self.active)
        if self.member.key == PRIMARY:
            for control in (self.visible_pet, self.active):
                control.setEnabled(False)
        self.notice = label("", "notice")
        form.addRow(self.notice)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("取消", self.reject))
        actions.addWidget(button("保存", self.save, primary=True))
        form.addRow(actions)

    def save(self):
        member = replace(self.member, name=self.name.text().strip(), role=self.role.toPlainText().strip(),
                         model=self.model.text().strip(),
                         visible=self.visible_pet.isChecked(), active=self.active.isChecked())
        try:
            self.persist(member)
        except (ValueError, sqlite3.Error) as error:
            self.notice.setText(str(error))
            return
        self.accept()


class ScheduledTaskDialog(QDialog):
    def __init__(self, parent, task, persist):
        super().__init__(parent)
        self.setWindowTitle("编辑定时任务" if task else "新建定时任务")
        self.setMinimumWidth(480)
        self.persist = persist
        self.key = task.key if task else uuid4().hex
        form = QFormLayout(self)
        self.name = QLineEdit(task.name if task else "")
        self.name.setMaxLength(50)
        self.prompt = QPlainTextEdit(task.prompt if task else "")
        self.prompt.setPlaceholderText("例如：出 3 道运放题，最后附上核对提示。")
        self.prompt.setFixedHeight(100)
        when = task.next_at if task else QDateTime.currentDateTime().addSecs(300).toPyDateTime()
        if task and not task.enabled and when < datetime.now():
            when = QDateTime.currentDateTime().addSecs(300).toPyDateTime()
        self.when = QDateTimeEdit(QDateTime(when))
        self.when.setCalendarPopup(True)
        self.when.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.frequency = QComboBox()
        for days, name in FREQUENCIES.items():
            self.frequency.addItem(name, days)
        self.mode = QComboBox()
        for mode, name in MODES.items():
            self.mode.addItem(name, mode)
        self.mode.setToolTip("并行协作由主学伴带队，使用当前启用的前三位子成员。")
        if task:
            self.frequency.setCurrentIndex(self.frequency.findData(task.repeat_days))
            self.mode.setCurrentIndex(self.mode.findData(task.mode))
        self.enabled = QCheckBox("保存后启用")
        self.enabled.setChecked(task is None or task.enabled)
        form.addRow("名称", self.name)
        form.addRow("任务内容", self.prompt)
        form.addRow("执行时间", self.when)
        form.addRow("重复", self.frequency)
        form.addRow("执行方式", self.mode)
        form.addRow(self.enabled)
        self.notice = label("", "notice")
        form.addRow(self.notice)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("取消", self.reject))
        actions.addWidget(button("保存任务", self.save, primary=True))
        form.addRow(actions)

    def save(self):
        when = self.when.dateTime().toPyDateTime().replace(second=0, microsecond=0, tzinfo=None)
        if self.enabled.isChecked() and when < datetime.now().replace(second=0, microsecond=0):
            self.notice.setText("请选择当前或之后的时间。")
            return
        task = ScheduledTask(self.key, self.name.text().strip(), self.prompt.toPlainText().strip(),
                             when, self.frequency.currentData(), self.mode.currentData(), self.enabled.isChecked())
        try:
            self.persist(task)
        except (ValueError, sqlite3.Error) as error:
            self.notice.setText(str(error))
            return
        self.accept()


class CasualRoomDialog(QDialog):
    def __init__(self, parent, team, room, *, new=False):
        super().__init__(parent)
        self.team = team
        self.room = room
        self.setWindowTitle("新建闲聊房间" if new else "闲聊房间设置")
        self.setMinimumWidth(560)
        form = QFormLayout(self)
        self.name = QLineEdit(room.name)
        self.name.setMaxLength(32)
        self.guidance = QPlainTextEdit(room.guidance)
        self.guidance.setPlaceholderText("例如：围绕电路学习交流，答复简短，遇到分歧先解释理由。")
        self.guidance.setFixedHeight(58)
        self.follow = QCheckBox("跟随当前可见桌宠")
        self.follow.setChecked(room.follow_visible)
        self.members = QListWidget()
        self.members.setAccessibleName("房间成员")
        self.members.setFixedHeight(112)
        self.lead = QComboBox()
        selected = room.members
        if room.follow_visible and not selected:
            selected = tuple(member.key for member in team.room_members(room))
        for member in team.store.members():
            self.lead.addItem(member.name, member.key)
            item = QListWidgetItem(member.name + ("" if member.active else "（已停用）"))
            item.setData(Qt.ItemDataRole.UserRole, member.key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if member.key in selected else Qt.CheckState.Unchecked)
            self.members.addItem(item)
        self.lead.setCurrentIndex(max(0, self.lead.findData(room.lead)))
        self.mode = QComboBox()
        for mode, name in ROOM_MODES.items():
            self.mode.addItem(name, mode)
        self.mode.setCurrentIndex(self.mode.findData(room.mode))
        self.topics = QPlainTextEdit(room.topics)
        self.topics.setPlaceholderText("每行一个话题，留空时使用内置话题。")
        self.topics.setFixedHeight(72)
        self.automatic = QCheckBox("把本房间设为定时闲聊房间")
        self.automatic.setToolTip("同一时间只启用一个定时闲聊房间。")
        self.automatic.setChecked(room.automatic)
        self.interval = QSpinBox()
        self.interval.setRange(MIN_INTERVAL, MAX_INTERVAL)
        self.interval.setSuffix(" 分钟")
        self.interval.setValue(room.minutes)
        form.addRow("房间名称", self.name)
        form.addRow("房间提示", self.guidance)
        form.addRow(self.follow)
        form.addRow("参与成员", self.members)
        form.addRow("默认发言", self.mode)
        form.addRow("主持人", self.lead)
        form.addRow("闲聊话题", self.topics)
        form.addRow(self.automatic)
        form.addRow("自动间隔", self.interval)
        self.notice = label("", "notice")
        form.addRow(self.notice)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("取消", self.reject))
        actions.addWidget(button("保存房间", self.save, primary=True))
        form.addRow(actions)
        self.follow.toggled.connect(self.update_fields)
        self.mode.currentIndexChanged.connect(self.update_fields)
        self.automatic.toggled.connect(self.update_fields)
        self.update_fields()

    def update_fields(self):
        self.members.setEnabled(not self.follow.isChecked())
        self.lead.setEnabled(self.mode.currentData() == "lead")
        self.interval.setEnabled(self.automatic.isChecked())

    def save(self):
        selected = tuple(item.data(Qt.ItemDataRole.UserRole)
                         for item in (self.members.item(i) for i in range(self.members.count()))
                         if item.checkState() == Qt.CheckState.Checked)
        room = replace(self.room, name=self.name.text().strip(), guidance=self.guidance.toPlainText().strip(),
                       topics=self.topics.toPlainText().strip(), members=selected, follow_visible=self.follow.isChecked(),
                       mode=self.mode.currentData(), lead=self.lead.currentData(), automatic=self.automatic.isChecked(),
                       minutes=self.interval.value())
        try:
            self.team.save_room(room)
        except (ValueError, sqlite3.Error) as error:
            self.notice.setText(str(error))
            return
        self.accept()


class ManagerWindow(QDialog):
    def __init__(self, team, desktop):
        super().__init__()
        self.team = team
        self.desktop = desktop
        self.room_notices = {}
        self.room_drafts = {}
        self.displayed_room = ""
        self.setWindowTitle("屏幕伴学 · 管理面板")
        self.setStyleSheet(PANEL_STYLE)
        self.resize(840, 570)
        self.setMinimumSize(718, 487)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(17, 17, 17, 17)
        layout.setSpacing(15)
        rail = QFrame()
        rail.setObjectName("rail")
        rail.setFixedWidth(146)
        layout.addWidget(rail)
        nav_layout = QVBoxLayout(rail)
        nav_layout.setContentsMargins(12, 12, 12, 12)
        nav_layout.addWidget(label("屏幕伴学", "section"))
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setAccessibleName("管理面板导航")
        nav_layout.addWidget(self.nav, 1)
        self.nav.addItems(["桌面", "会话", "群聊", "成员", "外观", "模型", "闲聊", "定时任务"])
        nav_layout.addWidget(button("收起到托盘", self.desktop.hide_to_tray))
        nav_layout.addWidget(button("退出桌面学伴", self.desktop.quit))
        self.pages = QStackedWidget()
        layout.addWidget(self.pages, 1)
        self.pages.addWidget(self.home_page())
        self.chat_index = self.pages.addWidget(self.chat_page())
        self.group_index = self.pages.addWidget(self.group_page())
        self.pages.addWidget(self.members_page())
        self.appearance_index = self.pages.addWidget(self.appearance_page())
        self.model_index = self.pages.addWidget(self.model_page())
        self.pages.addWidget(self.schedule_page())
        self.tasks_index = self.pages.addWidget(self.tasks_page())
        self.nav.currentRowChanged.connect(self.switch_page)
        self.nav.setCurrentRow(0)
        self.team.changed.connect(self.refresh)
        self.team.line.connect(self.add_line)
        self.team.notice.connect(self.show_notice)
        self.team.room_notice.connect(self.show_room_notice)
        self.desktop.scheduler.changed.connect(self.load_tasks)
        self.desktop.scheduler.notice.connect(self.tasks_notice.setText)
        self.reload_members()
        self.reload_rooms()
        self.load_tasks()
        self.refresh()

    def page(self, title):
        page = QWidget()
        page.setObjectName("panel")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(5, 4, 7, 4)
        layout.setSpacing(12)
        layout.addWidget(label(title, "title"))
        return page, layout

    def switch_page(self, index):
        self.pages.setCurrentIndex(index)
        if index == self.chat_index:
            self.select_chat()
        elif index == self.tasks_index:
            self.load_tasks()

    def home_page(self):
        page, layout = self.page("桌面学伴")
        card = QFrame()
        card.setObjectName("card")
        contents = QVBoxLayout(card)
        contents.setContentsMargins(15, 12, 15, 12)
        self.mode = label("", "section")
        contents.addWidget(self.mode)
        self.info = label("", "quiet")
        contents.addWidget(self.info)
        actions = QHBoxLayout()
        contents.addLayout(actions)
        actions.addWidget(button("日常对话", self.desktop.show_chat, primary=True))
        self.exit_practice = button("结束伴学", self.desktop.engine.practice.close)
        actions.addWidget(self.exit_practice)
        actions.addStretch()
        layout.addWidget(card)
        row = QHBoxLayout()
        layout.addLayout(row)
        row.addWidget(button("长期记忆", lambda: self.desktop.show_member_memory(PRIMARY)))
        row.addWidget(button("桌宠外观", lambda: self.open_appearance(PRIMARY)))
        row.addWidget(button("定时任务", lambda: self.nav.setCurrentRow(self.tasks_index)))
        row.addStretch()
        layout.addStretch()
        return page

    def group_page(self):
        page, layout = self.page("群聊")
        body = QHBoxLayout()
        layout.addLayout(body, 1)
        self.history = QPlainTextEdit()
        self.history.setReadOnly(True)
        self.history.setAccessibleName("群聊记录")
        body.addWidget(self.history, 1)
        roster = QVBoxLayout()
        roster.addWidget(label("参与成员", "section"))
        roster.addWidget(label(f"群聊每轮最多 {MAX_SPEAKERS} 位。\n并行协作由主学伴带队，最多 {MAX_WORKERS} 位子成员。", "quiet"))
        self.participants = QListWidget()
        self.participants.setAccessibleName("勾选参与成员")
        self.participants.setFixedWidth(156)
        roster.addWidget(self.participants, 1)
        body.addLayout(roster)
        self.prompt = QPlainTextEdit()
        self.prompt.setFixedHeight(69)
        self.prompt.setPlaceholderText("发给勾选的成员。也可以用 @成员名称 指定回复。")
        layout.addWidget(self.prompt)
        actions = QHBoxLayout()
        layout.addLayout(actions)
        self.group_notice = label("", "notice")
        actions.addWidget(self.group_notice, 1)
        self.stop_button = button("停止", self.team.stop)
        self.task_button = button("接力讨论", lambda: self.start("task"))
        self.parallel_button = button("并行协作", lambda: self.start("parallel"))
        self.parallel_button.setToolTip("主学伴负责拆分与汇总，勾选的子成员同时处理各自任务。")
        self.send_button = button("发送", lambda: self.start("chat"), primary=True)
        for action in (self.stop_button, self.task_button, self.parallel_button, self.send_button):
            actions.addWidget(action)
        return page

    def chat_page(self):
        page, layout = self.page("学伴会话")
        row = QHBoxLayout()
        row.addWidget(label("对话对象"))
        self.chat_member = QComboBox()
        self.chat_member.setAccessibleName("选择对话的学伴")
        row.addWidget(self.chat_member, 1)
        layout.addLayout(row)
        self.private_chats = QStackedWidget()
        layout.addWidget(self.private_chats, 1)
        self.chat_member.currentIndexChanged.connect(self.select_chat)
        return page

    def private_chat(self, key):
        pane = self.desktop.chat_for(key)
        if pane is not None and self.private_chats.indexOf(pane) < 0:
            self.private_chats.addWidget(pane)
        return pane

    def open_chat(self, key=PRIMARY):
        self.chat_member.setCurrentIndex(max(0, self.chat_member.findData(key)))
        self.nav.setCurrentRow(self.chat_index)
        self.select_chat()
        pane = self.private_chats.currentWidget()
        if pane is not None:
            pane.input.setFocus()

    def select_chat(self):
        key = self.chat_member.currentData()
        if key is not None:
            pane = self.private_chat(key)
            if pane is not None:
                self.private_chats.setCurrentWidget(pane)

    def open_model(self):
        self.nav.setCurrentRow(self.model_index)

    def members_page(self):
        page, layout = self.page("成员")
        self.members = QListWidget()
        self.members.setAccessibleName("桌面学伴成员")
        layout.addWidget(self.members, 1)
        self.member_hint = label("", "quiet")
        layout.addWidget(self.member_hint)
        actions = QHBoxLayout()
        layout.addLayout(actions)
        actions.addWidget(button("私聊", lambda: self.member_action(self.desktop.show_member_chat)))
        actions.addWidget(button("查看记忆", lambda: self.member_action(self.desktop.show_member_memory)))
        actions.addWidget(button("编辑外观", lambda: self.member_action(self.open_appearance)))
        actions.addWidget(button("编辑", lambda: self.edit_member()))
        actions.addStretch()
        actions.addWidget(button("添加成员", lambda: self.edit_member(add=True), primary=True))
        return page

    def appearance_page(self):
        page, layout = self.page("桌宠外观")
        row = QHBoxLayout()
        row.addWidget(label("编辑桌宠"))
        self.appearance_member = QComboBox()
        self.appearance_member.setAccessibleName("选择要编辑的桌宠")
        row.addWidget(self.appearance_member, 1)
        layout.addLayout(row)
        self.skin_library = SkinLibrary()
        self.skin_editor = AppearanceEditor(self.skin_library, self.skin_library.current(), self.apply_appearance)
        layout.addWidget(self.skin_editor, 1)
        self.appearance_member.currentIndexChanged.connect(self.load_appearance)
        return page

    def open_appearance(self, key=PRIMARY):
        self.appearance_member.setCurrentIndex(max(0, self.appearance_member.findData(key)))
        self.nav.setCurrentRow(self.appearance_index)

    def load_appearance(self):
        key = self.appearance_member.currentData()
        if key == PRIMARY:
            skin = self.skin_library.current()
        else:
            member = next(item for item in self.team.store.members() if item.key == key)
            skin = self.skin_library.resolve(member.skin, saved_palette=False)
            palette = self.team.store.palette(key)
            if palette:
                skin = replace(skin, palette=palette_for(palette))
        self.skin_editor.load(skin)

    def apply_appearance(self, skin):
        key = self.appearance_member.currentData()
        if key == PRIMARY:
            self.skin_library.select(skin)
            self.desktop.engine.practice.panel.refresh_skin()
        else:
            try:
                self.team.store.set_appearance(key, skin.key, palette_key(skin.palette))
            except sqlite3.Error as error:
                raise ValueError("无法保存桌宠外观，请检查本机目录的写入权限。") from error
            self.desktop.sync_pets()
            self.reload_members()

    def open_group(self, draft=""):
        self.nav.setCurrentRow(self.group_index)
        if draft and not self.prompt.toPlainText().strip():
            self.prompt.setPlainText(draft)
        self.prompt.setFocus()

    def model_page(self):
        page, layout = self.page("模型")
        form = QFormLayout()
        layout.addLayout(form)
        self.model_fields = {}
        for name, caption, hint in (("endpoint", "接口地址", "例如 http://127.0.0.1:11434/v1"),
                                    ("model", "默认模型", "成员可以单独覆盖模型名"),
                                    ("key_name", "密钥环境变量名", "例如 SCREEN_MENTOR_MODEL_KEY，可留空")):
            field = QLineEdit(self.desktop.agent_settings.value(name, "", type=str))
            field.setPlaceholderText(hint)
            form.addRow(caption, field)
            self.model_fields[name] = field
        status = label("", "notice")
        layout.addWidget(status)

        def save():
            for name, field in self.model_fields.items():
                self.desktop.agent_settings.setValue(name, field.text().strip())
            self.desktop.agent_settings.sync()
            status.setText("模型设置已保存。")
            self.refresh()

        layout.addWidget(button("保存模型设置", save, primary=True), alignment=Qt.AlignmentFlag.AlignRight)
        layout.addStretch()
        return page

    def schedule_page(self):
        page, layout = self.page("桌面闲聊")
        header = QHBoxLayout()
        self.rooms = QComboBox()
        self.rooms.setAccessibleName("选择闲聊房间")
        header.addWidget(self.rooms, 1)
        header.addWidget(button("房间设置", self.edit_room))
        header.addWidget(button("新建房间", lambda: self.edit_room(new=True)))
        layout.addLayout(header)
        self.room_members = label("", "quiet")
        layout.addWidget(self.room_members)
        self.room_history = QPlainTextEdit()
        self.room_history.setReadOnly(True)
        self.room_history.setAccessibleName("当前闲聊房间记录")
        self.room_history.setPlaceholderText("发条消息，或点「聊一次」让学伴开启下一个话题。")
        layout.addWidget(self.room_history, 1)
        self.room_post = QLineEdit()
        self.room_post.setMaxLength(MAX_INPUT)
        self.room_post.setPlaceholderText("在房间里发消息，也可以用 @成员名 或 @all")
        self.room_post.returnPressed.connect(self.send_room_post)
        layout.addWidget(self.room_post)
        actions = QHBoxLayout()
        layout.addLayout(actions)
        self.try_chat = button("聊一次", self.try_casual)
        actions.addWidget(self.try_chat)
        self.stop_casual = button("停止闲聊", self.team.stop)
        actions.addWidget(self.stop_casual)
        self.casual_notice = label("", "notice")
        actions.addWidget(self.casual_notice, 1)
        self.send_casual = button("发送", self.send_room_post, primary=True)
        actions.addWidget(self.send_casual)
        self.rooms.currentIndexChanged.connect(self.refresh_casual)
        return page

    def reload_rooms(self, key=None):
        current = self.rooms.currentData()
        key = key or (current.key if current else None)
        with QSignalBlocker(self.rooms):
            self.rooms.clear()
            for room in self.team.store.rooms():
                self.rooms.addItem(room.name, room)
                if room.key == key:
                    self.rooms.setCurrentIndex(self.rooms.count() - 1)
        self.refresh_casual()

    def edit_room(self, *, new=False):
        room = self.rooms.currentData()
        if new:
            members = tuple(member.key for member in self.team.store.members() if member.active)[:2]
            room = CasualRoom(uuid4().hex, members=members)
        if room is None:
            return
        dialog = CasualRoomDialog(self, self.team, room, new=new)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.reload_rooms(room.key)
        dialog.deleteLater()

    def refresh_casual(self):
        room = self.rooms.currentData()
        if room is None:
            return
        if self.displayed_room != room.key:
            if self.displayed_room:
                self.room_drafts[self.displayed_room] = self.room_post.text()
            self.room_post.setText(self.room_drafts.get(room.key, ""))
            self.displayed_room = room.key
        members = "、".join(member.name for member in self.team.room_members(room))
        schedule = f" · 每 {room.minutes} 分钟" if room.automatic else ""
        self.room_members.setText(f"{members or '暂无可用成员'} · {ROOM_MODES[room.mode]}{schedule}")
        rows = self.team.store.lines(casual_key=room.key)
        self.room_history.setPlainText("\n\n".join(f"{row['speaker']}\n{row['body']}" for row in rows))
        scrollbar = self.room_history.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        self.casual_notice.setText(self.room_notices.get(room.key, ""))
        for control in (self.try_chat, self.send_casual):
            control.setEnabled(not self.team.running and not self.team.blocked)
        self.stop_casual.setEnabled(self.team.active_room == room.key and self.team.running)

    def show_room_notice(self, key, text):
        self.room_notices[key] = text
        current = self.rooms.currentData()
        if current and current.key == key:
            self.casual_notice.setText(text)

    def try_casual(self):
        self.run_casual()

    def send_room_post(self):
        if self.run_casual(self.room_post.text()):
            self.room_post.clear()

    def run_casual(self, prompt=None):
        room = self.rooms.currentData()
        if room is None:
            return False
        try:
            self.team.casual(room, prompt)
        except (ValueError, sqlite3.Error) as error:
            self.show_room_notice(room.key, str(error))
            return False
        return True

    def tasks_page(self):
        page, layout = self.page("定时任务")
        layout.addWidget(label("按电脑本地时间执行。收起到托盘后继续运行，伴学或聊天时会等到空闲。", "quiet"))
        self.task_list = QListWidget()
        self.task_list.setAccessibleName("定时任务列表")
        self.task_list.setMinimumHeight(75)
        layout.addWidget(self.task_list, 1)
        actions = QHBoxLayout()
        self.edit_task_button = button("编辑", lambda: self.edit_task(self.current_task()))
        self.toggle_task_button = button("暂停计划", self.toggle_task)
        self.stop_task_button = button("停止本次", self.team.stop)
        self.remove_task_button = button("移除", self.remove_task)
        for control in (self.edit_task_button, self.toggle_task_button, self.stop_task_button, self.remove_task_button):
            actions.addWidget(control)
        actions.addStretch()
        actions.addWidget(button("新建任务", lambda: self.edit_task(), primary=True))
        layout.addLayout(actions)
        layout.addWidget(label("最近一次执行", "section"))
        self.tasks_notice = label("", "notice")
        layout.addWidget(self.tasks_notice)
        self.task_result = QPlainTextEdit()
        self.task_result.setReadOnly(True)
        self.task_result.setAccessibleName("定时任务的最近一次执行结果")
        self.task_result.setPlaceholderText("选中任务即可查看结果。")
        layout.addWidget(self.task_result, 1)
        self.task_list.currentItemChanged.connect(self.show_task_result)
        self.task_list.itemDoubleClicked.connect(lambda: self.edit_task(self.current_task()))
        return page

    def current_task(self):
        item = self.task_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def load_tasks(self):
        previous = self.current_task()
        try:
            tasks = self.desktop.scheduler.tasks()
        except sqlite3.Error as error:
            self.tasks_notice.setText(str(error))
            return
        with QSignalBlocker(self.task_list):
            self.task_list.clear()
            for task in tasks:
                if task.enabled:
                    next_run = f"下次 {task.next_at:%Y-%m-%d %H:%M}"
                else:
                    next_run = "已结束" if not task.repeat_days and task.last_round else "已暂停"
                item = QListWidgetItem(f"{task.name}\n{FREQUENCIES[task.repeat_days]} · {MODES[task.mode]} · {next_run}")
                item.setData(Qt.ItemDataRole.UserRole, task)
                self.task_list.addItem(item)
                if previous and task.key == previous.key:
                    self.task_list.setCurrentItem(item)
            if self.task_list.currentRow() < 0 and self.task_list.count():
                self.task_list.setCurrentRow(0)
        self.show_task_result()

    def show_task_result(self):
        task = self.current_task()
        for control in (self.edit_task_button, self.toggle_task_button, self.remove_task_button):
            control.setEnabled(task is not None)
        self.stop_task_button.setEnabled(bool(task and task.last_round and
                                             task.last_round == self.desktop.scheduler.active_round))
        if task is None:
            self.tasks_notice.setText("还没有定时任务。")
            self.task_result.clear()
            return
        self.toggle_task_button.setText("暂停计划" if task.enabled else "启用计划")
        self.tasks_notice.setText(task.last_status or "尚未执行。")
        try:
            rows = self.team.store.lines(round_id=task.last_round) if task.last_round else []
        except sqlite3.Error as error:
            self.tasks_notice.setText(str(error))
            return
        self.task_result.setPlainText("\n\n".join(f"{row['speaker']}\n{row['body']}" for row in rows))

    def edit_task(self, task=None):
        dialog = ScheduledTaskDialog(self, task, self.desktop.scheduler.save)
        dialog.exec()
        dialog.deleteLater()

    def toggle_task(self):
        task = self.current_task()
        if task is None:
            return
        if not task.enabled and not task.repeat_days and task.last_round:
            self.edit_task(task)
            return
        try:
            self.desktop.scheduler.save(replace(task, enabled=not task.enabled))
        except sqlite3.Error as error:
            self.tasks_notice.setText(str(error))

    def remove_task(self):
        task = self.current_task()
        if task is None:
            return
        reply = QMessageBox.question(self, "移除定时任务", f"移除「{task.name}」？群聊中的执行记录会保留。",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            try:
                self.desktop.scheduler.remove(task.key)
            except sqlite3.Error as error:
                self.tasks_notice.setText(str(error))

    def selected(self):
        return [self.participants.item(index).data(Qt.ItemDataRole.UserRole)
                for index in range(self.participants.count())
                if self.participants.item(index).checkState() == Qt.CheckState.Checked]

    def start(self, kind):
        try:
            self.team.start(kind, self.prompt.toPlainText(), self.selected())
        except (ValueError, sqlite3.Error) as error:
            self.group_notice.setText(str(error))
        else:
            self.prompt.clear()

    def reload_members(self):
        previous = self.current_member()
        checked = set(self.selected())
        first_load = not self.participants.count()
        self.members.clear()
        self.participants.clear()
        members = self.team.store.members()
        for member in members:
            state = "显示在桌面" if member.visible and member.active else "未显示"
            role = "主学伴" if member.key == PRIMARY else "子成员"
            item = QListWidgetItem(f"{member.name}　·　{role}　·　{state}\n{excerpt(member.role, 156)}")
            item.setData(Qt.ItemDataRole.UserRole, member)
            self.members.addItem(item)
            if previous and previous.key == member.key:
                self.members.setCurrentItem(item)
            if member.active:
                entry = QListWidgetItem(member.name)
                entry.setData(Qt.ItemDataRole.UserRole, member.key)
                entry.setFlags(entry.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                entry.setCheckState(Qt.CheckState.Checked if first_load or member.key in checked else Qt.CheckState.Unchecked)
                self.participants.addItem(entry)
        if self.members.currentRow() < 0:
            self.members.setCurrentRow(0)
        for combo in (self.appearance_member, self.chat_member):
            current_target = combo.currentData()
            with QSignalBlocker(combo):
                combo.clear()
                for member in members:
                    combo.addItem(member.name, member.key)
                combo.setCurrentIndex(max(0, combo.findData(current_target)))

    def current_member(self):
        item = self.members.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def member_action(self, callback):
        member = self.current_member()
        if member is not None:
            callback(member.key)

    def edit_member(self, *, add=False):
        current = self.current_member()
        dialog = MemberDialog(self, None if add else current, self.team.store.save_member)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.desktop.sync_pets()
            self.reload_members()
            self.refresh()
        dialog.deleteLater()

    def show_notice(self, message):
        self.group_notice.setText(message)

    def add_line(self, key, name, body):
        if self.team.active_room:
            self.refresh_casual()
        else:
            self.refresh_history()
        task = self.current_task()
        if task and task.last_round and task.last_round == self.desktop.scheduler.active_round:
            self.show_task_result()

    def refresh_history(self):
        kinds = {"task": "接力", "parallel": "并行协作", "chat": "群聊", "casual": "闲聊"}
        rows = self.team.store.lines(kinds=("chat", "task"))
        self.history.setPlainText("\n\n".join(
            f"{row['speaker']} · {kinds[row['display_kind']]}\n{row['body']}" for row in rows))
        scrollbar = self.history.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def refresh(self):
        active = self.team.blocked
        self.mode.setText("当前对话正在伴学" if active else "桌面学伴待命")
        count = sum(member.visible and member.active for member in self.team.store.members())
        room = self.team.automatic_room()
        schedule = f"{room.name} · 每 {room.minutes} 分钟" if room else "定时闲聊关闭"
        self.info.setText(f"{count} 位伙伴显示在桌面 · {schedule}")
        self.member_hint.setText(f"最多保存 {MAX_MEMBERS} 位成员。私聊和长期记忆分别保存。")
        self.exit_practice.setEnabled(active)
        for control in (self.send_button, self.task_button, self.parallel_button):
            control.setEnabled(not active and not self.team.running)
        self.stop_button.setEnabled(self.team.running)
        self.refresh_history()
        self.refresh_casual()
