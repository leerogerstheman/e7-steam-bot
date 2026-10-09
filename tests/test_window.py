"""窗口定位测试 —— 用**实测到的真实标题/进程名**做回归保护。

这些字符串不是编的，是从 `F:\\SteamLibrary\\steamapps\\common\\Epic Seven Demo`
里的 `EpicSeven_Steam.exe` 直接读出来的 UTF-16 字符串：

    窗口标题  = "EpicSeven (Steam)"
    主程序名  = "EpicSeven_Steam.exe"
    反作弊加载器 = "ucldr_Epic7_SM_loader_x64.exe"

如果哪天有人改了 `config/default.toml` 的窗口正则而把实测值漏掉，
这里的测试会立刻失败 —— 这正是它的价值。
"""

from __future__ import annotations

import pytest

from e7bot import winutil
from e7bot.config import Config
from e7bot.winutil import Rect, WindowInfo, _title_coverage, find_window

#: 实测值（来自真实客户端二进制）
REAL_TITLE = "EpicSeven (Steam)"
REAL_EXE = r"C:\SteamLibrary\steamapps\common\Epic Seven Demo\EpicSeven_Steam.exe"
LOADER_EXE = r"C:\SteamLibrary\steamapps\common\Epic Seven Demo\ucldr_Epic7_SM_loader_x64.exe"

#: 真实踩到的假阳性：浏览器标签页标题里含"第七史诗"，曾被误判成游戏窗口。
#: （在开发机上跑 `run.py doctor` 时它真的锁到了这个窗口。）
BROWSER_TITLE = "第七史诗steam端要上线了，根 — DeepSeek Harness"
BROWSER_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def _win(title: str, exe: str, w: int = 1920, h: int = 1080, hwnd: int = 1) -> WindowInfo:
    return WindowInfo(
        hwnd=hwnd, title=title, class_name="SDL_app", pid=100 + hwnd, exe=exe,
        client=Rect(0, 0, w, h), visible=True, minimized=False,
    )


# --------------------------------------------------------------------------- #
# 配置里的正则必须匹配实测值
# --------------------------------------------------------------------------- #


def test_default_config_matches_real_window_title() -> None:
    cfg = Config()
    import re

    patterns = cfg.window_titles()
    assert patterns, "title_patterns 不能为空"
    assert any(re.search(p, REAL_TITLE, re.IGNORECASE) for p in patterns), (
        f"没有任何 title_patterns 能匹配实测标题 {REAL_TITLE!r}；patterns={patterns}"
    )


def test_default_config_matches_real_exe_name() -> None:
    cfg = Config()
    import re

    patterns = cfg.window_exes()
    assert any(re.search(p, REAL_EXE, re.IGNORECASE) for p in patterns), (
        f"没有任何 exe_patterns 能匹配实测程序 {REAL_EXE!r}；patterns={patterns}"
    )


def test_default_config_excludes_anticheat_loader() -> None:
    """反作弊加载器绝不能被当成游戏窗口锁上。"""
    cfg = Config()
    import re

    excludes = cfg.window_excludes()
    assert any(re.search(p, LOADER_EXE, re.IGNORECASE) for p in excludes), (
        f"没有排除规则能挡住 {LOADER_EXE!r}；excludes={excludes}"
    )


def test_default_config_patterns_are_mutually_consistent() -> None:
    """标题模式不能和排除模式互相匹配 —— 否则永远找不到窗口。"""
    assert Config().validate() == []


def test_validate_catches_self_contradicting_patterns() -> None:
    cfg = Config()
    cfg.data["window"]["title_patterns"] = ["EpicSeven"]
    cfg.data["window"]["exclude_title_patterns"] = ["EpicSeven"]
    problems = cfg.validate()
    assert any("自相矛盾" in p for p in problems), problems


# --------------------------------------------------------------------------- #
# find_window 行为
# --------------------------------------------------------------------------- #


@pytest.fixture()
def fake_windows(monkeypatch):
    """把 list_windows 换成可控列表。"""
    state = {"windows": []}
    monkeypatch.setattr(winutil, "list_windows", lambda: list(state["windows"]))
    return state


def test_find_window_by_real_title(fake_windows) -> None:
    fake_windows["windows"] = [_win(REAL_TITLE, REAL_EXE, hwnd=7)]
    cfg = Config()
    w = find_window(
        title_patterns=cfg.window_titles(),
        exe_patterns=cfg.window_exes(),
        exclude_patterns=cfg.window_excludes(),
        min_size=cfg.window_min_size(),
    )
    assert w is not None
    assert w.hwnd == 7


def test_find_window_skips_anticheat_loader(fake_windows) -> None:
    """加载器窗口在旁边时，必须选中真正的游戏窗口。"""
    fake_windows["windows"] = [
        _win("UNCHEATER", LOADER_EXE, w=800, h=600, hwnd=1),
        _win(REAL_TITLE, REAL_EXE, hwnd=2),
    ]
    cfg = Config()
    w = find_window(
        title_patterns=cfg.window_titles(),
        exe_patterns=cfg.window_exes(),
        exclude_patterns=cfg.window_excludes(),
        min_size=cfg.window_min_size(),
    )
    assert w is not None and w.hwnd == 2, "不该锁到反作弊加载器的窗口"


def test_find_window_returns_none_when_only_loader_present(fake_windows) -> None:
    fake_windows["windows"] = [_win("UNCHEATER", LOADER_EXE, hwnd=1)]
    cfg = Config()
    assert cfg.find_game_window() is None


def test_find_window_ignores_tiny_windows(fake_windows) -> None:
    """启动瞬间窗口可能只有几十像素 —— 那时不该锁上去截图。"""
    fake_windows["windows"] = [_win(REAL_TITLE, REAL_EXE, w=64, h=48, hwnd=1)]
    assert Config().find_game_window() is None


def test_find_window_prefers_largest(fake_windows) -> None:
    fake_windows["windows"] = [
        _win(REAL_TITLE, REAL_EXE, w=1280, h=720, hwnd=1),
        _win(REAL_TITLE, REAL_EXE, w=2560, h=1440, hwnd=2),
    ]
    w = Config().find_game_window()
    assert w is not None and w.hwnd == 2


def test_find_window_matches_by_exe_when_title_unknown(fake_windows) -> None:
    """标题被改过（比如被别的工具改了）时，仍应能按进程名找到。"""
    fake_windows["windows"] = [_win("Some Other Title", REAL_EXE, hwnd=3)]
    w = Config().find_game_window()
    assert w is not None and w.hwnd == 3


def test_find_window_returns_none_on_empty(fake_windows) -> None:
    fake_windows["windows"] = []
    assert Config().find_game_window() is None


def test_find_window_picker_is_used_when_ambiguous(fake_windows) -> None:
    fake_windows["windows"] = [
        _win(REAL_TITLE, REAL_EXE, w=1280, h=720, hwnd=1),
        _win(REAL_TITLE, REAL_EXE, w=1280, h=720, hwnd=2),
    ]
    picked: list[list[WindowInfo]] = []

    def picker(cands):
        picked.append(cands)
        return cands[-1]

    w = Config().find_game_window(picker=picker)
    assert picked, "有多个候选时应该调用 picker"
    assert w is not None and w.hwnd == 2


# --------------------------------------------------------------------------- #
# 实测标题本身的正则语义
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("pattern,expected", [
    (r"EpicSeven\s*\(Steam\)", True),
    (r"EpicSeven", True),
    (r"Epic\s*Seven", True),        # \s* 可匹配零个空格
    (r"Epic Seven", False),         # 实测标题里没有空格
    (r"第七史诗", False),
])
def test_pattern_semantics_against_real_title(pattern: str, expected: bool) -> None:
    import re

    assert bool(re.search(pattern, REAL_TITLE, re.IGNORECASE)) is expected


# --------------------------------------------------------------------------- #
# 标题覆盖比例 —— 防"游戏名只是长标题里一小段"的假阳性
# --------------------------------------------------------------------------- #


def test_title_coverage_exact_title_is_full() -> None:
    import re

    pats = [re.compile(p, re.I) for p in (r"EpicSeven\s*\(Steam\)", r"EpicSeven")]
    assert _title_coverage(REAL_TITLE, pats) == 1.0


def test_title_coverage_browser_tab_is_low() -> None:
    """**这是回归测试。** 真实踩到的假阳性：浏览器标签页含"第七史诗"。"""
    import re

    pats = [re.compile(p, re.I) for p in (r"第七史诗",)]
    cov = _title_coverage(BROWSER_TITLE, pats)
    assert cov < 0.2, f"游戏名只占浏览器标题的 {cov:.0%}，应该被判为不像游戏窗口"


def test_find_window_rejects_browser_tab_with_game_name_in_title(fake_windows) -> None:
    """**核心回归测试**：只有浏览器开着时，绝不能把它当成游戏窗口。

    这个 bug 是实测跑 `doctor` 时抓到的 —— 表现是脚本锁上浏览器然后
    对着它截图点击，失败现象却是"识别不到场景"，极难定位。
    """
    fake_windows["windows"] = [_win(BROWSER_TITLE, BROWSER_EXE, hwnd=1)]
    assert Config().find_game_window() is None, "不该把浏览器当成游戏窗口"


def test_find_window_prefers_exe_match_over_browser_tab(fake_windows) -> None:
    """游戏和浏览器同时开着时，必须选游戏。"""
    fake_windows["windows"] = [
        _win(BROWSER_TITLE, BROWSER_EXE, hwnd=1),
        _win(REAL_TITLE, REAL_EXE, hwnd=2),
    ]
    w = Config().find_game_window()
    assert w is not None and w.hwnd == 2


def test_find_window_exe_match_beats_title_only_even_if_smaller(fake_windows) -> None:
    """进程名命中是**最强信号**：即使标题命中的窗口更大，也优先选进程名命中的。"""
    fake_windows["windows"] = [
        _win("第七史诗", BROWSER_EXE, w=3840, h=2160, hwnd=1),      # 标题命中且覆盖 100%
        _win("Untitled", REAL_EXE, w=1280, h=720, hwnd=2),          # 只有进程名命中
    ]
    w = Config().find_game_window()
    assert w is not None and w.hwnd == 2, "进程名命中应该优先于纯标题命中"


def test_find_window_accepts_short_title_with_suffix(fake_windows) -> None:
    """标题是「第七史诗 - 官方版」这类带后缀的：覆盖率约 0.4，默认阈值下会被拒。

    这是**刻意的安全侧取舍**：宁可找不到（有明确报错可排查），也不要锁错窗口。
    用户可调小 min_title_coverage 来放宽。
    """
    fake_windows["windows"] = [_win("第七史诗 - 官方版", BROWSER_EXE, hwnd=1)]
    assert Config().find_game_window() is None

    cfg = Config()
    cfg.data["window"]["min_title_coverage"] = 0.3
    assert cfg.find_game_window() is not None, "放宽阈值后应该能匹配"


def test_min_title_coverage_is_configurable(fake_windows) -> None:
    fake_windows["windows"] = [_win(BROWSER_TITLE, BROWSER_EXE, hwnd=1)]
    cfg = Config()
    cfg.data["window"]["min_title_coverage"] = 0.05   # 故意放宽到会误判
    assert cfg.find_game_window() is not None
