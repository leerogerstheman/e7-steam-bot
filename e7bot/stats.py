"""运行统计与报表。

`Stats`（在 engine.py 里）只保留**本次运行**的计数器，进程一退就没了。
这个模块把每次有意义的动作写成 **JSONL 事件流**（一行一个 JSON），
于是可以：

* 事后回答"昨晚到底刷了多少场、商店刷了多少次、买到什么"
* 生成 Markdown / CSV 报表
* 长期观察成功率，发现某个环节在退化（比如某张模板的匹配率在下降）

为什么用 JSONL 而不是 CSV/SQLite：
* 追加写、崩溃不损坏（最后一行可能残缺，读的时候跳过即可）
* 字段可以随时加，不用迁移 schema
* 人可以直接 `tail` 看
"""

from __future__ import annotations

import csv
import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

#: 事件类型（字符串常量，避免各处写错）
RUN_START = "run_start"
RUN_END = "run_end"
SCENE = "scene"
BATTLE_FINISHED = "battle_finished"
PURCHASE = "purchase"
REFRESH = "refresh"
TASK_DONE = "task_done"
ERROR = "error"
STUCK = "stuck"
ALERT = "alert"
CLICK = "click"


class StatsRecorder:
    """追加写 JSONL 事件流。线程安全（用简单锁）。"""

    def __init__(self, path: Path, enabled: bool = True):
        self.path = Path(path)
        self.enabled = enabled
        self._fh = None
        self._count = 0
        if enabled:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = self.path.open("a", encoding="utf-8")
            except Exception:
                self.enabled = False
                self._fh = None

    def event(self, kind: str, **data: Any) -> None:
        if not self.enabled or self._fh is None:
            return
        row = {"ts": round(time.time(), 3), "iso": datetime.now().isoformat(timespec="seconds"),
               "type": kind}
        row.update(data)
        try:
            self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._count += 1
            # 每条都 flush：崩溃时最多丢最后一条，而不是丢掉整个缓冲区
            self._fh.flush()
        except Exception:
            self.enabled = False

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    @property
    def written(self) -> int:
        return self._count


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #


def read_events(path: Path) -> list[dict]:
    """读事件流。**容忍最后一行残缺**（进程被强杀时会出现）。"""
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # 残缺行，跳过
        if isinstance(row, dict):
            out.append(row)
    return out


def filter_since(events: Iterable[dict], days: Optional[float] = None) -> list[dict]:
    if days is None:
        return list(events)
    cutoff = time.time() - days * 86400
    return [e for e in events if float(e.get("ts", 0)) >= cutoff]


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #


@dataclass
class Summary:
    sessions: int = 0
    total_seconds: float = 0.0
    battles: int = 0
    purchases: int = 0
    refreshes: int = 0
    errors: int = 0
    stucks: int = 0
    tasks_done: Counter = None  # type: ignore[assignment]
    scenes: Counter = None      # type: ignore[assignment]
    first_ts: float = 0.0
    last_ts: float = 0.0

    def __post_init__(self) -> None:
        if self.tasks_done is None:
            self.tasks_done = Counter()
        if self.scenes is None:
            self.scenes = Counter()

    @property
    def battles_per_hour(self) -> float:
        if self.total_seconds <= 0:
            return 0.0
        return self.battles / (self.total_seconds / 3600.0)

    @property
    def error_rate(self) -> float:
        if self.battles <= 0:
            return 0.0
        return self.errors / max(self.battles, 1)


def summarize(events: list[dict]) -> Summary:
    s = Summary()
    open_runs: dict[str, float] = defaultdict(float)
    for e in events:
        ts = float(e.get("ts", 0))
        if ts:
            s.first_ts = s.first_ts or ts
            s.last_ts = max(s.last_ts, ts)
        kind = e.get("type")
        if kind == RUN_START:
            s.sessions += 1
            open_runs["start"] = ts
        elif kind == RUN_END:
            dur = float(e.get("duration_seconds", 0) or 0)
            if dur <= 0 and open_runs.get("start"):
                dur = ts - open_runs["start"]
            s.total_seconds += max(0.0, dur)
        elif kind == BATTLE_FINISHED:
            s.battles += int(e.get("count", 1) or 1)
        elif kind == PURCHASE:
            s.purchases += int(e.get("count", 1) or 1)
        elif kind == REFRESH:
            s.refreshes += int(e.get("count", 1) or 1)
        elif kind == ERROR:
            s.errors += 1
        elif kind == STUCK:
            s.stucks += 1
        elif kind == TASK_DONE:
            s.tasks_done[str(e.get("task", "?"))] += 1
        elif kind == SCENE:
            s.scenes[str(e.get("scene", "?"))] += 1
    return s


def _fmt_duration(seconds: float) -> str:
    m, sec = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{sec:02d}s"


def build_markdown(events: list[dict], title: str = "e7bot 运行报表") -> str:
    s = summarize(events)
    if not events:
        return f"# {title}\n\n没有事件记录。先跑一次 `python run.py run`。\n"

    rng = ""
    if s.first_ts and s.last_ts:
        f = datetime.fromtimestamp(s.first_ts).strftime("%Y-%m-%d %H:%M")
        t = datetime.fromtimestamp(s.last_ts).strftime("%Y-%m-%d %H:%M")
        rng = f"{f} ~ {t}"

    lines = [
        f"# {title}",
        "",
        f"统计区间：{rng}",
        "",
        "## 总览",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 运行次数 | {s.sessions} |",
        f"| 累计运行时长 | {_fmt_duration(s.total_seconds)} |",
        f"| 完成战斗 | {s.battles} 场 |",
        f"| 平均效率 | {s.battles_per_hour:.1f} 场/小时 |",
        f"| 商店刷新 | {s.refreshes} 次 |",
        f"| 商店购买 | {s.purchases} 件 |",
        f"| 出错 | {s.errors} 次 |",
        f"| 疑似卡死 | {s.stucks} 次 |",
        "",
    ]

    if s.scenes:
        lines += ["## 场景停留分布", "", "| 场景 | 次数 |", "|---|---|"]
        for name, n in s.scenes.most_common(15):
            lines.append(f"| `{name}` | {n} |")
        lines.append("")

    if s.tasks_done:
        lines += ["## 任务完成", "", "| 任务 | 完成次数 |", "|---|---|"]
        for name, n in s.tasks_done.most_common():
            lines.append(f"| `{name}` | {n} |")
        lines.append("")

    # 每日战斗数：看长期趋势
    per_day: dict[str, int] = defaultdict(int)
    for e in events:
        if e.get("type") == BATTLE_FINISHED:
            day = datetime.fromtimestamp(float(e.get("ts", 0))).strftime("%Y-%m-%d")
            per_day[day] += int(e.get("count", 1) or 1)
    if per_day:
        lines += ["## 每日战斗数", "", "| 日期 | 场次 |", "|---|---|"]
        for day in sorted(per_day):
            lines.append(f"| {day} | {per_day[day]} |")
        lines.append("")

    # 错误明细
    errs = [e for e in events if e.get("type") in (ERROR, STUCK)]
    if errs:
        lines += ["## 最近的问题", "", "| 时间 | 类型 | 说明 |", "|---|---|---|"]
        for e in errs[-20:]:
            t = datetime.fromtimestamp(float(e.get("ts", 0))).strftime("%m-%d %H:%M:%S")
            msg = str(e.get("message", e.get("reason", "")))[:90].replace("|", "/")
            lines.append(f"| {t} | {e.get('type')} | {msg} |")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_csv(events: list[dict], path: Path) -> int:
    """把事件流摊平成 CSV。字段取所有事件的并集，保持稳定顺序。"""
    path = Path(path)
    preferred = ["iso", "ts", "type", "task", "scene", "count", "message", "reason"]
    keys: list[str] = list(preferred)
    for e in events:
        for k in e:
            if k not in keys:
                keys.append(k)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:  # utf-8-sig 让 Excel 不乱码
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for e in events:
            w.writerow(e)
    return len(events)


def default_path(log_dir: Path) -> Path:
    return Path(log_dir) / "stats" / "events.jsonl"


def prune_old_events(path: Path, keep_days: float = 90) -> int:
    """裁掉过期事件，返回删除条数。防止文件无限增长。"""
    events = read_events(path)
    if not events:
        return 0
    cutoff = time.time() - keep_days * 86400
    kept = [e for e in events if float(e.get("ts", 0)) >= cutoff]
    removed = len(events) - len(kept)
    if removed:
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for e in kept:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        tmp.replace(path)
    return removed


def recent_sessions(events: list[dict], limit: int = 10) -> list[tuple[str, float, str]]:
    """最近几次运行的 (开始时间, 时长秒, 结束原因)。"""
    out: list[tuple[str, float, str]] = []
    start: Optional[float] = None
    for e in events:
        if e.get("type") == RUN_START:
            start = float(e.get("ts", 0))
        elif e.get("type") == RUN_END and start:
            ts = float(e.get("ts", 0))
            dur = float(e.get("duration_seconds", 0) or (ts - start))
            out.append((
                datetime.fromtimestamp(start).strftime("%Y-%m-%d %H:%M"),
                dur,
                str(e.get("reason", "")),
            ))
            start = None
    return out[-limit:]


def cleanup_hint(days: float) -> str:
    return f"（超过 {days:g} 天的事件可用 `python run.py report --prune` 清理）"


__all__ = [
    "StatsRecorder", "read_events", "filter_since", "summarize", "Summary",
    "build_markdown", "write_csv", "default_path", "prune_old_events",
    "recent_sessions", "cleanup_hint",
    "RUN_START", "RUN_END", "SCENE", "BATTLE_FINISHED", "PURCHASE", "REFRESH",
    "TASK_DONE", "ERROR", "STUCK", "ALERT", "CLICK",
]
