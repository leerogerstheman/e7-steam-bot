"""硬件层冒烟测试：真的抓屏、真的发 SendInput。

`run.py selftest` 验证的是识别链路（用合成画面），但**截图后端和输入映射是跟本机
硬件/驱动绑定的**，只能实测。这个脚本就是干这个的：

1. 逐个尝试截图后端，报告分辨率、耗时、画面方差（判断是否黑屏）
2. 实测 SendInput 的虚拟桌面绝对坐标换算：把鼠标移到几个点，用 GetCursorPos 读回来
   比对 —— 多显示器（尤其带负坐标原点）时这里最容易算错
3. 只移动鼠标，**不点击、不按键**

    python tools/smoke_hardware.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from e7bot.capture import (  # noqa: E402
    BettercamBackend,
    CaptureError,
    MssBackend,
    PrintWindowBackend,
    is_blank,
)
from e7bot.humaninput import Humanizer, Mouse  # noqa: E402
from e7bot.winutil import (  # noqa: E402
    Rect,
    enable_dpi_awareness,
    list_monitors,
    list_windows,
)


def pick_window() -> tuple[int, str, Rect]:
    """挑一个够大的可见窗口来试抓屏（不限于游戏）。

    优先真正的应用窗口（排除桌面 Progman/WorkerW），因为抓桌面只能证明后端
    "能出图"，抓应用窗口才更接近抓游戏的情况。
    """
    candidates = []
    for w in list_windows():
        if w.minimized or w.client.width < 700 or w.client.height < 400:
            continue
        candidates.append(w)

    def rank(w) -> tuple:
        is_desktop = w.class_name in ("Progman", "WorkerW", "Shell_TrayWnd")
        return (0 if is_desktop else 1, w.client.width * w.client.height)

    if not candidates:
        raise RuntimeError("没找到够大的可见窗口做测试")
    best = max(candidates, key=rank)
    return best.hwnd, best.title, best.client


def test_backends() -> None:
    print("=" * 74)
    print("[1] 截图后端实测")
    print("=" * 74)

    for i, m in list_monitors():
        print(f"  显示器 {i}: {m.width}x{m.height} @ ({m.left},{m.top})")

    hwnd, title, rect = pick_window()
    print(f"  测试窗口: {title!r}  客户区 {rect.width}x{rect.height} @ ({rect.left},{rect.top})")

    for name, factory in (
        ("bettercam", lambda: BettercamBackend(0, 60)),
        ("mss", MssBackend),
        ("printwindow", lambda: PrintWindowBackend(hwnd)),
    ):
        print(f"\n  --- {name} ---")
        be = None
        try:
            be = factory()
            # 预热
            for _ in range(3):
                be.grab(rect)
            t0 = time.perf_counter()
            n = 15
            img = None
            for _ in range(n):
                img = be.grab(rect)
            dt = (time.perf_counter() - t0) / n
            std = float(img[::4, ::4].std())
            blank = is_blank(img)
            print(f"      形状 {img.shape[1]}x{img.shape[0]}  平均 {dt*1000:.2f} ms/帧 "
                  f"(≈{1/dt:.0f} fps)  方差 {std:.1f}")
            print(f"      结论: {'黑屏/纯色 —— 对该窗口不可用' if blank else '有画面，可用 ✓'}")
        except CaptureError as exc:
            print(f"      不可用: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"      失败: {type(exc).__name__}: {exc}")
        finally:
            if be is not None:
                try:
                    be.close()
                except Exception:
                    pass


def test_input_mapping() -> None:
    print()
    print("=" * 74)
    print("[2] SendInput 绝对坐标换算实测（只移动鼠标，不点击）")
    print("=" * 74)

    g = __import__("ctypes").windll.user32.GetSystemMetrics
    vx, vy, vw, vh = g(76), g(77), g(78), g(79)
    print(f"  虚拟桌面: 原点 ({vx},{vy})  尺寸 {vw}x{vh}")
    print("  -> 这是关键：负原点时 SendInput 的 0..65535 归一化必须减去原点，否则整体偏移")

    human = Humanizer(speed=4.0, move_jitter_px=0.0, tremor=0.0)
    mouse = Mouse(human)

    # 用虚拟桌面四角与中心做检验
    targets = [
        (vx + 200, vy + 200),
        (vx + vw // 2, vy + vh // 2),
        (vx + vw - 200, vy + vh - 200),
        (vx + 1500, vy + 800),
    ]

    # 2a) 换算正确性：瞬移（无插值），应精确到 0~1px
    print("\n  --- 换算正确性（move_to_instant，无插值） ---")
    ok = 0
    for tx, ty in targets:
        mouse.move_to_instant(tx, ty)
        time.sleep(0.2)
        gx, gy = mouse.position()
        err = max(abs(gx - tx), abs(gy - ty))
        good = err <= 1
        ok += good
        print(f"  目标 ({tx:6d},{ty:6d})  实际 ({gx:6d},{gy:6d})  误差 {err}px  "
              f"{'✓' if good else '✗'}")
    print(f"  结果: {ok}/{len(targets)} 个点误差 ≤1px")

    # 2b) 插值落点：贝塞尔移动后必须精确停在目标（这一步曾发现真实 bug ——
    #     高频绝对移动被系统合并，最后一个插值事件丢失，光标停在中途）
    print("\n  --- 插值落点（move_to 贝塞尔轨迹） ---")
    ok2 = 0
    for tx, ty in targets:
        mouse.move_to(tx, ty, duration=0.12)
        time.sleep(0.15)
        gx, gy = mouse.position()
        err = max(abs(gx - tx), abs(gy - ty))
        good = err <= 2
        ok2 += good
        print(f"  目标 ({tx:6d},{ty:6d})  实际 ({gx:6d},{gy:6d})  误差 {err}px  "
              f"{'✓' if good else '✗'}")
    print(f"  结果: {ok2}/{len(targets)} 个点误差 ≤2px")


def main() -> int:
    enable_dpi_awareness()
    print("DPI 感知: 已开启")
    fg = __import__("ctypes").windll.user32.GetForegroundWindow()
    print(f"前台窗口句柄: 0x{fg:X}（有前台窗口 = 输入能送达）")
    print()
    test_backends()
    test_input_mapping()
    print()
    print("=" * 74)
    print("注意：本脚本只移动鼠标、不点击。运行结束时鼠标会停在测试点上。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
