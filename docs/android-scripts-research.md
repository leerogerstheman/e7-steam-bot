# 现有第七史诗脚本调研（安卓端 / 模拟器）

调研日期 2026-10。目的是回答一个问题：**这些脚本里哪些能搬到 PC 端，哪些必须换掉。**
结论是"必须换掉的只有三类 ADB 调用，其余全是纯逻辑"——这也正是本项目的设计依据。

---

## 0. 一处重要更正

**[PhantomPilots/AutoFarming](https://github.com/PhantomPilots/AutoFarming) 不是第七史诗脚本。**
它的 README 首行即 `"7DS Grand Cross scripts for auto-farming"`，GUI 标题是
`"AutoFarmers — 7DS Grand Cross"`，813 张模板全部对应《七大罪》的 UI。

它是 **7DS 的 PC 客户端**脚本，因此**没有任何 ADB 代码**。
它的架构值得参考（见 §3），但游戏功能、坐标、模板对 E7 完全不适用。

真正做 E7 的是下面这些。

---

## 1. 逐仓库对比

| 仓库 | 语言/★/最后更新 | 架构 | 截图 | 点击 | 分辨率 | 匹配方法 / 阈值 |
|---|---|---|---|---|---|---|
| **[Solunium/Epic-Seven-E7-Secret-Shop-Refresh](https://github.com/Solunium/Epic-Seven-E7-Secret-Shop-Refresh)** | Python / **139★** / 2026-04 | 双模式（MOUSE / ADB），刷新→检测→购买→确认循环 | ADB `exec-out screencap -p`→灰度；MOUSE `ImageGrab.grab(all_screens=True)` | ADB `input tap`/`input swipe`；MOUSE `pyautogui` | 断言 **1920×1080** | `TM_CCOEFF_NORMED`；ADB 物品 0.75；MOUSE 物品 0.70、loading 0.75 |
| **[brunocordioli072/epic7_bot](https://github.com/brunocordioli072/epic7_bot)** | Python / 61★ | 最规范：`commands/`(Shop/Hunt/Arena/Daily) + `modules/`(SecretShop/Battle/Guild/Sanctuary/Summon) + `processes/`(并行进程) | `ppadb` ADB | `input tap` | **强制 `wm size 1600x900`** | 0.6（全屏）/ 0.55（ROI） |
| **[faifan/e7_rta_auto](https://github.com/faifan/e7_rta_auto)** | Python / 26★ / 2026-09 | **完整阶段状态机**：大厅→预禁→选秀→选秀后禁→战斗→结算；torch Transformer 推荐 | **双模**：ADB `screencap` ／ Win32 子窗口裁剪 + PIL | ADB `input tap` ／ **Win32 `SendInput` 归一化坐标** | profile 化 1280×720 / **1920×1080** | `TM_CCOEFF_NORMED` **多尺度**：ban 0.55 / preban 0.40 / NCC 0.35 / 烧魂 0.65 |
| **[adriagual/epic7Bot](https://github.com/adriagual/epic7Bot)** | Python / 3★ / 2021-08 | **无状态机**：`bot.py` 硬编码线性序列 | ADB `screencap -p \| busybox base64`→`imdecode(...,0)` 灰度 | `input tap` / `input swipe` | ~1600×900 | `TM_CCOEFF_NORMED` 0.8 |
| **[timthlu/e7-auto-ss](https://github.com/timthlu/e7-auto-ss)** | Python / 2★ / 2026-06 | 单循环：重置检查→买(页1)→滚动→买(页2)→刷新 | `pyautogui.screenshot()` 后裁窗口 | `pyautogui` + **贝塞尔曲线** + 噪声抖动 | **1302×776** | `TM_CCORR_NORMED` 0.9，BGRA→BGR |

**观察**：匹配方法清一色 `TM_CCOEFF_NORMED`（唯一例外是 e7-auto-ss 用 `TM_CCORR_NORMED`），
阈值散布在 **0.5 ~ 0.9**。本项目沿用 `TM_CCOEFF_NORMED`，默认 0.86、可按模板单独覆盖 —— 与主流一致。

---

## 2. 功能清单与关键 UI 序列

| 功能 | 实现仓库 | 关键交互序列 |
|---|---|---|
| 自动战斗 / 重复刷本 | epic7Bot, epic7_bot, e7_rta_auto | 找 `stage_clear` → 点确认 → 找 `another_time`/`try_again` → 点 `confirm` → 重选队伍 |
| 狩猎 Hunt | epic7Bot, epic7_bot | 刷新×4 → `enter_battle` → Hunt → 选龙 → 等 `hunt_level_11` → 队伍 → 循环 `stage_clear`/`confirm_hunt`/`hunt_another_time` |
| 秘密商店刷新购买 | Solunium, e7-auto-ss, epic7_bot | 检测 `cov`/`mys` 模板 → 点商品右侧 Buy 区 → 确认弹窗 → 刷新 → 确认消耗（3 天空石/次） |
| 竞技场 NPC / PVP | epic7Bot, epic7_bot | `arena` → `enter_arena` → 领 `arena_gems_reward` → `pnj_fight` → `autoplay_button` → `skip_arena` → 结束 |
| 圣域 / 森林生物 | epic7Bot, epic7_bot | `go_to_sanctuary` → `get_gems_sanctuary` → `open_sanctuary_forest` → 召唤×3 → `choose_forest_creature` 补粮 |
| 派遣 / 邮件 / 每日奖励 | epic7Bot, epic7_bot | `dispatch_mission_repeat`×2；邮件：开信箱 → 主标签 → `collect all`×3 |
| 抽卡（免费召唤） | epic7Bot, epic7_bot | `summon` → `free_summon` → `confirm_free_summon`×2 → 返回 |
| RTA 选秀 + 战斗 | e7_rta_auto | 阶段模板检测 → Transformer 推荐 → 搜索框输名 → 点首个结果 → 确认 → 按 `skill_priority.json` 放技能 |
| 装备售卖 / 筛选 | (7DS AutoFarming) | `equipment_menu` → 筛选 → `salvage_equipment` → `ok_after_salvaging` |

**本项目已实现的对应关系**：

| 上述功能 | 本项目的任务 |
|---|---|
| 自动战斗 / 重复刷本 | `repeat_stage`（含 AUTO/倍速开启、进本导航序列） |
| 秘密商店 | `secret_shop` |
| 派遣 / 邮件 / 圣域 / 每日 | `sequence`（配置驱动，可起多个实例） |
| 装备售卖 | `gear_cleanup`（配置驱动 + 安全闸） |
| 弹窗清理（各仓库都有，且都踩过坑） | 所有任务共用的 `dismiss_popup` |

---

## 3. 可复用的设计模式

### 状态机

- **正面**：`AutoFarming` 的 `class States(Enum)` + `while True: if current_state == States.X: handler()`，
  14 个玩法各自独立枚举 + `farming_factory.py` 工厂
- **正面**：`e7_rta_auto` 的阶段状态机（12 个阶段模板）
- **反面**：`epic7Bot` 完全无状态机，`bot.py` 里一条线性序列点到底 —— 一旦弹窗就全盘错位

本项目采用「**场景识别 + 任务链**」：引擎每 tick 先认场景，再把控制权交给 `can_handle(场景)`
为真的任务，**一个 tick 只让一个任务动作**。比固定序列鲁棒，比纯 Enum 状态机更容易扩展。

### "找不到就等"的超时策略（这是最容易出事的地方）

| 做法 | 出处 | 问题 |
|---|---|---|
| `while click_image(tpl) == 0: pass` | `epic7Bot` | **无超时，会永久卡死** |
| 轮询 `if find(...)` + 外层 `while` + `stop_event` | AutoFarming, e7_rta_auto | 正确 |
| `_wait_my_turn(timeout=60)` | e7_rta_auto | 正确 |

本项目：**所有等待都强制带 `timeout`**，超时即报错停机并存调试图（`Bot.wait` / `wait_any` /
`wait_scene`）。另有全局 `unknown_scene_timeout`（默认 90s）兜底。

### 卡住 / 异常弹窗处理

1. **专用模板清理**：`close_popup` / `close_popup2` / `another_time` / `close_offer` /
   `confirm_close_popup`（`epic7Bot` 开局连点十余次）
2. **图像差分**：`cv2.absdiff` 非零像素占比 ≥70% 判画面变化，点击无效则重试 2 次（`epic7_bot`）
3. **运行监控**：状态停滞 + 点击静默 ≥ `stuck_timeout_minutes`（默认 10）→ 推送告警带截图（AutoFarming）
4. **重连模板**：`connecting` / `reconnect` / `there_was_a_connection_error`

本项目：①对应 `Task.COMMON_POPUPS` + `dismiss_popup`；②对应 `vision.frame_diff_ratio()`；
③对应 `max_consecutive_errors` + `unknown_scene_timeout` + 自动存调试图；④已列入弹窗模板表。

### 反检测随机化

| 手段 | 出处 | 本项目 |
|---|---|---|
| ROI 内随机取点 | `epic7_bot` `randomPoint` | 落点高斯抖动 σ=2.5px |
| 坐标 ±75px / ±25px 偏移 | Solunium | 同上（但幅度更克制，太大反而点偏） |
| 贝塞尔鼠标轨迹 + 噪声 | `e7-auto-ss` | 三次贝塞尔 + smoothstep 缓动 + 途中抖动 |
| 随机右击 / 双击 / 漏点 | `e7-auto-ss` | **不做** —— 随机漏点只会降低可靠性，对规避检测无实质帮助 |
| `random.uniform(wait, wait+1)` | 多家 | 全延时随机化 + tick 间隔 ±25% |

### 配置外置

| 形式 | 出处 | 评价 |
|---|---|---|
| Python 常量 `config.py` | `epic7Bot` | 改配置要动代码 |
| GUI + `configparser` | Solunium | 对普通用户友好 |
| TinyDB + docopt | `epic7_bot` | 过度设计 |
| **JSON profile（坐标全部外置）** | `e7_rta_auto` | **最佳**，移植性最好 |
| YAML | AutoFarming | 好 |

本项目用 **TOML**（Python 3.11+ 内置 `tomllib`，零依赖、支持注释），
并且做得更彻底：不只坐标，连**整段 UI 操作序列**都能写进配置（`ClickSequence`），
`repeat_stage.enter_sequence` / `sequence.flow` / `gear_cleanup.flow` 都是。

---

## 4. PC 端移植：哪些必须换、哪些能直接搬

### 必须替换（只有这三类 + 设备管理）

| ADB 调用 | 替换为 |
|---|---|
| `adb exec-out screencap -p` / `screencap -p \| busybox base64` | DXGI（`bettercam`）/ GDI（`mss`）/ `ImageGrab` |
| `adb shell input tap x y` | `SendInput`（绝对坐标归一化）/ `pyautogui` |
| `adb shell input swipe x1 y1 x2 y2 ms` | 鼠标拖拽 或 滚轮 |
| `input keyevent` / `adb connect` / 设备发现 / `wm size` | 不需要 |

### 可直接搬（纯逻辑）

模板匹配函数与阈值表、坐标字典、状态机与阶段判定、各功能 UI 序列、分辨率缩放公式。

### 已有的 PC 原生实现（无需从 ADB 改写）

- **`e7_rta_auto` 的 Win32 分支** —— 最完整的参考：
  `focus_game_window`：`FindWindow` → `EnumChildWindows` 找渲染子窗 → `ClientToScreen` 取偏移
  → `dpi_scale = 物理宽 / 逻辑宽`；输入用 `SendInput` + 归一化坐标。
  本项目思路一致，但把 DPI 感知做成**进程级（Per-Monitor V2）**，比逐次换算更可靠、更少出错。
- **Solunium 的 MOUSE 模式** —— `pygetwindow` 定位窗口 + `ImageGrab` 截图 + `pyautogui` 点击。
- **`e7-auto-ss`** —— 同上，且自带贝塞尔轨迹。

### 已知坑（本项目都做了处理）

| 坑 | 本项目 |
|---|---|
| DPI 缩放必须换算，否则点击整体偏移 | 启动即 `SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)` |
| 窗口标题需**精确匹配**（含空格） | 改用**正则**匹配，中/英/韩/日标题都覆盖 |
| 窗口须保持前台（不能后台运行） | 默认「不在前台就暂停」，并自动尝试切回前台 |
| 客户区 vs 窗口矩形差一个标题栏 | 一律用 `GetClientRect` + `ClientToScreen`，不用 `GetWindowRect` |
| 分辨率需固定 | 模板自带 `ref_size`，自动缩放，**不要求固定分辨率** |
| 显示器休眠 → 截图黑屏 | 启动时 `SetThreadExecutionState` 保活 |

---

## 5. 坐标与模板命名原样摘录

> 这些是从源码里原样摘出来的硬编码坐标。**不要直接拿来用**（分辨率/版本不同，而且
> PC 端 UI 未必一致），但可以据此**推测 UI 元素的相对位置**，帮助你在采集模板时判断
> "这个按钮大概在屏幕的哪个区域"，从而更快定位。

### `epic7Bot`（~1600×900）

- hunt：(1500,618) 刷新×4、(1330,816) 进入战斗、(970,780) 进入 Hunt、(1058,276) 选龙、(1400,840) 队伍、(932,290) 确认
- arena：(1500,618)、(1050,830) 开战、(1512,10) 跳过对话、(1470,832) 结束、(1393,150) 选对手、(880,622) 刷新对手；swipe `1057 856 → 1057 156 200`
- sanctuary：(100,150)、(1250,520) 补粮
- abyss：(905,490)、(1072,804)、(158,832) Purify、(960,656)、(794,677)、(71,71)
- altar：(1330,816)、(740,250)、(1060,300)、(1440,820)
- mail：(1540,600)、(1405,38)、(265,169)、(924,105) 领取全部、(950,550)
- summon：swipe `1400 856 → 1400 156 200`
- **模板名（85 个）**：`epic_seven_logo, start, autoplay_button, stage_clear, stage_failed, another_time,
  treasure, arena, enter_arena, arena_receive_rewards, arena_gems_reward, arena_pnj_challenge,
  arena_pnj_fight, pnj_fight, skip_arena, pvp_confirm_end, gems_required_arena, free_flags,
  confirm_hunt, hunt_level_11, hunt_another_time, enter_battle, receive_mail_reward,
  receive_reputation_reward, dispatch_mission_repeat, close_popup, confirm_close_popup,
  close_offer, go_to_sanctuary, get_gems_sanctuary, open_sanctuary_forest,
  summon_forest_creature, choose_forest_creature, summon, free_summon, confirm_free_summon,
  no_energy_event, has_soul, start_quest, tavern, connecting, reconnect` …

### `e7-auto-ss`（1302×776）

`buy_x=1155, buy_delta_y=60, refresh_x=210, refresh_y=705, middle_x=850, middle_y=400,
middle_width=400, middle_height=400, buy_confirm_x=750, buy_confirm_y=550,
refresh_confirm_x=740, refresh_confirm_y=500, daily_confirm_x=1025, daily_confirm_y=700,
ss_x=60, ss_y=380, buffs_x=650, buffs_y=720`

模板：`bm_image_small.png`（誓约书签）、`mm_image_small.png`（神秘书签）、`ss_refresh_button.png`

### `Solunium` ADB 模式（比例式，基准 1920×1080）

- 进入商店：(0.0411W, 0.3835H) 与 (0.4406W, 0.2462H)
- 购买偏移：`x + 0.4718W, y + 0.1000H`
- 购买确认：(0.5677W, 0.7037H)
- 刷新：(0.1698W, 0.9138H)；刷新确认：(0.5828W, 0.6411H)
- 滑动：`x1 = 0.6250W, y1 = 0.7481H → y2 = 0.3629H`

> 注意：**它已经在用比例式坐标了** —— 和本项目"全链路归一化"的思路一致，
> 说明这个方向是对的。本项目只是把它推到所有环节（含模板缩放），而不只是点击坐标。

模板：`cov.png`（誓约）、`mys.png`（神秘）、`fb.png`（友情点）

### `e7_rta_auto`（1920×1080 profile）

- 技能位置：S1(1531,997) S2(1689,1001) S3(1839,997)；烧魂 (1043,995)
- 敌方位置 4 点：(1466,460) (1712,549) (1260,578) (1496,747)
- 选秀：搜索开 (1817,137)、输入 (1082,233)、搜索按钮 (1438,231)、首个结果 (1033,408)、
  确认 (1279,994)、选秀后禁 (1279,991)、清空搜索 (1282,231)
- 阶段模板：`main_menu, arena_menu, lobby_apply, lobby_waiting, preban, preban_first_pick,
  postban, battle_ready, my_turn, opp_turn, signin_reward, summon_page`
- 结果模板：`victory.png` / `defeat.png`
- 英雄立绘 `templates/hero_images/c{id}.png`（144 张）、选秀卡 `templates/draft_cards/`（约 570 张）

### `epic7_bot`（强制 1600×900）

- 买按钮区：`(x+w/2+580, y+h/2+10) - (x+w/2+800, y+h/2+55)`
- 购买确认：`(761,605)-(1059,660)`；刷新：`(287,808)-(387,838)`；刷新确认：`(878,537)-(986,568)`
- 滚动：`(750,781)-(1282,845) → (701,98)-(1288,174)`
- 大厅防休眠双击：`(894,848)-(935,879)`
- 模板：`images/secret_shop/{Buy_button,covenant,mystic}.png`、
  `images/hunt/{try_again,confirm,insufficient_energy,pet_auto_battle_active,repeat_battling_has_ended,stage_clear}.png`、
  `images/{skip_button,connecting,there_was_a_connection_error,dispatch_mission_completed,dispatch_mission}.png`

---

## 6. 调研中未获取到的内容（如实标注）

- `AutoFarming` 的 E7 功能 / ADB 实现：**不存在**（游戏与方案都不同）
- Solunium 的 `main.ipynb` 未逐行读取（其 `.py` 已覆盖全部逻辑）
- `purpxd/E7AutoShop`(49★)、`Wrong-pixel/epic7auto`(31★)、`steven010116/epic7autoBookmark`(77★)
  仅取 README，未深入源码
- `boluokk/e7Helper`(189★, Lua) 未分析
- `e7_rta_auto` 的 `config/{attack_priority,counter_picks,pick_rules,hero_config}.json`
  仅确认结构，未逐条翻译（终端编码导致中文乱码）
- SteamDB / PCGamingWiki 因 Cloudflare 拦截（403）未能抓取

## 7. 调研中发现的、原清单之外的 E7 仓库

`boluokk/e7Helper` 189★(Lua)、`steven010116/epic7autoBookmark` 77★、
`brunocordioli072/epic7_bot` 61★、`purpxd/E7AutoShop` 49★、`Wrong-pixel/epic7auto` 31★、
`SamTheCoder777/E7-RTA-Helper` 7★、`jasoncheung22/Epic7-PC-Secret-Shop-Refresh` 5★、
`Asgarrrr/e7-shop-refresher`(Rust)、`romanbrancato/e7-refresh-bot`、
`Rea1-ms/AutoEpicSeven`、`t-2ddy/E7ShopBot`、`Haolinc/Epic-Seven-Automation`
