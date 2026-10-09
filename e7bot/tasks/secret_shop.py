"""秘密商店自动刷新 / 自动购买。

对标 Solunium / sya1999 的 Epic-Seven-E7-Secret-Shop-Refresh（那个项目同时支持
模拟器 ADB 和 PC 端鼠标控制，是本项目在 PC 侧最重要的参考对象）。

流程（每一步都是"找模板再点"，找不到就等，绝不盲点）：

    secret_shop 场景
      ├─ 在商品列表区域找 buy_names 里的目标（默认：誓约书签 / 神秘书签）
      │    ├─ 找到 -> 点它 -> 等购买确认弹窗 -> 点确认
      │    └─ 没有 -> 点刷新 -> 等刷新确认弹窗 -> 点确认 -> 计数 +1
      └─ 达到 max_refreshes 或预算上限 -> 结束

关键安全点（照搬参考项目的告诫）：
**先手动刷到目标出现、确认脚本能识别出来，再让它无人值守运行。**
所以在 dry-run 模式下会打印识别结果但不点击。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from .base import Task, register

log = logging.getLogger("e7bot.task.shop")


@register
class SecretShopTask(Task):
    name = "secret_shop"
    scenes = ("secret_shop", "popup")

    #: 刷新按钮及其确认弹窗
    REFRESH = "shop/btn_refresh"
    REFRESH_CONFIRM = "shop/btn_refresh_confirm"
    #: 购买按钮及其确认弹窗
    BUY = "shop/btn_buy"
    BUY_CONFIRM = "shop/btn_buy_confirm"
    #: 商品列表区域（归一化），默认取屏幕中部大半区域
    LIST_REGION = (0.08, 0.18, 0.84, 0.62)

    def __init__(self, cfg: dict[str, Any]):
        super().__init__(cfg)
        self.refreshes = 0
        self.purchases = 0
        self.max_refreshes = int(cfg.get("max_refreshes", 0) or 0)
        self.max_purchases = int(cfg.get("max_purchases", 0) or 0)
        self.buy_names = list(cfg.get("buy_names", []) or [])
        self.list_region = tuple(cfg.get("list_region", self.LIST_REGION))  # type: ignore[arg-type]
        self.refresh_cost = int(cfg.get("refresh_cost_gold", 3))
        self.budget = int(cfg.get("gold_budget", 0) or 0)  # 0 = 不限
        self._last_action = 0.0

        # 用 OCR 读真实金币数（可选）。gold_budget 只能按"刷新次数 × 单次花费"
        # 估算，一旦游戏里涨价或买了别的东西就会算错；直接读画面上的金币最准。
        self.gold_region = tuple(cfg["gold_region"]) if cfg.get("gold_region") else None  # type: ignore[arg-type]
        self.min_gold = int(cfg.get("min_gold", 0) or 0)
        self._last_gold: Optional[int] = None

    # ------------------------------------------------------------------ #

    def tick(self, bot) -> None:
        scene = bot.scene()

        if scene.name == "popup":
            if not self.dismiss_popup(bot):
                bot.random_sleep([0.8, 1.6])
            return

        if self.max_refreshes and self.refreshes >= self.max_refreshes:
            self.done(bot, f"已刷新 {self.refreshes} 次（达到上限）")
            return

        if self.max_purchases and self.purchases >= self.max_purchases:
            self.done(bot, f"已购买 {self.purchases} 件（达到上限）")
            return

        if self.budget and self.refreshes * self.refresh_cost >= self.budget:
            self.done(bot, f"刷新花费已达金币预算 {self.budget}")
            return

        if not self._check_gold(bot):
            return

        # 一次 tick 只做一件事，动作之间留出 UI 动画时间
        if time.time() - self._last_action < 0.9:
            bot.random_sleep([0.3, 0.7])
            return

        if self._try_buy(bot):
            return

        self._do_refresh(bot)

    # ------------------------------------------------------------------ #

    def _check_gold(self, bot) -> bool:
        """用 OCR 读金币；不够就停机。返回 False 表示已经停机。

        读数失败（没启用 OCR / 字形没采全 / 数字被遮挡）时**放行** ——
        预算控制是"锦上添花"，不该因为读不出数字就拒绝工作。
        """
        if self.min_gold <= 0 or not self.gold_region:
            return True

        gold = bot.read_number(self.gold_region)
        if gold is None:
            if self._last_gold is None:
                log.debug("读不到金币数（OCR 未启用或字形不全），跳过金币下限检查")
            return True

        self._last_gold = gold
        log.debug("当前金币: %d（下限 %d）", gold, self.min_gold)
        if gold < self.min_gold:
            self.done(bot, f"金币 {gold} 低于下限 {self.min_gold}，停止刷新")
            return False
        return True

    def _try_buy(self, bot) -> bool:
        """在商品列表里找目标物品；找到就买。"""
        if not self.buy_names:
            return False

        for name in self.buy_names:
            try:
                m = bot.find(name, region=self.list_region, threshold=self.opt("item_threshold", 0.88))
            except KeyError:
                log.warning("购买目标模板缺失: %s（请用采集工具补上）", name)
                continue
            if m is None:
                continue

            log.info("发现目标商品 %s (score=%.3f)，执行购买", name, m.score)
            self._last_action = time.time()
            bot.click_match(m, label=f"选择商品 {name}")
            bot.random_sleep(self.opt("after_select_delay", [0.6, 1.2]))

            if bot.click_template(self.BUY, timeout=4.0, label="购买", required=False):
                bot.random_sleep(self.opt("after_buy_delay", [0.5, 1.0]))
                bot.click_template(self.BUY_CONFIRM, timeout=4.0, label="确认购买", required=False)
                self.purchases += 1
                log.info("已购买第 %d 件", self.purchases)
                try:
                    bot.recorder.event("purchase", count=1, item=name,
                                       purchases=self.purchases, task=self.name)
                except Exception:
                    pass
                bot.random_sleep(self.opt("after_purchase_delay", [1.2, 2.2]))
            else:
                log.debug("没有找到购买按钮，可能只是选中了商品")
            return True
        return False

    def _do_refresh(self, bot) -> None:
        self._last_action = time.time()
        try:
            m = bot.find(self.REFRESH)
        except KeyError:
            self.done(bot, f"缺少刷新按钮模板 {self.REFRESH}，请先采集")
            return
        if m is None:
            log.debug("没找到刷新按钮，等待中 …")
            bot.random_sleep([0.8, 1.6])
            return

        bot.click_match(m, label="刷新商店")
        bot.random_sleep(self.opt("after_refresh_click_delay", [0.5, 1.0]))
        bot.click_template(self.REFRESH_CONFIRM, timeout=4.0, label="确认刷新", required=False)
        self.refreshes += 1
        log.info("已刷新第 %d 次%s", self.refreshes,
                 f"（上限 {self.max_refreshes}）" if self.max_refreshes else "")
        try:
            bot.recorder.event("refresh", count=1, refreshes=self.refreshes, task=self.name)
        except Exception:
            pass
        bot.random_sleep(self.opt("after_refresh_delay", [1.0, 1.8]))

    def on_finish(self, bot) -> None:
        log.info("secret_shop 统计：刷新 %d 次，购买 %d 件%s",
                 self.refreshes, self.purchases,
                 f"，最后读到金币 {self._last_gold}" if self._last_gold is not None else "")
