from datetime import datetime, timedelta
from io import BytesIO
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
import unittest
from unittest.mock import Mock, patch

from PIL import Image


PLUGIN = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PLUGIN / "scripts"), str(PLUGIN.parent)]

from resident_agent import Archive, answer_preview, ask_model, extract_json_object, valid_key_name
from scheduled import ScheduledTask, TaskScheduler
from server import MentorServer
from team import DEFAULT_ROOM, PRIMARY, CasualRoom, Member, TeamCoordinator, TeamStore


class FakeStreamResponse:
    def __init__(self, payload, content_type="text/event-stream"):
        self._stream = BytesIO(payload)
        self.headers = self
        self._content_type = content_type

    def get_content_type(self):
        return self._content_type

    def readline(self, limit=-1):
        return self._stream.readline(limit)

    def read(self, limit=-1):
        return self._stream.read(limit)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self._stream.close()


class AgentAndArchiveImprovementTests(unittest.TestCase):
    def test_stream_flushes_trailing_event_without_final_blank_line(self):
        chunk = json.dumps({"choices": [{"delta": {"content": '{"answer":"你好世界"}'}, "finish_reason": "stop"}]})
        # No trailing blank line after the data line (common on some local proxies).
        payload = f"data: {chunk}\n".encode("utf-8")
        published = []
        with patch("resident_agent.build_opener") as opener:
            opener.return_value.open.return_value = FakeStreamResponse(payload)
            answer, learn, recall = ask_model(
                "https://example.com/v1", "test-model", "", [{"role": "user", "content": "hi"}],
                publish=published.append,
            )
        self.assertEqual((answer, learn, recall), ("你好世界", "", ""))
        self.assertEqual(published[-1], "你好世界")

    def test_interrupted_stream_rejects_partial_answer(self):
        chunk = json.dumps({"choices": [{"delta": {"content": "尚未说完"}}]})
        payload = f"data: {chunk}\n\n".encode("utf-8")
        published = []
        with patch("resident_agent.build_opener") as opener:
            opener.return_value.open.return_value = FakeStreamResponse(payload)
            with self.assertRaisesRegex(ValueError, "提前结束"):
                ask_model("https://example.com/v1", "test-model", "", [], publish=published.append)
        self.assertEqual(published, ["尚未说完"])

    def test_stream_accepts_done_without_final_blank_line(self):
        payload = b'data: {"choices":[{"delta":{"content":"Complete"}}]}\n\ndata: [DONE]'
        with patch("resident_agent.build_opener") as opener:
            opener.return_value.open.return_value = FakeStreamResponse(payload)
            self.assertEqual(ask_model("https://example.com/v1", "test-model", "", [], publish=lambda _: None),
                             ("Complete", "", ""))

    def test_think_tags_hidden_during_streaming_and_stripped_on_parse(self):
        self.assertEqual(answer_preview("<think>正在推理中…"), "")
        self.assertEqual(answer_preview('<think>推理完成</think>\n{"answer":"最终结论"}'), "最终结论")
        reply = '<think>内部推理</think>\n```json\n{"answer":"结论","learn":"偏好蓝莓","recall":""}\n```'
        response = {"choices": [{"message": {"content": reply}}]}
        with patch("resident_agent.build_opener") as opener:
            opener.return_value.open.return_value = FakeStreamResponse(
                json.dumps(response).encode("utf-8"), content_type="application/json"
            )
            self.assertEqual(ask_model("https://example.com/v1", "r1", "", []), ("结论", "偏好蓝莓", ""))

    def test_extract_json_object_handles_fences_and_think_tags(self):
        raw = '<think>plan</think>\n```JSON\n{"tasks": {"reviewer": "检查步骤"}}\n```'
        self.assertEqual(extract_json_object(raw), {"tasks": {"reviewer": "检查步骤"}})

    def test_invalid_key_name_is_rejected_before_network(self):
        self.assertTrue(valid_key_name(""))
        self.assertTrue(valid_key_name("SCREEN_MENTOR_KEY_1"))
        self.assertFalse(valid_key_name("sk-proj-secret123"))
        with self.assertRaisesRegex(ValueError, "环境变量名"):
            ask_model("https://example.com/v1", "test", "sk-secret", [])

    def test_forgotten_memory_does_not_resurrect_and_clear_transcript_preserves_memories(self):
        with TemporaryDirectory() as folder:
            archive = Archive(Path(folder) / "agent.sqlite3")
            source = archive.write("user", "记住：我的电阻箱编号是XK99")
            archive.remember("我的电阻箱编号是XK99", source_id=source)
            archive.write("assistant", "已经存入长期记忆。")
            archive.write("user", "平时聊一句天")
            archive.write("practice", "完成了运放仿真练习")

            # Searching returns the memory once (not duplicated by the "记住：" command).
            hits = archive.search("XK99")
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["role"], "memory")

            # Forgetting the memory prevents it from resurfacing via the "记住：" command.
            archive.review(hits[0]["id"], "forgotten")
            self.assertEqual(archive.search("XK99"), [])

            # Practice summaries can also be forgotten, while remaining memories survive transcript clear.
            archive.remember("常备万用表")
            practice_row = next(row for row in archive.memories() if row["role"] == "practice")
            archive.review(practice_row["id"], "forgotten")
            archive.clear_transcript()

            self.assertEqual(archive.transcript(), [])
            remaining = [row["body"] for row in archive.memories()]
            self.assertEqual(remaining, ["常备万用表"])
            self.assertEqual([row["body"] for row in archive.search("万用表")], ["常备万用表"])
            self.assertEqual(archive.search("聊一句天"), [])


class TeamAndSchedulerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = Path(self.enterContext(TemporaryDirectory()))
        self.store = TeamStore(self.dir / "team.sqlite3")
        self.team = TeamCoordinator(
            self.store, self.dir / "team.ini",
            lambda: ("https://example.com/v1", "default-model", ""),
            lambda: True,
        )
        self.addCleanup(self.team.timer.stop)

    def test_mentions_are_case_insensitive_and_group_chat_supports_broadcast(self):
        self.store.save_member(Member("alice", "Alice", "架构师"))
        roster = self.store.members()
        self.assertEqual(TeamCoordinator.mentions("请问 @alice 怎么看", roster), ["alice"])
        self.assertTrue(TeamCoordinator.is_broadcast("@all 大家讨论一下"))
        self.assertTrue(TeamCoordinator.is_broadcast("请 @Everyone 看看"))

    def test_mentions_keep_exact_names_and_skip_regex_only_matches(self):
        self.store.save_member(Member("review-i", "I", "检查"))
        self.store.save_member(Member("alice", "Alice", "设计"))
        roster = self.store.members()
        self.assertEqual(TeamCoordinator.mentions("@\u0131 请 @alice 检查", roster), ["alice"])
        self.assertEqual(TeamCoordinator.mentions("@i 请 @I 检查", roster), ["review-i"])
        same_casefold = [Member("upper", "Alice", "设计"), Member("lower", "alice", "检查")]
        self.assertEqual(TeamCoordinator.mentions("@Alice @alice", same_casefold), ["upper", "lower"])

    def test_parallel_run_parses_fenced_json_plan(self):
        token = Event()
        members = self.store.members()
        connections = {m.key: ("https://example.com/v1", "m", "") for m in members}
        responses = iter([
            ('```json\n{"tasks": {"reviewer": "核对电路原理图连线"}}\n```', "", ""),
            ("连线全部正确。", "", ""),
            ("综合确认：连线无误。", "", ""),
        ])
        with patch("team.ask_model", side_effect=lambda *a, **k: next(responses)):
            summary = self.team.run_parallel(token, "round-1", "检查电路", members, "", connections)
        self.assertIn("完成", summary)
        lines = self.store.lines(round_id="round-1")
        self.assertTrue(any("核对电路原理图连线" in row["body"] for row in lines))

    def test_member_and_room_removal_cleans_up_references(self):
        self.store.save_member(Member("helper", "小助手", "记录要点"))
        room = CasualRoom("custom-room", "实验角", members=(PRIMARY, "helper"), lead="helper")
        self.team.save_room(room)
        self.store.add_line("r1", "casual", "你", "开聊", casual_key="custom-room")
        # Calling add_line again with same round_id & casual_key should not raise IntegrityError.
        self.store.add_line("r1", "casual", "小助手", "收到", "helper", casual_key="custom-room")

        with self.assertRaises(ValueError):
            self.store.remove_member(PRIMARY)
        with self.assertRaises(ValueError):
            self.team.remove_room(DEFAULT_ROOM)

        self.store.remove_member("helper")
        self.assertNotIn("helper", {m.key for m in self.store.members()})
        updated_room = next(r for r in self.store.rooms() if r.key == "custom-room")
        self.assertNotIn("helper", updated_room.members)
        self.assertEqual(updated_room.lead, PRIMARY)

        self.team.remove_room("custom-room")
        self.assertNotIn("custom-room", {r.key for r in self.store.rooms()})
        self.assertEqual(self.store.lines(limit=None, casual_key="custom-room"), [])

    def test_scheduler_claim_commits_and_run_now_preserves_schedule(self):
        scheduler = TaskScheduler(self.team)
        scheduler.timer.stop()
        now = datetime(2026, 11, 25, 10, 0, 0)
        one_shot = ScheduledTask("t1", "单次复习", "出一道题", now - timedelta(minutes=1), 0, "chat", True)
        daily = ScheduledTask("t2", "每日打卡", "总结今天", now + timedelta(hours=2), 1, "chat", True)
        scheduler.save(one_shot)
        scheduler.save(daily)

        claimed = scheduler.claim(now)
        self.assertIsNotNone(claimed)
        task, round_id = claimed
        self.assertEqual(task.key, "t1")
        # Claim must be committed so a second claim does not pick t1 again.
        self.assertIsNone(scheduler.claim(now))
        saved_t1 = next(t for t in scheduler.tasks() if t.key == "t1")
        self.assertFalse(saved_t1.enabled)
        self.assertEqual(saved_t1.last_round, round_id)
        self.assertEqual(saved_t1.last_status, "运行中…")

        # run_now runs t2 immediately without modifying its scheduled next_at.
        with patch.object(self.team, "start") as start_mock:
            scheduler.run_now("t2")
        start_mock.assert_called_once()
        saved_t2 = next(t for t in scheduler.tasks() if t.key == "t2")
        self.assertEqual(saved_t2.next_at, daily.next_at)
        self.assertTrue(saved_t2.enabled)
        self.assertNotEqual(saved_t2.last_round, "")


class ScreenOverlayAndSkinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_region_changed_detects_equal_luminance_color_change_and_clamps_bounds(self):
        red = Image.new("RGB", (200, 120), (255, 0, 0))
        green = Image.new("RGB", (200, 120), (0, 130, 0))
        self.assertEqual(red.convert("L").tobytes(), green.convert("L").tobytes())
        first = (1, (0, 0, 200, 120, 1), red)
        second = (1, (0, 0, 200, 120, 1), green)
        self.assertTrue(MentorServer.region_changed(first, second, (10, 10, 50, 40)))
        self.assertFalse(MentorServer.region_changed(first, first, (-10, -10, 300, 300)))

    def test_overlay_renders_step_badge_and_handles_edge_coordinates(self):
        from PyQt6.QtGui import QPixmap
        from overlay import GuideOverlay

        overlay = GuideOverlay()
        try:
            overlay.resize(800, 600)
            overlay.set_capture_size(0, 0)  # Must not divide by zero.
            overlay.show_guide((10, 10, 120, 560), "点击主面板", "右侧应避让目标区域", number=3,
                               drag_to_phys=(200, 300))
            canvas = QPixmap(800, 600)
            overlay.render(canvas)
            self.assertFalse(canvas.isNull())
        finally:
            overlay.clear()
            overlay.close()

    def test_skin_library_direct_resolve_and_delete_preset_and_skin(self):
        from PyQt6.QtCore import QRect
        from appearance import BUILT_INS, SkinLibrary
        from skin_assets import crop_sheet

        with TemporaryDirectory() as folder:
            library = SkinLibrary(Path(folder) / "appearance")
            self.assertEqual(library.resolve("whale-maid").key, "whale-maid")

            sheet = Image.new("RGBA", (64, 32), (0, 0, 0, 0))
            for x in range(8, 24):
                for y in range(8, 24):
                    sheet.putpixel((x, y), (80, 180, 240, 255))
            for x in range(40, 56):
                for y in range(8, 24):
                    sheet.putpixel((x, y), (240, 120, 90, 255))
            idle_name, idle_raw = crop_sheet(sheet, QRect(0, 0, 32, 32), 2, 1, 5, "idle")
            side_name, side_raw = crop_sheet(sheet, QRect(0, 0, 32, 32), 1, 1, 5, "side")
            bottom_name, bottom_raw = crop_sheet(sheet, QRect(0, 0, 32, 32), 1, 1, 5, "bottom")

            created = library.create("自制测试皮肤", {idle_name: idle_raw, side_name: side_raw, bottom_name: bottom_raw})
            self.assertEqual(library.resolve(created.key).name, "自制测试皮肤")

            library.save_preset("我的方案", created)
            self.assertIn("我的方案", [name for name, _ in library.presets()])

            library.delete_preset("我的方案")
            self.assertNotIn("我的方案", [name for name, _ in library.presets()])

            library.save_preset("绑定皮肤的方案", created)
            library.delete_skin(created.key)
            self.assertEqual(library.resolve(created.key).key, BUILT_INS[0].key)
            self.assertNotIn("绑定皮肤的方案", [name for name, _ in library.presets()])
            with self.assertRaises(ValueError):
                library.delete_skin("default")


class DesktopAndManagerFeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from desktop import Desktop

        self.profile = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch("desktop.PROFILE", self.profile))
        self.enterContext(patch("appearance.PROFILE", self.profile))
        self.desktop = Desktop(self.app)
        self.addCleanup(self.cleanup_desktop)

    def cleanup_desktop(self):
        self.desktop.quit()
        self.desktop.engine.practice.stop_worker()
        if self.desktop.manager is not None:
            self.desktop.manager.close()
        self.app.processEvents()

    def test_chat_pane_stop_and_memory_dialog_search(self):
        from desktop import MemoryDialog

        pane = self.desktop.chat_for(PRIMARY)
        self.desktop.agent_settings.setValue("endpoint", "https://example.com/v1")
        self.desktop.agent_settings.setValue("model", "demo")
        self.desktop.agent_settings.sync()

        started = Event()
        release = Event()

        def slow_model(*args, **kwargs):
            started.set()
            release.wait(timeout=3)
            return "迟到的回复", "", ""

        with patch("desktop.ask_model", side_effect=slow_model):
            self.assertTrue(pane.send_message("帮我写个公式"))
            self.assertTrue(started.wait(timeout=2))
            self.assertTrue(pane.stop_button.isEnabled())
            pane.stop_reply()
            self.assertFalse(pane.busy)
            self.assertEqual(pane.notice.text(), "已停止本次回复。")
            self.assertTrue(pane.retry_button.isEnabled())
            release.set()
            self.app.processEvents()

        self.desktop.archive.remember("电路实验用 5V 电源")
        self.desktop.archive.remember("每周三下午去实验室")
        dialog = MemoryDialog(None, self.desktop.archive, member_name="学伴")
        try:
            self.assertEqual(dialog.windowTitle(), "长期记忆 · 学伴")
            self.assertEqual(dialog.items.count(), 2)
            dialog.search_input.setText("5V")
            self.assertEqual(dialog.items.count(), 1)
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_manager_theme_shades_member_deletion_and_pet_layout_persistence(self):
        from PyQt6.QtCore import QPoint
        from manager import panel_style

        light_css = panel_style("paper", "#f4f0e9")
        # Hover and focus colors should be differentiated in non-slate themes.
        self.assertIn("QListWidget::item:hover:!selected", light_css)
        self.assertNotEqual(light_css, panel_style("slate", "#1a232b"))

        saved = self.desktop.save_member(Member("temp-member", "临时伙伴", "协助测试", visible=True))
        self.assertIn(saved.key, self.desktop.pets)
        manager = self.desktop.get_manager()
        manager.open_model(saved.key)

        self.desktop.delete_member(saved.key)
        self.assertNotIn(saved.key, self.desktop.pets)
        self.assertNotIn(saved.key, {m.key for m in self.desktop.team.store.members()})
        self.assertEqual(manager.model_target.currentData(), "")

        panel = self.desktop.engine.practice.panel
        panel.move(QPoint(120, 140))
        self.desktop.save_pet_layout(PRIMARY)
        panel.move(QPoint(10, 10))
        self.assertTrue(self.desktop.restore_pet_layout(PRIMARY, panel))
        self.assertEqual((panel.x(), panel.y()), (120, 140))

        self.desktop.reset_pet_positions()
        self.assertEqual(self.desktop.preferences.value(f"pet_layouts/{PRIMARY}", "", type=str), "")

    def test_delete_active_skin_and_keep_files_on_rename_failure(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QMessageBox
        from appearance import ART, BUILT_INS, SkinLibrary

        library = SkinLibrary()
        source = ART / "whale-maid"
        assets = {name: (source / name).read_bytes() for name in ("idle.gif", "side.gif", "bottom.gif")}
        skin = library.create("Testing pet", assets)
        library.select(skin)
        library.save_preset("Active testing pet", skin)
        panel = self.desktop.engine.practice.panel
        panel.refresh_skin()
        helper = self.desktop.save_member(Member("skin-helper", "皮肤测试员", "测试", skin=skin.key, visible=True))
        other = self.desktop.pets[helper.key]
        area = self.app.primaryScreen().availableGeometry()
        other.peek.dock(Qt.Edge.LeftEdge, area.center(), area)
        other.hide()

        self.desktop.show_appearance()
        editor = self.desktop.manager.skin_editor
        self.app.processEvents()
        self.assertTrue(panel.sprite.movie().isValid())
        self.assertTrue(other.peek.frames[False].movie.isValid())
        self.assertIsNotNone(editor.preview_movie)
        self.assertTrue(editor.preview_movie.isValid())
        original = {file.name: file.read_bytes() for file in skin.path.iterdir()}
        with patch("appearance.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            with patch.object(Path, "rename", side_effect=PermissionError("file in use")):
                editor.delete_skin()
            self.assertEqual({file.name: file.read_bytes() for file in skin.path.iterdir()}, original)
            self.assertEqual(library.current().key, skin.key)
            self.assertIn("Active testing pet", [name for name, _ in library.presets()])
            self.assertEqual(panel.skin.key, skin.key)
            self.assertEqual(other.skin.key, skin.key)
            self.assertTrue(editor.preview_movie.isValid())

            editor.delete_skin()
        self.assertFalse(skin.path.exists())
        self.assertFalse(list(library.installed.glob(".removed-*")))
        self.assertEqual(library.current().key, BUILT_INS[0].key)
        self.assertEqual(panel.skin.key, BUILT_INS[0].key)
        self.assertEqual(other.skin.key, BUILT_INS[0].key)
        self.assertNotIn("Active testing pet", [name for name, _ in library.presets()])

    def test_default_model_rejects_raw_api_key_in_key_name(self):
        manager = self.desktop.get_manager()
        manager.open_model("")
        manager.model_fields["endpoint"].setText("https://api.example.com/v1")
        manager.model_fields["model"].setText("gpt-4o-mini")
        manager.model_fields["key_name"].setText("sk-raw-secret-key")
        manager.save_model()
        self.assertIn("环境变量名", manager.model_notice.text())


if __name__ == "__main__":
    unittest.main()
