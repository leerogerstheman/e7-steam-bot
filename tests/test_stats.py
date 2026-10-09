"""统计模块测试：事件流读写、容错、汇总、报表、CSV、清理。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from e7bot import stats as S


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")


def test_recorder_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    rec = S.StatsRecorder(path)
    rec.event("run_start", dry_run=False)
    rec.event("battle_finished", count=1, runs=1)
    rec.event("purchase", item="shop/item_covenant_bookmark")
    rec.close()

    events = S.read_events(path)
    assert len(events) == 3
    assert events[0]["type"] == "run_start"
    assert events[1]["runs"] == 1
    assert events[2]["item"] == "shop/item_covenant_bookmark"
    assert all("ts" in e and "iso" in e for e in events)


def test_recorder_disabled_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    rec = S.StatsRecorder(path, enabled=False)
    rec.event("run_start")
    rec.close()
    assert not path.exists()
    assert rec.written == 0


def test_read_tolerates_corrupt_last_line(tmp_path: Path) -> None:
    """进程被强杀时最后一行会残缺 —— 读取必须跳过而不是整份报废。"""
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps({"ts": time.time(), "type": "run_start"}) + "\n"
        + json.dumps({"ts": time.time(), "type": "battle_finished"}) + "\n"
        + '{"ts": 123, "type": "battle_fin',   # 残缺
        encoding="utf-8",
    )
    events = S.read_events(path)
    assert len(events) == 2
    assert [e["type"] for e in events] == ["run_start", "battle_finished"]


def test_read_missing_file_and_blank_lines(tmp_path: Path) -> None:
    assert S.read_events(tmp_path / "nope.jsonl") == []
    p = tmp_path / "b.jsonl"
    p.write_text('\n\n{"ts":1,"type":"run_start"}\n\n', encoding="utf-8")
    assert len(S.read_events(p)) == 1


def test_summarize_counts(tmp_path: Path) -> None:
    now = time.time()
    _write(tmp_path / "e.jsonl", [
        {"ts": now, "type": "run_start"},
        {"ts": now + 1, "type": "scene", "scene": "battle"},
        {"ts": now + 2, "type": "battle_finished", "count": 1},
        {"ts": now + 3, "type": "battle_finished", "count": 1},
        {"ts": now + 4, "type": "refresh", "count": 1},
        {"ts": now + 5, "type": "purchase", "count": 1},
        {"ts": now + 6, "type": "error", "message": "boom"},
        {"ts": now + 7, "type": "task_done", "task": "repeat_stage"},
        {"ts": now + 3600, "type": "run_end", "duration_seconds": 3600},
    ])
    s = S.summarize(S.read_events(tmp_path / "e.jsonl"))
    assert s.sessions == 1
    assert s.battles == 2
    assert s.refreshes == 1
    assert s.purchases == 1
    assert s.errors == 1
    assert s.total_seconds == 3600
    assert s.tasks_done["repeat_stage"] == 1
    assert s.scenes["battle"] == 1
    # 2 场 / 1 小时 = 2 场每小时
    assert abs(s.battles_per_hour - 2.0) < 0.01


def test_summarize_derives_duration_from_run_end_ts(tmp_path: Path) -> None:
    """没有 duration_seconds 时（旧格式）应该用 run_start/run_end 的时间差。"""
    now = time.time()
    _write(tmp_path / "e.jsonl", [
        {"ts": now, "type": "run_start"},
        {"ts": now + 600, "type": "run_end"},
    ])
    s = S.summarize(S.read_events(tmp_path / "e.jsonl"))
    assert abs(s.total_seconds - 600) < 1


def test_markdown_report_contains_key_numbers(tmp_path: Path) -> None:
    now = time.time()
    _write(tmp_path / "e.jsonl", [
        {"ts": now, "type": "run_start"},
        {"ts": now + 10, "type": "battle_finished", "count": 1},
        {"ts": now + 11, "type": "battle_finished", "count": 1},
        {"ts": now + 12, "type": "battle_finished", "count": 1},
        {"ts": now + 13, "type": "error", "message": "模板找不到: battle/btn_retry"},
        {"ts": now + 3600, "type": "run_end", "duration_seconds": 3600},
    ])
    md = S.build_markdown(S.read_events(tmp_path / "e.jsonl"), title="测试报表")
    assert "# 测试报表" in md
    assert "| 完成战斗 | 3 场 |" in md
    assert "3.0 场/小时" in md
    assert "模板找不到" in md
    assert "## 场景停留分布" not in md  # 没有 scene 事件就不该有这一节


def test_markdown_empty() -> None:
    md = S.build_markdown([])
    assert "没有事件记录" in md


def test_write_csv(tmp_path: Path) -> None:
    _write(tmp_path / "e.jsonl", [
        {"ts": 1.0, "type": "battle_finished", "count": 1, "extra": "x"},
        {"ts": 2.0, "type": "error", "message": "有,逗号|和竖线"},
    ])
    out = tmp_path / "out.csv"
    n = S.write_csv(S.read_events(tmp_path / "e.jsonl"), out)
    assert n == 2
    text = out.read_text(encoding="utf-8-sig")
    assert "battle_finished" in text
    assert "extra" in text          # 并集字段
    assert text.count("\n") >= 3    # 表头 + 2 行


def test_prune_old_events(tmp_path: Path) -> None:
    now = time.time()
    _write(tmp_path / "e.jsonl", [
        {"ts": now - 200 * 86400, "type": "run_start"},   # 200 天前，该删
        {"ts": now - 1 * 86400, "type": "run_start"},     # 1 天前，保留
        {"ts": now, "type": "battle_finished"},
    ])
    removed = S.prune_old_events(tmp_path / "e.jsonl", keep_days=90)
    assert removed == 1
    kept = S.read_events(tmp_path / "e.jsonl")
    assert len(kept) == 2
    assert all(e["type"] != "run_start" or e["ts"] > now - 100 * 86400 for e in kept)


def test_filter_since(tmp_path: Path) -> None:
    now = time.time()
    events = [
        {"ts": now - 10 * 86400, "type": "a"},
        {"ts": now - 1 * 86400, "type": "b"},
    ]
    assert len(S.filter_since(events, days=3)) == 1
    assert len(S.filter_since(events, days=None)) == 2


def test_recent_sessions(tmp_path: Path) -> None:
    now = time.time()
    _write(tmp_path / "e.jsonl", [
        {"ts": now, "type": "run_start"},
        {"ts": now + 120, "type": "run_end", "duration_seconds": 120, "reason": "急停"},
        {"ts": now + 200, "type": "run_start"},
        {"ts": now + 500, "type": "run_end", "duration_seconds": 300, "reason": "任务完成"},
    ])
    rows = S.recent_sessions(S.read_events(tmp_path / "e.jsonl"))
    assert len(rows) == 2
    assert rows[0][1] == 120
    assert rows[1][2] == "任务完成"
