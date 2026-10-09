"""实时场景探针 —— 看脚本"眼里的世界"。

用途：模板采完之后，靠它确认识别到底准不准。它会持续打印：
* 当前识别出的场景
* 所有场景锚点的匹配分数（按分数排序）
* 缺失的模板

分数语义：>= 0.86 基本就是命中了；0.6~0.86 说明"有点像但要调阈值/重采"；
< 0.5 说明当前画面根本没这个元素（正常）。

    python run.py probe --duration 60
"""

from __future__ import annotations

import time

from e7bot.capture import ScreenGrabber
from e7bot.config import Config
from e7bot.scene import SceneDetector
from e7bot.vision import Matcher, TemplateLibrary, annotate
from e7bot.winutil import (
    client_rect_screen,
    enable_dpi_awareness,
    monitor_index_of,
    window_scale_note,
)


def run_probe(cfg: Config, duration: float = 30.0, interval: float = 0.5,
              save_frames: bool = False) -> int:
    enable_dpi_awareness()

    win = cfg.find_game_window()
    if win is None:
        print("没找到第七史诗窗口。先启动游戏（Steam 版 / Demo）。")
        return 1

    grabber = ScreenGrabber(
        hwnd=win.hwnd,
        preferred=list(cfg.get("capture.backends", [])),
        monitor_index=monitor_index_of(win.hwnd),
        target_fps=int(cfg.get("capture.target_fps", 60)),
        verbose=False,
    )

    lib = TemplateLibrary(cfg.template_root(), str(cfg.get("templates.profile", "default")))
    matcher = Matcher(lib, float(cfg.get("templates.default_threshold", 0.86)))
    detector = SceneDetector(
        matcher,
        cfg.section("scenes"),
        threshold=float(cfg.get("templates.default_threshold", 0.86)),
        scale_tolerance=float(cfg.get("templates.scale_tolerance", 0.0)),
    )

    print("=" * 78)
    print(f"场景探针  |  窗口 {win.title!r}  |  后端 {grabber.backend_name}")
    print(f"模板库 {cfg.template_root() / lib.profile}  共 {len(lib)} 个模板")
    missing = detector.missing_templates()
    if missing:
        print(f"!! 缺失 {len(missing)} 个场景锚点模板: {', '.join(missing[:10])}"
              + (" …" if len(missing) > 10 else ""))
    print("=" * 78)

    out_dir = cfg.log_dir() / "probe"
    deadline = time.time() + duration
    last_line = ""

    try:
        while time.time() < deadline:
            rect = client_rect_screen(win.hwnd)
            frame = grabber.grab(rect)

            t0 = time.perf_counter()
            scores = detector.probe(frame, rect)
            dt = (time.perf_counter() - t0) * 1000
            scene = detector.detect(frame, rect, use_cache=False)

            ranked = sorted(
                ((k, v) for k, v in scores.items() if v >= 0),
                key=lambda kv: kv[1], reverse=True,
            )[:6]
            top = "  ".join(f"{k.split('::')[-1].split('/')[-1]}={v:.3f}" for k, v in ranked)

            line = (f"\r场景 {scene.name:<14} | {dt:6.1f}ms | {window_scale_note(rect)} | {top}")
            print(line.ljust(len(last_line))[:200], end="", flush=True)
            last_line = line

            if save_frames:
                stamp = time.strftime("%H%M%S")
                annotate(frame, rect, [m for m in [scene.matched] if m],
                         out_dir / f"{stamp}_{scene.name}.png")

            time.sleep(interval)
    except KeyboardInterrupt:
        print()
    finally:
        grabber.close()

    print()
    print("-" * 78)
    print("分数对照: >=0.86 命中 | 0.60~0.86 疑似(调阈值或重采) | <0.60 当前画面无此元素")
    print("若大量锚点都是 0.3 以下，通常是：分辨率没设成 16:9、模板采错界面、"
          "或截图后端抓到了黑屏。")
    return 0
