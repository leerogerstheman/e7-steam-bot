"""屏幕捕获后端。

第七史诗 PC 端是 Unity + DirectX 渲染，捕获方式的选择很关键：

===============  ==========================  ====================================
后端              原理                        在 DX 游戏上的表现
===============  ==========================  ====================================
bettercam        DXGI Desktop Duplication    推荐。速度快(数百fps)、支持独占全屏
mss              GDI BitBlt                  独占全屏会黑屏；无边框窗口一般可用
printwindow      WM_PRINT / PW_RENDERFULL    DX 渲染通常返回黑图，仅作最后兜底
===============  ==========================  ====================================

DXGI Desktop Duplication 复制的是「显示器输出」，所以：
* 游戏窗口必须真实显示在屏幕上（不能最小化，可以被遮挡但不要被别的全屏盖住）；
* 多显示器时只复制目标窗口所在的那块屏。

脚本默认按 bettercam -> mss 的顺序自动挑可用的后端，并用「黑屏检测」验证，
因为反作弊 / 驱动 / 独显集显切换都可能让某个后端失效。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

from .winutil import Rect


class CaptureError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# 后端实现
# --------------------------------------------------------------------------- #


class _Backend:
    name = "base"
    supports_occluded = False

    def grab(self, rect: Rect) -> np.ndarray:  # pragma: no cover - 抽象
        raise NotImplementedError

    def close(self) -> None:
        pass


class BettercamBackend(_Backend):
    """DXGI Desktop Duplication（推荐）。"""

    name = "bettercam"
    supports_occluded = False

    def __init__(self, monitor_index: int = 0, target_fps: int = 60):
        import bettercam  # 延迟导入，缺库时其它后端仍可用

        self._cam = bettercam.create(
            output_idx=monitor_index, output_color="BGR", max_buffer_len=2
        )
        if self._cam is None:
            raise CaptureError("bettercam 创建失败（显卡/驱动不支持 DXGI 复制）")
        self._cam.start(target_fps=target_fps, video_mode=True)
        self._monitor_index = monitor_index
        # 等待首帧
        for _ in range(40):
            if self._cam.get_latest_frame() is not None:
                break
            time.sleep(0.05)

    def grab(self, rect: Rect) -> np.ndarray:
        frame = self._cam.get_latest_frame()
        if frame is None:
            raise CaptureError("bettercam 未返回帧")
        h, w = frame.shape[:2]
        x0 = max(0, min(rect.left, w - 1))
        y0 = max(0, min(rect.top, h - 1))
        x1 = max(x0 + 1, min(rect.right, w))
        y1 = max(y0 + 1, min(rect.bottom, h))
        return np.ascontiguousarray(frame[y0:y1, x0:x1])

    def close(self) -> None:
        try:
            self._cam.stop()
        except Exception:
            pass
        try:
            self._cam.release()
        except Exception:
            pass


class MssBackend(_Backend):
    """GDI BitBlt 抓屏，兼容性最好，独占全屏会黑屏。"""

    name = "mss"
    supports_occluded = False

    def __init__(self):
        import mss

        self._sct = mss.mss()

    def grab(self, rect: Rect) -> np.ndarray:
        shot = self._sct.grab(
            {"left": rect.left, "top": rect.top, "width": rect.width, "height": rect.height}
        )
        arr = np.frombuffer(shot.raw, dtype=np.uint8)
        return arr.reshape(shot.height, shot.width, 4)[:, :, :3].copy()

    def close(self) -> None:
        try:
            self._sct.close()
        except Exception:
            pass


class PrintWindowBackend(_Backend):
    """窗口自绘抓取，可在窗口被遮挡时工作；但 DX 游戏多半返回黑图。"""

    name = "printwindow"
    supports_occluded = True

    def __init__(self, hwnd: int):
        import win32gui
        import win32ui
        from ctypes import windll

        self._hwnd = hwnd
        self._win32gui = win32gui
        self._win32ui = win32ui
        self._windll = windll

    def grab(self, rect: Rect) -> np.ndarray:
        win32ui, win32gui = self._win32ui, self._win32gui
        hwnd_dc = win32gui.GetWindowDC(self._hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc_dc, rect.width, rect.height)
        save_dc.SelectObject(bmp)
        # PW_RENDERFULLCONTENT = 0x00000002
        ok = self._windll.user32.PrintWindow(self._hwnd, save_dc.GetSafeHdc(), 2)
        info = bmp.GetInfo()
        buf = bmp.GetBitmapBits(True)
        img = np.frombuffer(buf, dtype=np.uint8).reshape(info["bmHeight"], info["bmWidth"], 4)
        out = img[:, :, :3].copy()
        win32gui.DeleteObject(bmp.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(self._hwnd, hwnd_dc)
        if not ok:
            raise CaptureError("PrintWindow 失败")
        return out

    def close(self) -> None:
        pass


BACKENDS = {
    "bettercam": BettercamBackend,
    "mss": MssBackend,
    "printwindow": PrintWindowBackend,
}


# --------------------------------------------------------------------------- #
# 统一抓取器
# --------------------------------------------------------------------------- #


@dataclass
class Frame:
    image: np.ndarray  # BGR, shape=(h, w, 3)
    rect: Rect
    ts: float

    @property
    def shape(self) -> tuple[int, int]:
        return (self.image.shape[1], self.image.shape[0])


def is_blank(image: np.ndarray, std_threshold: float = 6.0) -> bool:
    """判断是否为黑屏/纯色帧（后端失效的典型症状）。"""
    if image.size == 0:
        return True
    small = image[::8, ::8]
    return float(small.std()) < std_threshold


class ScreenGrabber:
    """按优先级自动选择可用后端，并在运行中检测失效自动降级。"""

    def __init__(
        self,
        hwnd: int,
        preferred: Optional[list[str]] = None,
        monitor_index: int = 0,
        target_fps: int = 60,
        verbose: bool = True,
    ):
        self.hwnd = hwnd
        self.monitor_index = monitor_index
        self.target_fps = target_fps
        self.verbose = verbose
        self._backend: Optional[_Backend] = None
        self._order = preferred or ["bettercam", "mss", "printwindow"]
        self._failed: set[str] = set()
        self._select_backend()

    # -- 后端管理 ---------------------------------------------------------- #

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[capture] {msg}")

    def _select_backend(self) -> None:
        errors: list[str] = []
        for name in self._order:
            if name in self._failed:
                continue
            try:
                self._backend = self._make(name)
                self._log(f"使用截图后端: {name}")
                return
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: {exc}")
                self._failed.add(name)
        raise CaptureError("所有截图后端均不可用:\n  " + "\n  ".join(errors))

    def _make(self, name: str) -> _Backend:
        if name == "bettercam":
            return BettercamBackend(self.monitor_index, self.target_fps)
        if name == "mss":
            return MssBackend()
        if name == "printwindow":
            return PrintWindowBackend(self.hwnd)
        raise CaptureError(f"未知后端 {name}")

    @property
    def backend_name(self) -> str:
        return self._backend.name if self._backend else "none"

    @property
    def supports_occluded(self) -> bool:
        return bool(self._backend and self._backend.supports_occluded)

    # -- 抓帧 -------------------------------------------------------------- #

    def grab(self, rect: Rect, retries: int = 3) -> np.ndarray:
        last: Optional[Exception] = None
        for attempt in range(retries):
            try:
                img = self._backend.grab(rect)
                if img is None or img.size == 0:
                    raise CaptureError("空帧")
                if attempt == 0 and is_blank(img) and self._backend.name != "printwindow":
                    # 首帧黑屏：可能是后端在这个游戏上失效，尝试降级
                    if self._degrade():
                        continue
                return img
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(0.05)
        raise CaptureError(f"抓帧失败: {last}")

    def _degrade(self) -> bool:
        """当前后端返回黑屏时，切到下一个后端。"""
        current = self._backend.name
        remaining = [n for n in self._order if n not in self._failed and n != current]
        if not remaining:
            self._log(f"后端 {current} 返回黑屏，但已无后备后端")
            return False
        self._log(f"后端 {current} 疑似失效（黑屏），尝试切换 …")
        self._failed.add(current)
        try:
            self.close()
        except Exception:
            pass
        try:
            self._select_backend()
            return True
        except CaptureError:
            return False

    def close(self) -> None:
        if self._backend:
            self._backend.close()
            self._backend = None
