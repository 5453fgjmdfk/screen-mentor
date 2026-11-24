from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch


PLUGIN = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PLUGIN / "scripts"), str(PLUGIN.parent)]

from PyQt6.QtWidgets import QDialog

from manager import ManagerWindow
from resident_agent import Archive


class ChatExportTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.root = Path(directory)
        self.archive = Archive(self.root / "agent.sqlite3")
        self.window = Mock()
        self.window.desktop.workspace_directory.return_value = directory
        self.pane = self.window.private_chats.currentWidget.return_value
        self.pane.archive = self.archive
        self.pane.member.name = "纸鹤"
        self.dialog_class = self.enterContext(patch("manager.QFileDialog"))
        self.dialog = self.dialog_class.return_value
        self.dialog.exec.return_value = QDialog.DialogCode.Accepted
        self.target = self.root / "私聊记录.md"
        self.dialog.selectedFiles.return_value = [str(self.target)]

    def export(self):
        ManagerWindow.export_chat(self.window)

    def test_exports_full_conversation_in_order_and_preserves_markdown(self):
        self.archive.write("user", "最早的消息")
        for index in range(100):
            self.archive.write("user", f"问题 {index}")
        self.archive.remember("单独保存的记忆")
        self.archive.write("practice", "单独保存的练习小结")
        self.archive.write("assistant", "中文回复\n\n```python\nprint('你好')\n```")
        self.target.write_text("previous export", encoding="utf-8")

        self.export()

        text = self.target.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# 屏幕伴学 · 与纸鹤的私聊\n"))
        self.assertIn("## 你 · ", text)
        self.assertIn("## 纸鹤 · ", text)
        self.assertLess(text.index("最早的消息"), text.index("问题 0"))
        self.assertLess(text.index("问题 99"), text.index("中文回复"))
        self.assertIn("```python\nprint('你好')\n```", text)
        self.assertNotIn("单独保存的", text)
        self.assertEqual(len(self.archive.transcript()), 100)
        self.pane.notice.setText.assert_called_once_with("已导出 102 条私聊消息。")

    def test_empty_conversation_does_not_open_file_dialog(self):
        self.export()

        self.dialog_class.assert_not_called()
        self.pane.notice.setText.assert_called_once_with("还没有可导出的私聊消息。")

    def test_cancel_leaves_existing_file_alone(self):
        self.archive.write("user", "测试一条私聊")
        self.target.write_text("existing file", encoding="utf-8")
        self.dialog.exec.return_value = QDialog.DialogCode.Rejected
        self.pane.member.name = "纸鹤/实验?"

        self.export()

        self.assertEqual(self.target.read_text(encoding="utf-8"), "existing file")
        suggested = Path(self.dialog_class.call_args.args[2])
        self.assertEqual(suggested.parent, self.root)
        self.assertTrue(suggested.name.startswith("学伴私聊-纸鹤_实验_"))
        self.pane.notice.setText.assert_not_called()

    def test_write_failure_is_reported(self):
        self.archive.write("assistant", "测试一条私聊")
        self.dialog.selectedFiles.return_value = [str(self.root / "missing" / "out.md")]

        self.export()

        self.pane.notice.setText.assert_called_once_with("无法保存 Markdown，请检查文件路径和写入权限。")


if __name__ == "__main__":
    unittest.main()
