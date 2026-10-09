"""场景识别：把"当前画面是什么界面"变成状态机的输入。

安卓端脚本普遍是"盲点"式流程 —— 按固定顺序点固定坐标，一旦弹窗/卡顿就错位。
PC 端因为要支持多种分辨率、还要应对 Steam 覆盖层和各种活动弹窗，
必须改成**先认场景、再动作**。

识别规则来自配置：

    [scenes.battle_result]
    any  = ["battle/btn_retry", "battle/btn_confirm_result"]   # 命中任一即认为该场景
    none = ["battle/btn_auto"]                                 # 命中任一则否决该场景

判定顺序（priority）从"最具体"到"最通用"：
loading > popup > battle_result > battle_ready > battle > lobby > unknown
例如结算界面底部可能还残留战斗 HUD，所以 battle_result 必须先于 battle 判定。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

from .vision import Match, MatchOptions, Matcher
from .winutil import Rect

UNKNOWN = "unknown"

DEFAULT_PRIORITY = [
    "loading",
    "popup",
    "battle_result",
    "battle_ready",
    "battle",
    "secret_shop",
    "sanctuary",
    "lobby",
]


@dataclass
class SceneResult:
    name: str
    matched: Optional[Match] = None
    scores: dict[str, float] = field(default_factory=dict)
    evaluated: list[str] = field(default_factory=list)
    elapsed: float = 0.0
    ts: float = field(default_factory=time.time)

    @property
    def is_unknown(self) -> bool:
        return self.name == UNKNOWN

    def describe(self) -> str:
        if self.matched:
            return f"{self.name} (锚点 {self.matched.name} {self.matched.score:.3f})"
        return f"{self.name}"


class SceneDetector:
    def __init__(
        self,
        matcher: Matcher,
        scenes: dict[str, Any],
        priority: Optional[list[str]] = None,
        threshold: float = 0.86,
        scale_tolerance: float = 0.0,
        cache_seconds: float = 0.25,
        verbose: bool = False,
    ):
        self.matcher = matcher
        self.scenes = {k: v for k, v in scenes.items() if isinstance(v, dict)}
        self.threshold = threshold
        self.scale_tolerance = scale_tolerance
        self.cache_seconds = cache_seconds
        self.verbose = verbose

        order = priority or DEFAULT_PRIORITY
        known = set(self.scenes)
        self.priority = [s for s in order if s in known]
        self.priority += [s for s in sorted(known) if s not in self.priority]

        self._cache: Optional[SceneResult] = None

    # -- 检测 -------------------------------------------------------------- #

    def detect(self, frame: np.ndarray, rect: Rect, use_cache: bool = True) -> SceneResult:
        if use_cache and self._cache is not None and (time.time() - self._cache.ts) < self.cache_seconds:
            return self._cache

        t0 = time.perf_counter()
        scores: dict[str, float] = {}
        evaluated: list[str] = []

        for scene in self.priority:
            spec = self.scenes[scene]
            any_tpls: list[str] = list(spec.get("any", []) or [])
            none_tpls: list[str] = list(spec.get("none", []) or [])

            # 1) 否决条件：命中任一 none 模板 -> 直接排除该场景
            vetoed = False
            for name in none_tpls:
                evaluated.append(f"-{name}")
                m = self._try(name, frame, rect)
                if m is not None:
                    scores[f"{scene}:veto:{name}"] = m.score
                    vetoed = True
                    break
            if vetoed:
                continue

            # 2) 命中条件：any 里任一命中即认定
            for name in any_tpls:
                evaluated.append(name)
                m = self._try(name, frame, rect)
                if m is not None:
                    scores[name] = m.score
                    res = SceneResult(
                        name=scene, matched=m, scores=scores,
                        evaluated=evaluated, elapsed=time.perf_counter() - t0,
                    )
                    self._cache = res
                    return res

        res = SceneResult(
            name=UNKNOWN, matched=None, scores=scores,
            evaluated=evaluated, elapsed=time.perf_counter() - t0,
        )
        self._cache = res
        return res

    def _try(self, name: str, frame: np.ndarray, rect: Rect) -> Optional[Match]:
        try:
            return self.matcher.find(
                name, frame, rect,
                opts=MatchOptions(threshold=self.threshold, scale_tolerance=self.scale_tolerance),
            )
        except KeyError:
            # 模板缺失不应让整个检测崩掉 —— 缺模板等于该锚点不可用
            if self.verbose:
                print(f"[scene] 缺少模板 {name}，跳过该锚点")
            return None

    def invalidate(self) -> None:
        self._cache = None

    # -- 调试 -------------------------------------------------------------- #

    def probe(self, frame: np.ndarray, rect: Rect) -> dict[str, float]:
        """不做短路，把所有场景所有锚点的分数都算出来（调参用）。"""
        out: dict[str, float] = {}
        for scene, spec in self.scenes.items():
            for name in list(spec.get("any", []) or []) + list(spec.get("none", []) or []):
                try:
                    m = self.matcher.find(
                        name, frame, rect,
                        opts=MatchOptions(threshold=0.0, scale_tolerance=self.scale_tolerance),
                    )
                    out[f"{scene}::{name}"] = m.score if m else 0.0
                except KeyError:
                    out[f"{scene}::{name}"] = -1.0  # 模板缺失
        return out

    def missing_templates(self) -> list[str]:
        need: list[str] = []
        for spec in self.scenes.values():
            need += list(spec.get("any", []) or [])
            need += list(spec.get("none", []) or [])
        return self.matcher.lib.missing(sorted(set(need)))
