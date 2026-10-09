# 第七史诗 Epic Seven — PC/Steam 客户端技术调研

调研日期 2026-10-10。标注：**✅已确认**（有官方/一手来源）／**⚠️推测**／**❌未查证到**

## 1. Steam 版上线信息
- ✅ **尚未上线，定档 2026-10-29**（Steam 官方 API 返回 `coming_soon: true`）。AppID **5019180**（免费）。Demo AppID **5129800**（Steam Next Fest，10/8–10/27；**试玩进度不继承**）。
- ✅ **跨平台账号互通**。商店页字段 `ext_user_account_notice`: "STOVE (Supports Linking to Steam Account)"。韩媒：现有账号可直接在 Steam 游玩；新服务器「Foundry (파운드리)」在 **Steam / STOVE / 手机三端互通**。
- ⚠️ 与官方 STOVE PC 客户端**系统需求逐条完全一致**（见 §3），高度指向同一构建；但官方未明说"同一版本"，故仅标推测。

## 2. 反作弊（最关键）
- ✅ **Steam 版使用内核级反作弊 UNCHEATER**。Steam 商店页明确标注 `Uses Kernel Level Anti-Cheat: UNCHEATER`（Valve 官方字段）。UNCHEATER 为 Wellbia 产品（与 XIGNCODE3 同厂）。
- ⚠️ **官方 STOVE PC 客户端**：社区证据（NGA 帖「[国际服]求助 pc 端一进游戏就显示 uncheater」）显示同样加载 UNCHEATER；官方文档 ❌未查证到。
- ✅ **不使用 VAC**：商店页无 VAC 标识，全部来源均未见 VAC。
- ⚠️ **对脚本的影响**：UNCHEATER 是内核驱动，具备进程/内存扫描能力。**内存读写、DLL 注入、Hook 一律视为高危**（封号风险）。纯「截图＋模拟输入」的外部脚本风险显著更低，但内核驱动通常也枚举窗口与输入来源，**无法保证安全**。

## 3. 客户端技术要求
- ✅ 系统需求（官方 STOVE 页与 Steam 页一致）：最低 Win10 64-bit / i5 / GTX 1060 / 8GB / **DX11** / 10GB；推荐 i7 / GTX 1660 / 16GB。
- ❌ **支持分辨率列表：未查证到官方清单**。仅确认 **1920×1080 为事实基准**（社区工具要求游戏内设为 1920×1080 才能稳定模板匹配）。
- ❌ 窗口化 / 无边框 / 全屏独占的官方说明**未查证到**。NGA 帖「PC 版有什么办法能自由修改尺寸吗」暗示尺寸调整受限，但页面有登录墙，未能取证 → ⚠️。

## 4. 输入方式
- ✅ **鼠标可完成全部操作**：Steam 分类含 `Mouse Only Option`（id 76）；另有 Partial Controller Support、DualShock/DualSense。
- ✅ 键盘有用途：社区自动化工具以 **ESC** 作中止热键（游戏内 ESC 通常呼出菜单）。
- ⚠️ **Windows UI Automation 大概率不可用**：E7 为自研引擎自绘 UI，❌未查证到任何 UIA 支持证据；社区成熟方案（E7-helper）**全部采用截图＋模板匹配＋鼠标点击**。综合判定 UIA 不可识别。

## 5. 游戏引擎
- ✅ **YUNA Engine**，Super Creative / Smilegate **自研**（Smilegate 官方 newsroom 专访确认，开发约 3 年）。原生 C++（Android 侧 `libur.so`）。**非 Unity、非 Unreal**。
- ✅ 渲染 API：**DirectX 11**。
- ⚠️ PC 客户端是否同为 YUNA 移植：❌无官方说明，推测是同一引擎的 Windows 移植。
- 影响：无 Unity/UE 的自动化插件可用；**截图必须走 GPU 侧（DDA/WGC）而非 GDI**。

## 6. 多开 / 防多开
- ❌ **未查证到**官方多开政策或防多开机制。Steam 支持 Family Sharing，但不等于多开。内核级反作弊常见做法是限制单实例，**需实测**。

## 7. PC 端 UI 与手机端差异
- ❌ 官方无对比说明。⚠️ 从社区工具看：UI 以 **16:9 / 1920×1080** 为基准，非 16:9 会黑边或整体缩放，**模板坐标必然失配**。1080p 下按钮相对位置与手机横屏布局一致度较高（推测）。

## 8. 云存档 / VAC
- ✅ **不使用 VAC**（见 §2）。
- ❌ **Steam 云存档未查证到**：商店页未列 Steam Cloud；存档由 STOVE 账号承载（服务器端），故"云存档"实质由厂商服务器提供。

---

# 附：Windows 游戏自动化最佳实践

## 截图
| 方案 | 结论 |
|---|---|
| **DXcam / BetterCam**（Desktop Duplication API） | ✅ **首选**。BetterCam README：可"捕获 Direct3D 独占全屏应用而不打断，即使 alt+tab"，自动处理拉伸分辨率；实测平均 **238.79 FPS**。BetterCam 是 DXcam 的活跃维护分支。 |
| **mss**（GDI/BitBlt） | ⚠️ 可用但慢（同基准 75.87 FPS，D3DShot 118.36）。BitBlt 不捕获 DX 独占全屏表面，独占全屏下易黑屏（推测）。 |
| **win32gui.PrintWindow** | ⚠️ 对 GPU 合成/DX 窗口常返回空白；Microsoft 自家工具在无 WGC 时降级用它，并需"blank-frame recovery"。 |
| **Windows.Graphics.Capture** | ✅ 可捕获**被遮挡**/GPU 合成窗口（Microsoft 官方确认），需 Win10 19041+。 |

- ✅ **后台截图**：**遮挡可行**（WGC 官方支持；DDA 抓整屏亦不受遮挡影响）。**最小化基本不可行**——WGC 对最小化窗口实测返回全黑（trycua/cua issue #1973："minimized window returns ~300-byte all-black PNG"）。
- ✅ **实测约束**（E7-helper README）："必须保持显示器常亮，程序依赖显示器活动才能截图" → 显示器关闭／锁屏时截图失败。

## 输入
- ✅ **`pydirectinput` 优于 `pyautogui`**。其 README：pyautogui 用虚拟键码＋已废弃的 `mouse_event`/`keybd_event`，"在依赖 DirectX 的游戏等应用中可能无效"；pydirectinput 改用 **DirectInput 扫描码＋`SendInput()`**，作者以 Half-Life 2 实测 DirectX 可用。限制：未实现滚轮、拖拽、hotkey。
- ❌ **`SendMessage`/`PostMessage` 对游戏无效**：Microsoft 官方文档指出 post-message 是投递到**窗口消息队列**，对不使用消息队列的控件无效（如 WinUI3/XAML windowless 控件"neither literal text nor named keys reach them"）。DirectX 游戏普遍用 **Raw Input / GetAsyncKeyState** 直接读硬件状态，不消费 `WM_KEYDOWN` → ⚠️推测（机制证据充分，无 E7 专项实测）。
- ✅ **管理员权限：官方 PC 客户端必须提权**。E7-helper README 明确："使用新的 PC 客户端时，**必须以管理员权限运行程序**"。根因是 **UIPI**——Microsoft 官方："Your application can only send commands to applications of the same or lower elevation"。即游戏若以管理员运行，脚本不提权则输入被静默丢弃。
- ✅ **必须解锁的交互式桌面**：Microsoft 官方——注入式输入（click/send-keys）需要 "unlocked, interactive desktop"；锁屏或安全桌面（UAC/LogonUI）以 `no_interactive_desktop` 快速失败。

## 窗口内相对坐标映射（含 DPI）
关键：**先设 DPI awareness，再取坐标**。Microsoft 官方：DPI-unaware 进程的 API 返回值会被**虚拟化**（按 96 DPI 折算），导致点击偏移。

```python
import ctypes, win32gui
# 必须在任何窗口/坐标 API 之前执行
try:    # Per-Monitor V2
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)

def client_origin(hwnd):
    """客户区左上角 -> 屏幕物理像素绝对坐标"""
    l, t, r, b = win32gui.GetClientRect(hwnd)      # 物理像素(已感知 DPI)
    sx, sy = win32gui.ClientToScreen(hwnd, (l, t))
    return sx, sy, r - l, b - t                    # 原点 + 客户区宽高

def to_screen(hwnd, tx, ty, base_w=1920, base_h=1080):
    """模板坐标(相对客户区) -> 屏幕绝对坐标，按实际客户区缩放"""
    sx, sy, cw, ch = client_origin(hwnd)
    return sx + int(tx * cw / base_w), sy + int(ty * ch / base_h)
```
- ✅ `GetClientRect` 不含标题栏/边框，`ClientToScreen` 转屏幕坐标——最稳的原点来源。
- ⚠️ 不要用 `GetWindowRect` 当客户区（有边框/阴影会偏移）。
- ✅ 截图区域也应使用 `client_origin()` 的物理像素矩形传给 `bettercam.grab(region=...)`，保证与点击同坐标系。

## 推荐技术栈
1. 脚本**以管理员运行**（UIPI＋官方 PC 客户端要求）。
2. 截图：`bettercam`（DDA，`region=` 客户区），**显示器保持常亮**，不可最小化。
3. 定位：OpenCV 模板匹配（社区成熟做法），基准 1920×1080 / 16:9。
4. 输入：`pydirectinput`（SendInput 扫描码），不用 pyautogui。
5. 窗口：`win32gui.FindWindow` 按**精确标题**定位（E7-helper 强调标题须精确到空格）。
6. **红线**：UNCHEATER 为内核级反作弊，**不要做内存读写/注入**。

---

## 实际抓取成功的 URL
- https://store.steampowered.com/app/5019180/Epic_Seven/ （Steam 商店页；UNCHEATER、STOVE 账号字段）
- https://store.steampowered.com/api/appdetails?appids=5019180&l=english （官方 API；需求、分类、`coming_soon`）
- https://epic7.onstove.com/en/guide/download （官方 PC 客户端安装与系统需求）
- https://newsroom.smilegate.com/en/eng/Epic_Seven_YUNA_Engine_EN （官方：YUNA 引擎）
- https://www.khgames.co.kr/news/articleView.html?idxno=309154 （韩媒：10/29 上线、Foundry 服务器三端互通）
- https://m.ruliweb.com/news/read/204268 （韩媒：PC 客户端与手机数据全量互通）
- https://github.com/ruenocos/E7-helper （E7 自动化实测：需管理员权限、显示器须常亮）
- https://raw.githubusercontent.com/rdp/pydirectinput/master/README.md （输入原理与限制）
- https://raw.githubusercontent.com/glpc/BetterCam/main/README.md （DDA 能力与 FPS 基准）
- https://learn.microsoft.com/en-us/troubleshoot/power-platform/power-automate/desktop-flows/ui-automation/uipi-issues （UIPI 官方）
- https://learn.microsoft.com/en-us/windows/win32/hidpi/high-dpi-desktop-application-development-on-windows （DPI 虚拟化官方）
- https://raw.githubusercontent.com/microsoft/winappCli/main/docs/ui-automation.md （Microsoft：post-message vs send-input、交互式桌面要求）
- https://github.com/trycua/cua/issues/1973 （WGC 最小化窗口全黑实测）

**抓取失败**：SteamDB（Cloudflare 拦截）、PCGamingWiki（403）、NGA/贴吧（登录墙/验证码）、page.onstove.com 帖子（JS 渲染，正文为空）、StackOverflow 51613903（403）。这些站点信息仅通过搜索结果标题间接引用，已相应标注为 ⚠️。
