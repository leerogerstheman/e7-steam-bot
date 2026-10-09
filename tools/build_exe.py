#!/usr/bin/env python
"""把托盘 GUI 打包成单文件 exe。

    python tools/build_exe.py                # 正常打包
    python tools/build_exe.py --clean        # 先删掉 build/ dist/ 再打
    python tools/build_exe.py --no-copy      # 不复制 config/ templates/

产物：

    dist/e7bot-gui.exe      单文件，--windowed（无控制台），带 UAC 提权清单
    dist/config/            外部目录（不打进 exe，用户可改）
    dist/templates/         外部目录（用户往里面放自己采的模板）

为什么要用包装脚本而不是让用户直接敲 pyinstaller：

* 忘了 `--noconfirm` 会卡在交互式确认上；
* 打完包**必须**把 config/ 与 templates/ 复制到 exe 旁边 ——
  spec 里刻意没把它们打进 exe（用户要自己往里放模板图），
  漏掉这一步的话 exe 启动就找不到配置和模板；
* 需要打印最终产物路径和大小，让用户确认拿到的是什么东西。
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "e7bot-gui.spec"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
EXE_NAME = "e7bot-gui.exe"

# 必须跟着 exe 一起躺在 dist/ 里的外部目录（用户要能改）
EXTERNAL_DIRS = ("config", "templates")


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def check_pyinstaller() -> bool:
    """检查 PyInstaller 是否可用（用 find_spec，不去真的 import 它）。"""
    if importlib.util.find_spec("PyInstaller") is None:
        print("=" * 72)
        print("[!! ] 没有找到 PyInstaller。")
        print("=" * 72)
        print("先安装（用当前虚拟环境的解释器）：")
        print()
        print(f"    {sys.executable} -m pip install pyinstaller")
        print()
        print("或者：")
        print()
        print("    .\\.venv\\Scripts\\python.exe -m pip install pyinstaller")
        print()
        return False

    try:
        import PyInstaller  # noqa: PLC0415

        print(f"[OK ] PyInstaller {getattr(PyInstaller, '__version__', '?')}")
    except Exception as exc:  # noqa: BLE001
        print(f"[!! ] PyInstaller 存在但导入失败: {exc}")
        return False
    return True


def check_spec() -> bool:
    if not SPEC.exists():
        print(f"[!! ] 找不到 spec 文件: {SPEC}")
        return False
    entry = ROOT / "e7bot" / "gui.py"
    if not entry.exists():
        # spec 里用 SPECPATH 定位入口；这里提前查一次，省得 PyInstaller
        # 报一句难懂的 "script ... not found"
        print(f"[!! ] 找不到 GUI 入口脚本: {entry}")
        return False
    return True


def check_gui_deps() -> bool:
    """GUI 依赖缺失时提前报错 —— 否则打完包运行才发现是白忙一场。"""
    missing = [name for name in ("pystray", "PIL") if importlib.util.find_spec(name) is None]
    if missing:
        print(f"[!! ] 缺少 GUI 依赖: {', '.join(missing)}")
        print(f"     先执行: {sys.executable} -m pip install -r requirements-gui.txt")
        return False
    return True


def clean() -> None:
    for d in (BUILD, DIST):
        if d.exists():
            print(f"[i  ] 删除 {d}")
            shutil.rmtree(d, ignore_errors=True)


def run_pyinstaller() -> bool:
    """调用 `pyinstaller e7bot-gui.spec --noconfirm`。

    用 `python -m PyInstaller` 而不是裸 `pyinstaller`：前者一定用的是
    **当前这个虚拟环境**里的 PyInstaller，不会串到系统里的另一个版本。
    """
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(SPEC),
        "--noconfirm",
        "--distpath",
        str(DIST),
        "--workpath",
        str(BUILD),
    ]
    print("[i  ] 执行:", " ".join(cmd))
    print("-" * 72)
    # 直接继承 stdio：PyInstaller 输出很长，不需要在这里抓取
    proc = subprocess.run(cmd, cwd=str(ROOT))
    print("-" * 72)
    if proc.returncode != 0:
        print(f"[!! ] PyInstaller 退出码 {proc.returncode}，打包失败")
        return False
    return True


def copy_external_dirs() -> None:
    """把 config/ 与 templates/ 复制到 dist/ 旁边（已存在就跳过，不覆盖用户数据）。

    不覆盖是刻意的：用户可能已经往 dist/templates/ 里放了自己采的模板图，
    重新打包不该把它们冲掉。
    """
    for name in EXTERNAL_DIRS:
        src = ROOT / name
        dst = DIST / name
        if not src.exists():
            print(f"[!! ] 源目录不存在，跳过: {src}")
            continue
        if dst.exists():
            print(f"[i  ] {dst} 已存在，跳过（不覆盖你自己放进去的模板/配置）")
            continue
        try:
            shutil.copytree(src, dst)
            print(f"[OK ] 已复制 {src.name}/ -> {dst}")
        except Exception as exc:  # noqa: BLE001
            print(f"[!! ] 复制 {src} -> {dst} 失败: {exc}")


def smoke_test_exe() -> bool | None:
    """跑一次 `e7bot-gui.exe --selftest`，验证打包产物真的能启动。

    为什么必须要这一步：`--help` 这种用法**测不出打包问题** —— argparse 在第一次
    相对导入之前就退出了。真实踩过的坑：PyInstaller 把入口脚本当 `__main__` 跑，
    `__package__` 是 None，于是 `from .config import Config` 直接
    "attempted relative import with no known parent package"，
    而 `--help` 一切正常 —— 表现就是"exe 双击没反应"。
    `--selftest` 会真正走一遍配置载入 / 引擎导入 / 任务构建 / 互斥体。

    返回 True = 通过，False = 有失败项，None = 没法判定（被 UAC 拦下等），
    后一种情况只警告、不判定失败。
    """
    exe = DIST / EXE_NAME
    if not exe.exists():
        return None

    cmd = [str(exe), "--selftest"]
    print(f"[i  ] 产物自检: {' '.join(cmd)}")
    print("      （exe 带 UAC 提权清单，可能会弹一次 UAC；弹不出来就只能跳过）")
    try:
        proc = subprocess.run(cmd, cwd=str(DIST), capture_output=True, timeout=300)
    except subprocess.TimeoutExpired:
        # 超时通常不是"自检跑得慢"（它只做导入和路径检查，正常 <5 秒），
        # 而是**某处弹了模态对话框在等点击** —— 例如管理员权限警告、
        # 或历史上 `run_selftest` 失败时弹的 MessageBoxW（那个已修掉）。
        # 所以提示要指向这个方向，而不是让用户以为只是慢。
        print("[!! ] 自检超时（300s），跳过判定")
        print("      自检本身只做导入/路径/互斥体检查，正常几秒内就该结束。")
        print("      超时几乎一定是**有模态对话框在等点击**（UAC 提示 / 权限警告）。")
        print(f"      请手动执行 `{exe} --selftest` 看看到底卡在哪个弹窗上。")
        return None
    except OSError as exc:
        print(f"[!! ] 自检无法启动（{exc}），跳过判定 —— 很可能是 UAC 被拒绝")
        return None

    out = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()
    if out:
        print("-" * 72)
        print(out)
        print("-" * 72)
    if err:
        print("stderr:", err[:2000])

    if proc.returncode == 0:
        print("[OK ] 产物自检通过")
        return True
    if proc.returncode == 3:
        print("[!! ] 产物里缺少 pystray / Pillow")
        return False
    print(f"[!! ] 产物自检失败（退出码 {proc.returncode}）")
    return False


def report() -> int:
    exe = DIST / EXE_NAME
    print("=" * 72)
    if not exe.exists():
        print("[!! ] 没有找到预期的产物:", exe)
        print("     PyInstaller 可能改了输出名，检查上面的日志。")
        return 1

    size = exe.stat().st_size
    print("[OK ] 打包成功")
    print(f"     产物: {exe}")
    print(f"     大小: {human_size(size)} ({size} 字节)")
    print("=" * 72)
    print("使用方式：")
    print("  1. 把整个 dist/ 目录拷给用户（exe + config/ + templates/ 必须在一起）")
    print("  2. 双击 dist/e7bot-gui.exe —— 会弹 UAC，必须同意（否则键鼠事件会被丢弃）")
    print("  3. 右键托盘图标 -> 以 dry-run 模式启动，先确认模板识别正常")
    print("  4. 往 dist/templates/default/ 里放自己采的模板图，再正式启动")
    print()
    print("注意: templates/ 与 config/ 是**外部目录**，不要塞回 exe 里；")
    print("      运行时是按 exe 所在目录去找 config/default.toml 和 templates/ 的。")
    print()
    print("排错: dist/e7bot-gui.exe --selftest   # 不建托盘，检查依赖/路径/互斥体")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tools/build_exe.py",
        description="把 e7bot 托盘 GUI 打包成单文件 exe（PyInstaller onefile + UAC 提权）",
    )
    parser.add_argument("--clean", action="store_true", help="打包前删除 build/ 和 dist/")
    parser.add_argument("--no-copy", action="store_true", help="不复制 config/ templates/ 到 dist/")
    parser.add_argument("--no-smoke", action="store_true", help="跳过产物自检（不启动 exe）")
    args = parser.parse_args(argv)

    print("=" * 72)
    print("e7bot-gui 打包")
    print("=" * 72)
    print(f"[i  ] 项目根: {ROOT}")
    print(f"[i  ] Python : {sys.executable} ({sys.version.split()[0]})")

    if not check_spec():
        return 2
    if not check_pyinstaller():
        return 2
    if not check_gui_deps():
        return 2

    if args.clean:
        clean()

    if not run_pyinstaller():
        return 1

    if not args.no_copy:
        copy_external_dirs()

    rc = report()
    if rc != 0:
        return rc

    if not args.no_smoke:
        smoke = smoke_test_exe()
        if smoke is False:
            print("=" * 72)
            print("[!! ] 打包产物自检失败 —— 产物不可用，请检查上面的失败项。")
            return 1
        if smoke is None:
            print("[i  ] 产物自检被跳过，请手动执行: dist\\e7bot-gui.exe --selftest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
