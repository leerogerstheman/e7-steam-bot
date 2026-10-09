#!/usr/bin/env python
"""e7bot 命令行入口。

    python run.py doctor                    环境自检（管理员/DPI/窗口/截图后端/模板）
    python run.py smoke                     硬件冒烟测试（真抓屏 + 真发输入，只移动鼠标）
    python run.py capture                   交互式模板采集（对着游戏框选）
    python run.py record                    录制回放：手动操作一遍自动生成进本序列
    python run.py probe                     实时场景探针（看脚本"眼里的世界"）
    python run.py templates list|health|unused|dedupe|threshold|rename
    python run.py alert-test                测试告警渠道能否真的送到
    python run.py report                    运行统计报表
    python run.py run --dry-run             只识别不点击，验证模板
    python run.py run                       正式运行
    python run.py gui                       系统托盘 GUI
    python run.py selftest                  离线自检（合成图像，不需要游戏）

一定要先 `doctor` -> `capture`/`record` -> `probe` -> `run --dry-run` -> `run`。
直接 `run` 是最容易出事的用法。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from e7bot import Config, __version__  # noqa: E402
from e7bot.winutil import enable_dpi_awareness  # noqa: E402

DEFAULT_CONFIG = ROOT / "config" / "default.toml"


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #


def setup_logging(cfg: Config, verbose: bool = False) -> None:
    level_name = "DEBUG" if verbose else str(cfg.get("logging.level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)

    log_dir = cfg.log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-18s %(message)s", datefmt="%H:%M:%S"
    )
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    from logging.handlers import RotatingFileHandler

    fh = RotatingFileHandler(
        log_dir / "e7bot.log", maxBytes=4 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    logging.getLogger("e7bot").info("日志目录: %s", log_dir)


def load_config(path: str | None) -> Config:
    p = Path(path) if path else DEFAULT_CONFIG
    if not p.exists() and p == DEFAULT_CONFIG:
        logging.getLogger("e7bot").warning("未找到 %s，使用内置默认配置", p)
        return Config()
    cfg = Config.load(p)
    problems = cfg.validate()
    for prob in problems:
        logging.getLogger("e7bot").warning("配置问题: %s", prob)
    return cfg


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #


def cmd_doctor(args) -> int:
    from e7bot.capture import ScreenGrabber
    from e7bot.engine import is_admin
    from e7bot.winutil import dpi_mode, list_monitors, monitor_index_of

    cfg = load_config(args.config)
    ok = True

    print("=" * 72)
    print(f"e7bot {__version__} 环境自检")
    print("=" * 72)

    # 1) 管理员
    admin = is_admin()
    print(f"[{'OK ' if admin else '!! '}] 管理员权限: {admin}")
    if not admin:
        print("     实测：EpicSeven_Steam.exe 的清单是 asInvoker，游戏本身**不请求**提权，")
        print("     所以理论上不是必须。但仍强烈建议提权，因为：")
        print("       * Steam 可能以管理员运行 -> 游戏继承高完整性级别 -> 不提权的脚本")
        print("         发的键鼠事件会被 UIPI 静默丢弃（'点了没反应'最常见的根因）")
        print("       * UNCHEATER 是内核级驱动，其安装/加载环节需要提权")
        print("     提权无副作用：高完整性进程向低完整性窗口发输入不受限制。")

    # 2) DPI
    enable_dpi_awareness()
    print(f"[OK ] DPI 感知模式: {dpi_mode()}")

    # 3) Python / 依赖
    print(f"[OK ] Python: {sys.version.split()[0]} ({sys.executable})")
    for mod in ("cv2", "numpy", "win32gui", "bettercam", "mss", "PIL"):
        try:
            __import__(mod)
            print(f"[OK ] 依赖 {mod}")
        except Exception as exc:  # noqa: BLE001
            print(f"[!! ] 依赖 {mod} 缺失: {exc}")
            if mod in ("cv2", "numpy", "win32gui"):
                ok = False

    # 4) 显示器
    print("-" * 72)
    for i, rect in list_monitors():
        print(f"[i  ] 显示器 {i}: {rect.width}x{rect.height} @ ({rect.left},{rect.top})")

    # 4.5) 游戏安装（Steam 库）
    print("-" * 72)
    from e7bot.steamlib import APPID_DEMO, APPID_FULL, find_all

    installs = find_all()
    if not installs:
        print(f"[i  ] 未在 Steam 库里找到 Epic Seven（AppID {APPID_FULL} 正式版 / {APPID_DEMO} Demo）")
        print("     装了但没找到？用 `run.py doctor --game-dir <路径>` 手动指定。")
    else:
        for inst in installs:
            print("[OK ] 找到游戏安装:")
            for line in inst.describe().splitlines():
                print(f"     {line}")
            if not inst.fully_installed:
                print("     !! 状态不是「已完整安装」，可能还在下载/更新")
            if inst.main_exe is None:
                print("     !! 没找到主程序，安装可能不完整")

    # 5) 窗口
    print("-" * 72)
    win = cfg.find_game_window()
    if win is None:
        print("[!! ] 没找到第七史诗窗口。请先启动游戏（Steam 版 / Demo）。")
        print("     可用 `python run.py doctor --list-windows` 看当前所有窗口标题。")
        if getattr(args, "list_windows", False):
            from e7bot.winutil import list_windows

            for w in list_windows():
                print(f"      {w.client.width}x{w.client.height}  {w.title!r}  [{w.exe}]")
        ok = False
    else:
        print(f"[OK ] 游戏窗口: {win.title!r}")
        print(f"     客户区: {win.client.width}x{win.client.height} @ "
              f"({win.client.left},{win.client.top})  pid={win.pid}")
        mon = monitor_index_of(win.hwnd)
        print(f"     所在显示器索引: {mon}  (config: capture.monitor_index = {mon})")
        if mon != int(cfg.get("capture.monitor_index", 0)):
            print(f"     !! 建议把 capture.monitor_index 改成 {mon}")

        # 6) 截图后端实测
        print("-" * 72)
        try:
            g = ScreenGrabber(
                hwnd=win.hwnd,
                preferred=list(cfg.get("capture.backends", [])),
                monitor_index=mon,
                target_fps=int(cfg.get("capture.target_fps", 60)),
            )
            import time as _t

            t0 = _t.perf_counter()
            n = 10
            img = None
            for _ in range(n):
                img = g.grab(win.client)
            dt = (_t.perf_counter() - t0) / n
            print(f"[OK ] 截图后端: {g.backend_name}  平均 {dt*1000:.1f} ms/帧 "
                  f"(≈{1/dt:.0f} fps)  画面 {img.shape[1]}x{img.shape[0]}")
            out = ROOT / "logs" / "doctor_capture.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            import cv2

            cv2.imencode(".png", img)[1].tofile(str(out))
            print(f"     已保存样例截图: {out}")
            g.close()
        except Exception as exc:  # noqa: BLE001
            print(f"[!! ] 截图失败: {exc}")
            print("     检查: 显示器是否已开启？游戏窗口是否可见（未最小化）？")
            ok = False

    # 7) 模板
    print("-" * 72)
    from e7bot.vision import TemplateLibrary

    lib = TemplateLibrary(cfg.template_root(), str(cfg.get("templates.profile", "default")))
    print(f"[{'OK ' if len(lib) else '!! '}] 模板库: {cfg.template_root()} "
          f"(profile={lib.profile}) 共 {len(lib)} 个模板")
    from e7bot.scene import SceneDetector
    from e7bot.vision import Matcher

    det = SceneDetector(Matcher(lib), cfg.section("scenes"))
    missing = det.missing_templates()
    if missing:
        print(f"[!! ] 缺少 {len(missing)} 个场景锚点模板，脚本无法识别场景：")
        for m in missing[:20]:
            print(f"       - {m}")
        print("     用 `python run.py capture` 从真实客户端采集。")
        ok = False
    else:
        print("[OK ] 所有场景锚点模板齐备")

    # 8) 配置
    print("-" * 72)
    problems = cfg.validate()
    if problems:
        for p in problems:
            print(f"[!! ] 配置: {p}")
        ok = False
    else:
        print("[OK ] 配置校验通过")

    # 9) 扩展子系统
    print("-" * 72)

    from e7bot.alerts import AlertManager

    mgr = AlertManager(cfg, cfg.log_dir())
    channels = mgr.active_channels()
    print(f"[OK ] 告警渠道: {', '.join(channels) if channels else '（无）'}")
    if not cfg.get("alerts.enabled", True):
        print("     告警总开关是关的（alerts.enabled = false）")
    elif channels == ["log"]:
        print("     只有日志渠道 —— 无人值守时你收不到通知。")
        print("     建议开 [alerts.sound]，或在 [alerts.ntfy] 填 topic 用手机收推送。")
        print("     配好后用 `python run.py alert-test` 验证。")

    try:
        stats_dir = cfg.log_dir() / "stats"
        stats_dir.mkdir(parents=True, exist_ok=True)
        probe = stats_dir / ".write_test"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        print(f"[OK ] 统计目录可写: {stats_dir}")
    except Exception as exc:  # noqa: BLE001
        print(f"[!! ] 统计目录不可写: {exc}")

    if cfg.get("ocr.enabled", False):
        try:
            from e7bot.ocr import DigitReader

            reader = DigitReader(Matcher(lib), prefix=str(cfg.get("ocr.digits_prefix", "ocr/digits")))
            if reader.is_available():
                print("[OK ] OCR 数字字形齐备")
            else:
                print(f"[!! ] OCR 已启用但缺字形: {', '.join(reader.missing_glyphs()[:12])}")
                print("     用 `python run.py capture` 采 ocr/digits/0.png ~ 9.png")
                ok = False
        except ImportError:
            print("[!! ] OCR 已启用但 e7bot/ocr.py 不可用")
            ok = False
    else:
        print("[i  ] OCR 未启用（[ocr] enabled = false）")

    print("=" * 72)
    print("自检结果:", "通过，可以 `python run.py capture` 或 `record` 了" if ok else "存在问题，见上方 !! 项")
    return 0 if ok else 1


def cmd_capture(args) -> int:
    cfg = load_config(args.config)
    from tools.capture_template import run_capture_ui

    return run_capture_ui(cfg)


def cmd_probe(args) -> int:
    cfg = load_config(args.config)
    from tools.probe import run_probe

    return run_probe(cfg, duration=args.duration, interval=args.interval)


def cmd_run(args) -> int:
    from e7bot import Bot, GameNotFound, StopRequested, SafetyViolation
    from e7bot.tasks import build_tasks

    cfg = load_config(args.config)
    setup_logging(cfg, verbose=args.verbose)

    log = logging.getLogger("e7bot")
    log.info("e7bot %s 启动", __version__)

    if args.tasks:
        cfg.data["tasks"]["active"] = args.tasks.split(",")

    problems = cfg.validate()
    if problems:
        for p in problems:
            log.error("配置问题: %s", p)
        return 2

    try:
        tasks = build_tasks(cfg)
    except KeyError as exc:
        log.error("%s", exc)
        return 2
    if not tasks:
        log.error("没有启用任何任务。检查 tasks.active 与各任务的 enabled 字段。")
        return 2

    bot = Bot(cfg, dry_run=args.dry_run, profile=args.profile)
    try:
        bot.start()
        bot.run(tasks, tick_interval=args.tick, max_ticks=args.max_ticks)
    except StopRequested as exc:
        log.info("已停止: %s", exc)
        return 0
    except GameNotFound as exc:
        log.error("%s", exc)
        return 4
    except SafetyViolation as exc:
        log.error("安全停机: %s", exc)
        return 3
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C，退出")
        bot.stop()
        return 0
    except Exception as exc:  # noqa: BLE001
        log.exception("致命错误: %s", exc)
        bot.stop()
        return 1
    return 0


def cmd_selftest(args) -> int:
    from tools.selftest import run_selftest

    return run_selftest(verbose=args.verbose)


def cmd_smoke(args) -> int:
    from tools.smoke_hardware import main as smoke_main

    return smoke_main()


def cmd_record(args) -> int:
    cfg = load_config(args.config)
    from tools.record import run_record

    return run_record(
        cfg,
        hotkey=args.hotkey,
        duration=args.duration,
        threshold=args.threshold,
        prefix=args.prefix,
        out_config=args.out,
        start_immediately=args.no_wait,
    )


def cmd_templates(args) -> int:
    cfg = load_config(args.config)
    from tools.template_admin import run_templates

    return run_templates(cfg, args.rest)


def cmd_report(args) -> int:
    from e7bot import stats as S

    cfg = load_config(args.config)
    path = S.default_path(cfg.log_dir())

    if args.prune:
        removed = S.prune_old_events(path, float(cfg.get("stats.keep_days", 90)))
        print(f"已清理 {removed} 条超过 {cfg.get('stats.keep_days', 90)} 天的事件")
        return 0

    events = S.read_events(path)
    if not events:
        print(f"没有事件记录: {path}")
        print("先跑一次 `python run.py run`（或 `run --dry-run`）就会有数据了。")
        return 1

    events = S.filter_since(events, args.days)

    if args.sessions:
        rows = S.recent_sessions(events, limit=args.limit)
        if not rows:
            print("没有完整的运行记录（可能进程被强杀，只留下了 run_start）")
            return 0
        print(f"{'开始时间':<20}{'时长':<12}结束原因")
        print("-" * 70)
        for started, dur, reason in rows:
            m, sec = divmod(int(dur), 60)
            h, m = divmod(m, 60)
            dur_s = f"{h}h{m:02d}m" if h else f"{m}m{sec:02d}s"
            print(f"{started:<20}{dur_s:<12}{reason or '-'}")
        return 0

    md = S.build_markdown(events, title=args.title or "e7bot 运行报表")
    print(md)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(f"\n报表已写入: {out}")
    if args.csv:
        n = S.write_csv(events, Path(args.csv))
        print(f"CSV 已写入: {args.csv}（{n} 行）")

    print(S.cleanup_hint(float(cfg.get("stats.keep_days", 90))))
    return 0


def cmd_alert_test(args) -> int:
    from e7bot.alerts import AlertManager

    cfg = load_config(args.config)
    mgr = AlertManager(cfg, cfg.log_dir())

    print("=" * 70)
    print("告警渠道测试")
    print("=" * 70)
    channels = mgr.active_channels()
    print(f"启用的渠道: {', '.join(channels) if channels else '（无）'}")
    print()

    inactive = [n.name for n in mgr.notifiers if not n.available()]
    if inactive:
        print(f"未启用/不可用的渠道: {', '.join(inactive)}")
        print("（ntfy 需要填 topic；webhook 需要填 url；messagebox 默认关闭）")
        print()

    print("正在发送测试告警 …")
    results = mgr.test()
    ok = 0
    for name, success in results.items():
        print(f"  [{'OK ' if success else '!! '}] {name}")
        ok += success

    print()
    if ok:
        print(f"{ok}/{len(results)} 个渠道发送成功。")
        print("如果你没收到 ntfy 推送，检查 topic 名是否正确、手机是否订阅了同一个 topic。")
    else:
        print("没有任何渠道发送成功。请检查 config 的 [alerts] 段。")
    return 0 if ok else 1


def cmd_gui(args) -> int:
    try:
        from e7bot.gui import main as gui_main
    except ImportError as exc:
        print(f"托盘 GUI 不可用: {exc}")
        print("安装依赖: .\\.venv\\Scripts\\python.exe -m pip install -r requirements-gui.txt")
        return 1

    argv: list[str] = []
    if args.config:
        argv += ["--config", args.config]
    if args.dry_run:
        argv += ["--dry-run"]
    if args.tasks:
        argv += ["--tasks", args.tasks]
    return int(gui_main(argv))


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run.py",
        description="第七史诗 Steam / PC 端自动化脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--version", action="version", version=f"e7bot {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--config", "-c", help="配置文件路径 (默认 config/default.toml)")
        sp.add_argument("--verbose", "-v", action="store_true", help="DEBUG 日志")

    d = sub.add_parser("doctor", help="环境自检")
    common(d)
    d.add_argument("--list-windows", action="store_true", help="列出所有窗口标题")
    d.set_defaults(func=cmd_doctor)

    c = sub.add_parser("capture", help="交互式模板采集")
    common(c)
    c.set_defaults(func=cmd_capture)

    pr = sub.add_parser("probe", help="实时场景探针")
    common(pr)
    pr.add_argument("--duration", type=float, default=30.0, help="运行秒数")
    pr.add_argument("--interval", type=float, default=0.5, help="刷新间隔")
    pr.set_defaults(func=cmd_probe)

    r = sub.add_parser("run", help="运行任务")
    common(r)
    r.add_argument("--dry-run", action="store_true", help="只识别不点击（强烈建议先跑这个）")
    r.add_argument("--tasks", help="覆盖 tasks.active，逗号分隔")
    r.add_argument("--profile", help="覆盖模板 profile")
    r.add_argument("--tick", type=float, default=0.35, help="tick 间隔秒")
    r.add_argument("--max-ticks", type=int, default=0, help="跑多少 tick 后退出（0=不限）")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("selftest", help="离线自检（合成图像，无需游戏）")
    common(s)
    s.set_defaults(func=cmd_selftest)

    sm = sub.add_parser("smoke", help="硬件冒烟测试（真抓屏 + 真发输入，只移动鼠标不点击）")
    common(sm)
    sm.set_defaults(func=cmd_smoke)

    rc = sub.add_parser("record", help="录制回放：手动操作一遍，自动生成进本序列和模板")
    common(rc)
    rc.add_argument("--hotkey", default="f8", help="开始/结束录制的热键（默认 f8）")
    rc.add_argument("--duration", type=float, default=0.0, help="录制多少秒后自动结束（0=手动）")
    rc.add_argument("--threshold", type=float, default=0.86, help="判定唯一匹配的阈值")
    rc.add_argument("--prefix", default="seq", help="模板名前缀（默认 seq）")
    rc.add_argument("--out", help="生成的配置输出路径")
    rc.add_argument("--no-wait", action="store_true", help="不等待热键，立即开始录制")
    rc.set_defaults(func=cmd_record)

    tp = sub.add_parser(
        "templates",
        help="模板批量管理（list/health/unused/dedupe/threshold/rename）",
        epilog=(
            "示例:\n"
            "  python run.py templates list\n"
            "  python run.py templates health --save\n"
            "  python run.py templates unused\n"
            "  python run.py templates dedupe --similarity 0.97\n"
            "  python run.py templates threshold \"battle/*\" 0.82\n"
            "  python run.py templates rename battle/btn_retry battle/btn_retry_stage\n"
            "\n"
            "注意: --config / --verbose 必须写在子命令**之前**\n"
            "      （正确: templates --config x.toml health）\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    common(tp)
    tp.add_argument("rest", nargs=argparse.REMAINDER, help="子命令，如 health --save")
    tp.set_defaults(func=cmd_templates)

    rp = sub.add_parser("report", help="运行统计报表")
    common(rp)
    rp.add_argument("--days", type=float, help="只看最近 N 天")
    rp.add_argument("--out", help="把 Markdown 报表写到文件")
    rp.add_argument("--csv", help="同时导出 CSV")
    rp.add_argument("--sessions", action="store_true", help="只看最近几次运行")
    rp.add_argument("--limit", type=int, default=10, help="--sessions 显示条数")
    rp.add_argument("--title", help="报表标题")
    rp.add_argument("--prune", action="store_true", help="清理过期事件")
    rp.set_defaults(func=cmd_report)

    at = sub.add_parser("alert-test", help="测试告警渠道能否真的送到")
    common(at)
    at.set_defaults(func=cmd_alert_test)

    gu = sub.add_parser("gui", help="启动系统托盘 GUI")
    common(gu)
    gu.add_argument("--dry-run", action="store_true")
    gu.add_argument("--tasks", help="覆盖 tasks.active，逗号分隔")
    gu.set_defaults(func=cmd_gui)

    return p


def main(argv: list[str] | None = None) -> int:
    enable_dpi_awareness()
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
