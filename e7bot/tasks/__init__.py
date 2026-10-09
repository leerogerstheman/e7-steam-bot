"""任务注册表与构建。

引擎不关心有哪些任务，只按 `tasks.active` 的顺序实例化。
每个任务的名字对应配置里的 `[tasks.<名字>]`，其中 `sequence` / `gear_cleanup`
是通用的配置驱动任务，可以起多个实例（用 `type` 字段指定实现类）：

    [tasks.mail]
    type = "sequence"
    flow = [...]

    [tasks.daily_dispatch]
    type = "sequence"
    flow = [...]

    [tasks.active]
    active = ["repeat_stage", "mail", "daily_dispatch"]
"""

from __future__ import annotations

from typing import Any

from ..config import Config
from .base import REGISTRY, ClickSequence, Task  # noqa: F401  (re-export)
from .gear_cleanup import GearCleanupTask
from .repeat_stage import RepeatStageTask
from .secret_shop import SecretShopTask
from .sequence import SequenceTask

__all__ = [
    "REGISTRY",
    "ClickSequence",
    "Task",
    "RepeatStageTask",
    "SecretShopTask",
    "SequenceTask",
    "GearCleanupTask",
    "build_tasks",
]


def build_tasks(cfg: Config, names: list[str] | None = None) -> list[Task]:
    """按名字构建任务实例。

    任务名不一定是注册名 —— 配置里可以用 `type` 指定实现，用任务名当别名。
    """
    names = names or cfg.active_tasks()
    tasks: list[Task] = []
    for name in names:
        spec: dict[str, Any] = cfg.task(name)
        impl = str(spec.get("type", name))
        cls = REGISTRY.get(impl)
        if cls is None:
            raise KeyError(
                f"未知任务 {name!r}（type={impl!r}）。可用: {sorted(REGISTRY)}"
            )
        if spec.get("enabled", True) is False:
            continue
        tasks.append(cls(spec))
    return tasks
