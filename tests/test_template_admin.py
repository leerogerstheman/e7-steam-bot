"""模板管理工具测试：指纹相似度、批量阈值、未使用检测、重复发现。

`health` 子命令需要真实游戏画面，所以不在单元测试范围内（由 `run.py smoke` /
人工在游戏里跑）。这里覆盖所有**纯逻辑**部分。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pytest

from e7bot.vision import TemplateLibrary
from tools.selftest import LOBBY_LAYOUT, crop_norm, render
from tools.template_admin import (
    _signature,
    cmd_dedupe,
    cmd_list,
    cmd_threshold,
    cmd_unused,
)

from tests.conftest import install_templates


@pytest.fixture()
def tcfg(cfg, project: Path):
    """装好大厅模板的配置。"""
    frame = render(LOBBY_LAYOUT, 1920, 1080)
    install_templates(cfg, frame, LOBBY_LAYOUT)
    return cfg


# --------------------------------------------------------------------------- #
# 指纹
# --------------------------------------------------------------------------- #


def test_signature_is_normalised() -> None:
    img = np.random.default_rng(7).integers(0, 255, (60, 100, 3), dtype=np.uint8)
    sig = _signature(img)
    assert sig.shape == (16, 16)
    assert abs(float(np.linalg.norm(sig)) - 1.0) < 1e-5, "指纹应该是单位向量"


def test_signature_identical_images_dot_to_one() -> None:
    img = np.random.default_rng(8).integers(0, 255, (60, 100, 3), dtype=np.uint8)
    assert abs(float(np.sum(_signature(img) * _signature(img))) - 1.0) < 1e-5


def test_signature_different_images_are_less_similar() -> None:
    a = np.random.default_rng(1).integers(0, 255, (60, 100, 3), dtype=np.uint8)
    b = np.random.default_rng(2).integers(0, 255, (60, 100, 3), dtype=np.uint8)
    same = float(np.sum(_signature(a) * _signature(a)))
    diff = float(np.sum(_signature(a) * _signature(b)))
    assert diff < same - 0.2


def test_signature_of_flat_image_does_not_crash() -> None:
    """纯色图的方差为 0，归一化会除以 0 —— 必须兜住，不能返回 NaN。"""
    flat = np.full((40, 40, 3), 128, np.uint8)
    sig = _signature(flat)
    assert np.isfinite(sig).all()


def test_signature_accepts_grayscale() -> None:
    g = np.random.default_rng(9).integers(0, 255, (40, 40), dtype=np.uint8)
    assert _signature(g).shape == (16, 16)


# --------------------------------------------------------------------------- #
# list / unused
# --------------------------------------------------------------------------- #


def test_list_runs_and_reports_count(tcfg, capsys) -> None:
    rc = cmd_list(tcfg, argparse.Namespace())
    out = capsys.readouterr().out
    assert rc == 0
    assert "lobby/btn_hero" in out
    assert f"共 {len(LOBBY_LAYOUT)} 个模板" in out


def test_list_on_empty_library(cfg, capsys) -> None:
    rc = cmd_list(cfg, argparse.Namespace())
    assert rc == 1
    assert "模板库是空的" in capsys.readouterr().out


def test_unused_flags_templates_absent_from_config(tcfg, capsys) -> None:
    """模板名不在配置文本里 -> 报为未使用。

    注意测试配置的 `[scenes.lobby]` 里已经引用了 btn_adventure / btn_hero，
    所以只有 3 个是未使用的（这也说明"未使用"的判定是有效的，不是全报）。
    """
    rc = cmd_unused(tcfg, argparse.Namespace())
    out = capsys.readouterr().out
    assert rc == 0
    assert "被配置引用: 2 个" in out
    assert "未被引用  : 3 个" in out
    assert "lobby/btn_mail" in out
    assert "lobby/btn_adventure" not in out.split("未被引用不一定是垃圾")[0]


def test_unused_recognises_newly_referenced_template(tcfg, capsys) -> None:
    """把一个未使用的模板名写进配置后，引用数应该 +1。"""
    cfg_path = tcfg.source
    assert cfg_path is not None
    text = cfg_path.read_text(encoding="utf-8")
    cfg_path.write_text(
        text + '\n[scenes.mail]\nany = ["lobby/btn_mail"]\nnone = []\n',
        encoding="utf-8",
    )

    cmd_unused(tcfg, argparse.Namespace())
    out = capsys.readouterr().out
    assert "被配置引用: 3 个" in out
    assert "未被引用  : 2 个" in out


# --------------------------------------------------------------------------- #
# threshold
# --------------------------------------------------------------------------- #


def test_threshold_batch_update(tcfg, capsys) -> None:
    rc = cmd_threshold(tcfg, argparse.Namespace(pattern="lobby/*", value=0.72, dry_run=False))
    assert rc == 0
    assert "已把 5 个模板的阈值设为 0.72" in capsys.readouterr().out

    lib = TemplateLibrary(tcfg.template_root(), "default")
    for name in LOBBY_LAYOUT:
        assert lib.get(name).meta.threshold == 0.72


def test_threshold_glob_only_matches_pattern(tcfg, capsys) -> None:
    cmd_threshold(tcfg, argparse.Namespace(pattern="lobby/btn_hero", value=0.55, dry_run=False))
    lib = TemplateLibrary(tcfg.template_root(), "default")
    assert lib.get("lobby/btn_hero").meta.threshold == 0.55
    assert lib.get("lobby/btn_mail").meta.threshold == 0.80   # 没被改


def test_threshold_dry_run_changes_nothing(tcfg, capsys) -> None:
    cmd_threshold(tcfg, argparse.Namespace(pattern="lobby/*", value=0.5, dry_run=True))
    out = capsys.readouterr().out
    assert "会修改" in out
    lib = TemplateLibrary(tcfg.template_root(), "default")
    assert lib.get("lobby/btn_hero").meta.threshold == 0.80


def test_threshold_rejects_out_of_range(tcfg, capsys) -> None:
    rc = cmd_threshold(tcfg, argparse.Namespace(pattern="lobby/*", value=1.5, dry_run=False))
    assert rc == 2
    assert "0.3 ~ 0.999" in capsys.readouterr().out


def test_threshold_no_match(tcfg, capsys) -> None:
    rc = cmd_threshold(tcfg, argparse.Namespace(pattern="nothing/*", value=0.8, dry_run=False))
    assert rc == 1
    assert "没有匹配" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# dedupe
# --------------------------------------------------------------------------- #


def _plant_duplicate(cfg, tcfg) -> None:
    """把 lobby/btn_hero 原样复制成另一个名字，模拟"同一按钮采了两遍"。"""
    root = cfg.template_root() / "default"
    src_png = root / "lobby" / "btn_hero.png"
    dst_png = root / "lobby" / "btn_hero_copy.png"
    dst_png.write_bytes(src_png.read_bytes())
    meta = json.loads((root / "lobby" / "btn_hero.json").read_text(encoding="utf-8"))
    (root / "lobby" / "btn_hero_copy.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )


def test_dedupe_finds_planted_duplicate(tcfg, capsys) -> None:
    _plant_duplicate(tcfg, tcfg)
    rc = cmd_dedupe(tcfg, argparse.Namespace(similarity=0.97))
    out = capsys.readouterr().out
    assert rc == 0
    assert "发现 1 组近似重复" in out
    assert "lobby/btn_hero" in out
    assert "lobby/btn_hero_copy" in out


def test_dedupe_reports_none_when_all_distinct(tcfg, capsys) -> None:
    rc = cmd_dedupe(tcfg, argparse.Namespace(similarity=0.99))
    out = capsys.readouterr().out
    assert rc == 0
    assert "没有发现" in out


def test_dedupe_ignores_different_sizes(tcfg, capsys) -> None:
    """尺寸差超过 2px 就不该被判为重复 —— 那可能是不同分辨率下采的同一元素。"""
    root = tcfg.template_root() / "default"
    big = render(LOBBY_LAYOUT, 1920, 1080)
    import cv2

    # 同一位置，但裁一个明显不同的尺寸
    crop = crop_norm(big, LOBBY_LAYOUT["lobby/btn_hero"])[0:20, 0:30]
    p = root / "lobby" / "btn_hero_small.png"
    cv2.imencode(".png", crop)[1].tofile(str(p))
    (p.with_suffix(".json")).write_text(
        json.dumps({"ref_size": [1920, 1080], "threshold": 0.8}), encoding="utf-8"
    )

    cmd_dedupe(tcfg, argparse.Namespace(similarity=0.9))
    out = capsys.readouterr().out
    assert "btn_hero_small" not in out.split("处理建议")[0]


def test_dedupe_on_tiny_library(cfg, capsys) -> None:
    rc = cmd_dedupe(cfg, argparse.Namespace(similarity=0.97))
    assert rc == 0
    assert "模板太少" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# 与真实模板库格式的一致性
# --------------------------------------------------------------------------- #


def test_sidecar_roundtrip_through_tool(tcfg) -> None:
    """工具改完阈值后，重新加载模板库必须能读到新值（sidecar 写对了）。"""
    cmd_threshold(tcfg, argparse.Namespace(pattern="lobby/btn_mail", value=0.61, dry_run=False))
    lib = TemplateLibrary(tcfg.template_root(), "default")
    tpl = lib.get("lobby/btn_mail")
    assert tpl.meta.threshold == 0.61
    assert tpl.meta.ref_size == (1920, 1080)
    assert tpl.meta.region is not None
