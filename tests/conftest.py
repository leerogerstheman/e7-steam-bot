"""pytest 共享夹具。

核心思路：**Bot 的窗口/截图/输入都可以在测试里替换掉**，因为它们的接口很窄
（`client_rect()` / `frame()` / `click_match()` / `press()`）。所以不需要
mock 框架，也不需要真的开游戏 —— 继承 Bot 覆盖几个方法就够了。

这样任务逻辑（状态流转、停止条件、弹窗处理）就能被真实地测到，
而不是只能靠人肉推演。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from e7bot.config import Config  # noqa: E402
from e7bot.engine import Bot  # noqa: E402
from e7bot.scene import SceneResult  # noqa: E402
from e7bot.winutil import Rect  # noqa: E402
from tools.selftest import LOBBY_LAYOUT, BATTLE_LAYOUT, crop_norm, render  # noqa: E402

#: 测试用的最小配置。刻意关掉告警（否则测试时会真的响铃），
#: 并把日志/模板都放在临时目录里，互不干扰。
TEST_CONFIG = """
[window]
title_patterns = ["Fake"]

[capture]
backends = ["fake"]

[templates]
root = "templates"
profile = "default"
default_threshold = 0.80
scale_tolerance = 0.06

[logging]
level = "WARNING"
dir = "logs"
save_debug_frames = false

[safety]
frozen_timeout = 0

[alerts]
enabled = false

[stats]
enabled = true

[tasks]
active = []

[scenes.lobby]
any = ["lobby/btn_adventure", "lobby/btn_hero"]
none = []

[scenes.battle]
any = ["battle/btn_auto_off", "battle/btn_speed_x1"]
none = ["battle/btn_retry"]

[scenes.battle_result]
any = ["battle/btn_retry"]
none = []

[scenes.popup]
any = ["common/btn_ok"]
none = []
"""


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """临时项目目录：config/default.toml + templates/default/ + logs/"""
    (tmp_path / "config").mkdir()
    (tmp_path / "templates" / "default").mkdir(parents=True)
    (tmp_path / "config" / "default.toml").write_text(TEST_CONFIG, encoding="utf-8")
    return tmp_path


@pytest.fixture()
def cfg(project: Path) -> Config:
    return Config.load(project / "config" / "default.toml")


def install_templates(cfg: Config, frame: np.ndarray, layout: dict) -> None:
    """把 layout 里的元素从 frame 裁出来，按真实格式落盘（png + sidecar json）。"""
    import json

    tdir = cfg.template_root() / str(cfg.get("templates.profile", "default"))
    h, w = frame.shape[:2]
    for name, box in layout.items():
        png = tdir / f"{name}.png"
        png.parent.mkdir(parents=True, exist_ok=True)
        cv2.imencode(".png", crop_norm(frame, box))[1].tofile(str(png))
        x, y, bw, bh = box
        png.with_suffix(".json").write_text(json.dumps({
            "ref_size": [w, h],
            "region": [max(0.0, x - bw), max(0.0, y - bh),
                       min(1.0, bw * 3), min(1.0, bh * 3)],
            "threshold": 0.80,
        }), encoding="utf-8")


def make_bot(cfg: Config, layout: dict, current: str = "lobby") -> "StubBot":
    """渲染画面 + 落盘模板 + 建 Bot，一次做完。

    顺序很重要：模板必须在 `Bot.__init__` 之前落盘，否则 `TemplateLibrary`
    加载不到它们（它只在构造时扫一次目录）。
    """
    frame = render(layout, 1920, 1080)
    install_templates(cfg, frame, layout)
    return StubBot(cfg, frame, current=current)


def swap_scene(bot: "StubBot", layout: dict) -> np.ndarray:
    """运行中换一套模板和画面。

    必须同时 reload 模板库并清掉匹配缓存 —— 库只在构造时扫目录，
    不清就永远看不到新模板（测试里踩过一次，表现为"刚装的模板找不到"）。
    """
    frame = render(layout, 1920, 1080)
    install_templates(bot.cfg, frame, layout)
    bot.lib.reload()
    bot.matcher.clear_cache()
    bot.grabber.frames = [frame]
    bot.grabber.i = 0
    bot.invalidate_frame()
    return frame


@pytest.fixture()
def scene_frame() -> np.ndarray:
    """同时含大厅与战斗元素的合成帧（1920x1080）。"""
    return render({**LOBBY_LAYOUT, **BATTLE_LAYOUT}, 1920, 1080)


@pytest.fixture()
def full_cfg(cfg: Config, scene_frame: np.ndarray) -> Config:
    """装好全部模板的配置。"""
    install_templates(cfg, scene_frame, {**LOBBY_LAYOUT, **BATTLE_LAYOUT})
    return cfg


# --------------------------------------------------------------------------- #
# 假窗口 / 假截图
# --------------------------------------------------------------------------- #


class FakeWindow:
    def __init__(self, w: int = 1920, h: int = 1080):
        self.hwnd = 0x1234
        self.title = "Epic Seven (fake)"
        self.class_name = "FakeClass"
        self.pid = 4242
        self.exe = "EpicSeven.exe"
        self.client = Rect(0, 0, w, h)
        self.visible = True
        self.minimized = False

    def describe(self) -> str:
        return f"fake window {self.client.width}x{self.client.height}"


class FakeGrabber:
    """返回固定帧（或一组按顺序轮换的帧）。"""

    def __init__(self, frames):
        self.frames = list(frames) if isinstance(frames, list) else [frames]
        self.i = 0
        self.closed = False
        self.calls = 0

    def grab(self, rect: Rect) -> np.ndarray:
        self.calls += 1
        img = self.frames[min(self.i, len(self.frames) - 1)]
        self.i += 1
        return img.copy()

    def close(self) -> None:
        self.closed = True

    @property
    def backend_name(self) -> str:
        return "fake"

    @property
    def supports_occluded(self) -> bool:
        return False


class StubBot(Bot):
    """把窗口/截图/输入全部替换掉的 Bot。

    * `current` 决定 `scene()` 返回什么（测试里手动切换）
    * 点击/按键只记录下来，不真的发 SendInput
    * 所有 sleep 变成空操作（否则测试要跑几分钟）
    """

    def __init__(self, cfg: Config, frame: np.ndarray, current: str = "lobby"):
        super().__init__(cfg)
        self.window = FakeWindow(frame.shape[1], frame.shape[0])
        self.grabber = FakeGrabber(frame)
        self.current = current
        self.clicks: list[tuple] = []
        self.keys: list[str] = []
        self.debug_frames: list[str] = []
        #: 测试里可控的 OCR 读数（None = 读不出来，模拟字形没采全）
        self.ocr_number: int | None = None
        self.ocr_reads: list[tuple] = []

    # -- 覆盖掉一切需要真实系统的东西 ---------------------------------- #

    def client_rect(self) -> Rect:
        # 走真实的尺寸校验（但不碰 win32 的 IsWindow）
        return self._validate_rect(self.window.client)  # type: ignore[union-attr]

    def ensure_foreground(self) -> bool:
        return True

    def scene(self, force: bool = False) -> SceneResult:
        # 注意：这里刻意**不**设置 `_last_scene_name` —— 那是真实 Bot 里
        # `_tick` 的职责（它靠对比前后场景来决定要不要记事件）。
        # 测试替身如果抢着设了，`_tick` 就永远看不到"变化"，事件全丢。
        res = SceneResult(name=self.current)
        self._last_scene = res
        self.stats.scenes_seen[self.current] = self.stats.scenes_seen.get(self.current, 0) + 1
        return res

    def click_norm(self, nx: float, ny: float, clicks: int = 1, label: str = "") -> None:
        self.clicks.append(("norm", round(nx, 4), round(ny, 4), label))
        self.stats.clicks += 1

    def click_match(self, m, label: str = "", jitter_px=None) -> None:
        self.clicks.append(("match", m.name, m.norm_center, label))
        self.stats.clicks += 1

    def press(self, key: str) -> None:
        self.keys.append(key)
        self.stats.keys += 1

    def read_number(self, region) -> int | None:
        self.ocr_reads.append(region)
        return self.ocr_number

    def read_text(self, region) -> str | None:
        self.ocr_reads.append(region)
        return None

    def sleep(self, seconds: float) -> None:
        pass

    def random_sleep(self, span) -> None:
        pass

    def maybe_dump_periodic_frame(self) -> None:
        pass

    def _dump_debug_frame(self, tag: str):
        self.debug_frames.append(tag)
        return None

    # -- 断言辅助 -------------------------------------------------------- #

    def clicked(self, template: str) -> bool:
        return any(c[0] == "match" and c[1] == template for c in self.clicks)

    def clicked_names(self) -> list[str]:
        return [c[1] for c in self.clicks if c[0] == "match"]

    def reset(self) -> None:
        self.clicks.clear()
        self.keys.clear()
        self.debug_frames.clear()


@pytest.fixture()
def stub(full_cfg: Config, scene_frame: np.ndarray) -> StubBot:
    return StubBot(full_cfg, scene_frame)
