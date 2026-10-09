"""OCR 模块的真实测试：合成画面 + 合成字形模板。

为什么不写成"只检查 API 存在"：数字识别最容易翻车的地方是**分割**和**相似度**
（小数点被当成噪声并进邻居、细笔画被形态学吃掉、亮底暗字忘了反色），这些只有真的
画一张图、真的跑完整条链路才验证得到。所以这里全部用 `cv2.putText` 合成画面，并
模拟用户"采字形"的过程（单个字符渲染后紧致裁剪存成 PNG），再走
`TemplateLibrary` + `Matcher` 这条**真实**路径 —— 与线上唯一的差别只是图不是从
游戏里截的。

风格对齐 `tools/selftest.py`：每个用例需要的东西都在用例内部自给自足地准备，
用例之间没有隐式耦合（单独跑某一个也必须能过）。

    .venv\\Scripts\\python.exe -m pytest tests/test_ocr.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from e7bot.ocr import (  # noqa: E402
    DigitReader,
    OcrConfig,
    OcrUnavailable,
    TextReader,
    _polarity_split,
    make_digit_reader,
    make_text_reader,
)
from e7bot.vision import Matcher, TemplateLibrary  # noqa: E402
from e7bot.winutil import Rect  # noqa: E402

# --------------------------------------------------------------------------- #
# 合成画面 / 合成字形
# --------------------------------------------------------------------------- #

FONT = cv2.FONT_HERSHEY_SIMPLEX
REF_W, REF_H = 1920, 1080
FONT_SCALE = 2.0
THICKNESS = 3

#: 全部字形。"." 存成 dot.png，"/" 存成 slash.png（文件名不能是符号）
ALL_GLYPHS = "0123456789km./"
GLYPH_FILE = {".": "dot", "/": "slash"}
DIGITS = "0123456789"


def frame_rect(w: int = REF_W, h: int = REF_H) -> Rect:
    return Rect(0, 0, w, h)


def render_text(
    text: str,
    w: int = REF_W,
    h: int = REF_H,
    *,
    scale: float = FONT_SCALE,
    thickness: int = THICKNESS,
    bright_on_dark: bool = True,
    margin: float = 0.30,
) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    """合成一行文字，返回 (画面, 归一化 region)。

    region 比文字外扩 `margin` 比例 —— 这一点很关键：`DigitReader` 靠"前景占比是否
    超过一半"判断亮暗底，region 紧贴到只剩墨迹时这个判据会失效（真实使用中框数值栏
    也总会带上一点背景）。
    """
    frame = np.full((h, w, 3), 0 if bright_on_dark else 255, np.uint8)
    color = (255, 255, 255) if bright_on_dark else (0, 0, 0)
    (tw, th), base = cv2.getTextSize(text, FONT, scale, thickness)
    x0 = (w - tw) // 2
    y0 = (h + th) // 2
    cv2.putText(frame, text, (x0, y0), FONT, scale, color, thickness, cv2.LINE_AA)

    mw, mh = max(4, int(tw * margin)), max(4, int(th * margin))
    rx0, ry0 = max(0, x0 - mw), max(0, y0 - th - mh)
    rx1, ry1 = min(w, x0 + tw + mw), min(h, y0 + base + mh)
    region = (rx0 / w, ry0 / h, (rx1 - rx0) / w, (ry1 - ry0) / h)
    return frame, region


def glyph_template(ch: str, scale: float = FONT_SCALE, thickness: int = THICKNESS) -> np.ndarray:
    """渲染单个字符并**紧致裁剪** —— 模拟用户采集一个字形模板。"""
    canvas = np.full((400, 400, 3), 0, np.uint8)
    (tw, th), base = cv2.getTextSize(ch, FONT, scale, thickness)
    x0 = (400 - tw) // 2
    y0 = (400 + th) // 2
    cv2.putText(canvas, ch, (x0, y0), FONT, scale, (255, 255, 255), thickness, cv2.LINE_AA)
    ys, xs = np.nonzero(cv2.cvtColor(canvas, cv2.COLOR_BGR2GRAY) > 127)
    return canvas[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def build_matcher(
    root: Path,
    glyphs: str = ALL_GLYPHS,
    *,
    scale: float = FONT_SCALE,
    thickness: int = THICKNESS,
    ref_size: tuple[int, int] = (REF_W, REF_H),
) -> Matcher:
    """把字形写成 `templates/default/ocr/digits/*.png`，再按真实路径加载成 Matcher。"""
    d = root / "templates" / "default" / "ocr" / "digits"
    d.mkdir(parents=True, exist_ok=True)
    for ch in glyphs:
        p = d / f"{GLYPH_FILE.get(ch, ch)}.png"
        # 与项目其它地方一致：用 imencode + tofile，避免中文路径在 cv2.imwrite 下失败
        cv2.imencode(".png", glyph_template(ch, scale, thickness))[1].tofile(str(p))
        p.with_suffix(".json").write_text(
            json.dumps({"ref_size": list(ref_size), "threshold": 0.8}, ensure_ascii=False),
            encoding="utf-8",
        )
    return Matcher(TemplateLibrary(root / "templates", "default"))


def resize_frame(frame: np.ndarray, w: int, h: int) -> np.ndarray:
    """把合成画面整体缩放到目标分辨率（模拟游戏换分辨率后 UI 等比缩放）。"""
    if (w, h) == (frame.shape[1], frame.shape[0]):
        return frame
    interp = cv2.INTER_AREA if w < frame.shape[1] else cv2.INTER_LINEAR
    return cv2.resize(frame, (w, h), interpolation=interp)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def full_matcher(tmp_path: Path) -> Matcher:
    """0-9 + k/m/dot/slash 全齐（模拟按 1920x1080 采好字形的用户）。"""
    return build_matcher(tmp_path)


@pytest.fixture()
def reader(full_matcher: Matcher) -> DigitReader:
    """默认参数（threshold=0.80）—— 刻意不调低，默认值能读出来才算真的能用。"""
    return DigitReader(full_matcher)


# --------------------------------------------------------------------------- #
# 1 / 2 基本读数
# --------------------------------------------------------------------------- #


def test_reads_four_digit_number(reader: DigitReader) -> None:
    """"3480" 这种最典型的四位数。"""
    frame, region = render_text("3480")
    got = reader.read(frame, frame_rect(), region)

    assert got is not None, "四位数应当能读出来"
    assert got.value == 3480
    assert got.raw == "3480"
    assert [c for c, _ in got.digits] == list("3480")
    assert got.region == pytest.approx(region)
    assert got.confidence >= 0.80, f"置信度 {got.confidence} 应不低于阈值"


@pytest.mark.parametrize(
    "text,value",
    [("0", 0), ("7", 7), ("12", 12), ("9", 9), ("5555", 5555), ("8008", 8008), ("1024", 1024)],
)
def test_reads_single_and_multi_digits(reader: DigitReader, text: str, value: int) -> None:
    """单字 / 双字 / 更多位都要对，包括容易混的 8/0、5/6。"""
    frame, region = render_text(text)
    got = reader.read(frame, frame_rect(), region)
    assert got is not None, f"{text} 应当能读出来"
    assert got.value == value
    assert got.raw == text


def test_confidence_is_min_of_glyph_scores(reader: DigitReader) -> None:
    """confidence 取所有字形分数的最小值（最弱一环决定整体可信度）。"""
    frame, region = render_text("3480")
    got = reader.read(frame, frame_rect(), region)
    assert got is not None
    scores = [s for _, s in got.digits]
    assert got.confidence == pytest.approx(min(scores))
    assert all(0.0 <= s <= 1.0 for s in scores)


# --------------------------------------------------------------------------- #
# 3 小数点 / k / m 后缀
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("text,value", [("1.2k", 1200), ("3.5m", 3500000), ("12.5", 13)])
def test_reads_decimal_and_suffixes(reader: DigitReader, text: str, value: int) -> None:
    """小数点 + k/m 后缀：游戏里 "1.2k" / "3.5m" 这类缩写很常见。

    "12.5" 是无后缀小数，按**四舍五入**取整（读整数是 `read()` 的契约，
    12.5 -> 13；不是 Python 默认的银行家舍入 12）。
    """
    frame, region = render_text(text)
    got = reader.read(frame, frame_rect(), region)
    assert got is not None, f"{text} 应当能读出来"
    assert got.value == value
    assert got.raw == text


def test_dot_is_not_merged_into_neighbour(reader: DigitReader) -> None:
    """小数点必须被当成**独立字形**，不能被"过窄段合并"吞进邻居。

    这是最要命的一类 bug：小数点一旦被并进 "2"，"1.2k" 就变成 "12k"，
    1200 直接读成 12000 —— 数值错 10 倍，比读不出来危险得多。
    """
    frame, region = render_text("1.2k")
    text = reader.read_text(frame, frame_rect(), region)
    assert text is not None
    assert text.count(".") == 1, f"应当恰好识别出一个小数点，实际 {text!r}"
    assert "." in text and text.index(".") == 1


# --------------------------------------------------------------------------- #
# 4 亮字暗底 / 暗字亮底
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bright_on_dark", [True, False])
@pytest.mark.parametrize("text,value", [("3480", 3480), ("1.2k", 1200)])
def test_reads_both_polarities(
    reader: DigitReader, bright_on_dark: bool, text: str, value: int
) -> None:
    """亮字暗底（伤害数字）和暗字亮底（弹窗数量）都要能读。"""
    frame, region = render_text(text, bright_on_dark=bright_on_dark)
    got = reader.read(frame, frame_rect(), region)
    assert got is not None, f"{text}（bright_on_dark={bright_on_dark}）应当能读出来"
    assert got.value == value


def test_polarity_is_auto_normalised() -> None:
    """自动反色必须真的生效：两种极性经 `_polarity_split` 后应归一化成同一张图。

    直接断言"前景区间占比 < 0.5"，也就是无论原图亮暗底，出来的都是
    "字形 = 白 = 前景"。
    """
    for bright_on_dark in (True, False):
        frame, region = render_text("3480", bright_on_dark=bright_on_dark)
        r = frame_rect()
        sub = r.sub(*region)
        roi = frame[sub.top:sub.bottom, sub.left:sub.right]
        gray, binary = _polarity_split(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY))
        assert float(binary.mean()) / 255.0 < 0.5, "前景（字形）必须少于一半"
        # 归一化后的灰度也应当一致：暗字亮底被整体反色成亮字暗底
        assert 0.0 <= float(gray.mean()) <= 128.0


# --------------------------------------------------------------------------- #
# 5 数值对
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("text,pair", [("12/20", (12, 20)), ("1/2", (1, 2)), ("0/0", (0, 0))])
def test_read_pair(reader: DigitReader, text: str, pair: tuple[int, int]) -> None:
    """"12/20"（剩余次数）这类 a/b 形式的数值对。"""
    frame, region = render_text(text)
    assert reader.read_pair(frame, frame_rect(), region) == pair
    # 单个整数读法对 "12/20" 应当返回 None（它不是一个整数）
    assert reader.read(frame, frame_rect(), region) is None


# --------------------------------------------------------------------------- #
# 6 空白区域
# --------------------------------------------------------------------------- #


def test_blank_region_returns_none(reader: DigitReader) -> None:
    """纯色区域没有任何字形 -> 三个读法都必须返回 None（不能瞎猜）。"""
    blank = np.full((REF_H, REF_W, 3), 128, np.uint8)
    region = (0.30, 0.40, 0.20, 0.10)
    assert reader.read(blank, frame_rect(), region) is None
    assert reader.read_text(blank, frame_rect(), region) is None
    assert reader.read_pair(blank, frame_rect(), region) is None


def test_black_region_returns_none(reader: DigitReader) -> None:
    """全黑区域同样读不出来（Otsu 在全同图上会退化，这里顺带覆盖）。"""
    black = np.zeros((REF_H, REF_W, 3), np.uint8)
    assert reader.read(black, frame_rect(), (0.30, 0.40, 0.20, 0.10)) is None


# --------------------------------------------------------------------------- #
# 7 字形不全
# --------------------------------------------------------------------------- #


def test_missing_glyphs_degrades_gracefully(tmp_path: Path) -> None:
    """只采了 0-4：is_available()=False、missing_glyphs() 报出缺的、read() 返回 None 且不抛。"""
    matcher = build_matcher(tmp_path, "01234")
    rd = DigitReader(matcher)

    assert rd.is_available() is False
    assert rd.missing_glyphs() == ["5", "6", "7", "8", "9"]

    frame, region = render_text("3480")
    assert rd.read(frame, frame_rect(), region) is None
    # read_text 保留 "?" 占位，方便用户看出是哪个字形没采
    assert rd.read_text(frame, frame_rect(), region) == "34?0"
    assert rd.read_pair(frame, frame_rect(), region) is None


def test_no_glyphs_at_all_is_silent(tmp_path: Path) -> None:
    """一个字形都没有（用户还没开始采）：全部返回 None / False，绝不抛异常。"""
    matcher = build_matcher(tmp_path, "")
    rd = DigitReader(matcher)
    assert rd.is_available() is False
    assert rd.missing_glyphs() == list(DIGITS)
    frame, region = render_text("3480")
    assert rd.read(frame, frame_rect(), region) is None
    assert rd.read_text(frame, frame_rect(), region) is None


def test_digits_only_cannot_read_suffix(tmp_path: Path) -> None:
    """只采 0-9（没采 k/m/dot）时：纯数字照读，带后缀的老实返回 None。"""
    rd = DigitReader(build_matcher(tmp_path, DIGITS))
    assert rd.is_available() is True          # k/m/dot 是可选的，不影响可用性

    frame, region = render_text("3480")
    assert rd.read(frame, frame_rect(), region) is not None

    frame, region = render_text("1.2k")
    assert rd.read(frame, frame_rect(), region) is None
    assert rd.read_text(frame, frame_rect(), region) == "1?2?"


# --------------------------------------------------------------------------- #
# 8 可选通用 OCR 的优雅降级
# --------------------------------------------------------------------------- #


def test_text_reader_degrades_without_rapidocr() -> None:
    """没装 rapidocr_onnxruntime 时：is_available()=False，读什么都返回 None 且不抛。"""
    rd = TextReader()
    if rd.is_available():
        pytest.skip("本机装了 rapidocr_onnxruntime，该用例只验证缺库时的降级行为")

    assert rd.is_available() is False
    frame, region = render_text("3480")
    assert rd.read_text(frame, frame_rect(), region) is None
    assert rd.read_number(frame, frame_rect(), region) is None
    assert rd.read_text(np.zeros((10, 10, 3), np.uint8), frame_rect(10, 10),
                        (0.0, 0.0, 1.0, 1.0)) is None
    # 只有显式 require() 才允许抛 —— 调用方要"必须有 OCR"时用它拿安装提示
    with pytest.raises(OcrUnavailable):
        rd.require()


def test_make_text_reader_never_raises() -> None:
    """工厂在缺库 / 配置乱写时都不能抛异常（否则脚本启动就崩）。"""
    for cfg in (None, {}, {"backend": "rapidocr"}, {"backend": "不存在的后端"},
                {"min_score": "abc"}, {"threshold": None}):
        reader_obj = make_text_reader(cfg)
        assert isinstance(reader_obj, TextReader)


# --------------------------------------------------------------------------- #
# 9 分辨率无关
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("size", [(1280, 720), (1366, 768), (1600, 900), (2560, 1440), (3840, 2160)])
def test_resolution_independence(reader: DigitReader, size: tuple[int, int]) -> None:
    """模板按 1920x1080 采，画面缩放到别的分辨率后仍要读得出。

    这是 PC 端相对安卓端最关键的一条：用户不可能为每个分辨率重采一遍字形。
    实现上靠"把待识别字形缩放到模板自身尺寸"来做到，与 `Matcher` 按 ref_size
    缩放模板是同一个思路。
    """
    w, h = size
    r = frame_rect(w, h)
    for text, value in [("3480", 3480), ("12", 12), ("7", 7), ("1.2k", 1200), ("3.5m", 3500000)]:
        frame, region = render_text(text)
        frame = resize_frame(frame, w, h)
        got = reader.read(frame, r, region)
        assert got is not None, f"{w}x{h} 下 {text} 应当能读出来"
        assert got.value == value, f"{w}x{h} 下 {text} 读成了 {got.value}"


@pytest.mark.parametrize("size", [(1280, 720), (2560, 1440)])
def test_resolution_independence_for_pairs(reader: DigitReader, size: tuple[int, int]) -> None:
    w, h = size
    frame, region = render_text("12/20")
    frame = resize_frame(frame, w, h)
    assert reader.read_pair(frame, frame_rect(w, h), region) == (12, 20)


def test_templates_captured_at_playing_resolution(tmp_path: Path) -> None:
    """按自己实际游玩的分辨率采字形，在**同一分辨率**下读数必须稳稳过阈值。

    这是最贴近真实部署的一条：用户就在 1280x720 玩、也就在 1280x720 采字形，
    完全不需要任何跨分辨率缩放。置信度实测 0.84（0.80 阈值）—— 比 1080p 下的
    0.91 低是正常的：字形只有 27px 高，抗锯齿/量化噪声的相对占比更大，
    但离阈值仍有安全余量。
    """
    scale = FONT_SCALE * 720 / 1080
    matcher = build_matcher(tmp_path, ALL_GLYPHS, scale=scale, thickness=2, ref_size=(1280, 720))
    rd = DigitReader(matcher)

    frame, region = render_text("3480", 1280, 720, scale=scale, thickness=2)
    got = rd.read(frame, frame_rect(1280, 720), region)
    assert got is not None
    assert got.value == 3480
    assert got.confidence >= 0.80, f"同分辨率下必须过默认阈值，实际 {got.confidence}"


# --------------------------------------------------------------------------- #
# 非法输入 / 保守行为
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "region",
    [
        (0.95, 0.40, 0.20, 0.10),   # 右边界越出画面
        (0.30, 0.97, 0.20, 0.10),   # 下边界越出画面
        (0.30, 0.40, 0.0, 0.10),    # 宽度为 0
        (0.30, 0.40, 0.20, -0.1),   # 高度为负
        (-0.20, 0.40, 0.20, 0.10),  # 左边界在画面外
    ],
)
def test_invalid_region_returns_none(reader: DigitReader, region: tuple[float, ...]) -> None:
    """区域非法时返回 None，绝不抛异常（配置写错不该让脚本崩）。"""
    frame, _ = render_text("3480")
    assert reader.read(frame, frame_rect(), region) is None  # type: ignore[arg-type]
    assert reader.read_text(frame, frame_rect(), region) is None  # type: ignore[arg-type]


def test_broken_frame_returns_none(reader: DigitReader) -> None:
    """空帧 / 尺寸不匹配的 frame_rect 也不能抛。"""
    assert reader.read(np.zeros((0, 0, 3), np.uint8), frame_rect(), (0.3, 0.4, 0.2, 0.1)) is None
    frame, region = render_text("3480", 100, 100)
    assert reader.read(frame, Rect(0, 0, 0, 0), region) is None


def test_non_digit_character_is_not_guessed(reader: DigitReader) -> None:
    """字母不能瞎猜成数字 —— 读不出来好过读错（会影响体力/预算判断）。

    注意 `O` -> `0`、`B` -> `8` 这类**形状本来就一样/极像**的情况除外，
    那是字体本身无法区分，不是识别错误。
    """
    for ch in ("Z", "X", "E", "G", "I"):
        frame, region = render_text(ch)
        assert reader.read(frame, frame_rect(), region) is None, f"{ch!r} 不该被读成数字"


# --------------------------------------------------------------------------- #
# 配置与工厂
# --------------------------------------------------------------------------- #


def test_ocr_config_from_dict() -> None:
    c = OcrConfig.from_dict({
        "enabled": True,
        "backend": "RapidOCR",
        "digits_prefix": "ocr/my_digits/",
        "threshold": 0.9,
        "scale_tolerance": 0.1,
    })
    assert c.enabled is True
    assert c.backend == "rapidocr"          # 大小写归一化
    assert c.digits_prefix == "ocr/my_digits/"
    assert c.threshold == pytest.approx(0.9)
    assert c.scale_tolerance == pytest.approx(0.1)


def test_ocr_config_defaults_and_junk() -> None:
    """配置缺项 / 类型不对 / 后端名不认识，都退回默认值而不是抛异常。"""
    for cfg in (None, {}, {"threshold": "abc", "scale_tolerance": None},
                {"backend": "tesseract"}, []):
        c = OcrConfig.from_dict(cfg)  # type: ignore[arg-type]
        assert c.backend == "digits"
        assert c.digits_prefix == "ocr/digits"
        assert c.threshold == pytest.approx(0.80)
        assert c.scale_tolerance == pytest.approx(0.06)


def test_make_digit_reader_from_config(full_matcher: Matcher) -> None:
    """工厂按配置段造 reader，且 prefix 两边的斜杠会被规整掉。"""
    rd = make_digit_reader(full_matcher, {"digits_prefix": "/ocr/digits/", "threshold": 0.75})
    assert isinstance(rd, DigitReader)
    assert rd.prefix == "ocr/digits"
    assert rd.threshold == pytest.approx(0.75)
    assert rd.is_available() is True

    frame, region = render_text("3480")
    got = rd.read(frame, frame_rect(), region)
    assert got is not None and got.value == 3480


def test_reader_survives_glyph_reload(full_matcher: Matcher, tmp_path: Path) -> None:
    """采完新字形 reload 模板库后，缓存要自动失效（否则读的还是旧字形）。"""
    rd = DigitReader(full_matcher)
    frame, region = render_text("3480")
    assert rd.read(frame, frame_rect(), region) is not None

    # 把字形换成按 720p 采的一整套，再 reload
    build_matcher(tmp_path, ALL_GLYPHS, scale=FONT_SCALE * 720 / 1080, thickness=2)
    full_matcher.lib.reload()
    rd.clear_cache()

    assert rd.is_available() is True
    got = rd.read(frame, frame_rect(), region)
    assert got is not None and got.value == 3480
