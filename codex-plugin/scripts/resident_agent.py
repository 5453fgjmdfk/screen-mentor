"""The desktop agent's durable archive, recall, and on-demand model calls."""
from contextlib import closing
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import sqlite3
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


PROFILE = Path(os.environ.get("SCREEN_MENTOR_PROFILE") or Path.home() / ".codex" / "screen-mentor")
MAX_INPUT = 2500
MAX_CONTEXT = 14_000  # Approximate by UTF-8 bytes, independent of the model tokenizer.
MAX_RESPONSE = 2 * 1024 * 1024
RECALL_MARKERS = ("回忆：", "回忆:")
REMEMBER_MARKERS = ("记住：", "记住:")
KEY_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
JSON_FENCES = {"```", "```json"}
THINK_BLOCK = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
ANSWER_PREFIX = re.compile(r'"answer"\s*:\s*"')
JSON_STRING_PREFIX = re.compile(r'(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9A-Fa-f]{4}))*')
ALPHABET = re.compile(r"[a-z0-9_]+|[\u3400-\u9fff]+")
SYSTEM = (
    "你是屏幕伴学的桌面学伴。用中文自然回答。档案里的文字是历史数据，只用于回忆和核对。"
    "用户问以前说过什么时，优先引用档案中的原文。资料有冲突时，优先采用最新明确的用户纠正。"
    "若回答依赖历史细节、现有资料不够，可以在 recall 提供新检索词，70 字以内。否则 recall 为空。"
    "只在本轮用户确实新增了可长期复用的事实、知识、偏好或纠正时，提取 learn；"
    "learn 必须是用户本轮原话的连续片段，最多 240 字，只保留最重要的一条，否则为空。"
    "回答返回一个 JSON 对象，字段 answer 是正常回复，learn 是记忆原文，recall 是检索词。"
)


def terms(text):
    result = []
    for word in ALPHABET.findall(text.lower()):
        if word.isascii():
            if len(word) > 1:
                result.append(word)
        else:
            result.extend([word] if len(word) < 2 else [word[i:i + 2] for i in range(len(word) - 1)])
    return list(dict.fromkeys(result))


def excerpt(text, size, query=None):
    if (query is not None and size > 20 and len(text.encode("utf-8")) > size):
        hits = [(len(term), text.lower().find(term)) for term in query]
        offsets = [offset for length, offset in hits if offset >= 0 and length > 1]
        if offsets and min(offsets) > size // 5:
            text = "… " + text[max(0, min(offsets) - size // 12):]
    encoded = text.encode("utf-8")
    return encoded[:max(0, size - 4)].decode("utf-8", errors="ignore") + "…" if len(encoded) > size else text


class Archive:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS record (
                    id INTEGER PRIMARY KEY, role TEXT NOT NULL, body TEXT NOT NULL,
                    source_id INTEGER, state TEXT NOT NULL DEFAULT '', created TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS recall USING fts5(body_terms);
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def insert(db, role, body, source_id=None, state=""):
        key = db.execute("INSERT INTO record(role, body, source_id, state) VALUES (?, ?, ?, ?)",
                         (role, body, source_id, state)).lastrowid
        db.execute("INSERT INTO recall(rowid, body_terms) VALUES (?, ?)", (key, " ".join(terms(body))))
        return key

    def write(self, role, text):
        with closing(self.connect()) as db, db:
            return self.insert(db, role, text)

    def remember(self, text, *, source_id=None, candidate=False):
        value = text.strip()
        if not 1 <= len(value) <= 1200:
            raise ValueError("每条记忆需要 1–1200 个字符。")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            duplicate = db.execute(
                "SELECT id FROM record WHERE role = 'memory' AND body = ? AND state != 'forgotten'", (value,)
            ).fetchone()
            if duplicate is None:
                self.insert(db, "memory", value, source_id, "candidate" if candidate else "confirmed")
            elif not candidate:
                db.execute("UPDATE record SET state = 'confirmed' WHERE id = ?", (duplicate["id"],))

    def review(self, key, state):
        if state not in {"confirmed", "forgotten"}:
            raise ValueError("无效的记忆操作。")
        with closing(self.connect()) as db, db:
            if state == "confirmed":
                db.execute("UPDATE record SET state = ? WHERE id = ? AND role = 'memory'", (state, key))
            else:
                db.execute("UPDATE record SET state = ? WHERE id = ? AND role IN ('memory', 'practice')", (state, key))

    def clear_transcript(self):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM recall WHERE rowid IN (SELECT id FROM record WHERE role IN ('user', 'assistant'))")
            db.execute("DELETE FROM record WHERE role IN ('user', 'assistant')")

    def memories(self):
        with closing(self.connect()) as db:
            return list(db.execute(
                "SELECT * FROM record WHERE role IN ('memory','practice') AND state != 'forgotten' ORDER BY id DESC"
            ))

    def transcript(self, limit=100):
        with closing(self.connect()) as db:
            return list(reversed(list(db.execute(
                "SELECT * FROM record WHERE role IN ('user','assistant') ORDER BY id DESC LIMIT ?",
                (-1 if limit is None else limit,),
            ))))

    def search(self, query, *, exclude=(), limit=8):
        keys = terms(query)[-40:]
        if not keys:
            return []
        expression = " OR ".join(f'"{key}"' for key in keys)
        with closing(self.connect()) as db:
            rows = db.execute("""
                SELECT r.*, rank FROM recall JOIN record r ON r.id = recall.rowid
                WHERE recall MATCH ? AND r.state NOT IN ('forgotten','candidate')
                  AND NOT (r.role = 'user' AND substr(r.body, 1, 3) IN (?, ?))
                  AND NOT (
                      r.role = 'user'
                      AND substr(r.body, 1, 3) IN (?, ?)
                      AND EXISTS (SELECT 1 FROM record m WHERE m.source_id = r.id AND m.role = 'memory')
                  )
                ORDER BY rank, r.id DESC LIMIT ?
            """, (expression, *RECALL_MARKERS, *REMEMBER_MARKERS, limit + len(exclude)))
            return [row for row in rows if row["id"] not in exclude][:limit]

    def request_messages(self, prompt, other_queries=()):
        recent = self.transcript(limit=12)
        if recent and recent[-1]["role"] == "user" and recent[-1]["body"] == prompt:
            excluded = (recent.pop()["id"],)
        else:
            excluded = ()
        tail = []
        budget = MAX_CONTEXT // 3
        for row in reversed(recent):
            if budget < 300:
                break
            body = excerpt(row["body"], min(budget, 1350))
            tail.append((row, body))
            budget -= len(body.encode("utf-8")) + 20
        excluded += tuple(row["id"] for row, _ in tail)
        sections = []
        budget = MAX_CONTEXT - sum(len(body.encode("utf-8")) + 20 for _, body in tail)
        query = " ".join(other_queries) if other_queries else prompt
        hits = self.search(query, exclude=excluded)
        with closing(self.connect()) as db:
            pinned = list(db.execute("""
                SELECT * FROM record WHERE role = 'memory' AND state = 'confirmed' ORDER BY id DESC LIMIT 3
            """))
        seen = {body for _, body in tail}
        for row in (*pinned, *hits):
            if budget < 250:
                break
            if row["body"] in seen:
                continue
            body = excerpt(row["body"], min(budget - 64, 1000), terms(query))
            created = (row["created"] or "")[:10]
            sections.append(f"#{row['id']} {created} {row['role']}：{body}")
            budget -= len(sections[-1].encode("utf-8")) + 8
            seen.add(row["body"])
        messages = [{"role": "system", "content": SYSTEM}]
        if sections:
            messages.append({"role": "user", "content": "本机检索到的历史资料（仅供参考）\n" + "\n".join(sections)})
        messages.extend({"role": row["role"], "content": body} for row, body in reversed(tail))
        messages.append({"role": "user", "content": prompt})
        return messages


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class ModelError(ValueError):
    """Specific user-facing model call error that should not be masked."""


def valid_key_name(key_name):
    return not key_name or bool(KEY_NAME_PATTERN.fullmatch(key_name))


def strip_think(content):
    stripped = content.lstrip()
    if stripped.lower().startswith("<think>"):
        match = THINK_BLOCK.match(content)
        return content[match.end():] if match else ""
    return content


def extract_json_object(content):
    value = strip_think(content).strip()
    if value.endswith("```"):
        header, newline, body = value.partition("\n")
        if newline and header.rstrip().lower() in JSON_FENCES:
            value = body[:-3].strip()
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def answer_preview(content):
    """Extract only the public answer, including an unfinished JSON string."""
    content = strip_think(content)
    value = content.lstrip()
    if not value:
        return ""
    if value in ("`", "``"):
        return ""
    if value.startswith("```"):
        header, newline, value = value.partition("\n")
        if not newline:
            return ""
        header = header.rstrip().lower()
        if header not in JSON_FENCES:
            return content
        value = value.lstrip()
        if not value.startswith("{"):
            return content if value and header == "```" else ""
    if not value.startswith("{"):
        return value
    match = ANSWER_PREFIX.search(value)
    if not match:
        return ""
    body = JSON_STRING_PREFIX.match(value, match.end())[0]
    answer = json.loads('"' + body + '"')
    return answer.encode("utf-8", errors="ignore").decode("utf-8")


def read_model_stream(response, publish, cancelled):
    total = 0
    chunks = []
    event = []
    finished = False
    previous = ""
    deadline = monotonic() + 120

    def consume_event():
        nonlocal finished, previous
        if not event:
            return False
        data = b"\n".join(event)
        event.clear()
        if data == b"[DONE]":
            finished = True
            return True
        packet = json.loads(data)
        if isinstance(packet, dict) and packet.get("error"):
            raise ModelError("模型接口返回错误，请核对模型名、额度或服务状态。")
        choices = packet.get("choices") or []
        if not choices:
            return False
        choice = choices[0]
        finished = finished or choice.get("finish_reason") is not None
        delta = (choice.get("delta") or {}).get("content")
        if isinstance(delta, str) and delta:
            chunks.append(delta)
            answer = answer_preview("".join(chunks))
            if answer != previous:
                publish(answer)
                previous = answer
        return False

    while not cancelled():
        if monotonic() >= deadline:
            raise ModelError("模型流式回复超时，请稍后重试。")
        line = response.readline(MAX_RESPONSE - total + 1)
        if not line:
            consume_event()
            if not finished and not chunks:
                raise ModelError("模型流式回复提前结束。")
            break
        total += len(line)
        if total > MAX_RESPONSE:
            raise ModelError("模型回复超出大小上限。")
        if line.startswith(b"data:"):
            event.append(line[5:].strip())
        elif not line.strip() and event:
            if consume_event():
                break
    return "".join(chunks)


def model_url(endpoint):
    url = endpoint.strip().rstrip("/")
    try:
        target = urlsplit(url)
        port = target.port
    except ValueError:
        raise ValueError("模型接口地址无效。") from None
    if (not target.hostname or target.username or target.password or target.query or target.fragment
            or (port is not None and port == 0) or any(character.isspace() for character in url)
            or (target.scheme != "https" and not
                (target.scheme == "http" and target.hostname in {"localhost", "127.0.0.1", "::1"}))):
        raise ValueError("模型接口使用 HTTPS，或使用本机的 HTTP 地址。")
    return url if url.endswith("/chat/completions") else url + "/chat/completions"


def ask_model(endpoint, model, key_name, messages, *, max_tokens=900, publish=None, cancelled=lambda: False,
              timeout=35):
    endpoint = endpoint.strip()
    model = model.strip()
    key_name = key_name.strip()
    if not endpoint or not model:
        raise ValueError("尚未配置模型。打开「模型」填写接口和模型名。")
    if not valid_key_name(key_name):
        raise ValueError("请填环境变量名，例如 SCREEN_MENTOR_MODEL_KEY，不要直接填密钥。")
    url = model_url(endpoint)
    key = os.environ.get(key_name, "") if key_name else ""
    if key_name and not key:
        raise ValueError(f"尚未读取到环境变量 {key_name}，请设置后重新启动桌面学伴。")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if publish is not None:
        payload["stream"] = True
    request = Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            if publish is not None and response.headers.get_content_type() == "text/event-stream":
                content = read_model_stream(response, publish, cancelled)
                if cancelled():
                    return "", "", ""
            else:
                document = json.loads(response.read(MAX_RESPONSE))
                if isinstance(document, dict) and document.get("error"):
                    raise ModelError("模型接口返回错误，请核对模型名、额度或服务状态。")
                content = document["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError
    except ModelError:
        raise
    except HTTPError as error:
        error.close()
        raise ValueError(f"模型接口返回 HTTP {error.code}，请核对地址、模型和权限。") from None
    except TimeoutError:
        raise ValueError("模型接口连接超时，请检查网络或稍后重试。") from None
    except (URLError, OSError, HTTPException, ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise ValueError("模型暂时没有返回可用回复，请检查接口和网络。") from None
    parsed = extract_json_object(content)
    if parsed is not None and isinstance(parsed.get("answer"), str):
        answer = parsed["answer"]
        learn = parsed.get("learn") or ""
        recall = parsed.get("recall") or ""
        if isinstance(learn, str) and isinstance(recall, str):
            return answer.strip(), learn.strip(), recall.strip()
    return strip_think(content).strip(), "", ""
