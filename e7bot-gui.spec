# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：把托盘 GUI 打成单文件 exe。

用法（不要直接手敲 pyinstaller 参数，用包装脚本）：

    python tools/build_exe.py

或手动：

    pyinstaller e7bot-gui.spec --noconfirm

关键点，逐条解释"为什么"：

1. **onefile**（把 a.binaries / a.datas 直接塞进 EXE，不用 COLLECT）：
   用户拿到的是一个 exe，好分发。代价是每次启动要解压到 %TEMP%\\_MEIPASS。
2. **console=False（--windowed）**：托盘程序不需要黑框控制台。
   代价是 print / traceback 全部不可见，所以 e7bot/gui.py 里的
   致命错误一律走 MessageBoxW，日志一律落文件。
3. **uac_admin=True**：**硬性要求**。官方 PC 端以管理员运行，受 Windows UIPI
   限制，非提权进程发的键鼠事件会被静默丢弃（不报错，只是游戏毫无反应）。
   这会让 exe 带上 requestedExecutionLevel=requireAdministrator 清单，
   双击时弹 UAC。
4. **datas 里刻意不放 config/ 和 templates/**：
   这两个目录必须以**外部目录**形式躺在 exe 旁边，因为用户要自己往
   templates/ 里放采集的模板图。打进 exe 的话用户根本改不了。
   运行时路径解析见 e7bot/gui.py 的 app_root()：
   打包后取 sys.executable 所在目录，未打包时取项目根。
   （注意：不是 sys._MEIPASS —— 那是临时解压目录，退出即删。）
5. **hiddenimports**：PyInstaller 的静态分析看不到的模块，见下面注释。
"""

import sys
from pathlib import Path

# PyInstaller 会往 spec 的命名空间里注入 SPECPATH。
# 注意：SPECPATH 是 **spec 文件所在目录本身**（PyInstaller 内部等价于
# os.path.dirname(os.path.abspath(spec))），不是它的上一级 —— 本 spec 就放在
# 项目根，所以 SPECPATH 直接就是项目根。
ROOT = Path(SPECPATH).resolve()  # noqa: F821

ENTRY = ROOT / "e7bot" / "gui.py"
if not ENTRY.exists():  # pragma: no cover - 构建期保护
    raise SystemExit(
        f"[e7bot-gui.spec] 找不到入口脚本: {ENTRY}\n"
        f"（SPECPATH={SPECPATH!r}，请确认 spec 放在项目根目录下）"  # noqa: F821
    )

hiddenimports = [
    # pywin32 的常见坑：win32timezone 是运行时动态导入的，
    # 静态分析找不到，漏了它会在启动时报 ModuleNotFoundError: win32timezone。
    "win32timezone",
    "pywintypes",
    # pystray 的后端是按平台动态选的，Windows 上必须显式带上
    "pystray._win32",
    # 关于 "PIL._tkinter_finder"：它确实是 Pillow + PyInstaller 的历史坑
    # （负责定位 Tcl/Tk 库），但它模块顶层就 `import tkinter`，
    # 而托盘 GUI 完全不用 Tk，我们在 excludes 里排掉了 tkinter —— 两者互斥。
    # 本程序不 import PIL.ImageTk，所以这里**刻意不带上它**：
    # 带上它反而会把整个 Tcl/Tk 拖进 exe（约 +10 MB）而没有任何收益。
    # 如果哪天要在 exe 里跑 tools/capture_template.py（那个用 Tk 框选），
    # 就把下面这行取消注释，并把 excludes 里的 "tkinter" 删掉。
    # "PIL._tkinter_finder",
    # 显式带上引擎/任务包，避免某些版本对包内相对导入分析不全
    "e7bot",
    "e7bot.config",
    "e7bot.engine",
    "e7bot.tasks",
    "e7bot.winutil",
]

a = Analysis(  # noqa: F821
    [str(ENTRY)],
    pathex=[str(ROOT)],
    binaries=[],
    # 空 datas：config/ 与 templates/ 是外部目录，见文件头第 4 条
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 明显用不到的大块头，排掉能显著减小体积
    excludes=[
        "tkinter",
        "unittest",
        "pydoc",
        "doctest",
        "matplotlib",
        "pandas",
        "scipy",
        "IPython",
        "pytest",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="e7bot-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX 压过的 exe 更容易被杀软/反作弊误报，这里关掉
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,               # --windowed：托盘程序不要控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,              # 硬性要求：请求管理员权限
    icon=None,                   # 图标在运行时用 Pillow 现画，不依赖外部 .ico
)

if sys.platform != "win32":  # pragma: no cover
    raise SystemExit("e7bot-gui 只能打包到 Windows：本项目依赖 pywin32 / SendInput")
