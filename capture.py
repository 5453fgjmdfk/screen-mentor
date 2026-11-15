"""屏幕捕获 + DPI 处理。

mss 是线程不安全的：每个线程自己建一个 Capturer。
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass

import mss
from PIL import Image


def enable_dpi_awareness() -> None:
    """必须在创建任何 Qt 窗口之前调用，否则整块界面坐标会偏。"""
    try:
        # 2 = PROCESS_PER_MONITOR_DPI_AWARE
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


@dataclass
class Shot:
    """一次整屏截图（物理像素）。left/top 是虚拟桌面左上角。"""

    image: Image.Image
    left: int
    top: int
    width: int
    height: int


class Capturer:
    def __init__(self) -> None:
        self._sct = mss.mss()

    def monitor(self) -> dict:
        return dict(self._sct.monitors[0])

    def grab(self) -> Shot:
        mon = self._sct.monitors[0]
        raw = self._sct.grab(mon)
        img = Image.frombytes("RGB", raw.size, raw.rgb)
        return Shot(img, mon["left"], mon["top"], mon["width"], mon["height"])

    def close(self) -> None:
        try:
            self._sct.close()
        except Exception:
            pass


def virtual_size() -> tuple[int, int]:
    """整块虚拟桌面的物理像素尺寸（不依赖 Qt）。"""
    with mss.mss() as sct:
        m = sct.monitors[0]
        return m["width"], m["height"]
