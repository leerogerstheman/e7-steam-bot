"""Steam 库定位：找到游戏装在哪、主程序叫什么。

为什么需要它：

* 用户常常不知道游戏装在哪个盘（Steam 库可以有好几个）；
* 我们要确认装的是**哪个 AppID** —— Steam 正式版是 5019180，Demo 是 5129800，
  两者主程序都叫 `EpicSeven_Steam.exe`，但内容不同；
* 排查"窗口找不到"时，第一件事就是确认**游戏到底装没装、装在哪、exe 叫什么**。

实现上刻意**不引入 vdf 解析库** —— `libraryfolders.vdf` 的格式足够简单，
一个正则就能取出所有库路径，而且 Valve 的格式偶尔会变，写死一个宽松的
正则比依赖第三方解析器更耐操。

只读：本模块只读取 Steam 的清单文件，不修改任何东西。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: Epic Seven 的 Steam AppID
APPID_FULL = "5019180"    # 正式版（2026-10-29 上线）
APPID_DEMO = "5129800"    # Demo（Steam Next Fest）

#: 已知的主程序名（实测自 Demo）
MAIN_EXE = "EpicSeven_Steam.exe"
#: 反作弊加载器（用于确认 UNCHEATER 是否随包存在）
ANTICHEAT_LOADER = "ucldr_Epic7_SM_loader_x64.exe"
ANTICHEAT_MODULE = "xnina_x64.xem"


@dataclass
class GameInstall:
    appid: str
    name: str
    library: Path
    installdir: Path
    size_on_disk: int = 0
    state_flags: int = 0

    @property
    def main_exe(self) -> Optional[Path]:
        p = self.installdir / MAIN_EXE
        return p if p.exists() else None

    @property
    def has_anticheat(self) -> bool:
        """包里是否带 UNCHEATER（加载器 + 模块）。"""
        return (self.installdir / ANTICHEAT_LOADER).exists() or \
               (self.installdir / ANTICHEAT_MODULE).exists()

    @property
    def fully_installed(self) -> bool:
        # StateFlags 4 = STATE_FULLY_INSTALLED
        return bool(self.state_flags & 4)

    @property
    def is_demo(self) -> bool:
        return self.appid == APPID_DEMO

    def describe(self) -> str:
        kind = "Demo" if self.is_demo else "正式版"
        mb = self.size_on_disk / 1024 / 1024
        lines = [
            f"AppID {self.appid}（{kind}）  {self.name}",
            f"  安装目录: {self.installdir}",
            f"  占用空间: {mb:,.0f} MB   状态: {'已完整安装' if self.fully_installed else f'StateFlags={self.state_flags}'}",
        ]
        exe = self.main_exe
        lines.append(f"  主程序  : {exe.name if exe else f'未找到 {MAIN_EXE}（安装可能不完整）'}")
        lines.append(f"  反作弊  : {'检测到 UNCHEATER 文件' if self.has_anticheat else '未检测到（可能被拆分到别处）'}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Steam 根目录 / 库路径
# --------------------------------------------------------------------------- #


def find_steam_root() -> Optional[Path]:
    """找 Steam 安装根目录。优先读注册表，失败则试常见路径。"""
    if sys.platform == "win32":
        try:
            import winreg

            for hive, key in (
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
            ):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        for value in ("SteamPath", "InstallPath"):
                            try:
                                raw, _ = winreg.QueryValueEx(k, value)
                                p = Path(str(raw))
                                if p.is_dir():
                                    return p
                            except FileNotFoundError:
                                continue
                except OSError:
                    continue
        except Exception:
            pass

    for cand in (
        Path(r"C:\Program Files (x86)\Steam"),
        Path(r"C:\Program Files\Steam"),
        Path(r"D:\Steam"),
        Path(r"D:\SteamLibrary"),
    ):
        if cand.is_dir():
            return cand
    return None


def list_library_folders(steam_root: Optional[Path] = None) -> list[Path]:
    """列出所有 Steam 库目录（含默认库）。

    `libraryfolders.vdf` 的格式形如：

        "libraryfolders"
        {
            "0" { "path"  "C:\\\\Program Files (x86)\\\\Steam" }
            "1" { "path"  "F:\\\\SteamLibrary" }
        }

    只抓 `"path"` 后面的字符串就够了，不需要完整 VDF 解析。
    """
    root = steam_root or find_steam_root()
    if root is None:
        return []

    libs: list[Path] = []
    vdf = root / "steamapps" / "libraryfolders.vdf"
    if vdf.exists():
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
            # VDF 里的反斜杠是转义的，Path 需要还原
            for m in re.finditer(r'"path"\s*"([^"]+)"', text):
                p = Path(m.group(1).replace("\\\\", "\\"))
                if p.is_dir() and p not in libs:
                    libs.append(p)
        except Exception:
            pass

    if root not in libs:
        libs.insert(0, root)
    return libs


# --------------------------------------------------------------------------- #
# 按 AppID 找安装
# --------------------------------------------------------------------------- #


def _read_acf(path: Path) -> dict[str, str]:
    """读 appmanifest_*.acf，返回扁平键值（只取顶层简单键）。"""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return out
    for m in re.finditer(r'"([A-Za-z_]+)"\s*"([^"]*)"', text):
        out.setdefault(m.group(1), m.group(2))
    return out


def find_install(appid: str, steam_root: Optional[Path] = None) -> Optional[GameInstall]:
    for lib in list_library_folders(steam_root):
        acf = lib / "steamapps" / f"appmanifest_{appid}.acf"
        if not acf.exists():
            continue
        kv = _read_acf(acf)
        installdir_name = kv.get("installdir", "")
        if not installdir_name:
            continue
        return GameInstall(
            appid=appid,
            name=kv.get("name", "?"),
            library=lib,
            installdir=lib / "steamapps" / "common" / installdir_name,
            size_on_disk=int(kv.get("SizeOnDisk", 0) or 0),
            state_flags=int(kv.get("StateFlags", 0) or 0),
        )
    return None


def find_epic_seven(steam_root: Optional[Path] = None) -> Optional[GameInstall]:
    """找 Epic Seven 安装。正式版优先，没有就找 Demo。"""
    return find_install(APPID_FULL, steam_root) or find_install(APPID_DEMO, steam_root)


def find_all(steam_root: Optional[Path] = None) -> list[GameInstall]:
    out: list[GameInstall] = []
    for appid in (APPID_FULL, APPID_DEMO):
        inst = find_install(appid, steam_root)
        if inst is not None:
            out.append(inst)
    return out
