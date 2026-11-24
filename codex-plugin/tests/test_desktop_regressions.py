from io import BytesIO
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch


PLUGIN = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PLUGIN / "scripts"), str(PLUGIN.parent)]

from resident_agent import Archive, answer_preview, ask_model


class ArchiveTests(unittest.TestCase):
    def test_repeat_recall_does_not_find_earlier_search_commands(self):
        with TemporaryDirectory() as folder:
            archive = Archive(Path(folder) / "agent.sqlite3")
            archive.write("user", "回忆：黑曜石")
            archive.write("user", "回忆: 黑曜石")
            self.assertEqual(archive.search("黑曜石"), [])
            archive.remember("材料是黑曜石。")
            self.assertEqual([hit["body"] for hit in archive.search("黑曜石")], ["材料是黑曜石。"])


class ModelReplyTests(unittest.TestCase):
    def test_preview_displays_inline_code_and_unlabeled_code_fences(self):
        self.assertEqual(answer_preview("`Ctrl+C` 可以复制"), "`Ctrl+C` 可以复制")
        self.assertEqual(answer_preview("```\nprint('hello')"), "```\nprint('hello')")

    def test_json_fence_shows_only_the_answer_during_and_after_streaming(self):
        reply = '```Json\r\n{"learn":"测试记忆","answer":"测试回复"}\r\n```'
        self.assertEqual(answer_preview(reply), "测试回复")
        self.assertEqual(answer_preview('```Json\r\n{"learn":"测试记忆"'), "")
        response = {"choices": [{"message": {"content": reply}}]}
        with patch("resident_agent.build_opener") as opener:
            opener.return_value.open.return_value = BytesIO(json.dumps(response).encode())
            self.assertEqual(ask_model("https://example.com/v1", "test", "", []),
                             ("测试回复", "测试记忆", ""))


class QuickChatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_quick_recall_opens_chat_with_scrollable_results(self):
        from PyQt6.QtWidgets import QWidget
        from desktop import ChatPane, QuickChat
        from team import Member, PRIMARY

        memory = "黑曜石。" * 120
        with TemporaryDirectory() as folder:
            archive = Archive(Path(folder) / "agent.sqlite3")
            archive.remember(memory)
            with patch("desktop.PROFILE", Path(folder)):
                pane = ChatPane(archive, Member(PRIMARY, "学伴", "测试"), None, Mock())
            anchor = QWidget()
            open_history = Mock()
            popup = QuickChat(pane, anchor, open_history)
            popup.input.setText("回忆：黑曜石")
            popup.send()

            open_history.assert_called_once_with()
            self.assertIn(memory, pane.history.toPlainText())
            self.assertEqual(pane.history.textCursor().block().text(), "回忆结果")
            self.assertEqual(pane.notice.text(), "找到 1 条相关记录。")
            self.assertEqual([row["body"] for row in archive.transcript()], ["回忆：黑曜石"])


if __name__ == "__main__":
    unittest.main()
