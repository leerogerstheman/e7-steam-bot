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

import re
import time
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np

from .winutil import Rect, is_primary_monitor, monitor_rect_of

#: Windows 允许的显示缩放档位。用来把 DXGI 报告的**逻辑分辨率**
#: 反推回 Windows 的**物理分辨率**。
DISPLAY_SCALES: tuple[float, ...] = (
    1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0, 3.5, 4.0,
)

#: bettercam.output_info() 的格式（它是人类可读的 dump，不是结构化数据）：
#:     Device[0] Output[0]: Res:(2048, 1152) Rot:0 Primary:True
_OUTPUT_RE = re.compile(
    r"Device\[(\d+)\]\s+Output\[(\d+)\]:\s+"
    r"Res:\((\d+),\s*(\d+)\)\s+Rot:(\d+)\s+Primary:(\w+)",
    re.IGNORECASE,
)


class CaptureError(RuntimeError):
    pass


def parse_output_info(text: str) -> list[tuple[int, int, int, int, bool]]:
    """解析 `bettercam.output_info()` 的字符串。

    返回 `[(device_idx, output_idx, width, height, is_primary), ...]`。
    宽高是 **DXGI 报告的分辨率**，在开启显示缩放时会小于物理分辨率
    （例如 2560×1440 物理 + 125% 缩放 -> 报告 2048×1152）。
    """
    out: list[tuple[int, int, int, int, bool]] = []
    for m in _OUTPUT_RE.finditer(text or ""):
        out.append((
            int(m.group(1)), int(m.group(2)),
            int(m.group(3)), int(m.group(4)),
            m.group(6).strip().lower() in ("true", "1", "yes"),
        ))
    return out


def implied_scale(out_w: int, out_h: int, mon_w: int, mon_h: int,
                  tol: int = 2) -> Optional[float]:
    """DXGI 输出分辨率 -> 目标显示器物理分辨率，需要的缩放比。对不上返回 None。"""
    if out_w <= 0 or out_h <= 0:
        return None
    for s in DISPLAY_SCALES:
        if abs(out_w * s - mon_w) <= tol and abs(out_h * s - mon_h) <= tol:
            return s
    return None


def bettercam_output_candidates(monitor_rect: Rect,
                                info_text: Optional[str] = None
                                ) -> list[tuple[int, Optional[int]]]:
    """给出 (device_idx, output_idx) 候选列表，**最可能的排在最前**。

    为什么需要这个而不是直接传显示器索引：

    DXGI 的输出是**按适配器**枚举的。双显卡笔记本上 NVIDIA 与 Intel 各驱动一块屏，
    两块 GPU 的 output 索引**都从 0 开始**，而 Windows 的显示器索引是全局的。
    实测踩过：把 Windows 索引 1 当 output_idx 传进去，在只有 1 个输出的 NVIDIA 上
    直接 `IndexError` —— 于是 bettercam 静默失效、退回慢得多的 mss。

    这里用「分辨率 × 缩放比」把 DXGI 输出匹配到目标显示器，匹配不上的排后面兜底，
    调用方逐个尝试即可。
    """
    if info_text is None:
        try:
            import bettercam

            info_text = bettercam.output_info()
        except Exception:
            return [(0, None)]

    outputs = parse_output_info(info_text)
    if not outputs:
        return [(0, None)]

    want_primary = is_primary_monitor(monitor_rect)
    scored: list[tuple[tuple[int, int], int, Optional[int]]] = []

    for dev, out, w, h, prim in outputs:
        scale = implied_scale(w, h, monitor_rect.width, monitor_rect.height)
        # 排序键：先看分辨率是否对得上（对得上优先），再看主显示器标志是否一致
        key = (0 if scale is not None else 1, 0 if prim == want_primary else 1)
        scored.append((key, dev, out))

    scored.sort(key=lambda t: t[0])
    cands: list[tuple[int, Optional[int]]] = [(dev, out) for _k, dev, out in scored]

    # 最后再兜一个"让 bettercam 自己选默认输出"
    if (0, None) not in cands:
        cands.append((0, None))
    return cands


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
    """DXGI Desktop Duplication（推荐）。

    **必须传 `monitor_rect`**（目标显示器的物理矩形）。只给索引是不够的 ——
    见 `bettercam_output_candidates` 的说明：DXGI 按适配器枚举输出，
    双显卡笔记本上索引与 Windows 的显示器索引对不上。
    """

    name = "bettercam"
    supports_occluded = False

    def __init__(self, monitor_index: int = 0, target_fps: int = 60,
                 monitor_rect: Optional[Rect] = None, verbose: bool = False):
        import bettercam  # 延迟导入，缺库时其它后端仍可用

        candidates = (
            bettercam_output_candidates(monitor_rect)
            if monitor_rect is not None and monitor_rect.width > 0
            else [(0, None)]
        )

        cam = None
        errors: list[str] = []
        for dev_idx, out_idx in candidates:
            try:
                cam = bettercam.create(
                    device_idx=dev_idx, output_idx=out_idx,
                    output_color="BGR", max_buffer_len=2,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"device={dev_idx} output={out_idx}: {exc}")
                cam = None
            if cam is not None:
                if verbose:
                    print(f"[capture] bettercam 使用 device_idx={dev_idx} output_idx={out_idx}")
                self._used = (dev_idx, out_idx)
                break

        if cam is None:
            raise CaptureError(
                "bettercam 创建失败（显卡/驱动不支持 DXGI 复制）。已尝试: "
                + "; ".join(errors[:4])
            )

        self._cam = cam
        self._monitor_index = monitor_index
        self._monitor_rect = monitor_rect
        self._cam.start(target_fps=target_fps, video_mode=True)
        # 等待首帧
        for _ in range(40):
            if self._cam.get_latest_frame() is not None:
                break
            time.sleep(0.05)

    def grab(self, rect: Rect) -> np.ndarray:
        """抓取 `rect`（**屏幕物理坐标**）对应的画面。

        这里有两处必须小心的地方，都踩过坑：

        1. **坐标系**：DXGI 的帧覆盖的是**整块显示器**，而 `rect` 是**屏幕坐标**。
           必须先减去显示器原点。漏掉这步时，游戏在副屏（原点为负）上会让
           裁剪坐标全为负数、被夹成 1 像素宽 —— 而这样得到的窄条**方差并不低**，
           黑屏检测抓不到它，表现是"模板全都不匹配"这种极难定位的症状。

        2. **显示缩放**：DXGI 帧的分辨率是**缩放后**的（实测 2560×1440 的屏在
           150% 缩放下帧只有 1707×960，125% 下是 2048×1152），而 `rect` 是物理像素。
           所以要按 `帧尺寸 / 显示器物理尺寸` 换算。

        最后把结果缩回 `rect` 的物理尺寸，保证 `grab()` 的契约始终是
        "返回恰好 rect 宽高的图"，下游（模板匹配、OCR）不用关心后端差异。
        """
        frame = self._cam.get_latest_frame()
        if frame is None:
            raise CaptureError("bettercam 未返回帧")

        fh, fw = frame.shape[:2]
        mon = self._monitor_rect

        if mon is None or mon.width <= 0 or mon.height <= 0:
            # 没有显示器信息（老调用路径）：退回"当作帧就是整块屏且无缩放"
            x0 = max(0, min(rect.left, fw - 1))
            y0 = max(0, min(rect.top, fh - 1))
            x1 = max(x0 + 1, min(rect.right, fw))
            y1 = max(y0 + 1, min(rect.bottom, fh))
        else:
            sx, sy = fw / mon.width, fh / mon.height
            x0 = int(round((rect.left - mon.left) * sx))
            y0 = int(round((rect.top - mon.top) * sy))
            x1 = int(round((rect.right - mon.left) * sx))
            y1 = int(round((rect.bottom - mon.top) * sy))
            x0 = max(0, min(x0, fw - 1))
            y0 = max(0, min(y0, fh - 1))
            x1 = max(x0 + 1, min(x1, fw))
            y1 = max(y0 + 1, min(y1, fh))

        sub = np.ascontiguousarray(frame[y0:y1, x0:x1])

        if sub.shape[1] != rect.width or sub.shape[0] != rect.height:
            sub = cv2.resize(sub, (rect.width, rect.height), interpolation=cv2.INTER_LINEAR)
        return sub

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
        # 窗口所在显示器的物理矩形 —— bettercam 靠它反查正确的 DXGI 输出，
        # 只给 monitor_index 在双显卡机器上会错（见 bettercam_output_candidates）。
        self.monitor_rect = monitor_rect_of(hwnd)
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
            return BettercamBackend(
                self.monitor_index, self.target_fps,
                monitor_rect=self.monitor_rect, verbose=self.verbose,
            )
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

                # 尺寸校验。后端把坐标系或缩放搞错时会返回错误尺寸的图，
                # 而**这种图的方差可能很高** —— 实测踩到：多显示器 + 显示缩放下，
                # 裁剪坐标没减显示器原点，得到一条 1 像素宽的竖条，方差 91.7，
                # 黑屏检测完全抓不到它，最后表现成"模板全都不匹配"，极难定位。
                # 所以尺寸不符直接判定该后端有问题并换后端。
                if img.shape[1] != rect.width or img.shape[0] != rect.height:
                    self._log(
                        f"后端 {self._backend.name} 返回尺寸不符: "
                        f"期望 {rect.width}x{rect.height}，实得 {img.shape[1]}x{img.shape[0]}"
                    )
                    if self._degrade():
                        continue
                    raise CaptureError(
                        f"帧尺寸不符: 期望 {rect.width}x{rect.height}，"
                        f"实得 {img.shape[1]}x{img.shape[0]}"
                    )

                if attempt == 0 and is_blank(img) and self._backend.name != "printwindow":
                    # 首帧黑屏：可能是后端在这个游戏上失效，尝试降级
                    if self._degrade():
                        continue
                return img
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(0.05)

        # 重试也没救回来。后端**初始化**成功不代表**抓帧**一定能成
        # （驱动重置、独占全屏切换、显示器热插拔都会让它突然失灵），
        # 所以这里再给一次换后端的机会，而不是直接判死刑。
        if self._degrade():
            try:
                return self._backend.grab(rect)
            except Exception as exc:  # noqa: BLE001
                last = exc

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
