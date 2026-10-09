"""告警模块测试：限流、渠道可用性、编码与 multipart 构造。

**不会真的发网络请求或弹窗** —— 只测纯逻辑与可用性判断。
"""

from __future__ import annotations

import time
from pathlib import Path

from e7bot.alerts import (
    AlertManager,
    LogNotifier,
    MessageBoxNotifier,
    Notifier,
    NtfyNotifier,
    SoundNotifier,
    WebhookNotifier,
    _ascii_safe,
    _multipart,
)
from e7bot.config import Config


class CountingNotifier(Notifier):
    """记录被调用次数，用来验证限流。"""

    name = "counting"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, title: str, message: str, image=None) -> bool:
        self.sent.append((title, message))
        return True


def _wait_for_dispatch(timeout: float = 1.0) -> None:
    """告警是异步派发的，测试里要等一下。"""
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.01)


def test_disabled_manager_sends_nothing(project: Path) -> None:
    cfg = Config.load(project / "config" / "default.toml")  # alerts.enabled = false
    mgr = AlertManager(cfg, project / "logs")
    counter = CountingNotifier()
    mgr.notifiers = [counter]
    mgr.stopped("测试")
    _wait_for_dispatch(0.2)
    assert counter.sent == []


def test_rate_limiting_dedupes_same_key(project: Path) -> None:
    cfg = Config.load(project / "config" / "default.toml")
    cfg.data["alerts"]["enabled"] = True
    cfg.data["alerts"]["min_interval_seconds"] = 60
    mgr = AlertManager(cfg, project / "logs")
    counter = CountingNotifier()
    mgr.notifiers = [counter]

    mgr.stuck("第一次卡住")
    mgr.stuck("第二次卡住")     # 同 key，应被限流
    mgr.stuck("第三次卡住")
    _wait_for_dispatch()
    assert len(counter.sent) == 1

    # 换一个 key 应该能发出去
    mgr.error("出错了")
    _wait_for_dispatch()
    assert len(counter.sent) == 2


def test_force_bypasses_rate_limit(project: Path) -> None:
    cfg = Config.load(project / "config" / "default.toml")
    cfg.data["alerts"]["enabled"] = True
    cfg.data["alerts"]["min_interval_seconds"] = 999
    mgr = AlertManager(cfg, project / "logs")
    counter = CountingNotifier()
    mgr.notifiers = [counter]

    mgr.stopped("停机一")
    mgr.stopped("停机二")      # stopped 是 force=True，不该被限流
    _wait_for_dispatch()
    assert len(counter.sent) == 2


def test_channel_availability_from_config(project: Path) -> None:
    cfg = Config.load(project / "config" / "default.toml")
    cfg.data["alerts"]["enabled"] = True
    cfg.data["alerts"]["ntfy"]["enabled"] = True
    cfg.data["alerts"]["ntfy"]["topic"] = "my-secret-topic"
    cfg.data["alerts"]["webhook"]["enabled"] = True
    cfg.data["alerts"]["webhook"]["url"] = "https://example.invalid/hook"
    mgr = AlertManager(cfg, project / "logs")
    channels = mgr.active_channels()
    assert "log" in channels
    assert "sound" in channels
    assert "ntfy" in channels
    assert "webhook" in channels


def test_ntfy_without_topic_is_unavailable() -> None:
    assert NtfyNotifier("").available() is False
    assert NtfyNotifier("   ").available() is False
    assert NtfyNotifier("abc").available() is True


def test_webhook_without_url_is_unavailable() -> None:
    assert WebhookNotifier("").available() is False
    assert WebhookNotifier("https://x.invalid").available() is True


def test_log_notifier_always_available() -> None:
    assert LogNotifier().available() is True
    assert SoundNotifier().available() is True          # Windows 上 winsound 一定在
    assert isinstance(MessageBoxNotifier().available(), bool)


def test_ntfy_bad_host_fails_gracefully() -> None:
    """推送失败必须返回 False，不能抛异常打断引擎。"""
    n = NtfyNotifier("topic", server="http://127.0.0.1:1", timeout=0.5)
    assert n.send("标题", "内容") is False


def test_webhook_bad_host_fails_gracefully() -> None:
    w = WebhookNotifier("http://127.0.0.1:1/hook", timeout=0.5)
    assert w.send("标题", "内容") is False


def test_ascii_safe() -> None:
    assert _ascii_safe("Epic Seven") == "Epic Seven"
    out = _ascii_safe("脚本已停止")
    assert out.isascii()
    assert "%" in out          # 中文被 URL 编码


def test_multipart_structure(tmp_path: Path) -> None:
    import numpy as np
    import cv2

    img = tmp_path / "shot.png"
    cv2.imencode(".png", np.zeros((8, 8, 3), np.uint8))[1].tofile(str(img))

    body, ctype = _multipart("有中文的消息", img)
    assert ctype.startswith("multipart/form-data; boundary=")
    boundary = ctype.split("boundary=")[1].encode()
    assert body.count(b"--" + boundary) >= 2
    assert b'name="message"' in body
    assert b'name="file"' in body
    assert "有中文的消息".encode("utf-8") in body
    assert body.endswith(b"--\r\n")


def test_multipart_without_image(tmp_path: Path) -> None:
    body, _ = _multipart("只有文字", tmp_path / "missing.png")
    assert b'name="file"' not in body
    assert "只有文字".encode("utf-8") in body


def test_history_records_alerts(project: Path) -> None:
    cfg = Config.load(project / "config" / "default.toml")
    cfg.data["alerts"]["enabled"] = True
    mgr = AlertManager(cfg, project / "logs")
    mgr.notifiers = [CountingNotifier()]
    mgr.send("出错", "出错了", key="e1")
    mgr.send("出错", "又错了", key="e2")
    _wait_for_dispatch()
    assert len(mgr.history) == 2
    assert mgr.history[0].message == "出错了"


def test_semantic_shortcuts_use_fixed_keys(project: Path) -> None:
    """stuck/error 各自一个限流 key —— 同类的重复告警会被合并。"""
    cfg = Config.load(project / "config" / "default.toml")
    cfg.data["alerts"]["enabled"] = True
    cfg.data["alerts"]["min_interval_seconds"] = 999
    mgr = AlertManager(cfg, project / "logs")
    counter = CountingNotifier()
    mgr.notifiers = [counter]

    mgr.stuck("卡住一")
    mgr.stuck("卡住二")
    mgr.error("错误一")
    mgr.error("错误二")
    _wait_for_dispatch()
    # stuck 和 error 是不同的 key，所以各放行一条
    assert len(counter.sent) == 2
    assert {t for t, _ in counter.sent} == {"脚本可能卡住了", "脚本出错"}
