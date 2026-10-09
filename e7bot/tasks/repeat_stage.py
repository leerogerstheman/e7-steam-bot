"""自动战斗 / 重复刷本 —— 本项目的主力功能。

对标安卓端脚本（如 AutoFarming）的"重复关卡"，但改成**场景驱动**而非盲点：

    battle         -> 确保 AUTO 与倍速已开，然后等
    battle_result  -> 点「再次挑战」
    battle_ready   -> 点「开始战斗」
    popup          -> 关掉弹窗（体力不足 / 背包满 / 公告 / 断线重连）
    lobby          -> 游戏内重复次数用完了，按配置的导航序列重新进本

这样即使中间插入任何弹窗、或者加载慢了几十秒，脚本也不会错位 —— 这是 PC 端
无人值守最关键的区别。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from ..vision import Match
from .base import ClickSequence, Task, register

log = logging.getLogger("e7bot.task.battle")


@register
class RepeatStageTask(Task):
    name = "repeat_stage"
    scenes = ("lobby", "battle", "battle_ready", "battle_result", "popup")

    #: AUTO 关闭状态的模板（看到它说明要开 AUTO）；开着的状态用于确认
    AUTO_OFF = "battle/btn_auto_off"
    AUTO_ON = "battle/btn_auto_on"
    #: 倍速 x1 / x2
    SPEED_1X = "battle/btn_speed_x1"
    SPEED_2X = "battle/btn_speed_x2"

    RETRY = "battle/btn_retry"
    CONFIRM_RESULT = "battle/btn_confirm_result"
    START_BATTLE = "battle/btn_start_battle"

    def __init__(self, cfg: dict[str, Any]):
        super().__init__(cfg)
        self.runs = 0
        self.max_runs = int(cfg.get("max_runs", 0) or 0)
        self.use_auto = bool(cfg.get("use_auto_battle", True))
        self.use_speed = bool(cfg.get("use_fast_speed", True))
        self.use_repeat = bool(cfg.get("use_repeat_battle", True))

        self.enter_sequence = ClickSequence(cfg.get("enter_sequence", []) or [])
        self._armed = False            # 本次战斗是否已经开过 AUTO/倍速
        self._battle_entered_at: Optional[float] = None
        self._last_lobby_action = 0.0

    # ------------------------------------------------------------------ #

    def tick(self, bot) -> None:
        scene = bot.scene()

        if scene.name == "popup":
            self._handle_popup(bot, scene)
        elif scene.name == "battle":
            self._handle_battle(bot, scene)
        elif scene.name == "battle_result":
            self._handle_result(bot, scene)
        elif scene.name == "battle_ready":
            self._handle_ready(bot, scene)
        elif scene.name == "lobby":
            self._handle_lobby(bot, scene)

    # ------------------------------------------------------------------ #

    def _handle_popup(self, bot, scene) -> None:
        # 优先识别"体力不足"这类必须停机的弹窗，避免无意义空转
        for fatal in ("common/popup_no_stamina", "common/popup_inventory_full"):
            if bot.exists(fatal, threshold=self.opt("popup_threshold", 0.88)):
                bot._dump_debug_frame(f"fatal_{fatal.replace('/', '_')}")
                self.done(bot, f"遇到需要人工处理的弹窗: {fatal}")
                return

        if not self.dismiss_popup(bot):
            log.debug("popup 场景但没有可识别的关闭按钮，等待中 …")
            bot.random_sleep([1.0, 2.0])

    def _handle_battle(self, bot, scene) -> None:
        if self._battle_entered_at is None:
            self._battle_entered_at = time.time()
            self._armed = False

        timeout = float(self.opt("battle_timeout", 600.0))
        elapsed = time.time() - self._battle_entered_at
        if timeout > 0 and elapsed > timeout:
            bot._dump_debug_frame("battle_timeout")
            self.done(bot, f"单场战斗超过 {timeout:.0f}s 未结束（卡住了？）")
            return

        if not self._armed:
            armed_any = False
            if self.use_auto and self._click_if_present(bot, self.AUTO_OFF, "开启 AUTO"):
                armed_any = True
            if self.use_speed and self._click_if_present(bot, self.SPEED_1X, "开启 2 倍速"):
                armed_any = True
            # 两个都点过了（或本来就开着）就标记完成，避免每 tick 重复点
            self._armed = True
            if armed_any:
                bot.random_sleep([0.3, 0.8])
            return

        # 战斗中什么都不做，只做低频的"是否已进结算"探测由场景识别负责
        bot.random_sleep([1.5, 3.0])

    def _handle_result(self, bot, scene) -> None:
        self._battle_entered_at = None
        self.runs += 1
        log.info("第 %d 场战斗结束%s", self.runs,
                 f"（上限 {self.max_runs}）" if self.max_runs else "")
        try:
            bot.recorder.event("battle_finished", count=1, runs=self.runs, task=self.name)
        except Exception:
            pass

        if self.max_runs and self.runs >= self.max_runs:
            self.done(bot, f"已完成 {self.runs} 场")
            return

        if self.use_repeat and self._click_if_present(bot, self.RETRY, "再次挑战"):
            bot.random_sleep(self.opt("after_battle_delay", [1.0, 2.5]))
            return

        if self._click_if_present(bot, self.CONFIRM_RESULT, "结算确认"):
            bot.random_sleep(self.opt("after_battle_delay", [1.0, 2.5]))
            return

        log.debug("结算界面没有找到可点按钮，等待中 …")
        bot.random_sleep([1.0, 2.0])

    def _handle_ready(self, bot, scene) -> None:
        if self._click_if_present(bot, self.START_BATTLE, "开始战斗"):
            self._armed = False
            self._battle_entered_at = None
            bot.random_sleep([1.5, 3.0])
        else:
            bot.random_sleep([0.8, 1.6])

    def _handle_lobby(self, bot, scene) -> None:
        """回到大厅说明游戏内重复次数用完了，按配置序列重新进本。"""
        if not self.enter_sequence:
            self.done(
                bot,
                "已回到大厅，且未配置 enter_sequence —— 请在 config 里配置进本导航序列，"
                "或手动进本后重新运行",
            )
            return

        # 避免在大厅高频重试
        if time.time() - self._last_lobby_action < 5.0:
            bot.random_sleep([1.0, 2.0])
            return
        self._last_lobby_action = time.time()

        log.info("在大厅，执行进本导航序列（%d 步）", len(self.enter_sequence))
        ok = self.enter_sequence.run(bot, log_prefix="进本")
        if ok:
            self._armed = False
            self._battle_entered_at = None
            bot.random_sleep([1.0, 2.0])
        else:
            bot._dump_debug_frame("enter_sequence_failed")
            self.done(bot, "进本导航序列执行失败（模板需重新采集/校准）")

    # ------------------------------------------------------------------ #

    def _click_if_present(self, bot, template: str, label: str) -> bool:
        """模板存在就点它。模板缺失时静默跳过（不当作错误）。"""
        try:
            m: Optional[Match] = bot.find(template)
        except KeyError:
            log.debug("模板 %s 不存在，跳过「%s」", template, label)
            return False
        if m is None:
            return False
        bot.click_match(m, label=label)
        return True

    def on_finish(self, bot) -> None:
        log.info("repeat_stage 统计：完成 %d 场战斗", self.runs)
