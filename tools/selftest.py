"""离线自检 —— 不需要游戏，用合成画面验证整条识别链路。

这一点很重要：本项目的模板必须从真实客户端采集，但**框架本身的正确性不能靠
"等游戏上线再说"**。所以这里用程序合成"游戏画面"，把整条链路跑通：

1. 合成一个 1920x1080 的假 UI（带纹理的按钮块），裁出模板并落盘（含 sidecar JSON）
2. 把同一个画面缩放到 1280x720 / 2560x1440，验证**分辨率无关匹配**仍然找得到，
   并且归一化坐标一致（这是 PC 端相对安卓端最核心的适配点）
3. 验证场景状态机在"大厅帧"和"战斗帧"上给出正确场景
4. 验证多目标查找 + NMS、颜色/灰度判断、帧差
5. 验证坐标换算、配置校验、TOML 往返、配置驱动序列解析

    python run.py selftest
"""

from __future__ import annotations

import json
import shutil
import tempfile
import zlib
from pathlib import Path

import cv2
import numpy as np

from e7bot.config import Config, dump_toml
from e7bot.scene import SceneDetector
from e7bot.tasks.base import ClickSequence
from e7bot.vision import (
    Matcher,
    MatchOptions,
    TemplateLibrary,
    frame_diff_ratio,
    is_greyed,
)
from e7bot.winutil import Rect

# --------------------------------------------------------------------------- #
# 合成"游戏画面"
# --------------------------------------------------------------------------- #

#: 归一化布局（与分辨率无关地定义假 UI 元素）
LOBBY_LAYOUT = {
    "lobby/btn_adventure": (0.06, 0.86, 0.11, 0.10),
    "lobby/btn_hero":      (0.20, 0.86, 0.11, 0.10),
    "lobby/btn_sanctuary": (0.34, 0.86, 0.11, 0.10),
    "lobby/btn_mail":      (0.90, 0.04, 0.06, 0.09),
    "lobby/btn_shop":      (0.82, 0.04, 0.06, 0.09),
}
BATTLE_LAYOUT = {
    "battle/btn_auto_off":   (0.04, 0.90, 0.10, 0.07),
    "battle/btn_speed_x1":   (0.88, 0.90, 0.09, 0.07),
    "battle/btn_retry":      (0.42, 0.72, 0.16, 0.09),
    "common/btn_ok":         (0.44, 0.80, 0.12, 0.07),
}

#: 每个元素给一个独特的基色，保证模板可区分
COLORS = {
    "lobby/btn_adventure": (60, 190, 240),
    "lobby/btn_hero":      (80, 230, 120),
    "lobby/btn_sanctuary": (240, 160, 70),
    "lobby/btn_mail":      (230, 90, 90),
    "lobby/btn_shop":      (200, 120, 240),
    "battle/btn_auto_off": (180, 180, 180),
    "battle/btn_speed_x1": (120, 200, 255),
    "battle/btn_retry":    (90, 240, 200),
    "common/btn_ok":       (255, 210, 90),
}


def _texture(w: int, h: int, seed: int) -> np.ndarray:
    """生成块状纹理（低频，缩放到别的分辨率后仍能被匹配到）。"""
    rng = np.random.default_rng(seed)
    bh, bw = max(2, h // 6), max(2, w // 6)
    small = rng.integers(60, 255, size=(max(2, h // bh), max(2, w // bw)), dtype=np.uint8)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)


def _seed_of(name: str) -> int:
    """由元素名稳定地推出纹理种子。

    刻意不用 `enumerate` 的下标：同一个 UI 元素出现在不同布局里时下标会变，
    纹理就跟着变，测试会假失败。真实游戏里同一按钮的贴图是固定的，
    这里必须模拟这个性质。
    """
    return 1000 + (zlib.crc32(name.encode("utf-8")) % 100000)


def render(layout: dict, width: int, height: int) -> np.ndarray:
    """把归一化布局画成一张 BGR 图。"""
    frame = np.full((height, width, 3), 26, np.uint8)
    # 背景渐变，避免整图过于平坦
    grad = np.linspace(20, 60, height, dtype=np.float32)[:, None]
    frame[:, :, 0] = np.clip(frame[:, :, 0] + grad, 0, 255)
    frame[:, :, 2] = np.clip(frame[:, :, 2] + grad * 0.6, 0, 255)

    for name, (nx, ny, nw, nh) in sorted(layout.items()):
        x, y = int(nx * width), int(ny * height)
        w, h = max(8, int(nw * width)), max(8, int(nh * height))
        base = np.array(COLORS.get(name, (200, 200, 200)), np.float32)
        tex = _texture(w, h, seed=_seed_of(name)).astype(np.float32) / 255.0
        patch = np.clip(base[None, None, :] * (0.55 + 0.75 * tex[:, :, None]), 0, 255)
        frame[y:y + h, x:x + w] = patch.astype(np.uint8)
        cv2.rectangle(frame, (x, y), (x + w - 1, y + h - 1), (255, 255, 255), 1)
    return frame


def crop_norm(frame: np.ndarray, box: tuple[float, float, float, float]) -> np.ndarray:
    h, w = frame.shape[:2]
    x, y = int(box[0] * w), int(box[1] * h)
    cw, ch = max(8, int(box[2] * w)), max(8, int(box[3] * h))
    return frame[y:y + ch, x:x + cw].copy()


# --------------------------------------------------------------------------- #
# 测试框架（极简，不引入 pytest 依赖）
# --------------------------------------------------------------------------- #


class Checker:
    def __init__(self, verbose: bool = False):
        self.passed = 0
        self.failed: list[str] = []
        self.verbose = verbose

    def check(self, cond: bool, label: str, detail: str = "") -> bool:
        if cond:
            self.passed += 1
            print(f"  [PASS] {label}" + (f"  ({detail})" if detail and self.verbose else ""))
        else:
            self.failed.append(label)
            print(f"  [FAIL] {label}" + (f"  ({detail})" if detail else ""))
        return bool(cond)

    def near(self, got: float, want: float, tol: float, label: str) -> bool:
        return self.check(abs(got - want) <= tol, label, f"got={got:.4f} want={want:.4f}±{tol}")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def run_selftest(verbose: bool = False) -> int:
    c = Checker(verbose)
    print("=" * 74)
    print("e7bot 离线自检（合成画面，不需要游戏）")
    print("=" * 74)

    tmp = Path(tempfile.mkdtemp(prefix="e7bot_selftest_"))
    try:
        _test_coords(c)
        _test_vision(c, tmp, verbose)
        _test_scene(c, tmp)
        _test_config(c, tmp)
        _test_sequence(c)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 74)
    total = c.passed + len(c.failed)
    if c.failed:
        print(f"结果: {c.passed}/{total} 通过，失败 {len(c.failed)} 项：")
        for f in c.failed:
            print(f"  - {f}")
        return 1
    print(f"结果: 全部 {total} 项通过 ✓")
    print("说明：这验证的是框架链路本身；真实游戏模板仍需用 run.py capture 采集。")
    return 0


# --------------------------------------------------------------------------- #


def _test_coords(c: Checker) -> None:
    print("\n[1] 坐标换算（分辨率无关的基础）")
    rect = Rect(100, 50, 1920, 1080)
    c.check(rect.to_screen(0.5, 0.5) == (1060, 590), "中心点换算",
            str(rect.to_screen(0.5, 0.5)))
    c.check(rect.to_screen(0.0, 0.0) == (100, 50), "左上角换算")
    c.check(rect.to_screen(1.0, 1.0) == (2020, 1130), "右下角换算")

    # 往返一致性
    for nx, ny in [(0.0, 0.0), (0.123, 0.877), (1.0, 1.0), (0.5, 0.25)]:
        sx, sy = rect.to_screen(nx, ny)
        bx, by = rect.to_local(sx, sy)
        c.near(bx, nx, 0.001, f"往返一致 x ({nx})")
        c.near(by, ny, 0.001, f"往返一致 y ({ny})")

    sub = rect.sub(0.1, 0.2, 0.5, 0.4)
    c.check(sub.as_tuple() == (292, 266, 960, 432), "归一化子区域切分", str(sub.as_tuple()))

    # 关键点：同一个归一化坐标在不同分辨率下指向同一个 UI 元素
    small = Rect(0, 0, 1280, 720)
    big = Rect(0, 0, 2560, 1440)
    c.near(small.to_screen(0.5, 0.5)[0] / 1280, big.to_screen(0.5, 0.5)[0] / 2560, 0.001,
           "跨分辨率归一化一致")


def _test_vision(c: Checker, tmp: Path, verbose: bool) -> None:
    print("\n[2] 模板匹配 + 分辨率无关性")

    ref_w, ref_h = 1920, 1080
    ref_frame = render(LOBBY_LAYOUT, ref_w, ref_h)

    # 模拟"采集模板"：从参考分辨率裁出并落盘（含 sidecar）
    tdir = tmp / "templates" / "default"
    for name, box in LOBBY_LAYOUT.items():
        p = tdir / f"{name}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        crop = crop_norm(ref_frame, box)
        cv2.imencode(".png", crop)[1].tofile(str(p))
        x, y, w, h = box
        (p.with_suffix(".json")).write_text(json.dumps({
            "ref_size": [ref_w, ref_h],
            "region": [max(0.0, x - w), max(0.0, y - h), min(1.0, w * 3), min(1.0, h * 3)],
            "threshold": 0.8,
        }), encoding="utf-8")

    lib = TemplateLibrary(tmp / "templates", "default")
    c.check(len(lib) == len(LOBBY_LAYOUT), f"模板库加载 {len(lib)} 个", str(lib.names()))

    matcher = Matcher(lib)

    # 2a) 同分辨率：应当近乎完美命中，且位置精确
    rect = Rect(0, 0, ref_w, ref_h)
    for name, box in LOBBY_LAYOUT.items():
        m = matcher.find(name, ref_frame, rect, opts=MatchOptions(threshold=0.7))
        if not c.check(m is not None, f"同分辨率命中 {name}"):
            continue
        assert m is not None
        c.check(m.score > 0.97, f"{name} 分数 > 0.97", f"{m.score:.4f}")
        ex = box[0] + box[2] / 2
        ey = box[1] + box[3] / 2
        c.near(m.norm_center[0], ex, 0.006, f"{name} 归一化 x 精确")
        c.near(m.norm_center[1], ey, 0.006, f"{name} 归一化 y 精确")

    # 2b) 缩放分辨率：模板应自动缩放后仍命中，归一化坐标保持一致
    for (w, h) in [(1280, 720), (2560, 1440), (1600, 900)]:
        frame = render(LOBBY_LAYOUT, w, h)
        r = Rect(0, 0, w, h)
        hits = 0
        for name, box in LOBBY_LAYOUT.items():
            m = matcher.find(name, frame, r, opts=MatchOptions(threshold=0.55))
            if m is None:
                print(f"      !! {w}x{h} 未命中 {name}")
                continue
            ex, ey = box[0] + box[2] / 2, box[1] + box[3] / 2
            if abs(m.norm_center[0] - ex) < 0.01 and abs(m.norm_center[1] - ey) < 0.01:
                hits += 1
        c.check(hits == len(LOBBY_LAYOUT),
                f"{w}x{h} 全部 {len(LOBBY_LAYOUT)} 个模板命中且位置一致", f"命中 {hits}")

    # 2c) 多尺度兜底：把画面整体放大 4%，精确缩放匹配应失败，多尺度应救回来
    frame_off = render(LOBBY_LAYOUT, int(ref_w * 1.04), int(ref_h * 1.04))
    r_off = Rect(0, 0, frame_off.shape[1], frame_off.shape[0])
    plain = matcher.find("lobby/btn_hero", frame_off, r_off,
                         opts=MatchOptions(threshold=0.9, scale_tolerance=0.0))
    rescued = matcher.find("lobby/btn_hero", frame_off, r_off,
                           opts=MatchOptions(threshold=0.85, scale_tolerance=0.08, scale_steps=9))
    c.check(rescued is not None and (plain is None or rescued.score >= (plain.score if plain else 0)),
            "多尺度兜底能处理 ±4% 缩放偏差",
            f"plain={plain.score if plain else None} rescued={rescued.score if rescued else None}")

    # 2d) 多目标查找 + NMS
    multi = render({"tile": (0, 0, 1, 1)}, 1920, 1080)
    # 铺 3x2 个相同图标
    tile = crop_norm(multi, (0.0, 0.0, 0.08, 0.12))
    grid = np.full((1080, 1920, 3), 26, np.uint8)
    for gy in range(2):
        for gx in range(3):
            y, x = 100 + gy * 300, 200 + gx * 500
            grid[y:y + tile.shape[0], x:x + tile.shape[1]] = tile
    p = tdir / "multi" / "tile.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    cv2.imencode(".png", tile)[1].tofile(str(p))
    (p.with_suffix(".json")).write_text(json.dumps({"ref_size": [1920, 1080], "threshold": 0.8}),
                                       encoding="utf-8")
    lib.reload()
    matcher.clear_cache()
    found = matcher.find_all("multi/tile", grid, Rect(0, 0, 1920, 1080),
                            opts=MatchOptions(threshold=0.8, max_results=32), nms_iou=0.3)
    c.check(len(found) == 6, "find_all + NMS 找到 6 个相同图标", f"找到 {len(found)}")

    # 2e) 灰度判断 / 帧差
    grey_frame = np.full((100, 100, 3), 128, np.uint8)
    c.check(is_greyed(grey_frame, Rect(0, 0, 100, 100), (0.0, 0.0, 1.0, 1.0)),
            "纯灰区域判定为灰色（禁用态）")
    color_frame = np.zeros((100, 100, 3), np.uint8)
    color_frame[:, :, 2] = 255
    c.check(not is_greyed(color_frame, Rect(0, 0, 100, 100), (0.0, 0.0, 1.0, 1.0)),
            "纯红区域判定为非灰（启用态）")
    a = np.zeros((50, 50, 3), np.uint8)
    b = a.copy()
    c.near(frame_diff_ratio(a, b), 0.0, 0.001, "相同帧差异为 0")
    b[:, :] = 255
    c.near(frame_diff_ratio(a, b), 1.0, 0.001, "完全不同帧差异为 1")


def _test_scene(c: Checker, tmp: Path) -> None:
    print("\n[3] 场景状态机")

    ref_w, ref_h = 1920, 1080
    tdir = tmp / "templates" / "default"

    # 关键：本用例必须自给自足地准备**全部**它要用到的模板。
    # 早期版本只写了 BATTLE_LAYOUT，隐式依赖 _test_vision 先把大厅模板写进同一个
    # 临时目录 —— 在 run_selftest 里共享 tmp 时能过，换成 pytest 的独立 tmp_path
    # 就失败。测试之间不能有这种隐式耦合。
    battle_frame = render({**LOBBY_LAYOUT, **BATTLE_LAYOUT}, ref_w, ref_h)
    for name, box in {**LOBBY_LAYOUT, **BATTLE_LAYOUT}.items():
        p = tdir / f"{name}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        cv2.imencode(".png", crop_norm(battle_frame, box))[1].tofile(str(p))
        x, y, w, h = box
        (p.with_suffix(".json")).write_text(json.dumps({
            "ref_size": [ref_w, ref_h],
            "region": [max(0.0, x - w), max(0.0, y - h), min(1.0, w * 3), min(1.0, h * 3)],
            "threshold": 0.8,
        }), encoding="utf-8")

    lib = TemplateLibrary(tmp / "templates", "default")
    matcher = Matcher(lib)

    scenes = {
        "lobby": {"any": ["lobby/btn_adventure", "lobby/btn_hero"], "none": []},
        "battle": {"any": ["battle/btn_auto_off", "battle/btn_speed_x1"],
                   "none": ["battle/btn_retry"]},
        # 结算界面用「再次挑战」作为唯一锚点：common/btn_ok 这类通用按钮太容易被
        # 别的界面命中，不适合当场景锚点（这也是采模板时的一条实用经验）。
        "battle_result": {"any": ["battle/btn_retry"], "none": []},
    }
    det = SceneDetector(matcher, scenes, threshold=0.8)

    # 纯大厅画面
    lobby_only = render(LOBBY_LAYOUT, ref_w, ref_h)
    r = Rect(0, 0, ref_w, ref_h)
    res = det.detect(lobby_only, r, use_cache=False)
    c.check(res.name == "lobby", "大厅帧 -> lobby", res.describe())

    # 战斗画面（含 retry 按钮 = 结算态）
    res = det.detect(battle_frame, r, use_cache=False)
    c.check(res.name == "battle_result", "含 retry 的帧 -> battle_result（优先级正确）",
            res.describe())

    # 战斗画面但去掉 retry -> battle
    battle_only = render({k: v for k, v in BATTLE_LAYOUT.items() if "retry" not in k},
                         ref_w, ref_h)
    res = det.detect(battle_only, r, use_cache=False)
    c.check(res.name == "battle", "无 retry 的战斗帧 -> battle", res.describe())

    # 完全不认识的画面
    blank = np.full((ref_h, ref_w, 3), 17, np.uint8)
    res = det.detect(blank, r, use_cache=False)
    c.check(res.name == "unknown", "空画面 -> unknown", res.describe())

    # 缺模板不应崩
    scenes2 = dict(scenes)
    scenes2["ghost"] = {"any": ["nope/does_not_exist"], "none": []}
    det2 = SceneDetector(matcher, scenes2, threshold=0.8)
    res = det2.detect(lobby_only, r, use_cache=False)
    c.check(res.name == "lobby", "模板缺失时仍能正确识别其他场景", res.describe())
    c.check("nope/does_not_exist" in det2.missing_templates(), "missing_templates 能报出缺失项")


def _test_config(c: Checker, tmp: Path) -> None:
    print("\n[4] 配置系统")

    cfg = Config()
    c.check(cfg.validate() == [], "内置默认配置校验通过", str(cfg.validate()))
    c.check(cfg.get("safety.fail_safe_key") == "f12", "默认急停键为 f12")
    c.check(cfg.get("safety.dry_run") is False, "默认非 dry-run")

    # 加载一个自定义配置（含配置驱动序列）
    p = tmp / "cfg.toml"
    p.write_text("""
[templates]
profile = "myprofile"

[input]
speed = 2.5

[safety]
fail_safe_key = "f10"

[tasks]
active = ["repeat_stage"]

[tasks.repeat_stage]
max_runs = 42

[[tasks.repeat_stage.enter_sequence]]
click = "lobby/btn_adventure"
wait_for = "adventure/btn_stage_list"
timeout = 25
""", encoding="utf-8")

    cfg2 = Config.load(p)
    c.check(cfg2.get("input.speed") == 2.5, "覆盖 input.speed")
    c.check(cfg2.get("input.move_jitter_px") == 2.5, "未覆盖项保留默认值（深合并）")
    c.check(cfg2.get("templates.profile") == "myprofile", "覆盖 profile")
    c.check(cfg2.task("repeat_stage").get("max_runs") == 42, "任务参数读取")
    c.check(cfg2.get("safety.pause_key") == "f9", "未覆盖的安全项保留默认")
    c.check(cfg2.template_root().name == "templates", "template_root 解析",
            str(cfg2.template_root()))

    # 非法配置应被报出来
    bad = Config()
    bad.data["input"]["speed"] = 999
    bad.data["safety"]["fail_safe_key"] = "not_a_key"
    bad.data["tasks"]["active"] = ["no_such_task"]
    problems = bad.validate()
    c.check(any("speed" in x for x in problems), "非法 speed 被检出")
    c.check(any("fail_safe_key" in x for x in problems), "非法按键被检出")
    c.check(any("no_such_task" in x for x in problems), "未知任务被检出")

    # TOML 往返
    dumped = dump_toml(cfg.data)
    rt = tmp / "roundtrip.toml"
    rt.write_text(dumped, encoding="utf-8")
    cfg3 = Config.load(rt)
    c.check(cfg3.get("input.speed") == cfg.get("input.speed"), "TOML 往返 speed 一致")
    c.check(cfg3.get("scenes.lobby.any") == cfg.get("scenes.lobby.any"), "TOML 往返嵌套数组一致")
    c.check(cfg3.get("tasks.repeat_stage.enter_sequence", []) ==
            cfg.get("tasks.repeat_stage.enter_sequence", []), "TOML 往返序列一致")
    # TOML 规范里没有 null 类型，值为 None 的键在序列化时被跳过；
    # 读回来会回退到 DEFAULTS（同样是 None），语义保持一致。
    c.check(cfg.get("tasks.secret_shop.gold_region") is None, "None 默认值正确")
    c.check(cfg3.get("tasks.secret_shop.gold_region") is None, "None 值往返后仍为 None")
    c.check("gold_region" not in dumped, "None 键不应被写进 TOML（TOML 没有 null）")


def _test_sequence(c: Checker) -> None:
    print("\n[5] 配置驱动点击序列")

    seq = ClickSequence([
        {"click": "a/btn1", "wait_for": "a/next", "timeout": 12},
        {"click": "a/btn2"},
        {"not_a_step": True},          # 应被忽略
        {"click": "a/btn3", "required": False},
    ])
    c.check(len(seq) == 3, "无效步骤被过滤", f"len={len(seq)}")
    c.check(bool(seq), "非空序列为真")

    empty = ClickSequence([])
    c.check(not empty and len(empty) == 0, "空序列为假")

    # 用假 bot 跑一遍，验证按序执行且能感知失败
    class FakeBot:
        def __init__(self, fail_on=None):
            self.calls = []
            self.fail_on = fail_on

        def click_template(self, name, **kw):
            self.calls.append(name)
            return name != self.fail_on

        def random_sleep(self, span):
            pass

        def wait_any(self, names, timeout=0):
            return object()

    bot = FakeBot()
    c.check(seq.run(bot, log_prefix="t"), "序列执行成功")
    c.check(bot.calls == ["a/btn1", "a/btn2", "a/btn3"], "按顺序点击", str(bot.calls))

    bot2 = FakeBot(fail_on="a/btn2")
    c.check(not seq.run(bot2, log_prefix="t"), "中途失败返回 False")
    c.check(bot2.calls == ["a/btn1", "a/btn2"], "失败后不再继续", str(bot2.calls))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_selftest(verbose=True))
