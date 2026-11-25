"""Read a chosen Windows window and resolve physical display coordinates."""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w

from PIL import Image


class WindowError(RuntimeError):
    pass


class MonitorInfo(c.Structure):
    _fields_ = [
        ("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT),
        ("dwFlags", w.DWORD), ("device", w.WCHAR * 32),
    ]


class BitmapInfo(c.Structure):
    _fields_ = [
        ("size", w.DWORD), ("width", w.LONG), ("height", w.LONG),
        ("planes", w.WORD), ("bits", w.WORD), ("compression", w.DWORD),
        ("image_size", w.DWORD), ("x_resolution", w.LONG), ("y_resolution", w.LONG),
        ("colors_used", w.DWORD), ("important_colors", w.DWORD),
    ]


class LastInput(c.Structure):
    _fields_ = [("size", w.UINT), ("tick", w.DWORD)]


user = c.WinDLL("user32")
gdi = c.WinDLL("gdi32")
dwm = c.WinDLL("dwmapi")
kernel = c.WinDLL("kernel32")
EnumProc = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)


def bind(dll, name, result, *arguments):
    function = getattr(dll, name)
    function.restype = result
    function.argtypes = arguments
    return function


enum_windows = bind(user, "EnumWindows", w.BOOL, EnumProc, w.LPARAM)
is_visible = bind(user, "IsWindowVisible", w.BOOL, w.HWND)
is_minimized = bind(user, "IsIconic", w.BOOL, w.HWND)
window_text = bind(user, "GetWindowTextW", c.c_int, w.HWND, w.LPWSTR, c.c_int)
window_rect = bind(user, "GetWindowRect", w.BOOL, w.HWND, c.POINTER(w.RECT))
window_process = bind(user, "GetWindowThreadProcessId", w.DWORD, w.HWND, c.POINTER(w.DWORD))
window_at = bind(user, "WindowFromPoint", w.HWND, w.POINT)
ancestor = bind(user, "GetAncestor", w.HWND, w.HWND, w.UINT)
monitor_at = bind(user, "MonitorFromPoint", w.HANDLE, w.POINT, w.DWORD)
monitor_info = bind(user, "GetMonitorInfoW", w.BOOL, w.HANDLE, c.POINTER(MonitorInfo))
dwm_attribute = bind(dwm, "DwmGetWindowAttribute", c.c_long, w.HWND, w.DWORD, c.c_void_p, w.DWORD)
ping = bind(user, "SendMessageTimeoutW", w.LPARAM, w.HWND, w.UINT, w.WPARAM, w.LPARAM,
            w.UINT, w.UINT, c.POINTER(c.c_size_t))
print_window = bind(user, "PrintWindow", w.BOOL, w.HWND, w.HDC, w.UINT)
new_dc = bind(gdi, "CreateCompatibleDC", w.HDC, w.HDC)
delete_dc = bind(gdi, "DeleteDC", w.BOOL, w.HDC)
new_bitmap = bind(gdi, "CreateDIBSection", w.HBITMAP, w.HDC, c.POINTER(BitmapInfo), w.UINT,
                  c.POINTER(c.c_void_p), w.HANDLE, w.DWORD)
select_object = bind(gdi, "SelectObject", w.HANDLE, w.HDC, w.HANDLE)
delete_object = bind(gdi, "DeleteObject", w.BOOL, w.HANDLE)
flush = bind(gdi, "GdiFlush", w.BOOL)
open_process = bind(kernel, "OpenProcess", w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
process_image = bind(kernel, "QueryFullProcessImageNameW", w.BOOL, w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD))
close_handle = bind(kernel, "CloseHandle", w.BOOL, w.HANDLE)
tick_count = bind(kernel, "GetTickCount", w.DWORD)
last_input = bind(user, "GetLastInputInfo", w.BOOL, c.POINTER(LastInput))
key_state = bind(user, "GetAsyncKeyState", c.c_short, c.c_int)
cursor_position = bind(user, "GetCursorPos", w.BOOL, c.POINTER(w.POINT))
system_metrics = bind(user, "GetSystemMetrics", c.c_int, c.c_int)


def primary_mouse():
    point = w.POINT()
    if not cursor_position(c.byref(point)):
        return None
    button = 2 if system_metrics(23) else 1  # SM_SWAPBUTTON; VK_RBUTTON / VK_LBUTTON
    return bool(key_state(button) & 0x8000), point.x, point.y


def window_pid(hwnd):
    pid = w.DWORD()
    window_process(hwnd, c.byref(pid))
    return pid.value


def executable_for(pid):
    handle = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        for size in (2048, 32768):
            length = w.DWORD(size)
            path = c.create_unicode_buffer(size)
            if process_image(handle, 0, path, c.byref(length)):
                return path.value.casefold()
        return None
    finally:
        close_handle(handle)


def idle_seconds():
    record = LastInput(c.sizeof(LastInput), 0)
    if not last_input(c.byref(record)):
        return 0
    return ((tick_count() - record.tick) & 0xFFFFFFFF) / 1000


def window_title(hwnd: int) -> str:
    title = c.create_unicode_buffer(512)
    window_text(hwnd, title, len(title))
    return title.value.strip()


def window_state(hwnd: int) -> tuple[int, int, int, int, int]:
    bounds, pid, cloaked = w.RECT(), w.DWORD(), w.DWORD()
    dwm_attribute(hwnd, 14, c.byref(cloaked), c.sizeof(cloaked))
    if cloaked.value or not is_visible(hwnd) or is_minimized(hwnd) or not window_rect(hwnd, c.byref(bounds)):
        raise WindowError("窗口已关闭、最小化或不可见，请先把它打开。")
    window_process(hwnd, c.byref(pid))
    return (bounds.left, bounds.top, bounds.right - bounds.left, bounds.bottom - bounds.top, pid.value)


def list_windows(query: str) -> list[dict]:
    matches = []
    query = query.casefold()

    @EnumProc
    def add_window(hwnd, _):
        title = window_title(hwnd)
        if title and query in title.casefold():
            try:
                window_state(hwnd)
                matches.append({"window_id": str(hwnd), "title": title})
            except WindowError:
                pass
        return len(matches) < 25

    enum_windows(add_window, 0)
    return matches


def read_window(hwnd: int, width: int, height: int) -> Image.Image:
    if min(width, height) < 2 or width * height > 40_000_000:
        raise WindowError("窗口尺寸超出截图范围，请调小窗口再试。")
    if not ping(hwnd, 0, 0, 0, 2, 300, None):
        raise WindowError("窗口当前没有响应，请等软件恢复后再试。")
    dc = new_dc(None)
    if not dc:
        raise WindowError("无法准备截图画布。")
    bitmap = previous = None
    try:
        info = BitmapInfo(c.sizeof(BitmapInfo), width, -height, 1, 32)
        pixels = c.c_void_p()
        bitmap = new_bitmap(dc, c.byref(info), 0, c.byref(pixels), None, 0)
        if not bitmap or not pixels:
            raise WindowError("无法为窗口截图分配空间。")
        previous = select_object(dc, bitmap)
        if not previous or not (print_window(hwnd, dc, 2) or print_window(hwnd, dc, 0)):
            raise WindowError("该窗口暂时无法读取，请尝试普通窗口模式。")
        flush()
        image = Image.frombytes("RGB", (width, height), c.string_at(pixels, width * height * 4), "raw", "BGRX")
        if all(high - low < 3 for low, high in image.getextrema()):
            raise WindowError("窗口截图为空白，可能是应用限制。请用文字描述你看到的界面。")
        return image
    finally:
        if previous:
            select_object(dc, previous)
        if bitmap:
            delete_object(bitmap)
        delete_dc(dc)


def display_for_point(x: int, y: int) -> tuple[str, tuple[int, int, int, int]]:
    info = MonitorInfo()
    info.cbSize = c.sizeof(info)
    monitor = monitor_at(w.POINT(x, y), 0)
    if not monitor or not monitor_info(monitor, c.byref(info)):
        raise WindowError("目标控件在屏幕之外，请把窗口移回屏幕。")
    rect = info.rcMonitor
    return info.device, (rect.left, rect.top, rect.right, rect.bottom)


def point_belongs_to(hwnd: int, x: int, y: int) -> bool:
    hit = window_at(w.POINT(x, y))
    return bool(hit and ancestor(hit, 2) == hwnd)
