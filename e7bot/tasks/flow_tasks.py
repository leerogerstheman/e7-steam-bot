"""更多游戏功能：竞技场 / 圣域 / 派遣 / 免费抽卡 / 每日清单。

这些玩法的 UI 序列我没法预设（每个账号的入口位置、当前活动都不同），
所以**导航步骤全部走配置**（`ClickSequence`）。但**控制逻辑是专用代码**，
不是纯配置能表达的：

* 竞技场：要循环打直到"没有对手了"，中间还要处理战斗内的自动/跳过
* 圣域：要判断"还有东西可收吗"，收完才退出
* 派遣：要一直重发直到没有可重发的
* 免费抽卡：一天只能抽一次，要按天记状态
* 每日清单：要把上面几个按顺序编排，每天跑一遍

这就是"配置驱动 UI + 专用控制逻辑"的分工：UI 会变（改配置），
控制逻辑不会变（改代码）。比把一切都塞进配置更清楚，也比全部硬编码更耐用。

所有模板名都是**约定名**，需要用 `python run.py capture` 从你的客户端采；
`python run.py templates unused` 能列出配置里引用了但还没采的模板。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from .base import ClickSequence, Task, register

log = logging.getLogger("e7bot.task.flows")


# --------------------------------------------------------------------------- #
# 共享基类
# --------------------------------------------------------------------------- #


class FlowTask(Task):
    """带"进入序列 + 循环条件 + 退出序列"的通用玩法任务。

    子类只需定义：默认模板名、以及"还有得做吗"的判断。
    """

    #: 场景（子类可覆盖）
    scenes: tuple[str, ...] = ("lobby", "popup")

    #: 命中任一即"必须人工处理，停机"
    FATAL_POPUPS: tuple[str, ...] = (
        "common/popup_no_stamina",
        "common/popup_inventory_full",
        "common/popup_insufficient_resource",
    )

    def __init__(self, cfg: dict[str, Any]):
        super().__init__(cfg)
        self.entry_flow = ClickSequence(cfg.get("entry_flow", []) or [])
        self.exit_flow = ClickSequence(cfg.get("exit_flow", []) or [])
        self.step_flow = ClickSequence(cfg.get("step_flow", []) or [])
        self.interval = float(cfg.get("interval_minutes", 0) or 0) * 60.0
        self.max_runs = int(cfg.get("max_runs", 0) or 0)
        self.scene_names = tuple(cfg.get("scenes", list(self.scenes)) or [])
        self.runs = 0
        self._next_at = 0.0
        self._entered = False
        self._step_started = 0.0

    # -- 调度 -------------------------------------------------------------- #

    def can_handle(self, scene: str) -> bool:
        """调度规则：**进入前**只在入口场景被调度，**进入后**接管一切。

        为什么进入后要接管所有场景：这些玩法的内部界面（竞技场大厅、派遣列表……）
        在没有专门模板时识别不出名字，会落到 `unknown`。如果继续要求
        `scene in scene_names`，任务一旦进去就再也拿不到 tick，直接僵住。
        进去之后该怎么走由任务自己的流程决定，不需要场景帮忙判断。
        """
        if self.finished:
            return False
        if time.time() < self._next_at:
            return False
        if self._entered:
            return True
        return not self.scene_names or scene in self.scene_names

    def _postpone(self, seconds: Optional[float] = None) -> None:
        self._next_at = time.time() + (self.interval if seconds is None else seconds)

    # -- 通用处理 ---------------------------------------------------------- #

    def _handle_fatal(self, bot) -> bool:
        """遇到必须人工处理的弹窗就停机，别空转。"""
        for tpl in self.FATAL_POPUPS:
            try:
                m = bot.find(tpl, threshold=self.opt("popup_threshold", 0.88))
            except KeyError:
                continue
            if m is not None:
                bot._dump_debug_frame(f"fatal_{tpl.replace('/', '_')}")
                self.done(bot, f"遇到需要人工处理的弹窗: {tpl}")
                return True
        return False

    def _enter(self, bot) -> bool:
        if not self.entry_flow:
            log.warning("%s 未配置 entry_flow，无法进入该玩法界面", self.name)
            self._postpone(300)
            return False
        log.info("%s: 执行进入序列（%d 步）", self.name, len(self.entry_flow))
        if not self.entry_flow.run(bot, log_prefix=f"{self.name} 进入"):
            bot._dump_debug_frame(f"{self.name}_enter_failed")
            log.warning("%s 进入失败，稍后重试", self.name)
            self._postpone(max(self.interval, 180.0))
            return False
        self._entered = True
        return True

    def _leave(self, bot) -> None:
        if self.exit_flow:
            self.exit_flow.run(bot, log_prefix=f"{self.name} 退出")
        self._entered = False

    def _click_if_present(self, bot, template: str, label: str = "",
                          threshold: Optional[float] = None) -> bool:
        """模板存在就点。模板缺失视为"不存在"而非错误（允许渐进式补模板）。"""
        try:
            m = bot.find(template, threshold=threshold)
        except KeyError:
            log.debug("模板 %s 不存在，跳过「%s」", template, label or template)
            return False
        if m is None:
            return False
        bot.click_match(m, label=label or template)
        return True

    def _exists(self, bot, template: str, threshold: Optional[float] = None) -> bool:
        try:
            return bot.find(template, threshold=threshold) is not None
        except KeyError:
            return False

    def _finish_round(self, bot, reason: str = "") -> None:
        self.runs += 1
        if self.max_runs and self.runs >= self.max_runs:
            self._leave(bot)
            self.done(bot, reason or f"已完成 {self.runs} 轮")
        else:
            self._postpone()


# --------------------------------------------------------------------------- #
# 竞技场
# --------------------------------------------------------------------------- #


@register
class ArenaTask(FlowTask):
    """竞技场：循环挑战直到没有可打的对手。

    流程：进入 → 领奖励 → 找对手 → 开战 → 战斗内开自动/跳过 → 结算确认 → 重复
    退出条件：出现 `arena/no_opponent`（没有对手/没有旗子）、达到 max_runs、
              或出现致命弹窗。
    """

    name = "arena"

    #: 约定模板名
    NO_OPPONENT = "arena/no_opponent"
    FIGHT = "arena/btn_fight"
    AUTOPLAY = "arena/btn_autoplay"
    SKIP = "arena/btn_skip"
    RESULT_CONFIRM = "arena/btn_result_confirm"
    CLAIM = "arena/btn_claim_reward"

    def tick(self, bot) -> None:
        if self._handle_fatal(bot):
            return

        scene = bot.scene()
        if scene.name == "popup":
            if not self.dismiss_popup(bot):
                bot.random_sleep([0.8, 1.6])
            return

        if not self._entered:
            if not self._enter(bot):
                return
            bot.random_sleep([0.8, 1.6])
            return

        # 没有对手了 -> 收工
        if self._exists(bot, self.NO_OPPONENT):
            log.info("竞技场：没有可挑战的对手了")
            self._leave(bot)
            self.done(bot, "没有可挑战的对手")
            return

        # 领奖励（一次性）
        if self._click_if_present(bot, self.CLAIM, "领取竞技场奖励"):
            bot.random_sleep([0.6, 1.2])
            return

        # 战斗内：开自动 / 跳过
        if bot.scene().name == "battle" or self._exists(bot, self.AUTOPLAY):
            did = self._click_if_present(bot, self.AUTOPLAY, "竞技场开启自动")
            did |= self._click_if_present(bot, self.SKIP, "跳过战斗动画")
            if did:
                bot.random_sleep([1.0, 2.0])
                return
            bot.random_sleep([1.5, 3.0])
            return

        # 结算确认
        if self._click_if_present(bot, self.RESULT_CONFIRM, "竞技场结算确认"):
            self._finish_round(bot)
            bot.random_sleep([1.0, 2.0])
            return

        # 找对手开战
        if self._click_if_present(bot, self.FIGHT, "挑战对手"):
            bot.random_sleep(self.opt("after_fight_click", [1.5, 3.0]))
            return

        # 配置了中间步骤（比如刷新对手列表）就走它
        if self.step_flow:
            self.step_flow.run(bot, log_prefix=f"{self.name} 步骤")
            self._finish_round(bot)
            return

        log.debug("竞技场：界面不认识，等待中 …")
        self._step_started = self._step_started or time.time()
        if time.time() - self._step_started > float(self.opt("step_timeout", 45.0)):
            bot._dump_debug_frame("arena_step_timeout")
            self.done(bot, "竞技场界面超过 45s 没有可执行动作（模板需重新采集）")
            return
        bot.random_sleep([1.0, 2.0])

    def on_finish(self, bot) -> None:
        log.info("arena 统计：完成 %d 轮", self.runs)


# --------------------------------------------------------------------------- #
# 圣域
# --------------------------------------------------------------------------- #


@register
class SanctuaryTask(FlowTask):
    """圣域：一键收取，收完（按钮消失）就退出。"""

    name = "sanctuary"

    COLLECT_ALL = "sanctuary/btn_collect_all"
    FOREST = "sanctuary/btn_forest"
    SUMMON_CREATURE = "sanctuary/btn_summon_creature"

    def tick(self, bot) -> None:
        if self._handle_fatal(bot):
            return

        scene = bot.scene()
        if scene.name == "popup":
            if not self.dismiss_popup(bot):
                bot.random_sleep([0.8, 1.6])
            return

        if not self._entered:
            if not self._enter(bot):
                return
            bot.random_sleep([0.8, 1.6])
            return

        if self._click_if_present(bot, self.COLLECT_ALL, "圣域一键收取"):
            bot.random_sleep(self.opt("after_collect", [1.0, 2.0]))
            self.runs += 1
            return

        # 可选：森林生物召唤
        if self.opt("summon_creature", False) and self._click_if_present(
            bot, self.FOREST, "进入森林"
        ):
            bot.random_sleep([0.8, 1.6])
            self._click_if_present(bot, self.SUMMON_CREATURE, "召唤森林生物")
            bot.random_sleep([1.0, 2.0])
            return

        log.info("圣域：没有可收取的内容了")
        self._leave(bot)
        self.done(bot, "收取完成")

    def on_finish(self, bot) -> None:
        log.info("sanctuary 统计：收取 %d 次", self.runs)


# --------------------------------------------------------------------------- #
# 派遣
# --------------------------------------------------------------------------- #


@register
class DispatchTask(FlowTask):
    """派遣：把所有已完成的派遣重新发出去。"""

    name = "dispatch"

    RESEND = "dispatch/btn_resend"
    CONFIRM = "dispatch/btn_resend_confirm"
    COMPLETED = "dispatch/mark_completed"

    def tick(self, bot) -> None:
        if self._handle_fatal(bot):
            return

        scene = bot.scene()
        if scene.name == "popup":
            if not self.dismiss_popup(bot):
                bot.random_sleep([0.8, 1.6])
            return

        if not self._entered:
            if not self._enter(bot):
                return
            bot.random_sleep([1.0, 2.0])
            return

        # 优先处理已完成的
        if self._exists(bot, self.COMPLETED) or self._exists(bot, self.RESEND):
            if self._click_if_present(bot, self.COMPLETED, "选择已完成的派遣"):
                bot.random_sleep([0.5, 1.0])
                return
            if self._click_if_present(bot, self.RESEND, "重新派遣"):
                bot.random_sleep([0.5, 1.0])
                self._click_if_present(bot, self.CONFIRM, "确认重新派遣")
                self.runs += 1
                log.info("已重发第 %d 个派遣", self.runs)
                bot.random_sleep(self.opt("after_resend", [1.2, 2.2]))
                if self.max_runs and self.runs >= self.max_runs:
                    self._leave(bot)
                    self.done(bot, f"已重发 {self.runs} 个派遣")
                return

        log.info("派遣：没有需要重发的了")
        self._leave(bot)
        self.done(bot, "派遣处理完成")

    def on_finish(self, bot) -> None:
        log.info("dispatch 统计：重发 %d 个", self.runs)


# --------------------------------------------------------------------------- #
# 免费抽卡
# --------------------------------------------------------------------------- #


@register
class FreeSummonTask(FlowTask):
    """免费抽卡：每天一次。按天记状态，同一天不会重复进。"""

    name = "summon"

    FREE = "summon/btn_free"
    CONFIRM = "summon/btn_confirm"
    CONFIRM2 = "summon/btn_confirm_2"
    NO_FREE = "summon/no_free"

    def __init__(self, cfg: dict[str, Any]):
        super().__init__(cfg)
        self._last_day = ""

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d")

    def can_handle(self, scene: str) -> bool:
        if not super().can_handle(scene):
            return False
        if self.opt("once_per_day", True) and self._last_day == self._today():
            return False
        return True

    def tick(self, bot) -> None:
        if self._handle_fatal(bot):
            return

        scene = bot.scene()
        if scene.name == "popup":
            if not self.dismiss_popup(bot):
                bot.random_sleep([0.8, 1.6])
            return

        if not self._entered:
            if not self._enter(bot):
                return
            bot.random_sleep([1.0, 2.0])
            return

        if self._exists(bot, self.NO_FREE):
            log.info("免费抽卡：本次免费次数已用完")
            self._last_day = self._today()
            self._leave(bot)
            self.done(bot, "免费次数已用完")
            return

        if self._click_if_present(bot, self.FREE, "免费召唤"):
            bot.random_sleep([0.8, 1.6])
            self._click_if_present(bot, self.CONFIRM, "确认召唤")
            bot.random_sleep([0.8, 1.6])
            self._click_if_present(bot, self.CONFIRM2, "再次确认")
            self.runs += 1
            self._last_day = self._today()
            log.info("免费抽卡完成")
            bot.random_sleep(self.opt("after_summon", [1.5, 2.5]))
            self._leave(bot)
            self.done(bot, "已抽完免费召唤")
            return

        log.debug("免费抽卡：界面不认识，等待中 …")
        bot.random_sleep([1.0, 2.0])


# --------------------------------------------------------------------------- #
# 每日清单编排
# --------------------------------------------------------------------------- #


@register
class DailyRoutineTask(FlowTask):
    """每日清单：把若干个子流程按顺序跑一遍，每天一次。

    配置：

        [tasks.daily]
        type = "daily"
        enabled = true
        interval_minutes = 60        # 每小时检查一次今天是否已经跑过
        [[tasks.daily.routines]]
        name = "邮件"
        flow = [ {click="lobby/btn_mail", wait_for="mail/btn_claim_all"}, ... ]
        [[tasks.daily.routines]]
        name = "声望"
        flow = [ ... ]

    这个任务只负责**编排**（按天去重、按顺序执行、失败不阻塞后续），
    具体的 UI 步骤仍然是配置。
    """

    name = "daily"

    def __init__(self, cfg: dict[str, Any]):
        super().__init__(cfg)
        self.routines: list[tuple[str, ClickSequence]] = []
        for i, r in enumerate(cfg.get("routines", []) or []):
            if not isinstance(r, dict) or not r.get("flow"):
                continue
            label = str(r.get("name", f"routine_{i + 1}"))
            self.routines.append((label, ClickSequence(r["flow"])))
        self._done_day = ""
        self._idx = 0

    def can_handle(self, scene: str) -> bool:
        if self.finished:
            return False
        if self.opt("once_per_day", True) and self._done_day == time.strftime("%Y-%m-%d"):
            return False
        if self._entered:
            return True
        if self.scene_names and scene not in self.scene_names:
            return False
        return time.time() >= self._next_at

    def tick(self, bot) -> None:
        if self._handle_fatal(bot):
            return

        scene = bot.scene()
        if scene.name == "popup":
            if not self.dismiss_popup(bot):
                bot.random_sleep([0.8, 1.6])
            return

        if not self.routines:
            self.done(bot, "未配置 routines")
            return

        if self._idx >= len(self.routines):
            self._done_day = time.strftime("%Y-%m-%d")
            self.done(bot, f"今日清单已完成（{len(self.routines)} 项）")
            return

        label, flow = self.routines[self._idx]
        log.info("每日清单 %d/%d：%s", self._idx + 1, len(self.routines), label)
        ok = flow.run(bot, log_prefix=f"每日-{label}")
        self._idx += 1

        if not ok:
            # 单项失败不阻塞后续 —— 一个活动改版不该让整个每日清单停摆
            bot._dump_debug_frame(f"daily_{self._idx}_failed")
            log.warning("每日清单「%s」失败，继续下一项", label)

        self._postpone(3.0)


__all__ = ["FlowTask", "ArenaTask", "SanctuaryTask", "DispatchTask",
           "FreeSummonTask", "DailyRoutineTask"]
