"""录制回放工具：手动操作一遍，自动生成进本导航序列 + 对应模板。

**这个工具解决的是本项目最大的摩擦点。**

原本要跑起来自动刷本，得做两件苦活：
  1. 逐个框选采集 `lobby/btn_adventure`、`adventure/...`、`battle/btn_start_battle` 等一堆模板
  2. 手写 `[[tasks.repeat_stage.enter_sequence]]` 配置，还得反复试哪一步等哪个模板

现在只要：**按 F8 开始 → 在游戏里手动从大厅点到开始战斗 → 再按 F8 结束**。
工具会自动完成：
  * 记录每次点击的位置（归一化坐标）
  * 从**点击前**的画面里自动裁出模板，并自动收缩到"在全屏里唯一匹配"的最小可靠尺寸
  * 落盘模板 + sidecar 元数据
  * 生成可直接粘贴的 `enter_sequence` 配置，并自动推导每一步的 `wait_for`
    （第 i 步等第 i+1 步的模板出现 —— 这通常正是你想要的行为）

## 为什么不挂钩子（hook）

全局鼠标钩子（`SetWindowsHookEx` / `pynput`）能更精确地拿到点击事件，但：
* 钩子是一种"注入式"行为，内核级反作弊（UNCHEATER）有理由注意到它；
* 而轮询 `GetAsyncKeyState` + `GetCursorPos` 只是**读取系统状态**，
  和任何普通程序查询鼠标位置没有区别。

所以这里用轮询。代价是可能漏掉极短的点击（<10ms），但真人点击通常 50~150ms，够用。

## 关于"点击前的画面"

点击被系统处理前，界面还是原样。所以模板应该取自**鼠标按下的那一瞬间**，
而不是按下之后（那时可能已经在切场景了）。为此后台线程持续抓帧存进环形缓冲，
检测到按下时回溯取"时间戳最接近按下时刻的那一帧"。
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from e7bot.capture import ScreenGrabber
from e7bot.config import Config
from e7bot.winutil import (
    GameNotFound,
    Rect,
    client_rect_screen,
    enable_dpi_awareness,
    find_window,
    monitor_index_of,
)

# 候选裁剪尺寸（宽, 高），从大到小试。数值基于 1920x1080 基准，
# 会按当前分辨率等比缩放。PC 端 UI 按钮普遍是宽扁形，所以宽 > 高。
CANDIDATE_SIZES: list[tuple[int, int]] = [
    (224, 96), (192, 84), (160, 72), (136, 60), (112, 50), (88, 40),
]

#: 环形缓冲保留的帧数（按 capture_fps 换算成秒）。0.35s 足够覆盖
#: "鼠标按下 → UI 开始变化"之间的延迟。
RING_SIZE = 8
CAPTURE_FPS = 22


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class Step:
    index: int
    norm_xy: tuple[float, float]        # 点击位置的归一化坐标
    screen_xy: tuple[int, int]          # 屏幕绝对坐标
    box: tuple[int, int, int, int]      # 裁剪框（帧内像素）
    crop: np.ndarray                    # 模板图
    self_score: float                   # 与自身所在位置的匹配分（越高越好）
    unique: bool                        # 在全屏里是否唯一
    frame_size: tuple[int, int]         # 采集时的客户区尺寸
    name: str = ""

    @property
    def flat(self) -> bool:
        return float(self.crop.std()) < 6.0


@dataclass
class RecordSession:
    steps: list[Step] = field(default_factory=list)
    frame_size: tuple[int, int] = (0, 0)
    started_at: float = field(default_factory=time.time)
    rejected: int = 0


# --------------------------------------------------------------------------- #
# 抓帧环形缓冲
# --------------------------------------------------------------------------- #


class FrameRing:
    """后台线程持续抓帧，保留最近若干帧供"回溯取点击前画面"。"""

    def __init__(self, grabber: ScreenGrabber, rect_provider, fps: int = CAPTURE_FPS,
                 size: int = RING_SIZE):
        self._grabber = grabber
        self._rect_provider = rect_provider
        self._buf: deque[tuple[float, np.ndarray, Rect]] = deque(maxlen=size)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._interval = 1.0 / max(fps, 1)
        self._thread: Optional[threading.Thread] = None
        self.errors = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="frame-ring")
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.perf_counter()
            try:
                rect = self._rect_provider()
                img = self._grabber.grab(rect)
                with self._lock:
                    self._buf.append((time.time(), img, rect))
            except Exception:  # noqa: BLE001
                self.errors += 1
            slack = self._interval - (time.perf_counter() - t0)
            if slack > 0:
                time.sleep(slack)

    def frame_before(self, ts: float) -> Optional[tuple[np.ndarray, Rect]]:
        """取时间戳 <= ts 的最近一帧；没有就取最旧的一帧。"""
        with self._lock:
            if not self._buf:
                return None
            candidates = [(t, img, r) for (t, img, r) in self._buf if t <= ts]
            if candidates:
                t, img, r = candidates[-1]
            else:
                t, img, r = self._buf[0]
            return img.copy(), r

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)


# --------------------------------------------------------------------------- #
# 唯一性判定
# --------------------------------------------------------------------------- #


def count_unique_matches(frame: np.ndarray, crop: np.ndarray, threshold: float) -> tuple[int, float]:
    """统计模板在整帧里有多少个"互相不重叠"的匹配点，以及最高分。

    为什么需要它：裁剪框越大越可能带上背景，而背景在不同界面里重复出现，
    会导致这个模板到处都能匹配上 —— 那样 `enter_sequence` 就会点错地方。
    所以我们要收缩裁剪框，直到它在全屏里**唯一**。
    """
    fh, fw = frame.shape[:2]
    ch, cw = crop.shape[:2]
    if fh < ch or fw < cw:
        return 0, 0.0

    res = cv2.matchTemplate(frame, crop, cv2.TM_CCOEFF_NORMED)
    res = np.nan_to_num(res, nan=0.0, posinf=0.0, neginf=0.0)

    if res.size == 0:
        return 0, 0.0
    best = float(res.max())

    # 简易 NMS：按分数从高到低扫，抑制与已选点距离过近的候选
    ys, xs = np.where(res >= threshold)
    if len(xs) == 0:
        return 0, best
    order = np.argsort(res[ys, xs])[::-1]
    min_dx, min_dy = max(2, cw // 2), max(2, ch // 2)
    kept: list[tuple[int, int]] = []
    for i in order:
        x, y = int(xs[i]), int(ys[i])
        if all(abs(x - kx) > min_dx or abs(y - ky) > min_dy for kx, ky in kept):
            kept.append((x, y))
    return len(kept), best


def centered_box(click: tuple[int, int], size: tuple[int, int], bounds: Rect,
                 frame_size: tuple[int, int]) -> Optional[tuple[int, int, int, int]]:
    """以点击点为中心开一个框，并夹到帧边界内。返回帧内坐标 (x, y, w, h)。"""
    cw, ch = size
    fw, fh = frame_size
    x = click[0] - cw // 2
    y = click[1] - ch // 2
    x = max(0, min(x, fw - cw))
    y = max(0, min(y, fh - ch))
    if cw > fw or ch > fh:
        return None
    return (x, y, cw, ch)


def refine_crop(frame: np.ndarray, click_local: tuple[int, int], scale: float,
                threshold: float) -> Optional[tuple[tuple[int, int, int, int], np.ndarray, float, bool]]:
    """从大到小试候选框，返回第一个"在全屏唯一"的裁剪。

    取"最大的唯一框"是个折中：框越大包含的视觉特征越多、对局部动画越鲁棒，
    但越容易撞上重复背景。从大到小扫，第一个唯一的即最优。
    """
    fh, fw = frame.shape[:2]
    for (bw, bh) in CANDIDATE_SIZES:
        size = (max(16, int(bw * scale)), max(12, int(bh * scale)))
        box = centered_box(click_local, size, Rect(0, 0, fw, fh), (fw, fh))
        if box is None:
            continue
        x, y, w, h = box
        crop = frame[y:y + h, x:x + w]
        if crop.size == 0 or float(crop.std()) < 6.0:
            continue  # 太"平"的区域（纯色/渐变）匹配无意义
        n, score = count_unique_matches(frame, crop, threshold)
        if n == 1 and score >= threshold:
            return box, crop.copy(), score, True
    return None


# --------------------------------------------------------------------------- #
# 录制
# --------------------------------------------------------------------------- #


def _mouse_down() -> bool:
    return bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)


def _key_down(vk: int) -> bool:
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


def _cursor_pos() -> tuple[int, int]:
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    pt = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return (pt.x, pt.y)


def record(
    cfg: Config,
    hotkey: str = "f8",
    duration: float = 0.0,
    threshold: float = 0.86,
    name_prefix: str = "seq",
    start_immediately: bool = False,
) -> RecordSession:
    enable_dpi_awareness()

    win = find_window(
        title_patterns=cfg.get("window.title_patterns", []),
        exe_patterns=cfg.get("window.exe_patterns", []),
        min_size=tuple(cfg.get("window.min_size", (800, 450))),
    )
    if win is None:
        raise GameNotFound(
            "没找到第七史诗窗口，请先启动游戏（Steam 版或 Demo 均可）。"
        )

    grabber = ScreenGrabber(
        hwnd=win.hwnd,
        preferred=list(cfg.get("capture.backends", [])),
        monitor_index=monitor_index_of(win.hwnd),
        target_fps=CAPTURE_FPS,
        verbose=False,
    )

    vk = {"f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79}.get(
        hotkey.lower(), 0x77
    )

    session = RecordSession()
    print("=" * 76)
    print("录制回放工具 —— 手动操作一遍，自动生成进本导航序列")
    print("=" * 76)
    print(f"游戏窗口: {win.title!r}  客户区 {win.client.width}x{win.client.height}")
    print(f"截图后端: {grabber.backend_name}")
    print()
    print(f"  按 {hotkey.upper()} 开始录制")
    print("  然后在游戏里正常操作一遍（例如：大厅 → 冒险 → 选关卡 → 开始战斗）")
    print(f"  再按 {hotkey.upper()} 结束录制")
    print()
    print("  提示：只记录落在游戏窗口内的点击；窗口外的点击（比如点控制台）会被忽略。")
    print()

    if start_immediately:
        print("  --no-wait 已启用，立即开始录制 …")
    else:
        print(f"  等待 {hotkey.upper()} …", end="", flush=True)
        armed = True
        while True:
            down = _key_down(vk)
            if down and armed:
                armed = False
                break
            if not down:
                armed = True
            time.sleep(0.03)
        print(" 开始")

    ring = FrameRing(grabber, lambda: client_rect_screen(win.hwnd))
    ring.start()
    time.sleep(0.3)  # 让缓冲先填几帧

    # 记录点击
    session.started_at = time.time()
    deadline = session.started_at + duration if duration > 0 else None
    pressed = False
    last_click: Optional[tuple[float, tuple[int, int]]] = None
    status = ""

    try:
        while True:
            now = time.time()
            if deadline and now > deadline:
                print("\n  已达到指定时长，结束录制")
                break
            if _key_down(vk):
                # 等按键松开，避免同一次按下被当成开始和结束
                while _key_down(vk):
                    time.sleep(0.02)
                print("\n  收到结束信号")
                break

            is_down = _mouse_down()
            if is_down and not pressed:
                pressed = True
                ts = time.time()
                sx, sy = _cursor_pos()
                rect_now = client_rect_screen(win.hwnd)

                if not (rect_now.left <= sx < rect_now.right and rect_now.top <= sy < rect_now.bottom):
                    session.rejected += 1  # 点在游戏窗口外
                else:
                    # 双击/连点去重：位置几乎相同且间隔很短
                    if last_click and (ts - last_click[0]) < 0.35 and \
                            abs(sx - last_click[1][0]) < 6 and abs(sy - last_click[1][1]) < 6:
                        session.rejected += 1
                    else:
                        got = ring.frame_before(ts)
                        if got is None:
                            session.rejected += 1
                        else:
                            frame, frect = got
                            session.frame_size = (frect.width, frect.height)
                            scale = frect.width / 1920.0
                            click_local = (sx - frect.left, sy - frect.top)
                            refined = refine_crop(frame, click_local, scale, threshold)
                            if refined is None:
                                session.rejected += 1
                                status = "该点击附近找不到可唯一识别的区域，已跳过"
                            else:
                                box, crop, score, uniq = refined
                                norm = frect.to_local(sx, sy)
                                step = Step(
                                    index=len(session.steps) + 1,
                                    norm_xy=norm,
                                    screen_xy=(sx, sy),
                                    box=box,
                                    crop=crop,
                                    self_score=score,
                                    unique=uniq,
                                    frame_size=(frect.width, frect.height),
                                    name=f"{name_prefix}/step_{len(session.steps) + 1:02d}",
                                )
                                session.steps.append(step)
                                last_click = (ts, (sx, sy))
                                status = (f"记录第 {step.index} 步  位置 ({norm[0]:.3f}, {norm[1]:.3f})  "
                                          f"裁剪 {box[2]}x{box[3]}  匹配 {score:.3f}")
            elif not is_down:
                pressed = False

            line = (f"\r  已记录 {len(session.steps)} 步  跳过 {session.rejected} 次  |  {status}")
            print(line.ljust(110)[:110], end="", flush=True)
            time.sleep(0.008)
    except KeyboardInterrupt:
        print("\n  收到 Ctrl+C，结束录制")
    finally:
        ring.stop()
        grabber.close()

    print()
    return session


# --------------------------------------------------------------------------- #
# 落盘 + 生成配置
# --------------------------------------------------------------------------- #


def save_session(session: RecordSession, cfg: Config, out_config: Optional[Path] = None) -> Path:
    profile = str(cfg.get("templates.profile", "default"))
    tdir = cfg.template_root() / profile

    for step in session.steps:
        png = tdir / f"{step.name}.png"
        png.parent.mkdir(parents=True, exist_ok=True)
        cv2.imencode(".png", step.crop)[1].tofile(str(png))

        fw, fh = step.frame_size
        x, y, w, h = step.box
        # 自动推导搜索区域：以模板为中心放大 3 倍（与采集器同一套逻辑），
        # 这样运行时的场景/步骤识别只在小范围内匹配，速度快一两个数量级。
        cx, cy = (x + w / 2) / fw, (y + h / 2) / fh
        rw = max(0.08, min(1.0, (w * 3) / fw))
        rh = max(0.08, min(1.0, (h * 3) / fh))
        meta = {
            "ref_size": [fw, fh],
            "region": [
                round(max(0.0, min(1.0 - rw, cx - rw / 2)), 4),
                round(max(0.0, min(1.0 - rh, cy - rh / 2)), 4),
                round(rw, 4), round(rh, 4),
            ],
            "threshold": 0.86,
            "note": f"录制生成：点击 ({step.norm_xy[0]:.4f}, {step.norm_xy[1]:.4f})，"
                    f"裁剪 {w}x{h}，自匹配 {step.self_score:.3f}",
        }
        import json

        (png.with_suffix(".json")).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    out = out_config or (cfg.template_root().parent / "config" / "recorded_sequence.toml")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(generate_toml(session), encoding="utf-8")
    return out


def generate_toml(session: RecordSession, task: str = "repeat_stage") -> str:
    """生成可直接粘贴的 enter_sequence 配置。"""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    fw, fh = session.frame_size
    n = len(session.steps)

    lines = [
        f"# 由 tools/record.py 自动生成于 {stamp}",
        f"# 采集分辨率: {fw}x{fh}    共 {n} 步",
        "#",
        "# 用法：把下面内容追加到 config/default.toml 的 [tasks.repeat_stage] 之后，",
        "#      然后用 `python run.py run --dry-run` 验证每一步是否都能找到。",
        "#",
        "# wait_for 是自动推导的（第 i 步等第 i+1 步的模板出现），通常正是你要的行为，",
        "# 但请务必 dry-run 确认；最后一步没有后续可等，需要你手动填一个",
        "# 「关卡准备界面」的锚点模板（例如 battle/btn_start_battle）。",
        "",
    ]

    for i, step in enumerate(session.steps):
        lines.append(f"[[tasks.{task}.enter_sequence]]")
        lines.append(f'click = "{step.name}"')
        if i + 1 < n:
            lines.append(f'wait_for = "{session.steps[i + 1].name}"')
        else:
            lines.append("# wait_for = \"battle/btn_start_battle\"   # ← 请改成关卡准备界面的锚点")
        lines.append("timeout = 20")
        lines.append("delay = [0.8, 1.6]")
        lines.append(f"# 点击位置 ({step.norm_xy[0]:.4f}, {step.norm_xy[1]:.4f})  "
                     f"裁剪 {step.box[2]}x{step.box[3]}  自匹配 {step.self_score:.3f}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def print_summary(session: RecordSession, out: Path) -> None:
    print()
    print("=" * 76)
    print(f"录制完成：{len(session.steps)} 步（跳过 {session.rejected} 次点击）")
    print("=" * 76)
    if not session.steps:
        print("  没有记录到任何有效步骤。可能原因：")
        print("    - 点击都在游戏窗口之外（工具只记录窗口内的点击）")
        print("    - 点击位置附近没有可唯一识别的区域（纯色背景 / 动态区域）")
        print("    - 游戏窗口不是前台（那样点击根本不会作用到游戏上）")
        return

    print(f"  {'步骤':<6}{'归一化位置':<20}{'裁剪':<12}{'自匹配':<10}{'唯一'}")
    for s in session.steps:
        print(f"  {s.index:<6}({s.norm_xy[0]:.3f}, {s.norm_xy[1]:.3f})"
              f"{'':<8}{s.box[2]}x{s.box[3]:<7}{s.self_score:<10.3f}"
              f"{'是' if s.unique else '否'}")
    print()
    print(f"  模板已写入: {out.parent}")
    print(f"  配置已生成: {out}")
    print()
    print("  下一步：")
    print(f"    1. 打开 {out}，把内容粘贴到 config/default.toml 的 [tasks.repeat_stage] 之后")
    print("    2. 把最后一步的 wait_for 改成关卡准备界面的锚点模板")
    print("    3. python run.py run --dry-run      # 只识别不点击，确认每步都能找到")
    print("    4. python run.py run                # 正式跑")
    print()
    print("  提醒：录制出的模板质量取决于当时的画面。如果某步在 dry-run 里找不到，")
    print("        用 `python run.py capture` 手动重采那一个，或重新录制那一段。")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def run_record(cfg: Config, hotkey: str = "f8", duration: float = 0.0,
               threshold: float = 0.86, prefix: str = "seq",
               out_config: Optional[str] = None, start_immediately: bool = False) -> int:
    try:
        session = record(cfg, hotkey=hotkey, duration=duration, threshold=threshold,
                         name_prefix=prefix, start_immediately=start_immediately)
    except GameNotFound as exc:
        print(f"[record] {exc}")
        return 4
    except Exception as exc:  # noqa: BLE001
        print(f"[record] 录制失败: {type(exc).__name__}: {exc}")
        return 1

    out = save_session(session, cfg, Path(out_config) if out_config else None)
    print_summary(session, out)
    return 0 if session.steps else 1


if __name__ == "__main__":  # pragma: no cover
    _root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(_root))
    raise SystemExit(run_record(Config.load(_root / "config" / "default.toml")))
