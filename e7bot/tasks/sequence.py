"""通用序列任务：把任意一段 UI 操作写进配置，不用改代码。

用途举例：
* 定时领邮件 / 领签到奖励
* 圣域收菜、派遣
* 活动图一次性通关
* 每日任务清单

配置：

    [tasks.daily_chores]
    enabled  = true
    scenes   = ["lobby", "popup"]      # 只在这些场景里动手
    interval_minutes = 30              # 每隔多久跑一次（0 = 每个 tick 都试）
    repeat   = 0                       # 总共跑几次（0 = 无限）
    on_finish_stop_all = false         # 跑完是否结束整个脚本

    [[tasks.daily_chores.flow]]
    click = "lobby/btn_mail"
    wait_for = "mail/btn_claim_all"

    [[tasks.daily_chores.flow]]
    click = "mail/btn_claim_all"
    wait_for = "common/btn_ok"
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .base import ClickSequence, Task, register

log = logging.getLogger("e7bot.task.sequence")


@register
class SequenceTask(Task):
    name = "sequence"

    def __init__(self, cfg: dict[str, Any]):
        super().__init__(cfg)
        self.scene_names = tuple(cfg.get("scenes", ["lobby", "popup"]) or [])
        self.flow = ClickSequence(cfg.get("flow", []) or [])
        self.interval = float(cfg.get("interval_minutes", 0) or 0) * 60.0
        self.repeat = int(cfg.get("repeat", 0) or 0)
        self.stop_all_when_done = bool(cfg.get("on_finish_stop_all", False))
        self.runs = 0
        self._next_at = 0.0

    def can_handle(self, scene: str) -> bool:
        if self.finished:
            return False
        if self.scene_names and scene not in self.scene_names:
            return False
        return time.time() >= self._next_at

    def tick(self, bot) -> None:
        if not self.flow:
            self.done(bot, "未配置 flow，无事可做")
            return

        scene = bot.scene()
        if scene.name == "popup":
            self.dismiss_popup(bot)
            return

        log.info("执行序列任务 %s（第 %d 次）", self.name, self.runs + 1)
        ok = self.flow.run(bot, log_prefix=self.name)
        self.runs += 1
        self._next_at = time.time() + self.interval if self.interval else time.time()

        if not ok:
            bot._dump_debug_frame(f"{self.name}_failed")
            log.warning("序列任务 %s 执行失败，稍后重试", self.name)
            self._next_at = time.time() + max(self.interval, 120.0)
            return

        if self.repeat and self.runs >= self.repeat:
            self.done(bot, f"已执行 {self.runs} 次")
            if self.stop_all_when_done:
                from ..engine import StopRequested

                raise StopRequested(f"{self.name} 完成，按要求结束整个脚本")
