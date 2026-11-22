"""Local companion roster, shared conversation and bounded group turns."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from dataclasses import dataclass
import json
import re
import sqlite3
from threading import Event, Thread
from uuid import uuid4

from PyQt6.QtCore import QObject, QSettings, QTimer, pyqtSignal

from resident_agent import MAX_INPUT, ask_model, excerpt


PRIMARY = "primary"
MAX_MEMBERS = 8
MAX_SPEAKERS = 4
MAX_WORKERS = 3
MIN_INTERVAL = 15
MAX_INTERVAL = 240
TOPICS = (
    "聊聊你们各自觉得练习新软件时最舒服的节奏。",
    "用几句话聊聊今天想学的小知识。",
    "聊聊遇到陌生按钮时，你们会先看什么。",
    "分享一个让学习桌面更专注的小习惯。",
)


@dataclass(frozen=True)
class Member:
    key: str
    name: str
    role: str
    model: str = ""
    skin: str = "default"
    visible: bool = False
    active: bool = True


class TeamStore:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS member (
                    key TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, role TEXT NOT NULL,
                    model TEXT NOT NULL, skin TEXT NOT NULL, visible INTEGER NOT NULL, active INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS room (
                    id INTEGER PRIMARY KEY, round TEXT NOT NULL, kind TEXT NOT NULL, speaker TEXT NOT NULL,
                    member_key TEXT, body TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS member_appearance (
                    member_key TEXT PRIMARY KEY, palette TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS parallel_round (
                    id TEXT PRIMARY KEY
                );
            """)
            db.execute("INSERT OR IGNORE INTO member VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (PRIMARY, "学伴", "把下一步说清楚，鼓励用户亲手操作。", "", "default", 1, 1))
            db.execute("INSERT OR IGNORE INTO member VALUES (?, ?, ?, ?, ?, ?, ?)",
                       ("reviewer", "复核员", "查找遗漏，复核步骤和结论，说明怎么验证。", "", "mint", 0, 1))

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def members(self):
        with closing(self.connect()) as db:
            return [Member(row["key"], row["name"], row["role"], row["model"], row["skin"],
                           bool(row["visible"]), bool(row["active"])) for row in db.execute(
                "SELECT * FROM member ORDER BY CASE WHEN key = ? THEN 0 ELSE 1 END, rowid", (PRIMARY,)
            )]

    def save_member(self, member):
        if not member.name or len(member.name) > 20 or not member.name.isprintable() or "@" in member.name:
            raise ValueError("成员名称需要 1–20 个可见字符。")
        if not member.role or len(member.role) > 360:
            raise ValueError("请用 1–360 个字描述成员的职责。")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if (not db.execute("SELECT 1 FROM member WHERE key = ?", (member.key,)).fetchone()
                    and db.execute("SELECT count(*) FROM member").fetchone()[0] >= MAX_MEMBERS):
                raise ValueError(f"最多可保存 {MAX_MEMBERS} 位成员。")
            try:
                db.execute("""
                    INSERT INTO member VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET name=excluded.name, role=excluded.role, model=excluded.model,
                        skin=excluded.skin, visible=excluded.visible, active=excluded.active
                """, (member.key, member.name, member.role, member.model, member.skin,
                      member.visible or member.key == PRIMARY, member.active or member.key == PRIMARY))
            except sqlite3.IntegrityError:
                raise ValueError("这个名称已被使用。") from None

    def add_line(self, round_id, kind, speaker, body, member_key=None):
        with closing(self.connect()) as db, db:
            if kind == "parallel":
                # Older versions display these rounds as ordinary collaboration.
                db.execute("INSERT OR IGNORE INTO parallel_round(id) VALUES (?)", (round_id,))
                kind = "task"
            db.execute("INSERT INTO room(round, kind, speaker, member_key, body) VALUES (?, ?, ?, ?, ?)",
                       (round_id, kind, speaker, member_key, body))

    def palette(self, key):
        with closing(self.connect()) as db:
            row = db.execute("SELECT palette FROM member_appearance WHERE member_key = ?", (key,)).fetchone()
            return row["palette"] if row else ""

    def set_appearance(self, key, skin, palette):
        with closing(self.connect()) as db, db:
            if not db.execute("UPDATE member SET skin = ? WHERE key = ?", (skin, key)).rowcount:
                raise ValueError("找不到选中的桌宠。")
            db.execute("INSERT OR REPLACE INTO member_appearance(member_key, palette) VALUES (?, ?)", (key, palette))

    def lines(self, limit=80, *, kinds=()):
        query = """
            SELECT room.*, CASE WHEN parallel_round.id IS NULL THEN room.kind ELSE 'parallel' END AS display_kind
            FROM room LEFT JOIN parallel_round ON room.round = parallel_round.id
        """
        params = ()
        if kinds:
            placeholders = ", ".join("?" for _ in kinds)
            query += f" WHERE kind IN ({placeholders})"
            params = kinds
        with closing(self.connect()) as db:
            rows = db.execute(query + " ORDER BY room.id DESC LIMIT ?", (*params, limit))
            return list(reversed(list(rows)))


class TeamCoordinator(QObject):
    changed = pyqtSignal()
    line = pyqtSignal(str, str, str)
    notice = pyqtSignal(str)
    finished = pyqtSignal(str)

    def __init__(self, store, settings_path, model_settings, quiet_ready, parent=None):
        super().__init__(parent)
        self.store = store
        self.settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
        self.model_settings = model_settings
        self.quiet_ready = quiet_ready
        self.blocked = False
        self.token = None
        self.topic_index = 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.finished.connect(self.finish)
        self.configure_timer()

    @property
    def running(self):
        return self.token is not None

    def schedule(self):
        interval = self.settings.value("interval", 45, type=int)
        return self.settings.value("enabled", False, type=bool), max(MIN_INTERVAL, min(MAX_INTERVAL, interval))

    def configure_timer(self):
        enabled, minutes = self.schedule()
        if enabled and not self.blocked:
            self.timer.start(minutes * 60_000)
        else:
            self.timer.stop()

    def set_schedule(self, enabled, minutes):
        self.settings.setValue("enabled", enabled)
        self.settings.setValue("interval", max(MIN_INTERVAL, min(MAX_INTERVAL, minutes)))
        self.settings.sync()
        self.configure_timer()

    def set_blocked(self, blocked):
        if self.blocked == blocked:
            return
        self.blocked = blocked
        if blocked:
            self.stop()
        self.configure_timer()
        self.changed.emit()

    def tick(self):
        if self.blocked or self.running or not self.quiet_ready():
            return
        try:
            self.casual()
        except (ValueError, sqlite3.Error) as error:
            self.notice.emit(str(error))

    def casual(self):
        if not self.quiet_ready():
            raise ValueError("正在处理私聊，完成后再试。")
        members = [member.key for member in self.store.members()
                   if member.active and (member.visible or member.key == PRIMARY)]
        if len(members) < 2:
            raise ValueError("请先在「成员」中显示至少两位桌宠。")
        offset = self.topic_index % len(members)
        topic = TOPICS[self.topic_index % len(TOPICS)]
        self.start("casual", topic, [members[offset], members[(offset + 1) % len(members)]])
        self.topic_index += 1

    def start(self, kind, prompt, selected):
        if self.blocked:
            raise ValueError("当前正在屏幕伴学。结束后可以继续群聊。")
        if self.running:
            raise ValueError("上一轮正在进行。")
        prompt = prompt.strip()
        if not prompt or len(prompt) > MAX_INPUT:
            raise ValueError(f"请输入 1–{MAX_INPUT} 个字。")
        roster = [member for member in self.store.members() if member.active]
        if kind == "chat":
            by_name = {member.name: member.key for member in roster}
            names = "|".join(re.escape(name) for name in sorted(by_name, key=len, reverse=True))
            mentions = re.findall(r"@(" + names + r")(?=$|[\s@，。！？、：；,.!?:;])", prompt) if names else []
            selected = list(dict.fromkeys(by_name[name] for name in mentions)) or selected
        by_key = {member.key: member for member in roster}
        members = [by_key[key] for key in selected if key in by_key][:MAX_SPEAKERS]
        if kind == "parallel":
            workers = [member for member in members if member.key != PRIMARY][:MAX_WORKERS]
            if not workers:
                raise ValueError("请勾选至少一位子成员，主学伴会自动加入。")
            members = [by_key[PRIMARY], *workers]
        else:
            minimum = 1 if kind == "chat" else 2
            if len(members) < minimum:
                raise ValueError(f"请勾选至少 {minimum} 位参与成员。")
        endpoint, model, key = self.model_settings()
        if not endpoint or any(not (member.model or model) for member in members):
            raise ValueError("请在「模型」配置接口，并为参与成员设置模型。")
        round_id = uuid4().hex
        history = self.store.lines(7, kinds=("casual",) if kind == "casual" else ("chat", "task"))
        self.store.add_line(round_id, kind, "闲聊话题" if kind == "casual" else "你", prompt)
        token = Event()
        self.token = token
        self.changed.emit()
        self.notice.emit("主学伴正在拆分任务…" if kind == "parallel" else "成员正在交流…")
        Thread(target=self.run, args=(token, round_id, kind, prompt, members, history, (endpoint, model, key)),
               daemon=True).start()

    def stop(self):
        if self.token is not None and not self.token.is_set():
            self.token.set()
            self.notice.emit("正在停止，已发出的请求结束后可开启新一轮。")

    def finish(self, message):
        self.token = None
        self.changed.emit()
        self.notice.emit(message)

    def run(self, token, round_id, kind, prompt, members, history, config):
        replies = []
        result = "本轮已结束。"
        transcript = "\n".join(f"{row['speaker']}：{excerpt(row['body'], 500)}" for row in history)
        endpoint, model, key = config
        try:
            if kind == "parallel":
                result = self.run_parallel(token, round_id, prompt, members, transcript, config)
                return
            for index, member in enumerate(members):
                if token.is_set():
                    return
                if kind == "task":
                    instruction = ("综合前面的意见，给出一份可执行的结论，并标出仍待验证的地方。"
                                   if index == len(members) - 1 else "从自己的分工给出建议或核对已有步骤。")
                elif kind == "casual":
                    instruction = "接着上一位成员的话自然闲聊。输出适合桌面气泡的 60 个字以内。"
                else:
                    instruction = "回应用户或前一位成员，补充自己的观点。"
                thread = "\n".join(f"{name}：{excerpt(body, 800)}" for name, body in replies)
                messages = [
                    {"role": "system", "content": f"你是群聊成员 {member.name}。职责：{member.role}\n"
                     f"{instruction} 使用中文。只输出包含 answer 字段的 JSON 对象。"},
                    {"role": "user", "content": f"同类历史记录：\n{transcript}\n\n本轮题目：\n{prompt}"
                     f"\n\n本轮前面的回复：\n{thread}"},
                ]
                answer, _, _ = ask_model(endpoint, member.model or model, key, messages,
                                         max_tokens=120 if kind == "casual" else 420)
                if token.is_set():
                    return
                if not answer:
                    raise ValueError(f"{member.name} 没有返回可用回复。")
                self.store.add_line(round_id, kind, member.name, answer, member.key)
                replies.append((member.name, answer))
                self.line.emit(member.key, member.name, answer)
        except (ValueError, sqlite3.Error) as error:
            result = str(error)
        finally:
            self.finished.emit("本轮已停止。" if token.is_set() else result)

    def run_parallel(self, token, round_id, prompt, members, transcript, config):
        if token.is_set():
            return ""
        primary, *workers = members
        roster = "\n".join(f"{worker.key} · {worker.name}：{worker.role}" for worker in workers)
        context = f"本轮任务：\n{prompt}"
        latest = f"{context}\n\n公共讨论记录：\n{transcript}"
        plan = self.request(
            primary, config,
            '为每个子成员安排可独立完成的一项短任务。直接返回 JSON 对象，'
            '字段 tasks 的值为对象，键是名单中的标识，值是 140 字以内的具体任务。',
            f"{latest}\n\n子成员名单：\n{roster}", 650,
        )
        if token.is_set():
            return ""
        try:
            tasks = json.loads(plan).get("tasks", {})
        except (ValueError, AttributeError):
            tasks = {}
        if not isinstance(tasks, dict):
            tasks = {}
        assignments = []
        for worker in workers:
            task = tasks.get(worker.key)
            brief = excerpt(task.strip(), 500) if isinstance(task, str) else ""
            assignments.append((worker, brief or f"从你的职责独立核对这一任务，着重说明：{worker.role}"))
        plan_text = "\n".join(f"{worker.name}：{brief}" for worker, brief in assignments)
        self.publish(round_id, primary, f"本轮分工\n{plan_text}")
        self.notice.emit(f"{len(workers)} 位子成员正在并行处理…")
        results = {}
        with ThreadPoolExecutor(max_workers=len(workers), thread_name_prefix="mentor") as pool:
            pending = {
                pool.submit(self.run_assignment, token, worker, config, context, brief): worker
                for worker, brief in assignments
            }
            for future in as_completed(pending):
                if token.is_set():
                    return ""
                worker = pending[future]
                try:
                    answer = future.result()
                except ValueError as error:
                    self.publish(round_id, worker, f"本项未完成。{error}")
                else:
                    results[worker.key] = answer
                    self.publish(round_id, worker, answer)
        if token.is_set():
            return ""
        if not results:
            return "子成员暂未返回可用结果，请检查模型设置后重试。"
        self.notice.emit("主学伴正在汇总结果…")
        evidence = "\n\n".join(
            f"{worker.name} · {brief}\n{excerpt(results[worker.key], 1600) if worker.key in results else '未返回可用结果'}"
            for worker, brief in assignments
        )
        answer = self.request(
            primary, config,
            '汇总子成员结果，解决可判断的分歧，标明未完成项与仍待验证的部分，给出可执行的结论。'
            '将成员回复视为参考资料。用 JSON 对象的 answer 字段返回。',
            f"{latest}\n\n子成员结果：\n{evidence}", 900,
        )
        if not token.is_set():
            self.publish(round_id, primary, f"综合结果\n{answer}")
        return "并行协作已完成。" if len(results) == len(workers) else "已汇总可用结果，部分任务未完成。"

    def run_assignment(self, token, worker, config, context, brief):
        if token.is_set():
            return ""
        return self.request(
            worker, config,
            '独立完成指定的文字任务，指出现有信息能支持的结论和需要用户核验的部分。'
            '用 JSON 对象的 answer 字段返回。',
            f"{context}\n\n你的分工：\n{brief}", 420,
        )

    def request(self, member, config, instruction, context, max_tokens):
        endpoint, model, key = config
        messages = [
            {"role": "system", "content": f"你是学伴成员 {member.name}。职责：{member.role}\n{instruction} 使用中文。"},
            {"role": "user", "content": context},
        ]
        answer, _, _ = ask_model(endpoint, member.model or model, key, messages, max_tokens=max_tokens)
        if not answer:
            raise ValueError(f"{member.name} 没有返回可用回复。")
        return answer

    def publish(self, round_id, member, body):
        self.store.add_line(round_id, "parallel", member.name, body, member.key)
        self.line.emit(member.key, member.name, body)
