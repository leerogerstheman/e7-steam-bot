# 系统托盘 GUI

`e7bot/gui.py` 把引擎包成一个**托盘图标 + 右键菜单**，不弹主窗口。

为什么是托盘而不是窗口：脚本绝大部分时间在后台挂机，用户需要的只是
「一眼看到状态 + 一键急停」，任何会挡住游戏画面的窗口都是负担。

---

## 1. 快速开始

### 从源码运行

```powershell
# 1) 装 GUI 依赖（pystray + Pillow）
.\.venv\Scripts\python.exe -m pip install -r requirements-gui.txt

# 2) 先 dry-run 验证模板（只识别不点击）
.\.venv\Scripts\python.exe -m e7bot.gui --dry-run

# 3) 确认识别正常后正式跑
.\.venv\Scripts\python.exe -m e7bot.gui
```

托盘图标出现在任务栏右下角，**右键**打开菜单。

> 没有装 pystray 时，`python -m e7bot.gui` 不会抛 traceback，
> 而是弹一个中文提示框告诉你执行 `pip install -r requirements-gui.txt`。

### 从打包好的 exe 运行

```powershell
python tools/build_exe.py     # 产物在 dist/
```

双击 `dist/e7bot-gui.exe`，会弹 UAC —— **必须同意**（原因见第 4 节）。

---

## 2. 命令行参数

```
python -m e7bot.gui [--config PATH] [--dry-run] [--tasks a,b] [--profile NAME]
                    [--tick SECONDS] [--verbose] [--allow-multi]
```

| 参数 | 说明 |
|---|---|
| `--config`, `-c` | 配置文件路径，默认 `<应用根>/config/default.toml` |
| `--dry-run` | 「开始」菜单项默认走 dry-run（只识别不点击） |
| `--tasks` | 覆盖 `tasks.active`，逗号分隔，例如 `repeat_stage,secret_shop` |
| `--profile` | 覆盖模板 profile |
| `--tick` | tick 间隔秒，默认 0.35 |
| `--verbose`, `-v` | DEBUG 日志 |
| `--allow-multi` | 跳过单实例检查（调试用） |

`--config` 传相对路径时先按当前工作目录找；找不到就退回**应用根目录** ——
从开始菜单快捷方式启动时 cwd 可能是 `system32`，直接按 cwd 解析必然找不到。

---

## 3. 托盘菜单

```
未运行                          <- 状态行，不可点击，动态文本
─────────────
开始
以 dry-run 模式启动
停止
─────────────
暂停
继续
─────────────
打开日志目录
打开模板目录
─────────────
退出
```

### 状态行与图标颜色

| 状态 | 状态行文本 | 图标颜色 |
|---|---|---|
| 未启动 | `未运行` | 灰 `(128,128,128)` |
| 运行中 | `运行中 · tick 123 · 点击 45` | 绿 `(46,160,67)` |
| 暂停 | `已暂停` | 黄 `(219,154,4)` |
| 已停止 | `已停止` | 深灰 `(96,96,96)` |
| 出错 | `出错` | 红 `(203,56,56)` |

鼠标悬停在图标上的 tooltip 会显示更详细的信息（出错时附带原因）。

### 暂停 / 继续

「暂停」「继续」直接操作引擎的 `HotkeyWatcher` 状态，所以：

* 点菜单暂停 ≈ 按 F9；
* **物理按 F9 暂停后，菜单里的状态会跟着变**（暂停状态是实时从
  `bot.hotkeys.paused` 读的，不是 GUI 自己记的，单一数据源）；
* 「暂停」「继续」是两个独立菜单项，按当前状态自动灰掉其中一个。

> 实现细节：`engine.HotkeyWatcher` 只暴露只读的 `paused` 属性和
> `request_stop()`，没有公开的写入接口，而本项目要求不改 `engine.py`。
> 所以 `gui.set_hotkeys_paused()` 按「先找公开 API（`set_paused()`），
> 再退到内部 `_pause_flag`」的顺序来 —— 上游哪天补上公开方法，这里会自动切过去。
> 详见 `docs` 与源码注释。

### 热键

沿用配置里的 `safety.fail_safe_key` / `safety.pause_key`（默认 **F12 急停 / F9 暂停继续**），
由引擎的监听线程处理，和 GUI 无关 —— 也就是说**游戏全屏时也能急停**。

---

## 4. 管理员权限（重要）

启动时若检测到不是管理员，托盘会弹通知 + 一个置顶的 MessageBox 告警。

原因：第七史诗官方 PC 端通常以管理员身份运行。受 Windows **UIPI**
（User Interface Privilege Isolation）限制，低完整性级别的进程向高完整性级别
窗口发送的键鼠事件会被**静默丢弃** —— 不报错、不弹窗，脚本日志一切正常，
游戏里就是一动不动。这是最难排查的一类问题，所以必须显式告警。

打包出来的 exe 已经在清单里带了 `requestedExecutionLevel=requireAdministrator`
（spec 里的 `uac_admin=True`），双击就会弹 UAC。

---

## 5. 单实例保护

两个托盘程序同时跑会互相抢输入焦点、互相抢热键，画面直接乱掉，
而且用户很难意识到自己开了两个。

实现：Windows 命名互斥体 `Local\e7bot-gui-single-instance`。

* 用 `Local\` 而不是 `Global\`：创建 `Global\` 对象需要
  `SeCreateGlobalPrivilege`（普通用户没有），非管理员启动会直接失败。
* 第二个实例被挡住时，会弹框提示「已经有一个 e7bot 托盘程序在运行」，
  进程退出码 `2`。
* 若已有一个**提权**实例在跑，非提权进程连打开互斥体都会
  `ERROR_ACCESS_DENIED` —— 这种情况同样按「已在运行」处理。
* 互斥体创建因其它原因失败时**保守放行**（宁可让用户能启动，
  也不要因为拿不到锁就开不了程序），并在日志里记一条警告。

---

## 6. 线程模型

```
主线程         icon.run() 的 Windows 消息循环（pystray 硬性要求）
  │
  ├─ worker 线程    Bot.start() + Bot.run()（阻塞的 while True）
  └─ refresh 线程   每秒刷新图标颜色 / tooltip / 菜单文本
```

* **pystray 必须跑在主线程**：Windows 托盘图标依赖消息循环，
  `icon.run()` 内部就是 `GetMessage` 循环。
* **`Bot.run()` 是阻塞的**，必须扔进 `threading.Thread`；
  所有工作线程都是 daemon，主循环退出时不会被卡住。
* **每个 Bot 实例只能用一次**：`Bot.stop()` 会把热键急停标志永久置位，
  所以「停止后再次开始」会**重新 new 一个 Bot**，不复用旧实例。
* **每次开始都重新读配置**：改了 `config/default.toml` 不必重启托盘程序。
* worker 线程里的任何异常都会被捕获，映射到红色图标 + 托盘通知，
  **绝不静默死掉**。

### 关于 `Bot.run()` 吞异常

`engine.Bot.run()` 会把主循环里的异常全部捕获（内部记 `_stop_reason`、
走 alerts）后正常返回。因此 GUI 在 `run()` 返回后会回头检查
`bot._stop_reason`：

* `异常: ...` → 状态置为**红色「出错」** + 托盘通知；
* 其它非空原因（画面静止、超时、连续错误等**自发的安全停机**）→
  状态仍是「已停止」，但补一条托盘通知提醒用户；
* 用户自己点「停止」时不重复播报。

---

## 7. 路径解析（打包 / 未打包）

`gui.app_root()` 是唯一的路径基准：

| 运行形态 | 应用根目录 | config / templates / logs |
|---|---|---|
| 未打包 | 项目根（`e7bot/` 的上一级） | `<项目根>/config`、`<项目根>/templates` |
| 打包后 | **exe 所在目录**（`sys.executable` 的父目录） | `<exe目录>/config`、`<exe目录>/templates` |

刻意**不用** `sys._MEIPASS`：那是 onefile 解压出来的临时目录，进程退出就删，
用户往里放模板等于白放。

`config/` 与 `templates/` 是**外部目录**，spec 里 `datas=[]`，不打进 exe ——
因为用户要自己往 `templates/` 里放采集到的模板图。

> 细节：`config.resolve_path()` 以「配置文件所在目录」为基准，而配置在
> `config/` 子目录里，所以它有一层回退：`<root>/config/templates` 不存在时
> 退到 `<root>/templates`。即使配置文件缺失、回退到内置默认配置，
> `gui.load_config()` 也会给 `cfg.source` 设一个合理值，避免模板目录被
> 解析到 cwd（从快捷方式启动时 cwd 可能是 `system32`）。

---

## 8. 打包

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller   # 只需一次
.\.venv\Scripts\python.exe tools\build_exe.py
```

产物：

```
dist/
├─ e7bot-gui.exe     单文件、--windowed（无控制台）、带 UAC 提权清单
├─ config/           外部目录
└─ templates/        外部目录（往这里放你自己采的模板）
```

`tools/build_exe.py` 做的事：

1. 检查 `PyInstaller` 是否安装 —— 没装就打印安装命令并退出（返回码 2）；
2. 检查 `pystray` / `Pillow` 是否安装 —— 缺了就先报错，省得白打一次包；
3. 执行 `pyinstaller e7bot-gui.spec --noconfirm`（用 `python -m PyInstaller`，
   保证用的是当前虚拟环境里的那个，不会串到系统里的别的版本）；
4. 把 `config/` 与 `templates/` 复制到 `dist/`（**已存在则跳过**，
   不会覆盖你已经放进去的模板）；
5. 打印最终产物路径和大小。

参数：`--clean`（先删 `build/` `dist/`）、`--no-copy`（不复制外部目录）。

### spec 里的关键设置

| 设置 | 为什么 |
|---|---|
| `console=False` | 托盘程序不需要黑框控制台。代价是 print/traceback 全不可见，所以 GUI 的致命错误一律走 `MessageBoxW`，日志一律落文件 |
| `uac_admin=True` | **硬性要求**。非提权进程的键鼠事件会被 UIPI 静默丢弃 |
| onefile（`EXE` 里直接带 `a.binaries`/`a.datas`，无 `COLLECT`） | 用户拿到一个 exe，好分发 |
| `datas=[]` | `config/`、`templates/` 必须是外部目录 |
| `upx=False` | UPX 压过的 exe 更容易被杀软/反作弊误报 |
| `hiddenimports` 含 `win32timezone` | pywin32 的经典坑：运行时动态导入，静态分析找不到，漏了会在启动时报 `ModuleNotFoundError: win32timezone` |
| `hiddenimports` 含 `pystray._win32` | pystray 后端按平台动态选择，Windows 上必须显式带上 |
| `hiddenimports` 含 `pywintypes` | 同上，pywin32 的运行时依赖 |
| `excludes` 含 `tkinter` | 托盘 GUI 完全不用 Tk，排掉能省掉整个 Tcl/Tk |

> `PIL._tkinter_finder` 在 spec 里是**注释掉的**：它模块顶层就 `import tkinter`，
> 和「excludes 里排掉 tkinter」互斥；本程序不 import `PIL.ImageTk`，
> 带上它反而会拖进整个 Tcl/Tk（约 +10 MB）。如果哪天要在 exe 里跑
> `tools/capture_template.py`（那个用 Tk 框选），把它取消注释、
> 并把 excludes 里的 `"tkinter"` 删掉即可。

### 分发

把**整个 `dist/` 目录**拷给用户，三个东西必须在一起：

```
e7bot-gui.exe + config/ + templates/
```

---

## 9. 排错

| 现象 | 原因 / 处理 |
|---|---|
| 双击 exe 没反应 | 先看 `logs/e7bot.log`；`--windowed` 没有控制台，错误只在日志和弹框里 |
| 弹框说缺少依赖 | `pip install -r requirements-gui.txt` |
| 弹框说「已在运行」 | 已经有一个托盘实例，去托盘图标右键退出；调试时用 `--allow-multi` |
| 脚本在跑但游戏没反应 | 十有八九是没提权（UIPI 静默丢弃）；看托盘有没有权限告警 |
| 菜单状态文字不刷新 | win32 后端的菜单是**一次性构建**的，动态文本靠 `update_menu()` 重建；刷新线程 1Hz 触发，正常 1 秒内会更新 |
| 图标是红的 | 悬停看 tooltip / 看日志。常见原因：没找到游戏窗口、配置校验失败、运行中异常 |
| 想改模板 / 配置 | 「打开模板目录」「打开日志目录」菜单项；改完配置点「开始」会自动重新读 |

---

## 10. 已知限制（如实说明）

1. **托盘交互无法自动化测试**。`icon.run()` 会阻塞且需要真实桌面会话，
   所以「右键菜单能不能点」「气泡通知长什么样」只能在真机上手点验证。
   已自动化验证的是菜单**结构**、状态文本、图标像素、暂停逻辑、
   单实例互斥体、路径解析、以及用假 Bot 跑通的全部状态机转移。
2. **菜单文本刷新有 1 秒延迟**，且理论上存在「菜单正打开时重建 HMENU」的
   极短竞争窗口（pystray win32 后端的固有行为）。刷新频率刻意压到 1Hz
   来缩小这个窗口。
3. **暂停依赖 `hotkeys`**。`Bot.start()` 之前那一小段窗口里 hotkeys 还不存在，
   此时点「暂停」只会让界面显示「已暂停」，引擎不会真的等待
   （`engine.wait_if_paused()` 只认 `hotkeys.paused`），日志里会记一条警告。
4. **`hotkeys` 的暂停状态是通过内部 `_pause_flag` 设置的**，
   因为 `engine.HotkeyWatcher` 没有公开的写入接口，而本项目不改 `engine.py`。
   上游若改了私有属性名，暂停会失效（此时日志有警告，`set_hotkeys_paused()`
   返回 `False`）。
5. **exe 首次启动较慢**（onefile 要解压到 `%TEMP%`），体积也偏大
   （opencv + numpy 占大头）。
