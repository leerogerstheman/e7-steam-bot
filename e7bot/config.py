"""配置系统。

用 TOML（Python 3.11+ 内置 tomllib，零依赖、支持注释），结构：

    [window] / [capture] / [input] / [safety] / [templates]   —— 全局
    [tasks.<名字>]                                            —— 各功能模块参数
    [scenes.<名字>]                                           —— 场景识别的锚点模板

所有"位置"一律是**归一化坐标(0..1)**，相对于游戏窗口客户区。
换分辨率 / 换窗口大小都不用改配置，这是 PC 端相对安卓端最重要的适配。
"""

from __future__ import annotations

import copy
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .humaninput import Humanizer

DEFAULTS: dict[str, Any] = {
    "window": {
        # Steam 版窗口标题；韩/中/日/英都列上，用正则匹配
        "title_patterns": [r"Epic\s*Seven", r"第七史诗", r"에픽세븐", r"エピックセブン"],
        "exe_patterns": [r"EpicSeven", r"Epic Seven"],
        "min_size": [800, 450],
        "auto_activate": True,
        "activate_settle": 0.35,
        # 游戏窗口不在前台时是否暂停（SendInput 只作用于前台窗口）
        "pause_when_not_foreground": True,
    },
    "capture": {
        "backends": ["bettercam", "mss", "printwindow"],
        "target_fps": 60,
        "monitor_index": 0,
    },
    "input": {
        "speed": 1.0,
        "move_jitter_px": 2.5,
        "click_hold_ms": [45, 110],
        "pre_click_delay": [60, 180],
        "post_click_delay": [90, 260],
        "key_hold_ms": [35, 80],
        "curve_deviation": 0.18,
        "tremor": 0.6,
    },
    "safety": {
        "fail_safe_key": "f12",     # 急停
        "pause_key": "f9",          # 暂停/继续
        "max_runtime_minutes": 480,
        "unknown_scene_timeout": 90.0,   # 连续多久认不出场景就停
        "frozen_timeout": 180.0,         # 画面完全静止多久判定卡死
        "max_consecutive_errors": 8,
        "dry_run": False,           # True = 只识别不点击，用来验证模板
    },
    "stats": {
        "enabled": True,
        "keep_days": 90,
    },
    "alerts": {
        "enabled": True,
        "min_interval_seconds": 60,
        "include_screenshot": True,
        "sound": {"enabled": True, "repeat": 3},
        "messagebox": {"enabled": False},
        "ntfy": {
            "enabled": False,
            "server": "https://ntfy.sh",
            "topic": "",
            "token": "",
            "priority": "high",
        },
        "webhook": {"enabled": False, "url": ""},
    },
    "ocr": {
        "enabled": False,
        "backend": "digits",
        "digits_prefix": "ocr/digits",
        "threshold": 0.80,
        "scale_tolerance": 0.06,
    },
    "templates": {
        "root": "templates",
        "profile": "default",
        "default_threshold": 0.86,
        "scale_tolerance": 0.06,    # 单尺度失败后的多尺度兜底范围
    },
    "logging": {
        "level": "INFO",
        "dir": "logs",
        "save_debug_frames": False,
        "debug_frame_interval": 30.0,
    },
    "tasks": {
        "active": ["repeat_stage"],
        "repeat_stage": {
            "enabled": True,
            "max_runs": 0,             # 0 = 无限
            "use_auto_battle": True,
            "use_fast_speed": True,
            "use_repeat_battle": True, # 用游戏内"再次挑战"，减少 UI 往返
            "lobby_enter_timeout": 60.0,
            "battle_timeout": 600.0,
            "result_timeout": 60.0,
            "after_battle_delay": [1.0, 2.5],
        },
        "claim_rewards": {
            "enabled": False,
            "interval_minutes": 30,
        },
        "secret_shop": {
            "enabled": False,
            "max_refreshes": 100,
            "buy_names": [],           # 留空 = 只刷新不购买
            "refresh_cost_gold": 3,
            "gold_budget": 0,          # 按"刷新次数 × 单次花费"估算的上限
            "gold_region": None,       # 画面上金币数字所在的归一化区域
            "min_gold": 0,             # 金币低于此值就停（需先启用 OCR）
        },
        "gear_cleanup": {
            "enabled": False,
            "max_items": 200,
            "sell_rarity_below": 0,    # 0 = 关闭按稀有度过滤
            "keep_locked": True,
        },
        "arena": {
            "enabled": False,
            "interval_minutes": 0,
            "max_runs": 0,
            "entry_flow": [],
            "step_flow": [],
            "exit_flow": [],
            "popup_threshold": 0.88,
            "step_timeout": 45.0,
        },
        "sanctuary": {
            "enabled": False,
            "interval_minutes": 0,
            "entry_flow": [],
            "exit_flow": [],
            "summon_creature": False,
        },
        "dispatch": {
            "enabled": False,
            "interval_minutes": 0,
            "max_runs": 0,
            "entry_flow": [],
            "exit_flow": [],
        },
        "summon": {
            "enabled": False,
            "interval_minutes": 60,
            "once_per_day": True,
            "entry_flow": [],
            "exit_flow": [],
        },
        "daily": {
            "enabled": False,
            "interval_minutes": 60,
            "once_per_day": True,
            "routines": [],
        },
    },
    "scenes": {
        "lobby": {
            "any": ["lobby/btn_adventure", "lobby/btn_hero", "lobby/btn_sanctuary"],
            "none": [],
        },
        "battle": {
            "any": ["battle/btn_auto", "battle/btn_speed", "battle/icon_pause"],
            "none": ["battle/btn_retry"],
        },
        "battle_result": {
            "any": ["battle/btn_retry", "battle/btn_confirm_result"],
            "none": [],
        },
        "battle_ready": {
            "any": ["battle/btn_start_battle"],
            "none": [],
        },
        "popup": {
            "any": ["common/btn_ok", "common/btn_confirm", "common/btn_close"],
            "none": [],
        },
        "loading": {
            "any": ["common/icon_loading"],
            "none": [],
        },
    },
}


# --------------------------------------------------------------------------- #
# 深度合并
# --------------------------------------------------------------------------- #


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


@dataclass
class Config:
    data: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULTS))
    source: Optional[Path] = None

    # -- 载入 -------------------------------------------------------------- #

    @classmethod
    def load(cls, path: Optional[str | Path] = None) -> "Config":
        if path is None:
            return cls()
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"配置文件不存在: {p}")
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
        return cls(data=_deep_merge(DEFAULTS, raw), source=p)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(dump_toml(self.data), encoding="utf-8")

    # -- 访问 -------------------------------------------------------------- #

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def section(self, name: str) -> dict[str, Any]:
        v = self.data.get(name, {})
        return v if isinstance(v, dict) else {}

    def task(self, name: str) -> dict[str, Any]:
        v = self.section("tasks").get(name, {})
        return v if isinstance(v, dict) else {}

    def active_tasks(self) -> list[str]:
        return list(self.get("tasks.active", []) or [])

    # -- 派生对象 ---------------------------------------------------------- #

    def humanizer(self) -> Humanizer:
        s = self.section("input")
        return Humanizer(
            speed=float(s.get("speed", 1.0)),
            move_jitter_px=float(s.get("move_jitter_px", 2.5)),
            click_hold_ms=tuple(s.get("click_hold_ms", (45, 110))),        # type: ignore[arg-type]
            pre_click_delay=tuple(s.get("pre_click_delay", (60, 180))),    # type: ignore[arg-type]
            post_click_delay=tuple(s.get("post_click_delay", (90, 260))),  # type: ignore[arg-type]
            key_hold_ms=tuple(s.get("key_hold_ms", (35, 80))),            # type: ignore[arg-type]
            curve_deviation=float(s.get("curve_deviation", 0.18)),
            tremor=float(s.get("tremor", 0.6)),
        )

    def template_root(self) -> Path:
        return self.resolve_path(str(self.get("templates.root", "templates")))

    def log_dir(self) -> Path:
        return self.resolve_path(str(self.get("logging.dir", "logs")))

    def resolve_path(self, value: str) -> Path:
        """把配置里的相对路径解析成绝对路径。

        相对路径的基准是**配置文件所在目录**。但本项目（以及绝大多数项目）
        把配置放在 `config/` 子目录里，而 `templates/` `logs/` 在项目根 ——
        如果只按配置文件目录解析，就会变成 `config/templates`，这显然不是用户
        的本意。所以这里做一步明确的回退：

            1. 配置文件目录下存在 -> 用它
            2. 否则若配置文件在 config/ 之类的子目录里 -> 用它的上一级（项目根）
            3. 都不存在 -> 按项目根理解（采集器要能新建这个目录）
        """
        p = Path(value)
        if p.is_absolute():
            return p

        base = self.source.parent if self.source else Path.cwd()
        candidate = base / p
        if candidate.exists():
            return candidate.resolve()

        if base.name.lower() in ("config", "configs", ".config"):
            return (base.parent / p).resolve()
        return candidate.resolve()

    def validate(self) -> list[str]:
        """返回配置问题列表（空 = 没问题）。"""
        problems: list[str] = []

        for key in ("fail_safe_key", "pause_key"):
            v = self.get(f"safety.{key}")
            try:
                from .humaninput import vk_of

                vk_of(str(v))
            except Exception:
                problems.append(f"safety.{key} 不是合法按键名: {v!r}")

        if not self.get("window.title_patterns") and not self.get("window.exe_patterns"):
            problems.append("window.title_patterns 与 exe_patterns 不能同时为空")

        speed = self.get("input.speed", 1.0)
        if not (0.1 <= float(speed) <= 20):
            problems.append(f"input.speed 应在 0.1~20 之间，当前 {speed}")

        thr = self.get("templates.default_threshold", 0.86)
        if not (0.3 <= float(thr) <= 0.999):
            problems.append(f"templates.default_threshold 应在 0.3~0.999，当前 {thr}")

        scenes = self.section("scenes")
        for name, spec in scenes.items():
            if not isinstance(spec, dict):
                problems.append(f"scenes.{name} 必须是表")
                continue
            if not spec.get("any"):
                problems.append(f"scenes.{name}.any 不能为空（至少一个锚点模板）")

        active = self.active_tasks()
        known = set(self.section("tasks").keys()) - {"active"}
        for t in active:
            if t not in known:
                problems.append(f"tasks.active 引用了不存在的任务: {t!r}")

        return problems


# --------------------------------------------------------------------------- #
# 极简 TOML 写出（只支持本配置用到的类型，避免引入依赖）
# --------------------------------------------------------------------------- #


def _fmt_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_fmt_value(x) for x in v) + "]"
    raise TypeError(f"不支持的 TOML 值: {v!r}")


def dump_toml(data: dict[str, Any], prefix: str = "") -> str:
    """把配置字典写成 TOML。

    **值为 None 的键会被跳过** —— TOML 根本没有 null 类型（这是规范决定的，
    不是实现偷懒）。跳过是安全的：读回来时该键缺失，`Config.get` 会回退到
    DEFAULTS 里的默认值，而默认值同样是 None，语义完全一致。
    """
    lines: list[str] = []
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict) and v is not None}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}

    for k, v in scalars.items():
        lines.append(f"{k} = {_fmt_value(v)}")
    if scalars and tables:
        lines.append("")

    for k, v in tables.items():
        name = f"{prefix}{k}"
        lines.append(f"[{name}]")
        lines.append(dump_toml(v, prefix=f"{name}.").rstrip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
