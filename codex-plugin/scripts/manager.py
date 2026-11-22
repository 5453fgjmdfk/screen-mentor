"""Native management panel and desktop views for companion members."""
from dataclasses import replace
import sqlite3
from uuid import uuid4

from PyQt6.QtCore import QPoint, QTimer, Qt
from PyQt6.QtGui import QCursor, QGuiApplication
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from appearance import SkinLibrary
from companion import CompanionAction, CompanionPanel, fit_position
from resident_agent import excerpt
from team import MAX_MEMBERS, MAX_SPEAKERS, MAX_INTERVAL, MIN_INTERVAL, Member, PRIMARY


PANEL_STYLE = """
QDialog, QWidget#panel {background:#1a232b;color:#e5edf1;}
QLabel, QLineEdit, QPlainTextEdit, QListWidget, QPushButton, QCheckBox, QComboBox, QSpinBox {
    color:#e5edf1;font:13px 'Microsoft YaHei UI';
}
QLabel#title {font-size:23px;font-weight:600;}
QLabel#section {font-size:16px;font-weight:600;}
QLabel#quiet {color:#a7bbc7;}
QLabel#notice {color:#b2ccd9;}
QFrame#rail {background:#202b35;border:1px solid #354752;border-radius:14px;}
QFrame#card {background:#222f3a;border:1px solid #344754;border-radius:13px;}
QListWidget, QPlainTextEdit, QLineEdit, QComboBox, QSpinBox {
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
    def __init__(self, member, open_chat, slot):
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
        chat.clicked.connect(lambda: open_chat(self.member.key))
        self.controls.layout().addWidget(chat)
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

    def update_member(self, member):
        changed = self.member != member
        self.member = member
        self.skin_key = member.skin
        self.refresh_skin()
        if changed:
            self.setWindowTitle(member.name)
            self.sprite.setAccessibleName(member.name)
            self.say("")

    def say(self, text):
        message = f"{self.member.name}\n{excerpt(' '.join(text.split()), 220)}" if text else self.member.name
        bottom = self.y() + self.height()
        self.caption.setText(message)
        self.layout().activate()
        self.adjustSize()
        if self.isVisible():
            area = self.screen().availableGeometry()
            self.move(fit_position(QPoint(self.x(), bottom - self.height()), self.size(), area))
        self.peek.set_thought(message)
        if text:
            self.set_pose("ready")
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
        self.skin = QComboBox()
        for skin in SkinLibrary().all():
            self.skin.addItem(skin.name, skin.key)
        self.skin.setCurrentIndex(max(0, self.skin.findData(self.member.skin)))
        self.visible_pet = QCheckBox("在桌面显示这个伙伴")
        self.visible_pet.setChecked(self.member.visible)
        self.active = QCheckBox("可参加群聊和协作")
        self.active.setChecked(self.member.active)
        form.addRow("成员名称", self.name)
        form.addRow("分工", self.role)
        form.addRow("模型覆盖", self.model)
        form.addRow("角色皮肤", self.skin)
        form.addRow(self.visible_pet)
        form.addRow(self.active)
        if self.member.key == PRIMARY:
            for control in (self.skin, self.visible_pet, self.active):
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
                         model=self.model.text().strip(), skin=self.skin.currentData(),
                         visible=self.visible_pet.isChecked(), active=self.active.isChecked())
        try:
            self.persist(member)
        except (ValueError, sqlite3.Error) as error:
            self.notice.setText(str(error))
            return
        self.accept()


class ManagerWindow(QDialog):
    def __init__(self, team, desktop):
        super().__init__()
        self.team = team
        self.desktop = desktop
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
        self.nav.addItems(["桌面", "群聊", "成员", "模型", "闲聊"])
        self.pages = QStackedWidget()
        layout.addWidget(self.pages, 1)
        self.pages.addWidget(self.home_page())
        self.pages.addWidget(self.group_page())
        self.pages.addWidget(self.members_page())
        self.pages.addWidget(self.model_page())
        self.pages.addWidget(self.schedule_page())
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(0)
        self.team.changed.connect(self.refresh)
        self.team.line.connect(self.add_line)
        self.team.notice.connect(self.show_notice)
        self.reload_members()
        self.refresh()

    def page(self, title):
        page = QWidget()
        page.setObjectName("panel")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(5, 4, 7, 4)
        layout.setSpacing(12)
        layout.addWidget(label(title, "title"))
        return page, layout

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
        row.addWidget(button("外观管理", self.desktop.engine.practice.panel.open_appearance))
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
        roster.addWidget(label(f"每轮最多 {MAX_SPEAKERS} 位发言。", "quiet"))
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
        self.task_button = button("分工协作", lambda: self.start("task"))
        self.send_button = button("发送", lambda: self.start("chat"), primary=True)
        for action in (self.stop_button, self.task_button, self.send_button):
            actions.addWidget(action)
        return page

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
        actions.addWidget(button("编辑", lambda: self.edit_member()))
        actions.addStretch()
        actions.addWidget(button("添加成员", lambda: self.edit_member(add=True), primary=True))
        return page

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
        layout.addWidget(label("开启后，桌面上可见的两位伙伴会定期各说一句。伴学或已有对话运行时会跳过。", "quiet"))
        enabled, interval = self.team.schedule()
        form = QFormLayout()
        layout.addLayout(form)
        self.casual_enabled = QCheckBox("启用定期闲聊")
        self.casual_enabled.setChecked(enabled)
        form.addRow(self.casual_enabled)
        self.casual_interval = QSpinBox()
        self.casual_interval.setRange(MIN_INTERVAL, MAX_INTERVAL)
        self.casual_interval.setSuffix(" 分钟")
        self.casual_interval.setValue(interval)
        form.addRow("间隔", self.casual_interval)
        actions = QHBoxLayout()
        layout.addLayout(actions)
        self.try_chat = button("聊一次", self.try_casual)
        actions.addWidget(self.try_chat)
        actions.addWidget(button("保存闲聊设置", self.save_schedule, primary=True))
        actions.addStretch()
        self.casual_notice = label("", "notice")
        layout.addWidget(self.casual_notice)
        layout.addStretch()
        return page

    def save_schedule(self):
        self.team.set_schedule(self.casual_enabled.isChecked(), self.casual_interval.value())
        self.casual_notice.setText("定时闲聊已开启。" if self.casual_enabled.isChecked() else "定时闲聊已关闭。")
        self.refresh()

    def try_casual(self):
        try:
            self.team.casual()
        except (ValueError, sqlite3.Error) as error:
            self.casual_notice.setText(str(error))

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
        for member in self.team.store.members():
            state = "显示在桌面" if member.visible and member.active else "未显示"
            item = QListWidgetItem(f"{member.name}　·　{state}\n{excerpt(member.role, 156)}")
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
        self.casual_notice.setText(message)

    def add_line(self, key, name, body):
        self.refresh_history()

    def refresh_history(self):
        kinds = {"task": "协作", "chat": "群聊", "casual": "闲聊"}
        rows = self.team.store.lines()
        self.history.setPlainText("\n\n".join(
            f"{row['speaker']} · {kinds[row['kind']]}\n{row['body']}" for row in rows))
        scrollbar = self.history.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def refresh(self):
        active = self.team.blocked
        self.mode.setText("当前对话正在伴学" if active else "桌面学伴待命")
        count = sum(member.visible and member.active for member in self.team.store.members())
        enabled, minutes = self.team.schedule()
        schedule = f"闲聊间隔 {minutes} 分钟" if enabled else "闲聊关闭"
        self.info.setText(f"{count} 位伙伴显示在桌面 · {schedule}")
        self.member_hint.setText(f"最多保存 {MAX_MEMBERS} 位成员。私聊和长期记忆分别保存。")
        self.exit_practice.setEnabled(active)
        for control in (self.send_button, self.task_button, self.try_chat):
            control.setEnabled(not active and not self.team.running)
        self.stop_button.setEnabled(self.team.running)
        self.refresh_history()
