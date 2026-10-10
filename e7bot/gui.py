"""系统托盘 GUI。

命令行入口：

    python -m e7bot.gui [--config config/default.toml] [--dry-run] [--tasks repeat_stage]

为什么是"托盘"而不是窗口：这个脚本的绝大部分时间都在后台跑游戏，
用户需要的是一个**随时可见的状态灯 + 一键急停**，而不是一个会挡着游戏的窗口。
所以整个 GUI 就是一个托盘图标 + 右键菜单，没有任何主窗口。

几个关键设计决定（都是被 Windows / 打包环境逼出来的）：

1. **pystray 必须跑在主线程**：Windows 的托盘图标依赖一个消息循环，
   pystray 的 ``icon.run()`` 内部就是 ``GetMessage`` 循环，它只能在主线程跑。
   于是 ``Bot.run()``（阻塞的 ``while True``）必须扔进 ``threading.Thread``。
2. **图标在内存里画**（Pillow），不读外部 .ico —— PyInstaller 打包后
   资源路径会变成 ``sys._MEIPASS`` 临时目录，任何"相对当前文件找图片"的写法
   都会在打包后翻车。
3. **单实例互斥体**：两个托盘程序同时跑会互相抢输入焦点、互相抢热键，
   画面会乱套。用 Windows 命名 Mutex 挡住。
4. **启动就检查管理员权限**：官方 PC 端以管理员运行，受 UIPI 限制，
   非管理员进程发的键鼠事件会被**静默丢弃**（不报错，只是没反应），
   这是最难排查的坑，所以必须显式告警。
5. **每个 Bot 实例只能用一次**：``engine.Bot.stop()`` 会把
   ``HotkeyWatcher`` 的急停标志永久置位，所以"停止后再次开始"必须
   **重新 new 一个 Bot**，不能复用。
"""

from __future__ import annotations

import argparse
import ctypes
import importlib
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# 入口兼容：让本文件既能 `python -m e7bot.gui`，也能被 PyInstaller 当成
# __main__ 直接执行
#
# 这是打包最阴的一个坑：PyInstaller 把入口脚本以 **__main__** 身份执行，
# 此时 __package__ 是 None，于是文件里所有相对导入
# （`from .config import Config` / `from .engine import Bot` …）都会抛
#     ImportError: attempted relative import with no known parent package
# 而且 `--help` 之类的用法**测不出来**（argparse 在第一次相对导入之前就退出了），
# `--windowed` 下连 traceback 都看不见，表现就是"双击 exe 没反应"。
#
# 修法：**一律用绝对导入**（`from e7bot.config import Config`），
# 并在"源码直接执行"（`python e7bot/gui.py`）时把项目根塞进 sys.path。
#
# 一个反直觉的细节：打包后入口脚本的 __file__ 是 **<_MEIPASS>/gui.py**
# （PyInstaller 把入口脚本摊平到 _MEIPASS 根，而不是 <_MEIPASS>/e7bot/gui.py），
# 所以**不能**用 `Path(__file__).parent.name` 猜包名 —— 会猜成 `_MEI000072d82`，
# 报 `No module named '_MEI000072d82.config'`。包名在这里是常量。
# 打包后 e7bot 包在 PYZ 里、_MEIPASS 已在 sys.path，什么都不用做。
# --------------------------------------------------------------------------- #
PACKAGE = "e7bot"

if __package__ in (None, ""):  # pragma: no cover - 只在直接执行 / 打包后触发
    if not getattr(sys, "frozen", False):
        # 源码直接执行：__file__ = <项目根>/e7bot/gui.py
        _package_parent = Path(__file__).resolve().parent.parent
        if str(_package_parent) not in sys.path:
            sys.path.insert(0, str(_package_parent))

# --------------------------------------------------------------------------- #
# 可选依赖：pystray / Pillow
#
# 这两个只服务于 GUI，不是跑任务必需的。缺失时必须给出**中文可操作提示**，
# 而不是让用户看到 ImportError traceback（--windowed 打包后连 traceback 都看不见）。
# --------------------------------------------------------------------------- #

try:  # pragma: no cover - 依赖环境
    import pystray
    from pystray import Menu, MenuItem

    PYSTRAY_AVAILABLE = True
    _PYSTRAY_ERROR = ""
except Exception as _exc:  # noqa: BLE001
    pystray = None  # type: ignore[assignment]
    Menu = None  # type: ignore[assignment]
    MenuItem = None  # type: ignore[assignment]
    PYSTRAY_AVAILABLE = False
    _PYSTRAY_ERROR = f"{type(_exc).__name__}: {_exc}"

try:  # pragma: no cover - 依赖环境
    from PIL import Image, ImageDraw

    PIL_AVAILABLE = True
    _PIL_ERROR = ""
except Exception as _exc:  # noqa: BLE001
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    PIL_AVAILABLE = False
    _PIL_ERROR = f"{type(_exc).__name__}: {_exc}"


log = logging.getLogger("e7bot.gui")

APP_NAME = "e7bot-gui"
MUTEX_NAME = r"Local\e7bot-gui-single-instance"

INSTALL_HINT = (
    "缺少托盘 GUI 依赖。请在项目目录执行：\n\n"
    "    pip install -r requirements-gui.txt\n\n"
    "（等价于 pip install pystray Pillow）\n"
    "只跑命令行不需要这两个包：python run.py run"
)

# --------------------------------------------------------------------------- #
# 状态常量与颜色
#
# 颜色语义和"能不能一眼看出脚本在干什么"直接相关，所以固定下来：
# 灰=没跑 / 绿=在跑 / 黄=暂停 / 红=出错。
# --------------------------------------------------------------------------- #

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_PAUSED = "paused"
STATE_STOPPED = "stopped"
STATE_ERROR = "error"

STATE_COLORS: dict[str, tuple[int, int, int]] = {
    STATE_IDLE: (128, 128, 128),      # 灰：从未启动
    STATE_RUNNING: (46, 160, 67),     # 绿：运行中
    STATE_PAUSED: (219, 154, 4),      # 黄：已暂停
    STATE_STOPPED: (96, 96, 96),      # 深灰：跑过但已停
    STATE_ERROR: (203, 56, 56),       # 红：出错
}


# --------------------------------------------------------------------------- #
# 路径解析（打包 / 未打包 两种形态）
# --------------------------------------------------------------------------- #


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打出来的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """应用根目录 —— config/ templates/ logs/ 都挂在这个目录下。

    * 打包后：``sys.executable`` 所在目录（exe 旁边）。
      **不能**用 ``sys._MEIPASS``：那是 onefile 解压出来的临时目录，
      进程退出就删，用户往里放模板等于白放。
    * 未打包：``e7bot/`` 的上一级，也就是项目根。

    这个函数是"config/templates 作为外部目录"这条要求的落地点。
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def default_config_path() -> Path:
    return app_root() / "config" / "default.toml"


def resolve_config_arg(value: Optional[str]) -> Path:
    """把 --config 的值变成绝对路径。

    相对路径优先按当前工作目录理解；找不到就退回 exe / 项目根目录 ——
    从开始菜单快捷方式启动时 cwd 可能是 system32，直接按 cwd 解析必然找不到。
    """
    if not value:
        return default_config_path()
    p = Path(value)
    if p.is_absolute():
        return p
    if p.exists():
        return p.resolve()
    return (app_root() / p).resolve()


# --------------------------------------------------------------------------- #
# 日志（复制 run.py 的 setup_logging 思路，但必须容忍 --windowed 下没有 stdout）
# --------------------------------------------------------------------------- #


def setup_logging(cfg: Any, verbose: bool = False) -> None:
    """初始化日志。

    与 run.py 的差异：打包成 ``--windowed`` 后 ``sys.stdout`` / ``sys.stderr``
    可能是 ``None``，此时再挂 ``StreamHandler`` 会在第一次写日志时抛异常。
    所以控制台 handler 变成"有才挂"。
    """
    level_name = "DEBUG" if verbose else str(cfg.get("logging.level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)

    try:
        log_dir = cfg.log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        log_dir = app_root() / "logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        print(f"日志目录不可用（{exc}），改用 {log_dir}", file=sys.stderr or sys.stdout or sys.__stdout__)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-18s %(message)s", datefmt="%H:%M:%S"
    )
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    if sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        root.addHandler(console)

    try:
        from logging.handlers import RotatingFileHandler

        fh = RotatingFileHandler(
            log_dir / "e7bot.log", maxBytes=4 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception as exc:  # noqa: BLE001
        log.warning("无法写日志文件: %s", exc)

    log.info("日志目录: %s", log_dir)


def load_config(path: Optional[str | Path] = None) -> Any:
    """载入配置；任何失败都退回内置默认配置，绝不抛异常给 GUI。

    这里刻意不 import 顶层的 ``e7bot``（避免循环导入），直接用 ``e7bot.config``。
    """
    from e7bot.config import Config

    p = resolve_config_arg(str(path)) if path is not None else default_config_path()
    if not p.exists():
        log.warning("未找到配置文件 %s，使用内置默认配置", p)
        cfg = Config()
        # 关键：给 source 一个合理值。config.resolve_path() 以"配置文件所在目录"
        # 为基准解析 templates/logs；source=None 时基准会退化成 cwd，
        # 打包后从快捷方式启动就变成 system32/templates，用户会一脸问号。
        cfg.source = p
        return cfg

    try:
        cfg = Config.load(p)
    except Exception as exc:  # noqa: BLE001
        log.error("配置文件解析失败: %s（改用内置默认配置）", exc)
        cfg = Config()
        cfg.source = p
        return cfg

    for prob in cfg.validate():
        log.warning("配置问题: %s", prob)
    return cfg


def open_in_explorer(path: Path) -> bool:
    """用资源管理器打开目录（不存在就建出来）。返回是否成功。"""
    try:
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        os.startfile(str(p))  # type: ignore[attr-defined]  # 仅 Windows
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("打开目录失败 %s: %s", path, exc)
        return False


# --------------------------------------------------------------------------- #
# 图标生成（纯函数，方便无头测试）
# --------------------------------------------------------------------------- #


def make_icon_image(color: tuple[int, int, int], size: int = 64) -> Any:
    """用 Pillow 在内存里画一个状态圆点图标。

    刻意不读外部 .ico：onefile 打包后资源在临时目录里，
    "相对文件路径找图片"是最经典的打包翻车点。
    """
    if not PIL_AVAILABLE:
        raise RuntimeError(f"缺少 Pillow：{_PIL_ERROR}")
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = max(1, size // 16)
    draw.ellipse(
        (pad, pad, size - pad - 1, size - pad - 1),
        fill=color + (255,),
        outline=(24, 24, 24, 255),
        width=max(1, size // 20),
    )
    # 中间一个白色小点：托盘图标只有 16x16，纯色块在深浅任务栏上都不好认。
    inner = size // 4
    c = size // 2
    draw.ellipse((c - inner, c - inner, c + inner, c + inner), fill=(255, 255, 255, 180))
    return img


def icon_color_for(state: str) -> tuple[int, int, int]:
    return STATE_COLORS.get(state, STATE_COLORS[STATE_IDLE])


def format_status(state: str, ticks: int = 0, clicks: int = 0) -> str:
    """状态 -> 菜单里那行不可点击的动态文本（纯函数，方便无头测试）。"""
    if state == STATE_RUNNING:
        return f"运行中 · tick {int(ticks)} · 点击 {int(clicks)}"
    if state == STATE_PAUSED:
        return "已暂停"
    if state == STATE_STOPPED:
        return "已停止"
    if state == STATE_ERROR:
        return "出错"
    return "未运行"


def format_tooltip(state: str, ticks: int = 0, clicks: int = 0, detail: str = "") -> str:
    """鼠标悬停时的提示文本（比菜单项宽松，可以塞更多信息）。"""
    text = f"e7bot · {format_status(state, ticks, clicks)}"
    if state == STATE_ERROR and detail:
        return f"{text} · {detail}"
    return text


# --------------------------------------------------------------------------- #
# 暂停控制
# --------------------------------------------------------------------------- #


def set_hotkeys_paused(hotkeys: Any, paused: bool) -> bool:
    """设置 ``HotkeyWatcher`` 的暂停状态；返回是否生效。

    ``engine.HotkeyWatcher`` 只暴露了**只读**的 ``paused`` 属性和
    ``request_stop()``，没有公开的写入接口（我们被要求不要改 engine.py）。
    所以这里按"先找公开 API，再退到内部 Event"的顺序来：
    上游哪天加了 ``set_paused()``，这里自动切过去，不需要再改。
    """
    if hotkeys is None:
        return False

    setter = getattr(hotkeys, "set_paused", None)
    if callable(setter):
        try:
            setter(bool(paused))
            return True
        except Exception:  # noqa: BLE001
            log.debug("hotkeys.set_paused() 调用失败", exc_info=True)
            return False

    flag = getattr(hotkeys, "_pause_flag", None)
    if flag is None:
        return False
    try:
        if paused:
            flag.set()
        else:
            flag.clear()
        return True
    except Exception:  # noqa: BLE001
        log.debug("直接操作 hotkeys._pause_flag 失败", exc_info=True)
        return False


def hotkeys_paused(hotkeys: Any) -> bool:
    try:
        return bool(getattr(hotkeys, "paused", False))
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# 单实例保护
# --------------------------------------------------------------------------- #


class SingleInstance:
    """Windows 命名互斥体实现的单实例锁。

    为什么必须要有：两个托盘程序同时跑，会各自 ``SendInput``，
    还会互相把游戏窗口抢到前台，画面直接乱掉 —— 而且用户很难意识到
    自己开了两个。

    命名空间用 ``Local\\``（会话级）而不是 ``Global\\``：我们只关心
    "同一个桌面会话里别开两个"。创建 ``Global\\`` 对象需要
    SeCreateGlobalPrivilege（普通用户没有），非管理员启动会直接失败。
    """

    ERROR_ALREADY_EXISTS = 183
    ERROR_ACCESS_DENIED = 5

    def __init__(self, name: str = MUTEX_NAME) -> None:
        self.name = name
        self._handle: Optional[int] = None

    def acquire(self) -> bool:
        """返回 True = 本进程拿到所有权（或者无法判定时保守放行）。"""
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateMutexW.restype = ctypes.c_void_p
            k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
            handle = k32.CreateMutexW(None, 0, self.name)
            err = ctypes.get_last_error()
        except Exception as exc:  # noqa: BLE001
            log.warning("创建互斥体失败，跳过单实例检查: %s", exc)
            return True

        if handle:
            if err == self.ERROR_ALREADY_EXISTS:
                # 同名互斥体已存在 = 已经有一个实例在跑
                self._handle = handle
                self.release()
                return False
            self._handle = handle
            return True

        if err == self.ERROR_ACCESS_DENIED:
            # 同会话里已有一个**更高完整性级别**（提权）的实例持有它，
            # 我们连打开都没权限 —— 这同样等于"已经在运行"。
            return False

        # 其它错误：宁可让用户能启动，也不要因为拿不到锁就开不了程序。
        log.warning("CreateMutexW 失败 (err=%s)，跳过单实例检查", err)
        return True

    def release(self) -> None:
        if self._handle:
            try:
                ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(
                    ctypes.c_void_p(self._handle)
                )
            except Exception:  # noqa: BLE001
                pass
            self._handle = None


# --------------------------------------------------------------------------- #
# 消息框 / 通知
# --------------------------------------------------------------------------- #

MB_OK = 0x00000000
MB_ICONERROR = 0x00000010
MB_ICONWARNING = 0x00000030
MB_ICONINFORMATION = 0x00000040
MB_TOPMOST = 0x00040000
MB_SETFOREGROUND = 0x00010000


def message_box(text: str, title: str = APP_NAME, flags: int = MB_OK | MB_ICONINFORMATION) -> None:
    """原生 MessageBox。

    ``--windowed`` 打包后没有控制台，print 出去的信息用户永远看不到，
    所以"致命错误"必须走 MessageBox。任何失败都静默吞掉。
    """
    try:
        ctypes.windll.user32.MessageBoxW(None, str(text), str(title), int(flags))
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# 托盘应用
# --------------------------------------------------------------------------- #


class TrayApp:
    """托盘 GUI 的全部状态与逻辑。

    线程模型：
    * **主线程**：``icon.run()`` 的 Windows 消息循环（pystray 硬性要求）。
    * **worker 线程**：``Bot.start()`` + ``Bot.run()``（阻塞）。
    * **refresh 线程**：每秒刷新图标颜色 / tooltip / 菜单文本。
    """

    def __init__(
        self,
        config_path: Optional[Path] = None,
        dry_run: bool = False,
        task_names: Optional[list[str]] = None,
        profile: Optional[str] = None,
        tick: float = 0.35,
    ) -> None:
        self._cfg_path: Path = Path(config_path) if config_path else default_config_path()
        self._dry_run = bool(dry_run)
        self._task_names = list(task_names) if task_names else None
        self._profile = profile
        self._tick = float(tick)

        self._cfg = load_config(self._cfg_path)

        self._lock = threading.RLock()
        self._bot: Any = None
        self._worker: Optional[threading.Thread] = None
        self._state: str = STATE_IDLE
        self._last_error: str = ""
        # 用户是否主动点了"停止"：用来区分"用户停的"和"引擎自己安全停机的"，
        # 后者需要额外提醒（见 _worker_main 里对 _stop_reason 的处理）。
        self._stop_requested = False
        # hotkeys 缺失时的兜底暂停标志。注意：engine 的 wait_if_paused()
        # 只认 hotkeys.paused，所以这个 Event 只在没有 hotkeys 时用来
        # 维持"界面上显示已暂停"的一致性 —— 见 docs/GUI.md 的说明。
        self._pause_event = threading.Event()
        self._refresh_stop = threading.Event()
        self._refresh_thread: Optional[threading.Thread] = None
        self._icon: Any = None
        self._last_menu_text = ""

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #

    def _effective_state(self) -> str:
        """对外可见的状态。

        "暂停"不单独存变量，而是**实时读 hotkeys**：用户可能按 F9 物理键暂停，
        那样 GUI 里的按钮状态必须跟着变，单一数据源才不会有分歧。
        """
        with self._lock:
            state = self._state
            bot = self._bot
        if state != STATE_RUNNING:
            return state
        hotkeys = getattr(bot, "hotkeys", None) if bot is not None else None
        if hotkeys_paused(hotkeys):
            return STATE_PAUSED
        # hotkeys 还没建好（start() 到 Bot.start() 之间那一小段窗口）：
        # 这时 engine 的 wait_if_paused() 不会真的等待，只能用 GUI 侧 Event
        # 维持界面状态一致，并在日志里说明。
        if hotkeys is None and self._pause_event.is_set():
            return STATE_PAUSED
        return STATE_RUNNING

    def _is_running(self) -> bool:
        with self._lock:
            worker = self._worker
        return worker is not None and worker.is_alive()

    def _counters(self) -> tuple[int, int]:
        with self._lock:
            bot = self._bot
        stats = getattr(bot, "stats", None) if bot is not None else None
        return int(getattr(stats, "ticks", 0) or 0), int(getattr(stats, "clicks", 0) or 0)

    def _status_text(self) -> str:
        ticks, clicks = self._counters()
        return format_status(self._effective_state(), ticks, clicks)

    def _tooltip_text(self) -> str:
        ticks, clicks = self._counters()
        return format_tooltip(self._effective_state(), ticks, clicks, self._last_error)

    # ------------------------------------------------------------------ #
    # 菜单
    # ------------------------------------------------------------------ #

    def _build_menu(self) -> Any:
        assert Menu is not None and MenuItem is not None  # 调用前已检查依赖
        return Menu(
            # 状态行：不可点击，文本是动态的
            MenuItem(lambda _item: self._status_text(), None, enabled=False),
            Menu.SEPARATOR,
            MenuItem("开始", self._on_start, enabled=lambda _item: not self._is_running()),
            MenuItem(
                "以 dry-run 模式启动",
                self._on_start_dry,
                enabled=lambda _item: not self._is_running(),
            ),
            MenuItem("停止", self._on_stop, enabled=lambda _item: self._is_running()),
            Menu.SEPARATOR,
            MenuItem(
                "暂停",
                self._on_pause,
                enabled=lambda _item: self._is_running()
                and self._effective_state() != STATE_PAUSED,
            ),
            MenuItem(
                "继续",
                self._on_resume,
                enabled=lambda _item: self._effective_state() == STATE_PAUSED,
            ),
            Menu.SEPARATOR,
            MenuItem("打开日志目录", self._on_open_logs),
            MenuItem("打开模板目录", self._on_open_templates),
            Menu.SEPARATOR,
            MenuItem("退出", self._on_quit),
        )

    # ------------------------------------------------------------------ #
    # 菜单动作（每个都自己兜异常 —— GUI 绝不能因为一次点击就死掉）
    # ------------------------------------------------------------------ #

    def _on_start(self, _icon: Any = None, _item: Any = None) -> None:
        self.start(dry_run=self._dry_run)

    def _on_start_dry(self, _icon: Any = None, _item: Any = None) -> None:
        self.start(dry_run=True)

    def _on_stop(self, _icon: Any = None, _item: Any = None) -> None:
        self.stop()

    def _on_pause(self, _icon: Any = None, _item: Any = None) -> None:
        self.set_paused(True)

    def _on_resume(self, _icon: Any = None, _item: Any = None) -> None:
        self.set_paused(False)

    def _on_open_logs(self, _icon: Any = None, _item: Any = None) -> None:
        try:
            target = self._cfg.log_dir()
        except Exception:  # noqa: BLE001
            target = app_root() / "logs"
        if not open_in_explorer(target):
            self._notify(f"打不开日志目录：{target}")

    def _on_open_templates(self, _icon: Any = None, _item: Any = None) -> None:
        try:
            target = self._cfg.template_root()
        except Exception:  # noqa: BLE001
            target = app_root() / "templates"
        if not open_in_explorer(target):
            self._notify(f"打不开模板目录：{target}")

    def _on_quit(self, _icon: Any = None, _item: Any = None) -> None:
        log.info("用户选择退出")
        try:
            self.stop(notify=False)
        except Exception:  # noqa: BLE001
            log.debug("退出时停止 Bot 失败", exc_info=True)
        self._refresh_stop.set()
        try:
            if self._icon is not None:
                self._icon.stop()
        except Exception:  # noqa: BLE001
            log.debug("停止托盘图标失败", exc_info=True)

    # ------------------------------------------------------------------ #
    # 控制
    # ------------------------------------------------------------------ #

    def start(self, dry_run: Optional[bool] = None) -> None:
        """在后台线程启动 Bot。"""
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                self._notify("已经在运行了")
                return
            run_dry = self._dry_run if dry_run is None else bool(dry_run)
            self._state = STATE_RUNNING
            self._last_error = ""
            self._stop_requested = False
            self._pause_event.clear()
            self._worker = threading.Thread(
                target=self._worker_main, args=(run_dry,), name="e7bot-bot", daemon=True
            )
            self._worker.start()
        log.info("已在后台线程启动 Bot（dry_run=%s）", dry_run)
        self._sync_icon(force_menu=True)

    def stop(self, notify: bool = True) -> None:
        """请求停止（不阻塞等待线程结束）。"""
        with self._lock:
            bot = self._bot
            self._stop_requested = True
        hotkeys = getattr(bot, "hotkeys", None) if bot is not None else None
        if hotkeys is not None:
            try:
                hotkeys.request_stop()
            except Exception:  # noqa: BLE001
                log.debug("request_stop 失败", exc_info=True)
        # 兜底：万一 hotkeys 还没建好，也把 GUI 侧标志清掉
        self._pause_event.clear()
        if notify:
            log.info("已请求停止")
            self._notify("已请求停止，等待当前 tick 结束")
        self._sync_icon(force_menu=True)

    def set_paused(self, paused: bool) -> None:
        """暂停 / 继续。"""
        with self._lock:
            bot = self._bot
        if not self._is_running():
            self._notify("未在运行")
            return

        hotkeys = getattr(bot, "hotkeys", None) if bot is not None else None
        ok = set_hotkeys_paused(hotkeys, paused)
        if not ok:
            # hotkeys 还没就绪（start() 之前的极短窗口）—— 用内部 Event 维持
            # 界面状态；engine 只有在 hotkeys 存在时才真的会等待。
            if paused:
                self._pause_event.set()
            else:
                self._pause_event.clear()
            log.warning("hotkeys 尚未就绪，暂停状态只反映在界面上")
        log.info("暂停状态 -> %s", paused)
        self._notify("已暂停" if paused else "已继续")
        self._sync_icon(force_menu=True)

    # ------------------------------------------------------------------ #
    # Bot 工作线程
    # ------------------------------------------------------------------ #

    def _worker_main(self, dry_run: bool) -> None:
        from e7bot.engine import Bot, GameNotFound, StopRequested
        from e7bot.tasks import build_tasks

        bot: Any = None
        try:
            # 每次启动都重新读配置：用户改了 default.toml 之后不必重启托盘程序
            cfg = load_config(self._cfg_path)
            self._cfg = cfg

            if self._task_names:
                cfg.data.setdefault("tasks", {})["active"] = list(self._task_names)

            problems = cfg.validate()
            for prob in problems:
                log.warning("配置问题: %s", prob)

            tasks = build_tasks(cfg, self._task_names)
            if not tasks:
                raise RuntimeError(
                    "没有启用任何任务。检查 tasks.active 与各任务的 enabled 字段。"
                )

            # 必须新建 Bot：Bot.stop() 会把热键急停标志永久置位，旧实例无法复用
            bot = Bot(cfg, dry_run=dry_run, profile=self._profile)
            with self._lock:
                self._bot = bot

            bot.start()
            bot.run(tasks, tick_interval=self._tick)
            # engine.Bot.run() 会把主循环里的异常**全部吞掉**（内部记 _stop_reason
            # 并走 alerts），返回时不一定代表"正常结束"。所以这里回头查一下它到底
            # 为什么退出 —— 否则主循环崩了，托盘只会显示一个平静的"已停止"，
            # 用户完全不知道出事了。
            reason = str(getattr(bot, "_stop_reason", "") or "")
            with self._lock:
                user_stopped = self._stop_requested
            if reason.startswith("异常:"):
                self._fail("运行异常", reason)
            elif reason and not user_stopped:
                # 自发的安全停机（画面静止/超时/连续错误）—— engine 自己会告警，
                # 这里再补一条托盘通知，但状态仍是"已停止"而不是"出错"：
                # 这是设计内的保护动作，不是崩溃。
                log.warning("Bot 自行停止: %s", reason)
                self._notify(f"已停止：{reason}"[:200])
            else:
                log.info("Bot 主循环结束（%s）", reason or "正常")

        except GameNotFound as exc:
            self._fail("没找到游戏窗口", str(exc))
        except StopRequested as exc:
            # SafetyViolation 是 StopRequested 的子类，属于"干净停机"
            log.info("已停止: %s", exc)
        except Exception as exc:  # noqa: BLE001
            self._fail(f"{type(exc).__name__}", str(exc))
        finally:
            if bot is not None:
                try:
                    bot.stop()  # Bot.run() 内部已经 stop 过，这里幂等兜底
                except Exception:  # noqa: BLE001
                    log.debug("收尾 stop() 失败", exc_info=True)
            with self._lock:
                self._bot = None
                if self._state != STATE_ERROR:
                    self._state = STATE_STOPPED
            self._sync_icon(force_menu=True)

    def _fail(self, kind: str, detail: str) -> None:
        """把异常暴露到托盘上 —— 不能静默死掉。"""
        log.exception("Bot 异常终止: %s", detail)
        self._last_error = f"{kind}: {detail}"[:120]
        with self._lock:
            self._state = STATE_ERROR
        self._notify(f"出错：{self._last_error}")

    # ------------------------------------------------------------------ #
    # 图标 / 菜单刷新
    # ------------------------------------------------------------------ #

    def _notify(self, message: str, title: str = APP_NAME) -> None:
        """托盘气泡通知；任何失败都不能影响主流程。"""
        try:
            if self._icon is not None:
                self._icon.notify(str(message), str(title))
        except Exception:  # noqa: BLE001
            log.debug("托盘通知失败: %s", message, exc_info=True)

    def _sync_icon(self, force_menu: bool = False) -> None:
        """把当前状态映射到图标颜色 / tooltip / 菜单文本。"""
        icon = self._icon
        if icon is None:
            return
        try:
            state = self._effective_state()
            icon.icon = make_icon_image(icon_color_for(state))
            icon.title = self._tooltip_text()
            text = self._status_text()
            # 菜单在 win32 后端是**一次性构建**的（TrackPopupMenuEx 用的是缓存 HMENU），
            # 动态文本必须显式 update_menu() 才会生效。为减少"菜单正开着时重建
            # HMENU"的窗口，只在文本变化时才调用。
            if force_menu or text != self._last_menu_text:
                self._last_menu_text = text
                icon.update_menu()
        except Exception:  # noqa: BLE001
            log.debug("刷新托盘图标失败", exc_info=True)

    def _start_refresh_thread(self) -> None:
        if self._refresh_thread is not None and self._refresh_thread.is_alive():
            return
        self._refresh_stop.clear()
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop, name="e7bot-gui-refresh", daemon=True
        )
        self._refresh_thread.start()

    def _refresh_loop(self) -> None:
        """每秒刷新一次。

        tick 计数一直在变，菜单文本必须跟着变；用 1Hz 而不是更密，
        是为了缩短"菜单正被显示时重建 HMENU"的时间窗口。
        """
        while not self._refresh_stop.is_set():
            try:
                self._sync_icon()
            except Exception:  # noqa: BLE001
                log.debug("刷新循环异常", exc_info=True)
            self._refresh_stop.wait(1.0)

    # ------------------------------------------------------------------ #
    # 权限告警
    # ------------------------------------------------------------------ #

    def _warn_not_admin(self) -> None:
        """非管理员时的醒目告警。

        为什么值得弹模态框：UIPI 会**静默丢弃**低完整性进程发往高完整性窗口的
        键鼠事件 —— 脚本看起来在跑、日志一切正常，游戏就是不动。
        这个坑不说清楚，用户能查一整天。
        """
        text = (
            "当前不是管理员权限。\n\n"
            "第七史诗官方 PC 端通常以管理员身份运行。受 Windows UIPI 限制，\n"
            "非管理员进程发送的键盘/鼠标事件会被**静默丢弃**：\n"
            "脚本看起来在正常运行，游戏里却毫无反应。\n\n"
            "请关闭本程序，右键 -> 以管理员身份运行（打包版 exe 已内置 UAC 提权请求）。"
        )
        self._notify("警告：当前不是管理员权限，键鼠事件可能被静默丢弃")
        message_box(text, f"{APP_NAME} · 权限警告", MB_OK | MB_ICONWARNING | MB_TOPMOST | MB_SETFOREGROUND)

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #

    def _on_setup(self, icon: Any) -> None:
        """pystray 在消息循环就绪后回调（跑在独立线程里）。

        注意：传了自定义 setup 就必须自己把 ``visible`` 置 True。
        """
        try:
            icon.visible = True
            self._icon = icon
            self._sync_icon(force_menu=True)
            self._start_refresh_thread()
            log.info("托盘已就绪：右键图标打开菜单（日志目录 %s）", self._cfg.log_dir())

            from e7bot.engine import is_admin

            if not is_admin():
                self._warn_not_admin()
        except Exception:  # noqa: BLE001
            log.exception("托盘初始化失败")

    def run(self) -> int:
        """进入托盘消息循环（**必须在主线程调用**）。"""
        if not PYSTRAY_AVAILABLE:
            raise RuntimeError(f"pystray 不可用：{_PYSTRAY_ERROR}")
        self._icon = pystray.Icon(
            APP_NAME,
            make_icon_image(icon_color_for(STATE_IDLE)),
            title=self._tooltip_text(),
            menu=self._build_menu(),
        )
        self._icon.run(setup=self._on_setup)
        return 0

    def shutdown(self) -> None:
        """退出后的收尾（幂等）。"""
        self._refresh_stop.set()
        try:
            self.stop(notify=False)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# 自检（--selftest）
#
# 为什么需要它：`--windowed` 的 exe 没有控制台，一旦打包漏了模块或者路径解析错了，
# 用户看到的只是"双击没反应"（外加一个 PyInstaller 弹窗）。`--selftest` 不碰游戏、
# 不建托盘，把 GUI 依赖的东西全摸一遍并把结果打到 stdout（从命令行启动时可见）
# 和日志文件里；tools/build_exe.py 也用它来自动验证打包产物。
# --------------------------------------------------------------------------- #


def run_selftest(
    cfg_path: Path,
    task_names: Optional[list[str]] = None,
    dialog: bool = False,
) -> int:
    """返回 0 = 全部通过，1 = 有失败项。

    ``dialog=False``（默认）时**只 print + 写日志 + 返回退出码，绝不弹模态框**。
    这一点是硬性的：``tools/build_exe.py`` 会执行
    ``dist/e7bot-gui.exe --selftest`` 并用退出码判断产物是否可用，
    一旦失败路径弹了 MessageBox，构建脚本就会卡到超时，
    报出来的是一个误导性的"超时"而不是真正失败的那一项 —— 恰好是自检最该
    发挥作用的时候。

    ``dialog=True``（对应 ``--selftest-dialog``）是给"打包后双击排查、
    根本看不到 stdout"的人用的显式选项，自动化流程永远不要开它。
    """
    lines: list[str] = ["e7bot-gui 自检", "=" * 68]
    failures = 0

    def check(ok: bool, name: str, detail: str = "") -> None:
        nonlocal failures
        if not ok:
            failures += 1
        tag = "OK " if ok else "!! "
        suffix = f": {detail}" if detail else ""
        lines.append(f"[{tag}] {name}{suffix}")

    def info(name: str, detail: str = "") -> None:
        lines.append(f"[i  ] {name}{(': ' + detail) if detail else ''}")

    # 1) 冻结形态与路径解析 —— 打包后这里必须指向 exe 所在目录
    if is_frozen():
        check(
            app_root() == Path(sys.executable).resolve().parent,
            "打包后应用根 = exe 所在目录",
            str(app_root()),
        )
    else:
        info("冻结运行 (sys.frozen)", "False（源码运行，正常）")
    root = app_root()
    check(root.is_dir(), "应用根目录存在", str(root))
    cfg_file = Path(cfg_path)
    if cfg_file.is_file():
        check(True, "配置文件存在", str(cfg_file))
    else:
        info("配置文件不存在（将使用内置默认配置）", str(cfg_file))

    # 2) 配置载入 —— 这一步会真正执行 `from e7bot.config import Config`，
    #    正是"相对导入在打包后失效"会炸掉的地方
    try:
        cfg = load_config(cfg_file)
        check(True, "配置载入", f"source={cfg.source}")
        check(True, "模板目录", str(cfg.template_root()))
        check(True, "日志目录", str(cfg.log_dir()))
    except Exception as exc:  # noqa: BLE001
        check(False, "配置载入", f"{type(exc).__name__}: {exc}")

    # 3) 托盘依赖
    check(PYSTRAY_AVAILABLE, "pystray 可用", _PYSTRAY_ERROR or "ok")
    check(PIL_AVAILABLE, "Pillow 可用", _PIL_ERROR or "ok")
    try:
        # 用 import_module 而不是 `import pystray._win32`：后者只是为了触发导入，
        # 会被 pyflakes 判成"未使用"，而我们要求 pyflakes 干净。
        importlib.import_module("pystray._win32")

        check(True, "pystray._win32 后端（打包 hiddenimports 的关键项）")
    except Exception as exc:  # noqa: BLE001
        check(False, "pystray._win32 后端", f"{type(exc).__name__}: {exc}")

    # 4) 图标与状态文本
    try:
        img = make_icon_image(icon_color_for(STATE_RUNNING))
        check(img.size == (64, 64), "内存图标生成", f"{img.size} {img.mode}")
        check(
            format_status(STATE_RUNNING, 1, 2) == "运行中 · tick 1 · 点击 2",
            "状态文本格式化",
            format_status(STATE_RUNNING, 1, 2),
        )
    except Exception as exc:  # noqa: BLE001
        check(False, "图标 / 状态文本", f"{type(exc).__name__}: {exc}")

    # 5) 引擎与任务（会把 cv2 / numpy / pywin32 / bettercam 全部拖出来验证一遍）
    try:
        # import_module 而不是 `from .engine import Bot`：只为了触发导入，
        # 用 from-import 会被 pyflakes 判成未使用。
        importlib.import_module(f"{PACKAGE}.engine")
        from e7bot.engine import is_admin

        check(True, "引擎导入 (e7bot.engine)")
        info("管理员权限", str(is_admin()))
    except Exception as exc:  # noqa: BLE001
        check(False, "引擎导入 (e7bot.engine)", f"{type(exc).__name__}: {exc}")

    try:
        from e7bot.tasks import build_tasks

        # 用和真正启动时同一份任务名，这样 `--selftest --tasks 拼错的名字`
        # 能立刻暴露出来，而不是等到点了"开始"才报错。
        tasks = build_tasks(cfg, task_names)  # type: ignore[possibly-undefined]
        check(True, "任务构建 (e7bot.tasks)", f"{len(tasks)} 个: {[t.name for t in tasks]}")
        if not tasks:
            info("当前配置没有启用任何任务（检查 tasks.active / enabled）")
    except Exception as exc:  # noqa: BLE001
        check(False, "任务构建 (e7bot.tasks)", f"{type(exc).__name__}: {exc}")

    try:
        from e7bot.winutil import dpi_mode

        info("DPI 模式", str(dpi_mode()))
    except Exception as exc:  # noqa: BLE001
        check(False, "winutil 导入", f"{type(exc).__name__}: {exc}")

    # 6) 单实例互斥体（用自检专用名字，不干扰真正在跑的实例）
    try:
        probe_name = r"Local\e7bot-gui-selftest-probe"
        m1 = SingleInstance(probe_name)
        m2 = SingleInstance(probe_name)
        first, second = m1.acquire(), m2.acquire()
        m1.release()
        m2.release()
        check(first is True and second is False, "单实例互斥体", f"first={first} second={second}")
    except Exception as exc:  # noqa: BLE001
        check(False, "单实例互斥体", f"{type(exc).__name__}: {exc}")

    lines.append("=" * 68)
    lines.append(f"结果: {'全部通过' if not failures else f'{failures} 项失败'}")
    if failures:
        lines.append("")
        lines.append("完整报告已写入日志文件（logs/e7bot.log）。")
    report = "\n".join(lines)

    # 无控制台时 stdout 是 None，print 会安静地什么都不做（不会抛异常）
    print(report)
    try:
        log.info("自检结果:\n%s", report)
    except Exception:  # noqa: BLE001
        pass

    # ⚠️ 这里**刻意不弹模态对话框**。
    #
    # 曾经有过 `if failures: message_box(...)`，那是个真 bug：
    # `tools/build_exe.py` 用 `subprocess.run([exe, "--selftest"], timeout=300)`
    # 自动验证打包产物，而 MessageBoxW 会阻塞到有人点击 —— 于是**自检一有失败项，
    # 构建脚本就卡满 300 秒**，最后报一个误导性的 TimeoutExpired，而不是真正的
    # 失败原因。这恰好是自检最该起作用的时候。
    #
    # 自检的定位就是"给自动化和命令行用的检查"：结果打到 stdout + 写进日志，
    # 用退出码表达成败。人工排查时从命令行跑就能看到全部内容。
    # 需要弹窗的场景（`--selftest` 之外的失败）由 main() 里的其他分支负责。
    if failures and dialog:
        # 显式 opt-in（--selftest-dialog）：只给"打包后双击排查、完全看不到 stdout"
        # 的人用。自动化流程（tools/build_exe.py）永远不要开 ——
        # MessageBoxW 会阻塞到有人点击，构建脚本会卡到超时。
        message_box(report, f"{APP_NAME} · 自检失败", MB_OK | MB_ICONERROR | MB_TOPMOST)
    return 1 if failures else 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m e7bot.gui",
        description="第七史诗 Steam / PC 端自动化脚本 —— 系统托盘 GUI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python -m e7bot.gui\n"
            "  python -m e7bot.gui --dry-run\n"
            "  python -m e7bot.gui --config config/default.toml --tasks repeat_stage,secret_shop\n"
            "\n"
            "托盘热键沿用引擎配置：F12 急停 / F9 暂停继续（见 safety.fail_safe_key / pause_key）。\n"
        ),
    )
    p.add_argument("--config", "-c", help="配置文件路径（默认 <应用根>/config/default.toml）")
    p.add_argument("--dry-run", action="store_true", help="只识别不点击（强烈建议先跑这个）")
    p.add_argument("--tasks", help="覆盖 tasks.active，逗号分隔，例如 repeat_stage,secret_shop")
    p.add_argument("--profile", help="覆盖模板 profile")
    p.add_argument("--tick", type=float, default=0.35, help="tick 间隔秒（默认 0.35）")
    p.add_argument("--verbose", "-v", action="store_true", help="DEBUG 日志")
    p.add_argument("--allow-multi", action="store_true", help="跳过单实例检查（调试用）")
    p.add_argument(
        "--selftest",
        action="store_true",
        help="自检：不建托盘、不碰游戏，检查依赖/路径/互斥体后退出（打包后排查用）",
    )
    p.add_argument(
        "--selftest-dialog",
        action="store_true",
        help="自检失败时额外弹一个模态框（仅限打包后人工排查；自动化不要用，会一直阻塞）",
    )
    return p


def parse_task_names(value: Optional[str]) -> Optional[list[str]]:
    """把 --tasks 的逗号分隔值解析成列表（空/None -> None，表示用配置里的 tasks.active）。"""
    if not value:
        return None
    names = [t.strip() for t in value.split(",") if t.strip()]
    return names or None


def _guarded_main(args: argparse.Namespace) -> int:
    """main() 的真正主体。单独拆出来是为了让入口处能统一兜住所有异常。

    GUI 程序**绝不能**因为任何异常直接崩掉：打包成 --windowed 后崩溃只有一个
    PyInstaller 弹窗，用户拿不到任何可操作的信息。
    """
    try:
        from e7bot.winutil import enable_dpi_awareness

        enable_dpi_awareness()  # 托盘图标 / 后续截图坐标都依赖它，越早越好
    except Exception as exc:  # noqa: BLE001
        print(f"DPI 感知设置失败（忽略）: {exc}", file=sys.stderr)

    cfg_path = resolve_config_arg(args.config)
    cfg = load_config(cfg_path)
    try:
        setup_logging(cfg, verbose=args.verbose)
    except Exception as exc:  # noqa: BLE001
        print(f"日志初始化失败（忽略）: {exc}", file=sys.stderr)

    # 自检：在建立托盘之前做完就退出，方便自动化验证打包产物
    if args.selftest:
        # 把 --tasks 也带进去：自检要验证的正是"这次启动真正会用到的任务链"，
        # 否则 `--selftest --tasks 打错的名字` 会假装通过。
        return run_selftest(
            cfg_path,
            task_names=parse_task_names(args.tasks),
            dialog=args.selftest_dialog,
        )

    task_names = parse_task_names(args.tasks)

    inst = SingleInstance()
    if not args.allow_multi and not inst.acquire():
        text = (
            "已经有一个 e7bot 托盘程序在运行。\n\n"
            "同时开两个会让两边互相抢输入焦点、互相抢热键，画面会乱掉。\n"
            "请先退出已有的那个（托盘图标 -> 右键 -> 退出）。"
        )
        log.warning("单实例检查失败，已有实例在运行")
        message_box(text, f"{APP_NAME} · 已在运行", MB_OK | MB_ICONWARNING | MB_TOPMOST)
        return 2

    app = TrayApp(
        config_path=cfg_path,
        dry_run=args.dry_run,
        task_names=task_names,
        profile=args.profile,
        tick=args.tick,
    )
    try:
        return app.run()
    finally:
        app.shutdown()
        inst.release()


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    # 依赖检查要在最前面：缺 pystray 时给中文提示，而不是 traceback
    if not PYSTRAY_AVAILABLE or not PIL_AVAILABLE:
        detail = []
        if not PYSTRAY_AVAILABLE:
            detail.append(f"pystray: {_PYSTRAY_ERROR}")
        if not PIL_AVAILABLE:
            detail.append(f"Pillow: {_PIL_ERROR}")
        text = INSTALL_HINT + "\n\n详细信息:\n  " + "\n  ".join(detail)
        print(text, file=sys.stderr)
        message_box(text, f"{APP_NAME} · 缺少依赖", MB_OK | MB_ICONERROR | MB_TOPMOST)
        return 3

    try:
        return _guarded_main(args)
    except Exception as exc:  # noqa: BLE001
        # 兜住一切：GUI 不能因为异常静默死掉，必须把原因暴露给用户
        import traceback

        tb = traceback.format_exc()
        log.exception("托盘 GUI 异常退出: %s", exc)
        try:
            print(tb, file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass
        message_box(
            f"e7bot 托盘 GUI 异常退出：\n\n{type(exc).__name__}: {exc}\n\n"
            f"完整堆栈已写入日志：\n{app_root() / 'logs' / 'e7bot.log'}\n\n"
            f"--- 堆栈（末尾）---\n{tb[-1200:]}",
            f"{APP_NAME} · 异常退出",
            MB_OK | MB_ICONERROR | MB_TOPMOST,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
