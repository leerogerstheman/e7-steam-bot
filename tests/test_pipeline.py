"""pytest 包装：把 tools/selftest.py 的离线自检接入 pytest。

    pytest -q

没有 pytest 也能跑：`python run.py selftest`
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.selftest import (  # noqa: E402
    Checker,
    _test_config,
    _test_coords,
    _test_scene,
    _test_sequence,
    _test_vision,
)


@pytest.fixture()
def tmp_templates(tmp_path: Path) -> Path:
    return tmp_path


def test_coordinate_mapping() -> None:
    c = Checker()
    _test_coords(c)
    assert not c.failed, c.failed


def test_vision_pipeline(tmp_templates: Path) -> None:
    c = Checker()
    _test_vision(c, tmp_templates, verbose=False)
    assert not c.failed, c.failed


def test_scene_state_machine(tmp_templates: Path) -> None:
    c = Checker()
    _test_scene(c, tmp_templates)
    assert not c.failed, c.failed


def test_config_system(tmp_templates: Path) -> None:
    c = Checker()
    _test_config(c, tmp_templates)
    assert not c.failed, c.failed


def test_click_sequence() -> None:
    c = Checker()
    _test_sequence(c)
    assert not c.failed, c.failed
