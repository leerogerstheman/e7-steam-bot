"""模板批量管理：健康检查 / 重命名 / 批量调阈值 / 去重 / 找未使用。

模板是这套脚本里**唯一需要人工维护**的资产，采多了以后会出这些问题：

* **腐烂**：游戏改版后某张模板匹配不上了，但脚本只在真正用到时才失败，
  你可能几周后才发现。-> `health` 主动体检
* **冗余**：同一个按钮在不同时间采了两遍。-> `dedupe` 找近似重复
* **僵尸**：采了但配置里从没引用过。-> `unused` 列出来
* **阈值不统一**：有的 0.86 有的 0.7，不知道哪个是对的。-> `threshold` 批量改
* **命名混乱**：`btn_ok` 和 `ok_button` 混用。-> `rename` 安全重命名（含 sidecar）

    python run.py templates list
    python run.py templates health            # 需要游戏在运行
    python run.py templates health --save     # 顺便存带标注的调试图
    python run.py templates unused
    python run.py templates dedupe
    python run.py templates threshold "battle/*" 0.82
    python run.py templates rename battle/btn_retry battle/btn_retry_stage
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from e7bot.capture import ScreenGrabber
from e7bot.config import Config
from e7bot.vision import Matcher, MatchOptions, TemplateLibrary, annotate
from e7bot.winutil import (
    GameNotFound,
    client_rect_screen,
    enable_dpi_awareness,
    monitor_index_of,
)


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #


def cmd_list(cfg: Config, args: argparse.Namespace) -> int:
    lib = TemplateLibrary(cfg.template_root(), str(cfg.get("templates.profile", "default")))
    if not len(lib):
        print(f"模板库是空的: {cfg.template_root() / lib.profile}")
        return 1

    rows = []
    for name in lib.names():
        t = lib.get(name)
        rows.append((name, f"{t.size[0]}x{t.size[1]}",
                     f"{t.meta.ref_size[0]}x{t.meta.ref_size[1]}",
                     f"{t.meta.threshold:.2f}",
                     "有" if t.meta.region else "-",
                     f"{t.meta.note[:38]}" if t.meta.note else ""))
    w = max(len(r[0]) for r in rows)
    print(f"{'模板':<{w}}  {'尺寸':<10}{'参考':<11}{'阈值':<7}{'区域':<5}备注")
    print("-" * (w + 48))
    for r in sorted(rows):
        print(f"{r[0]:<{w}}  {r[1]:<10}{r[2]:<11}{r[3]:<7}{r[4]:<5}{r[5]}")
    print(f"\n共 {len(rows)} 个模板   profile={lib.profile}   目录={cfg.template_root() / lib.profile}")

    missing_region = [r[0] for r in rows if r[4] == "-"]
    if missing_region:
        print(f"\n提示：{len(missing_region)} 个模板没有搜索区域(region)，"
              f"场景识别会退化成全屏搜索，明显变慢。")
        print("      重新用 `run.py capture` 采一遍会自动补上，或手动编辑同名 .json。")
    return 0


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #


def cmd_health(cfg: Config, args: argparse.Namespace) -> int:
    enable_dpi_awareness()
    win = cfg.find_game_window()
    if win is None:
        print("没找到第七史诗窗口。健康检查需要在游戏运行时做（要拿实时画面）。")
        return 4

    grabber = ScreenGrabber(
        hwnd=win.hwnd,
        preferred=list(cfg.get("capture.backends", [])),
        monitor_index=monitor_index_of(win.hwnd),
        target_fps=30,
        verbose=False,
    )
    lib = TemplateLibrary(cfg.template_root(), str(cfg.get("templates.profile", "default")))
    matcher = Matcher(lib)
    names = lib.names()
    if not names:
        print("模板库是空的")
        grabber.close()
        return 1

    rect = client_rect_screen(win.hwnd)
    print(f"健康检查 | 窗口 {win.title!r} | {rect.width}x{rect.height} | "
          f"后端 {grabber.backend_name} | {len(names)} 个模板")
    print("说明：一次检查只能反映**当前这一个界面**。要真正体检，请在每个界面各跑一次。")
    print()

    results = []
    frames = int(getattr(args, "frames", 3) or 3)
    for name in names:
        t = lib.get(name)
        scores = []
        best_match = None
        for _ in range(max(1, frames)):
            try:
                frame = grabber.grab(rect)
            except Exception as exc:  # noqa: BLE001
                print(f"抓帧失败: {exc}")
                grabber.close()
                return 1
            m = matcher.find(name, frame, rect, opts=MatchOptions(threshold=0.0))
            scores.append(m.score if m else 0.0)
            if m is not None and (best_match is None or m.score > best_match.score):
                best_match = m
        # 取中位数：单帧可能正好撞上动画/过渡
        scores.sort()
        median = scores[len(scores) // 2]
        spread = max(scores) - min(scores)
        results.append((name, median, t.meta.threshold, spread, best_match, frame))

    results.sort(key=lambda r: r[1])

    ok = [r for r in results if r[1] >= r[2]]
    weak = [r for r in results if 0.55 <= r[1] < r[2]]
    absent = [r for r in results if r[1] < 0.55]

    print(f"{'模板':<42}{'中位分':<10}{'阈值':<8}{'波动':<8}判定")
    print("-" * 96)
    for name, med, thr, spread, _m, _f in results:
        verdict = "通过" if med >= thr else ("偏低 ⚠" if med >= 0.55 else "当前界面无")
        flag = "  ← 波动大，画面有动态元素" if spread > 0.15 else ""
        print(f"{name:<42}{med:<10.3f}{thr:<8.2f}{spread:<8.3f}{verdict}{flag}")

    print()
    print(f"通过 {len(ok)} | 偏低 {len(weak)} | 当前界面无 {len(absent)}")
    if weak:
        print("\n偏低的最常见原因：模板带了背景、或采的时候画面有动画。")
        print("处理：用 run.py capture 重采（框紧贴元素），或适度调低该模板阈值。")
    if absent:
        print("\n「当前界面无」是正常的 —— 说明你现在不在那些界面。")
        print("要全面体检，请在各个界面（大厅/战斗/结算/商店）分别跑一次 health。")

    if getattr(args, "save", False):
        out_dir = cfg.log_dir() / "health"
        stamp = time.strftime("%H%M%S")
        matches = [r[4] for r in results if r[4] is not None]
        annotate(results[0][5], rect, matches, out_dir / f"{stamp}_health.png")
        print(f"\n已保存带标注的调试图: {out_dir / f'{stamp}_health.png'}")

    grabber.close()
    return 0 if not weak else 1


# --------------------------------------------------------------------------- #
# unused
# --------------------------------------------------------------------------- #


def cmd_unused(cfg: Config, args: argparse.Namespace) -> int:
    lib = TemplateLibrary(cfg.template_root(), str(cfg.get("templates.profile", "default")))
    names = lib.names()
    if not names:
        print("模板库是空的")
        return 1

    # 把配置里所有文本拼起来做子串匹配。配置里的模板名可能出现在
    # scenes.*.any/none、tasks.*.flow/enter_sequence 的 click/wait_for 等位置，
    # 全是纯文本，所以直接搜字符串最省事也最不容易漏。
    text = ""
    if cfg.source and cfg.source.exists():
        text = cfg.source.read_text(encoding="utf-8")
    else:
        from e7bot.config import dump_toml

        text = dump_toml(cfg.data)

    used, unused = [], []
    for n in names:
        (used if n in text else unused).append(n)

    print(f"被配置引用: {len(used)} 个")
    print(f"未被引用  : {len(unused)} 个")
    if unused:
        print()
        for n in unused:
            print(f"  {n}")
        print()
        print("未被引用不一定是垃圾 —— 可能是：")
        print("  * 被录制工具生成的 config/recorded_sequence.toml 引用（那个文件不在主配置里）")
        print("  * 你打算用但还没写进配置")
        print("  * 手动 dry-run 时用 --tasks 临时指定过")
        print("确认没用再删：删掉同名 .png 和 .json 即可。")
    return 0


# --------------------------------------------------------------------------- #
# dedupe
# --------------------------------------------------------------------------- #


def _signature(img: np.ndarray, size: int = 16) -> np.ndarray:
    """把图压成一个小灰度指纹，用于快速比较相似度。"""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    small = cv2.resize(g, (size, size), interpolation=cv2.INTER_AREA).astype(np.float32)
    small -= small.mean()
    n = float(np.linalg.norm(small))
    return small / n if n > 1e-6 else small


def cmd_dedupe(cfg: Config, args: argparse.Namespace) -> int:
    lib = TemplateLibrary(cfg.template_root(), str(cfg.get("templates.profile", "default")))
    names = lib.names()
    if len(names) < 2:
        print("模板太少，无需去重")
        return 0

    threshold = float(getattr(args, "similarity", 0.97) or 0.97)
    sigs = {}
    for n in names:
        t = lib.get(n)
        sigs[n] = (_signature(t.image), t.size)

    groups: list[list[str]] = []
    seen: set[str] = set()
    for i, a in enumerate(names):
        if a in seen:
            continue
        group = [a]
        for b in names[i + 1:]:
            if b in seen:
                continue
            # 尺寸差异大就不用比了（同一元素在不同分辨率下采的除外，那属于合法重复）
            sa, sb = sigs[a][1], sigs[b][1]
            if abs(sa[0] - sb[0]) > 2 or abs(sa[1] - sb[1]) > 2:
                continue
            sim = float(np.sum(sigs[a][0] * sigs[b][0]))
            if sim >= threshold:
                group.append(b)
                seen.add(b)
        if len(group) > 1:
            seen.add(a)
            groups.append(group)

    if not groups:
        print(f"没有发现相似度 ≥{threshold:.2f} 的重复模板")
        return 0

    print(f"发现 {len(groups)} 组近似重复（相似度 ≥{threshold:.2f}）：\n")
    for g in groups:
        print("  组:")
        for n in g:
            t = lib.get(n)
            print(f"    {n}   {t.size[0]}x{t.size[1]}  阈值 {t.meta.threshold:.2f}")
        print()
    print("处理建议：保留命名更规范的那个，其余删除。")
    print("注意：如果它们分别是不同界面上的**同名按钮**（比如不同弹窗的确定键），")
    print("      那就是合法的重复，不要删 —— 用不同的名字区分开更好。")
    return 0


# --------------------------------------------------------------------------- #
# threshold
# --------------------------------------------------------------------------- #


def cmd_threshold(cfg: Config, args: argparse.Namespace) -> int:
    lib = TemplateLibrary(cfg.template_root(), str(cfg.get("templates.profile", "default")))
    pattern = args.pattern or "*"
    value = float(args.value)
    if not (0.3 <= value <= 0.999):
        print("阈值应在 0.3 ~ 0.999 之间")
        return 2

    # 把 glob 转成正则（模板名里有 '/'，pathlib 的 glob 对它处理不直观）
    rx = re.compile("^" + re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".") + "$")
    targets = [n for n in lib.names() if rx.match(n)]
    if not targets:
        print(f"没有匹配 {pattern!r} 的模板")
        return 1

    if getattr(args, "dry_run", False):
        print(f"会修改 {len(targets)} 个模板的阈值 -> {value:.2f}：")
        for n in targets:
            print(f"  {n}  ({lib.get(n).meta.threshold:.2f} -> {value:.2f})")
        return 0

    for n in targets:
        t = lib.get(n)
        if t.path is None:
            continue
        t.meta.threshold = value
        t.meta.save(t.path)
    print(f"已把 {len(targets)} 个模板的阈值设为 {value:.2f}")
    return 0


# --------------------------------------------------------------------------- #
# rename
# --------------------------------------------------------------------------- #


def cmd_rename(cfg: Config, args: argparse.Namespace) -> int:
    lib = TemplateLibrary(cfg.template_root(), str(cfg.get("templates.profile", "default")))
    old, new = args.old, args.new
    if old not in lib:
        print(f"模板不存在: {old}")
        return 1
    if new in lib:
        print(f"目标名已存在: {new}")
        return 1

    root = cfg.template_root() / str(cfg.get("templates.profile", "default"))
    src_png = root / f"{old}.png"
    src_json = root / f"{old}.json"
    dst_png = root / f"{new}.png"
    dst_json = root / f"{new}.json"

    if not src_png.exists():
        print(f"找不到文件: {src_png}")
        return 1
    if dst_png.exists():
        print(f"目标文件已存在: {dst_png}")
        return 1

    dst_png.parent.mkdir(parents=True, exist_ok=True)
    src_png.rename(dst_png)
    if src_json.exists():
        src_json.rename(dst_json)

    # 提醒：配置里的引用不会自动改（怕误改），只报告出现次数
    text = ""
    if cfg.source and cfg.source.exists():
        text = cfg.source.read_text(encoding="utf-8")
    count = text.count(old)
    print(f"已重命名: {old} -> {new}")
    if count:
        print(f"\n注意：config 里还有 {count} 处引用了旧名字 {old!r}，需要手动改成 {new!r}。")
        for i, line in enumerate(text.splitlines(), 1):
            if old in line:
                print(f"  {cfg.source}:{i}  {line.strip()}")
    return 0


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run.py templates", description="模板批量管理")
    sub = p.add_subparsers(dest="action", required=True)

    sub.add_parser("list", help="列出所有模板").set_defaults(func=cmd_list)

    h = sub.add_parser("health", help="健康检查（需游戏运行中）")
    h.add_argument("--frames", type=int, default=3, help="每个模板采样几帧取中位数")
    h.add_argument("--save", action="store_true", help="保存带标注的调试图")
    h.set_defaults(func=cmd_health)

    sub.add_parser("unused", help="列出配置未引用的模板").set_defaults(func=cmd_unused)

    d = sub.add_parser("dedupe", help="查找近似重复的模板")
    d.add_argument("--similarity", type=float, default=0.97)
    d.set_defaults(func=cmd_dedupe)

    t = sub.add_parser("threshold", help="批量设置阈值")
    t.add_argument("pattern", help="模板名通配，如 'battle/*'")
    t.add_argument("value", type=float, help="新阈值 0.3~0.999")
    t.add_argument("--dry-run", action="store_true")
    t.set_defaults(func=cmd_threshold)

    r = sub.add_parser("rename", help="重命名模板（含 sidecar）")
    r.add_argument("old")
    r.add_argument("new")
    r.set_defaults(func=cmd_rename)

    return p


def main(cfg: Config, argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(cfg, args))


def run_templates(cfg: Config, argv: Optional[list[str]] = None) -> int:
    try:
        return main(cfg, argv)
    except GameNotFound as exc:
        print(f"[templates] {exc}")
        return 4
    except KeyboardInterrupt:
        print("\n已中断")
        return 0


if __name__ == "__main__":  # pragma: no cover
    _root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(_root))
    raise SystemExit(run_templates(Config.load(_root / "config" / "default.toml"),
                                  sys.argv[1:]))
