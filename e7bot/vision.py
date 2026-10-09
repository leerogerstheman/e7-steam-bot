"""视觉识别：分辨率无关的模板匹配。

安卓端脚本（ADB + 1280x720）可以直接硬编码像素坐标，PC 端不行 ——
Steam 客户端支持多种分辨率和窗口尺寸。所以本模块的核心是：

1. **模板自带参考分辨率**：每张模板 PNG 旁边放一个同名 .json，
   记录它是在多大分辨率下截的。匹配时按 `当前客户区宽 / 参考宽` 自动缩放模板。
2. **对外只暴露归一化坐标(0..1)**：脚本内部所有位置、区域、阈值都用比例表示，
   换分辨率零改动。
3. **单尺度优先、多尺度兜底**：先用精确缩放匹配（快），失败再在 ±tol 范围内
   扫几个尺度（慢但鲁棒），兼顾性能与稳定性。

匹配方法：
* 不透明模板 -> `TM_CCOEFF_NORMED`（对整体亮度变化不敏感，最适合游戏 UI）；
* 带 alpha 的模板 -> `TM_CCORR_NORMED` + mask（只比较不透明像素）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

import cv2
import numpy as np

from .winutil import Rect


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class Match:
    """一次成功的匹配。"""

    name: str
    score: float
    rect: Rect          # 屏幕物理像素矩形
    center: tuple[int, int]
    norm_center: tuple[float, float]
    scale: float
    frame_rect: Rect    # 匹配时使用的客户区矩形（用于反算归一化坐标）

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Match {self.name} score={self.score:.3f} "
            f"center={self.center} norm=({self.norm_center[0]:.3f},{self.norm_center[1]:.3f})>"
        )


@dataclass
class TemplateMeta:
    ref_size: tuple[int, int] = (1920, 1080)
    region: Optional[tuple[float, float, float, float]] = None  # 归一化搜索区
    threshold: float = 0.86
    grayscale: bool = False
    note: str = ""

    @staticmethod
    def load(path: Path) -> "TemplateMeta":
        js = path.with_suffix(".json")
        if not js.exists():
            return TemplateMeta()
        try:
            raw = json.loads(js.read_text(encoding="utf-8"))
        except Exception:
            return TemplateMeta()
        return TemplateMeta(
            ref_size=tuple(raw.get("ref_size", (1920, 1080))),  # type: ignore[arg-type]
            region=tuple(raw["region"]) if raw.get("region") else None,  # type: ignore[arg-type]
            threshold=float(raw.get("threshold", 0.86)),
            grayscale=bool(raw.get("grayscale", False)),
            note=str(raw.get("note", "")),
        )

    def save(self, path: Path) -> None:
        path.with_suffix(".json").write_text(
            json.dumps(
                {
                    "ref_size": list(self.ref_size),
                    "region": list(self.region) if self.region else None,
                    "threshold": self.threshold,
                    "grayscale": self.grayscale,
                    "note": self.note,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


@dataclass
class Template:
    name: str
    image: np.ndarray            # BGR
    mask: Optional[np.ndarray]   # uint8 0/255，或 None
    meta: TemplateMeta
    path: Optional[Path] = None

    @property
    def size(self) -> tuple[int, int]:
        return (self.image.shape[1], self.image.shape[0])


# --------------------------------------------------------------------------- #
# 模板库
# --------------------------------------------------------------------------- #


class TemplateLibrary:
    """从目录加载模板，支持多个 profile（例如 default / 16x9 / 自定义皮肤）。"""

    def __init__(self, root: str | os.PathLike, profile: str = "default"):
        self.root = Path(root)
        self.profile = profile
        self.dir = self.root / profile
        self._cache: dict[str, Template] = {}
        self.reload()

    def reload(self) -> None:
        self._cache.clear()
        if not self.dir.exists():
            return
        for png in sorted(self.dir.rglob("*.png")):
            name = png.relative_to(self.dir).with_suffix("").as_posix()
            tpl = self._load_one(png, name)
            if tpl is not None:
                self._cache[name] = tpl

    @staticmethod
    def _load_one(png: Path, name: str) -> Optional[Template]:
        # 用 imdecode 读，避免中文路径在 cv2.imread 下失败
        try:
            buf = np.fromfile(str(png), dtype=np.uint8)
            img = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
        except Exception:
            return None
        if img is None or img.size == 0:
            return None

        mask = None
        if img.ndim == 3 and img.shape[2] == 4:
            alpha = img[:, :, 3]
            img = img[:, :, :3]
            if int(alpha.min()) < 250:
                mask = (alpha > 8).astype(np.uint8) * 255
        elif img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

        return Template(name=name, image=np.ascontiguousarray(img), mask=mask,
                        meta=TemplateMeta.load(png), path=png)

    # -- 访问 -------------------------------------------------------------- #

    def __contains__(self, name: str) -> bool:
        return name in self._cache

    def __len__(self) -> int:
        return len(self._cache)

    def get(self, name: str) -> Template:
        if name not in self._cache:
            near = [n for n in self._cache if name.lower() in n.lower()]
            hint = f"，相似的有: {near[:5]}" if near else ""
            raise KeyError(f"模板不存在: {name!r}（profile={self.profile}）{hint}")
        return self._cache[name]

    def names(self) -> list[str]:
        return sorted(self._cache)

    def missing(self, required: Iterable[str]) -> list[str]:
        return [n for n in required if n not in self._cache]


# --------------------------------------------------------------------------- #
# 匹配器
# --------------------------------------------------------------------------- #


@dataclass
class MatchOptions:
    threshold: Optional[float] = None
    scale_tolerance: float = 0.0   # 0 = 只按精确缩放匹配
    scale_steps: int = 5
    grayscale: Optional[bool] = None
    max_results: int = 1


class Matcher:
    def __init__(self, library: TemplateLibrary, default_threshold: float = 0.86):
        self.lib = library
        self.default_threshold = default_threshold
        self._scaled: dict[tuple[str, int, int], np.ndarray] = {}
        self._scaled_mask: dict[tuple[str, int, int], Optional[np.ndarray]] = {}

    # -- 缓存 -------------------------------------------------------------- #

    def _resize(self, tpl: Template, size: tuple[int, int]) -> tuple[np.ndarray, Optional[np.ndarray]]:
        key = (tpl.name, size[0], size[1])
        if key not in self._scaled:
            interp = cv2.INTER_AREA if size[0] < tpl.size[0] else cv2.INTER_LINEAR
            self._scaled[key] = cv2.resize(tpl.image, size, interpolation=interp)
            self._scaled_mask[key] = (
                cv2.resize(tpl.mask, size, interpolation=cv2.INTER_NEAREST)
                if tpl.mask is not None
                else None
            )
        return self._scaled[key], self._scaled_mask[key]

    def clear_cache(self) -> None:
        self._scaled.clear()
        self._scaled_mask.clear()

    # -- 单次匹配 ---------------------------------------------------------- #

    def _match_one(
        self,
        frame: np.ndarray,
        frame_rect: Rect,
        tpl: Template,
        search: Rect,
        size: tuple[int, int],
        scale: float,
        opts: MatchOptions,
    ) -> Optional[Match]:
        tw, th = size
        if tw < 4 or th < 4:
            return None

        off_x = search.left - frame_rect.left
        off_y = search.top - frame_rect.top
        roi = frame[off_y: off_y + search.height, off_x: off_x + search.width]
        if roi.shape[0] < th or roi.shape[1] < tw:
            return None

        tpl_img, tpl_mask = self._resize(tpl, (tw, th))
        gray = opts.grayscale if opts.grayscale is not None else tpl.meta.grayscale

        if gray:
            hay = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            needle = cv2.cvtColor(tpl_img, cv2.COLOR_BGR2GRAY)
        else:
            hay, needle = roi, tpl_img

        if tpl_mask is not None:
            res = cv2.matchTemplate(hay, needle, cv2.TM_CCORR_NORMED, mask=tpl_mask)
        else:
            res = cv2.matchTemplate(hay, needle, cv2.TM_CCOEFF_NORMED)

        res = np.nan_to_num(res, nan=0.0, posinf=0.0, neginf=0.0)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val < (opts.threshold if opts.threshold is not None else tpl.meta.threshold):
            return None

        x = search.left + max_loc[0]
        y = search.top + max_loc[1]
        rect = Rect(x, y, tw, th)
        cx, cy = rect.center
        return Match(
            name=tpl.name,
            score=float(max_val),
            rect=rect,
            center=(cx, cy),
            norm_center=frame_rect.to_local(cx, cy),
            scale=scale,
            frame_rect=frame_rect,
        )

    def _base_size(self, tpl: Template, frame_rect: Rect) -> tuple[tuple[int, int], float]:
        scale = frame_rect.width / max(tpl.meta.ref_size[0], 1)
        return (
            (max(4, int(round(tpl.size[0] * scale))), max(4, int(round(tpl.size[1] * scale)))),
            scale,
        )

    # -- 公开接口 ---------------------------------------------------------- #

    def find(
        self,
        name: str,
        frame: np.ndarray,
        frame_rect: Rect,
        region: Optional[tuple[float, float, float, float]] = None,
        opts: Optional[MatchOptions] = None,
    ) -> Optional[Match]:
        """在帧里找模板，返回最佳匹配或 None。

        region 为归一化 (x, y, w, h)，限定搜索范围能大幅提速并降低误匹配。

        匹配按三级递进，兼顾速度与鲁棒性：
          1. 按 `当前宽/参考宽` 精确缩放 —— 绝大多数情况一次命中；
          2. ±1 像素尺寸补偿 —— 缩放比不是整数倍时 `round()` 会让模板大小差 1px，
             小模板（<300px）对此非常敏感，这一步专门治它；
          3. 多尺度扫描 —— 应对游戏内 UI 缩放、窗口被非等比拉伸等情况。
        """
        opts = opts or MatchOptions()
        tpl = self.lib.get(name)
        search = self._search_rect(frame_rect, region if region is not None else tpl.meta.region)
        if search.width < 4 or search.height < 4:
            return None

        (tw, th), base_scale = self._base_size(tpl, frame_rect)

        best = self._match_one(frame, frame_rect, tpl, search, (tw, th), base_scale, opts)
        if best is not None:
            return best

        # 2) ±1px 尺寸补偿
        if min(tpl.size) < 300:
            for dw, dh in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                cand = self._match_one(
                    frame, frame_rect, tpl, search, (tw + dw, th + dh), base_scale, opts
                )
                if cand is not None and (best is None or cand.score > best.score):
                    best = cand
            if best is not None:
                return best

        # 3) 多尺度兜底
        if opts.scale_tolerance <= 0:
            return best
        tol = opts.scale_tolerance
        steps = max(2, opts.scale_steps)
        for i in range(steps):
            f = 1.0 - tol + (2 * tol) * i / (steps - 1)
            if abs(f - 1.0) < 1e-6:
                continue
            cand = self._match_one(
                frame, frame_rect, tpl, search,
                (max(4, int(round(tw * f))), max(4, int(round(th * f)))),
                base_scale * f, opts,
            )
            if cand is not None and (best is None or cand.score > best.score):
                best = cand
        return best

    def find_all(
        self,
        name: str,
        frame: np.ndarray,
        frame_rect: Rect,
        region: Optional[tuple[float, float, float, float]] = None,
        opts: Optional[MatchOptions] = None,
        nms_iou: float = 0.35,
    ) -> list[Match]:
        """找出所有匹配（用于"列表里挑一个"这类场景，例如装备列表、商店列表）。"""
        opts = opts or MatchOptions()
        tpl = self.lib.get(name)
        search = self._search_rect(frame_rect, region if region is not None else tpl.meta.region)
        scale = frame_rect.width / max(tpl.meta.ref_size[0], 1)
        tw = max(4, int(round(tpl.size[0] * scale)))
        th = max(4, int(round(tpl.size[1] * scale)))
        off_x, off_y = search.left - frame_rect.left, search.top - frame_rect.top
        roi = frame[off_y: off_y + search.height, off_x: off_x + search.width]
        if roi.shape[0] < th or roi.shape[1] < tw:
            return []

        tpl_img, tpl_mask = self._resize(tpl, (tw, th))
        gray = opts.grayscale if opts.grayscale is not None else tpl.meta.grayscale
        if gray:
            roi = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            tpl_img = cv2.cvtColor(tpl_img, cv2.COLOR_BGR2GRAY)

        method = cv2.TM_CCORR_NORMED if tpl_mask is not None else cv2.TM_CCOEFF_NORMED
        res = cv2.matchTemplate(roi, tpl_img, method, mask=tpl_mask) if tpl_mask is not None \
            else cv2.matchTemplate(roi, tpl_img, method)
        res = np.nan_to_num(res, nan=0.0)

        thr = opts.threshold if opts.threshold is not None else tpl.meta.threshold
        ys, xs = np.where(res >= thr)
        if len(xs) == 0:
            return []
        cand = sorted(
            (
                Match(
                    name=name,
                    score=float(res[y, x]),
                    rect=Rect(search.left + int(x), search.top + int(y), tw, th),
                    center=(search.left + int(x) + tw // 2, search.top + int(y) + th // 2),
                    norm_center=frame_rect.to_local(search.left + int(x) + tw // 2, search.top + int(y) + th // 2),
                    scale=scale,
                    frame_rect=frame_rect,
                )
                for x, y in zip(xs, ys)
            ),
            key=lambda m: m.score,
            reverse=True,
        )
        return _nms(cand, nms_iou)[: max(1, opts.max_results)]

    @staticmethod
    def _search_rect(frame_rect: Rect, region: Optional[Sequence[float]]) -> Rect:
        if region is None:
            return frame_rect
        x, y, w, h = region
        return frame_rect.sub(x, y, w, h)

    def wait_for(
        self,
        name: str,
        grab: "object",
        frame_rect_getter,
        timeout: float = 15.0,
        interval: float = 0.25,
        region: Optional[tuple[float, float, float, float]] = None,
        opts: Optional[MatchOptions] = None,
        stop_check=None,
    ) -> Optional[Match]:
        """轮询等待某个模板出现（最常用的原语：等加载完、等弹窗）。"""
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            if stop_check and stop_check():
                return None
            rect = frame_rect_getter()
            frame = grab(rect)  # type: ignore[operator]
            m = self.find(name, frame, rect, region, opts)
            if m is not None:
                return m
            time.sleep(interval)
        return None


def _nms(matches: list[Match], iou_thr: float) -> list[Match]:
    kept: list[Match] = []
    for m in matches:
        drop = False
        for k in kept:
            if _iou(m.rect, k.rect) > iou_thr:
                drop = True
                break
        if not drop:
            kept.append(m)
    return kept


def _iou(a: Rect, b: Rect) -> float:
    x1, y1 = max(a.left, b.left), max(a.top, b.top)
    x2, y2 = min(a.right, b.right), min(a.bottom, b.bottom)
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    if inter == 0:
        return 0.0
    union = a.width * a.height + b.width * b.height - inter
    return inter / union if union > 0 else 0.0


# --------------------------------------------------------------------------- #
# 颜色 / 亮度辅助判断
# --------------------------------------------------------------------------- #


def color_at(frame: np.ndarray, frame_rect: Rect, nx: float, ny: float) -> tuple[int, int, int]:
    """取归一化坐标处的 BGR 颜色（小邻域均值，抗噪）。"""
    x, y = frame_rect.to_screen(nx, ny)
    lx, ly = x - frame_rect.left, y - frame_rect.top
    h, w = frame.shape[:2]
    lx = max(0, min(lx, w - 1))
    ly = max(0, min(ly, h - 1))
    patch = frame[max(0, ly - 1): ly + 2, max(0, lx - 1): lx + 2]
    return tuple(int(v) for v in patch.reshape(-1, 3).mean(axis=0))  # type: ignore[return-value]


def region_mean_hsv(
    frame: np.ndarray, frame_rect: Rect, region: tuple[float, float, float, float]
) -> tuple[float, float, float]:
    r = frame_rect.sub(*region)
    roi = frame[r.top - frame_rect.top: r.bottom - frame_rect.top,
                r.left - frame_rect.left: r.right - frame_rect.left]
    if roi.size == 0:
        return (0.0, 0.0, 0.0)
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    return tuple(float(v) for v in hsv.reshape(-1, 3).mean(axis=0))  # type: ignore[return-value]


def is_greyed(frame: np.ndarray, frame_rect: Rect, region: tuple[float, float, float, float],
              sat_threshold: float = 40.0) -> bool:
    """判断某个按钮区域是不是"灰色不可点"状态。

    游戏里按钮的启用/禁用经常只是换了个灰度贴图，模板匹配会同时命中两者；
    用饱和度判断更可靠（彩色=可点，灰=禁用）。
    """
    _, s, _ = region_mean_hsv(frame, frame_rect, region)
    return s < sat_threshold


def frame_diff_ratio(a: np.ndarray, b: np.ndarray, threshold: int = 18) -> float:
    """两帧差异比例，用于判断"画面是否变化 / 是否还在加载动画"。"""
    if a.shape != b.shape:
        return 1.0
    d = cv2.absdiff(a, b)
    if d.ndim == 3:
        d = d.max(axis=2)
    return float((d > threshold).mean())


# --------------------------------------------------------------------------- #
# 调试可视化
# --------------------------------------------------------------------------- #


def annotate(frame: np.ndarray, frame_rect: Rect, matches: Iterable[Match], out_path: str | Path) -> None:
    """把匹配结果画在帧上存盘，排查误匹配时非常有用。"""
    vis = frame.copy()
    for m in matches:
        x, y = m.rect.left - frame_rect.left, m.rect.top - frame_rect.top
        cv2.rectangle(vis, (x, y), (x + m.rect.width, y + m.rect.height), (0, 255, 0), 2)
        cv2.putText(vis, f"{m.name} {m.score:.2f}", (x, max(12, y - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1, cv2.LINE_AA)
        cv2.circle(vis, (m.center[0] - frame_rect.left, m.center[1] - frame_rect.top), 3, (0, 0, 255), -1)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".png", vis)
    if ok:
        buf.tofile(str(out_path))
