"""Isolate the blocking Win32 window capture and impose a reply deadline."""
import json
import os
from pathlib import Path
from queue import Empty, Queue
import struct
import subprocess
import sys
import sysconfig
from threading import Thread

from PIL import Image

import native


CAPTURE_DEADLINE = 2.5


def serve():
    from capture import enable_dpi_awareness

    enable_dpi_awareness()
    for request in sys.stdin.buffer:
        try:
            hwnd, width, height = json.loads(request)
            data = native.read_window(hwnd, width, height).tobytes()
            length = len(data)
        except (native.WindowError, ValueError, OSError) as exc:
            data = str(exc).encode("utf-8")
            length = -len(data)
        sys.stdout.buffer.write(struct.pack("<i", length))
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


class WindowReader:
    def __init__(self):
        self.process = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def start(self):
        # Run the real interpreter so killing a timed-out capture also works with
        # Windows venvs whose python.exe is a process-launching redirector.
        environment = os.environ.copy()
        environment["PYTHONPATH"] = sysconfig.get_path("purelib")
        self.process = subprocess.Popen(
            [sys._base_executable, "-B", str(Path(__file__).resolve())],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW, env=environment,
        )
        self.replies = Queue()
        Thread(target=self.read_replies, args=(self.process.stdout, self.replies), daemon=True).start()

    @staticmethod
    def read_replies(stream, replies):
        with stream:
            try:
                while header := stream.read(4):
                    size, = struct.unpack("<i", header)
                    data = stream.read(abs(size))
                    if len(data) != abs(size):
                        break
                    replies.put((size, data))
            except (OSError, struct.error):
                pass
            finally:
                replies.put((0, b""))

    def read(self, hwnd, width, height):
        try:
            if self.process is None:
                self.start()
            self.process.stdin.write(json.dumps([hwnd, width, height]).encode() + b"\n")
            self.process.stdin.flush()
            size, data = self.replies.get(timeout=CAPTURE_DEADLINE)
        except Empty as exc:
            self.close()
            raise native.WindowError("窗口截图超时，已重启截图进程。") from exc
        except OSError as exc:
            self.close()
            raise native.WindowError("截图进程意外退出，将自动重试。") from exc
        if size < 0:
            raise native.WindowError(data.decode("utf-8", errors="replace"))
        if size != width * height * 3:
            self.close()
            raise native.WindowError("截图未返回完整画面，将自动重试。")
        return Image.frombytes("RGB", (width, height), data)

    def close(self):
        process, self.process = self.process, None
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
            finally:
                process.stdin.close()
                try:
                    process.wait(timeout=.3)
                except subprocess.TimeoutExpired:
                    pass


if __name__ == "__main__":
    serve()
