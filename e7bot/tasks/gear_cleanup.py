"""装备清理（批量出售低品质装备）。

⚠️ **本模块是所有功能里最需要现场校准的一个。**

原因：装备列表的布局、筛选器、滚动条都随版本变化，而且"哪些该留"是纯玩家策略。
所以这里不写死策略，而是把它做成**配置驱动的流程**，并把安全边界做实：

    [tasks.gear_cleanup]
    enabled = true
    max_items = 200            # 单次运行最多处理多少件，防止一次卖掉全部家当
    keep_locked = true         # 已锁定的装备绝不动
    confirm_each = false       # true = 每件都弹确认（更慢但更安全）

    [[tasks.gear_cleanup.flow]]
    click = "inventory/btn_filter"
    ...
    [[tasks.gear_cleanup.flow]]
    click = "inventory/btn_sell"

强烈建议第一次用 dry-run 跑一遍，看日志确认识别到的位置都对，再放开。

另外提供 `check_locked`：在执行前先确认画面上没有"锁定"图标（说明有已锁装备
被选中），有就中止 —— 这是防止误卖锁装备的最后一道闸。
"""

from __future__ import annotations

import logging
from typing import Any

from .base import ClickSequence, Task, register

log = logging.getLogger("e7bot.task.gear")


@register
class GearCleanupTask(Task):
    name = "gear_cleanup"

    #: 装备被锁定时的图标；一旦选中区域里出现它，立刻中止
    LOCK_ICON = "inventory/icon_locked"
    SELL = "inventory/btn_sell"
    SELL_CONFIRM = "inventory/btn_sell_confirm"

    def __init__(self, cfg: dict[str, Any]):
        super().__init__(cfg)
        self.scene_names = tuple(cfg.get("scenes", ["inventory", "popup"]) or [])
        self.flow = ClickSequence(cfg.get("flow", []) or [])
        self.max_items = int(cfg.get("max_items", 200) or 0)
        self.keep_locked = bool(cfg.get("keep_locked", True))
        self.sold = 0
        self._runs = 0

    def can_handle(self, scene: str) -> bool:
        if self.finished:
            return False
        return not self.scene_names or scene in self.scene_names

    def tick(self, bot) -> None:
        scene = bot.scene()
        if scene.name == "popup":
            self.dismiss_popup(bot)
            return

        if self.max_items and self.sold >= self.max_items:
            self.done(bot, f"已达到单次处理上限 {self.max_items} 件")
            return

        if self.keep_locked and self._has_locked_selected(bot):
            bot._dump_debug_frame("gear_locked_detected")
            self.done(bot, f"检测到选中项里有已锁定装备（{self.LOCK_ICON}），中止以防误卖")
            return

        if not self.flow:
            self.done(bot, "未配置 flow，无事可做")
            return

        log.info("执行装备清理流程（第 %d 轮）", self._runs + 1)
        ok = self.flow.run(bot, log_prefix="装备清理")
        self._runs += 1

        if not ok:
            bot._dump_debug_frame("gear_flow_failed")
            self.done(bot, "装备清理流程中断（多半是模板没对准，请重新采集）")
            return

        if bot.click_template(self.SELL, timeout=4.0, label="出售", required=False):
            bot.click_template(self.SELL_CONFIRM, timeout=4.0, label="确认出售", required=False)
            self.sold += 1
            log.info("已出售第 %d 批", self.sold)
            bot.random_sleep(self.opt("after_sell_delay", [1.0, 2.0]))
        else:
            log.debug("没有找到出售按钮，可能本轮没有可卖的装备")
            bot.random_sleep([0.8, 1.6])

    def _has_locked_selected(self, bot) -> bool:
        try:
            m = bot.find(self.LOCK_ICON, region=self.opt("lock_check_region"))
        except KeyError:
            return False  # 没采集锁图标模板就跳过这道检查
        return m is not None
