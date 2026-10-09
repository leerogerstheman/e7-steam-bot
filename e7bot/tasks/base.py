"""任务基类与注册表。

任务 = 「在某个场景下该做什么」的一个小状态机。引擎每个 tick 只让一个任务动作，
避免两个任务同时抢同一个界面（这是安卓端脚本最常见的翻车原因）。

任务接口：
    can_handle(scene) -> bool     当前场景是否轮到我
    tick(bot)                    做一步动作
    finished                     我是否已经干完（引擎据此收尾）
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

log = logging.getLogger("e7bot.task")

REGISTRY: dict[str, type] = {}


def register(cls: type) -> type:
    REGISTRY[cls.name] = cls  # type: ignore[attr-defined]
    return cls


class Task:
    name: str = "task"
    #: 该任务关心的场景；空 = 关心所有场景
    scenes: tuple[str, ...] = ()

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.finished = False
        self.started_at = time.time()
        self.actions = 0

    # -- 生命周期 ---------------------------------------------------------- #

    def can_handle(self, scene: str) -> bool:
        if self.finished:
            return False
        return not self.scenes or scene in self.scenes

    def tick(self, bot) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError

    def on_finish(self, bot) -> None:
        pass

    # -- 辅助 -------------------------------------------------------------- #

    def opt(self, key: str, default: Any = None) -> Any:
        return self.cfg.get(key, default)

    def done(self, bot, reason: str = "") -> None:
        self.finished = True
        log.info("任务 %s 完成%s", self.name, f"（{reason}）" if reason else "")
        self.on_finish(bot)

    #: 需要在场景出现时被"清掉"的通用弹窗模板（按顺序尝试）
    COMMON_POPUPS: tuple[str, ...] = (
        "common/btn_ok",
        "common/btn_confirm",
        "common/btn_close",
        "common/btn_cancel",
        "common/btn_notice_close",
        "common/btn_reconnect",
    )

    def dismiss_popup(self, bot, max_clicks: int = 3) -> bool:
        """尝试关掉当前弹窗；关掉了返回 True。

        弹窗处理放在所有任务最前面：公告、体力不足、背包已满、断线重连……
        安卓端脚本最常见的死法就是被一个弹窗卡住，而 PC 端因为要长时间无人值守，
        这一环更不能省。
        """
        for _ in range(max_clicks):
            for tpl in self.COMMON_POPUPS:
                m = bot.find(tpl, threshold=self.opt("popup_threshold", 0.88))
                if m is not None:
                    bot.click_match(m, label=f"关闭弹窗 {tpl}")
                    bot.random_sleep(self.opt("after_popup_delay", [0.4, 1.0]))
                    return True
        return False


# --------------------------------------------------------------------------- #
# 配置驱动的点击序列
# --------------------------------------------------------------------------- #


class ClickSequence:
    """配置驱动的 UI 导航序列。

    这是本项目的关键设计之一：**把"怎么从大厅走到某个关卡"变成配置而不是代码**。
    因为每个玩家的关卡位置不同、活动 UI 会变，硬编码坐标必然很快失效。

    config 里这样写：

        [[tasks.repeat_stage.enter_sequence]]
        click   = "lobby/btn_adventure"     # 要点的模板
        wait_for = "adventure/btn_stage_list"  # 点完后等这个出现（可省）
        timeout = 20                        # 等待上限（可省）
        delay   = [0.8, 1.6]                # 点完后的随机停顿（可省）
    """

    def __init__(self, steps: Iterable[dict[str, Any]], default_timeout: float = 20.0):
        self.steps = [s for s in steps if isinstance(s, dict) and s.get("click")]
        self.default_timeout = default_timeout

    def __len__(self) -> int:
        return len(self.steps)

    def __bool__(self) -> bool:
        return bool(self.steps)

    def run(self, bot, log_prefix: str = "导航") -> bool:
        for i, step in enumerate(self.steps, 1):
            tpl = str(step["click"])
            timeout = float(step.get("timeout", self.default_timeout))
            log.info("%s %d/%d: 点击 %s", log_prefix, i, len(self.steps), tpl)
            try:
                ok = bot.click_template(
                    tpl,
                    timeout=timeout,
                    region=tuple(step["region"]) if step.get("region") else None,
                    threshold=step.get("threshold"),
                    label=f"{log_prefix} {tpl}",
                    required=bool(step.get("required", True)),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("%s 第 %d 步失败: %s", log_prefix, i, exc)
                return False
            if not ok:
                return False

            delay = step.get("delay")
            if delay:
                bot.random_sleep(delay)
            else:
                bot.random_sleep([0.6, 1.4])

            wait_for = step.get("wait_for")
            if wait_for:
                names = [wait_for] if isinstance(wait_for, str) else list(wait_for)
                m = bot.wait_any(names, timeout=timeout)
                if m is None:
                    log.warning("%s 第 %d 步：等待 %s 超时", log_prefix, i, names)
                    return False
        return True
