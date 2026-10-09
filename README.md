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
| 权限 | 不需要 | **必须管理员**（UIPI） | 启动即检测并明确告警 |

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
| 引擎 | Smilegate 自研 **YUNA Engine**（原生 C++ / **DirectX 11**） |
| 账号 | 需要 **STOVE** 第三方账号，支持与 Steam 账号联动；与手机端数据互通 |
| 输入 | 商店页带 `Mouse Only Option` 分类 —— **纯鼠标即可完成全部操作** |
| 系统 | Win10 64-bit / GTX 1060 起 / 10GB / DX11；**1920×1080 是事实基准分辨率** |

完整调研（含来源 URL 与"未查证到"的标注）见 [docs/steam-client-research.md](docs/steam-client-research.md)。

> ⚠️ 引擎是**自研 YUNA，不是 Unity/UE**，所以没有现成的游戏自动化插件，
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

然后**以管理员身份**运行（UIPI 限制，不提权键鼠事件会被静默丢弃）：

```powershell
# 在「管理员: Windows PowerShell」里
.\.venv\Scripts\python.exe run.py doctor
```

---

## 4. 使用流程

**不要直接 `run`。** 按下面五步走，每步都有明确目的：

```powershell
# ① 环境自检：管理员权限 / DPI / 截图后端实测 / 窗口 / 显示器 / 模板齐备度
python run.py doctor
python run.py doctor --list-windows     # 找不到窗口时，列出所有窗口标题

# ①' 硬件冒烟测试：真抓屏 + 真发 SendInput（只移动鼠标，不点击）
python run.py smoke

# ② 采模板（最关键的一步，见下）
python run.py capture

# ③ 看识别准不准：实时打印场景判定 + 所有锚点的匹配分数
python run.py probe --duration 60

# ④ 干跑：只识别不点击，确认整条流程的判断都对
python run.py run --dry-run

# ⑤ 正式运行
python run.py run
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
每个人的关卡位置、当前活动都不一样。在 `config/default.toml` 里按格式填：

```toml
[[tasks.repeat_stage.enter_sequence]]
click    = "lobby/btn_adventure"        # 要点的按钮模板
wait_for = "adventure/btn_stage_list"   # 点完等这个出现（可省）
timeout  = 20                           # 等待上限秒（可省）
delay    = [0.8, 1.6]                   # 点完随机停顿（可省）
```

每一步都是「找模板 → 点击 → 等下一个模板出现」，找不到就报错停机，**绝不盲点**。

---

## 5. 功能模块

| 任务 | 状态 | 说明 |
|---|---|---|
| `repeat_stage` | ✅ 完整 | **主力功能**。战斗里确保 AUTO/倍速开着 → 结算点「再次挑战」→ 循环；游戏内重复次数用完后按 `enter_sequence` 重新进本。含战斗超时保护、致命弹窗识别（体力不足/背包满） |
| 弹窗清理 | ✅ 内建 | 所有任务共用。公告、体力不足、背包满、断线重连等自动关闭。**这是无人值守最容易翻车的地方** |
| `secret_shop` | ✅ 完整 | 秘密商店自动刷新 + 按模板识别目标商品自动购买。含刷新次数/金币预算上限 |
| `sequence` | ✅ 通用 | **配置驱动**的任意 UI 序列：定时领邮件、圣域收菜、派遣、每日任务。可起多个实例 |
| `gear_cleanup` | ⚠️ 需校准 | 批量出售装备。配置驱动 + 安全闸（已锁定装备检测、单次上限）。**第一次务必 dry-run** |

---

## 6. 配置速查

完整注释见 [`config/default.toml`](config/default.toml)。最常改的几项：

```toml
[safety]
fail_safe_key = "f12"          # 急停键
dry_run = false                # true = 只识别不点击

[input]
speed = 1.0                    # >1 更快；<1 更慢更保守
move_jitter_px = 2.5           # 落点抖动

[capture]
monitor_index = 0              # 游戏在第几块显示器上（doctor 会告诉你）

[templates]
default_threshold = 0.86       # 全局匹配阈值，误判多就调高，识别不到就调低

[tasks]
active = ["repeat_stage"]      # 要跑的任务，按顺序
```

---

## 7. 排查

| 现象 | 原因与处理 |
|---|---|
| 找不到窗口 | `doctor --list-windows` 看真实标题，改 `window.title_patterns`（正则） |
| 截图全黑 / 抓帧失败 | 显示器休眠了（DXGI 依赖显示器输出，脚本已自动 `SetThreadExecutionState` 保活，但别手动关屏）；或游戏在另一块屏上 → 改 `capture.monitor_index` |
| 点击没反应 | **没以管理员运行**（UIPI 会静默丢弃事件）；或游戏不在前台 |
| 大量锚点分数都 < 0.3 | 分辨率不是 16:9 / 不是 1920×1080 基准；或模板采错界面；或截到了黑屏 |
| 分数 0.6~0.86 之间 | 典型是模板带了背景、或画面有动态元素。重采，或适度降低该模板阈值 |
| 场景识别乱跳 | 用通用按钮（`btn_ok` 之类）当了场景锚点，换成该界面**独有**的元素 |
| 报「连续 N 秒无法识别场景」 | 停在某个没采过的界面了。看 `logs/frames/` 里自动存的调试图 |
| 想换分辨率 | 不用改任何配置，直接换 —— 模板会按 `ref_size` 自动缩放 |

调试图、运行日志都在 `logs/` 下。

---

## 8. 项目结构

```
e7bot/
├── run.py                  CLI 入口（doctor / capture / probe / run / selftest）
├── config/default.toml     全注释配置
├── e7bot/
│   ├── winutil.py          窗口定位、客户区矩形、DPI 感知、多显示器
│   ├── capture.py          截图后端（bettercam/mss/printwindow）+ 黑屏自动降级
│   ├── humaninput.py       SendInput 封装：贝塞尔鼠标轨迹、随机化、扫描码键盘
│   ├── vision.py           分辨率无关模板匹配、多尺度、NMS、颜色/灰度判断
│   ├── scene.py            场景状态机（any/none 规则 + 优先级）
│   ├── engine.py           主循环、安全闸、热键、统计、调试帧
│   ├── config.py           TOML 配置（零依赖，用内置 tomllib）
│   └── tasks/              功能模块（repeat_stage / secret_shop / sequence / gear_cleanup）
├── tools/
│   ├── capture_template.py 交互式模板采集器（框选 + 自动回验）
│   ├── probe.py            实时场景探针
│   ├── smoke_hardware.py   硬件冒烟测试（真抓屏 + 真发输入）
│   └── selftest.py         离线自检（合成画面，71 项断言）
└── templates/default/      模板库（按 profile 分组）
```

### 两级验证

**`run.py selftest`** —— 不需要游戏，验证识别链路（合成画面，71 项断言）：

坐标换算与跨分辨率一致性、模板匹配与自动缩放、±1px 取整补偿、多尺度兜底、
多目标 + NMS、灰度判断、场景优先级、配置校验与 TOML 往返、配置驱动序列。

**`run.py smoke`** —— 需要真机，验证硬件层：截图后端可用性与帧率、
SendInput 绝对坐标换算（含负原点多显示器）、贝塞尔插值落点精度。

> 这两级测试**都真的抓到过 bug**：
> - 合成测试发现"缩放比非整数倍时模板尺寸 `round()` 差 1px，小模板就会失配"
>   → `vision.py` 加了 ±1px 尺寸补偿
> - 硬件测试发现"贝塞尔插值最后一个事件被系统合并丢掉，落点最大偏 49px"
>   → `humaninput.py` 加了插值后的精确落点补偿
> - pytest 化时发现 `_test_scene` 隐式依赖 `_test_vision` 先写入模板（共享临时目录
>   才通过）→ 改为自给自足

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
