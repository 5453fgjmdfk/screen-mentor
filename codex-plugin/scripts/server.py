"""Codex MCP server. Its tools read a window and draw click-through guidance."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import io
import json
import math
import sys
import time
from uuid import uuid4

from capture import enable_dpi_awareness

enable_dpi_awareness()

from PIL import Image, ImageChops, ImageStat
from PyQt6.QtCore import QEventLoop, QObject, QTimer, pyqtSignal

import native
from overlay import GuideOverlay
from practice import CHAT_LEASE_SECONDS, Practice, WindowScope
from protocol import DEFAULT_POLL_SECONDS, PROGRESS_PHASES, tool_error, validated_arguments
from target import tighten
from window_reader import WindowReader


RECONNECT_SECONDS = 60


@dataclass(frozen=True)
class Snapshot:
    id: str
    hwnd: int
    window: tuple[int, int, int, int, int]
    image_size: tuple[int, int]
    captured_at: float


def text_content(data):
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return {"type": "text", "text": text}


class MentorServer(QObject):
    management_requested = pyqtSignal()

    def __init__(self, app, archive, *, create_member=None, list_skins=None):
        super().__init__()
        self.app = app
        self.archive = archive
        self.create_member = create_member
        self.list_skins = list_skins
        self.snapshot = None
        self.snapshot_owner = None
        self.practice = Practice(None, app, persistent=True)
        self.practice.wake.connect(self.invalidate_practice_guide)
        self.practice_id = None
        self.practice_owner = None
        self.practice_frame = None
        self.displayed_frame = None
        self.displayed_regions = ()
        self.guide_window = None
        self.pending_hint = None
        self.action = None
        self.mouse_down = False
        self.pressed_target = False
        self.waiting = False
        self.last_watchdog = self.last_analysis = 0.0
        self.last_window = None
        self.idle_rechecked = False
        self.overlay = GuideOverlay()
        self.guard = QTimer(self)
        self.guard.timeout.connect(self.check_window)
        self.expiration = QTimer(self)
        self.expiration.setSingleShot(True)
        self.expiration.timeout.connect(self.hide_guide)
        self.watchdog = QTimer(self)
        self.watchdog.timeout.connect(self.watch_practice)
        self.action_poll = QTimer(self)
        self.action_poll.setInterval(16)
        self.action_poll.timeout.connect(self.check_action)

    def reset_guide(self):
        self.guard.stop()
        self.expiration.stop()
        self.action_poll.stop()
        self.action = None
        self.pressed_target = False
        self.displayed_frame = self.guide_window = None
        self.displayed_regions = ()

    def hide_guide(self):
        self.reset_guide()
        self.overlay.clear()
        self.overlay.hide()

    def check_action(self):
        mouse = native.primary_mouse()
        if not self.action or not mouse or self.practice.scope is None or self.guide_window is None:
            return
        down, x, y = mouse
        if down == self.mouse_down:
            return
        if self.practice.scope.active() != self.guide_window[0]:
            self.check_window()
            return
        self.mouse_down = down
        (left, top, width, height), destination = self.action
        in_source = left - 4 <= x <= left + width + 4 and top - 4 <= y <= top + height + 4
        if down:
            hwnd = self.guide_window[0]
            self.pressed_target = in_source and native.point_belongs_to(hwnd, x, y)
            return
        matched = (math.hypot(x - destination[0], y - destination[1]) <= 16
                   if destination is not None else in_source)
        accepted = self.pressed_target and matched
        self.pressed_target = False
        if not accepted or not self.practice.running.is_set():
            return
        self.hide_guide()
        self.snapshot = self.practice_frame = None
        self.practice.force = True
        self.practice.recheck_reason = "action_performed"
        self.practice.pending = time.monotonic()
        self.practice.sample_after = self.practice.pending
        self.last_watchdog = self.practice.pending
        self.practice.set_progress("已检测到拖动，正在查看结果" if destination else "已检测到点击，正在查看结果",
                                   pose="reading")
        self.practice.wake.emit()

    def check_window(self):
        if self.guide_window is None:
            return
        hwnd, bounds = self.guide_window
        try:
            same_window = native.window_state(hwnd) == bounds
        except native.WindowError:
            same_window = False
        if not same_window:
            self.hide_guide()
            self.practice.force = True
        elif self.displayed_frame is not None and (self.practice.scope is None or self.practice.scope.active() != hwnd):
            self.conceal_guide()

    def conceal_guide(self):
        self.action_poll.stop()
        self.pressed_target = False
        self.overlay.hide()

    def observe_window(self, window_id, *, live=False):
        hwnd = int(window_id)
        if live:
            practice = self.practice
            frame = practice.current
            if (frame is None or frame[:2] != (hwnd, native.window_state(hwnd))
                    or practice.scope.active() != hwnd or time.monotonic() - practice.last_sample > 4):
                practice.force = True
                raise native.WindowError("正在等待练习窗口的新画面，请切回窗口后继续等待。")
            self.use_practice_frame(frame)
            return self.image_blocks(frame[2])
        self.hide_guide()
        self.snapshot = None
        window = native.window_state(hwnd)
        _, _, width, height, _ = window
        with WindowReader() as reader:
            image = reader.read(hwnd, width, height)
        if native.window_state(hwnd) != window:
            raise native.WindowError("截图时窗口发生了移动，请重新观察。")
        image.thumbnail((1600, 1200), Image.Resampling.LANCZOS)
        self.snapshot = Snapshot(uuid4().hex, hwnd, window, image.size, time.monotonic())
        self.practice_frame = None
        return self.image_blocks(image)

    def use_practice_frame(self, frame):
        hwnd, bounds, image = frame[:3]
        self.practice_frame = frame
        self.snapshot = Snapshot(uuid4().hex, hwnd, bounds, image.size, time.monotonic())
        self.snapshot_owner = self.practice_owner

    @staticmethod
    def encode_image(image):
        with io.BytesIO() as output:
            image.save(output, format="JPEG", quality=88)
            encoded = base64.b64encode(output.getvalue()).decode("ascii")
        return {"type": "image", "mimeType": "image/jpeg", "data": encoded}

    def image_blocks(self, image):
        meta = {"snapshot_id": self.snapshot.id, "width": image.width, "height": image.height}
        return [text_content(meta), self.encode_image(image)]

    def start_practice(self, window_id, additional_window_ids=(), *, owner, practice_id=None):
        windows = tuple(dict.fromkeys(map(int, (window_id, *additional_window_ids))))
        if not self.practice.ending.is_set():
            if practice_id != self.practice_id:
                raise ValueError("另一条对话正在伴学。请先结束原会话，或用原 practice_id 恢复。")
            if not all(self.practice.scope.accepts(hwnd) for hwnd in windows):
                raise ValueError("已有一场不同窗口的伴学。请先结束旧练习，再开始新练习。")
            if self.waiting and owner != self.practice_owner:
                raise ValueError("原连接还在等待画面，请稍后重试。")
            self.practice_owner = owner
            self.renew_practice(explicit=True)
            return [text_content({"practice_id": self.practice_id, "state": "resumed"})]
        if self.waiting:
            raise ValueError("上一场伴学正在退出，请稍后重试。")
        scope = WindowScope(windows)
        self.hide_guide()
        self.snapshot = self.practice_frame = None
        self.pending_hint = None
        self.practice.begin(scope)
        self.practice_id = uuid4().hex
        self.practice_owner = owner
        self.snapshot_owner = None
        self.last_watchdog = self.last_analysis = time.monotonic()
        self.last_window = None
        self.idle_rechecked = False
        self.watchdog.start(1000)
        return [text_content({"practice_id": self.practice_id, "state": "started"})]

    def renew_practice(self, *, explicit=False):
        practice = self.practice
        now = time.monotonic()
        reconnect = practice.chat_suspended or now - practice.last_poll >= CHAT_LEASE_SECONDS
        if reconnect:
            self.hide_guide()
            self.snapshot = self.practice_frame = None
            self.pending_hint = None
            practice.update(None)
        practice.last_poll = now
        practice.chat_suspended = False
        if reconnect or explicit:
            practice.force = True
            practice.recheck_reason = "chat_resumed"
            practice.set_status("已接回原聊天。切到练习窗口，我会重新查看。" if practice.running.is_set()
                                else "已接回原聊天。观察已暂停，点「继续」恢复。")

    def invalidate_practice_guide(self):
        practice = self.practice
        if practice.ending.is_set() or not practice.running.is_set():
            self.hide_guide()
            self.snapshot = self.practice_frame = self.pending_hint = None
            return
        current = practice.current
        if self.practice_frame and (current is None or self.practice_frame[:2] != current[:2]):
            self.snapshot = self.practice_frame = None
        if self.displayed_frame is not None:
            if current is None or practice.scope.active() != current[0]:
                self.conceal_guide()
            elif self.displayed_frame[:2] != current[:2]:
                self.hide_guide()
                practice.force = True
            else:
                changed_region = any(self.region_changed(self.displayed_frame, current, box)
                                     for box in self.displayed_regions)
                if changed_region and not self.overlay.isVisible():
                    # A previously hidden guide must not cover a different control after a task switch.
                    self.hide_guide()
                    practice.force = True
                elif changed_region and time.monotonic() - practice.pending >= .55:
                    self.hide_guide()
                    practice.force = True
                elif not changed_region and not self.overlay.isVisible():
                    self.overlay.restore_guide()
                    if self.action is not None:
                        mouse = native.primary_mouse()
                        self.mouse_down = bool(mouse and mouse[0])
                        self.action_poll.start()
        if self.pending_hint:
            self.display_pending_hint()

    def watch_practice(self):
        practice = self.practice
        if practice is None or practice.ending.is_set():
            self.watchdog.stop()
            return
        now = time.monotonic()
        idle_for = now - practice.last_poll
        if idle_for >= CHAT_LEASE_SECONDS + RECONNECT_SECONDS:
            practice.close()
            return
        if idle_for >= CHAT_LEASE_SECONDS:
            if not practice.chat_suspended:
                practice.chat_suspended = True
                self.hide_guide()
                self.snapshot = self.practice_frame = self.pending_hint = None
                practice.update(None)
                practice.set_progress()
                practice.set_status("原聊天暂时断开。重连前会暂停读图。")
            return
        if not practice.running.is_set():
            return
        hwnd = practice.scope.active()
        if hwnd is None:
            return
        # A hidden overlay needs a fresh frame before being restored.
        if self.guide_window and self.guide_window[0] == hwnd and self.overlay.isVisible():
            if not native.is_visible(int(self.overlay.winId())):
                self.overlay.restore_guide()
        if now - self.last_watchdog < 20:
            return
        if practice.last_sample is None or now - practice.last_sample > 8:
            practice.set_progress()
            if not practice.capture_error:
                practice.set_status("正在等练习窗口返回可用画面。")
            self.last_watchdog = now
            return
        if practice.current is None or practice.current[0] != hwnd or practice.pending is None:
            return
        idle = native.idle_seconds()
        if idle < 8:
            self.idle_rechecked = False
        missing = (not self.overlay.isVisible() or not self.overlay.has_guide) and now - self.last_analysis >= 12
        stalled = idle >= 25 and now - practice.pending >= 12 and not self.idle_rechecked
        if missing or stalled:
            self.last_watchdog = now
            self.idle_rechecked = self.idle_rechecked or stalled
            practice.recheck_reason = "guide_missing" if missing else "idle"
            practice.force = True
            practice.set_status("正在重新查看当前窗口，确认下一步…")
            practice.wake.emit()

    @staticmethod
    def region_changed(first, second, box):
        first_image, second_image = first[2], second[2]
        if first_image.size != second_image.size:
            return True
        iw, ih = first_image.size
        x, y, width, height = box
        left = max(0, min(iw, int(x)))
        top = max(0, min(ih, int(y)))
        right = max(left, min(iw, math.ceil(x + width)))
        bottom = max(top, min(ih, math.ceil(y + height)))
        if right <= left or bottom <= top:
            return False
        crop = (left, top, right, bottom)
        before = first_image.crop(crop)
        after = second_image.crop(crop)
        return max(ImageStat.Stat(ImageChops.difference(before, after)).mean) >= 16

    def wait_for_practice_change(self, practice_id, timeout_seconds=DEFAULT_POLL_SECONDS, *, disconnected):
        practice = self.practice
        if practice is None or practice_id != self.practice_id or self.waiting:
            raise ValueError("当前工具没有对应的伴学会话。")
        if practice.ending.is_set():
            return [text_content({"state": "ended"})]
        self.waiting = True
        self.renew_practice()
        deadline = practice.last_poll + timeout_seconds
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(180)
        timer.timeout.connect(loop.quit)
        practice.wake.connect(loop.quit)
        timer.start()
        try:
            while not practice.ending.is_set():
                if disconnected():
                    return [text_content({"state": "waiting", "reason": "cancelled"})]
                if practice.running.is_set() and practice.issues:
                    problem, images = practice.issues.popleft()
                    blocks = [text_content({"state": "issue", "user_reported": problem,
                                            "screenshot_attached": bool(images), "attachment_count": len(images)})]
                    blocks.extend(self.encode_image(image) for image in images)
                    practice.set_progress("正在查看你提交的反馈", pose="reading")
                    return blocks
                frame = practice.take() if practice.running.is_set() else None
                if frame:
                    hwnd, _, image, _, _ = frame
                    if practice.scope.active() == hwnd:
                        self.use_practice_frame(frame)
                        self.last_analysis = time.monotonic()
                        event = {"state": "changed", "window_title": native.window_title(hwnd)}
                        if self.last_window is not None and self.last_window != hwnd:
                            event["reason"] = "window_changed"
                            practice.recheck_reason = None
                            practice.show_instruction("已切换练习窗口，正在查看新界面…")
                        elif practice.recheck_reason:
                            event["reason"], practice.recheck_reason = practice.recheck_reason, None
                        self.last_window = hwnd
                        if not self.displayed_regions or not self.overlay.isVisible():
                            practice.set_progress("画面已收到，正在判断下一步")
                        return [text_content(event), *self.image_blocks(image)]
                if time.monotonic() >= deadline:
                    if practice.ending.is_set():
                        break
                    state = "waiting" if practice.running.is_set() else "paused"
                    active = practice.scope.active() if practice.scope is not None else None
                    reason = ("waiting_for_window" if active is None else
                              "capture_retry" if practice.capture_error else
                              "no_new_frame" if practice.current is None else "unchanged")
                    event = {"state": state, "reason": reason}
                    if reason == "capture_retry":
                        event["detail"] = practice.capture_error
                    return [text_content(event)]
                loop.exec()
            return [text_content({"state": "ended"})]
        finally:
            timer.stop()
            try:
                practice.wake.disconnect(loop.quit)
            except TypeError:
                pass
            self.waiting = False

    def show_guide(self, snapshot_id, x, y, width, height, text, detail="", step=1, seconds=20,
                   input_field=False, text_to_type="", drag_to_x=None, drag_to_y=None):
        shot = self.snapshot
        if not shot or shot.id != snapshot_id or time.monotonic() - shot.captured_at > 90:
            raise native.WindowError("这张截图已失效，请重新观察窗口。")
        if native.window_state(shot.hwnd) != shot.window:
            self.snapshot = None
            raise native.WindowError("窗口位置或尺寸已变，请重新观察。")
        iw, ih = shot.image_size
        if x + width > iw or y + height > ih or not text.strip():
            raise ValueError("请使用截图范围内的坐标和一条有效提示。")
        is_input = input_field or bool(text_to_type)
        dragging = drag_to_x is not None or drag_to_y is not None
        if dragging and (is_input or drag_to_x is None or drag_to_y is None
                         or drag_to_x >= iw or drag_to_y >= ih):
            raise ValueError("拖动需要提供截图内的落点 x 和 y。")
        regions = [(x, y, width, height)]
        if dragging:
            regions.append((max(0, drag_to_x - 8), max(0, drag_to_y - 8),
                            min(16, iw - max(0, drag_to_x - 8)), min(16, ih - max(0, drag_to_y - 8))))
        if self.practice_frame:
            if self.practice.scope.active() != shot.hwnd:
                raise ValueError("请切回练习窗口后继续。")
            current = self.practice.current
            if current is None or any(self.region_changed(self.practice_frame, current, box) for box in regions):
                self.practice.force = True
                raise ValueError("目标附近已变化，请等待新画面后再标注。")
            if not is_input and not dragging:
                adjusted = tighten(self.practice_frame[4], shot.image_size,
                                   {"x": x, "y": y, "width": width, "height": height})
                x, y, width, height = (adjusted[key] for key in ("x", "y", "width", "height"))
        left, top, ww, wh, _ = shot.window
        sx, sy = ww / iw, wh / ih
        gx, gy, gw, gh = left + x * sx, top + y * sy, width * sx, height * sy
        cx, cy = round(gx + gw / 2), round(gy + gh / 2)
        if not native.point_belongs_to(shot.hwnd, cx, cy):
            raise native.WindowError("目标被遮住了，请把练习窗口里的这个位置露出来。")
        screen, (ml, mt, mr, mb) = self.screen_at(cx, cy)
        drag_to = None
        if dragging:
            dx, dy = round(left + drag_to_x * sx), round(top + drag_to_y * sy)
            if not (ml <= dx < mr and mt <= dy < mb and native.point_belongs_to(shot.hwnd, dx, dy)):
                raise ValueError("拖动落点被遮挡或不在同一屏幕，请重新确认起点和落点。")
            drag_to = dx - ml, dy - mt
        gx, gy, right, bottom = max(gx, ml), max(gy, mt), min(gx + gw, mr), min(gy + gh, mb)
        self.reset_guide()
        self.pending_hint = None
        self.place_overlay(screen, mr - ml, mb - mt)
        live = self.practice_frame is not None
        if text_to_type and text_to_type not in text + detail:
            if len(text_to_type) <= 60 and text_to_type.isprintable():
                detail = f"输入 {text_to_type}" + (f"\n{detail}" if detail else "")
            elif live:
                detail = "从右下角复制后粘贴。" + (f"\n{detail}" if detail else "")
        self.overlay.show_guide((gx - ml, gy - mt, right - gx, bottom - gy), text, detail, step,
                                0 if live else seconds, mark_target=not is_input, drag_to_phys=drag_to)
        self.guide_window = shot.hwnd, shot.window
        if live:
            self.displayed_frame, self.displayed_regions = self.practice_frame, regions
            self.practice.show_instruction(text, text_to_type)
            if not is_input:
                self.action = ((gx, gy, right - gx, bottom - gy), (dx, dy) if dragging else None)
                mouse = native.primary_mouse()
                self.mouse_down = bool(mouse and mouse[0])
                self.pressed_target = False
                self.action_poll.start()
        else:
            self.expiration.start(seconds * 1000)
        self.guard.start(250)
        return [text_content({"shown": True, "surface": "screen", "persistent": live,
                              "seconds": None if live else seconds})]

    def screen_at(self, x, y):
        device, bounds = native.display_for_point(x, y)
        screens = self.app.screens()
        screen = next((s for s in screens if s.name().casefold() == device.casefold()), None)
        if screen is None and len(screens) == 1:
            screen = screens[0]
        if screen is None:
            raise native.WindowError("无法匹配目标显示器，请把练习窗口移到主屏后重试。")
        return screen, bounds

    def place_overlay(self, screen, width, height):
        self.overlay.winId()
        handle = self.overlay.windowHandle()
        if handle.screen() != screen:
            self.overlay.hide()
            handle.setScreen(screen)
        geometry = screen.geometry()
        if self.overlay.geometry() != geometry:
            self.overlay.setGeometry(geometry)
        self.overlay.set_capture_size(width, height)

    def display_pending_hint(self):
        current = self.practice.current
        if current is None or self.practice.scope.active() != current[0]:
            return False
        hwnd, bounds = current[:2]
        left, top, width, height, _ = bounds
        try:
            screen, (ml, mt, mr, mb) = self.screen_at(left + width // 2, top + height // 2)
        except native.WindowError:
            return False
        title, detail = self.pending_hint
        self.pending_hint = None
        self.reset_guide()
        self.place_overlay(screen, mr - ml, mb - mt)
        self.overlay.show_guide(None, title, detail, seconds=0,
                                hint_at_phys=(left + width / 2 - ml, top + min(98, height / 4) - mt))
        self.guide_window = hwnd, bounds
        self.displayed_frame = current
        self.guard.start(250)
        return True

    def call_tool(self, params, owner, *, disconnected=lambda: False):
        try:
            name = params.get("name")
            args = validated_arguments(name, params.get("arguments", {}))
            live = not self.practice.ending.is_set()
            session = args.get("practice_id")
            lesson = {"observe_window", "show_guide", "show_practice_hint", "show_practice_progress", "clear_guide",
                      "wait_for_practice_change", "end_live"}
            if name in lesson and (live or session is not None):
                if session is None or session != self.practice_id or owner != self.practice_owner:
                    raise ValueError("请使用本条对话收到的 practice_id。")
                if not live:
                    if name == "wait_for_practice_change":
                        return {"content": [text_content({"state": "ended"})]}
                    raise ValueError("这场伴学已结束。")
                if not self.practice.running.is_set() and name in lesson - {
                    "clear_guide", "wait_for_practice_change", "end_live",
                }:
                    return {"content": [text_content({"state": "paused", "shown": False})]}
            if name == "open_appearance":
                self.practice.appearance_requested.emit()
                blocks = [text_content({"opened": True, "surface": "appearance_manager"})]
            elif name == "open_management":
                self.management_requested.emit()
                blocks = [text_content({"opened": True, "surface": "desktop_manager"})]
            elif name == "list_member_skins":
                if self.list_skins is None:
                    raise ValueError("桌面成员管理尚未打开。")
                blocks = [text_content({"skins": self.list_skins()})]
            elif name == "create_member":
                if self.create_member is None:
                    raise ValueError("桌面成员管理尚未打开。")
                blocks = [text_content(self.create_member(args))]
            elif name == "start_live":
                blocks = self.start_practice(owner=owner, **args)
            elif name == "end_live":
                summary = args.get("summary", "").strip()
                if summary:
                    self.archive.write("practice", summary)
                self.practice.close()
                blocks = [text_content({"state": "ended", "summary_saved": bool(summary)})]
            elif name == "wait_for_practice_change":
                blocks = self.wait_for_practice_change(**args, disconnected=disconnected)
            elif name == "show_practice_progress":
                if not self.practice or self.practice.ending.is_set() or args["practice_id"] != self.practice_id:
                    raise ValueError("当前工具没有对应的伴学会话。")
                if args.get("snapshot_id") and (not self.snapshot or self.snapshot.id != args["snapshot_id"]):
                    raise ValueError("练习画面已切换，请先等待新画面。")
                label, pose = PROGRESS_PHASES[args.get("phase", "checking")]
                self.practice.set_progress(args["text"].strip(), pose=pose, label=label)
                blocks = [text_content({"shown": True, "surface": "companion_bubble"})]
            elif name == "show_practice_hint":
                if not self.practice or self.practice.ending.is_set() or args["practice_id"] != self.practice_id:
                    raise ValueError("当前工具没有对应的伴学会话。")
                self.practice.show_instruction(args["text"], args.get("text_to_type", ""))
                text, shortcut = args["text"], args.get("shortcut")
                self.pending_hint = (shortcut, text) if shortcut else (text, "")
                shown = self.display_pending_hint() if self.practice.running.is_set() else False
                blocks = [text_content({"shown": shown, "surface": "screen" if shown else "pending_foreground"})]
            elif name == "list_windows":
                blocks = [text_content({"windows": native.list_windows(args.get("query", "").strip())})]
            elif name == "observe_window":
                if live and not self.practice.scope.accepts(int(args["window_id"])):
                    raise ValueError("该窗口不在本场伴学的范围内。")
                blocks = self.observe_window(args["window_id"], live=live)
                self.snapshot_owner = owner
            elif name == "show_guide":
                if self.snapshot_owner != owner:
                    raise ValueError("请使用本条对话取得的快照。")
                args.pop("practice_id", None)
                blocks = self.show_guide(**args)
            else:
                if not live and self.snapshot is not None and self.snapshot_owner != owner:
                    raise ValueError("请在原对话中清除标注。")
                self.hide_guide()
                self.snapshot = None
                self.pending_hint = None
                blocks = [text_content({"cleared": True})]
            return {"content": blocks}
        except (native.WindowError, ValueError) as exc:
            return tool_error(str(exc))

    def respond(self, request, owner, *, disconnected=lambda: False):
        if (not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or "id" not in request
                or request.get("method") != "tools/call" or not isinstance(request.get("params", {}), dict)):
            raise ValueError("Invalid request")
        response = {"jsonrpc": "2.0", "id": request["id"]}
        try:
            response["result"] = self.call_tool(request.get("params", {}), owner, disconnected=disconnected)
        except Exception as exc:
            if sys.excepthook is not sys.__excepthook__:
                sys.excepthook(type(exc), exc, exc.__traceback__)
            response["error"] = {"code": -32603, "message": "伴学窗口暂时无法处理这一步。请重新获取画面。"}
        return response
