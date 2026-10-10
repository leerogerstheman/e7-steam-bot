# e7bot — 第七史诗（Epic Seven）Steam / PC 端自动化脚本

面向 **Steam 版 / 官方 PC 客户端** 的第七史诗自动化框架。基于现有安卓端（模拟器 + ADB）
脚本的技术路线，针对 PC 客户端的特性重新做了架构适配。

> **当前状态**：框架、识别链路、输入层、配置系统、采集工具、离线自检均已完整可用并通过
> 71 项自动化测试。**游戏 UI 模板需要你在自己的客户端上采集**（见 [采模板](#4-采模板最关键的一步)）——
> 这不是偷懒，是唯一正确的做法：任何预置的 UI 图都会随版本更新失效，且不同分辨率/语言下
> 贴图不同。Steam 版 2026-10-29 上线，**但 Demo（AppID 5129800）现在就能下载**，
> 可以提前把模板采好。

---

## 1. 为什么不能照搬安卓端脚本

网上现成的第七史诗脚本（[Solunium/Epic-Seven-E7-Secret-Shop-Refresh](https://github.com/Solunium/Epic-Seven-E7-Secret-Shop-Refresh) 139★、
[faifan/e7_rta_auto](https://github.com/faifan/e7_rta_auto) 26★、
[brunocordioli072/epic7_bot](https://github.com/brunocordioli072/epic7_bot) 61★、
[adriagual/epic7Bot](https://github.com/adriagual/epic7Bot)、
[timthlu/e7-auto-ss](https://github.com/timthlu/e7-auto-ss) 等）
基本都是「模拟器 + ADB」路线：`adb screencap` 截图、`adb shell input tap x y` 点击、
硬编码像素坐标（常见 1280×720 / 1600×900 / 1920×1080 三档）。这套东西在 PC 端**逐条失效**：

| 环节 | 安卓端（模拟器 + ADB） | PC / Steam 端 | 本项目怎么解 |
|---|---|---|---|
| 截图 | `adb exec-out screencap -p`（含 base64 管道） | 没有 ADB，是 Windows 原生窗口 | DXGI Desktop Duplication（`bettercam`），GDI/mss 兜底 |
| 点击 | `input tap x y`（进程外注入，游戏无感） | 只能 `SendInput` 合成系统输入事件 | 自实现 SendInput 包装，扫描码键盘 + 绝对坐标鼠标 |
| 滑动 | `input swipe x1 y1 x2 y2 ms` | 无此命令 | 鼠标拖拽 / 滚轮 |
| 坐标 | 固定分辨率，可硬编码像素 | 分辨率/窗口尺寸可变 | **全链路归一化坐标 (0..1)** + 模板按参考分辨率自动缩放 |
| 流程 | 按顺序盲点固定坐标 | 分辨率不同、Steam 覆盖层、活动弹窗多 | **先认场景再动作**的状态机，每 tick 只让一个任务动手 |
| 反作弊 | 模拟器通常无 | **内核级 UNCHEATER** | 纯外部：只读屏幕、只发键鼠；不碰内存/不注入/不 hook |
| 权限 | 不需要 | **建议管理员**（实测 exe 是 `asInvoker`，但 Steam 可能提权） | 启动即检测并明确告警 |

> 顺带纠正一个容易搞混的点：**[PhantomPilots/AutoFarming](https://github.com/PhantomPilots/AutoFarming) 不是第七史诗脚本**，
> 它是《七大罪：光与暗之交战》(7DS Grand Cross) 的 PC 端农场脚本。它的**架构**（`IFarmer` 基类 +
> 每个玩法一个 `States(Enum)` 状态机、813 张模板）非常值得参考，本项目从中借鉴了
> "状态机 + 图像差分判断卡死"的思路，但它的游戏功能和坐标全部不适用 E7。

### 从这些脚本里学到的（以及刻意避开的）

| 观察 | 本项目的做法 |
|---|---|
| `epic7Bot` 用 `while click_image(tpl)==0: pass` —— **无超时，会永久卡死** | 所有等待都有 `timeout`，超时即报错停机并存调试图 |
| `brunocordioli072/epic7_bot` 用 `cv2.absdiff` 非零像素占比判断画面是否变化 | `vision.frame_diff_ratio()` 提供同一能力 |
| 各仓库阈值 0.5~0.9，匹配方法清一色 `TM_CCOEFF_NORMED` | 沿用 `TM_CCOEFF_NORMED`；默认阈值 0.86，可按模板单独覆盖 |
| 反检测手段：ROI 内随机取点、坐标 ±75px 偏移、贝塞尔轨迹、随机漏点 | 贝塞尔 + 高斯抖动 + 全延时随机化（但**不做随机漏点**——那只会降低可靠性） |
| `e7_rta_auto` 的 **Win32 分支**：`FindWindow`→`EnumChildWindows` 找渲染子窗→`ClientToScreen`→`dpi_scale=物理宽/逻辑宽`，`SendInput` 用归一化坐标 | 思路一致，本项目把 DPI 感知做成进程级（Per-Monitor V2），比逐次换算更可靠 |
| `Solunium` 的 MOUSE 模式硬断言 1920×1080 | 本项目**不假设分辨率**，模板自带 `ref_size` 自动缩放 |
| 大量硬编码像素坐标（如 `epic7Bot` 的 `(1500,618)`） | 全部外置为配置 + 模板，代码里没有一处硬编码像素坐标 |

完整调研（逐仓库架构、功能清单、坐标与模板命名原样摘录）见
[docs/android-scripts-research.md](docs/android-scripts-research.md)。

### Steam 版的关键事实（已核实）

| 项 | 值 |
|---|---|
| AppID | **5019180**（免费） |
| 计划上线 | **2026-10-29** |
| Demo | **5129800**（Steam Next Fest，**现在可下载**，进度不继承） |
| 反作弊 | **UNCHEATER**，内核级（Wellbia 出品，与 XIGNCODE3 同厂）。**不使用 VAC** |
| 引擎 | **cocos2d-x + SDL2 + Lua**，Smilegate 包装为 YUNA2D；**OpenGL/GLES 渲染**（不是 DirectX） |
| 账号 | 需要 **STOVE** 第三方账号，支持与 Steam 账号联动；与手机端数据互通 |
| 输入 | 商店页带 `Mouse Only Option` 分类 —— **纯鼠标即可完成全部操作** |
| 系统 | Win10 64-bit / GTX 1060 起 / 10GB；**1920×1080 是事实基准分辨率** |

完整调研见 [docs/steam-client-research.md](docs/steam-client-research.md)。

### ✅ 已对真实 Demo 做过静态实测

Demo 已安装后，我对 `EpicSeven_Steam.exe` 做了只读的二进制分析，
**其中两条推翻了上面的推断**，配置也已按实测值修正：

| 项 | 实测结论 | 之前（推断） |
|---|---|---|
| **窗口标题** | **`EpicSeven (Steam)`** | 只知道大概含 "Epic Seven" |
| **主程序名** | **`EpicSeven_Steam.exe`** | 猜测 |
| **渲染 API** | **OpenGL/GLES**（`OPENGL32.dll`/`EGL.dll`/`GLESv2.dll`） | ~~DirectX 11~~ ❌ |
| **权限** | exe 清单是 **`asInvoker`**，不请求提权 | ~~必须管理员~~ ❌（改为强烈建议） |
| **DPI** | 运行时自己调 `SetProcessDPIAware` → **DPI 感知** | 未知 |
| **反作弊加载** | **进程内加载**（主 exe 不 spawn 加载器）；但目录里有独立的 `ucldr_Epic7_SM_loader_x64.exe` | 未知 |

> 渲染 API 的修正**不影响本项目**：DXGI Desktop Duplication 复制的是**显示器输出**，
> 与游戏用什么 API 渲染无关，OpenGL 一样照抓 —— 这反而印证了 `bettercam` 是正确选择。

完整证据、可复现的验证脚本、以及"哪些只能靠你实跑确定"见
**[docs/demo-findings.md](docs/demo-findings.md)**。

> ⚠️ 引擎是 cocos2d-x 血统、**不是 Unity/UE**，所以没有现成的游戏自动化插件，
> Windows UI Automation 也读不到它的 UI —— 只能走「GPU 截图 + 模板匹配 + 合成输入」。

---

## 2. 安全边界（先看这个）

**反作弊是内核级的，能扫进程和内存。** 本项目因此在设计上把自己限制成一个"看不见的手"：

### 做

- ✅ 通过 DXGI 读取**屏幕像素**
- ✅ 通过 `SendInput` 发送**与真实硬件同级**的键鼠事件
- ✅ 所有延时、鼠标轨迹、落点都做了**拟人化随机化**
- ✅ 默认「游戏不在前台就暂停」——不在别的程序上乱点
- ✅ 全局急停热键（默认 **F12**），检查粒度细到鼠标移动的每一段

### 不做（红线）

- ❌ 读写游戏进程内存
- ❌ DLL 注入 / API Hook / 调试器附加
- ❌ 修改游戏文件、封包、网络协议
- ❌ 驱动级模拟输入（`interception` 之类）

**但这不构成"不会被封"的保证。** 任何自动化都违反游戏的用户协议，
检测与否、封不封由厂商决定，风险由使用者自担。请阅读 [docs/SAFETY.md](docs/SAFETY.md)。

> 为什么急停键不用 ESC？因为 **ESC 在第七史诗里是打开菜单的游戏键**。
> 参考项目 E7-helper 用 ESC，会误触菜单。

---

## 3. 安装

```powershell
# 需要 Python 3.11+（用到内置 tomllib），Windows 10/11 64 位
cd e7-steam-bot
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

然后**建议以管理员身份**运行：

```powershell
# 在「管理员: Windows PowerShell」里
.\.venv\Scripts\python.exe run.py doctor
```

> **关于管理员权限**（实测修正）：`EpicSeven_Steam.exe` 的清单是 `asInvoker`，
> 游戏本身**不请求**提权，所以理论上不是必须。但仍强烈建议提权，因为
> ① **Steam 可能以管理员运行**，那样游戏会继承高完整性级别，此时不提权的脚本
> 发的键鼠事件会被 UIPI **静默丢弃**（"点了没反应"最常见的根因）；
> ② UNCHEATER 是内核级驱动，安装/加载环节需要提权。
> 提权本身无副作用：高完整性进程向低完整性窗口发输入不受限制。

---

## 4. 使用流程

**不要直接 `run`。** 按下面五步走，每步都有明确目的：

```powershell
# ① 环境自检：管理员权限 / DPI / **Steam 库里的游戏安装** / 截图后端实测 /
#    窗口 / 显示器 / 模板齐备度
python run.py doctor
python run.py doctor --list-windows     # 找不到窗口时，列出所有窗口标题

# ①' 硬件冒烟测试：真抓屏 + 真发 SendInput（只移动鼠标，不点击）
python run.py smoke

# ② 采模板（最关键的一步，见 §4.1）
python run.py capture

# ②' 或者更省事：录一遍真人操作，自动裁模板 + 生成进本序列（见 §4.3）
python run.py record

# ③ 看识别准不准：实时打印场景判定 + 所有锚点的匹配分数
python run.py probe --duration 60

# ④ 干跑：只识别不点击，确认整条流程的判断都对
python run.py run --dry-run

# ⑤ 正式运行（建议先跑一次 alert-test，确认出事时能找到你）
python run.py alert-test
python run.py run

# 事后看战果
python run.py report
```

运行中：**F12 急停**，**F9 暂停/继续**（都可改配置）。

### 4.0 本机实测数据

`python run.py smoke` 在开发机上的真实结果（双 2560×1440 显示器，虚拟桌面原点 `-2560,0`）：

| 项目 | 结果 |
|---|---|
| `bettercam`（DXGI） | **62 fps**（16.2 ms/帧）✓ 有画面 |
| `mss`（GDI BitBlt） | 24 fps（41.2 ms/帧）✓ 有画面 |
| `printwindow` | **全黑**（方差 0.0）✗ 不可用 —— 印证了"GPU 合成的 DX 窗口抓不到" |
| `SendInput` 绝对坐标换算 | 4/4 测试点 **误差 0px**（含负坐标原点） |
| 贝塞尔插值落点 | 4/4 测试点 **误差 0px** |

> 后两项不是一开始就对的。首轮实测误差最大到 **49px**，且随移动距离增大 ——
> 原因是 Windows 会合并高频绝对移动事件，贝塞尔插值循环发出的最后一个事件被丢掉，
> 光标停在中途。修复方式是插值结束后**补一次精确落点**，并保证每步至少 3ms。
> 这就是为什么必须有 `run.py smoke`：这类问题和算法无关，只和系统输入栈有关，
> 只能在真机上量出来。

### 4.1 采模板（最关键的一步）

```powershell
python run.py capture
```

界面里操作：

1. 游戏切到目标界面（比如战斗结算画面）
2. 在「建议」下拉里选一个名字（如 `battle/btn_retry`），或自己填
3. 点 **① 框选新模板**，鼠标拖出一个**紧贴按钮边缘**的框
4. 工具会立刻回验并显示分数：
   - `✓ 可用` —— 分数 ≥ 阈值
   - `⚠ 偏低` —— 建议降阈值或重采
5. 换界面继续采，直到 `doctor` 不再报缺模板

**采集要点**（这些直接决定脚本稳不稳）：

- 框要**紧贴元素**，不要带上周围的背景 —— 背景会让模板在不同界面误匹配
- 优先选**图形独特、颜色鲜明**的部分（图标 > 纯文字 > 纯色块）
- **避开**会变化的区域：数字、计时器、进度条、角色立绘
- **避开**通用按钮当场景锚点：`common/btn_ok` 这种到处都有的按钮不能用来判断
  "我在哪个界面"，否则场景识别必然混乱
- 采完后**切到别的界面**再用 **② 验证全部模板** 跑一遍，
  确认"当前界面该高的高、该低的低" —— 这一步能提前发现 90% 的问题

模板是**一对文件**：

```
templates/default/battle/btn_retry.png    裁剪出的小图
templates/default/battle/btn_retry.json   元数据
```

`.json` 里：

```json
{
  "ref_size": [1920, 1080],   // 采集时的客户区分辨率 —— 匹配时按此自动缩放模板
  "region":   [0.38, 0.65, 0.24, 0.23],  // 归一化搜索区（工具自动推导，以模板为中心放大3倍）
  "threshold": 0.86,          // 该模板的匹配阈值
  "note": "采集自 1920x1080 ..."
}
```

`region` 是性能关键：有了它，场景识别只在小范围做匹配，速度能快一两个数量级，
误匹配也大幅减少。

### 4.2 配置进本导航

自动刷本需要一个「从大厅走到关卡」的点击序列。这部分**没法预设**——
每个人的关卡位置、当前活动都不一样。有两种做法：

**做法 A（推荐）：用录制工具自动生成** —— 见 §4.3。

**做法 B：手写** —— 在 `config/default.toml` 里按格式填：

```toml
[[tasks.repeat_stage.enter_sequence]]
click    = "lobby/btn_adventure"        # 要点的按钮模板
wait_for = "adventure/btn_stage_list"   # 点完等这个出现（可省）
timeout  = 20                           # 等待上限秒（可省）
delay    = [0.8, 1.6]                   # 点完随机停顿（可省）
```

每一步都是「找模板 → 点击 → 等下一个模板出现」，找不到就报错停机，**绝不盲点**。

### 4.3 录制回放（省掉大部分手工活）

```powershell
python run.py record
```

1. 按 **F8** 开始录制
2. 在游戏里**正常手动操作一遍**（例如：大厅 → 冒险 → 选关卡 → 开始战斗）
3. 再按 **F8** 结束

工具会自动完成：

* 记录每次点击的归一化坐标（只记落在游戏窗口内的点击）
* 从**点击前**的画面里自动裁出模板，并自动收缩到"在全屏里唯一匹配"的可靠尺寸
* 落盘模板 + sidecar 元数据到 `templates/<profile>/seq/`
* 生成 `config/recorded_sequence.toml`，里面是可直接粘贴的 `enter_sequence` 配置，
  并自动推导每一步的 `wait_for`（第 i 步等第 i+1 步的模板出现）

**为什么模板要取自"点击前"的画面**：点击被系统处理前界面还是原样；按下之后可能
已经在切场景了。所以工具用后台线程持续抓帧存进环形缓冲，检测到按下时回溯取
时间戳最接近的那一帧。

**为什么不挂钩子**：全局鼠标钩子（`SetWindowsHookEx` / `pynput`）能更精确地拿到
点击，但钩子是一种"注入式"行为，内核级反作弊有理由注意到它。轮询
`GetAsyncKeyState` + `GetCursorPos` 只是**读取系统状态**，和任何程序查询鼠标位置
没有区别。代价是可能漏掉极短的点击（<10ms），但真人点击通常 50~150ms，够用。

录制完**务必先 `run --dry-run` 验证**：录到的模板质量取决于当时的画面，
某一步找不到就用 `run.py capture` 手动重采那一个。

---

## 5. 功能模块

### 游戏玩法任务

| 任务 | 状态 | 说明 |
|---|---|---|
| `repeat_stage` | ✅ 完整 | **主力功能**。战斗里确保 AUTO/倍速开着 → 结算点「再次挑战」→ 循环；游戏内重复次数用完后按 `enter_sequence` 重新进本。含战斗超时保护、致命弹窗识别（体力不足/背包满） |
| 弹窗清理 | ✅ 内建 | 所有任务共用。公告、体力不足、背包满、断线重连等自动关闭。**这是无人值守最容易翻车的地方** |
| `secret_shop` | ✅ 完整 | 秘密商店自动刷新 + 按模板识别目标商品自动购买。含刷新次数/金币预算上限 |
| `arena` | ✅ 完整 | 竞技场：循环挑战直到没有对手。战斗内自动开自动/跳过，出现 `arena/no_opponent` 或达到 `max_runs` 收工 |
| `sanctuary` | ✅ 完整 | 圣域一键收取，收完（按钮消失）自动退出；可选森林生物召唤 |
| `dispatch` | ✅ 完整 | 派遣：把所有已完成的派遣重新发出去 |
| `summon` | ✅ 完整 | 免费抽卡，**按天记状态**，同一天不会重复进 |
| `daily` | ✅ 完整 | 每日清单编排：把若干子流程按顺序跑一遍，每天一次。单项失败不阻塞后续 |
| `sequence` | ✅ 通用 | 配置驱动的任意 UI 序列，可起多个实例（邮件、声望、活动等） |
| `gear_cleanup` | ⚠️ 需校准 | 批量出售装备。配置驱动 + 安全闸（已锁定装备检测、单次上限）。**第一次务必 dry-run** |

> 所有玩法任务的**导航步骤走配置**（因为每个人入口位置不同、活动会改版），
> 但**控制逻辑是专用代码**（循环条件、停止条件、按天去重）。
> 这个分工比"全塞进配置"清楚，也比"全硬编码"耐用。

### 支撑子系统

| 子系统 | 说明 |
|---|---|
| **模板采集** | 交互式框选 + 立刻回验分数（`run.py capture`） |
| **录制回放** | 手动操作一遍，自动裁模板 + 生成进本序列（`run.py record`）—— 见 §4.3 |
| **告警** | 声音 / 置顶弹窗 / ntfy 推送 / webhook，出事时让你知道（`run.py alert-test` 验证） |
| **卡死检测** | 画面完全静止超时即判定卡住并停机告警 |
| **运行统计** | JSONL 事件流 + Markdown/CSV 报表（`run.py report`） |
| **模板管理** | 健康检查 / 批量调阈值 / 去重 / 找未使用（`run.py templates`） |
| **OCR** | 用自采数字字形读体力/金币/次数，做预算控制（见 [docs/OCR.md](docs/OCR.md)） |
| **托盘 GUI** | 图形界面 + 打包 exe（见 [docs/GUI.md](docs/GUI.md)） |
| **离线自检** | 135 项自动化测试，不需要游戏（`run.py selftest` + `pytest`） |
| **硬件冒烟** | 真抓屏 + 真发输入，验证截图后端与坐标换算（`run.py smoke`） |

---

## 6. 配置速查

完整注释见 [`config/default.toml`](config/default.toml)。最常改的几项：

```toml
[safety]
fail_safe_key = "f12"          # 急停键
dry_run = false                # true = 只识别不点击
frozen_timeout = 180.0         # 画面完全静止多久判定卡死（0=关闭）

[input]
speed = 1.0                    # >1 更快；<1 更慢更保守
move_jitter_px = 2.5           # 落点抖动

[capture]
monitor_index = 0              # 游戏在第几块显示器上（doctor 会告诉你）

[templates]
default_threshold = 0.86       # 全局匹配阈值，误判多就调高，识别不到就调低

[tasks]
active = ["repeat_stage"]      # 要跑的任务，按顺序

[alerts]                       # 出事时怎么通知你（先跑 alert-test 验证）
enabled = true
[alerts.ntfy]
enabled = false
topic = ""                     # 手机装 ntfy App 订阅同一 topic

[stats]
enabled = true                 # 记事件流，供 `run.py report` 出报表
```

---

## 7. 排查

| 现象 | 原因与处理 |
|---|---|
| 找不到窗口 | `doctor --list-windows` 看真实标题，改 `window.title_patterns`（正则） |
| 截图全黑 / 抓帧失败 | 显示器休眠了（DXGI 依赖显示器输出，脚本已自动 `SetThreadExecutionState` 保活，但别手动关屏）；或游戏在另一块屏上 → 改 `capture.monitor_index` |
| 点击没反应 | ① **没以管理员运行**且 Steam 是提权的（UIPI 会静默丢弃事件）；② 游戏不在前台；③ 反作弊加载器窗口被误锁 —— 检查 `doctor` 报出的窗口标题是否为 `EpicSeven (Steam)` |
| 大量锚点分数都 < 0.3 | 分辨率不是 16:9 / 不是 1920×1080 基准；或模板采错界面；或截到了黑屏 |
| 分数 0.6~0.86 之间 | 典型是模板带了背景、或画面有动态元素。重采，或适度降低该模板阈值 |
| 场景识别乱跳 | 用通用按钮（`btn_ok` 之类）当了场景锚点，换成该界面**独有**的元素 |
| 报「连续 N 秒无法识别场景」 | 停在某个没采过的界面了。看 `logs/frames/` 里自动存的调试图 |
| 报「画面静止 N 秒，判定卡死」 | 多半是弹了个没采到模板的对话框在等人点。看 `logs/frames/*_frozen.png`，采上那个弹窗的关闭按钮 |
| 脚本莫名停机但日志没线索 | 看 `run.py report` 的「最近的问题」一节；停机时都有截图和原因记录 |
| 告警没收到 | `run.py alert-test` 验证；ntfy 要填 topic 且手机订阅同一 topic（topic 名等于密码，用随机串） |
| 玩法任务进去就"完成"了 | 检查画面里是不是有 `common/popup_no_stamina` 之类的致命弹窗模板被误匹配，或 `arena/no_opponent` 被误判 |
| 录制出来的模板 dry-run 找不到 | 录制时画面有动画/过渡。重新录那一段，或用 `run.py capture` 手动重采 |
| 模板匹配率在下降 | `run.py templates health` 体检；`unused` 找僵尸模板；`dedupe` 找重复 |
| 想换分辨率 | 不用改任何配置，直接换 —— 模板会按 `ref_size` 自动缩放 |
| `doctor` 报「忽略了标题 … 游戏名只占标题的 N%」 | 那是**防误锁的保护**在起作用（比如浏览器标签页里含"第七史诗"）。若确实是你的游戏窗口标题，调小 `window.min_title_coverage` |
| 锁到的窗口不是游戏 | 看 `doctor` 报出的标题与进程名。优先靠 `window.exe_patterns` 匹配（进程名不会被浏览器撞上）；必要时用 `exclude_title_patterns` 排掉 |
| **游戏报 `code:101 网络连接异常`** | **实测真因：STOVE 的地区校验没过**（不是网络不通）。日志里写得很明白：`stove_error_not_supported_country` / 「当前国家不支持此功能」。地区由**代理出口 IP 的归属国**决定（`device_nation`）——**节点名写着日本、出口在新加坡，一样会失败**。排查第一现场：`%LOCALAPPDATA%\STOVEPCCLIENTMODULE\logs\EpicSeven_Steam\` 与 `%LOCALAPPDATA%\STOVEPCSDK3\logs\STOVE_EPIC7\`。详见 [docs/demo-findings.md](docs/demo-findings.md) §7.9 |
| 加速器相关的另一个坑 | 雷神加速器会装 `CN=Leigod CA` 根证书做 TLS 中间人，而游戏用 libcurl + **自带的 133 张 CA 列表**校验。这**不是** `code:101` 的原因（见上），但**仍建议避开**这类会中间人的加速器 |
| 截图后端一直是 mss，不是 bettercam | ① **双显卡笔记本**上 DXGI 按适配器枚举输出，旧版会选错 —— 已修（按"分辨率×缩放比"反查）；② **游戏运行时 DDA 可能被整体屏蔽**（实测两块屏都返回纯黑）→ 自动降级到 mss 是预期行为，mss 在 1280×720 下实测 56~79 fps，够用 |
| 手动截图/OCR 读到的是别的窗口 | `mss`/DXGI 抓的是**屏幕**不是窗口。手动诊断前必须 `activate(hwnd)` 把游戏切到前台（引擎内部每次都做，只有自己写脚本时要注意） |

调试图、运行日志都在 `logs/` 下。

---

## 8. 项目结构

```
e7bot/
├── run.py                  CLI 入口（doctor/smoke/capture/record/probe/templates/
│                           alert-test/report/run/gui/selftest）
├── config/default.toml     全注释配置
├── e7bot/
│   ├── winutil.py          窗口定位、客户区矩形、DPI 感知、多显示器、标题覆盖率防误锁
│   ├── steamlib.py         Steam 库定位：找游戏装在哪、主程序叫什么、是否带反作弊
│   ├── capture.py          截图后端（bettercam/mss/printwindow）+ 黑屏/失败自动降级
│   ├── humaninput.py       SendInput 封装：贝塞尔鼠标轨迹、随机化、扫描码键盘
│   ├── vision.py           分辨率无关模板匹配、多尺度、±1px 补偿、NMS、灰度判断
│   ├── scene.py            场景状态机（any/none 规则 + 优先级）
│   ├── engine.py           主循环、安全闸、冻结检测、热键、统计、调试帧
│   ├── alerts.py           告警（log/sound/messagebox/ntfy/webhook）+ 限流
│   ├── stats.py            JSONL 事件流 + Markdown/CSV 报表
│   ├── ocr.py              数字/文本识别（自采字形优先，rapidocr 可选）
│   ├── gui.py              系统托盘 GUI
│   ├── config.py           TOML 配置（零依赖，用内置 tomllib）
│   └── tasks/              repeat_stage / secret_shop / sequence / gear_cleanup
│                           + flow_tasks（arena/sanctuary/dispatch/summon/daily）
├── tools/
│   ├── capture_template.py 交互式模板采集器（框选 + 自动回验）
│   ├── record.py           录制回放（录操作 → 自动裁模板 → 生成配置）
│   ├── template_admin.py   模板批量管理（健康检查/阈值/去重/未使用）
│   ├── probe.py            实时场景探针
│   ├── smoke_hardware.py   硬件冒烟测试（真抓屏 + 真发输入）
│   ├── build_exe.py        打包 exe
│   └── selftest.py         离线自检（合成画面）
├── tests/                  250+ 项 pytest（conftest 用假窗口/假截图/假输入驱动）
├── docs/                   SAFETY / OCR / GUI / demo-findings / 调研报告
└── templates/default/      模板库（按 profile 分组）
```

### 文档

| 文档 | 内容 |
|---|---|
| [docs/demo-findings.md](docs/demo-findings.md) | **真实 Demo 的静态实测记录**：窗口标题、引擎、反作弊、权限、DPI，含可复现的验证脚本与被推翻的推断 |
| [docs/SAFETY.md](docs/SAFETY.md) | 安全边界、行为层风险、四层停机与告警保障 |
| [docs/OCR.md](docs/OCR.md) | 数字识别：怎么采字形、怎么配 region、实测精度与已知限制 |
| [docs/GUI.md](docs/GUI.md) | 托盘 GUI 与 exe 打包 |
| [docs/steam-client-research.md](docs/steam-client-research.md) | 早期调研（靠商店页与社区帖推断，已被 demo-findings 部分更正） |
| [docs/android-scripts-research.md](docs/android-scripts-research.md) | 现有安卓端脚本逐仓库调研 |

### 三级验证

| 级别 | 命令 | 验证什么 | 需要游戏？ |
|---|---|---|---|
| 单元/集成 | `pytest` | 引擎调度、任务状态机、输入事件构造、截图降级、告警限流、统计读写、视觉链路、录制裁剪、模板管理、OCR | ❌ |
| 离线自检 | `run.py selftest` | 合成画面上的完整识别链路（74 项断言） | ❌ |
| 硬件冒烟 | `run.py smoke` | 真抓屏（后端可用性/帧率）、真发 SendInput（坐标换算精度） | ❌ 但需要真桌面 |

当前规模：**234 项 pytest + 74 项自检断言**，`pyflakes` 全树干净。

> 这些测试**都真的抓到过 bug**，不是摆设：
> - 合成测试发现"缩放比非整数倍时模板尺寸 `round()` 差 1px，小模板失配"
>   → `vision.py` 加了 ±1px 尺寸补偿
> - 硬件测试发现"贝塞尔插值最后一个事件被系统合并丢掉，落点最大偏 49px"
>   → `humaninput.py` 加了插值后的精确落点补偿
> - pytest 化时发现 `_test_scene` 隐式依赖 `_test_vision` 先写入模板（共享临时目录才通过）
>   → 改为自给自足
> - 补测试时发现 `dismiss_popup` 没兜 KeyError（少采一个弹窗模板就崩）、
>   `click_template(required=False)` 对"模板根本没采"仍抛 KeyError、
>   截图后端**初始化成功但抓帧失败**时不会降级、`FlowTask` 进入玩法界面后
>   再也拿不到 tick → 全部已修
> - 写 OCR 预算测试时发现 `dump_toml` 遇到 `None` 直接抛错 —— 而 TOML 规范里
>   根本没有 null 类型，正确做法是序列化时跳过 → 已修

### 实测数据（开发机，双 2560×1440，虚拟桌面原点 `-2560,0`）

| 项目 | 结果 |
|---|---|
| `bettercam`（DXGI） | **62 fps** ✓ |
| `mss`（GDI） | 24 fps ✓ |
| `printwindow` | **全黑** ✗（印证"GPU 合成的 DX 窗口抓不到"） |
| SendInput 坐标换算 | 4/4 测试点 **误差 0px**（含负原点） |
| 贝塞尔插值落点 | 4/4 测试点 **误差 0px** |
| OCR 数字识别 | 换字体 + 亮/暗底 **20/20 正确**；1280×720 以上可用，1024×576 读不出（如实标注） |
| 打包 exe | `dist/e7bot-gui.exe` **68.3 MB**，PE 清单 `requireAdministrator` 已确认 |

---

## 9. 参考

### 第七史诗自动化脚本（安卓端 / 模拟器）

- [Solunium/Epic-Seven-E7-Secret-Shop-Refresh](https://github.com/Solunium/Epic-Seven-E7-Secret-Shop-Refresh) 139★ — 秘密商店刷新，**双模式（ADB / PC 鼠标）**，其 MOUSE 模式本身就是 PC 原生实现，是 PC 侧最重要的先例
- [ruenocos/E7-helper](https://github.com/ruenocos/E7-helper) — 同一体系的 PC 端分支，README 明确要求**管理员权限 + 显示器常亮**（本项目的对应设计即源于此）
- [brunocordioli072/epic7_bot](https://github.com/brunocordioli072/epic7_bot) 61★ — 架构最规范的 E7 脚本（`commands/` + `modules/` + `processes/`），用 `cv2.absdiff` 差分重试防卡死
- [faifan/e7_rta_auto](https://github.com/faifan/e7_rta_auto) 26★ — RTA 自动对战，**唯一把全部坐标外置到 `profiles/*.json`** 的项目，且内置 Win32 `SendInput` 分支
- [adriagual/epic7Bot](https://github.com/adriagual/epic7Bot)、[timthlu/e7-auto-ss](https://github.com/timthlu/e7-auto-ss) — 后者自带贝塞尔鼠标轨迹
- 完整调研（逐仓库架构 / 功能清单 / 坐标与模板命名原样摘录）：[docs/android-scripts-research.md](docs/android-scripts-research.md)

### 其他游戏的 PC 端脚本（架构参考）

- [PhantomPilots/AutoFarming](https://github.com/PhantomPilots/AutoFarming) — **《七大罪》(7DS) 的 PC 端农场脚本，不是 E7**。但其 `IFarmer` 基类 + 每玩法独立 `States(Enum)` 状态机 + 卡死监控告警的架构值得参考

### 技术依据

- [BetterCam](https://github.com/glpc/BetterCam) — DXGI 截图，作者实测 238 fps（同基准 mss 76 fps、D3DShot 118 fps）；本项目实测 62 fps（2560×1440）
- [pydirectinput](https://github.com/rdp/pydirectinput) — DirectInput 扫描码 + `SendInput` 的实践依据（明确说明 `pyautogui` 的 `mouse_event` 在 DirectX 游戏上可能无效）
- Microsoft Docs — [UIPI 与合成输入](https://learn.microsoft.com/en-us/troubleshoot/power-platform/power-automate/desktop-flows/ui-automation/uipi-issues)、[高 DPI 桌面应用开发](https://learn.microsoft.com/en-us/windows/win32/hidpi/high-dpi-desktop-application-development-on-windows)
- Steam 商店页 — [Epic Seven (AppID 5019180)](https://store.steampowered.com/app/5019180/Epic_Seven/)：`Uses Kernel Level Anti-Cheat: UNCHEATER`、`Mouse Only Option`、STOVE 账号要求

---

## 10. 免责声明

本项目仅供**学习 Windows 自动化技术**（DXGI 截图、模板匹配、SendInput 合成输入）使用。
使用自动化脚本违反第七史诗的用户协议，可能导致账号被封禁。作者不对任何后果负责。
请自行评估风险，建议只在小号上试验。
