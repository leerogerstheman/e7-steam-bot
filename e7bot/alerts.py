"""告警系统：脚本出事时让你知道。

无人值守跑脚本最怕的不是出错，而是**出错了没人知道**——早上起来发现卡在某个弹窗上
空转了 6 小时。参考项目 AutoFarming 就是靠 ntfy.sh 推送 + 截图解决这个问题的。

这里提供四种通知渠道，可任意组合：

| 渠道 | 适用场景 | 依赖 |
|---|---|---|
| `log` | 总是启用，写进日志文件 | 无 |
| `sound` | 你就在电脑旁边 | 无（winsound 标准库） |
| `messagebox` | 需要强提醒（置顶弹窗） | 无（user32） |
| `ntfy` | 你不在电脑旁边 | 无（urllib 标准库，走 HTTP） |
| `webhook` | 接自己的服务 / 企业微信 / Discord | 无（urllib） |

设计上的两个注意点：

1. **不能阻塞引擎**。`MessageBoxW` 会一直阻塞到用户点掉，而 ntfy 是网络请求
   可能超时好几秒。所以所有通知都在**后台线程**里发，引擎不等待。
2. **要限流**。卡死场景下每 tick 都可能触发告警，不加限制会刷爆 ntfy 配额
   或者弹一屏窗口。所以有 `min_interval_seconds` 去重。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("e7bot.alerts")


# --------------------------------------------------------------------------- #
# 渠道
# --------------------------------------------------------------------------- #


class Notifier:
    name = "base"

    def available(self) -> bool:
        return True

    def send(self, title: str, message: str, image: Optional[Path] = None) -> bool:  # pragma: no cover
        raise NotImplementedError


class LogNotifier(Notifier):
    name = "log"

    def send(self, title: str, message: str, image: Optional[Path] = None) -> bool:
        log.warning("[告警] %s —— %s%s", title, message, f"（截图: {image}）" if image else "")
        return True


class SoundNotifier(Notifier):
    """用系统提示音。比 winsound.Beep 好：不占用频率参数，尊重用户的系统音效设置。"""

    name = "sound"

    def __init__(self, repeat: int = 3):
        self.repeat = max(1, repeat)

    def available(self) -> bool:
        import importlib.util

        # 用 find_spec 探测而不 import：避免为了一次可用性检查就去加载模块
        return importlib.util.find_spec("winsound") is not None

    def send(self, title: str, message: str, image: Optional[Path] = None) -> bool:
        try:
            import winsound

            for i in range(self.repeat):
                # MB_ICONHAND = 0x10，系统"严重错误"音
                winsound.MessageBeep(0x10)
                if i + 1 < self.repeat:
                    time.sleep(0.35)
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("播放告警音失败: %s", exc)
            return False


class MessageBoxNotifier(Notifier):
    """置顶弹窗。

    `MB_SYSTEMMODAL (0x1000)` 让窗口置顶于所有程序之上，`MB_SETFOREGROUND` 抢焦点。
    因为它会阻塞到用户点击，所以**必须在后台线程调用**，否则引擎会僵在那里。

    适合"你必须马上过来看"的场景，不适合频繁告警。
    """

    name = "messagebox"

    def send(self, title: str, message: str, image: Optional[Path] = None) -> bool:
        try:
            import ctypes

            MB_OK = 0x0
            MB_ICONWARNING = 0x30
            MB_SYSTEMMODAL = 0x1000
            MB_SETFOREGROUND = 0x10000
            text = message + (f"\n\n截图: {image}" if image else "")
            ctypes.windll.user32.MessageBoxW(
                None, text, f"e7bot — {title}",
                MB_OK | MB_ICONWARNING | MB_SYSTEMMODAL | MB_SETFOREGROUND,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("弹窗告警失败: %s", exc)
            return False


class NtfyNotifier(Notifier):
    """推送到 ntfy.sh（或自建实例）。

    手机装 ntfy App、订阅同一个 topic 就能收到推送。不需要注册账号。
    **topic 名等于密码** —— 别人猜到 topic 就能看到你的推送，所以用随机字符串。
    """

    name = "ntfy"

    def __init__(self, topic: str, server: str = "https://ntfy.sh", token: str = "",
                 priority: str = "high", timeout: float = 8.0, attach_image: bool = True):
        self.topic = (topic or "").strip()
        self.server = (server or "https://ntfy.sh").rstrip("/")
        self.token = token.strip()
        self.priority = priority
        self.timeout = timeout
        self.attach_image = attach_image

    def available(self) -> bool:
        return bool(self.topic)

    def send(self, title: str, message: str, image: Optional[Path] = None) -> bool:
        if not self.available():
            return False

        url = f"{self.server}/{self.topic}"
        headers = {
            "Title": _ascii_safe(title),   # ntfy 的 HTTP header 只能是 ASCII
            "Priority": self.priority,
            "Tags": "warning,robot",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        try:
            # 带图时用 multipart 上传（ntfy 支持 file 字段）
            if image and self.attach_image and Path(image).exists():
                body, ctype = _multipart(message, Path(image))
                headers["Content-Type"] = ctype
            else:
                body = message.encode("utf-8")
                headers["Content-Type"] = "text/plain; charset=utf-8"

            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                ok = 200 <= resp.status < 300
            if not ok:
                log.debug("ntfy 返回非 2xx")
            return ok
        except urllib.error.HTTPError as exc:
            log.warning("ntfy 推送失败 (HTTP %s)。topic 是否正确？服务端是否允许匿名发布？", exc.code)
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("ntfy 推送失败: %s", exc)
            return False


class WebhookNotifier(Notifier):
    """通用 JSON webhook。接企业微信机器人 / Discord / 自建服务。"""

    name = "webhook"

    def __init__(self, url: str, timeout: float = 8.0):
        self.url = (url or "").strip()
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.url)

    def send(self, title: str, message: str, image: Optional[Path] = None) -> bool:
        if not self.available():
            return False
        payload = json.dumps(
            {"title": title, "message": message, "image": str(image) if image else None,
             "source": "e7bot", "ts": time.time()},
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            req = urllib.request.Request(
                self.url, data=payload,
                headers={"Content-Type": "application/json; charset=utf-8"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return 200 <= resp.status < 300
        except Exception as exc:  # noqa: BLE001
            log.warning("webhook 推送失败: %s", exc)
            return False


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def _ascii_safe(text: str) -> str:
    """HTTP header 只能放 ASCII；中文标题降级成 URL 编码形式，避免请求直接被拒。"""
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        from urllib.parse import quote

        return quote(text, safe="")


def _multipart(message: str, image: Path) -> tuple[bytes, str]:
    boundary = "----e7bot" + str(int(time.time() * 1000))
    data = bytearray()

    def part(headers: str, body: bytes) -> None:
        data.extend(f"--{boundary}\r\n{headers}\r\n".encode())
        data.extend(body)
        data.extend(b"\r\n")

    part('Content-Disposition: form-data; name="message"', message.encode("utf-8"))
    try:
        raw = image.read_bytes()
    except Exception:
        raw = b""
    if raw:
        part(
            f'Content-Disposition: form-data; name="file"; filename="{image.name}"\r\n'
            f"Content-Type: image/png",
            raw,
        )
    data.extend(f"--{boundary}--\r\n".encode())
    return bytes(data), f"multipart/form-data; boundary={boundary}"


# --------------------------------------------------------------------------- #
# 管理器
# --------------------------------------------------------------------------- #


@dataclass
class AlertRecord:
    ts: float
    title: str
    message: str
    image: Optional[Path]


class AlertManager:
    """按配置分发告警，带限流。所有发送都在后台线程，不阻塞引擎。"""

    def __init__(self, cfg, log_dir: Optional[Path] = None):
        section = cfg.section("alerts") if hasattr(cfg, "section") else {}
        self.enabled = bool(section.get("enabled", True))
        self.min_interval = float(section.get("min_interval_seconds", 60))
        self.include_screenshot = bool(section.get("include_screenshot", True))
        self.log_dir = Path(log_dir) if log_dir else Path("logs")

        self.notifiers: list[Notifier] = [LogNotifier()]

        if self.enabled:
            snd = section.get("sound", {}) or {}
            if snd.get("enabled", True):
                self.notifiers.append(SoundNotifier(int(snd.get("repeat", 3))))

            mb = section.get("messagebox", {}) or {}
            if mb.get("enabled", False):
                self.notifiers.append(MessageBoxNotifier())

            n = section.get("ntfy", {}) or {}
            if n.get("enabled", False):
                self.notifiers.append(NtfyNotifier(
                    topic=str(n.get("topic", "")),
                    server=str(n.get("server", "https://ntfy.sh")),
                    token=str(n.get("token", "")),
                    priority=str(n.get("priority", "high")),
                ))

            w = section.get("webhook", {}) or {}
            if w.get("enabled", False):
                self.notifiers.append(WebhookNotifier(str(w.get("url", ""))))

        self._last_sent: dict[str, float] = {}
        self._lock = threading.Lock()
        self.history: list[AlertRecord] = []

    def active_channels(self) -> list[str]:
        return [n.name for n in self.notifiers if n.available()]

    def send(self, title: str, message: str, image: Optional[Path] = None,
             key: Optional[str] = None, force: bool = False) -> None:
        """发一条告警。key 用于限流分组（默认用 title）。"""
        if not self.enabled and title != "test":
            return

        dedupe_key = key or title
        now = time.time()
        with self._lock:
            last = self._last_sent.get(dedupe_key, 0.0)
            if not force and self.min_interval > 0 and (now - last) < self.min_interval:
                log.debug("告警被限流: %s", dedupe_key)
                return
            self._last_sent[dedupe_key] = now
            self.history.append(AlertRecord(now, title, message, image))

        img = Path(image) if (image and self.include_screenshot) else None

        def _dispatch() -> None:
            for n in self.notifiers:
                try:
                    if n.available():
                        n.send(title, message, img)
                except Exception as exc:  # noqa: BLE001
                    log.debug("通知渠道 %s 失败: %s", n.name, exc)

        threading.Thread(target=_dispatch, daemon=True, name="alert").start()

    # -- 语义化快捷方法 ---------------------------------------------------- #

    def stopped(self, reason: str, image: Optional[Path] = None) -> None:
        self.send("脚本已停止", reason, image, key="stopped", force=True)

    def stuck(self, reason: str, image: Optional[Path] = None) -> None:
        self.send("脚本可能卡住了", reason, image, key="stuck")

    def error(self, reason: str, image: Optional[Path] = None) -> None:
        self.send("脚本出错", reason, image, key="error")

    def test(self) -> dict[str, bool]:
        """测试所有渠道是否真的能通（`run.py alert-test` 用）。"""
        results: dict[str, bool] = {}
        for n in self.notifiers:
            try:
                results[n.name] = bool(n.send(
                    "e7bot 测试告警",
                    "如果你看到这条消息，说明该渠道配置正确。",
                    None,
                )) if n.available() else False
            except Exception as exc:  # noqa: BLE001
                log.warning("渠道 %s 测试失败: %s", n.name, exc)
                results[n.name] = False
        return results
