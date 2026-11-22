"""A stdio MCP adapter for the current user's resident desktop process."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from threading import Event, Lock
import time
from uuid import uuid4

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtNetwork import QLocalSocket

from protocol import MODERN_VERSION, discovery, requested_version, validated_arguments
from resident_agent import PROFILE


CHANNEL = "screen-mentor-v1-" + hashlib.sha256(str(PROFILE.resolve()).casefold().encode()).hexdigest()[:20]
MAX_REQUEST = 65_536


def connect():
    socket = QLocalSocket()
    socket.connectToServer(CHANNEL)
    if socket.waitForConnected(350) or socket.state() == QLocalSocket.LocalSocketState.ConnectedState:
        return socket
    return None


def roundtrip(socket, message, *, cancelled=None):
    try:
        payload = json.dumps(message, ensure_ascii=False).encode() + b"\n"
        if len(payload) > MAX_REQUEST:
            raise ValueError("工具请求超出大小限制。")
        socket.write(payload)
        socket.flush()
        deadline = time.monotonic() + 50
        while not socket.canReadLine():
            if cancelled is not None and cancelled.is_set():
                return None
            if socket.state() == QLocalSocket.LocalSocketState.UnconnectedState or time.monotonic() >= deadline:
                raise ValueError("桌面学伴没有返回结果。")
            socket.waitForReadyRead(150)
        return json.loads(bytes(socket.readLine()))
    finally:
        socket.abort()


def ensure_desktop(*, cancelled=None):
    socket = connect()
    if socket:
        return socket
    python = Path(sys.executable).with_name("pythonw.exe")
    desktop = Path(__file__).with_name("desktop.py")
    subprocess.Popen([str(python), "-B", str(desktop), "--background"], cwd=desktop.parent,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW)
    limit = time.monotonic() + 9
    while time.monotonic() < limit:
        if cancelled is not None and cancelled.is_set():
            return None
        socket = connect()
        if socket:
            return socket
        time.sleep(.15)
    raise ValueError("无法启动桌面学伴，可从桌面快捷方式打开后再试。")


def handle_call(request, owner, cancelled):
    params = request["params"]
    if params.get("name") == "wait_for_practice_change":
        validated_arguments("wait_for_practice_change", params.get("arguments", {}))
        socket = connect()
        if socket is None:
            return {"jsonrpc": "2.0", "id": request["id"], "result": {
                "content": [{"type": "text", "text": json.dumps({"state": "ended"})}]}}
    else:
        socket = ensure_desktop(cancelled=cancelled)
    if socket is None:
        return None
    return roundtrip(socket, {"owner": owner, "request": request}, cancelled=cancelled)


def rpc_error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def write_reply(request, reply):
    if requested_version(request) == MODERN_VERSION and "result" in reply:
        reply["result"].setdefault("resultType", "complete")
    sys.stdout.buffer.write(json.dumps(reply, ensure_ascii=False).encode() + b"\n")
    sys.stdout.buffer.flush()


def main():
    app = QCoreApplication(sys.argv[:1])
    owner = uuid4().hex
    active = {}
    lock = Lock()

    def run_call(request, cancelled):
        key = request["id"]
        try:
            reply = handle_call(request, owner, cancelled)
        except (OSError, ValueError) as error:
            reply = rpc_error(key, -32001, str(error))
        except Exception:
            reply = rpc_error(key, -32603, "桌面学伴连接失败。")
        with lock:
            active.pop(key, None)
            if reply is not None and not cancelled.is_set():
                write_reply(request, reply)

    with ThreadPoolExecutor(max_workers=2) as executor:
        try:
            for line in sys.stdin.buffer:
                try:
                    request = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                    continue
                key = request.get("id")
                with lock:
                    if "id" not in request:
                        params = request.get("params")
                        if request.get("method") == "notifications/cancelled" and isinstance(params, dict):
                            cancelled_id = params.get("requestId")
                            if type(cancelled_id) in (int, str) and cancelled_id in active:
                                active[cancelled_id].set()
                        continue
                    if type(key) not in (int, str):
                        continue
                    reply = discovery(request)
                    if reply is not None:
                        write_reply(request, reply)
                    elif key in active:
                        continue
                    elif len(active) == 2:
                        write_reply(request, rpc_error(key, -32001, "伴学工具正忙，请在上一调用结束后重试。"))
                    else:
                        cancelled = active[key] = Event()
                        executor.submit(run_call, request, cancelled)
        finally:
            with lock:
                for cancelled in active.values():
                    cancelled.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
