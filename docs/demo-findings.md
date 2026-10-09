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

**下一步**（需要你在旁边）：

```powershell
# 1. 启动游戏（Steam 里点开始游戏），等到进入大厅
# 2. 以管理员打开终端，跑自检
python run.py doctor
#    -> 应能看到 "游戏窗口: 'EpicSeven (Steam)'"
#    -> 截图后端应显示 bettercam
# 3. 采模板 / 录制进本序列
python run.py capture        # 或 python run.py record
# 4. 验证识别
python run.py probe --duration 60
# 5. 干跑
python run.py run --dry-run
```
