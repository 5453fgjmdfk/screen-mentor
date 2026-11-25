from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import unittest
from unittest.mock import patch


PLUGIN = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PLUGIN / "scripts"), str(PLUGIN.parent)]

from diagnostics import support_report


class DiagnosticsStorageTests(unittest.TestCase):
    def test_missing_profile_is_not_created(self):
        with TemporaryDirectory() as folder:
            profile = Path(folder) / "missing"
            report = support_report(profile / "archive.sqlite3", profile / "team.sqlite3", False)
            self.assertFalse(profile.exists())
            self.assertIn("数据目录：目录不可用", report)
            self.assertIn("主档案数据库：文件不可用", report)
            self.assertIn("团队与任务数据库：文件不可用", report)
            self.assertIn("本机 MCP 监听：未启动", report)
            self.assertNotIn(str(profile), report)

    def test_locked_and_corrupt_databases_are_reported_without_paths_or_content(self):
        with TemporaryDirectory() as folder:
            profile = Path(folder)
            archive = profile / "路径#sentinel-archive.sqlite3"
            team = profile / "sentinel-team.sqlite3"
            team.write_text("sentinel-file-content", encoding="utf-8")
            with closing(sqlite3.connect(archive)) as db:
                db.execute("CREATE TABLE record (body TEXT)")
                db.execute("BEGIN EXCLUSIVE")
                report = support_report(archive, team, True)
                self.assertIn("主档案数据库：暂时被占用", report)
                self.assertIn("团队与任务数据库：读取失败", report)
                db.rollback()

            report = support_report(archive, team, True)
            self.assertIn("主档案数据库：结构可读", report)
            self.assertIn("数据目录：临时文件读写正常", report)
            self.assertIn("本机 MCP 监听：已启动", report)
            self.assertNotIn("sentinel", report)
            self.assertEqual(set(profile.iterdir()), {archive, team})


class DiagnosticsPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_settings_runs_and_copies_only_support_status(self):
        from PyQt6.QtTest import QTest

        with TemporaryDirectory() as folder:
            profile = Path(folder) / "sentinel-profile"
            with patch("resident_agent.PROFILE", profile):
                from desktop import Desktop
            with patch("desktop.PROFILE", profile), patch("appearance.PROFILE", profile):
                desktop = Desktop(self.app)
                try:
                    desktop.archive.write("user", "sentinel-chat-text")
                    desktop.agent_settings.setValue("endpoint", "https://sentinel-api.invalid/v1")
                    desktop.agent_settings.setValue("key_name", "SENTINEL_PRIVATE_KEY")
                    manager = desktop.get_manager()
                    manager.show()
                    manager.nav.setCurrentRow(manager.settings_index)
                    index = next(i for i in range(manager.settings_tabs.count())
                                 if manager.settings_tabs.tabText(i) == "本机诊断")
                    manager.settings_tabs.setCurrentIndex(index)

                    self.assertFalse(manager.diagnostics_copy.isEnabled())
                    self.assertFalse(manager.settings_save.isVisible())
                    manager.diagnostics_check.click()
                    deadline = monotonic() + 4
                    while not manager.diagnostics_copy.isEnabled() and monotonic() < deadline:
                        QTest.qWait(10)

                    self.assertTrue(manager.diagnostics_copy.isEnabled())
                    report = manager.diagnostics_report.toPlainText()
                    for expected in ("应用版本：", "操作系统：Windows", "Python：", "Qt：", "SQLite：",
                                     "主档案数据库：结构可读", "团队与任务数据库：结构可读"):
                        self.assertIn(expected, report)
                    self.assertNotIn("sentinel", report.casefold())
                    self.assertNotIn(str(profile), report)
                    with patch("manager.QGuiApplication") as gui:
                        manager.diagnostics_copy.click()
                    gui.clipboard.return_value.setText.assert_called_once_with(report)
                finally:
                    desktop.quit()
                    desktop.engine.practice.stop_worker()
                    if desktop.manager is not None:
                        desktop.manager.close()
                    self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
