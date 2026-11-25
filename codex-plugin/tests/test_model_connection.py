from io import BytesIO
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
from urllib.error import HTTPError
import unittest
from unittest.mock import patch


PLUGIN = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PLUGIN / "scripts"), str(PLUGIN.parent)]


class ModelConnectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from desktop import Desktop
        from team import Member

        self.profile = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch("desktop.PROFILE", self.profile))
        self.enterContext(patch("appearance.PROFILE", self.profile))
        self.desktop = Desktop(self.app)
        self.addCleanup(self.close_desktop)
        self.saved_defaults = ("https://saved.example/v1", "saved-model", "MENTOR_TEST_DEFAULT_KEY")
        for key, value in zip(("endpoint", "model", "key_name"), self.saved_defaults):
            self.desktop.agent_settings.setValue(key, value)
        self.desktop.agent_settings.sync()
        self.desktop.save_member(Member("test-member", "测试学伴", "测试"))
        self.dialog = self.desktop.get_manager()
        self.dialog.open_model()
        self.dialog.show()

    def close_desktop(self):
        self.desktop.quit()
        self.desktop.engine.practice.stop_worker()
        if self.desktop.manager is not None:
            self.desktop.manager.close()
        self.app.processEvents()

    def set_fields(self, **fields):
        for key, value in fields.items():
            self.dialog.model_fields[key].setText(value)

    def wait_for_result(self):
        from PyQt6.QtTest import QTest

        deadline = monotonic() + 3
        while not self.dialog.model_test_button.isEnabled() and monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(self.dialog.model_test_button.isEnabled(), "Model test did not finish")
        return self.dialog.model_notice.text()

    def assert_request(self, opener, endpoint, model, key):
        self.dialog.model_test_button.click()
        self.assertIn("连接成功", self.wait_for_result())
        request, = opener.call_args.args
        self.assertEqual(request.full_url, endpoint + "/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + key)
        self.assertEqual(request.get_method(), "POST")
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], model)
        self.assertEqual(payload["messages"], [{"role": "user", "content": "请只回复 OK。"}])
        self.assertGreater(payload["max_tokens"], 0)
        self.assertLess(opener.call_args.kwargs["timeout"], 35)

    def test_connection_uses_unsaved_form_and_member_inheritance_without_saving(self):
        keys = {"MENTOR_TEST_DEFAULT_KEY": "default-key", "MENTOR_TEST_FORM_KEY": "form-key"}
        with patch.dict(os.environ, keys), patch("resident_agent.build_opener") as build_opener:
            opener = build_opener.return_value.open
            response = json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode()
            opener.side_effect = lambda *args, **kwargs: BytesIO(response)
            self.desktop.archive.write("user", "私聊原文")
            self.desktop.archive.remember("长期记忆")

            self.set_fields(endpoint="https://default-edit.example/v1", model="unsaved-default",
                            key_name="MENTOR_TEST_FORM_KEY")
            self.assert_request(opener, "https://default-edit.example/v1", "unsaved-default", "form-key")
            self.dialog.open_model("test-member")
            self.assert_request(opener, self.saved_defaults[0], self.saved_defaults[1], "default-key")
            self.set_fields(model="override")
            self.assert_request(opener, self.saved_defaults[0], "override", "default-key")
            self.dialog.own_connection.setChecked(True)
            self.set_fields(endpoint="https://personal-edit.example/v1", own_model="personal",
                            key_name="MENTOR_TEST_FORM_KEY")
            self.assert_request(opener, "https://personal-edit.example/v1", "personal", "form-key")

        self.assertEqual(opener.call_count, 4)
        self.assertEqual(self.desktop.model_settings(), self.saved_defaults)
        member = self.dialog.selected_model_member()
        self.assertEqual((member.endpoint, member.model, member.own_model, member.key_name), ("", "", "", ""))
        self.assertEqual(len(self.desktop.archive.transcript()), 1)

    def test_connection_surfaces_configuration_and_http_errors(self):
        self.set_fields(endpoint="not-a-url")
        self.dialog.model_test_button.click()
        self.assertIn("HTTPS", self.wait_for_result())
        self.set_fields(endpoint="https://error.example/v1", key_name="")
        with patch("resident_agent.build_opener") as build_opener:
            opener = build_opener.return_value.open
            opener.side_effect = HTTPError("https://error.example/v1", 401, "unauthorized", {}, None)
            self.dialog.model_test_button.click()
            self.assertIn("HTTP 401", self.wait_for_result())

    def test_default_model_does_not_apply_settings_when_save_fails_and_can_retry(self):
        from stat import S_IREAD, S_IWRITE

        from PyQt6.QtCore import QSettings

        filename = Path(self.desktop.agent_settings.fileName())
        self.set_fields(endpoint="https://test.example/v1", model="test-model", key_name="TEST_KEY")
        reader = QSettings(str(filename), QSettings.Format.IniFormat)
        names = ("endpoint", "model", "key_name")
        filename.chmod(S_IREAD)
        try:
            self.dialog.save_model()

            self.assertIn("无法保存模型设置", self.dialog.model_notice.text())
            self.assertEqual(tuple(reader.value(name) for name in names), self.saved_defaults)
        finally:
            filename.chmod(S_IWRITE)

        self.dialog.save_model()

        self.assertEqual(self.dialog.model_notice.text(), "模型设置已保存。")
        reader.sync()
        self.assertEqual(tuple(reader.value(name) for name in names),
                         ("https://test.example/v1", "test-model", "TEST_KEY"))

    def test_old_result_is_discarded_after_edit_target_switch_page_switch_or_close(self):
        from PyQt6.QtCore import QTimer

        def edit():
            self.set_fields(model="edited-while-testing")

        for invalidate in (edit, lambda: self.dialog.open_model("test-member"),
                           lambda: self.dialog.nav.setCurrentRow(self.dialog.chat_index), self.dialog.close):
            with self.subTest(invalidate=invalidate):
                self.dialog.showNormal()
                self.dialog.open_model()
                waiting = Event()
                release = Event()

                def slow_reply(*args, **kwargs):
                    waiting.set()
                    if not release.wait(timeout=3):
                        raise ValueError("测试等待超时")
                    return "OK", "", ""

                with patch("manager.ask_model", side_effect=slow_reply) as ask:
                    self.dialog.model_test_button.click()
                    try:
                        self.assertTrue(waiting.wait(timeout=2))
                        self.assertFalse(self.dialog.model_test_button.isEnabled())
                        invalidate()
                        tick = Event()
                        QTimer.singleShot(0, tick.set)
                        self.app.processEvents()
                        self.assertTrue(tick.is_set(), "The Qt event loop was blocked by the request")
                        self.assertFalse(self.dialog.model_test_button.isEnabled())
                    finally:
                        release.set()
                    self.assertEqual(self.wait_for_result(), "")
                    ask.assert_called_once()
