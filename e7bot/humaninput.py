"""拟人化键鼠输入（SendInput）。

为什么不用 pyautogui / SendMessage：
* `SendMessage(WM_LBUTTONDOWN)` 对使用 Raw Input / DirectInput 的游戏**完全无效**，
  Unity 客户端一律收不到；
* `SetCursorPos` + `mouse_event` 能生效但轨迹是瞬移，行为特征非常"机器人"；
* 正确做法是 `SendInput`，它进入的是 Windows 系统输入队列，与真实硬件事件同级，
  是内核级反作弊眼里唯一"正常"的来源。

本模块在此之上做了拟人化：
* 鼠标按三次贝塞尔曲线移动，速度用 ease-in-out，距离越远越慢；
* 落点加高斯抖动、按下时长随机；
* 键盘用扫描码（scan code）发送，兼容 DirectInput；
* 全局急停回调，任何一步都能被打断。
"""

from __future__ import annotations

import ctypes
import math
import random
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Optional

if sys.platform != "win32":  # pragma: no cover
    raise RuntimeError("e7bot 仅支持 Windows")

user32 = ctypes.WinDLL("user32", use_last_error=True)

# --------------------------------------------------------------------------- #
# SendInput 结构体
# --------------------------------------------------------------------------- #

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

ULONG_PTR = ctypes.POINTER(wintypes.ULONG)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT


def _send(*inputs: INPUT) -> int:
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    if sent != n:
        raise OSError(f"SendInput 失败 (err={ctypes.get_last_error()})")
    return sent


def _mouse_input(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
    return INPUT(
        type=INPUT_MOUSE,
        u=_INPUTUNION(
            mi=MOUSEINPUT(dx=dx, dy=dy, mouseData=data, dwFlags=flags, time=0, dwExtraInfo=None)
        ),
    )


# --------------------------------------------------------------------------- #
# 虚拟键名 -> VK 码
# --------------------------------------------------------------------------- #

VK: dict[str, int] = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "shift": 0x10,
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "pause": 0x13, "capslock": 0x14,
    "esc": 0x1B, "escape": 0x1B, "space": 0x20, "pageup": 0x21, "pagedown": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "insert": 0x2D, "delete": 0x2E, "num0": 0x60, "num1": 0x61, "num2": 0x62,
    "num3": 0x63, "num4": 0x64, "num5": 0x65, "num6": 0x66, "num7": 0x67,
    "num8": 0x68, "num9": 0x69, "multiply": 0x6A, "add": 0x6B, "subtract": 0x6D,
    "decimal": 0x6E, "divide": 0x6F,
}
for _i in range(1, 25):
    VK[f"f{_i}"] = 0x6F + _i  # F1=0x70
for _c in "abcdefghijklmnopqrstuvwxyz":
    VK[_c] = ord(_c.upper())
for _d in "0123456789":
    VK[_d] = ord(_d)


def vk_of(key: str) -> int:
    k = key.strip().lower()
    if k in VK:
        return VK[k]
    if len(k) == 1:
        return ord(k.upper())
    raise KeyError(f"未知按键: {key!r}")


# --------------------------------------------------------------------------- #
# 拟人化参数
# --------------------------------------------------------------------------- #


@dataclass
class Humanizer:
    """控制"像人"的程度。想快就把速度调高、抖动调小。"""

    speed: float = 1.0            # >1 更快, <1 更慢
    move_jitter_px: float = 2.5   # 落点高斯抖动标准差（像素）
    click_hold_ms: tuple[int, int] = (45, 110)
    pre_click_delay: tuple[int, int] = (60, 180)
    post_click_delay: tuple[int, int] = (90, 260)
    key_hold_ms: tuple[int, int] = (35, 80)
    curve_deviation: float = 0.18  # 贝塞尔控制点相对位移的比例
    tremor: float = 0.6            # 移动途中的细微抖动

    def hold(self, span: tuple[int, int]) -> float:
        lo, hi = span
        return random.uniform(lo, hi) / 1000.0

    def sleep_pre_click(self) -> None:
        time.sleep(self.hold(self.pre_click_delay) / max(self.speed, 0.05))

    def sleep_post_click(self) -> None:
        time.sleep(self.hold(self.post_click_delay) / max(self.speed, 0.05))


# --------------------------------------------------------------------------- #
# 鼠标
# --------------------------------------------------------------------------- #


class Mouse:
    def __init__(self, human: Optional[Humanizer] = None, stop_check: Optional[Callable[[], bool]] = None):
        self.human = human or Humanizer()
        self._stop = stop_check or (lambda: False)
        self._last: Optional[tuple[int, int]] = None

    # -- 坐标换算 ---------------------------------------------------------- #

    @staticmethod
    def _virtual_desktop() -> tuple[int, int, int, int]:
        g = user32.GetSystemMetrics
        return (g(SM_XVIRTUALSCREEN), g(SM_YVIRTUALSCREEN),
                g(SM_CXVIRTUALSCREEN), g(SM_CYVIRTUALSCREEN))

    def _to_absolute(self, x: int, y: int) -> tuple[int, int]:
        vx, vy, vw, vh = self._virtual_desktop()
        ax = int(round((x - vx) * 65535.0 / max(vw - 1, 1)))
        ay = int(round((y - vy) * 65535.0 / max(vh - 1, 1)))
        return max(0, min(65535, ax)), max(0, min(65535, ay))

    def position(self) -> tuple[int, int]:
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return (pt.x, pt.y)

    # -- 移动 -------------------------------------------------------------- #

    def move_to(self, x: int, y: int, duration: Optional[float] = None) -> None:
        """三次贝塞尔 + ease-in-out 移动，途中做细微抖动。"""
        sx, sy = self.position()
        dist = math.hypot(x - sx, y - sy)
        if dist < 1:
            self._last = (x, y)
            return

        if duration is None:
            # 距离越远越慢：0.10s ~ 0.45s，再按 speed 缩放
            duration = (0.10 + min(dist, 1200) / 1200 * 0.35) / max(self.human.speed, 0.05)
        duration = max(duration, 0.03)

        dev = dist * self.human.curve_deviation
        c1 = (sx + (x - sx) * 0.30 + random.uniform(-dev, dev),
              sy + (y - sy) * 0.30 + random.uniform(-dev, dev))
        c2 = (sx + (x - sx) * 0.70 + random.uniform(-dev, dev),
              sy + (y - sy) * 0.70 + random.uniform(-dev, dev))

        steps = max(8, min(48, int(dist / 18) + 8))
        # 每步至少留 3ms：SendInput 的绝对移动发得太密时，Windows 会做输入合并，
        # 中间的移动事件可能被丢掉（实测：距离越远、步进越大，残留误差越大）。
        duration = max(duration, steps * 0.003)

        t0 = time.perf_counter()
        for i in range(1, steps + 1):
            if self._stop():
                return
            t = i / steps
            e = t * t * (3 - 2 * t)  # smoothstep
            mt = 1 - e
            bx = (mt ** 3) * sx + 3 * (mt ** 2) * e * c1[0] + 3 * mt * (e ** 2) * c2[0] + (e ** 3) * x
            by = (mt ** 3) * sy + 3 * (mt ** 2) * e * c1[1] + 3 * mt * (e ** 2) * c2[1] + (e ** 3) * y
            if i < steps:
                bx += random.gauss(0, self.human.tremor)
                by += random.gauss(0, self.human.tremor)
            ax, ay = self._to_absolute(int(round(bx)), int(round(by)))
            _send(_mouse_input(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, ax, ay))
            target_t = t0 + duration * t
            slack = target_t - time.perf_counter()
            if slack > 0:
                time.sleep(slack)

        # 兜底：插值过程可能被系统合并掉最后一个事件，这里补一次精确落点，
        # 保证光标一定停在目标像素上。少了这一步，点击就会整体偏移几个到几十个像素。
        if not self._stop():
            ax, ay = self._to_absolute(x, y)
            _send(_mouse_input(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, ax, ay))
        self._last = (x, y)

    def move_to_instant(self, x: int, y: int) -> None:
        """瞬移（仅用于采集模板 / 校准，不要用在正式跑脚本里）。"""
        ax, ay = self._to_absolute(x, y)
        _send(_mouse_input(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, ax, ay))
        self._last = (x, y)

    # -- 点击 -------------------------------------------------------------- #

    def click(
        self,
        x: int,
        y: int,
        button: str = "left",
        clicks: int = 1,
        jitter: Optional[float] = None,
        move: bool = True,
    ) -> None:
        if jitter is None:
            jitter = self.human.move_jitter_px
        tx = int(round(x + random.gauss(0, jitter))) if jitter else x
        ty = int(round(y + random.gauss(0, jitter))) if jitter else y

        down, up = {
            "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
            "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
            "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
        }[button]

        if move:
            self.move_to(tx, ty)
        else:
            self.move_to_instant(tx, ty)

        for i in range(clicks):
            if self._stop():
                return
            self.human.sleep_pre_click()
            _send(_mouse_input(down))
            time.sleep(self.human.hold(self.human.click_hold_ms))
            _send(_mouse_input(up))
            self.human.sleep_post_click()
            if i + 1 < clicks:
                time.sleep(random.uniform(0.08, 0.20) / max(self.human.speed, 0.05))

    def scroll(self, amount: int, x: Optional[int] = None, y: Optional[int] = None) -> None:
        """amount > 0 向上滚，< 0 向下滚（单位: 120 的倍数）。"""
        if x is not None and y is not None:
            self.move_to(x, y)
        step = 120 if amount > 0 else -120
        for _ in range(abs(int(amount))):
            if self._stop():
                return
            _send(_mouse_input(MOUSEEVENTF_WHEEL, data=step))
            time.sleep(random.uniform(0.03, 0.09))

    def drag(self, x1: int, y1: int, x2: int, y2: int, hold: float = 0.12) -> None:
        self.move_to(x1, y1)
        time.sleep(self.human.hold(self.human.pre_click_delay))
        _send(_mouse_input(MOUSEEVENTF_LEFTDOWN))
        time.sleep(hold)
        self.move_to(x2, y2)
        time.sleep(hold)
        _send(_mouse_input(MOUSEEVENTF_LEFTUP))


# --------------------------------------------------------------------------- #
# 键盘
# --------------------------------------------------------------------------- #


class Keyboard:
    def __init__(self, human: Optional[Humanizer] = None, stop_check: Optional[Callable[[], bool]] = None):
        self.human = human or Humanizer()
        self._stop = stop_check or (lambda: False)

    @staticmethod
    def _scan(vk: int) -> int:
        return user32.MapVirtualKeyW(vk, 0)  # MAPVK_VK_TO_VSC

    def _key_input(self, vk: int, up: bool = False) -> INPUT:
        flags = KEYEVENTF_SCANCODE
        if up:
            flags |= KEYEVENTF_KEYUP
        if vk in (0x25, 0x26, 0x27, 0x28, 0x21, 0x22, 0x23, 0x24, 0x2D, 0x2E, 0x5B, 0x5C, 0x0D):
            flags |= KEYEVENTF_EXTENDEDKEY
        return INPUT(
            type=INPUT_KEYBOARD,
            u=_INPUTUNION(ki=KEYBDINPUT(wVk=0, wScan=self._scan(vk), dwFlags=flags, time=0, dwExtraInfo=None)),
        )

    def key_down(self, key: str) -> None:
        _send(self._key_input(vk_of(key), up=False))

    def key_up(self, key: str) -> None:
        _send(self._key_input(vk_of(key), up=True))

    def tap(self, key: str, hold: Optional[float] = None) -> None:
        if self._stop():
            return
        vk = vk_of(key)
        _send(self._key_input(vk, up=False))
        time.sleep(hold if hold is not None else self.human.hold(self.human.key_hold_ms))
        _send(self._key_input(vk, up=True))
        time.sleep(random.uniform(0.04, 0.12) / max(self.human.speed, 0.05))

    def hotkey(self, *keys: str) -> None:
        """按下组合键，例如 hotkey('ctrl','shift','a')。"""
        for k in keys:
            self.key_down(k)
            time.sleep(random.uniform(0.02, 0.06))
        for k in reversed(keys):
            self.key_up(k)
            time.sleep(random.uniform(0.02, 0.06))

    def type_text(self, text: str, interval: tuple[int, int] = (35, 90)) -> None:
        """逐字符输入（只用 VK，不处理中文/输入法）。"""
        for ch in text:
            if self._stop():
                return
            self.tap(ch)
            time.sleep(random.uniform(*interval) / 1000.0)
