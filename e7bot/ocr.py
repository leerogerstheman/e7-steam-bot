"""数字 / 文本识别（OCR）。

本项目有一条硬规则：**模板必须从真实客户端采集，不预置任何 UI 图**。
数字识别同样遵守它 —— 与其塞一个几十 MB 的通用 OCR 模型（还得拖上 onnxruntime），
不如让用户采 10 张数字字形图（`0.png` ~ `9.png`），用模板匹配读数值：

1. 游戏里的数值是**固定点阵字体**，同一数字在不同界面是同一张字形。模板匹配
   比通用 OCR 更准（不会把 8 认成 3）也更快（10 张小图的相关系数，微秒级）；
2. 零新依赖：只用 cv2 / numpy，与项目现有 `TemplateLibrary` / `Matcher` 体系一致；
3. 通用 OCR 模型面对「亮字暗底 + 描边 + 半透明底」的游戏 HUD 反而容易翻车，
   而且换语言 / 换分辨率就得换模型，与「不预置图」的哲学冲突。

可选的 `TextReader` 只在用户自己装了 `rapidocr_onnxruntime` 时才可用（读中文 /
英文文本，例如商品名、任务标题）。它**必须优雅降级**：库不存在时
`is_available()` 返回 False、所有读取方法返回 None，绝不抛异常 —— 脚本运行中
不能因为缺一个可选库就崩。

## 识别流程（`DigitReader.read`）

```
裁 ROI -> 灰度 -> Otsu 二值化 + 自动反色 -> 开运算去噪
       -> 垂直投影分割字形 -> 每个字形缩放到模板尺寸 -> 与 0-9 逐个比相似度
       -> 拼接 -> 解析 "3480" / "1.2k" / "3.5m"
```

**相似度用的是灰度图而不是二值图**：小字形（尤其是小数点）二值化之后几乎是一块
实心方块，`TM_CCOEFF_NORMED` 的分母趋 0、结果全凭几个边缘像素抖动，实测"1.2k"
里的点只能拿到 0.49 分（判为读不出来）。改成拿灰度（保留抗锯齿的笔画信息）后
同一个点能拿到 0.97 分。分割仍然用二值图 —— 两件事各自用合适的图。

坐标约定与项目其余部分一致：`region` 是相对游戏窗口客户区的**归一化** (x, y, w, h)。

## 字形模板怎么放

```
templates/<profile>/ocr/digits/
├── 0.png ~ 9.png     必需（缺一个就 is_available()=False）
├── k.png            可选，"1.2k" 的千
├── m.png            可选，"3.5m" 的百万
├── dot.png          可选，小数点（不能叫 ".png"）
└── slash.png        可选，斜杠（"12/20"）
```

每个 PNG 都应该是**单个字形**（框紧一点最好，本模块也会自动把多带的背景裁掉）。
按 1920x1080 采集即可 —— 匹配前会把待识别字形缩放到模板自身尺寸，所以
**天生分辨率无关**，换分辨率不用重采。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

import cv2
import numpy as np

from .vision import Matcher
from .winutil import Rect

__all__ = [
    "OcrUnavailable",
    "NumberReading",
    "DigitReader",
    "TextReader",
    "OcrConfig",
    "make_digit_reader",
    "make_text_reader",
]

#: 必备字形。缺任何一个都没法读数值，`is_available()` 直接判 False
REQUIRED_DIGITS = "0123456789"

#: 可选字形（模板文件名）。只在读 "1.2k" / "12/20" 这类文本时才需要
OPTIONAL_GLYPHS: tuple[str, ...] = ("k", "m", "dot", "slash")

#: 模板文件名 -> 实际字符。只有 dot/slash 需要改名，因为 "." "/" 不能当文件名
_GLYPH_CHAR: dict[str, str] = {"dot": ".", "slash": "/"}

#: 读不出来的字形用这个占位。出现它就说明整个读数不可信（见 DigitReader.read）
_UNKNOWN = "?"


class OcrUnavailable(RuntimeError):
    """OCR 后端不可用（缺库 / 缺字形模板 / 模型加载失败）。

    这是**可预期的环境问题**，不是程序缺陷。脚本运行期绝不允许因为
    「用户还没采字形」或「没装 rapidocr」就崩掉，所以公开 API 一律返回
    None / False；只有显式要求「必须有 OCR」的调用点才会看到这个异常
    （例如 `TextReader.require()`、启动自检里给安装提示）。
    """


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class NumberReading:
    """一次数值识别的结果。"""

    value: int
    raw: str
    region: tuple[float, float, float, float]
    confidence: float
    digits: list[tuple[str, float]]


@dataclass
class OcrConfig:
    """`config/default.toml` 里 `[ocr]` 段的类型化视图。

    单独放一个 dataclass 而不是直接读 dict：配置项散落在各处最容易出现
    「这里用 0.8、那里用 0.86」的不一致，集中解析一次就没这问题。
    """

    enabled: bool = False
    backend: str = "digits"      # "digits" | "rapidocr"
    digits_prefix: str = "ocr/digits"
    threshold: float = 0.80
    scale_tolerance: float = 0.06

    @classmethod
    def from_dict(cls, cfg: Optional[dict[str, Any]]) -> "OcrConfig":
        """从配置段构造，缺项/类型不对都退回默认值（配置永远不该让脚本崩）。"""
        raw: dict[str, Any] = cfg if isinstance(cfg, dict) else {}

        def pick(*names: str, default: Any = None) -> Any:
            for n in names:
                if n in raw and raw[n] is not None:
                    return raw[n]
            return default

        backend = str(pick("backend", default="digits")).strip().lower()
        if backend not in ("digits", "rapidocr"):
            backend = "digits"          # 未知后端不报错，退回默认（保守）

        try:
            threshold = float(pick("threshold", default=0.80))
        except (TypeError, ValueError):
            threshold = 0.80
        try:
            tol = float(pick("scale_tolerance", default=0.06))
        except (TypeError, ValueError):
            tol = 0.06

        return cls(
            enabled=bool(pick("enabled", default=False)),
            backend=backend,
            digits_prefix=str(pick("digits_prefix", "prefix", default="ocr/digits")),
            threshold=threshold,
            scale_tolerance=tol,
        )


# --------------------------------------------------------------------------- #
# 图像处理工具
# --------------------------------------------------------------------------- #


def _crop_roi(
    frame: np.ndarray,
    frame_rect: Rect,
    region: tuple[float, float, float, float],
) -> Optional[np.ndarray]:
    """按归一化 region 从帧里裁出 ROI。

    偏移基准容易搞错，这里说明白：`frame` 是**客户区截图**（左上角对应
    frame_rect 的左上角），而 region 是相对客户区的归一化坐标。所以要先
    `frame_rect.sub()` 变成屏幕绝对坐标，再减掉 `frame_rect.left/top` 得到帧内
    偏移 —— 与 `Matcher._match_one` 的写法完全一致。

    区域跑出帧外时返回 None：宁可读不出来，也不能拿半个数字去匹配
    （半个 "8" 很可能被认成 "3"，那比读不出来危险得多）。
    """
    try:
        if frame is None or frame.size == 0:
            return None
        if frame_rect.width <= 0 or frame_rect.height <= 0:
            return None

        x, y, w, h = (float(v) for v in region)
        if not (w > 0 and h > 0):
            return None

        r = frame_rect.sub(x, y, w, h)
        off_x, off_y = r.left - frame_rect.left, r.top - frame_rect.top
        if off_x < 0 or off_y < 0:
            return None

        roi = frame[off_y: off_y + r.height, off_x: off_x + r.width]
        if roi.shape[0] < r.height or roi.shape[1] < r.width:
            return None
        if roi.shape[0] < 4 or roi.shape[1] < 4:
            return None
        return roi
    except Exception:
        return None


def _to_gray(img: np.ndarray) -> np.ndarray:
    """统一转灰度（已经是灰度就原样返回）。"""
    if img.ndim == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return img


def _polarity_split(gray: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Otsu 二值化 + 自动反色，返回 (字形=亮 的灰度图, 字形=白 的二值图)。

    游戏 HUD 里同一个数值框可能是**亮字暗底**（伤害数字）也可能是**暗字亮底**
    （弹窗里的消耗数量），写死"白字"必然有一半场景读不出来。Otsu 只负责把两类
    像素分开，方向由前景占比决定：前景超过一半说明**白的那一类是背景**，反色。
    灰度图跟着一起反色，保证"字形=亮"，后面拿灰度做相似度时两个来源方向一致。

    注意这个判据要求 ROI 里**留一点背景边距**（正常框选数值栏都会带上），
    如果 region 紧贴到只剩墨迹，前景占比会虚高而误判方向 —— 这也是
    `docs/OCR.md` 里反复强调"region 留边距"的原因。
    """
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if float(binary.mean()) > 127.5:
        binary = cv2.bitwise_not(binary)
        gray = cv2.bitwise_not(gray)
    return gray, binary


def _denoise(binary: np.ndarray) -> np.ndarray:
    """开运算去孤立噪点 —— 但**不能把笔画一起削掉**。

    低分辨率下字形笔画只有 1~2px，稍大的核会把一个数字拆成两半，读出来就是
    错的数值。所以这里做两步保险：核按 ROI 尺寸取 2 或 3（矩形核比椭圆核对
    细线更宽容），并且**只在"没伤到主体"时才采用开运算结果**（前景像素损失
    超过 25% 就判定为削到笔画了，退回原图）。宁可留着噪点让这次读数变成
    "读不出来"，也不要削掉笔画读出一个错的数值。
    """
    h, w = binary.shape[:2]
    k = 2 if min(h, w) < 64 else 3
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    opened = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)

    before = int(np.count_nonzero(binary))
    after = int(np.count_nonzero(opened))
    if before == 0 or after < before * 0.75:
        return binary
    return opened


def _runs(mask: np.ndarray) -> list[list[int]]:
    """一维布尔数组 -> 连续 True 段的 [start, end) 列表。"""
    out: list[list[int]] = []
    start = -1
    for i, v in enumerate(mask.tolist()):
        if v:
            if start < 0:
                start = i
        elif start >= 0:
            out.append([start, i])
            start = -1
    if start >= 0:
        out.append([start, len(mask)])
    return out


def _merge_runs(runs: list[list[int]], min_w: int, merge_gap: int) -> list[tuple[int, int]]:
    """把过窄的列段并进相邻段。

    过窄的段只有两种可能：**笔画断裂**（一个数字被噪点切成两段）或**噪声**，
    两种都该并回邻近的字形。但判据不能只看宽度 —— 小数点也是一个很窄的段，
    如果无脑并进邻居，"1.2k" 就会被读成 "12k"（1200 变 12000，直接读错数值）。
    真正的笔画断裂只留下 1~2px 的缝，而字形之间的正常间距远大于此，所以这里
    **要求缝隙 <= merge_gap 才合并**，孤立的小点（小数点）自然就留下了。

    先向前并（窄段贴前一段），再向后并（前一段太远、贴后一段的情况）。
    """
    segs = [list(r) for r in runs]

    i = 1
    while i < len(segs):
        s, e = segs[i]
        if (e - s) < min_w and (s - segs[i - 1][1]) <= merge_gap:
            segs[i - 1][1] = e
            del segs[i]
            continue
        i += 1

    i = 0
    while i < len(segs) - 1:
        s, e = segs[i]
        if (e - s) < min_w and (segs[i + 1][0] - e) <= merge_gap:
            segs[i + 1][0] = s
            del segs[i]
            continue
        i += 1

    return [(s, e) for s, e in segs]


def _segment(binary: np.ndarray) -> list[tuple[int, int, int, int]]:
    """垂直投影分割字形，返回每个字形的紧致包围盒 (x0, y0, x1, y1)。

    为什么用投影而不是 findContours：点阵字体的一个数字**本来就可能不连通**
    （"5" 的两笔、"4" 的斜杠与竖笔），轮廓法会把它拆成多个目标；按列投影天然
    把"同一列区间里的墨迹"当成一个字形，正符合数字排版的实际情况。

    每段再各自做一次水平投影取紧致包围盒（而不是全 ROI 统一裁）：这样小数点
    的框就只有那一个小方块，不会带一堆空白把形状匹配带偏。
    """
    if binary.size == 0:
        return []

    ink_rows = np.where(binary.any(axis=1))[0]
    if ink_rows.size == 0:
        return []

    y0, y1 = int(ink_rows[0]), int(ink_rows[-1]) + 1
    # 先用水平投影裁掉上下空白：后面"15% 高度"的宽度门限才有意义
    band = binary[y0:y1]
    roi_h = band.shape[0]

    runs = _runs(band.any(axis=0))
    if not runs:
        return []

    min_w = max(1, int(round(0.15 * roi_h)))
    merge_gap = max(2, int(round(0.08 * roi_h)))
    boxes: list[tuple[int, int, int, int]] = []
    for s, e in _merge_runs(runs, min_w, merge_gap):
        sub = band[:, s:e]
        rows = np.where(sub.any(axis=1))[0]
        if rows.size == 0:
            continue
        boxes.append((s, y0 + int(rows[0]), e, y0 + int(rows[-1]) + 1))
    return boxes


_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)?$")


def _parse_number(raw: str) -> Optional[int]:
    """把 "3480" / "1.2k" / "3.5m" 解析成整数，解析不了返回 None。

    支持 k/m 后缀（游戏里"1.2k"这种缩写很常见），大小写都认（模板名是小写，
    但游戏贴图可能画的是大写，字形相同所以没必要区分）。
    """
    s = raw.strip()
    if not s or _UNKNOWN in s:
        return None

    mult = 1
    if s[-1] in "kK":
        mult, s = 1000, s[:-1]
    elif s[-1] in "mM":
        mult, s = 1_000_000, s[:-1]

    if not _NUMBER_RE.match(s):
        return None
    try:
        # 无后缀的小数（"1.5"）按四舍五入取整。用 int(x + 0.5) 而不是 round()：
        # Python 的 round() 是银行家舍入，round(2.5) == 2 会让人莫名其妙。
        # （s 由正则保证非负，所以 int(x + 0.5) 就是标准的四舍五入。）
        return int(float(s) * mult + 0.5)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# 数字字形读取器（主路径）
# --------------------------------------------------------------------------- #


class DigitReader:
    """用用户采集的数字字形模板，从屏幕区域读整数。

    字形模板放在 `templates/<profile>/ocr/digits/` 下，命名 `0.png` ~ `9.png`，
    可选 `k.png`（千）、`m.png`（百万）、`dot.png`（小数点）、`slash.png`（斜杠，
    用于 "12/20"）。

    匹配**刻意不走 `matcher.find`**：那个是在整帧里滑窗搜索模板，而这里字形已经
    被分割出来了，只需要判断"这是哪个数字"。而且模板库里的图是按 ref_size 缩放
    过的，字形比较要的是**原生尺寸**（`tpl.image`），把待识别字形缩放到模板尺寸
    再比，才能做到分辨率无关。
    """

    #: 会尝试匹配的全部字形（数字 + 可选符号）
    CANDIDATES: tuple[str, ...] = tuple(REQUIRED_DIGITS) + OPTIONAL_GLYPHS

    def __init__(
        self,
        matcher: Matcher,
        prefix: str = "ocr/digits",
        threshold: float = 0.80,
        scale_tolerance: float = 0.06,
    ) -> None:
        self.matcher = matcher
        self.prefix = str(prefix).strip("/") or "ocr/digits"
        self.threshold = float(threshold)
        self.scale_tolerance = max(0.0, float(scale_tolerance))
        self._cache: Optional[dict[str, np.ndarray]] = None
        self._gen: int = -1

    # -- 模板 -------------------------------------------------------------- #

    def _name_of(self, glyph: str) -> str:
        return f"{self.prefix}/{glyph}"

    def _generation(self) -> int:
        """模板库"代数"：用模板数量当版本号，采完字形 reload 后缓存自动失效。"""
        try:
            return len(self.matcher.lib)
        except Exception:
            return -1

    def _load_glyph(self, glyph: str) -> Optional[np.ndarray]:
        """取单个字形的**原生尺寸灰度图**（比较用），缺模板返回 None。

        刻意不做二值化：小字形二值化后几乎是一块实心方块，相关系数会失去分辨力
        （实测"1.2k"里的点只能拿 0.49 分）。灰度保留了抗锯齿的笔画粗细信息。
        也刻意不按 ref_size 缩放：比较时把待识别字形缩放到这个原生尺寸即可。

        也刻意**不**自动裁剪模板（曾经试过按"少数派像素"找墨迹包围盒，结果在
        框得很紧的字形上把墨迹当成了背景、反而裁掉笔画，"k" 直接认不出来）。
        框松了会让分数整体下降，这个交给 docs/OCR.md 里的采集规范去管。
        """
        try:
            tpl = self.matcher.lib.get(self._name_of(glyph))
        except Exception:
            return None
        img = tpl.image
        if img is None or img.size == 0:
            return None
        gray = _to_gray(img)
        if gray.ndim != 2 or min(gray.shape[:2]) < 2:
            return None
        return np.ascontiguousarray(gray)

    def _templates(self) -> dict[str, np.ndarray]:
        """字符 -> 灰度模板（原生尺寸）。"""
        gen = self._generation()
        if self._cache is None or gen != self._gen:
            cache: dict[str, np.ndarray] = {}
            for g in self.CANDIDATES:
                img = self._load_glyph(g)
                if img is not None:
                    cache[_GLYPH_CHAR.get(g, g)] = img
            self._cache = cache
            self._gen = gen
        return self._cache

    def clear_cache(self) -> None:
        """采了新字形后手动清缓存（正常情况下 `_generation` 会自动发现）。"""
        self._cache = None
        self._gen = -1

    # -- 可用性 ------------------------------------------------------------ #

    def missing_glyphs(self) -> list[str]:
        """列出缺失的**必备**数字字形（0-9）。

        只报必备项：k/m/dot/slash 是可选的，只在读 "1.2k" 这类文本时才需要，
        把它们算成"不可用"会让只想读整数的用户莫名其妙。
        """
        missing: list[str] = []
        for d in REQUIRED_DIGITS:
            try:
                if self._name_of(d) not in self.matcher.lib:
                    missing.append(d)
            except Exception:
                missing.append(d)
        return missing

    def is_available(self) -> bool:
        """0-9 十个字形齐备才为 True。"""
        return not self.missing_glyphs()

    # -- 相似度 ------------------------------------------------------------ #

    def _scales(self) -> tuple[float, ...]:
        """匹配时尝试的字形缩放倍率。

        允许字形比模板略大/略小（±scale_tolerance）：分割出来的字形高度会受
        抗锯齿、二值化阈值影响差一两个像素，卡死在"必须一模一样大"上会白白
        丢分。缩小的一侧用补边实现（matchTemplate 要求搜索图不小于模板）。
        """
        tol = self.scale_tolerance
        if tol <= 0:
            return (1.0,)
        return (1.0, 1.0 + tol, 1.0 - tol)

    @staticmethod
    def _is_flat(img: np.ndarray) -> bool:
        """判断图像是否"几乎是一整块均匀像素"。

        相关系数在这种图上没有意义（分母趋 0，OpenCV 可能返回 1.0 或 nan）。
        一块纯色模板若对**任何**字形都给满分，就会把数字读成那个符号 ——
        正是"宁可读不出来也不能读错"要避免的，所以先判退化再走兜底。
        """
        return img.size == 0 or float(img.std()) < 4.0

    @staticmethod
    def _flat_score(glyph: np.ndarray, tpl: np.ndarray) -> float:
        """退化情形的兜底相似度：用**填充率 + 宽高比**代替相关系数。

        只在**模板**是一整块均匀像素时使用（典型是用户把很小的"小数点"框得极紧、
        图里几乎没有背景，甚至没有抗锯齿）。此时相关系数无从谈起，能判断的只有
        "是不是差不多大、差不多方"：实心块 vs 实心块 -> 高；实心块 vs 数字 -> 明显低。
        （字形侧是实心块时**不给分**，见 `_score`。）
        """
        def fill(img: np.ndarray) -> float:
            lo, hi = float(img.min()), float(img.max())
            return float((img > (lo + hi) / 2.0).mean())

        ga, ta = fill(glyph), fill(tpl)
        fill_sim = 1.0 - abs(ga - ta)
        gar = glyph.shape[1] / max(glyph.shape[0], 1)
        tar = tpl.shape[1] / max(tpl.shape[0], 1)
        asp_sim = min(gar, tar) / max(gar, tar, 1e-6)
        return float(min(fill_sim, asp_sim))

    @staticmethod
    def _pad_to(img: np.ndarray, w: int, h: int) -> np.ndarray:
        """把 img 放到至少 w x h 的零画布上居中。

        matchTemplate 要求搜索图不小于模板，所以字形比模板小时必须补边；
        补**零**（背景色）而不是补 ROI 的真实背景 —— 真实背景会带进噪声，
        实测反而把分数拉低。
        """
        cw, ch = max(w, img.shape[1]), max(h, img.shape[0])
        if cw == img.shape[1] and ch == img.shape[0]:
            return img
        canvas = np.zeros((ch, cw), np.uint8)
        ox, oy = (cw - img.shape[1]) // 2, (ch - img.shape[0]) // 2
        canvas[oy:oy + img.shape[0], ox:ox + img.shape[1]] = img
        return canvas

    @staticmethod
    def _corr(g: np.ndarray, t: np.ndarray) -> float:
        """两张同尺寸/可滑窗图的最大相关度（取绝对值 -> 极性无关）。

        取绝对值的原因：相关系数在模板整体反色时正好取负，而模板是从真实客户端
        裁的、可能是亮字也可能是暗字。形状不同时相关系数本身就在 0 附近，所以取
        绝对值不会把别的数字抬上来（实测对角 0.84~1.00、最大误配 0.76）。
        """
        if g.std() < 1 or t.std() < 1:
            return 0.0
        try:
            res = cv2.matchTemplate(g, t, cv2.TM_CCOEFF_NORMED)
        except Exception:
            return 0.0
        res = np.nan_to_num(res, nan=0.0, posinf=0.0, neginf=0.0)
        return min(1.0, float(np.abs(res).max()))

    def _score(self, glyph: np.ndarray, tpl: np.ndarray, *, refine: bool = False) -> float:
        """字形与模板的相似度（0~1）。`refine=True` 时走更贵的精细搜索。"""
        th, tw = tpl.shape[:2]
        if th < 2 or tw < 2:
            return 0.0
        if self._is_flat(tpl):
            return self._flat_score(glyph, tpl)
        if self._is_flat(glyph):
            # 字形是一整块均匀像素（噪声团、或裁剪裁到只剩墨迹）：相关系数无从谈起。
            # 这里**不给分**而不是走兜底 —— 兜底只看"填充率 + 宽高比"，一个 3x3 的
            # 噪点团正好和"小数点"模板很像，给分就会把噪点读成 "."，把 3480 变成 34.0。
            return 0.0
        if refine:
            return self._refine_score(glyph, tpl)

        best = 0.0
        for f in self._scales():
            gw = max(2, int(round(tw * f)))
            gh = max(2, int(round(th * f)))
            interp = (
                cv2.INTER_AREA
                if (gw < glyph.shape[1] or gh < glyph.shape[0])
                else cv2.INTER_LINEAR
            )
            try:
                g = cv2.resize(glyph, (gw, gh), interpolation=interp)
            except Exception:
                continue
            best = max(best, self._corr(self._pad_to(g, tw, th), tpl))
        return best

    def _refine_score(self, glyph: np.ndarray, tpl: np.ndarray) -> float:
        """精细搜索：**尺寸网格 + 模糊对齐**，只在便宜的一遍没过阈值时才用。

        它比便宜的一遍贵约 50 倍，但正是它把 1280x720 下缩放的 "8" 从 0.74 救到
        0.83（阈值 0.80）。两件事各治一个病：

        1. **尺寸网格**（宽高各 ±3px）：低分辨率下 Otsu 的边界差 1px，在 27px 高的
           字形上就是 4% 的形变，硬按模板尺寸缩放会整体走形；
        2. **模糊对齐**：字形比模板小时，把它放大等于凭空插值（边缘是软的），而模板
           是硬的 —— 把模板先降到字形尺寸再放回来，两者的模糊程度就一致了。
        """
        th, tw = tpl.shape[:2]
        ref = tpl
        if glyph.shape[0] < th or glyph.shape[1] < tw:
            try:
                small = cv2.resize(
                    tpl, (max(2, glyph.shape[1]), max(2, glyph.shape[0])),
                    interpolation=cv2.INTER_AREA,
                )
                ref = cv2.resize(small, (tw, th), interpolation=cv2.INTER_LINEAR)
            except Exception:
                ref = tpl

        best = 0.0
        for dw in (-3, -2, -1, 0, 1, 2, 3):
            for dh in (-3, -2, -1, 0, 1, 2, 3):
                gw, gh = max(2, tw + dw), max(2, th + dh)
                interp = (
                    cv2.INTER_AREA
                    if (gw < glyph.shape[1] or gh < glyph.shape[0])
                    else cv2.INTER_LINEAR
                )
                try:
                    g = cv2.resize(glyph, (gw, gh), interpolation=interp)
                except Exception:
                    continue
                best = max(best, self._corr(self._pad_to(g, tw, th), ref))
        return best

    def _best_of(self, glyph: np.ndarray, *, refine: bool) -> tuple[str, float]:
        """在全部候选字形里取最高分。"""
        best_char, best_score = _UNKNOWN, 0.0
        for ch, tpl in self._templates().items():
            s = self._score(glyph, tpl, refine=refine)
            if s > best_score:
                best_char, best_score = ch, s
        return best_char, best_score

    def _threshold_for(self, glyph_h: int, line_h: int) -> float:
        """按字形大小微调阈值：小字形可用像素少，相关系数天然偏低。

        实测（模板按 1920x1080 采、画面缩到 1280x720）：数字与自己的模板相关度
        0.80~0.98，而小数点只有 6x6 像素、相关度 0.75 —— 但它的最大误配只有 0.44，
        也就是"分得很开，只是绝对值上不去"。这种情况下卡死 0.80 会把唯一能读出来的
        小数点判成"读不出来"（"3.5m" 直接读不到），所以对**高度不到本行最高字形
        一半**的小字形放宽 0.10。放宽是有底线的：小字形本来就只有点/斜杠/小写字母
        这几种可能，它们之间分得很开（实测误配 <= 0.5）。
        """
        if line_h > 0 and glyph_h < line_h * 0.5:
            return max(0.0, self.threshold - 0.10)
        return self.threshold

    def _match_glyph(self, glyph: np.ndarray, line_h: int = 0) -> tuple[str, float]:
        """单个字形 -> (字符, 分数)。全部候选都低于阈值时字符为 "?"。

        两级匹配：先用便宜的一遍比全部候选；**只有没过阈值**的字形才做一次精细
        搜索再决定。绝大多数情况（原生分辨率下）第一级就过了，平均开销不受影响；
        真正要付代价的只有"本来就要判 ?"的字形，而那正是我们最希望救回来的。
        """
        thr = self._threshold_for(glyph.shape[0], line_h)
        best_char, best_score = self._best_of(glyph, refine=False)
        if best_score < thr:
            best_char, best_score = self._best_of(glyph, refine=True)
        if best_score < thr:
            return _UNKNOWN, best_score
        return best_char, best_score

    # -- 读取 -------------------------------------------------------------- #

    def _read_glyphs(
        self,
        frame: np.ndarray,
        frame_rect: Rect,
        region: tuple[float, float, float, float],
    ) -> Optional[list[tuple[str, float]]]:
        """整条链路：裁 ROI -> 二值化定极性 -> 去噪 -> 投影分割 -> 逐字形匹配。

        分割用二值图、比较用灰度图：各取所需（二值图分割干净，灰度图保留笔画
        粗细）。两者共用同一套包围盒，所以灰度裁剪的坐标系不会跑偏。

        任何一步不满足条件都返回 None（不抛异常）。缺字形模板时同样走这里：
        `_templates()` 为空 -> 没有候选 -> 每个字形都是 "?" -> 上层返回 None，
        调用方通过 `missing_glyphs()` 知道缺什么。
        """
        try:
            if not self._templates():
                return None

            roi = _crop_roi(frame, frame_rect, region)
            if roi is None:
                return None

            gray = _to_gray(roi)
            # 灰度化后仍不是单通道（例如喂进来的图带 alpha）就放弃，
            # 后面的 Otsu / matchTemplate 才不会抛异常
            if gray.ndim != 2:
                return None

            gray, binary = _polarity_split(gray)
            boxes = _segment(_denoise(binary))
            if not boxes:
                return None

            # 本行最高字形的高度：用来判断"这个字形是不是特别小"（小数点、
            # 后缀字母的 x-height），见 `_threshold_for`
            line_h = max(y1 - y0 for _x0, y0, _x1, y1 in boxes)

            out: list[tuple[str, float]] = []
            for x0, y0, x1, y1 in boxes:
                glyph = gray[y0:y1, x0:x1]
                if glyph.size == 0 or min(glyph.shape[:2]) < 2:
                    return None
                out.append(self._match_glyph(glyph, line_h))
            return out or None
        except Exception:
            # 识别链路永远不能让脚本崩：读不出来就交给调用方处理（返回 None）
            return None

    def read(
        self,
        frame: np.ndarray,
        frame_rect: Rect,
        region: tuple[float, float, float, float],
    ) -> Optional[NumberReading]:
        """读一个整数。任何不可信的情况都返回 None。

        **只要有一个字形判为 "?"，整体就返回 None** —— 读不出来只是这一轮跳过，
        读错数值却可能让预算判断失守（比如把 3480 读成 348，脚本以为体力够）。
        所以这里刻意不返回"部分结果"。
        """
        try:
            chars = self._read_glyphs(frame, frame_rect, region)
            if not chars:
                return None
            raw = "".join(c for c, _ in chars)
            if _UNKNOWN in raw:
                return None
            value = _parse_number(raw)
            if value is None:
                return None
            return NumberReading(
                value=value,
                raw=raw,
                region=(float(region[0]), float(region[1]), float(region[2]), float(region[3])),
                # 取最小值：最弱的一环决定整体可信度，不能让 9 个 0.99 掩盖 1 个 0.81
                confidence=min(s for _, s in chars),
                digits=list(chars),
            )
        except Exception:
            return None

    def read_text(
        self,
        frame: np.ndarray,
        frame_rect: Rect,
        region: tuple[float, float, float, float],
    ) -> Optional[str]:
        """读原始拼接文本（"1.2k" / "3480" / "12/20"）。

        与 `read()` 的区别：这里**保留 "?" 占位**而不是返回 None —— 调试"到底哪个
        字形没认出来"时，"34?0" 比一个 None 有用得多；要判断可信度请用 `read()`。
        """
        try:
            chars = self._read_glyphs(frame, frame_rect, region)
            if not chars:
                return None
            return "".join(c for c, _ in chars)
        except Exception:
            return None

    def read_pair(
        self,
        frame: np.ndarray,
        frame_rect: Rect,
        region: tuple[float, float, float, float],
    ) -> Optional[tuple[int, int]]:
        """读 "12/20" 这种 a/b 形式的数值对（剩余次数、体力上下限等）。"""
        try:
            text = self.read_text(frame, frame_rect, region)
            if not text or _UNKNOWN in text:
                return None
            parts = text.split("/")
            if len(parts) != 2:
                return None
            left, right = _parse_number(parts[0]), _parse_number(parts[1])
            if left is None or right is None:
                return None
            return (left, right)
        except Exception:
            return None


# --------------------------------------------------------------------------- #
# 可选通用文本 OCR
# --------------------------------------------------------------------------- #

_RAPIDOCR_MODULE: Any = None
_RAPIDOCR_PROBED = False


def _probe_rapidocr() -> Any:
    """探测 rapidocr_onnxruntime 是否可用（只探测一次，避免反复 import 开销）。

    刻意吞掉**所有**异常：缺库、库装了但 onnxruntime 缺 DLL、版本不兼容……
    对使用者来说都是同一件事 —— "通用 OCR 用不了，走字形模板那条路"。
    """
    global _RAPIDOCR_MODULE, _RAPIDOCR_PROBED
    if not _RAPIDOCR_PROBED:
        try:
            import rapidocr_onnxruntime as mod

            _RAPIDOCR_MODULE = mod
        except Exception:
            _RAPIDOCR_MODULE = None
        _RAPIDOCR_PROBED = True
    return _RAPIDOCR_MODULE


class TextReader:
    """可选通用 OCR（rapidocr_onnxruntime），用来读中文 / 英文文本。

    为什么做成可选：它拖着 onnxruntime（几十 MB），而本项目刻意保持零重依赖；
    更重要的是**库不存在时脚本必须照常跑**。所以本类所有读取方法在缺库时返回
    None，`is_available()` 返回 False，绝不抛异常。真需要"必须有 OCR"的调用点
    （例如启动自检要给出安装提示）可以显式调 `require()`，它才抛
    `OcrUnavailable`。
    """

    def __init__(self, min_score: float = 0.5) -> None:
        self.min_score = float(min_score)
        self._engine: Any = None

    # -- 可用性 ------------------------------------------------------------ #

    def is_available(self) -> bool:
        """只检查**库是否可导入**，不加载模型（加载模型要几百毫秒，不该在探测里做）。"""
        return _probe_rapidocr() is not None

    def require(self) -> Any:
        """拿到 OCR 引擎；缺库 / 加载失败抛 `OcrUnavailable`（带安装提示）。"""
        if self._engine is not None:
            return self._engine
        mod = _probe_rapidocr()
        if mod is None:
            raise OcrUnavailable(
                "未安装 rapidocr_onnxruntime，通用文本 OCR 不可用。"
                "需要时安装：.venv\\Scripts\\python.exe -m pip install rapidocr_onnxruntime"
                "（不装也不影响数字识别：采好 ocr/digits 字形即可）"
            )
        try:
            self._engine = mod.RapidOCR()
        except Exception as exc:
            raise OcrUnavailable(f"rapidocr_onnxruntime 加载失败: {exc}") from exc
        return self._engine

    # -- 读取 -------------------------------------------------------------- #

    def read_text(
        self,
        frame: np.ndarray,
        frame_rect: Rect,
        region: tuple[float, float, float, float],
    ) -> Optional[str]:
        """读区域内的文本；不可用时返回 None（不抛异常）。

        同一行的多个识别框直接拼起来（region 应当框住**一个逻辑字段**），
        不同行用换行分隔 —— 这样 `read_number()` 的取数不会把两行粘成一个数。
        """
        try:
            roi = _crop_roi(frame, frame_rect, region)
            if roi is None:
                return None
            if roi.ndim == 2:
                roi = cv2.cvtColor(roi, cv2.COLOR_GRAY2BGR)

            engine = self.require()
            out = engine(roi)
            # rapidocr 正常返回 (结果, 耗时)，但不同版本/异常路径可能只给结果
            result = out[0] if isinstance(out, tuple) else out
            if not result:
                return None

            entries: list[tuple[float, float, str]] = []
            heights: list[float] = []
            for item in result:
                if item is None or len(item) < 3:
                    continue
                box, text, score = item[0], item[1], item[2]
                try:
                    if float(score) < self.min_score:
                        continue
                except (TypeError, ValueError):
                    continue
                text = str(text).strip()
                if not text:
                    continue
                arr = np.asarray(box, dtype=float).reshape(-1, 2)
                if arr.shape[0] == 0:
                    continue
                entries.append((float(arr[:, 1].mean()), float(arr[:, 0].min()), text))
                heights.append(float(arr[:, 1].max() - arr[:, 1].min()))
            if not entries:
                return None

            entries.sort(key=lambda e: (e[0], e[1]))
            line_tol = max(8.0, float(np.median(heights)) * 0.6) if heights else 8.0
            lines: list[list[str]] = []
            line_y: Optional[float] = None
            for y, _x, text in entries:
                if line_y is None or abs(y - line_y) > line_tol:
                    lines.append([text])
                    line_y = y
                else:
                    lines[-1].append(text)
            return "\n".join("".join(parts) for parts in lines)
        except Exception:
            return None

    def read_number(
        self,
        frame: np.ndarray,
        frame_rect: Rect,
        region: tuple[float, float, float, float],
    ) -> Optional[int]:
        """从识别文本里抠出第一个整数（"3,480 G" -> 3480）。"""
        try:
            text = self.read_text(frame, frame_rect, region)
            if not text:
                return None
            m = re.search(r"-?\d[\d,]*", text)
            if m is None:
                return None
            return int(m.group(0).replace(",", ""))
        except Exception:
            return None


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #


def make_digit_reader(matcher: Matcher, cfg: Optional[dict[str, Any]]) -> DigitReader:
    """按 `[ocr]` 配置段造一个 `DigitReader`（cfg 为 None 时全用默认值）。"""
    c = OcrConfig.from_dict(cfg)
    return DigitReader(
        matcher,
        prefix=c.digits_prefix,
        threshold=c.threshold,
        scale_tolerance=c.scale_tolerance,
    )


def make_text_reader(cfg: Optional[dict[str, Any]]) -> TextReader:
    """按 `[ocr]` 配置段造一个 `TextReader`（缺库时它自己会降级）。

    刻意**不**在这里检查 `backend` / `is_available()`：工厂只负责造对象，
    "用不用"由调用方按 `backend`/`enabled` 决定。如果在工厂里因为缺库就抛异常，
    "配置写了 rapidocr 但没装库"会变成启动即崩，正好违背优雅降级的原则。
    """
    raw: dict[str, Any] = cfg if isinstance(cfg, dict) else {}
    try:
        min_score = float(raw.get("min_score", 0.5))
    except (TypeError, ValueError):
        min_score = 0.5
    return TextReader(min_score=min_score)
