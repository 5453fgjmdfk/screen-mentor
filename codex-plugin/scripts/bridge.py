"""A stdio MCP adapter for the current user's resident desktop process."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtNetwork import QLocalSocket

from protocol import discovery
from resident_agent import PROFILE


CHANNEL = "screen-mentor-v1-" + hashlib.sha256(str(PROFILE.resolve()).casefold().encode()).hexdigest()[:20]
MAX_REQUEST = 65_536


def connect():
    socket = QLocalSocket()
    socket.connectToServer(CHANNEL)
    if socket.waitForConnected(350) or socket.state() == QLocalSocket.LocalSocketState.ConnectedState:
        return socket
    return None


def roundtrip(socket, message):
    try:
        payload = json.dumps(message, ensure_ascii=False).encode() + b"\n"
        if len(payload) > MAX_REQUEST:
            raise ValueError("工具请求超出大小限制。")
        socket.write(payload)
        socket.flush()
        deadline = time.monotonic() + 50
        while not socket.canReadLine():
            if socket.state() == QLocalSocket.LocalSocketState.UnconnectedState or time.monotonic() >= deadline:
                raise ValueError("桌面学伴没有返回结果。")
            socket.waitForReadyRead(500)
        return json.loads(bytes(socket.readLine()))
    finally:
        socket.abort()


def ensure_desktop():
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
        socket = connect()
        if socket:
            return socket
        time.sleep(.15)
    raise ValueError("无法启动桌面学伴，可从桌面快捷方式打开后再试。")


def main():
    app = QCoreApplication(sys.argv[:1])
    owner = uuid4().hex
    for line in sys.stdin.buffer:
        try:
            request = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or "id" not in request:
            continue
        try:
            response = discovery(request)
            if response is None:
                response = roundtrip(ensure_desktop(), {"owner": owner, "request": request})
        except (OSError, ValueError) as error:
            response = {"jsonrpc": "2.0", "id": request["id"],
                        "error": {"code": -32001, "message": str(error)}}
        sys.stdout.buffer.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
        sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
