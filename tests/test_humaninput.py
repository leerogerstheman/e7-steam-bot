"""输入层测试：虚拟键映射、绝对坐标换算、贝塞尔落点、SendInput 事件构造。

**不会真的移动鼠标或敲键盘** —— 通过 monkeypatch 掉 `_send`，只检查发出去的
事件结构。这样既能验证"发得对不对"，又不会干扰跑测试的人。
"""

from __future__ import annotations

import pytest

from e7bot import humaninput as H
from e7bot.humaninput import (
    INPUT_KEYBOARD,
    INPUT_MOUSE,
    KEYEVENTF_KEYUP,
    KEYEVENTF_SCANCODE,
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_VIRTUALDESK,
    MOUSEEVENTF_WHEEL,
    Humanizer,
    Keyboard,
    Mouse,
    vk_of,
)


# --------------------------------------------------------------------------- #
# 虚拟键映射
# --------------------------------------------------------------------------- #


def test_vk_of_common_keys() -> None:
    assert vk_of("f12") == 0x7B
    assert vk_of("F12") == 0x7B          # 大小写不敏感
    assert vk_of("f9") == 0x78
    assert vk_of("esc") == 0x1B
    assert vk_of("escape") == 0x1B
    assert vk_of("space") == 0x20
    assert vk_of("enter") == 0x0D
    assert vk_of("return") == 0x0D
    assert vk_of("a") == 0x41
    assert vk_of("A") == 0x41
    assert vk_of("0") == 0x30
    assert vk_of(" left ") == 0x25       # 前后空白被忽略


def test_vk_of_unknown_raises() -> None:
    with pytest.raises(KeyError):
        vk_of("not_a_real_key")
    with pytest.raises(KeyError):
        vk_of("")


# --------------------------------------------------------------------------- #
# 拟人化参数
# --------------------------------------------------------------------------- #


def test_humanizer_hold_within_range() -> None:
    h = Humanizer()
    for _ in range(200):
        v = h.hold((45, 110))
        assert 0.045 <= v <= 0.110


def test_humanizer_speed_scales_sleep() -> None:
    """speed 越大，停顿越短。"""
    import time

    fast = Humanizer(speed=20.0, pre_click_delay=(200, 200))
    slow = Humanizer(speed=1.0, pre_click_delay=(200, 200))

    t0 = time.perf_counter()
    fast.sleep_pre_click()
    fast_dt = time.perf_counter() - t0

    t0 = time.perf_counter()
    slow.sleep_pre_click()
    slow_dt = time.perf_counter() - t0

    assert fast_dt < slow_dt


# --------------------------------------------------------------------------- #
# 绝对坐标换算
# --------------------------------------------------------------------------- #


@pytest.fixture()
def virtual_desktop(monkeypatch):
    """模拟一个带负原点的双屏桌面（正是本机实测过的配置）。"""
    monkeypatch.setattr(Mouse, "_virtual_desktop",
                        staticmethod(lambda: (-2560, 0, 5120, 1440)))
    return (-2560, 0, 5120, 1440)


def test_to_absolute_corners(virtual_desktop) -> None:
    m = Mouse()
    # 左上角 -> 0，右下角 -> 65535
    assert m._to_absolute(-2560, 0) == (0, 0)
    assert m._to_absolute(2559, 1439) == (65535, 65535)


def test_to_absolute_center(virtual_desktop) -> None:
    """中心点应该是 32774/32790（而不是 32768）—— 因为分母是 vw-1 不是 vw。

    这个细节在实测里很关键：用 65535/vw 会让每个点都差 1px，
    用 65535/(vw-1) 才是 0px 误差。
    """
    m = Mouse()
    ax, ay = m._to_absolute(0, 720)
    assert abs(ax - 32774) <= 1
    assert abs(ay - 32790) <= 1


def test_to_absolute_clamps_out_of_range(virtual_desktop) -> None:
    m = Mouse()
    assert m._to_absolute(-99999, -99999) == (0, 0)
    assert m._to_absolute(99999, 99999) == (65535, 65535)


def test_to_absolute_is_monotonic(virtual_desktop) -> None:
    m = Mouse()
    prev = -1
    for x in range(-2560, 2560, 137):
        ax, _ = m._to_absolute(x, 0)
        assert ax >= prev
        prev = ax


# --------------------------------------------------------------------------- #
# 鼠标事件
# --------------------------------------------------------------------------- #


@pytest.fixture()
def captured(monkeypatch):
    """把 _send 换掉，收集所有发出的 INPUT 结构。"""
    sent: list = []
    monkeypatch.setattr(H, "_send", lambda *inputs: (sent.extend(inputs), len(inputs))[1])
    monkeypatch.setattr(Mouse, "_virtual_desktop",
                        staticmethod(lambda: (0, 0, 1920, 1080)))
    monkeypatch.setattr(Mouse, "position", lambda self: (0, 0))
    return sent


def test_move_to_lands_exactly_on_target(captured) -> None:
    """**这是回归测试。**

    实测发现：Windows 会合并高频绝对移动事件，贝塞尔插值循环发出的最后一个事件
    可能被丢掉，导致光标停在中途（实测偏差最大到 49px，且随距离增大）。
    修法是插值结束后补一次精确落点。这条测试锁住那个修复。
    """
    m = Mouse(Humanizer(speed=20.0, move_jitter_px=0.0, tremor=0.0))
    m.move_to(1500, 900, duration=0.02)

    assert len(captured) > 5, "应该有多段插值"

    last = captured[-1]
    assert last.type == INPUT_MOUSE
    assert last.u.mi.dwFlags & MOUSEEVENTF_MOVE
    assert last.u.mi.dwFlags & MOUSEEVENTF_ABSOLUTE
    assert last.u.mi.dwFlags & MOUSEEVENTF_VIRTUALDESK

    ex, ey = m._to_absolute(1500, 900)
    assert last.u.mi.dx == ex
    assert last.u.mi.dy == ey


def test_move_to_zero_distance_is_noop(captured) -> None:
    m = Mouse(Humanizer(speed=20.0))
    m.move_to(0, 0)          # 起点就是 (0,0)
    assert captured == []


def test_move_to_instant_sends_single_event(captured) -> None:
    m = Mouse()
    m.move_to_instant(960, 540)
    assert len(captured) == 1
    ex, ey = m._to_absolute(960, 540)
    assert (captured[0].u.mi.dx, captured[0].u.mi.dy) == (ex, ey)


def test_click_sends_down_then_up(captured) -> None:
    m = Mouse(Humanizer(speed=50.0))
    m.click(500, 400, jitter=0)
    flags = [i.u.mi.dwFlags for i in captured]
    assert MOUSEEVENTF_LEFTDOWN in flags
    assert MOUSEEVENTF_LEFTUP in flags
    assert flags.index(MOUSEEVENTF_LEFTDOWN) < flags.index(MOUSEEVENTF_LEFTUP)


def test_right_and_middle_click(captured) -> None:
    from e7bot.humaninput import MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_RIGHTDOWN

    m = Mouse(Humanizer(speed=50.0))
    m.click(100, 100, button="right", jitter=0)
    m.click(100, 100, button="middle", jitter=0)
    flags = [i.u.mi.dwFlags for i in captured]
    assert MOUSEEVENTF_RIGHTDOWN in flags
    assert MOUSEEVENTF_MIDDLEDOWN in flags


def test_click_jitter_stays_within_reason(captured) -> None:
    """抖动是随机的，但落点必须仍在目标附近（换算回像素验证）。

    两个坑：
    * 最后一个事件是 LEFTUP（dx=dy=0），要取最后一个 **MOVE** 事件才是落点；
    * dx 是 DWORD 无符号，比较前得换算回像素空间。
    """
    m = Mouse(Humanizer(speed=50.0, move_jitter_px=3.0))
    for _ in range(30):
        captured.clear()
        m.click(960, 540)
        moves = [i for i in captured if i.u.mi.dwFlags & MOUSEEVENTF_MOVE]
        assert moves, "点击前必须先移动鼠标"
        last = moves[-1]
        px = last.u.mi.dx * 1919 / 65535
        py = last.u.mi.dy * 1079 / 65535
        # 3σ 抖动 ≈ 9px，留到 20px 足够宽松又不至于漏掉"完全没抖动"的 bug
        assert abs(px - 960) <= 20
        assert abs(py - 540) <= 20


def test_click_with_zero_jitter_lands_exactly(captured) -> None:
    """jitter=0 时落点必须精确等于目标（这条能抓到坐标换算的取整错误）。"""
    m = Mouse(Humanizer(speed=50.0, move_jitter_px=0.0, tremor=0.0))
    m.click(1234, 567, jitter=0.0)
    moves = [i for i in captured if i.u.mi.dwFlags & MOUSEEVENTF_MOVE]
    ex, ey = m._to_absolute(1234, 567)
    assert (moves[-1].u.mi.dx, moves[-1].u.mi.dy) == (ex, ey)


def _signed(v: int) -> int:
    """DWORD -> 有符号整数（滚轮向下是 -120，存进去会变成 4294967176）。"""
    return v - 2 ** 32 if v >= 2 ** 31 else v


def test_scroll_direction(captured) -> None:
    m = Mouse(Humanizer(speed=50.0))
    m.scroll(2)
    ups = [_signed(i.u.mi.mouseData) for i in captured if i.u.mi.dwFlags & MOUSEEVENTF_WHEEL]
    assert ups and all(v == 120 for v in ups)

    captured.clear()
    m.scroll(-2)
    downs = [_signed(i.u.mi.mouseData) for i in captured if i.u.mi.dwFlags & MOUSEEVENTF_WHEEL]
    assert downs and all(v == -120 for v in downs)


def test_drag_presses_and_releases(captured) -> None:
    m = Mouse(Humanizer(speed=50.0, move_jitter_px=0.0, tremor=0.0))
    m.drag(200, 200, 800, 600, hold=0.001)
    flags = [i.u.mi.dwFlags for i in captured]
    assert MOUSEEVENTF_LEFTDOWN in flags
    assert MOUSEEVENTF_LEFTUP in flags
    assert flags.index(MOUSEEVENTF_LEFTDOWN) < flags.index(MOUSEEVENTF_LEFTUP)


def test_stop_check_aborts_movement(captured) -> None:
    """急停必须能打断移动 —— 检查粒度要到插值的每一段。"""
    m = Mouse(Humanizer(speed=1.0), stop_check=lambda: True)
    m.move_to(1500, 900)
    assert captured == []


# --------------------------------------------------------------------------- #
# 键盘事件
# --------------------------------------------------------------------------- #


def test_keyboard_tap_uses_scan_codes(captured) -> None:
    kb = Keyboard(Humanizer())
    kb.tap("space", hold=0.001)
    assert len(captured) == 2

    down, up = captured
    assert down.type == INPUT_KEYBOARD
    assert up.type == INPUT_KEYBOARD
    # 关键：必须用扫描码（KEYEVENTF_SCANCODE），虚拟键码对 DirectInput 游戏无效
    assert down.u.ki.dwFlags & KEYEVENTF_SCANCODE
    assert not (down.u.ki.dwFlags & KEYEVENTF_KEYUP)
    assert up.u.ki.dwFlags & KEYEVENTF_KEYUP
    assert down.u.ki.wScan != 0


def test_keyboard_extended_keys_flag(captured) -> None:
    """方向键/小键盘属于扩展键，必须带 KEYEVENTF_EXTENDEDKEY，否则游戏认不出。"""
    from e7bot.humaninput import KEYEVENTF_EXTENDEDKEY

    kb = Keyboard(Humanizer())
    kb.tap("up", hold=0.001)
    assert captured[0].u.ki.dwFlags & KEYEVENTF_EXTENDEDKEY

    captured.clear()
    kb.tap("a", hold=0.001)
    assert not (captured[0].u.ki.dwFlags & KEYEVENTF_EXTENDEDKEY)


def test_keyboard_hotkey_order(captured) -> None:
    kb = Keyboard(Humanizer())
    kb.hotkey("ctrl", "shift", "a")
    # 按下 ctrl, shift, a 再松开 a, shift, ctrl = 6 个事件
    assert len(captured) == 6
    ups = [i.u.ki.dwFlags & KEYEVENTF_KEYUP for i in captured]
    assert ups == [0, 0, 0, KEYEVENTF_KEYUP, KEYEVENTF_KEYUP, KEYEVENTF_KEYUP]


def test_keyboard_respects_stop(captured) -> None:
    kb = Keyboard(Humanizer(), stop_check=lambda: True)
    kb.tap("space")
    assert captured == []


def test_type_text_length(captured) -> None:
    kb = Keyboard(Humanizer())
    kb.type_text("abc", interval=(1, 1))
    assert len(captured) == 6      # 3 个字符 × 按下+松开
