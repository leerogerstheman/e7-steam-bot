"""引擎：把窗口 / 截图 / 识别 / 输入 / 安全 缝合成一个可用的运行时。

一次 tick 的生命周期：

    check_safety()          # 急停键、运行时长、错误计数
    ensure_foreground()     # 游戏不在前台就等待（SendInput 只作用于前台窗口）
    frame()                 # 抓一帧（同一 tick 内复用，避免重复抓屏）
    scene()                 # 认场景
    task.tick(bot)          # 任务根据场景决定动作
    sleep(tick_interval)    # 随机化间隔，避免固定节拍

安全设计（针对 UNCHEATER 内核级反作弊）：
* **只读屏幕 + 只发键鼠事件**，不读游戏内存、不注入、不 hook、不改游戏文件；
* 全局急停热键（默认 F12）在任何时刻生效，检查粒度细到鼠标移动的每一段；
* 默认「游戏不在前台就暂停」，既保证输入有效，也避免在别的程序上乱点；
* 所有延时/轨迹/落点随机化，不做毫秒级固定节拍；
* dry-run 模式只识别不点击，用于上线前验证模板。
"""

from __future__ import annotations

import ctypes
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

import numpy as np

from .capture import CaptureError, ScreenGrabber
from .config import Config
from .humaninput import Keyboard, Mouse, vk_of
from .scene import SceneDetector, SceneResult
from .vision import Match, MatchOptions, Matcher, TemplateLibrary, annotate
from .winutil import (
    GameNotFound,
    Rect,
    WindowInfo,
    activate,
    client_rect_screen,
    enable_dpi_awareness,
    find_window,
    is_foreground,
    window_scale_note,
)

log = logging.getLogger("e7bot")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


# --------------------------------------------------------------------------- #
# 管理员权限 / 显示器常亮
# --------------------------------------------------------------------------- #


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def keep_display_awake(enable: bool = True) -> None:
    """阻止显示器休眠。

    DXGI Desktop Duplication 依赖显示器持续输出画面；显示器一睡，截图就会黑屏，
    脚本会以为"游戏卡住"。E7 的社区 PC 工具也踩过这个坑。
    """
    try:
        if enable:
            ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED
            )
        else:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 热键监听
# --------------------------------------------------------------------------- #


class HotkeyWatcher(threading.Thread):
    """后台轮询急停/暂停键。

    不用 RegisterHotKey 是因为它会被前台窗口抢走；GetAsyncKeyState 全局可靠，
    且不需要钩子（hook 容易被反作弊盯上）。
    """

    def __init__(self, stop_key: str, pause_key: str):
        super().__init__(daemon=True, name="hotkeys")
        self.stop_vk = vk_of(stop_key)
        self.pause_vk = vk_of(pause_key)
        self._stop_flag = threading.Event()
        self._pause_flag = threading.Event()
        self._stop_armed = True
        self._pause_armed = True

    @staticmethod
    def _down(vk: int) -> bool:
        return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)

    def run(self) -> None:
        while not self._stop_flag.is_set():
            if self._stop_armed and self._down(self.stop_vk):
                self._stop_armed = False
                log.warning("检测到急停热键，正在停止 …")
                self._stop_flag.set()
            elif not self._stop_armed and not self._down(self.stop_vk):
                self._stop_armed = True

            if self._pause_armed and self._down(self.pause_vk):
                self._pause_armed = False
                if self._pause_flag.is_set():
                    self._pause_flag.clear()
                    log.info("已继续运行")
                else:
                    self._pause_flag.set()
                    log.info("已暂停（再按一次继续）")
            elif not self._pause_armed and not self._down(self.pause_vk):
                self._pause_armed = True

            time.sleep(0.04)

    # -- 查询 -------------------------------------------------------------- #

    @property
    def stopped(self) -> bool:
        return self._stop_flag.is_set()

    @property
    def paused(self) -> bool:
        return self._pause_flag.is_set()

    def request_stop(self) -> None:
        self._stop_flag.set()


# --------------------------------------------------------------------------- #
# 运行统计
# --------------------------------------------------------------------------- #


@dataclass
class Stats:
    started: float = field(default_factory=time.time)
    ticks: int = 0
    clicks: int = 0
    keys: int = 0
    errors: int = 0
    consecutive_errors: int = 0
    unknown_scene_ticks: int = 0
    first_unknown_ts: Optional[float] = None
    scenes_seen: dict[str, int] = field(default_factory=dict)

    @property
    def uptime(self) -> float:
        return time.time() - self.started

    def summary(self) -> str:
        m, s = divmod(int(self.uptime), 60)
        h, m = divmod(m, 60)
        top = sorted(self.scenes_seen.items(), key=lambda kv: kv[1], reverse=True)[:5]
        scenes = ", ".join(f"{k}×{v}" for k, v in top) or "-"
        return (
            f"运行 {h:02d}:{m:02d}:{s:02d} | tick {self.ticks} | 点击 {self.clicks} "
            f"| 按键 {self.keys} | 错误 {self.errors} | 主要场景: {scenes}"
        )


# --------------------------------------------------------------------------- #
# 运行时异常
# --------------------------------------------------------------------------- #


class StopRequested(Exception):
    """需要干净退出（用户急停 / 超时 / 任务完成）。"""


class SafetyViolation(StopRequested):
    pass


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #


class Bot:
    def __init__(self, cfg: Config, dry_run: Optional[bool] = None, profile: Optional[str] = None):
        self.cfg = cfg
        self.dry_run = bool(cfg.get("safety.dry_run", False) if dry_run is None else dry_run)
        self.stats = Stats()

        self.window: Optional[WindowInfo] = None
        self.grabber: Optional[ScreenGrabber] = None

        profile = profile or str(cfg.get("templates.profile", "default"))
        self.lib = TemplateLibrary(cfg.template_root(), profile)
        self.matcher = Matcher(self.lib, float(cfg.get("templates.default_threshold", 0.86)))
        self.scenes = SceneDetector(
            self.matcher,
            cfg.section("scenes"),
            threshold=float(cfg.get("templates.default_threshold", 0.86)),
            scale_tolerance=float(cfg.get("templates.scale_tolerance", 0.0)),
        )

        human = cfg.humanizer()
        self.mouse = Mouse(human, stop_check=self.should_stop)
        self.keyboard = Keyboard(human, stop_check=self.should_stop)
        self.hotkeys: Optional[HotkeyWatcher] = None

        self._frame: Optional[np.ndarray] = None
        self._rect: Optional[Rect] = None
        self._frame_tick = -1
        self._last_scene: Optional[SceneResult] = None
        self._debug_dir = cfg.log_dir() / "frames"
        self._last_debug_dump = 0.0

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def start(self, picker=None) -> None:
        enable_dpi_awareness()
        keep_display_awake(True)

        if not is_admin():
            log.warning(
                "当前不是管理员权限。官方 PC 端通常以管理员运行，"
                "受 UIPI 限制，非管理员进程发送的键鼠事件会被静默丢弃。"
                "建议右键 -> 以管理员身份运行。"
            )

        self.window = find_window(
            title_patterns=self.cfg.get("window.title_patterns", []),
            exe_patterns=self.cfg.get("window.exe_patterns", []),
            min_size=tuple(self.cfg.get("window.min_size", (800, 450))),  # type: ignore[arg-type]
            picker=picker,
        )
        if self.window is None:
            raise GameNotFound(
                "没有找到第七史诗窗口。请先启动游戏（Steam 版 / Demo），"
                "并确认窗口标题与 config 里的 window.title_patterns 匹配。"
                "可以用 `python run.py doctor --list-windows` 查看所有窗口标题。"
            )

        log.info("已锁定游戏窗口: %s", self.window.describe())
        log.info("客户区分辨率: %s（DPI 模式 %s）",
                 window_scale_note(self.window.client), __import__("e7bot.winutil", fromlist=["x"]).dpi_mode())

        self.grabber = ScreenGrabber(
            hwnd=self.window.hwnd,
            preferred=list(self.cfg.get("capture.backends", ["bettercam", "mss", "printwindow"])),
            monitor_index=int(self.cfg.get("capture.monitor_index", 0)),
            target_fps=int(self.cfg.get("capture.target_fps", 60)),
        )

        missing = self.scenes.missing_templates()
        if missing:
            log.warning(
                "缺少 %d 个场景锚点模板，相关场景无法识别: %s",
                len(missing), ", ".join(missing[:12]) + (" …" if len(missing) > 12 else ""),
            )
            log.warning("用 `python tools/capture_template.py` 从真实客户端截图补全模板。")

        self.hotkeys = HotkeyWatcher(
            str(self.cfg.get("safety.fail_safe_key", "f12")),
            str(self.cfg.get("safety.pause_key", "f9")),
        )
        self.hotkeys.start()
        log.info(
            "热键已就绪: %s = 急停, %s = 暂停/继续%s",
            self.cfg.get("safety.fail_safe_key", "f12"),
            self.cfg.get("safety.pause_key", "f9"),
            "  [DRY-RUN 模式：只识别不点击]" if self.dry_run else "",
        )

    def stop(self) -> None:
        keep_display_awake(False)
        if self.hotkeys:
            self.hotkeys.request_stop()
        if self.grabber:
            try:
                self.grabber.close()
            except Exception:
                pass
            self.grabber = None
        log.info("已停止。%s", self.stats.summary())

    # ------------------------------------------------------------------ #
    # 安全
    # ------------------------------------------------------------------ #

    def should_stop(self) -> bool:
        return bool(self.hotkeys and self.hotkeys.stopped)

    def check_safety(self) -> None:
        if self.should_stop():
            raise StopRequested("收到急停指令")

        max_min = float(self.cfg.get("safety.max_runtime_minutes", 480))
        if max_min > 0 and self.stats.uptime > max_min * 60:
            raise SafetyViolation(f"已达到最大运行时长 {max_min:.0f} 分钟，自动停止")

        limit = int(self.cfg.get("safety.max_consecutive_errors", 8))
        if self.stats.consecutive_errors >= limit:
            raise SafetyViolation(f"连续出错 {limit} 次，自动停止（请看日志定位原因）")

    def wait_if_paused(self) -> None:
        while self.hotkeys and self.hotkeys.paused and not self.should_stop():
            time.sleep(0.2)

    def ensure_foreground(self) -> bool:
        """确保游戏在前台。返回 True 表示可以继续操作。"""
        if self.window is None:
            return False
        if not bool(self.cfg.get("window.auto_activate", True)):
            return True
        if is_foreground(self.window.hwnd):
            return True
        if not bool(self.cfg.get("window.pause_when_not_foreground", True)):
            return True

        log.debug("游戏不在前台，尝试切回前台 …")
        return activate(self.window.hwnd, float(self.cfg.get("window.activate_settle", 0.35)))

    # ------------------------------------------------------------------ #
    # 帧
    # ------------------------------------------------------------------ #

    def client_rect(self) -> Rect:
        if self.window is None:
            raise StopRequested("窗口已丢失")
        if not self.window.hwnd or not ctypes.windll.user32.IsWindow(self.window.hwnd):
            raise StopRequested("游戏窗口已关闭")
        rect = client_rect_screen(self.window.hwnd)
        if rect.width < 64 or rect.height < 64:
            raise SafetyViolation(f"客户区尺寸异常: {rect.as_tuple()}（窗口被最小化？）")
        return rect

    def frame(self) -> tuple[np.ndarray, Rect]:
        """取当前帧。同一 tick 内多次调用复用同一帧。"""
        if self._frame is not None and self._frame_tick == self.stats.ticks:
            assert self._rect is not None
            return self._frame, self._rect
        rect = self.client_rect()
        assert self.grabber is not None
        img = self.grabber.grab(rect)
        self._frame, self._rect, self._frame_tick = img, rect, self.stats.ticks
        return img, rect

    def invalidate_frame(self) -> None:
        self._frame = None
        self._rect = None
        self.scenes.invalidate()

    # ------------------------------------------------------------------ #
    # 识别
    # ------------------------------------------------------------------ #

    def find(
        self,
        name: str,
        region: Optional[tuple[float, float, float, float]] = None,
        threshold: Optional[float] = None,
        scale_tolerance: Optional[float] = None,
    ) -> Optional[Match]:
        img, rect = self.frame()
        return self.matcher.find(
            name, img, rect, region,
            MatchOptions(
                threshold=threshold,
                scale_tolerance=(
                    float(self.cfg.get("templates.scale_tolerance", 0.0))
                    if scale_tolerance is None else scale_tolerance
                ),
            ),
        )

    def find_all(self, name: str, region=None, threshold=None) -> list[Match]:
        img, rect = self.frame()
        return self.matcher.find_all(
            name, img, rect, region, MatchOptions(threshold=threshold, max_results=32)
        )

    def exists(self, name: str, **kw) -> bool:
        return self.find(name, **kw) is not None

    def wait(
        self,
        name: str,
        timeout: float = 15.0,
        interval: float = 0.25,
        region=None,
        threshold: Optional[float] = None,
    ) -> Optional[Match]:
        """等待某个模板出现；超时返回 None（不抛异常，交给调用方决策）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.check_safety()
            self.wait_if_paused()
            self.invalidate_frame()
            m = self.find(name, region, threshold)
            if m is not None:
                return m
            time.sleep(interval * random.uniform(0.8, 1.2))
        return None

    def wait_any(
        self, names: Iterable[str], timeout: float = 15.0, interval: float = 0.25
    ) -> Optional[Match]:
        """等待一组模板中任意一个出现（常用于"等加载完/等弹窗"）。"""
        names = list(names)
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.check_safety()
            self.wait_if_paused()
            self.invalidate_frame()
            img, rect = self.frame()
            for n in names:
                try:
                    m = self.matcher.find(
                        n, img, rect,
                        opts=MatchOptions(
                            threshold=float(self.cfg.get("templates.default_threshold", 0.86)),
                            scale_tolerance=float(self.cfg.get("templates.scale_tolerance", 0.0)),
                        ),
                    )
                except KeyError:
                    continue
                if m is not None:
                    return m
            time.sleep(interval * random.uniform(0.8, 1.2))
        return None

    def scene(self, force: bool = False) -> SceneResult:
        self.check_safety()
        img, rect = self.frame()
        res = self.scenes.detect(img, rect, use_cache=not force)
        self._last_scene = res
        self.stats.scenes_seen[res.name] = self.stats.scenes_seen.get(res.name, 0) + 1

        if res.is_unknown:
            if self.stats.first_unknown_ts is None:
                self.stats.first_unknown_ts = time.time()
            waited = time.time() - self.stats.first_unknown_ts
            limit = float(self.cfg.get("safety.unknown_scene_timeout", 90))
            if limit > 0 and waited > limit:
                self._dump_debug_frame("unknown_scene")
                raise SafetyViolation(
                    f"连续 {waited:.0f} 秒无法识别场景（上限 {limit:.0f}s）。"
                    f"最近一次尝试: {res.evaluated[:8]}。已保存调试图到 {self._debug_dir}"
                )
        else:
            self.stats.first_unknown_ts = None
        return res

    def wait_scene(self, name: str, timeout: float = 30.0, interval: float = 0.3) -> Optional[SceneResult]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.check_safety()
            self.wait_if_paused()
            self.invalidate_frame()
            res = self.scene(force=True)
            if res.name == name:
                return res
            time.sleep(interval * random.uniform(0.8, 1.2))
        return None

    # ------------------------------------------------------------------ #
    # 操作
    # ------------------------------------------------------------------ #

    def click_norm(self, nx: float, ny: float, clicks: int = 1, label: str = "") -> None:
        _, rect = self.frame()
        x, y = rect.to_screen(nx, ny)
        if self.dry_run:
            log.info("[dry-run] 点击 (%.3f, %.3f) -> 屏幕 (%d, %d) %s", nx, ny, x, y, label)
            return
        self.mouse.click(x, y, clicks=clicks)
        self.stats.clicks += 1
        log.debug("点击 %s (%.3f, %.3f)", label or "-", nx, ny)

    def click_match(self, m: Match, label: str = "", jitter_px: Optional[float] = None) -> None:
        if self.dry_run:
            log.info("[dry-run] 点击模板 %s @ %s score=%.3f %s", m.name, m.center, m.score, label)
            return
        self.mouse.click(m.center[0], m.center[1], jitter=jitter_px)
        self.stats.clicks += 1
        log.debug("点击模板 %s score=%.3f %s", m.name, m.score, label)

    def click_template(
        self,
        name: str,
        timeout: float = 6.0,
        region=None,
        threshold: Optional[float] = None,
        label: str = "",
        required: bool = True,
    ) -> bool:
        """找到模板并点击。找不到时按 required 决定抛错还是返回 False。"""
        m = self.find(name, region, threshold)
        if m is None and timeout > 0:
            m = self.wait(name, timeout=timeout, region=region, threshold=threshold)
        if m is None:
            if required:
                self._dump_debug_frame(f"miss_{name.replace('/', '_')}")
                raise SafetyViolation(f"找不到并点击失败: {name}（已保存调试图）")
            log.debug("未找到模板 %s（可选）", name)
            return False
        self.click_match(m, label=label or name)
        return True

    def press(self, key: str) -> None:
        if self.dry_run:
            log.info("[dry-run] 按键 %s", key)
            return
        self.keyboard.tap(key)
        self.stats.keys += 1

    def sleep(self, seconds: float) -> None:
        """可被打断的睡眠。"""
        end = time.time() + seconds
        while time.time() < end:
            if self.should_stop():
                return
            time.sleep(min(0.1, max(0.0, end - time.time())))

    def random_sleep(self, span: Iterable[float]) -> None:
        lo, hi = list(span)[:2]
        self.sleep(random.uniform(float(lo), float(hi)))

    # ------------------------------------------------------------------ #
    # 调试
    # ------------------------------------------------------------------ #

    def _dump_debug_frame(self, tag: str) -> None:
        if not bool(self.cfg.get("logging.save_debug_frames", True)):
            return
        try:
            img, rect = self.frame()
            stamp = time.strftime("%H%M%S")
            annotate(img, rect, [], self._debug_dir / f"{stamp}_{tag}.png")
            log.info("已保存调试图: %s", self._debug_dir / f"{stamp}_{tag}.png")
        except Exception:
            pass

    def maybe_dump_periodic_frame(self) -> None:
        if not bool(self.cfg.get("logging.save_debug_frames", False)):
            return
        interval = float(self.cfg.get("logging.debug_frame_interval", 30.0))
        if time.time() - self._last_debug_dump < interval:
            return
        self._last_debug_dump = time.time()
        try:
            img, rect = self.frame()
            stamp = time.strftime("%H%M%S")
            annotate(img, rect, [], self._debug_dir / f"{stamp}_periodic.png")
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #

    def run(self, tasks: list, tick_interval: float = 0.35, max_ticks: int = 0) -> None:
        if self.window is None or self.grabber is None:
            raise RuntimeError("请先调用 start()")

        log.info("开始运行，任务链: %s", " -> ".join(t.name for t in tasks))
        try:
            while True:
                if max_ticks and self.stats.ticks >= max_ticks:
                    log.info("达到 max_ticks=%d，退出", max_ticks)
                    break

                self.check_safety()
                self.wait_if_paused()

                if not self.ensure_foreground():
                    log.debug("等待游戏回到前台 …")
                    self.sleep(0.5)
                    continue

                self.stats.ticks += 1
                self.invalidate_frame()
                self.maybe_dump_periodic_frame()

                try:
                    done = self._tick(tasks)
                    self.stats.consecutive_errors = 0
                    if done:
                        log.info("所有任务已完成")
                        break
                except StopRequested:
                    raise
                except SafetyViolation:
                    raise
                except CaptureError as exc:
                    self.stats.errors += 1
                    self.stats.consecutive_errors += 1
                    log.warning("抓帧失败: %s", exc)
                except Exception as exc:  # noqa: BLE001
                    self.stats.errors += 1
                    self.stats.consecutive_errors += 1
                    log.exception("tick 异常: %s", exc)

                self.sleep(tick_interval * random.uniform(0.75, 1.3))
        except StopRequested as exc:
            log.info("停止: %s", exc)
        finally:
            self.stop()

    def _tick(self, tasks: list) -> bool:
        """跑一遍任务链；返回 True 表示全部完成。"""
        scene = self.scene()
        log.debug("场景: %s", scene.describe())

        for task in tasks:
            if task.finished:
                continue
            if not task.can_handle(scene.name):
                continue
            task.tick(self)
            return False  # 一个 tick 只让一个任务动作，避免抢同一个界面
        return all(t.finished for t in tasks)
