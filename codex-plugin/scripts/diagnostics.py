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


def error_log_status(directory):
    log_file = directory / "desktop-errors.log"
    try:
        if not log_file.is_file():
            return "无异常记录"
        with log_file.open("r", encoding="utf-8", errors="ignore") as handle:
            count = sum(1 for line in handle if line.strip())
        return f"已记录 {count} 条异常" if count else "无异常记录"
    except OSError:
        return "读取失败"


def custom_skins_count(directory):
    skins_dir = directory / "appearance" / "skins"
    try:
        if not skins_dir.is_dir():
            return "0 个"
        count = sum(1 for item in skins_dir.iterdir() if item.is_dir() and item.name.startswith("skin-"))
        return f"{count} 个"
    except OSError:
        return "读取失败"


def dependency_versions():
    versions = []
    for module_name, label in (("PIL", "Pillow"), ("mss", "mss")):
        try:
            module = __import__(module_name)
            versions.append(f"{label} {getattr(module, '__version__', '未知')}")
        except ImportError:
            versions.append(f"{label} 缺失")
    return " / ".join(versions)


def support_report(archive_path, team_path, listening):
    system = sys.platform
    if sys.platform == "win32":
        version = sys.getwindowsversion().platform_version
        system = "Windows " + ".".join(map(str, version))
    profile_dir = archive_path.parent
    return "\n".join((
        "屏幕伴学 · 本机诊断",
        f"应用版本：{VERSION}",
        f"操作系统：{system}",
        f"Python：{platform.python_version()}",
        f"Qt：{qVersion()}",
        f"SQLite：{sqlite3.sqlite_version}",
        f"依赖库：{dependency_versions()}",
        "",
        f"数据目录：{profile_status(profile_dir)}",
        f"主档案数据库：{database_status(archive_path, 'record')}",
        f"团队与任务数据库：{database_status(team_path, 'member')}",
        f"自定义皮肤：{custom_skins_count(profile_dir)}",
        f"错误日志：{error_log_status(profile_dir)}",
        f"本机 MCP 监听：{'已启动' if listening else '未启动'}",
    ))
