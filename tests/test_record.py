"""录制工具测试：裁剪收缩算法、配置生成、以及"录出来的模板真的能用"的端到端验证。

录制工具的价值全在**自动裁出的模板质量**上。所以这里不只测函数签名，
而是走一遍完整链路：合成画面 → 模拟点击 → 自动裁剪 → 落盘 →
重新加载模板库 → 用匹配器把它找回来。最后一步才是真正有说服力的。
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from e7bot.vision import Matcher, MatchOptions, TemplateLibrary
from tools.record import (
    CANDIDATE_SIZES,
    FrameRing,
    RecordSession,
    Step,
    centered_box,
    count_unique_matches,
    generate_toml,
    refine_crop,
    save_session,
)
from e7bot.winutil import Rect
from tests.conftest import FakeGrabber
from tools.selftest import LOBBY_LAYOUT, crop_norm, render


# --------------------------------------------------------------------------- #
# 帧环形缓冲（"取点击前那一帧"）
# --------------------------------------------------------------------------- #


def _solid(value: int) -> np.ndarray:
    return np.full((20, 20, 3), value, np.uint8)


def _ring_with(frames: list[tuple[float, np.ndarray]]) -> FrameRing:
    """直接往缓冲里塞 (时间戳, 帧)，绕开线程，让时间可控。"""
    ring = FrameRing(FakeGrabber(_solid(0)), lambda: Rect(0, 0, 100, 100))
    with ring._lock:
        for ts, img in frames:
            ring._buf.append((ts, img, Rect(0, 0, 100, 100)))
    return ring


def test_frame_before_picks_newest_at_or_before_ts() -> None:
    """点击前的那一帧 = 时间戳 <= 按下时刻的**最近**一帧。

    这是整个录制工具的核心：取早了会拿到上一个界面，取晚了会拿到已经开始切换的界面。
    """
    now = time.time()
    ring = _ring_with([
        (now - 0.30, _solid(10)),
        (now - 0.20, _solid(20)),
        (now - 0.05, _solid(30)),
    ])
    img, _rect = ring.frame_before(now - 0.10)
    assert int(img[0, 0, 0]) == 20, "应该取到 ts<=点击时刻 里最新的那一帧"


def test_frame_before_falls_back_to_oldest() -> None:
    """点击时刻早于所有缓存帧（缓冲还没填满就点了）-> 退而取最旧的一帧。"""
    now = time.time()
    ring = _ring_with([(now + 1.0, _solid(77)), (now + 2.0, _solid(88))])
    img, _ = ring.frame_before(now)
    assert int(img[0, 0, 0]) == 77


def test_frame_before_on_empty_buffer() -> None:
    ring = FrameRing(FakeGrabber(_solid(0)), lambda: Rect(0, 0, 100, 100))
    assert ring.frame_before(time.time()) is None


def test_frame_before_returns_a_copy() -> None:
    """必须返回拷贝 —— 否则调用方裁剪/绘制会污染缓冲里的原始帧。"""
    now = time.time()
    ring = _ring_with([(now - 0.1, _solid(50))])
    img, _ = ring.frame_before(now)
    img[:] = 0
    img2, _ = ring.frame_before(now)
    assert int(img2[0, 0, 0]) == 50


def test_ring_thread_fills_buffer_and_stops() -> None:
    frame = render(LOBBY_LAYOUT, 640, 360)
    ring = FrameRing(FakeGrabber(frame), lambda: Rect(0, 0, 640, 360), fps=60, size=4)
    ring.start()
    time.sleep(0.35)
    ring.stop()

    with ring._lock:
        n = len(ring._buf)
    assert n > 0, "后台线程应该抓到了帧"
    assert n <= 4, "缓冲上限必须生效（否则内存会被吃光）"
    assert ring.errors == 0


def test_ring_survives_grab_errors() -> None:
    """抓帧失败不能让后台线程死掉 —— 否则录制会静默停止。"""

    class FlakyGrabber(FakeGrabber):
        def grab(self, rect):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("抓帧失败")
            return super().grab(rect)

    g = FlakyGrabber(_solid(5))
    ring = FrameRing(g, lambda: Rect(0, 0, 100, 100), fps=60)
    ring.start()
    time.sleep(0.3)
    ring.stop()

    assert ring.errors >= 1, "失败应该被计数"
    with ring._lock:
        assert len(ring._buf) > 0, "失败之后仍然要继续抓"


def test_ring_stop_is_idempotent() -> None:
    ring = FrameRing(FakeGrabber(_solid(1)), lambda: Rect(0, 0, 100, 100), fps=30)
    ring.start()
    ring.stop()
    ring.stop()          # 不该抛


# --------------------------------------------------------------------------- #
# 唯一性判定
# --------------------------------------------------------------------------- #


def test_count_unique_matches_single() -> None:
    frame = render(LOBBY_LAYOUT, 1920, 1080)
    crop = crop_norm(frame, LOBBY_LAYOUT["lobby/btn_hero"])
    n, score = count_unique_matches(frame, crop, 0.86)
    assert n == 1
    assert score > 0.95


def test_count_unique_matches_repeated() -> None:
    """同一张图铺 4 份 -> 应该数出 4 个（不能只报 1 个，否则会点错位置）。"""
    tile = render({"t": (0, 0, 1, 1)}, 200, 120)
    tpl = crop_norm(tile, (0.0, 0.0, 0.3, 0.4))

    frame = np.full((600, 900, 3), 26, np.uint8)
    for gy in range(2):
        for gx in range(2):
            y, x = 50 + gy * 200, 50 + gx * 300
            frame[y:y + tpl.shape[0], x:x + tpl.shape[1]] = tpl

    n, _ = count_unique_matches(frame, tpl, 0.86)
    assert n == 4


def test_count_unique_matches_absent() -> None:
    frame = render(LOBBY_LAYOUT, 1920, 1080)
    noise = np.random.default_rng(3).integers(0, 255, (40, 40, 3), dtype=np.uint8)
    n, score = count_unique_matches(frame, noise, 0.86)
    assert n == 0
    assert score < 0.86


def test_count_unique_matches_template_larger_than_frame() -> None:
    frame = np.zeros((50, 50, 3), np.uint8)
    big = np.zeros((100, 100, 3), np.uint8)
    assert count_unique_matches(frame, big, 0.8) == (0, 0.0)


# --------------------------------------------------------------------------- #
# 裁剪框定位
# --------------------------------------------------------------------------- #


def test_centered_box_centres_on_click() -> None:
    box = centered_box((500, 400), (100, 60), Rect(0, 0, 1920, 1080), (1920, 1080))
    assert box == (450, 370, 100, 60)


def test_centered_box_clamps_at_edges() -> None:
    # 点在左上角附近 -> 框被推到 (0,0)
    assert centered_box((10, 10), (100, 60), Rect(0, 0, 1920, 1080), (1920, 1080)) == (0, 0, 100, 60)
    # 点在右下角附近 -> 框被推回边界内
    assert centered_box((1910, 1070), (100, 60), Rect(0, 0, 1920, 1080), (1920, 1080)) == (1820, 1020, 100, 60)


def test_centered_box_rejects_oversized() -> None:
    assert centered_box((10, 10), (2000, 60), Rect(0, 0, 1920, 1080), (1920, 1080)) is None


# --------------------------------------------------------------------------- #
# 裁剪收缩
# --------------------------------------------------------------------------- #


def test_refine_crop_finds_unique_box_on_button() -> None:
    frame = render(LOBBY_LAYOUT, 1920, 1080)
    # 点在大厅「英雄」按钮中心
    click = (int(0.20 * 1920 + 0.11 * 1920 / 2), int(0.86 * 1080 + 0.10 * 1080 / 2))
    result = refine_crop(frame, click, scale=1.0, threshold=0.86)

    assert result is not None
    box, crop, score, unique = result
    x, y, w, h = box

    assert unique is True
    assert score >= 0.86
    assert crop.shape[:2] == (h, w)
    # 点击点必须落在框内 —— 否则脚本运行时点不到该按钮
    assert x <= click[0] < x + w
    assert y <= click[1] < y + h
    # 框应该足够大，能容纳整个按钮
    assert w >= 112 and h >= 40


def test_refine_crop_prefers_largest_unique_box() -> None:
    """从大到小扫，第一个唯一的即最优 —— 框越大视觉特征越多，越抗局部动画。"""
    frame = render(LOBBY_LAYOUT, 1920, 1080)
    click = (int(0.20 * 1920 + 0.11 * 1920 / 2), int(0.86 * 1080 + 0.10 * 1080 / 2))
    result = refine_crop(frame, click, scale=1.0, threshold=0.86)
    assert result is not None
    assert result[0][2] == CANDIDATE_SIZES[0][0], "第一个候选就该是唯一的"


def test_refine_crop_returns_none_on_flat_background() -> None:
    """点在纯色背景上 -> 没有可唯一识别的区域，必须如实返回 None。

    这里绝不能"随便裁一块" —— 纯色块在整屏到处都是，会导致脚本点错位置。
    """
    flat = np.full((1080, 1920, 3), 30, np.uint8)
    assert refine_crop(flat, (960, 540), scale=1.0, threshold=0.86) is None


def test_refine_crop_scales_with_resolution() -> None:
    """在 1280x720 上录制时，候选框要按比例缩小，否则框会盖住好几个按钮。"""
    frame = render(LOBBY_LAYOUT, 1280, 720)
    click = (int(0.20 * 1280 + 0.11 * 1280 / 2), int(0.86 * 720 + 0.10 * 720 / 2))
    result = refine_crop(frame, click, scale=1280 / 1920.0, threshold=0.86)
    assert result is not None
    box = result[0]
    # 最大候选 224x96 按 0.667 缩放 -> 约 149x64
    assert box[2] < 200 and box[3] < 90


# --------------------------------------------------------------------------- #
# 配置生成
# --------------------------------------------------------------------------- #


def _fake_step(i: int) -> Step:
    return Step(
        index=i,
        norm_xy=(0.1 * i, 0.2 * i),
        screen_xy=(100 * i, 200 * i),
        box=(10, 20, 100, 50),
        crop=np.zeros((50, 100, 3), np.uint8),
        self_score=0.95,
        unique=True,
        frame_size=(1920, 1080),
        name=f"seq/step_{i:02d}",
    )


def test_generate_toml_chains_wait_for() -> None:
    session = RecordSession(steps=[_fake_step(i) for i in (1, 2, 3)], frame_size=(1920, 1080))
    text = generate_toml(session)

    assert text.count("[[tasks.repeat_stage.enter_sequence]]") == 3
    assert 'click = "seq/step_01"' in text
    # 第 1 步等第 2 步、第 2 步等第 3 步
    assert 'wait_for = "seq/step_02"' in text
    assert 'wait_for = "seq/step_03"' in text
    # 最后一步没有后续可等，应该是注释掉的提示
    assert '# wait_for = "battle/btn_start_battle"' in text
    assert text.count("timeout = 20") == 3


def test_generate_toml_is_valid_toml() -> None:
    """生成的配置必须能被 tomllib 解析 —— 否则用户粘贴进去会直接报错。"""
    import tomllib

    session = RecordSession(steps=[_fake_step(i) for i in (1, 2)], frame_size=(1920, 1080))
    data = tomllib.loads(generate_toml(session))
    seq = data["tasks"]["repeat_stage"]["enter_sequence"]
    assert len(seq) == 2
    assert seq[0]["click"] == "seq/step_01"
    assert seq[0]["wait_for"] == "seq/step_02"
    assert seq[0]["timeout"] == 20


def test_generate_toml_custom_task_name() -> None:
    import tomllib

    session = RecordSession(steps=[_fake_step(1)], frame_size=(1920, 1080))
    data = tomllib.loads(generate_toml(session, task="arena"))
    assert "arena" in data["tasks"]


def test_generate_toml_records_click_positions_as_comments() -> None:
    session = RecordSession(steps=[_fake_step(1)], frame_size=(1920, 1080))
    text = generate_toml(session)
    assert "0.1000, 0.2000" in text          # 点击位置留在注释里，方便人工核对
    assert "1920x1080" in text


# --------------------------------------------------------------------------- #
# 端到端：录出来的模板必须真的能被匹配到
# --------------------------------------------------------------------------- #


def test_saved_session_templates_are_matchable(cfg) -> None:
    """**这是录制工具最有说服力的一条测试。**

    合成画面 -> 模拟点击 -> 自动裁剪 -> 落盘 -> 重新加载模板库 -> 匹配器找回它。
    只要这条通过，"录一遍就能用"这个承诺才站得住。
    """
    frame = render(LOBBY_LAYOUT, 1920, 1080)

    steps = []
    for i, name in enumerate(["lobby/btn_hero", "lobby/btn_adventure"], start=1):
        box_n = LOBBY_LAYOUT[name]
        click = (
            int((box_n[0] + box_n[2] / 2) * 1920),
            int((box_n[1] + box_n[3] / 2) * 1080),
        )
        refined = refine_crop(frame, click, scale=1.0, threshold=0.86)
        assert refined is not None, f"{name} 应该能裁出唯一区域"
        box, crop, score, unique = refined
        steps.append(Step(
            index=i, norm_xy=(click[0] / 1920, click[1] / 1080), screen_xy=click,
            box=box, crop=crop, self_score=score, unique=unique,
            frame_size=(1920, 1080), name=f"seq/step_{i:02d}",
        ))

    session = RecordSession(steps=steps, frame_size=(1920, 1080))
    out_cfg = save_session(session, cfg, Path(cfg.log_dir()) / "recorded.toml")

    # 模板和配置都落盘了
    assert out_cfg.exists()
    for s in steps:
        assert (cfg.template_root() / "default" / f"{s.name}.png").exists()
        assert (cfg.template_root() / "default" / f"{s.name}.json").exists()

    # 重新加载模板库（模拟"下次运行脚本"），匹配器应该能找回它们
    lib = TemplateLibrary(cfg.template_root(), "default")
    assert lib.missing(["seq/step_01", "seq/step_02"]) == []

    matcher = Matcher(lib)
    for s in steps:
        m = matcher.find(s.name, frame, Rect(0, 0, 1920, 1080),
                         opts=MatchOptions(threshold=0.80))
        assert m is not None, f"录制的模板 {s.name} 匹配不上"
        assert m.score > 0.9
        # 匹配到的位置必须就是当初点击的位置（归一化误差 <1%）
        assert abs(m.norm_center[0] - s.norm_xy[0]) < 0.01
        assert abs(m.norm_center[1] - s.norm_xy[1]) < 0.01


def test_saved_session_generates_usable_config(cfg) -> None:
    import tomllib

    frame = render(LOBBY_LAYOUT, 1920, 1080)
    click = (int(0.255 * 1920), int(0.91 * 1080))
    refined = refine_crop(frame, click, scale=1.0, threshold=0.86)
    assert refined is not None
    box, crop, score, unique = refined

    session = RecordSession(
        steps=[Step(1, (click[0] / 1920, click[1] / 1080), click, box, crop,
                    score, unique, (1920, 1080), "seq/step_01")],
        frame_size=(1920, 1080),
    )
    out = save_session(session, cfg, Path(cfg.log_dir()) / "seq.toml")

    data = tomllib.loads(out.read_text(encoding="utf-8"))
    seq = data["tasks"]["repeat_stage"]["enter_sequence"]
    assert len(seq) == 1
    assert seq[0]["click"] == "seq/step_01"

    # 落盘的 sidecar 里应该有自动推导的搜索区域（性能关键）
    import json

    sidecar = json.loads(
        (cfg.template_root() / "default" / "seq" / "step_01.json").read_text(encoding="utf-8")
    )
    assert sidecar["ref_size"] == [1920, 1080]
    region = sidecar["region"]
    assert len(region) == 4
    assert 0.0 <= region[0] <= 1.0 and 0.0 <= region[1] <= 1.0
    assert 0.0 < region[2] <= 1.0 and 0.0 < region[3] <= 1.0


def test_empty_session_generates_header_only(cfg) -> None:
    session = RecordSession(steps=[], frame_size=(1920, 1080))
    text = generate_toml(session)
    assert "共 0 步" in text
    assert "enter_sequence" not in text


@pytest.mark.parametrize("size", [(1920, 1080), (1280, 720), (2560, 1440)])
def test_refine_crop_works_across_resolutions(size) -> None:
    w, h = size
    frame = render(LOBBY_LAYOUT, w, h)
    click = (int(0.20 * w + 0.11 * w / 2), int(0.86 * h + 0.10 * h / 2))
    result = refine_crop(frame, click, scale=w / 1920.0, threshold=0.86)
    assert result is not None, f"{w}x{h} 上裁剪失败"
    assert result[3] is True
