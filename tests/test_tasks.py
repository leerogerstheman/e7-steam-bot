"""任务模块测试：状态流转、停止条件、弹窗处理。

这是**此前完全没有自动化验证**的部分 —— 之前只能靠人肉推演
"战斗→结算→重试→大厅→重新进本"这条链路对不对。

窗口/截图/输入都被 StubBot 替换，`bot.current` 手动切换场景，
所以每个测试都是确定性的、毫秒级。
"""

from __future__ import annotations

import time

import pytest

from e7bot.tasks import build_tasks
from e7bot.tasks.flow_tasks import ArenaTask, DailyRoutineTask, FreeSummonTask
from e7bot.tasks.gear_cleanup import GearCleanupTask
from e7bot.tasks.repeat_stage import RepeatStageTask
from e7bot.tasks.secret_shop import SecretShopTask
from e7bot.tasks.sequence import SequenceTask
from tests.conftest import StubBot, install_templates, make_bot, swap_scene
from tools.selftest import BATTLE_LAYOUT, LOBBY_LAYOUT, render

#: 额外元素：录制生成的进本序列 + 商店 + 各玩法按钮
#:
#: ⚠️ 刻意**不含** common/popup_no_stamina 之类的"致命弹窗"模板 ——
#: 一旦它在画面里，所有 flow 任务的第一件事就是检测到致命弹窗然后停机，
#: 于是所有玩法测试都会莫名其妙地"立即结束"（这个坑踩过一次）。
#: 致命弹窗只在需要它的那一个测试里单独加。
EXTRA_LAYOUT = {
    "seq/step_01": (0.30, 0.20, 0.10, 0.07),
    "seq/step_02": (0.58, 0.20, 0.10, 0.07),
    "seq/step_03": (0.30, 0.34, 0.10, 0.07),
    "shop/item_covenant_bookmark": (0.30, 0.40, 0.10, 0.08),
    "shop/item_mystic_medal": (0.30, 0.52, 0.10, 0.08),
    "shop/btn_buy": (0.62, 0.44, 0.10, 0.06),
    "shop/btn_refresh": (0.20, 0.90, 0.10, 0.06),
    "shop/btn_refresh_confirm": (0.46, 0.62, 0.10, 0.06),
    "shop/btn_buy_confirm": (0.62, 0.72, 0.10, 0.06),
    "arena/btn_fight": (0.62, 0.30, 0.10, 0.07),
    "summon/btn_free": (0.28, 0.72, 0.12, 0.07),
    "summon/no_free": (0.72, 0.30, 0.12, 0.07),
    "dispatch/btn_resend": (0.30, 0.72, 0.12, 0.07),
    "common/btn_ok": (0.44, 0.80, 0.12, 0.07),
}

#: 基础布局。刻意**不含** `arena/no_opponent` 和 `common/popup_no_stamina` 这类
#: "终止条件"模板 —— 它们在画面里会让对应任务立刻收工，导致别的测试测不到东西。
#: 需要它们的测试各自用下面专门定义的布局。
ALL_LAYOUT = {**LOBBY_LAYOUT, **BATTLE_LAYOUT, **EXTRA_LAYOUT}

#: 竞技场"没有对手了"
NO_OPPONENT_LAYOUT = {**ALL_LAYOUT, "arena/no_opponent": (0.30, 0.62, 0.12, 0.07)}

#: 带"体力不足"致命弹窗
FATAL_LAYOUT = {**ALL_LAYOUT, "common/popup_no_stamina": (0.62, 0.60, 0.14, 0.07)}


@pytest.fixture()
def bot(cfg) -> StubBot:
    return make_bot(cfg, ALL_LAYOUT, current="lobby")


# --------------------------------------------------------------------------- #
# repeat_stage：主力功能的状态流转
# --------------------------------------------------------------------------- #


def test_battle_arms_auto_and_speed(bot: StubBot) -> None:
    task = RepeatStageTask({"use_auto_battle": True, "use_fast_speed": True})
    bot.current = "battle"
    task.tick(bot)

    assert bot.clicked("battle/btn_auto_off"), "AUTO 关着就该点开"
    assert bot.clicked("battle/btn_speed_x1"), "1 倍速就该点成 2 倍"
    assert task._armed is True


def test_battle_does_not_reclick_after_armed(bot: StubBot) -> None:
    """第二 tick 不该再点 AUTO/倍速 —— 否则会把已经开着的又切回去。"""
    task = RepeatStageTask({})
    bot.current = "battle"
    task.tick(bot)
    bot.reset()

    task.tick(bot)
    assert bot.clicked_names() == []


def test_battle_timeout_finishes_task(bot: StubBot) -> None:
    task = RepeatStageTask({"battle_timeout": 10})
    bot.current = "battle"
    task.tick(bot)                     # 进入战斗，开始计时
    task._battle_entered_at = time.time() - 11
    task.tick(bot)

    assert task.finished
    assert "battle_timeout" in bot.debug_frames


def test_result_clicks_retry_and_counts_run(bot: StubBot) -> None:
    task = RepeatStageTask({"use_repeat_battle": True})
    bot.current = "battle_result"
    task.tick(bot)

    assert bot.clicked("battle/btn_retry")
    assert task.runs == 1


def test_result_falls_back_to_confirm_when_no_retry(cfg) -> None:
    """没有「再次挑战」时应该退而点结算确认。"""
    layout = {k: v for k, v in ALL_LAYOUT.items() if k != "battle/btn_retry"}
    layout["battle/btn_confirm_result"] = (0.42, 0.72, 0.16, 0.09)
    frame = render(layout, 1920, 1080)
    install_templates(cfg, frame, layout)
    bot = StubBot(cfg, frame)

    task = RepeatStageTask({"use_repeat_battle": True})
    bot.current = "battle_result"
    task.tick(bot)
    assert bot.clicked("battle/btn_confirm_result")
    assert task.runs == 1


def test_max_runs_finishes(bot: StubBot) -> None:
    task = RepeatStageTask({"max_runs": 2})
    bot.current = "battle_result"

    task.tick(bot)
    assert not task.finished
    task.tick(bot)
    assert task.finished
    assert task.runs == 2


def test_lobby_without_enter_sequence_finishes_with_hint(bot: StubBot) -> None:
    task = RepeatStageTask({})
    bot.current = "lobby"
    task.tick(bot)

    assert task.finished
    assert not bot.clicked_names(), "没配置导航就不该乱点"


def test_lobby_runs_enter_sequence(bot: StubBot) -> None:
    task = RepeatStageTask({
        "enter_sequence": [
            {"click": "seq/step_01"},
            {"click": "seq/step_02"},
            {"click": "seq/step_03"},
        ]
    })
    bot.current = "lobby"
    task.tick(bot)

    assert bot.clicked_names() == ["seq/step_01", "seq/step_02", "seq/step_03"]
    assert not task.finished


def test_lobby_enter_sequence_failure_stops(bot: StubBot) -> None:
    """导航中途找不到模板 -> 停机并存调试图，绝不盲点下去。"""
    task = RepeatStageTask({
        "enter_sequence": [{"click": "seq/step_01"}, {"click": "does/not/exist"}]
    })
    bot.current = "lobby"
    task.tick(bot)

    assert task.finished
    assert "enter_sequence_failed" in bot.debug_frames


def test_lobby_throttles_retries(bot: StubBot) -> None:
    """大厅里不该高频重试导航序列。"""
    task = RepeatStageTask({"enter_sequence": [{"click": "seq/step_01"}]})
    bot.current = "lobby"
    task.tick(bot)
    bot.reset()
    task.tick(bot)                     # 5 秒内第二次
    assert bot.clicked_names() == []


def test_fatal_popup_stops_task(cfg) -> None:
    """体力不足必须停机等人工处理，不能空转。"""
    bot = make_bot(cfg, FATAL_LAYOUT, current="popup")
    task = RepeatStageTask({})
    task.tick(bot)

    assert task.finished
    assert "fatal_common_popup_no_stamina" in bot.debug_frames


def test_battle_ready_clicks_start(bot: StubBot) -> None:
    swap_scene(bot, {**ALL_LAYOUT, "battle/btn_start_battle": (0.30, 0.50, 0.12, 0.07)})

    task = RepeatStageTask({})
    bot.current = "battle_ready"
    task.tick(bot)
    assert bot.clicked("battle/btn_start_battle")


# --------------------------------------------------------------------------- #
# 弹窗清理
# --------------------------------------------------------------------------- #


def test_dismiss_popup_clicks_ok(bot: StubBot) -> None:
    task = RepeatStageTask({})
    assert task.dismiss_popup(bot) is True
    assert bot.clicked("common/btn_ok")


def test_dismiss_popup_returns_false_when_none(bot: StubBot) -> None:
    task = RepeatStageTask({})
    bot.grabber = type(bot.grabber)(render({}, 1920, 1080))
    bot.invalidate_frame()
    assert task.dismiss_popup(bot) is False


# --------------------------------------------------------------------------- #
# secret_shop
# --------------------------------------------------------------------------- #


def test_shop_buys_target_item(bot: StubBot) -> None:
    task = SecretShopTask({"buy_names": ["shop/item_covenant_bookmark"]})
    task._last_action = 0.0
    bot.current = "secret_shop"
    task.tick(bot)

    assert bot.clicked("shop/item_covenant_bookmark"), "应该选中目标商品"
    assert bot.clicked("shop/btn_buy")
    assert bot.clicked("shop/btn_buy_confirm")
    assert task.purchases == 1
    assert task.refreshes == 0, "找到目标就不该刷新"


def test_shop_refreshes_when_no_target(bot: StubBot) -> None:
    task = SecretShopTask({"buy_names": ["shop/item_does_not_exist"]})
    task._last_action = 0.0
    bot.current = "secret_shop"
    task.tick(bot)

    assert bot.clicked("shop/btn_refresh")
    assert bot.clicked("shop/btn_refresh_confirm")
    assert task.refreshes == 1


def test_shop_refreshes_when_no_buy_list(bot: StubBot) -> None:
    task = SecretShopTask({"buy_names": []})
    task._last_action = 0.0
    bot.current = "secret_shop"
    task.tick(bot)
    assert task.refreshes == 1


def test_shop_stops_at_max_refreshes(bot: StubBot) -> None:
    task = SecretShopTask({"max_refreshes": 1, "buy_names": []})
    task._last_action = 0.0
    bot.current = "secret_shop"
    task.tick(bot)
    assert task.refreshes == 1
    assert not task.finished

    task._last_action = 0.0
    task.tick(bot)
    assert task.finished
    assert task.refreshes == 1


def test_shop_respects_gold_budget(bot: StubBot) -> None:
    task = SecretShopTask({"refresh_cost_gold": 3, "gold_budget": 3, "buy_names": []})
    task.refreshes = 1                 # 已经花了 3 金
    task._last_action = 0.0
    bot.current = "secret_shop"
    task.tick(bot)
    assert task.finished
    assert task.refreshes == 1, "达到预算后不该再刷新"


def test_shop_missing_refresh_template_stops(bot: StubBot) -> None:
    task = SecretShopTask({"buy_names": []})
    task.REFRESH = "shop/no_such_button"
    task._last_action = 0.0
    bot.current = "secret_shop"
    task.tick(bot)
    assert task.finished


# --------------------------------------------------------------------------- #
# 商店 + OCR 金币预算
# --------------------------------------------------------------------------- #


GOLD_REGION = (0.80, 0.02, 0.10, 0.04)


def test_shop_stops_when_gold_below_min(bot: StubBot) -> None:
    """OCR 读出金币低于下限 -> 停机，不要再刷新。"""
    task = SecretShopTask({"buy_names": [], "gold_region": GOLD_REGION, "min_gold": 1000})
    task._last_action = 0.0
    bot.current = "secret_shop"
    bot.ocr_number = 250

    task.tick(bot)

    assert task.finished
    assert bot.ocr_reads == [GOLD_REGION]
    assert not bot.clicked("shop/btn_refresh"), "金币不够就不该刷新"


def test_shop_continues_when_gold_sufficient(bot: StubBot) -> None:
    task = SecretShopTask({"buy_names": [], "gold_region": GOLD_REGION, "min_gold": 1000})
    task._last_action = 0.0
    bot.current = "secret_shop"
    bot.ocr_number = 5000

    task.tick(bot)

    assert not task.finished
    assert task._last_gold == 5000
    assert bot.clicked("shop/btn_refresh")


def test_shop_continues_when_ocr_cannot_read(bot: StubBot) -> None:
    """读不出金币（字形没采全 / OCR 没启用）时必须**放行**。

    预算控制是锦上添花，不该因为读不出数字就拒绝工作。
    """
    task = SecretShopTask({"buy_names": [], "gold_region": GOLD_REGION, "min_gold": 1000})
    task._last_action = 0.0
    bot.current = "secret_shop"
    bot.ocr_number = None

    task.tick(bot)

    assert not task.finished
    assert bot.clicked("shop/btn_refresh"), "读不出金币不该阻塞刷新"


def test_shop_skips_ocr_when_not_configured(bot: StubBot) -> None:
    """没配 gold_region 就完全不该调用 OCR（省掉每 tick 的开销）。"""
    task = SecretShopTask({"buy_names": [], "min_gold": 1000})
    task._last_action = 0.0
    bot.current = "secret_shop"
    bot.ocr_number = 1

    task.tick(bot)

    assert bot.ocr_reads == []
    assert bot.clicked("shop/btn_refresh")


def test_shop_skips_ocr_when_min_gold_zero(bot: StubBot) -> None:
    task = SecretShopTask({"buy_names": [], "gold_region": GOLD_REGION, "min_gold": 0})
    task._last_action = 0.0
    bot.current = "secret_shop"
    bot.ocr_number = 1

    task.tick(bot)

    assert bot.ocr_reads == []
    assert bot.clicked("shop/btn_refresh")


# --------------------------------------------------------------------------- #
# sequence（配置驱动通用任务）
# --------------------------------------------------------------------------- #


def test_sequence_runs_flow_and_respects_repeat(bot: StubBot) -> None:
    task = SequenceTask({
        "flow": [{"click": "seq/step_01"}, {"click": "seq/step_02"}],
        "repeat": 1,
        "scenes": ["lobby"],
    })
    bot.current = "lobby"
    task.tick(bot)
    assert bot.clicked_names() == ["seq/step_01", "seq/step_02"]
    assert task.runs == 1
    assert task.finished


def test_sequence_without_flow_finishes(bot: StubBot) -> None:
    task = SequenceTask({})
    task.tick(bot)
    assert task.finished


def test_sequence_interval_blocks_early_rerun(bot: StubBot) -> None:
    task = SequenceTask({
        "flow": [{"click": "seq/step_01"}],
        "interval_minutes": 30,
        "scenes": ["lobby"],
    })
    bot.current = "lobby"
    assert task.can_handle("lobby") is True
    task.tick(bot)
    assert task.can_handle("lobby") is False, "间隔未到不该再跑"


def test_sequence_scene_filter(bot: StubBot) -> None:
    task = SequenceTask({"flow": [{"click": "seq/step_01"}], "scenes": ["lobby"]})
    assert task.can_handle("lobby") is True
    assert task.can_handle("battle") is False


# --------------------------------------------------------------------------- #
# flow_tasks：进入前受场景限制，进入后接管一切
# --------------------------------------------------------------------------- #


def test_flow_task_can_handle_before_and_after_entry() -> None:
    """**这是回归测试。**

    早期版本进入玩法界面后仍要求 `scene in scene_names`，而玩法内部界面在没有
    专门模板时会被识别成 unknown —— 任务一旦进去就再也拿不到 tick，直接僵住。
    """
    task = ArenaTask({"scenes": ["lobby", "popup"]})
    assert task.can_handle("lobby") is True
    assert task.can_handle("battle") is False

    task._entered = True
    assert task.can_handle("battle") is True
    assert task.can_handle("unknown") is True


def test_arena_finishes_when_no_opponent(cfg) -> None:
    bot = make_bot(cfg, NO_OPPONENT_LAYOUT, current="lobby")

    task = ArenaTask({"entry_flow": [{"click": "seq/step_01"}]})
    task.tick(bot)                       # 执行进入序列
    assert task._entered is True
    assert bot.clicked("seq/step_01")

    bot.reset()
    task.tick(bot)                       # 看到 arena/no_opponent
    assert task.finished
    assert not bot.clicked_names(), "没有对手了就不该再点任何东西"


def test_arena_clicks_fight_when_available(bot: StubBot) -> None:
    """在竞技场大厅（非战斗场景）应该点"挑战对手"。

    注意不能用 "battle" —— 那个场景会走战斗内分支（开自动/跳过）然后 return，
    根本到不了"找对手"这一步。
    """
    task = ArenaTask({})
    task._entered = True
    bot.current = "unknown"
    task.tick(bot)
    assert bot.clicked("arena/btn_fight")


def test_arena_uses_autoplay_inside_battle(bot: StubBot) -> None:
    swap_scene(bot, {**ALL_LAYOUT,
                     "arena/btn_autoplay": (0.62, 0.20, 0.12, 0.07),
                     "arena/btn_skip": (0.30, 0.62, 0.12, 0.07)})

    task = ArenaTask({})
    task._entered = True
    bot.current = "battle"
    task.tick(bot)
    assert bot.clicked("arena/btn_autoplay")
    assert bot.clicked("arena/btn_skip")
    assert not bot.clicked("arena/btn_fight"), "战斗中不该去点挑战对手"


def test_arena_max_runs_stops(bot: StubBot) -> None:
    swap_scene(bot, {**ALL_LAYOUT, "arena/btn_result_confirm": (0.62, 0.52, 0.12, 0.07)})

    task = ArenaTask({"max_runs": 1})
    task._entered = True
    task.runs = 1
    bot.current = "battle_result"
    task.tick(bot)                       # 结算确认 -> _finish_round -> 达到 max_runs
    assert task.finished


def test_flow_task_fatal_popup_stops(cfg) -> None:
    bot = make_bot(cfg, FATAL_LAYOUT, current="popup")
    task = ArenaTask({})
    task.tick(bot)
    assert task.finished
    assert "fatal_common_popup_no_stamina" in bot.debug_frames


def test_flow_task_dismisses_ordinary_popup(bot: StubBot) -> None:
    """普通弹窗（确定/关闭）应该被关掉然后继续，而不是停机。"""
    task = ArenaTask({})
    bot.current = "popup"
    task.tick(bot)
    assert not task.finished
    assert bot.clicked("common/btn_ok")


def test_flow_task_missing_entry_flow_postpones(bot: StubBot) -> None:
    task = ArenaTask({})
    bot.current = "lobby"
    task.tick(bot)
    assert not task.finished
    assert task._next_at > time.time(), "没配置 entry_flow 应该推迟而不是崩"


def test_summon_once_per_day(bot: StubBot) -> None:
    task = FreeSummonTask({"once_per_day": True})
    task._entered = True
    bot.current = "lobby"

    task.tick(bot)                       # 看到 summon/no_free
    assert task.finished
    assert task._last_day == time.strftime("%Y-%m-%d")


def test_summon_does_not_rerun_same_day(bot: StubBot) -> None:
    task = FreeSummonTask({"once_per_day": True, "scenes": ["lobby"]})
    task._last_day = time.strftime("%Y-%m-%d")
    assert task.can_handle("lobby") is False


def test_daily_runs_routines_in_order(bot: StubBot) -> None:
    task = DailyRoutineTask({
        "scenes": ["lobby"],
        "routines": [
            {"name": "邮件", "flow": [{"click": "seq/step_01"}]},
            {"name": "声望", "flow": [{"click": "seq/step_02"}]},
        ],
    })
    bot.current = "lobby"

    task.tick(bot)
    assert bot.clicked_names() == ["seq/step_01"]
    task._next_at = 0.0                  # 跳过 3 秒间隔
    task.tick(bot)
    assert bot.clicked_names() == ["seq/step_01", "seq/step_02"]
    task._next_at = 0.0
    task.tick(bot)
    assert task.finished


def test_daily_failure_does_not_block_later_routines(bot: StubBot) -> None:
    """一个活动改版不该让整个每日清单停摆。"""
    task = DailyRoutineTask({
        "scenes": ["lobby"],
        "routines": [
            {"name": "坏的", "flow": [{"click": "does/not/exist"}]},
            {"name": "好的", "flow": [{"click": "seq/step_01"}]},
        ],
    })
    bot.current = "lobby"

    task.tick(bot)                       # 第一项失败
    assert not task.finished
    task._next_at = 0.0
    task.tick(bot)                       # 第二项仍然执行
    assert bot.clicked("seq/step_01")
    task._next_at = 0.0
    task.tick(bot)
    assert task.finished


def test_daily_without_routines_finishes(bot: StubBot) -> None:
    task = DailyRoutineTask({"scenes": ["lobby"]})
    task.tick(bot)
    assert task.finished


# --------------------------------------------------------------------------- #
# gear_cleanup 的安全闸
# --------------------------------------------------------------------------- #


def test_gear_cleanup_aborts_on_locked_item(cfg) -> None:
    """检测到选中项里有已锁定装备 -> 立刻中止，这是防误卖的最后一道闸。"""
    layout = dict(ALL_LAYOUT)
    layout["inventory/icon_locked"] = (0.20, 0.20, 0.06, 0.06)
    frame = render(layout, 1920, 1080)
    install_templates(cfg, frame, layout)
    bot = StubBot(cfg, frame, current="inventory")

    task = GearCleanupTask({"flow": [{"click": "seq/step_01"}]})
    task.tick(bot)

    assert task.finished
    assert "gear_locked_detected" in bot.debug_frames
    assert not bot.clicked_names(), "发现锁定装备后不该再点任何东西"


def test_gear_cleanup_without_lock_template_proceeds(bot: StubBot) -> None:
    task = GearCleanupTask({"flow": [{"click": "seq/step_01"}]})
    bot.current = "inventory"
    task.tick(bot)
    assert bot.clicked("seq/step_01")


# --------------------------------------------------------------------------- #
# 任务构建
# --------------------------------------------------------------------------- #


def test_build_tasks_by_name(full_cfg) -> None:
    full_cfg.data["tasks"]["active"] = ["repeat_stage", "secret_shop"]
    full_cfg.data["tasks"]["secret_shop"]["enabled"] = True
    tasks = build_tasks(full_cfg)
    assert [t.name for t in tasks] == ["repeat_stage", "secret_shop"]


def test_build_tasks_honours_enabled_false(full_cfg) -> None:
    full_cfg.data["tasks"]["active"] = ["repeat_stage"]
    full_cfg.data["tasks"]["repeat_stage"]["enabled"] = False
    assert build_tasks(full_cfg) == []


def test_build_tasks_uses_type_alias(full_cfg) -> None:
    """任务名可以当别名，用 type 指定实现类。"""
    full_cfg.data["tasks"]["active"] = ["my_mail"]
    full_cfg.data["tasks"]["my_mail"] = {
        "type": "sequence",
        "flow": [{"click": "seq/step_01"}],
    }
    tasks = build_tasks(full_cfg)
    assert len(tasks) == 1
    assert isinstance(tasks[0], SequenceTask)


def test_build_tasks_unknown_name_raises(full_cfg) -> None:
    full_cfg.data["tasks"]["active"] = ["nope"]
    with pytest.raises(KeyError) as ei:
        build_tasks(full_cfg)
    assert "nope" in str(ei.value)


def test_all_expected_tasks_are_registered() -> None:
    from e7bot.tasks import REGISTRY

    for name in ("repeat_stage", "secret_shop", "sequence", "gear_cleanup",
                 "arena", "sanctuary", "dispatch", "summon", "daily"):
        assert name in REGISTRY, f"{name} 没有注册"
