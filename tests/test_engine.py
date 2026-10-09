"""引擎测试：任务调度、安全闸、冻结检测、dry-run、统计事件。

窗口/截图/输入全部被 `StubBot` 替换掉（见 conftest.py），所以这些测试
**不需要游戏、不会移动鼠标、不会响铃**。
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from e7bot import stats as S
from e7bot.engine import Bot, SafetyViolation, StopRequested
from e7bot.winutil import Rect
from tests.conftest import FakeGrabber, FakeWindow, StubBot
from tools.selftest import render


# --------------------------------------------------------------------------- #
# 任务调度
# --------------------------------------------------------------------------- #


class FakeTask:
    def __init__(self, name: str, scenes=(), finished: bool = False):
        self.name = name
        self.scenes = tuple(scenes)
        self.finished = finished
        self.ticks = 0

    def can_handle(self, scene: str) -> bool:
        if self.finished:
            return False
        return not self.scenes or scene in self.scenes

    def tick(self, bot) -> None:
        self.ticks += 1


def test_tick_gives_control_to_first_matching_task(stub: StubBot) -> None:
    lobby_task = FakeTask("lobby_task", scenes=("lobby",))
    battle_task = FakeTask("battle_task", scenes=("battle",))

    stub.current = "battle"
    done = stub._tick([lobby_task, battle_task])

    assert done is False
    assert lobby_task.ticks == 0, "不关心当前场景的任务不该被调用"
    assert battle_task.ticks == 1


def test_tick_only_one_task_acts_per_tick(stub: StubBot) -> None:
    """一个 tick 只让一个任务动作 —— 否则两个任务会抢同一个界面。"""
    a = FakeTask("a", scenes=("lobby",))
    b = FakeTask("b", scenes=("lobby",))
    stub.current = "lobby"

    stub._tick([a, b])
    assert (a.ticks, b.ticks) == (1, 0)

    stub._tick([a, b])
    assert (a.ticks, b.ticks) == (2, 0), "排在前面的任务优先，直到它完成"


def test_tick_returns_true_when_all_finished(stub: StubBot) -> None:
    a = FakeTask("a", finished=True)
    b = FakeTask("b", finished=True)
    assert stub._tick([a, b]) is True

    c = FakeTask("c", scenes=("lobby",))
    assert stub._tick([c]) is False
    assert c.ticks == 1


def test_scene_change_is_recorded_not_every_tick(stub: StubBot) -> None:
    """只在场景**切换**时记事件 —— 每 tick 都记会把事件流刷爆。"""
    stub.current = "battle"
    stub._tick([])
    stub._tick([])              # 同一场景，不该重复记
    stub.current = "lobby"
    stub._tick([])

    events = [e for e in S.read_events(stub.recorder.path) if e["type"] == "scene"]
    assert [e["scene"] for e in events] == ["battle", "lobby"]
    assert events[1]["prev"] == "battle"


# --------------------------------------------------------------------------- #
# 安全闸
# --------------------------------------------------------------------------- #


def test_runtime_limit_stops(stub: StubBot) -> None:
    stub.cfg.data["safety"]["max_runtime_minutes"] = 1
    stub.stats.started = time.time() - 61
    with pytest.raises(SafetyViolation) as ei:
        stub.check_safety()
    assert "最大运行时长" in str(ei.value)


def test_runtime_limit_disabled_when_zero(stub: StubBot) -> None:
    stub.cfg.data["safety"]["max_runtime_minutes"] = 0
    stub.stats.started = time.time() - 99999
    stub.check_safety()          # 不该抛


def test_consecutive_error_limit_stops(stub: StubBot) -> None:
    stub.cfg.data["safety"]["max_consecutive_errors"] = 3
    stub.stats.consecutive_errors = 3
    with pytest.raises(SafetyViolation) as ei:
        stub.check_safety()
    assert "连续出错" in str(ei.value)


def test_hotkey_stop_is_not_a_safety_violation(stub: StubBot) -> None:
    """急停是用户主动行为，不该被当成"安全违规"去发告警。"""

    class H:
        stopped = True
        paused = False

    stub.hotkeys = H()  # type: ignore[assignment]
    with pytest.raises(StopRequested) as ei:
        stub.check_safety()
    assert not isinstance(ei.value, SafetyViolation)


# --------------------------------------------------------------------------- #
# 冻结（卡死）检测
# --------------------------------------------------------------------------- #


def test_frozen_detection_triggers_on_static_screen(stub: StubBot) -> None:
    stub.cfg.data["safety"]["frozen_timeout"] = 10.0
    stub._frozen_ref = None

    stub.check_frozen()                       # 第一帧只建立基准
    assert stub._frozen_ref is not None
    assert stub._frozen_ref.shape == (90, 160, 3)

    stub._frozen_ref_ts = time.time() - 11    # 假装画面静止了 11 秒
    with pytest.raises(SafetyViolation) as ei:
        stub.check_frozen()
    assert "静止" in str(ei.value)
    assert "frozen" in stub.debug_frames, "停机时应该存一张调试图"


def test_frozen_detection_resets_when_screen_changes(full_cfg, scene_frame) -> None:
    """画面一变就重置计时 —— 战斗中动画不断，绝不能误判成卡死。"""
    bot = StubBot(full_cfg, scene_frame)
    bot.cfg.data["safety"]["frozen_timeout"] = 10.0
    bot._frozen_ref = None
    bot.check_frozen()

    bot._frozen_ref_ts = time.time() - 30    # 假装静止很久
    bot.grabber.frames = [render({}, 1920, 1080)]   # 换成完全不同的画面
    bot.grabber.i = 0
    bot.invalidate_frame()

    bot.check_frozen()                       # 不该抛
    assert time.time() - bot._frozen_ref_ts < 2.0


def test_frozen_detection_disabled_when_zero(stub: StubBot) -> None:
    stub.cfg.data["safety"]["frozen_timeout"] = 0
    stub._frozen_ref = None
    stub._frozen_ref_ts = time.time() - 99999
    stub.check_frozen()
    stub.check_frozen()          # 不该抛，也不该建立基准
    assert stub._frozen_ref is None


# --------------------------------------------------------------------------- #
# dry-run
# --------------------------------------------------------------------------- #


class RecordingMouse:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def click(self, x, y, button="left", clicks=1, jitter=None, move=True) -> None:
        self.calls.append((x, y, button, clicks))


class RawClickBot(Bot):
    """不覆盖 click_norm/click_match，走真实的 dry-run 分支。"""

    def __init__(self, cfg, frame: np.ndarray, dry_run: bool):
        super().__init__(cfg, dry_run=dry_run)
        self.window = FakeWindow(frame.shape[1], frame.shape[0])
        self.grabber = FakeGrabber(frame)
        self.mouse = RecordingMouse()

    def client_rect(self) -> Rect:
        return self.window.client  # type: ignore[union-attr]

    def sleep(self, seconds: float) -> None:
        pass

    def random_sleep(self, span) -> None:
        pass


def test_dry_run_sends_no_click(full_cfg, scene_frame) -> None:
    bot = RawClickBot(full_cfg, scene_frame, dry_run=True)
    bot.click_norm(0.5, 0.5)
    assert bot.mouse.calls == []
    assert bot.stats.clicks == 0


def test_live_click_hits_normalized_position(full_cfg, scene_frame) -> None:
    bot = RawClickBot(full_cfg, scene_frame, dry_run=False)
    bot.click_norm(0.5, 0.5)
    assert len(bot.mouse.calls) == 1
    x, y, button, clicks = bot.mouse.calls[0]
    assert (x, y) == (960, 540)
    assert button == "left" and clicks == 1
    assert bot.stats.clicks == 1


def test_dry_run_click_match_sends_no_click(full_cfg, scene_frame) -> None:
    bot = RawClickBot(full_cfg, scene_frame, dry_run=True)
    m = bot.find("battle/btn_retry")
    assert m is not None
    bot.click_match(m)
    assert bot.mouse.calls == []


def test_live_click_match_uses_match_center(full_cfg, scene_frame) -> None:
    bot = RawClickBot(full_cfg, scene_frame, dry_run=False)
    m = bot.find("battle/btn_retry")
    assert m is not None
    bot.click_match(m)
    assert len(bot.mouse.calls) == 1
    assert bot.mouse.calls[0][:2] == m.center


def test_click_template_returns_false_when_optional_and_missing(full_cfg, scene_frame) -> None:
    bot = RawClickBot(full_cfg, scene_frame, dry_run=False)
    # 模板存在但当前画面里没有它 —— 用一张空画面
    bot.grabber = FakeGrabber(render({}, 1920, 1080))
    bot.invalidate_frame()
    assert bot.click_template("battle/btn_retry", timeout=0, required=False) is False


def test_click_template_raises_when_required_and_missing(full_cfg, scene_frame) -> None:
    bot = RawClickBot(full_cfg, scene_frame, dry_run=False)
    bot.grabber = FakeGrabber(render({}, 1920, 1080))
    bot.invalidate_frame()
    with pytest.raises(SafetyViolation):
        bot.click_template("battle/btn_retry", timeout=0, required=True)


# --------------------------------------------------------------------------- #
# 抓帧复用
# --------------------------------------------------------------------------- #


def test_frame_is_reused_within_same_tick(stub: StubBot) -> None:
    """同一 tick 内多次识别只抓一帧 —— 否则每个模板匹配都要重抓，慢十倍。"""
    stub.grabber.calls = 0
    stub.stats.ticks = 5
    stub.invalidate_frame()
    stub.frame()
    stub.frame()
    stub.frame()
    assert stub.grabber.calls == 1


def test_frame_is_regrabbed_on_new_tick(stub: StubBot) -> None:
    stub.grabber.calls = 0
    stub.stats.ticks = 5
    stub.invalidate_frame()
    stub.frame()
    stub.stats.ticks = 6
    stub.frame()
    assert stub.grabber.calls == 2


def test_client_rect_rejects_tiny_window(stub: StubBot) -> None:
    stub.window.client = Rect(0, 0, 10, 10)  # type: ignore[union-attr]
    with pytest.raises(SafetyViolation) as ei:
        stub.client_rect()
    assert "尺寸异常" in str(ei.value)


def test_client_rect_accepts_normal_window(stub: StubBot) -> None:
    assert stub.client_rect().as_tuple() == (0, 0, 1920, 1080)


def test_window_alive_check_can_be_overridden(stub: StubBot) -> None:
    """`_window_alive` 是刻意留的接缝：真实实现要调 win32，测试里替换掉。"""
    assert stub._window_alive() is False          # hwnd=0x1234 不是真窗口
    stub._window_alive = lambda: True             # type: ignore[method-assign]
    assert stub.client_rect().width == 1920


# --------------------------------------------------------------------------- #
# 统计事件
# --------------------------------------------------------------------------- #


def test_run_end_event_written_on_stop(stub: StubBot, project: Path) -> None:
    stub.stats.ticks = 7
    stub.stats.clicks = 3
    stub._stop_reason = "测试结束"
    stub.stop()

    events = S.read_events(stub.recorder.path)
    ends = [e for e in events if e["type"] == "run_end"]
    assert len(ends) == 1
    assert ends[0]["ticks"] == 7
    assert ends[0]["clicks"] == 3
    assert ends[0]["reason"] == "测试结束"


def test_stats_summary_is_human_readable(stub: StubBot) -> None:
    stub.stats.ticks = 100
    stub.stats.clicks = 42
    stub.stats.scenes_seen["battle"] = 30
    text = stub.stats.summary()
    assert "tick 100" in text
    assert "点击 42" in text
    assert "battle×30" in text
