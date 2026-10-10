# 真实客户端实测记录（Steam Demo）

对已安装的 **Epic Seven Demo** 做的静态分析记录。所有结论都附**可复现的验证方法**，
不含推测。分析对象只读，未修改任何游戏文件。

```
安装位置: F:\SteamLibrary\steamapps\common\Epic Seven Demo
Steam AppID: 5129800（appmanifest_5129800.acf，SizeOnDisk 135467955）
```

> 为什么要做这个：在拿到客户端之前，项目的配置（窗口标题、进程名、引擎、反作弊）
> 只能靠商店页和社区帖子推断。拿到二进制之后，这些全都能**直接读出来**，
> 而且其中有两条把之前的推断**推翻**了。

---

## 1. 目录清单

```
EpicSeven_Steam.exe                  27.9 MB   主程序
ucldr_Epic7_SM_loader_x64.exe        11.5 MB   UNCHEATER 反作弊加载器
xnina_x64.xem                         2.09 MB   UNCHEATER/XIGNCODE 模块
bundle.pack                          47.9 MB   游戏资源包（加密/混淆，见 §5）
tensorflowlite.dll                   19.6 MB   端上 ML 推理
BaseSDK.dll / APIModule.dll / IAPSDK.dll / GamingServicesSDK.dll / ViewSDK.dll
                                                Smilegate STOVE SDK 系列
fmod.dll / fmodstudio.dll             FMOD 音频
sentry.dll / crashpad_handler.exe     崩溃上报
steam_api64.dll / WebView2Loader.dll
```

总共只有 **17 个文件 / 0.13 GB** —— Demo 是精简包（大概是教程 + 少量内容）。

---

## 2. 已确认的事实（含验证方法）

| 项 | 结论 | 验证方法 |
|---|---|---|
| **窗口标题** | **`EpicSeven (Steam)`** | `EpicSeven_Steam.exe` 里的 UTF-16LE 字符串 |
| **主程序名** | **`EpicSeven_Steam.exe`** | 目录清单 + exe 内字符串 |
| **反作弊** | **UNCHEATER（Wellbia）确实存在** | exe 内含 `uncheater_windows_sdk`、`Wellbia.com Co., Ltd.`、`Error: failed uncheater init error code:`；目录内有 `ucldr_Epic7_SM_loader_x64.exe` 与 `xnina_x64.xem` |
| **反作弊加载方式** | **进程内加载**（主 exe 不 spawn 加载器） | 主 exe 里搜 `[A-Za-z0-9_]+\.exe` → **0 命中**，但含 `failed uncheater init` 运行时错误串 |
| **渲染 API** | **OpenGL / OpenGL ES**，**不是 DirectX** | 依赖表含 `OPENGL32.dll`、`EGL.dll`、`GLESv2.dll`、`GLESv1_CM.dll`、`OSMesa.dll`；搜 `D3D11` / `dxgi` / `SwapChain` → **0 命中** |
| **引擎** | **cocos2d-x + SDL2 + Lua 脚本层**，Smilegate 包装为 YUNA2D | `cocos2dx_GLView_getDesignResolutionSize`、`cc.GLView:getDesignResolution`、`TMXLayer`/`ListView`/`PageView`/`ImageView`；`SDL.window.create.fullscreen/borderless/hidden`；`lua_yuna2d_LayerPostProcess_create`；Lua 搜索路径 `.\?.lua;!\lua\?.lua;!\lua\?\init.lua` |
| **权限** | **`asInvoker`** —— exe **不请求**管理员 | 嵌入清单 `<requestedExecutionLevel level='asInvoker' uiAccess='false' />` |
| **DPI 感知** | **运行时自己设为 DPI 感知** | 导入表含 `SetProcessDPIAware`、`SetProcessDpiAwareness`、`SetProcessDpiAwarenessContext`、`GetDpiForWindow`、`AdjustWindowRectExForDpi`、`EnableNonClientDpiScaling` |
| **输入相关** | 含 `dinput8.dll`（手柄支持） | 导入表字符串 |
| **可改分辨率** | 支持运行时改分辨率 | 字符串 `application_change_resolution`、`application_scene_reload` |
| **配置文件** | `packenv.ini`、`win32_env_home.ini`、`lua.ini` | exe 内字符串 |
| **日志** | `account.log`、`patch.log` | exe 内字符串 |

### 验证脚本（可自行复现）

```python
from pathlib import Path
import re
P = Path(r"F:\SteamLibrary\steamapps\common\Epic Seven Demo")

# 窗口标题：找 UTF-16LE 里的 "Epic"
exe = (P / "EpicSeven_Steam.exe").read_bytes()
print([s.decode('utf-16-le') for s in re.findall(rb'(?:[\x20-\x7e]\x00){4,}', exe)
       if 'Epic' in s.decode('utf-16-le', 'ignore')])
# -> ['EpicSeven', 'EpicSeven (Steam)', 'EpicSeven_Steam.exe']

# 权限与 DPI：读嵌入清单
m = re.search(rb'<\?xml.*?</assembly>', exe, re.S)
print(m.group(0).decode())        # 只有 requestedExecutionLevel asInvoker

# 渲染 API：看依赖
print([d for d in ('OPENGL32.dll','EGL.dll','GLESv2.dll','D3D11.dll','dxgi.dll')
       if d.encode() in exe])
# -> ['OPENGL32.dll', 'EGL.dll', 'GLESv2.dll']
```

---

## 3. 推翻了之前的两条推断

这两条都是我在拿到客户端**之前**写进 README 的，现在必须改。

### 3.1 ❌「引擎是 YUNA Engine（C++/DX11）」

- **之前**：依据 Steam 商店页的系统需求写了 "DirectX 11"，加上 Smilegate 官方
  newsroom 提过自研 YUNA 引擎，就写成"自研 YUNA Engine（C++/DX11）"。
- **实际**：**OpenGL/GLES 渲染**（依赖表实打实列着 `OPENGL32.dll` / `EGL.dll` /
  `GLESv2.dll`），窗口层是 **SDL2**，UI 是 **cocos2d-x** 血统（`GLView` / `TMXLayer` /
  `ListView` / `PageView`），脚本层是 **Lua**（`yuna2d_*` API）。
- 商店页的 "DirectX 11" 更可能是指**系统需求的最低图形能力**，而不是游戏的渲染后端。

**对本项目的影响：无。** DXGI Desktop Duplication 复制的是**显示器输出**，
与应用用什么 API 渲染无关 —— OpenGL / DirectX / Vulkan 一律照抓。
这反而**印证**了 `bettercam`（DDA）是正确选择，而 `PrintWindow` 在
OpenGL 窗口上同样抓不到（实测全黑，见 README 的实测数据表）。

**一个额外收获**：SDL2 的窗口默认走 **Windows 消息 + 系统输入队列**，
不像纯 DirectInput 游戏那样只读原始输入状态。这意味着 `SendInput` 一定有效
（它本来就走系统队列）；理论上 `SendMessage` 也可能被 SDL2 消费 ——
但本项目**不用**它：窗口消息投递不像真实输入，行为特征明显。

### 3.2 ❌「必须管理员权限运行」

- **之前**：依据社区项目 E7-helper 的 README（"使用新的 PC 客户端时必须以管理员权限运行"）
  写成了硬性要求。
- **实际**：`EpicSeven_Steam.exe` 与 `ucldr_Epic7_SM_loader_x64.exe` 的清单都是
  **`asInvoker`** —— **游戏本身不请求提权**。

**修正后的准确说法**：

> 游戏 exe 不请求提权，所以**理论上不是必须**。但仍**强烈建议以管理员运行**，原因有三：
> 1. **Steam 可能以管理员运行** —— 那样游戏会继承高完整性级别，此时不提权的脚本
>    发的键鼠事件会被 UIPI 静默丢弃（这是"点了没反应"最常见的根因）；
> 2. UNCHEATER 是**内核级驱动**，它的安装/加载环节需要提权，游戏在首次运行或
>    驱动更新时可能触发提权流程；
> 3. **提权是安全的**：高完整性进程向低完整性窗口发输入没有任何限制，
>    所以以管理员运行在游戏未提权时也不会有副作用。

因此 `run.py doctor` 仍然会对非管理员给出告警，但措辞应从"必须"改为"建议"。

---

## 4. 由实测得出的新结论

### 4.1 反作弊加载器必须被排除在窗口搜索之外 ⭐

`ucldr_Epic7_SM_loader_x64.exe` 是独立进程，可能弹出自己的窗口
（初始化失败提示、驱动安装提示等）。**一旦锁错窗口**，脚本就会对着一个
非游戏窗口截图和点击 —— 而且失败现象很隐蔽（"识别不到场景"），
很难联想到是锁错了窗口。

已实现：`config` 新增 `exclude_title_patterns` / `exclude_exe_patterns`，
`winutil.find_window` 支持排除；默认排除 `ucldr` / `uncheater` / `xnina` / `crashpad`。
并有回归测试 `tests/test_window.py` 锁住这个行为。

### 4.2 游戏是 DPI 感知的 → 物理像素坐标一致

清单里没有 `dpiAware`，但导入表证明游戏**运行时自己调用了 DPI API**。
所以游戏按**物理像素**创建窗口，与本项目（Per-Monitor V2）的坐标系一致，
不存在"系统把游戏位图拉伸"导致的模糊问题。

> 这里有个方法论教训：**不能只看清单就断定 DPI 感知**。清单缺失时，
> 必须再去导入表里查 `SetProcessDPIAware` / `SetProcessDpiAwarenessContext`。
> 我第一遍就是只看清单、差点写出错误结论。

### 4.3 分辨率确实可以在运行时改

`application_change_resolution` 说明游戏支持改分辨率 —— 这进一步说明
本项目"全链路归一化坐标 + 模板按 `ref_size` 自动缩放"的设计是必要的，
硬编码像素坐标会随分辨率变化失效。

### 4.4 ⭐ 加完实测模式后，`doctor` 立刻锁错了窗口

这条是**真跑出来的**，不是设想。把实测标题写进配置、在开发机上跑 `run.py doctor`，
得到：

```
[OK ] 游戏窗口: '第七史诗steam端要上线了，根 — DeepSeek Harness'
```

**它锁到了浏览器窗口。** 原因：为了兼容中文客户端，`title_patterns` 里加了
`第七史诗`，而当时正开着的浏览器标签页标题里恰好含这四个字。

这类假阳性**极其危险且极难定位**：

- 脚本会对着浏览器截图、往浏览器里点击
- 表面现象却是"识别不到场景 / 找不到模板"，根本联想不到是锁错了窗口
- 更糟的是它可能真的点中浏览器里的东西（关标签、点链接）

**修法（已实现）**：匹配改成**两级 + 标题覆盖率门槛**

1. **进程名命中 = 最强信号**，优先采用。实测进程名是 `EpicSeven_Steam.exe`，
   不像窗口标题那样容易被浏览器/编辑器撞上。
2. **仅标题命中时**，要求游戏名**覆盖标题的 ≥50%** 才算数
   （`window.min_title_coverage`）。浏览器标题里 `第七史诗` 只占 11% → 拒绝；
   真实标题 `EpicSeven (Steam)` 占 100% → 通过。
3. 拒绝时**打印原因**，而不是默默返回"没找到"。

修复后的真实输出：

```
[find_window] 忽略了标题 '第七史诗steam端要上线了，根 — DeepSeek Harness'：
              游戏名只占标题的 11%（阈值 50%），不像游戏窗口。
[find_window] 忽略了标题 'Epic Seven Demo - 文件资源管理器'：
              游戏名只占标题的 40%（阈值 50%），不像游戏窗口。
[!! ] 没找到第七史诗窗口。请先启动游戏（Steam 版 / Demo）。
```

代价是「第七史诗 - 官方版」这类带后缀的标题（覆盖率 ~0.4）也会被拒。
这是**刻意选安全侧**：宁可找不到（有明确报错 + `--list-windows` 可排查），
也不要锁错窗口乱点。有回归测试 `tests/test_window.py` 用真实浏览器标题锁住这个行为。

> 方法论收获：**这类问题只有真跑才暴露得出来。** 单测可以验证
> "`EpicSeven (Steam)` 能被匹配到"，但"`第七史诗` 会撞上浏览器"这件事，
> 是环境给的 —— 而它恰恰是实际使用中最容易翻车的地方。

---

## 5. `bundle.pack` 是加密的 —— 而且我们**不需要**它

**观测结果**：

- 头部 32 字节无任何可识别 magic（不是 UnityFS / ZIP / RPA 等）
- 熵 ≈ **6.7 bits/byte**（不是 8.0），说明是**混淆或压缩**而非强加密
- 头部与尾部出现**重复的字节序列**（尾部含头部前 16 字节），
  这符合**重复密钥 XOR** 的特征
- 全包有 6108 个 ≥8 字符的可打印串，但内容随机（`+bny(#|y` 之类）

**结论：技术上很可能可以还原出密钥并解包。**

**但我刻意没有去做**，理由有两条，都不是"怕麻烦"：

1. **没有技术收益。** 解出来的是**图集原始素材**（atlas），和屏幕上**实际渲染**
   出来的 UI 不是一回事 —— 有缩放、有九宫格拉伸、有状态变化（亮/灰）、有特效叠加。
   本项目做模板匹配需要的是"屏幕上真实长什么样"，所以正确做法始终是
   **截运行中的游戏**（`run.py capture` / `run.py record`）。拿素材图当模板
   反而会失配。
2. **违背项目的设计原则。** 整个项目的立身之本是"**不碰任何受保护的内容**" ——
   不读内存、不注入、不 hook、不改文件。在装了内核级反作弊（UNCHEATER）的机器上
   对受保护资源做密码学分析，恰恰是这条原则的反面；即使动机是善意的，
   行为特征也与破解无异。

所以这里只记录观测事实，不做进一步动作。

---

## 6. 需要用户实测才能确定的事

静态分析到此为止。以下只能靠**实际运行**确定，我无法代替：

| 待确定 | 怎么确定 |
|---|---|
| 窗口标题是否真的是 `EpicSeven (Steam)` | `python run.py doctor`（会自动列出匹配到的窗口） |
| 游戏窗口的默认分辨率与窗口模式 | 启动游戏 → 设置 → 图像 |
| UI 是否与手机端一致、按钮实际位置 | 截图后用 `run.py capture` 框选 |
| 反作弊加载器是否会弹窗、弹什么 | 首次启动时观察 |
| UNCHEATER 驱动是否需要额外提权步骤 | 首次启动时观察 |
| 模板识别阈值是否合适 | `run.py probe` |

---

## 7. 首次真机运行的实测结果（用户启动 Demo 之后）

### 7.1 窗口标题是**中文** `第七史诗`，不是 exe 里的 `EpicSeven (Steam)`

`doctor` 实测锁定到：

```
hwnd=0x167072A pid=32628 title='第七史诗' client=(790, 397, 1280, 720)
exe=F:\SteamLibrary\steamapps\common\Epic Seven Demo\EpicSeven_Steam.exe
```

**结论：窗口标题是按客户端语言动态设置的。** exe 里那个 `EpicSeven (Steam)` 是英文标题，
中文客户端用的是 `第七史诗`。

好消息是配置**同时覆盖了两种**：`第七史诗` 与标题完全相等 → 覆盖率 100% → 通过。
而 §4.4 那个浏览器假阳性（覆盖率 11%）被正确拒绝。**两个用例在同一次运行里都验证到了。**

### 7.2 默认分辨率 1280×720，16:9 —— 符合预期

### 7.3 ⭐ 诊断出 `code:101 网络连接异常` 的根因：加速器的 TLS 中间人

> ⚠️ **本节结论后来被推翻了，见 §7.9。** 退出雷神后游戏**依然**报 `code:101`，
> 真因是 **STOVE 的地区校验（`stove_error_not_supported_country`）**，
> 由代理出口国（`device_nation`）决定。本节保留原样，是为了留下排查过程的完整轨迹 ——
> 它记录的观测本身没错（雷神确实装了根证书、确实做 TLS 中间人），
> 只是**不是本次故障的原因**。这也说明：现象对得上不等于因果成立。

用户反馈"游戏没反应"。诊断过程与结论：

**现象**：`doctor` 显示进程在跑、`Responding=True`、CPU 已用 419 秒、内存 817 MB；
画面**持续动画**（每秒 0.4%~1.5% 变化）——所以**不是卡死**。

**读屏幕**：用 `rapidocr` 直接 OCR 游戏画面，读出：

```
[0.99] (0.501,0.485)  网络连接异常，请重新连接。
[1.00] (0.501,0.645)  code:101
[0.99] (0.501,0.748)  点击重试
[0.90] (0.090,0.985)  App:1.0.987dR:0T:0M:0S:0P:0
```

**游戏停在网络错误提示页，在等用户点「点击重试」。**

**定位根因**（三条证据链）：

1. 游戏用 **libcurl**（exe 里有 `curl.se/docs/alt-svc.html` / `hsts.html` / `http-cookies.html`
   这些 libcurl 内建文档链接），并自带 `data.unpacked\ssl\cacert.pem`（**133 张证书**）。
   **它校验的是自己打包的 CA 列表，不是 Windows 证书库。**
2. 机器上装了 **雷神加速器**，它在 `C:\ProgramData\leigod_person_7002` 放了
   `ca.cer` + `certimport.exe` + NSS 库（`libnspr4.dll` / `freebl3.dll` / `libplc4.dll`），
   并把 **`CN=Leigod CA, OU=Leigod, O=Leigod, L=SH, C=CN`** 装进了 `LocalMachine\Root`。
   **它在做 TLS 中间人拦截。**
3. 两者一撞：雷神签发的证书**不在游戏的 133 张 CA 里** → TLS 校验失败 → `code:101`。

**为什么 CDN 反而下得动**：游戏目录的 inet 缓存里有 **2.9 MB** 的
`epic-down.game.playstove.com/.../bgm_ep0.mp3`（标题 BGM）——静态 CDN 走的是直连
（`Find-NetRoute` 显示该连接走 WiFi，没走 clash 的 TUN），所以能下；
而需要走加速器的那部分 API 流量被 MITM 掉了。

**修法**：**完全退出雷神加速器**（不是断开，是退出进程）。
若游戏仍需连韩服（`world_kor`），改用 **clash-verge + 韩国节点**——
clash/mihomo 是透明转发，**不做 TLS 中间人**，端到端 TLS 保持完整，
游戏的 CA 校验就能通过。

> 顺带一提：装了这种根证书意味着该加速器**能解密这台机器上所有走它代理的 HTTPS 流量**
> （网银、邮箱都算）。这是这类工具的固有代价，知道一下有好处。

### 7.4 ⚠️ 踩坑：手动截图前必须先把游戏切到前台

我第一次 OCR 读出来的是**我自己聊天窗口的文字**——因为 `mss`/DXGI 抓的是**屏幕**，
不是窗口；我一跑命令，终端就把游戏盖住了。

**这不是产品缺陷**：引擎里的 `ensure_foreground()` 每次操作前都会把游戏切回前台。
是我那个临时诊断脚本绕过了它。修复很简单：

```python
from e7bot.winutil import activate
activate(win.hwnd, settle=0.8)   # 手动截图/诊断前一定要做
```

切前台后立刻读到了正确的 `code:101` 画面。

### 7.5 ✅ 本项目在真实游戏上跑通了

这次诊断顺带证明了整条链路在真游戏上可用：

| 环节 | 结果 |
|---|---|
| `Config.find_game_window()` | ✅ 锁定 `第七史诗`（中文标题） |
| `ScreenGrabber`（mss 后端） | ✅ 抓到 1280×720 帧，方差 31.1 |
| `frame_diff_ratio` 冻结检测 | ✅ 正确判定"在动画，非卡死" |
| `activate()` 前台切换 | ✅ `is_foreground` 从 False 变 True |
| OCR 读屏 | ✅ 读出错误码与按钮位置 |

`点击重试` 按钮的归一化位置 **(0.501, 0.748)** 已经拿到——这正是
`run.py capture` 要采的那种模板。

### 7.6 ⭐ 两个必须修的真 bug（都由真机实测暴露）

#### bug 1：双显卡笔记本上 bettercam 永远用不了，静默退回慢后端

`bettercam` 在游戏运行时**一直没被选中**，自动降级到了 `mss`。查下去发现两层问题：

**表层**：`IndexError: list index out of range`。原因是
**DXGI 的输出是「按适配器」枚举的**，而 `monitor_index_of()` 返回的是
Windows 的**全局显示器索引**。实测的 DXGI 枚举：

```
Device[0] Output[0]: Res:(2048, 1152) Primary:True    ← NVIDIA RTX 3060 Laptop
Device[1] Output[0]: Res:(1707, 960)                  ← Intel UHD Graphics
```

**两块 GPU 各驱动一块屏，output 索引都从 0 开始**，而 Windows 索引是 0/1 全局的。
把 Windows 索引 1 当 `output_idx` 传进 `device_idx=0`（NVIDIA 只有 1 个输出）→ 越界。
**混合显卡笔记本极其常见，所以这个 bug 的影响面很大。**

**修法**：用「**DXGI 报告分辨率 × 显示缩放比 = Windows 物理分辨率**」反查正确输出。
实测数据正好印证：
`2048×1152 × 1.25 = 2560×1440`（125% 缩放）、`1707×960 × 1.5 = 2560×1440`（150% 缩放）
—— 两块屏 DPI 缩放还不一样。现在候选排序为 `[(1,0), (0,0), (0,None)]`，正确选到 Intel 那块。

新增 `winutil.monitor_rect_of()`（显示器物理矩形，比索引可靠）、
`capture.parse_output_info()` / `implied_scale()` / `bettercam_output_candidates()`，
以及 9 项回归测试（用**真实**的 `output_info()` dump 字符串）。

#### bug 2：游戏运行时 DXGI 桌面复制整体返回纯黑

修完索引后 bettercam 能创建了，但抓出来**方差 0.00 / 均值 0.00 —— 纯黑**。
而且**两块屏都黑**，包括**没跑游戏的那块主屏**。

对比证据：
- 游戏**没**运行时（`run.py smoke`）：bettercam 抓到方差 **94.9**、62 fps —— 正常
- 游戏**运行中**：两块 DXGI 输出都是纯黑

**结论：游戏（或其内核级反作弊 UNCHEATER）在运行时会屏蔽 DXGI Desktop Duplication。**
这不是我们代码的问题 —— 但意味着在这台机器上**必须依赖 `mss`**。

好消息是：**自动降级逻辑正确接管了**（`bettercam 疑似失效（黑屏），尝试切换 …` → 切到 mss），
mss 实测 **56~79 fps @1280×720**，对"每 2~3 秒识别一次场景"完全够用。

> 这正是当初把截图做成**多后端 + 黑屏自动降级**的价值所在：主后端被游戏屏蔽时，
> 脚本仍然能跑，而不是直接死在启动阶段。
>
> **后续可考虑**：加一个 **Windows Graphics Capture（WGC）** 后端。WGC 是 Win10 19041+
> 的现代截屏 API，走的是与 DDA 完全不同的路径，**有可能在被 DDA 屏蔽的环境下仍然可用**。
> 这需要引入 `windows-capture` 依赖，暂未实现。

### 7.7 本次实测新增/修正的清单

| 项 | 结论 |
|---|---|
| 窗口标题 | **中文 `第七史诗`**（按客户端语言动态设置），配置已覆盖 |
| 默认分辨率 | 1280×720，16:9，窗口化（有标题栏边框） |
| `find_game_window` | ✅ 正确锁定，且正确拒绝了浏览器假阳性 |
| 截图后端 | ⚠️ bettercam 被游戏屏蔽 → 自动降级 **mss**（可用，56~79 fps） |
| DXGI 输出映射 | 🔧 已修（双显卡按适配器枚举，索引对不上） |
| 游戏可玩性 | ❌ 卡在 `code:101`，根因是**雷神加速器的 TLS 中间人**（见 §7.3） |

### 7.8 服务器（World）结构 —— 想玩日服的人看这里

用户问能否连**日服**。查了客户端二进制 + 官方/韩媒公告，结论如下。

**客户端侧的证据**：

- exe 里 **只有 `world_kor` 这一个区域值**，且全 exe **只出现 1 次**，
  形式是 `&region=world_kor`；而同族的 URL 参数模板是 `&region=`（空值）。
  说明**区域是运行时传入的参数，`world_kor` 只是这个构建的默认/兜底值**。
- `BaseSDK.dll` 里有 `StoveAPI_GetWorld` / `Stove_Internal_GetWorld`，
  API 形如 `.../member/v1.0/character/member_no/{}?game_id={}&world_id={}`。
  **World 由 STOVE SDK 决定，不是游戏内设置。**
- `APIModule.dll` 里有 `HandleCommonPopupUnserviceableCountry` 和文案
  `"Some games may be unavailable depending on your region."`，
  以及 `Stove_IStoveSignin_GetRegisteredCountryCode`
  —— **区域与 STOVE 账号的注册国家绑定**。
- **客户端里搜不到任何"选服/选世界"相关的键**（无 `select_world`、
  无 `server_list`、无 `choose_server`）。

**官方/媒体侧的结论**（[게임동아 2026-10-08](https://game.donga.com/124602/)）：

> 스팀 정식 서비스는 오는 29일부터 시작되며, 같은 날 신규 서버 **'파운드리'**도 문을 연다.
> 파운드리 서버는 스팀뿐 아니라 PC 클라이언트 스토브와 모바일에서도 접속할 수 있다.
> **기존 계정으로 스팀에 접속하는 것도 가능하다.** 이용자는 기존 계정 정보를 그대로
> 활용해 스팀에서 게임을 이어서 즐길 수 있다.

翻译：**10/29 Steam 正式上线，同日开放新服「Foundry」（Steam/STOVE/手机三端互通）；
并且「可以用现有账号登录 Steam 继续玩」**。

**因此**：

| 想做什么 | 可行吗 |
|---|---|
| 用**已有的日服账号**在 Steam 上玩 | ✅ **可以** —— 官方明确支持现有账号登录 Steam 继承进度 |
| 把已有账号**转**到日服 | ❌ 不行 —— E7 的 World 在账号创建时确定，之后不可更改（官方规则，非本次实测） |
| 在 Steam 上**新建**账号并选日服 | ⚠️ **未确认**。客户端里没有选服 UI 的痕迹，World 由 STOVE SDK 按账号注册国家决定；日服官方站是 `epic7.onstove.com/ja/`，日服确实存在（[日服官方 X @Epic7_jp](https://x.com/Epic7_jp) 2026-08-22 官宣了 Steam 版） |
| 在**试用版（Demo）**里选日服 | ⚠️ 大概率不行 —— Demo 实测连的是 `world_kor`，且试用记录不继承到正式版 |

**对本项目的影响：无。** 各服的 UI 布局、按钮、图标一致，只是文案与活动不同。
模板是按**画面**采的，换服不用重采（若某服用了不同语言，纯文字模板可能要重采，
图标类不受影响）。

> ⚠️ 别忘了：**日服玩家要连的是日本节点**。如果用 clash-verge 加速，
> 记得把节点选到**日本**而不是韩国 —— 你之前那条连接去的是 `world_kor`（韩服）。
> 但无论哪个服，**都不能用做 TLS 中间人的加速器**（见 §7.3）。

### 7.9 ⭐⭐ `code:101` 的**真正**根因：STOVE 的地区校验，不是网络不通

§7.3 把 `code:101` 归因于雷神加速器的 TLS 中间人 —— **那个结论是错的**。
退出雷神、改用 clash 日本节点之后，游戏**依然** `code:101`。继续挖，才找到真因。

#### 决定性证据在 STOVE 自己的日志里

位置（这两处是关键，以后排查都从这儿开始）：

```
%LOCALAPPDATA%\STOVEPCCLIENTMODULE\logs\EpicSeven_Steam\APIModule_*.log
%LOCALAPPDATA%\STOVEPCSDK3\logs\STOVE_EPIC7\BaseSDK_*.log
```

`BaseSDK` 日志末尾：

```
07:47:27.435  nation : JP            timeZone : Asia/Tokyo  utcOffset : 540
07:47:27.436  nationality : SG       providerCode : STEAM_SHADOW
07:47:27.436  memberNo : 251212253   nickname : S1791609014528511
07:47:27.920  stringId=stove_error_not_supported_country,
              translate=当前国家不支持此功能。
07:47:27.920  ERROR This feature is not supported in the current country.
```

**`stove_error_not_supported_country` / 「当前国家不支持此功能」——
这就是 `code:101` 的来源。不是连不上，是地区校验没过。**

#### 关键：STOVE 的 `device_nation` 跟着**代理出口**走

APIModule 日志里那条策略请求：

```
https://api.onstove.com/ngds/v1.1/client/policy/total
    ?policy_grp=launcher&client_lang=zh&device_nation=SG
```

`device_nation=SG`（新加坡）—— 而用户当时的 clash 节点**名义上是日本，实际出口在新加坡**。
后来换到真正的日本出口后，公网 IP 变成 `Japan (Tokyo) / FDCservers.net`。

**所以：地区判定 = 代理出口 IP 的归属国，与账号无关。**
选错节点 → `device_nation` 错 → 地区不支持 → `code:101`。

#### 另一条重要证据：STOVE 层**全部成功**

APIModule 日志显示启动链路一条都没失败：

```
ServerConfigService::RequestSync Success      PublicIpService: HTTP 200
NGdsInformationService: code=0, message=OK    TranslateLanguageService Success (474896B)
GameCheckerForSteamService: code=0, Success   provider_cd=STEAM_SHADOW
FetchGameMetaService: code=0, message=OK      FetchGameInfoService: code=0, message=OK
```

**这直接排除了"网络不通"的假设** —— 如果线路有问题，这些请求会先失败。
它们全成功，说明网络通畅，问题在**业务层的地区校验**。

#### 还有一条线索：`[Restful] Proxy delegated to session AUTOMATIC_PROXY`

STOVE SDK 的 HTTP 会话**会自动读系统代理**（WinINET）。所以：
- 系统代理开着（clash 7897）→ STOVE 的请求走 clash → `device_nation` = clash 出口国
- 游戏本体（cocos2d-x + libcurl）**不走系统代理**（exe 里虽有 `http_proxy` 等串，但环境变量没设）
  → 两者走的线路**可能不一致**

#### 方法论收获

1. **先看应用自己的日志，再猜网络。** 我一开始从"连接 CloseWait / DNS 解析失败"往下推，
   推出"网络不通"的错误结论；而日志里一句话就写明了真因。
   **`%LOCALAPPDATA%\STOVE*\logs\` 是排查这个游戏的第一现场。**
2. **"能下 CDN 但连不上游戏服"这个现象具有误导性。** CDN 是 Akamai（全球有节点，直连就通），
   游戏 API 走的是另一条路。现象差异会让人以为是路由问题，实际是地区校验。
3. **代理的"节点名"不等于出口国。** 标着日本、出口在新加坡，直接导致 `device_nation=SG`。
   **验证方法**：`Invoke-RestMethod https://ip-api.com/json/` 看 `country`（注意 PowerShell
   默认走系统代理，所以"直连"那次其实也走了 clash —— 要测真直连得显式指定不走代理）。
