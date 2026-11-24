"""Collect local support status without including profile contents or identifiers."""
from contextlib import closing
import platform
import sqlite3
import sys
from tempfile import TemporaryFile

from PyQt6.QtCore import qVersion

from protocol import VERSION


def profile_status(directory):
    try:
        if not directory.is_dir():
            return "目录不可用"
        with TemporaryFile(dir=directory, prefix=".screen-mentor-check-") as probe:
            probe.write(b"ok")
            probe.seek(0)
            return "临时文件读写正常" if probe.read() == b"ok" else "临时文件读写失败"
    except OSError:
        return "临时文件读写失败"


def database_status(path, table):
    try:
        if not path.is_file():
            return "文件不可用"
        uri = path.absolute().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=0.2)) as db:
            exists = db.execute("SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?", (table,)).fetchone()
        return "结构可读" if exists else "缺少预期结构"
    except sqlite3.Error as error:
        code = getattr(error, "sqlite_errorcode", 0) & 0xff
        return "暂时被占用" if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) else "读取失败"
    except OSError:
        return "读取失败"


def support_report(archive_path, team_path, listening):
    system = sys.platform
    if sys.platform == "win32":
        version = sys.getwindowsversion().platform_version
        system = "Windows " + ".".join(map(str, version))
    return "\n".join((
        "屏幕伴学 · 本机诊断",
        f"应用版本：{VERSION}",
        f"操作系统：{system}",
        f"Python：{platform.python_version()}",
        f"Qt：{qVersion()}",
        f"SQLite：{sqlite3.sqlite_version}",
        "",
        f"数据目录：{profile_status(archive_path.parent)}",
        f"主档案数据库：{database_status(archive_path, 'record')}",
        f"团队与任务数据库：{database_status(team_path, 'member')}",
        f"本机 MCP 监听：{'已启动' if listening else '未启动'}",
    ))
