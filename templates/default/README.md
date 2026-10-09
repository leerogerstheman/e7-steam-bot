# 模板库

这个目录放**从你自己的游戏客户端采集的 UI 模板**。

```
templates/
└── default/                  <- profile 名，对应 config 里的 templates.profile
    ├── lobby/
    │   ├── btn_adventure.png
    │   └── btn_adventure.json
    ├── battle/
    │   ├── btn_retry.png
    │   └── btn_retry.json
    ├── common/
    ├── shop/
    └── inventory/
```

## 怎么生成

**不要手动画、不要从网上找图。** 用采集器从真实客户端截：

```powershell
python run.py capture
```

框选 → 自动落盘 PNG + JSON（含参考分辨率、搜索区域、阈值）→ 立刻回验分数。

## 命名规范

场景识别靠这些名字，请**照抄**（`config/default.toml` 的 `[scenes.*]` 引用了它们）：

### 场景锚点（必须采，否则认不出场景）

| 模板名 | 说明 |
|---|---|
| `lobby/btn_adventure` | 大厅 · 冒险按钮（3 个里任一个即可，采 2~3 个更稳） |
| `lobby/btn_hero` | 大厅 · 英雄按钮 |
| `lobby/btn_sanctuary` | 大厅 · 圣域按钮 |
| `battle/btn_auto_off` | 战斗 · AUTO **关闭**状态（看到它就点一下开 AUTO） |
| `battle/btn_auto_on` | 战斗 · AUTO 开启状态（用于确认） |
| `battle/btn_speed_x1` | 战斗 · 1 倍速（看到它就点一下切 2 倍） |
| `battle/btn_speed_x2` | 战斗 · 2 倍速 |
| `battle/icon_pause` | 战斗 · 暂停图标 |
| `battle/btn_start_battle` | 关卡准备 · 开始战斗 |
| `battle/btn_retry` | 结算 · 再次挑战（`battle_result` 的唯一锚点） |
| `battle/btn_confirm_result` | 结算 · 确认 |
| `common/btn_ok` | 通用 · 确定 |
| `common/btn_confirm` | 通用 · 确认 |
| `common/btn_close` | 通用 · 关闭 |
| `common/icon_loading` | 通用 · 加载中（可选，用于 `loading` 场景） |
| `shop/btn_refresh` | 秘密商店 · 刷新（`secret_shop` 的锚点） |
| `sanctuary/btn_collect_all` | 圣域 · 一键收取（可选） |
| `inventory/btn_sell` | 背包 · 出售（`inventory` 的锚点，可选） |

### 可选（按你要跑的功能采）

| 模板名 | 用途 |
|---|---|
| `battle/btn_auto_on` / `battle/btn_speed_x2` | 确认状态，避免重复点击 |
| `common/btn_cancel` | 关弹窗 |
| `common/popup_no_stamina` | 体力不足 —— 采了它脚本会**主动停机**而不是空转 |
| `common/popup_inventory_full` | 背包已满 —— 同上 |
| `common/popup_insufficient_resource` | 资源不足 —— 同上 |
| `shop/btn_buy` / `shop/btn_buy_confirm` | 秘密商店购买 |
| `shop/btn_refresh_confirm` | 刷新确认弹窗 |
| `shop/item_covenant_bookmark` | 誓约书签（要买的目标） |
| `shop/item_mystic_medal` | 神秘书签（要买的目标） |
| `inventory/icon_locked` | 锁定图标 —— 采了它装备清理才有防误卖闸门 |
| `inventory/btn_sell` / `inventory/btn_sell_confirm` | 装备出售 |

### 竞技场 `arena`（`tasks.arena`）

| 模板名 | 用途 |
|---|---|
| `arena/no_opponent` | **退出条件**：没有对手/没有旗子了 |
| `arena/btn_fight` | 挑战对手 |
| `arena/btn_autoplay` / `arena/btn_skip` | 战斗内开自动 / 跳过动画 |
| `arena/btn_result_confirm` | 结算确认 |
| `arena/btn_claim_reward` | 领取竞技场奖励 |

### 圣域 `sanctuary`

| 模板名 | 用途 |
|---|---|
| `sanctuary/btn_collect_all` | **退出条件**：一键收取（收完它就消失了） |
| `sanctuary/btn_forest` / `sanctuary/btn_summon_creature` | 森林生物（可选） |

### 派遣 `dispatch`

| 模板名 | 用途 |
|---|---|
| `dispatch/mark_completed` | 已完成的派遣标记 |
| `dispatch/btn_resend` / `dispatch/btn_resend_confirm` | 重新派遣 / 确认 |

### 免费抽卡 `summon`

| 模板名 | 用途 |
|---|---|
| `summon/btn_free` | **退出条件**：免费召唤按钮（没有它就是今天抽完了） |
| `summon/no_free` | 明确表示"免费次数已用完" |
| `summon/btn_confirm` / `summon/btn_confirm_2` | 两级确认 |

### 录制生成的进本序列 `seq/*`

用 `python run.py record` 自动生成，命名是 `seq/step_01`、`seq/step_02`……
不需要手工采。详见 README 的 §4.3。

### OCR 数字字形 `ocr/digits/*`

用于读体力/金币/剩余次数（见 [docs/OCR.md](../../docs/OCR.md)）：

```
ocr/digits/0.png ~ 9.png    必需
ocr/digits/k.png            千（"1.2k" -> 1200）
ocr/digits/m.png            百万（"3.5m" -> 3500000）
ocr/digits/dot.png          小数点
ocr/digits/slash.png        斜杠（读 "12/20" 这种形式）
```

**采集要点**：只框数字本身，**不要带背景**；一个数字一张图；
尽量在数字清晰、没有选中高亮的界面上采。

### 你自己扩展的

给 `sequence` / `gear_cleanup` 任务用的模板随便命名，只要在 `config/default.toml`
的对应 `flow` / `enter_sequence` 里写上同样的名字即可。

## 采集要点（直接决定脚本稳不稳）

1. **框紧贴元素**，别带背景 —— 带背景会导致在不同界面误匹配
2. 选**图形独特、颜色鲜明**的部分（图标 > 纯文字 > 纯色块）
3. **避开动态区域**：数字、计时器、进度条、角色立绘、活动横幅
4. **不要用通用按钮当场景锚点** —— `common/btn_ok` 到处都是，
   拿它判断"我在哪个界面"必然混乱
5. 采完**切到别的界面**，用采集器的「② 验证全部模板」跑一遍：
   当前界面该高的高、该低的低，才算采对了
6. **别换分辨率后重采** —— 模板会自动按 `ref_size` 缩放，不需要重采。
   但建议统一在 **1920×1080**（官方事实基准）下采集

## 关于 16:9

第七史诗 PC 端以 **1920×1080 / 16:9** 为基准。非 16:9（如 2560×1080 带鱼屏）
下 UI 可能有黑边或非等比拉伸，模板匹配会失配。**请把游戏设成 16:9 分辨率。**

## 多套模板

`config/default.toml` 里的 `templates.profile` 可以切换：

```
templates/
├── default/      <- 默认
└── tw/           <- 繁体客户端（UI 文字不同，但图标一般一样）
```

命令行临时切换：`python run.py run --profile tw`
