"""截图层测试：黑屏判定、后端选择与自动降级。

**不依赖真实显示器/游戏** —— 用假后端驱动 `ScreenGrabber` 的调度逻辑。
真实的抓屏能力由 `python run.py smoke` 在真机上验证。
"""

from __future__ import annotations

import numpy as np
import pytest

from e7bot.capture import (
    CaptureError,
    ScreenGrabber,
    is_blank,
)
from e7bot.winutil import Rect


# --------------------------------------------------------------------------- #
# 黑屏判定
# --------------------------------------------------------------------------- #


def test_is_blank_uniform_is_blank() -> None:
    assert is_blank(np.zeros((100, 100, 3), np.uint8)) is True
    assert is_blank(np.full((100, 100, 3), 128, np.uint8)) is True


def test_is_blank_noise_is_not_blank() -> None:
    rng = np.random.default_rng(0)
    noisy = rng.integers(0, 255, (100, 100, 3), dtype=np.uint8)
    assert is_blank(noisy) is False


def test_is_blank_empty_is_blank() -> None:
    assert is_blank(np.zeros((0, 0, 3), np.uint8)) is True


def test_is_blank_respects_threshold() -> None:
    """低对比度画面（比如纯深色加载页）应该被判为"接近黑屏"。

    注意坑：`is_blank` 内部是 `image[::8, ::8]` 降采样后算标准差的。
    如果测试图案也正好落在 8 的倍数上，降采样会**恰好只采到变化的那些像素**，
    标准差就失真了（第一版测试就踩了这个，图案 `[::8,::8]=40` 被采成纯色）。
    所以这里用一个不对齐 8 网格的方块。
    """
    low = np.full((64, 64, 3), 20, np.uint8)
    low[10:30, 10:30] = 40                   # 不对齐 8 网格的方块
    assert is_blank(low, std_threshold=6.0) is True
    assert is_blank(low, std_threshold=1.0) is False


# --------------------------------------------------------------------------- #
# 假后端
# --------------------------------------------------------------------------- #


class FakeBackend:
    def __init__(self, name: str, img=None, exc=None, occluded: bool = False):
        self.name = name
        self._img = img
        self._exc = exc
        self.supports_occluded = occluded
        self.grabs = 0
        self.closed = False

    def grab(self, rect: Rect) -> np.ndarray:
        self.grabs += 1
        if self._exc is not None:
            raise self._exc
        return self._img

    def close(self) -> None:
        self.closed = True


GOOD = np.random.default_rng(1).integers(0, 255, (100, 100, 3), dtype=np.uint8)
BLACK = np.zeros((100, 100, 3), np.uint8)


class Harness(ScreenGrabber):
    """把后端工厂换成可控的假后端。

    刻意不叫 Test* —— pytest 会把 Test 开头的类当测试类去收集，而它有 __init__。
    """

    def __init__(self, order, factory):
        self._factory = factory
        super().__init__(hwnd=0, preferred=order, verbose=False)

    def _make(self, name):
        return self._factory(name)


def test_picks_first_working_backend() -> None:
    made: list[str] = []

    def factory(name):
        made.append(name)
        return FakeBackend(name, GOOD)

    g = Harness(["a", "b", "c"], factory)
    assert g.backend_name == "a"
    assert made == ["a"]          # 第一个就成了，不该碰后面的


def test_degrades_when_initial_grab_raises() -> None:
    """后端能初始化但抓帧就失败（驱动重置/独占全屏切换）-> 应换下一个后端。

    后端构造函数没法预知自己抓不抓得到画面，所以这种失败只能在第一次
    grab 时暴露。如果那时不换后端，脚本会直接死在启动阶段。
    """
    made: list[str] = []

    def factory(name):
        made.append(name)
        if name == "a":
            return FakeBackend(name, exc=RuntimeError("DXGI 设备丢失"))
        return FakeBackend(name, GOOD)

    g = Harness(["a", "b"], factory)
    assert g.backend_name == "a"          # 初始化阶段还看不出来
    img = g.grab(Rect(0, 0, 100, 100))
    assert not is_blank(img)
    assert g.backend_name == "b"          # 抓帧失败后换掉了
    assert made == ["a", "b"]


def test_degrades_when_backend_returns_black() -> None:
    """后端能出图但全是黑的（DX 独占全屏下的 GDI 就是这样）→ 自动降级。"""
    made: list[str] = []

    def factory(name):
        made.append(name)
        return FakeBackend(name, BLACK if name == "a" else GOOD)

    g = Harness(["a", "b"], factory)
    assert g.backend_name == "a"

    img = g.grab(Rect(0, 0, 100, 100))
    assert not is_blank(img), "降级后应该拿到真实画面"
    assert g.backend_name == "b"
    assert made == ["a", "b"]


def test_printwindow_black_does_not_trigger_degrade() -> None:
    """printwindow 抓 DX 窗口返回黑图是**预期行为**，不该因此再降级（会死循环）。"""
    made: list[str] = []

    def factory(name):
        made.append(name)
        return FakeBackend(name, BLACK)

    g = Harness(["printwindow"], factory)
    img = g.grab(Rect(0, 0, 100, 100))
    assert is_blank(img)          # 就是黑的，如实返回
    assert made == ["printwindow"]


def test_all_backends_fail_to_initialize_raises() -> None:
    def factory(name):
        raise RuntimeError(f"{name} 初始化失败")

    with pytest.raises(CaptureError) as ei:
        Harness(["a", "b"], factory)
    msg = str(ei.value)
    assert "a" in msg and "b" in msg


def test_grab_raises_when_no_backend_left() -> None:
    class AlwaysFail(FakeBackend):
        def grab(self, rect):
            self.grabs += 1
            raise RuntimeError("抓帧失败")

    g = Harness(["a"], lambda name: AlwaysFail(name))
    with pytest.raises(CaptureError):
        g.grab(Rect(0, 0, 100, 100), retries=2)


def test_close_releases_backend() -> None:
    be = FakeBackend("a", GOOD)
    g = Harness(["a"], lambda name: be)
    assert g.backend_name == "a"
    g.close()
    assert be.closed is True
    assert g.backend_name == "none"


def test_supports_occluded_flag() -> None:
    g = Harness(["pw"], lambda name: FakeBackend(name, GOOD, occluded=True))
    assert g.supports_occluded is True

    g2 = Harness(["bc"], lambda name: FakeBackend(name, GOOD, occluded=False))
    assert g2.supports_occluded is False


def test_empty_frame_treated_as_failure() -> None:
    """返回空数组也应算失败并重试，而不是把空图交给匹配器。"""
    class Empty(FakeBackend):
        def grab(self, rect):
            self.grabs += 1
            return np.zeros((0, 0, 3), np.uint8)

    g = Harness(["a"], lambda name: Empty(name))
    with pytest.raises(CaptureError):
        g.grab(Rect(0, 0, 100, 100), retries=2)
