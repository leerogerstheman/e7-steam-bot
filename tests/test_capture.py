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
    bettercam_output_candidates,
    implied_scale,
    is_blank,
    parse_output_info,
)
from e7bot.winutil import Rect

#: **真实**的 bettercam.output_info() 输出（取自双显卡笔记本实测）。
#: NVIDIA RTX 3060 驱动主屏（2560x1440 @125% -> 报告 2048x1152），
#: Intel UHD 驱动另一块（2560x1440 @150% -> 报告 1707x960）。
REAL_OUTPUT_INFO = (
    "Device[0] Output[0]: Res:(2048, 1152) Rot:0 Primary:True\n"
    "Device[1] Output[0]: Res:(1707, 960) Rot:0 Primary:False\n"
)

#: 左屏（Intel 那块），原点为负
LEFT_MONITOR = Rect(-2560, 0, 2560, 1440)
#: 右屏（NVIDIA 那块，Windows 主显示器）
RIGHT_MONITOR = Rect(0, 0, 2560, 1440)


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


# --------------------------------------------------------------------------- #
# bettercam 的 DXGI 输出解析 —— 双显卡笔记本上的真实坑
# --------------------------------------------------------------------------- #


def test_parse_output_info_real_dump() -> None:
    outs = parse_output_info(REAL_OUTPUT_INFO)
    assert outs == [
        (0, 0, 2048, 1152, True),
        (1, 0, 1707, 960, False),
    ]


def test_parse_output_info_tolerates_garbage() -> None:
    assert parse_output_info("") == []
    assert parse_output_info("not a dump at all") == []
    assert parse_output_info(None) == []  # type: ignore[arg-type]


def test_parse_output_info_multiple_outputs_per_device() -> None:
    text = (
        "Device[0] Output[0]: Res:(1920, 1080) Rot:0 Primary:True\n"
        "Device[0] Output[1]: Res:(1280, 720) Rot:0 Primary:False\n"
    )
    outs = parse_output_info(text)
    assert len(outs) == 2
    assert outs[0][:2] == (0, 0)
    assert outs[1][:2] == (0, 1)


def test_implied_scale_matches_real_scaling() -> None:
    """DXGI 报告的是**逻辑**分辨率，除以缩放比才是物理分辨率。"""
    assert implied_scale(2048, 1152, 2560, 1440) == 1.25
    assert implied_scale(1707, 960, 2560, 1440) == 1.5
    assert implied_scale(2560, 1440, 2560, 1440) == 1.0
    assert implied_scale(1280, 720, 2560, 1440) == 2.0


def test_implied_scale_rejects_mismatch() -> None:
    assert implied_scale(1000, 500, 2560, 1440) is None
    assert implied_scale(0, 0, 2560, 1440) is None


def test_bettercam_candidates_pick_intel_output_for_left_monitor() -> None:
    """**核心回归测试。**

    左屏由 Intel（Device[1]）驱动，而 Device[0]（NVIDIA）**只有 1 个输出**。
    旧代码把 Windows 显示器索引当 output_idx 传，在 NVIDIA 上直接 IndexError，
    于是 bettercam 静默失效、退回慢得多的 mss。

    现在应该按"分辨率 × 缩放比"正确选到 (1, 0)。
    """
    cands = bettercam_output_candidates(LEFT_MONITOR, REAL_OUTPUT_INFO)
    assert cands[0] == (1, 0), f"应优先选 Intel 那块，实际 {cands}"


def test_bettercam_candidates_pick_nvidia_output_for_primary() -> None:
    cands = bettercam_output_candidates(RIGHT_MONITOR, REAL_OUTPUT_INFO)
    assert cands[0] == (0, 0), f"主显示器应由 NVIDIA(Device[0]) 驱动，实际 {cands}"


def test_bettercam_candidates_always_include_fallback() -> None:
    """候选列表末尾要留一个"让 bettercam 自己选"的兜底项。"""
    for mon in (LEFT_MONITOR, RIGHT_MONITOR, Rect(0, 0, 1920, 1080)):
        cands = bettercam_output_candidates(mon, REAL_OUTPUT_INFO)
        assert (0, None) in cands, f"{mon.as_tuple()} 的候选缺兜底项: {cands}"


def test_bettercam_candidates_handles_empty_info() -> None:
    assert bettercam_output_candidates(RIGHT_MONITOR, "") == [(0, None)]


def test_bettercam_candidates_prefers_resolution_match_over_primary_flag() -> None:
    """分辨率对得上比 Primary 标志一致更重要 —— 标志可能因虚拟显示器而不可靠。"""
    text = (
        "Device[0] Output[0]: Res:(999, 999) Rot:0 Primary:False\n"
        "Device[1] Output[0]: Res:(2560, 1440) Rot:0 Primary:False\n"
    )
    cands = bettercam_output_candidates(RIGHT_MONITOR, text)
    assert cands[0] == (1, 0)
