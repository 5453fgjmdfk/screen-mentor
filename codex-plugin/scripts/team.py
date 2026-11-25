"""Local companion roster, shared conversation and bounded group turns."""
from contextlib import closing
from dataclasses import dataclass, replace
import json
from queue import Queue
import re
import sqlite3
from threading import Event, Thread
from uuid import uuid4

from PyQt6.QtCore import QObject, QSettings, QTimer, pyqtSignal

from resident_agent import MAX_INPUT, ask_model, excerpt, extract_json_object, model_url, valid_key_name


PRIMARY = "primary"
MAX_MEMBERS = 8
MAX_ROLE = 360
MAX_PERSONALITY = 400
MAX_PERSONA = 800
MAX_SPEAKERS = 4
MAX_WORKERS = 3
MIN_INTERVAL = 15
MAX_INTERVAL = 240
MAX_TOPICS = 30
MAX_TOPIC_LENGTH = 120
DEFAULT_ROOM = "desktop"
ROOM_MODES = {"rotate": "双人轮换", "everyone": "全组依次发言", "lead": "主持人先发言"}
MENTION_START = r"(?<![A-Za-z0-9_.+%-])@"
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
    personality: str = ""
    persona: str = ""
    endpoint: str = ""
    own_model: str = ""
    key_name: str = ""

    def connection(self, defaults):
        if self.endpoint:
            return self.endpoint, self.own_model, self.key_name
        endpoint, model, key_name = defaults
        return endpoint, self.model or model, key_name

    def identity(self):
        parts = [f"你的名字是{self.name}。", f"工作职责：{self.role}"]
        if self.personality:
            parts.append(f"沟通性格和表达方式：{self.personality}")
        if self.persona:
            parts.append(f"角色设定（虚构背景）：{self.persona}")
        return "\n".join(parts)


@dataclass(frozen=True)
class CasualRoom:
    key: str
    name: str = ""
    guidance: str = ""
    topics: str = ""
    members: tuple[str, ...] = ()
    follow_visible: bool = False
    mode: str = "rotate"
    lead: str = PRIMARY
    automatic: bool = False
    minutes: int = 45


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
                CREATE TABLE IF NOT EXISTS member_profile (
                    member_key TEXT PRIMARY KEY, personality TEXT NOT NULL, persona TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS member_connection (
                    member_key TEXT PRIMARY KEY, endpoint TEXT NOT NULL, model TEXT NOT NULL, key_name TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS parallel_round (
                    id TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS casual_room (
                    key TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, guidance TEXT NOT NULL,
                    topics TEXT NOT NULL, members TEXT NOT NULL, follow_visible INTEGER NOT NULL,
                    mode TEXT NOT NULL, lead TEXT NOT NULL, automatic INTEGER NOT NULL, minutes INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS casual_round (
                    round_id TEXT PRIMARY KEY, room_key TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS room_by_round ON room(round);
                CREATE INDEX IF NOT EXISTS room_group_history ON room(id) WHERE kind IN ('chat', 'task');
                CREATE INDEX IF NOT EXISTS casual_round_by_room ON casual_round(room_key);
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
                           bool(row["visible"]), bool(row["active"]), row["personality"] or "", row["persona"] or "",
                           row["endpoint"] or "", row["own_model"] or "", row["key_name"] or "")
                    for row in db.execute("""
                        SELECT member.*, member_profile.personality, member_profile.persona,
                               member_connection.endpoint, member_connection.model AS own_model, member_connection.key_name
                        FROM member LEFT JOIN member_profile ON member.key = member_profile.member_key
                        LEFT JOIN member_connection ON member.key = member_connection.member_key
                        ORDER BY CASE WHEN member.key = ? THEN 0 ELSE 1 END, member.rowid
                    """, (PRIMARY,))]

    def save_member(self, member, *, install_skin=None):
        if not member.name or len(member.name) > 20 or not member.name.isprintable() or "@" in member.name:
            raise ValueError("成员名称需要 1–20 个可见字符。")
        if not member.role or len(member.role) > MAX_ROLE:
            raise ValueError(f"请用 1–{MAX_ROLE} 个字描述成员的职责。")
        if len(member.personality) > MAX_PERSONALITY or len(member.persona) > MAX_PERSONA:
            raise ValueError(f"性格最多 {MAX_PERSONALITY} 字，角色设定最多 {MAX_PERSONA} 字。")
        if len(member.model) > 120 or len(member.own_model) > 120 or len(member.endpoint) > 1024:
            raise ValueError("模型名称最多 120 字，接口地址最多 1024 字。")
        if member.endpoint:
            model_url(member.endpoint)
            if not member.own_model:
                raise ValueError("使用独立接口时，请填写该学伴的模型名。")
            if not valid_key_name(member.key_name):
                raise ValueError("请填环境变量名，例如 LOCAL_MENTOR_KEY。")
        elif member.own_model or member.key_name:
            raise ValueError("使用独立模型时，请填写该学伴的接口地址。")
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
            if install_skin is not None:
                skin = install_skin()
                member = replace(member, skin=skin.key)
                db.execute("UPDATE member SET skin=? WHERE key=?", (skin.key, member.key))
            db.execute("""
                INSERT INTO member_profile(member_key, personality, persona) VALUES (?, ?, ?)
                ON CONFLICT(member_key) DO UPDATE SET personality=excluded.personality, persona=excluded.persona
            """, (member.key, member.personality, member.persona))
            if member.endpoint:
                db.execute("""
                    INSERT INTO member_connection(member_key, endpoint, model, key_name) VALUES (?, ?, ?, ?)
                    ON CONFLICT(member_key) DO UPDATE SET
                        endpoint=excluded.endpoint, model=excluded.model, key_name=excluded.key_name
                """, (member.key, member.endpoint, member.own_model, member.key_name))
            else:
                db.execute("DELETE FROM member_connection WHERE member_key=?", (member.key,))
        return member

    def remove_member(self, key):
        if key == PRIMARY:
            raise ValueError("主学伴不能删除。")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("DELETE FROM member WHERE key = ?", (key,)).rowcount:
                raise ValueError("找不到要删除的学伴。")
            for table in ("member_profile", "member_connection", "member_appearance"):
                db.execute(f"DELETE FROM {table} WHERE member_key = ?", (key,))
            for row in db.execute("SELECT key, members, lead FROM casual_room").fetchall():
                members = [item for item in json.loads(row["members"]) if item != key]
                lead = PRIMARY if row["lead"] == key else row["lead"]
                if members != json.loads(row["members"]) or lead != row["lead"]:
                    db.execute("UPDATE casual_room SET members = ?, lead = ? WHERE key = ?",
                               (json.dumps(members), lead, row["key"]))

    def reset_skin(self, skin_key):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE member SET skin = 'default' WHERE skin = ?", (skin_key,))

    def add_line(self, round_id, kind, speaker, body, member_key=None, *, casual_key=None):
        with closing(self.connect()) as db, db:
            if kind == "parallel":
                # Older versions display these rounds as ordinary collaboration.
                db.execute("INSERT OR IGNORE INTO parallel_round(id) VALUES (?)", (round_id,))
                kind = "task"
            if casual_key:
                db.execute("INSERT OR IGNORE INTO casual_round(round_id, room_key) VALUES (?, ?)",
                           (round_id, casual_key))
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

    def rooms(self):
        with closing(self.connect()) as db:
            return [CasualRoom(row["key"], row["name"], row["guidance"], row["topics"], tuple(json.loads(row["members"])),
                               bool(row["follow_visible"]), row["mode"], row["lead"], bool(row["automatic"]), row["minutes"])
                    for row in db.execute("SELECT * FROM casual_room ORDER BY rowid")]

    def remove_room(self, key):
        if key == DEFAULT_ROOM:
            raise ValueError("默认闲聊房间不能删除。")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("DELETE FROM casual_room WHERE key = ?", (key,)).rowcount:
                raise ValueError("找不到要删除的闲聊房间。")
            db.execute(
                "DELETE FROM room WHERE kind = 'casual' AND round IN (SELECT round_id FROM casual_round WHERE room_key = ?)",
                (key,),
            )
            db.execute("DELETE FROM casual_round WHERE room_key = ?", (key,))

    def save_room(self, room, *, initialize=False):
        with closing(self.connect()) as db, db:
            if initialize and db.execute("SELECT 1 FROM casual_room WHERE key=?", (room.key,)).fetchone():
                return
            if room.automatic:
                db.execute("UPDATE casual_room SET automatic=0 WHERE key <> ?", (room.key,))
            try:
                db.execute("""
                    INSERT INTO casual_room VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET name=excluded.name, guidance=excluded.guidance, topics=excluded.topics,
                        members=excluded.members, follow_visible=excluded.follow_visible, mode=excluded.mode,
                        lead=excluded.lead, automatic=excluded.automatic, minutes=excluded.minutes
                """, (room.key, room.name, room.guidance, room.topics, json.dumps(room.members), room.follow_visible,
                      room.mode, room.lead, room.automatic, room.minutes))
            except sqlite3.IntegrityError:
                raise ValueError("已有同名闲聊房间。") from None

    def lines(self, limit=80, *, kinds=(), round_id=None, casual_key=None):
        query = """
            SELECT room.*, CASE WHEN parallel_round.id IS NULL THEN room.kind ELSE 'parallel' END AS display_kind
            FROM {source} LEFT JOIN parallel_round ON room.round = parallel_round.id
        """
        where = []
        params = []
        if kinds == ("chat", "task"):
            # Keep the predicate identical to the partial index for the group history.
            where.append("room.kind IN ('chat', 'task')")
        elif kinds:
            placeholders = ", ".join("?" for _ in kinds)
            where.append(f"kind IN ({placeholders})")
            params.extend(kinds)
        if round_id:
            where.append("room.round = ?")
            params.append(round_id)
        if casual_key:
            query += " LEFT JOIN casual_round ON room.round = casual_round.round_id "
            where.append("room.kind = 'casual'")
            if casual_key == DEFAULT_ROOM:
                where.append("COALESCE(casual_round.room_key, ?) = ?")
                params.extend((DEFAULT_ROOM, casual_key))
            else:
                where.append("casual_round.room_key = ?")
                params.append(casual_key)
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY room.id DESC LIMIT ?"
        sql_limit = -1 if limit is None else limit
        with closing(self.connect()) as db:
            if casual_key and casual_key != DEFAULT_ROOM and limit is not None and limit > 0:
                # Active rooms can fill the page from a short tail scan, without sorting all their history.
                recent = "(SELECT id FROM room ORDER BY id DESC LIMIT ?) AS recent JOIN room ON room.id = recent.id"
                rows = db.execute(query.format(source=recent), (limit * 2, *params, limit)).fetchall()
                if len(rows) == limit:
                    return list(reversed(rows))
            rows = db.execute(query.format(source="room"), (*params, sql_limit))
            return list(reversed(list(rows)))


class TeamCoordinator(QObject):
    changed = pyqtSignal()
    line = pyqtSignal(str, str, str)
    notice = pyqtSignal(str)
    room_notice = pyqtSignal(str, str)
    finished = pyqtSignal(str)

    def __init__(self, store, settings_path, model_settings, quiet_ready, parent=None):
        super().__init__(parent)
        self.store = store
        self.settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
        self.model_settings = model_settings
        self.quiet_ready = quiet_ready
        self.blocked = False
        self.token = None
        self.turns = {}
        self.topics_played = {}
        self.active_room = ""
        legacy_minutes = self.settings.value("interval", 45, type=int)
        self.store.save_room(CasualRoom(
            DEFAULT_ROOM, "桌面闲聊", topics=self.settings.value("casual_topics", "", type=str),
            follow_visible=True, automatic=self.settings.value("enabled", False, type=bool),
            minutes=max(MIN_INTERVAL, min(MAX_INTERVAL, legacy_minutes)),
        ), initialize=True)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.finished.connect(self.finish)
        self.configure_timer()

    @property
    def running(self):
        return self.token is not None

    def automatic_room(self):
        return next((room for room in self.store.rooms() if room.automatic), None)

    def configure_timer(self):
        room = self.automatic_room()
        if room and not self.blocked:
            self.timer.start(room.minutes * 60_000)
        else:
            self.timer.stop()

    def save_room(self, room):
        if not room.name or len(room.name) > 32 or not room.name.isprintable():
            raise ValueError("请填写 1–32 字的房间名称。")
        if len(room.guidance) > 400:
            raise ValueError("房间提示最多 400 字。")
        if room.mode not in ROOM_MODES or not MIN_INTERVAL <= room.minutes <= MAX_INTERVAL:
            raise ValueError("请选择有效的发言方式和闲聊间隔。")
        self.parse_topics(room.topics)
        if not room.follow_visible and len(room.members) < 2:
            raise ValueError("请选择至少两位成员。")
        if room.mode == "lead" and not room.follow_visible and room.lead not in room.members:
            raise ValueError("主持人需要加入本房间。")
        previous = next((entry for entry in self.store.rooms() if entry.key == room.key), None)
        self.store.save_room(room)
        if previous is None or previous.topics != room.topics:
            self.topics_played.pop(room.key, None)
        self.configure_timer()
        self.changed.emit()

    def remove_room(self, key):
        if self.running and self.active_room == key:
            raise ValueError("该房间正在闲聊，请先停止后再删除。")
        self.store.remove_room(key)
        self.turns.pop(key, None)
        self.topics_played.pop(key, None)
        self.configure_timer()
        self.changed.emit()

    @staticmethod
    def parse_topics(text):
        topics = tuple(line.strip() for line in text.splitlines() if line.strip())
        if len(topics) > MAX_TOPICS or any(len(topic) > MAX_TOPIC_LENGTH for topic in topics):
            raise ValueError(f"最多填写 {MAX_TOPICS} 条，每条不超过 {MAX_TOPIC_LENGTH} 字。")
        return topics

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
        room = None
        try:
            room = self.automatic_room()
            if room:
                self.casual(room)
        except (ValueError, sqlite3.Error) as error:
            if room:
                self.room_notice.emit(room.key, str(error))
            else:
                self.notice.emit(str(error))

    @staticmethod
    def is_broadcast(prompt):
        return bool(re.search(MENTION_START + r"(?:all|everyone)(?!\w)", prompt, flags=re.IGNORECASE))

    @staticmethod
    def mentions(prompt, roster):
        by_name = {member.name: member.key for member in roster}
        folded = {}
        for name, key in by_name.items():
            folded.setdefault(name.casefold(), key)
        names = "|".join(re.escape(name) for name in sorted(by_name, key=len, reverse=True))
        if not names:
            return []
        hits = re.findall(MENTION_START + r"(" + names + r")(?=$|[\s@，。！？、：；,.!?:;])",
                          prompt, flags=re.IGNORECASE)
        keys = (by_name.get(name) or folded.get(name.casefold()) for name in hits)
        return list(dict.fromkeys(key for key in keys if key is not None))

    def room_members(self, room):
        roster = self.store.members()
        return [member for member in roster if member.active
                and (member.visible if room.follow_visible else member.key in room.members)]

    def casual(self, room, prompt=None):
        if not self.quiet_ready():
            raise ValueError("正在处理私聊，完成后再试。")
        members = [member.key for member in self.room_members(room)]
        if len(members) < 2:
            raise ValueError("本房间需要至少两位可参与的成员，请检查房间设置。")
        offset = self.turns.get(room.key, 0) % len(members)
        order = members[offset:] + members[:offset]
        topic_step = self.topics_played.get(room.key, 0)
        topics = self.parse_topics(room.topics) or TOPICS
        topic = prompt if prompt is not None else topics[topic_step % len(topics)]
        targeted = self.mentions(topic, self.store.members()) if prompt is not None else []
        broadcast = prompt is not None and self.is_broadcast(topic)
        if broadcast:
            selected = order[:MAX_SPEAKERS]
        elif targeted:
            if any(key not in members for key in targeted):
                raise ValueError("点名的学伴不在本房间，或已停用。")
            selected = targeted[:MAX_SPEAKERS]
        elif room.mode == "everyone":
            selected = order[:MAX_SPEAKERS]
        elif room.mode == "lead":
            if room.lead not in members:
                raise ValueError("本房间的主持人暂时不可参与，请检查成员设置。")
            selected = [room.lead, next(key for key in order if key != room.lead)]
        else:
            selected = order[:2]
        self.start("casual", topic, selected, room=room, human_post=prompt is not None)
        self.turns[room.key] = self.turns.get(room.key, 0) + 1
        if prompt is None:
            self.topics_played[room.key] = topic_step + 1

    def start(self, kind, prompt, selected, *, scheduled=None, room=None, human_post=False):
        if self.blocked:
            raise ValueError("当前正在屏幕伴学。结束后可以继续群聊。")
        if self.running:
            raise ValueError("上一轮正在进行。")
        prompt = prompt.strip()
        if not prompt or len(prompt) > MAX_INPUT:
            raise ValueError(f"请输入 1–{MAX_INPUT} 个字。")
        roster = [member for member in self.store.members() if member.active]
        if kind == "chat" and not scheduled:
            if self.is_broadcast(prompt):
                selected = [member.key for member in roster][:MAX_SPEAKERS]
            else:
                selected = self.mentions(prompt, roster) or selected
        by_key = {member.key: member for member in roster}
        members = [by_key[key] for key in selected if key in by_key][:MAX_SPEAKERS]
        if kind == "parallel":
            workers = [member for member in members if member.key != PRIMARY][:MAX_WORKERS]
            if not workers:
                raise ValueError("请勾选至少一位子成员，主学伴会自动加入。")
            members = [by_key[PRIMARY], *workers]
        else:
            minimum = 1 if kind in {"chat", "casual"} else 2
            if len(members) < minimum:
                raise ValueError(f"请勾选至少 {minimum} 位参与成员。")
        defaults = self.model_settings()
        connections = {member.key: member.connection(defaults) for member in members}
        for member in members:
            endpoint, model, _ = connections[member.key]
            if not endpoint or not model:
                raise ValueError(f"「{member.name}」还没有可用的模型配置，请在「模型」页设置。")
        if scheduled:
            round_id, name = scheduled
            speaker = f"定时 · {name}"
            history = []
        elif room:
            round_id = uuid4().hex
            speaker = "你" if human_post else "闲聊话题"
            history = self.store.lines(7, casual_key=room.key)
        else:
            round_id = uuid4().hex
            speaker = "你"
            history = self.store.lines(7, kinds=("chat", "task"))
        self.store.add_line(round_id, kind, speaker, prompt, casual_key=room.key if room else None)
        token = Event()
        self.token = token
        self.active_room = room.key if room else ""
        self.changed.emit()
        self.report("主学伴正在拆分任务…" if kind == "parallel" else "成员正在交流…")
        Thread(target=self.run, args=(token, round_id, kind, prompt, members, history, connections,
                                    room.guidance if room else ""),
               daemon=True).start()

    def report(self, message):
        if self.active_room:
            self.room_notice.emit(self.active_room, message)
        else:
            self.notice.emit(message)

    def stop(self):
        if self.token is not None and not self.token.is_set():
            self.token.set()
            self.report("正在停止，已发出的请求结束后可开启新一轮。")

    def finish(self, message):
        self.token = None
        self.report(message)
        self.active_room = ""
        self.changed.emit()

    def run(self, token, round_id, kind, prompt, members, history, connections, guidance):
        replies = []
        result = "本轮已结束。"
        transcript = "\n".join(f"{row['speaker']}：{excerpt(row['body'], 500)}" for row in history)
        try:
            if kind == "parallel":
                result = self.run_parallel(token, round_id, prompt, members, transcript, connections)
                return
            for index, member in enumerate(members):
                if token.is_set():
                    return
                if kind == "task":
                    instruction = ("综合前面的意见，给出一份可执行的结论，并标出仍待验证的地方。"
                                   if index == len(members) - 1 else "从自己的分工给出建议或核对已有步骤。")
                elif kind == "casual":
                    instruction = "围绕本轮话题接话。输出适合桌面气泡的 60 个字以内。"
                    if guidance:
                        instruction += f" 房间提示：{guidance}"
                else:
                    instruction = "回应用户或前一位成员，补充自己的观点。"
                thread = "\n".join(f"{name}：{excerpt(body, 800)}" for name, body in replies)
                messages = [
                    {"role": "system", "content": f"你是群聊成员 {member.name}。\n{member.identity()}\n"
                     f"{instruction} 使用中文。只输出包含 answer 字段的 JSON 对象。"},
                    {"role": "user", "content": f"同类历史记录：\n{transcript}\n\n本轮题目：\n{prompt}"
                     f"\n\n本轮前面的回复：\n{thread}"},
                ]
                try:
                    answer, _, _ = ask_model(*connections[member.key], messages,
                                             max_tokens=120 if kind == "casual" else 420, cancelled=token.is_set)
                except ValueError as error:
                    raise ValueError(f"{member.name}：{error}") from error
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

    def run_parallel(self, token, round_id, prompt, members, transcript, connections):
        if token.is_set():
            return ""
        primary, *workers = members
        roster = "\n".join(f"{worker.key} · {worker.name}：{worker.role}" for worker in workers)
        context = f"本轮任务：\n{prompt}"
        latest = f"{context}\n\n公共讨论记录：\n{transcript}"
        plan = self.request(
            primary, connections[primary.key],
            '为每个子成员安排可独立完成的一项短任务。直接返回 JSON 对象，'
            '字段 tasks 的值为对象，键是名单中的标识，值是 140 字以内的具体任务。',
            f"{latest}\n\n子成员名单：\n{roster}", 650, cancelled=token.is_set,
        )
        if token.is_set():
            return ""
        parsed_plan = extract_json_object(plan)
        tasks = parsed_plan.get("tasks", {}) if parsed_plan else {}
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
        completed = Queue()

        def request_assignment(worker, brief):
            try:
                answer = self.run_assignment(token, worker, connections[worker.key], context, brief)
            except ValueError as error:
                completed.put((worker, "", str(error)))
            except Exception:
                completed.put((worker, "", "子成员未能返回结果。"))
            else:
                completed.put((worker, answer, ""))

        for worker, brief in assignments:
            Thread(target=request_assignment, args=(worker, brief), daemon=True).start()
        for _ in assignments:
            worker, answer, error = completed.get()
            if token.is_set():
                continue
            if error:
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
            primary, connections[primary.key],
            '汇总子成员结果，解决可判断的分歧，标明未完成项与仍待验证的部分，给出可执行的结论。'
            '将成员回复视为参考资料。用 JSON 对象的 answer 字段返回。',
            f"{latest}\n\n子成员结果：\n{evidence}", 900, cancelled=token.is_set,
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
            f"{context}\n\n你的分工：\n{brief}", 420, cancelled=token.is_set,
        )

    def request(self, member, config, instruction, context, max_tokens, *, cancelled=lambda: False):
        messages = [
            {"role": "system", "content": f"你是学伴成员 {member.name}。\n{member.identity()}\n{instruction} 使用中文。"},
            {"role": "user", "content": context},
        ]
        try:
            answer, _, _ = ask_model(*config, messages, max_tokens=max_tokens, cancelled=cancelled)
        except ValueError as error:
            raise ValueError(f"{member.name}：{error}") from error
        if cancelled():
            return ""
        if not answer:
            raise ValueError(f"{member.name} 没有返回可用回复。")
        return answer

    def publish(self, round_id, member, body):
        self.store.add_line(round_id, "parallel", member.name, body, member.key)
        self.line.emit(member.key, member.name, body)
