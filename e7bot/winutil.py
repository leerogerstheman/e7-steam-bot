"""Windows 窗口与 DPI 工具。

第七史诗 Steam 端是 Windows 原生客户端（Unity 渲染 + UNCHEATER 内核级反作弊），
因此所有自动化都必须走「外部」路线：只能看屏幕、只能发键鼠事件，
绝不读写游戏进程内存、不注入 DLL、不 hook API。

本模块负责把「游戏窗口客户区」变成一套稳定的坐标系：
屏幕绝对像素 <-> 客户区归一化坐标(0..1)。全脚本内部一律使用归一化坐标，
这样换分辨率 / 换窗口大小都不用改配置。
"""

from __future__ import annotations

import ctypes
import sys
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

if sys.platform != "win32":  # pragma: no cover
    raise RuntimeError("e7bot 仅支持 Windows：第七史诗 Steam 端为 Windows 客户端")

import win32api
import win32con
import win32gui
import win32process


# --------------------------------------------------------------------------- #
# DPI
# --------------------------------------------------------------------------- #

_DPI_MODE: Optional[str] = None


def enable_dpi_awareness() -> str:
    """开启进程 DPI 感知。

    必须在任何截图 / 取窗口矩形之前调用。若不开启，Windows 会对本进程做
    DPI 虚拟化（例如 150% 缩放下 GetClientRect 返回的是逻辑像素），
    导致「截图坐标」和「鼠标坐标」不是同一套像素，点击会整体偏移。
    """
    global _DPI_MODE
    if _DPI_MODE is not None:
        return _DPI_MODE

    # Windows 10 1703+ : DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            _DPI_MODE = "per-monitor-v2"
            return _DPI_MODE
    except Exception:
        pass

    # Windows 8.1+ : PROCESS_PER_MONITOR_DPI_AWARE = 2
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        _DPI_MODE = "per-monitor"
        return _DPI_MODE
    except Exception:
        pass

    # Vista+
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        _DPI_MODE = "system"
    except Exception:
        _DPI_MODE = "none"
    return _DPI_MODE


def dpi_mode() -> str:
    return _DPI_MODE or "unset"


# --------------------------------------------------------------------------- #
# 几何
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Rect:
    """屏幕物理像素矩形。"""

    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)

    def to_screen(self, nx: float, ny: float) -> tuple[int, int]:
        """归一化客户区坐标(0..1) -> 屏幕绝对像素坐标。"""
        return (
            int(round(self.left + nx * self.width)),
            int(round(self.top + ny * self.height)),
        )

    def to_local(self, sx: int, sy: int) -> tuple[float, float]:
        """屏幕绝对像素坐标 -> 归一化客户区坐标(0..1)。"""
        if self.width <= 0 or self.height <= 0:
            raise ValueError("窗口尺寸非法")
        return ((sx - self.left) / self.width, (sy - self.top) / self.height)

    def sub(self, nx: float, ny: float, nw: float, nh: float) -> "Rect":
        """按归一化比例切出一个子区域（用于限制模板搜索范围）。"""
        return Rect(
            self.left + int(round(nx * self.width)),
            self.top + int(round(ny * self.height)),
            max(1, int(round(nw * self.width))),
            max(1, int(round(nh * self.height))),
        )

    def clamp_to(self, bounds: "Rect") -> "Rect":
        left = max(self.left, bounds.left)
        top = max(self.top, bounds.top)
        right = min(self.right, bounds.right)
        bottom = min(self.bottom, bounds.bottom)
        return Rect(left, top, max(0, right - left), max(0, bottom - top))

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.width, self.height)


# --------------------------------------------------------------------------- #
# 窗口查找
# --------------------------------------------------------------------------- #


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    pid: int
    exe: str
    client: Rect
    visible: bool
    minimized: bool

    def describe(self) -> str:
        return (
            f"hwnd=0x{self.hwnd:X} pid={self.pid} exe={self.exe or '?'} "
            f"title={self.title!r} client={self.client.as_tuple()}"
        )


class GameNotFound(RuntimeError):
    """没找到游戏窗口。

    这属于「用户还没启动游戏」的正常情况，不是程序缺陷，所以单独给一个异常类型，
    让上层输出干净提示而不是甩一段 traceback。
    """


def _pid_exe(pid: int) -> str:
    try:
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        try:
            return win32process.GetModuleFileNameEx(handle, 0)
        finally:
            win32api.CloseHandle(handle)
    except Exception:
        return ""


def client_rect_screen(hwnd: int) -> Rect:
    """取窗口客户区在屏幕上的物理像素矩形（已排除标题栏/边框）。"""
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    sx, sy = win32gui.ClientToScreen(hwnd, (left, top))
    return Rect(sx, sy, right - left, bottom - top)


def is_minimized(hwnd: int) -> bool:
    return bool(win32gui.IsIconic(hwnd))


def is_foreground(hwnd: int) -> bool:
    return win32gui.GetForegroundWindow() == hwnd


def activate(hwnd: int, settle: float = 0.35) -> bool:
    """把游戏窗口切到前台。

    合成键鼠事件（SendInput）只会送给前台窗口，所以每次操作前都要确保
    游戏在前台 —— 这同时也让它最接近「真人在玩」的行为特征。
    """
    if is_minimized(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.25)

    if is_foreground(hwnd):
        return True

    # 某些情况下 SetForegroundWindow 会被系统拒绝（前台锁），
    # 用 Alt 键「热身」可以绕过该限制。
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        try:
            win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
            win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            return False

    deadline = time.time() + max(settle, 0.5)
    while time.time() < deadline:
        if is_foreground(hwnd):
            time.sleep(settle)
            return True
        time.sleep(0.03)
    return is_foreground(hwnd)


def list_windows() -> list[WindowInfo]:
    """枚举所有可见的顶层窗口（用于交互式挑选游戏窗口）。"""
    out: list[WindowInfo] = []

    def _cb(hwnd: int, _param) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            rect = client_rect_screen(hwnd)
            out.append(
                WindowInfo(
                    hwnd=hwnd,
                    title=title,
                    class_name=win32gui.GetClassName(hwnd),
                    pid=pid,
                    exe=_pid_exe(pid),
                    client=rect,
                    visible=True,
                    minimized=is_minimized(hwnd),
                )
            )
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_cb, None)
    return out


def _title_coverage(title: str, patterns: list) -> float:
    """标题正则匹配**覆盖了标题的多大比例**（0~1）。

    为什么需要这个而不是简单的 `re.search`：

    实测踩到过 —— `第七史诗` 这个模式匹配到了**浏览器标签页**，
    标题是「第七史诗steam端要上线了… — DeepSeek Harness」，于是脚本把浏览器
    当成了游戏窗口。这类"游戏名只是长标题里的一小段"的假阳性很危险：
    脚本会对着一个完全无关的窗口截图和点击，而失败现象是"识别不到场景"，
    根本联想不到是锁错了窗口。

    覆盖比例能干净地区分两种情况：
      * 窗口标题**就是**游戏名（如 `EpicSeven (Steam)`）-> 接近 1.0
      * 游戏名只是长标题里的一小段 -> 很低（4/38 ≈ 0.1）

    代价是"第七史诗 - Smilegate"这种带后缀的标题覆盖比例只有 ~0.3，
    会被拒绝。这是**刻意选择安全侧**：宁可找不到（有明确报错 + `--list-windows`
    可排查），也不要锁错窗口乱点。
    """
    best = 0.0
    for r in patterns:
        m = r.search(title)
        if m:
            best = max(best, (m.end() - m.start()) / max(len(title), 1))
    return best


def find_window(
    title_patterns: Iterable[str] = (),
    exe_patterns: Iterable[str] = (),
    exclude_patterns: Iterable[str] = (),
    min_size: tuple[int, int] = (640, 360),
    min_title_coverage: float = 0.5,
    picker: Optional[Callable[[list[WindowInfo]], Optional[WindowInfo]]] = None,
) -> Optional[WindowInfo]:
    """按窗口标题 / 进程名找游戏窗口。

    匹配分两级，**进程名优先**：

    1. **进程名命中**（最强信号）—— 实测已知 Steam 端主程序是
       `EpicSeven_Steam.exe`，进程名不会像窗口标题那样被浏览器/编辑器撞上。
    2. **标题命中且覆盖比例 >= `min_title_coverage`** —— 仅在没有任何进程名
       命中时才考虑，用于兼容进程名未知的其它客户端版本。

    两级都命不中时返回 None（而不是勉强挑一个），让上层给出明确报错。

    `exclude_patterns` 用来排掉**不该锁定的窗口**：Steam 端启动链路里有
    UNCHEATER 的独立加载器 `ucldr_Epic7_SM_loader_x64.exe`，它可能弹自己的窗口。
    """
    import re

    title_res = [re.compile(p, re.IGNORECASE) for p in title_patterns if p]
    exe_res = [re.compile(p, re.IGNORECASE) for p in exe_patterns if p]
    exclude_res = [re.compile(p, re.IGNORECASE) for p in exclude_patterns if p]

    strong: list[WindowInfo] = []
    weak: list[tuple[float, WindowInfo]] = []
    rejected_titles: list[tuple[float, str]] = []

    for w in list_windows():
        if w.client.width < min_size[0] or w.client.height < min_size[1]:
            continue
        if exclude_res and any(r.search(w.title) or r.search(w.exe) for r in exclude_res):
            continue

        if exe_res and any(r.search(w.exe) for r in exe_res):
            strong.append(w)
            continue

        if title_res:
            cov = _title_coverage(w.title, title_res)
            if cov >= min_title_coverage:
                weak.append((cov, w))
            elif cov > 0.0:
                rejected_titles.append((cov, w.title))

    if rejected_titles and not strong and not weak:
        # 有"像但不是"的窗口时把原因说清楚，否则用户只会看到"没找到游戏窗口"
        for cov, title in rejected_titles[:5]:
            print(
                f"[find_window] 忽略了标题 {title!r}："
                f"游戏名只占标题的 {cov:.0%}（阈值 {min_title_coverage:.0%}），"
                f"不像游戏窗口。若是误判，请调小 window.min_title_coverage 或改用进程名匹配。"
            )

    candidates = strong if strong else [w for _cov, w in sorted(weak, key=lambda t: -t[0])]
    if not candidates:
        return None

    # 优先选已经在后台的、面积最大的那个
    candidates.sort(
        key=lambda w: (is_foreground(w.hwnd), w.client.width * w.client.height),
        reverse=True,
    )
    if len(candidates) == 1 or picker is None:
        return candidates[0]
    return picker(candidates) or candidates[0]


def window_scale_note(rect: Rect) -> str:
    """给出一个人类可读的分辨率标签，例如 '1920x1080'。"""
    return f"{rect.width}x{rect.height}"


# --------------------------------------------------------------------------- #
# 多显示器
# --------------------------------------------------------------------------- #


def list_monitors() -> list[tuple[int, Rect]]:
    """列出所有显示器，返回 [(索引, 屏幕矩形)]。

    DXGI Desktop Duplication 是按**显示器**复制的，不是按窗口。
    游戏放在第二块屏上时，必须把 bettercam 的 output_idx 设成 1，否则抓不到画面。
    """
    out: list[tuple[int, Rect]] = []
    for i, (hmon, _hdc, _rect) in enumerate(win32api.EnumDisplayMonitors()):
        info = win32api.GetMonitorInfo(hmon)
        left, top, right, bottom = info["Monitor"]
        out.append((i, Rect(left, top, right - left, bottom - top)))
    return out


def monitor_index_of(hwnd: int) -> int:
    """窗口当前在哪块显示器上（对应 ScreenGrabber 的 monitor_index）。"""
    try:
        hmon = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
    except Exception:
        return 0
    for i, (m, _hdc, _rect) in enumerate(win32api.EnumDisplayMonitors()):
        if m == hmon:
            return i
    return 0
