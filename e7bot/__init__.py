"""e7bot —— 第七史诗（Epic Seven）Steam / PC 端自动化脚本。

设计前提（详见 README 的「安全边界」一节）：

* Steam 版带**内核级反作弊 UNCHEATER**（Wellbia，与 XIGNCODE3 同厂），
  因此本项目**只做两件事**：读屏幕像素、发合成键鼠事件。
  不读游戏内存、不注入、不 hook、不改游戏文件、不碰网络协议。
* 引擎是 Smilegate 自研的 **YUNA Engine（C++/DX11）**，
  所以没有 Unity/UE 插件可用，UI Automation 也不可用，
  只能走 **DXGI 截图 + OpenCV 模板匹配 + SendInput**。
* 官方 PC 端通常以管理员权限运行，受 UIPI 限制，
  本脚本**也必须以管理员身份运行**，否则键鼠事件会被静默丢弃。

典型用法：

    from e7bot import Config, Bot, build_tasks

    cfg = Config.load("config/default.toml")
    bot = Bot(cfg, dry_run=True)      # 先 dry-run 验证模板
    bot.start()
    bot.run(build_tasks(cfg))
"""

from .config import Config
from .engine import Bot, StopRequested, SafetyViolation, is_admin, keep_display_awake
from .humaninput import Humanizer, Keyboard, Mouse
from .scene import SceneDetector, SceneResult
from .tasks import build_tasks
from .vision import Match, Matcher, Template, TemplateLibrary
from .winutil import (
    GameNotFound,
    Rect,
    WindowInfo,
    enable_dpi_awareness,
    find_window,
)

__version__ = "0.1.0"

__all__ = [
    "Config",
    "Bot",
    "StopRequested",
    "SafetyViolation",
    "GameNotFound",
    "is_admin",
    "keep_display_awake",
    "Humanizer",
    "Mouse",
    "Keyboard",
    "SceneDetector",
    "SceneResult",
    "build_tasks",
    "Match",
    "Matcher",
    "Template",
    "TemplateLibrary",
    "Rect",
    "WindowInfo",
    "enable_dpi_awareness",
    "find_window",
    "__version__",
]
