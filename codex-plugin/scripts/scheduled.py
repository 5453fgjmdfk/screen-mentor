"""Persistent local schedules for model-only desktop tasks."""
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
import sqlite3
from uuid import uuid4

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from resident_agent import MAX_INPUT
from team import PRIMARY


FREQUENCIES = {0: "仅一次", 1: "每天", 3: "每 3 天", 7: "每周", 14: "每两周", 30: "每 30 天"}
MODES = {"chat": "主学伴", "parallel": "并行协作"}


def timestamp(value):
    return value.isoformat(timespec="seconds")


@dataclass(frozen=True)
class ScheduledTask:
    key: str
    name: str
    prompt: str
    next_at: datetime
    repeat_days: int
    mode: str
    enabled: bool = True
    last_round: str = ""
    last_status: str = ""


class TaskScheduler(QObject):
    changed = pyqtSignal()
    notice = pyqtSignal(str)

    def __init__(self, team, parent=None):
        super().__init__(parent)
        self.team = team
        self.connect = team.store.connect
        self.active_round = ""
        with closing(self.connect()) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_task (
                    key TEXT PRIMARY KEY, name TEXT NOT NULL, prompt TEXT NOT NULL, next_at TEXT NOT NULL,
                    repeat_days INTEGER NOT NULL, mode TEXT NOT NULL, enabled INTEGER NOT NULL,
                    last_round TEXT NOT NULL DEFAULT '', last_status TEXT NOT NULL DEFAULT ''
                )
            """)
            db.execute("UPDATE scheduled_task SET last_status = ? WHERE last_status = ?",
                       ("上次运行中断。", "运行中…"))
        self.team.finished.connect(self.completed)
        self.timer = QTimer(self)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(self.check)
        self.timer.start()

    @staticmethod
    def from_row(row):
        return ScheduledTask(row["key"], row["name"], row["prompt"], datetime.fromisoformat(row["next_at"]),
                             row["repeat_days"], row["mode"], bool(row["enabled"]), row["last_round"], row["last_status"])

    def tasks(self):
        with closing(self.connect()) as db:
            rows = db.execute("SELECT * FROM scheduled_task ORDER BY enabled DESC, next_at, rowid")
            return [self.from_row(row) for row in rows]

    def save(self, task):
        if not task.name.strip() or len(task.name) > 50 or not task.name.isprintable():
            raise ValueError("请填写 1–50 字的任务名称。")
        if not task.prompt.strip() or len(task.prompt) > MAX_INPUT:
            raise ValueError(f"请填写 1–{MAX_INPUT} 字的任务内容。")
        if task.repeat_days not in FREQUENCIES or task.mode not in MODES:
            raise ValueError("请选择任务频率和执行方式。")
        with closing(self.connect()) as db, db:
            db.execute("""
                INSERT INTO scheduled_task(key, name, prompt, next_at, repeat_days, mode, enabled)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET name=excluded.name, prompt=excluded.prompt, next_at=excluded.next_at,
                    repeat_days=excluded.repeat_days, mode=excluded.mode, enabled=excluded.enabled
            """, (task.key, task.name, task.prompt, timestamp(task.next_at), task.repeat_days, task.mode, task.enabled))
        self.changed.emit()

    def remove(self, key):
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM scheduled_task WHERE key = ?", (key,))
        self.changed.emit()

    def claim(self, now):
        """Advance the schedule before starting the model requests, once per local due time."""
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""
                SELECT * FROM scheduled_task WHERE enabled = 1 AND next_at <= ? ORDER BY next_at, rowid LIMIT 1
            """, (timestamp(now),)).fetchone()
            if row is None:
                return None
            task = self.from_row(row)
            next_at = task.next_at
            if task.repeat_days:
                interval = timedelta(days=task.repeat_days)
                next_at += interval * ((now - next_at) // interval + 1)
            round_id = uuid4().hex
            saved = db.execute("""
                UPDATE scheduled_task SET enabled=?, next_at=?, last_round=?, last_status=?
                WHERE key=? AND enabled=1 AND next_at=?
            """, (bool(task.repeat_days), timestamp(next_at), round_id, "运行中…", task.key, row["next_at"]))
            return (task, round_id) if saved.rowcount else None

    def run_now(self, key):
        if self.team.blocked:
            raise ValueError("当前正在屏幕伴学。结束后可以执行定时任务。")
        if self.team.running or not self.team.quiet_ready():
            raise ValueError("当前已有对话正在进行，请完成后再试。")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM scheduled_task WHERE key = ?", (key,)).fetchone()
            if row is None:
                raise ValueError("找不到选中的定时任务。")
            task = self.from_row(row)
            round_id = uuid4().hex
            db.execute("UPDATE scheduled_task SET last_round=?, last_status=? WHERE key=?",
                       (round_id, "运行中…", task.key))
        self.active_round = round_id
        try:
            choices = [member.key for member in self.team.store.members() if member.active]
            selected = [PRIMARY] if task.mode == "chat" else choices
            self.team.start(task.mode, task.prompt, selected, scheduled=(self.active_round, task.name))
        except (ValueError, sqlite3.Error) as error:
            self.completed(f"执行失败。{error}")
            raise ValueError(str(error)) from error
        self.changed.emit()

    def check(self, now=None):
        if self.team.blocked or self.team.running or not self.team.quiet_ready():
            return
        try:
            claimed = self.claim(now or datetime.now())
            if claimed is None:
                return
            task, self.active_round = claimed
            choices = [member.key for member in self.team.store.members() if member.active]
            selected = [PRIMARY] if task.mode == "chat" else choices
            self.team.start(task.mode, task.prompt, selected, scheduled=(self.active_round, task.name))
        except (ValueError, sqlite3.Error) as error:
            if self.active_round:
                self.completed(f"执行失败。{error}")
            else:
                self.notice.emit(str(error))
        else:
            self.changed.emit()

    def completed(self, message):
        if not self.active_round:
            return
        try:
            with closing(self.connect()) as db, db:
                db.execute("UPDATE scheduled_task SET last_status = ? WHERE last_round = ?", (message, self.active_round))
        except sqlite3.Error as error:
            self.notice.emit(str(error))
        self.active_round = ""
        self.changed.emit()

    def stop(self):
        self.timer.stop()
        self.completed("退出桌面学伴时已停止。")
