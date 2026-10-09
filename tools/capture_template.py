"""交互式模板采集器。

**为什么必须有这个工具**：模板只能从真实客户端截取 —— 任何"我替你画好了 UI 图"
的做法都是错的（版本一更新就全废）。所以本项目的交付方式是：
框架 + 采集工具 + 命名规范，模板由你在自己的客户端上采。

采集出来的每个模板是**一对文件**：

    templates/default/battle/btn_retry.png    裁剪出来的小图
    templates/default/battle/btn_retry.json   元数据（参考分辨率 + 搜索区域 + 阈值）

元数据里的 `region` 是自动推导的：以模板为中心、放大若干倍的归一化矩形。
有了它，场景识别只在小范围内做匹配，速度能快一两个数量级，
误匹配也大幅减少 —— 这是本项目能实时跑起来的关键。

用法：
    python run.py capture
"""

from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Optional

import cv2
import numpy as np
from PIL import Image, ImageTk

from e7bot.capture import ScreenGrabber
from e7bot.config import Config
from e7bot.vision import Matcher, MatchOptions, TemplateLibrary
from e7bot.winutil import (
    GameNotFound,
    Rect,
    client_rect_screen,
    enable_dpi_awareness,
    find_window,
    window_scale_note,
)

#: 建议的模板命名（场景识别靠这些名字）。采集界面里会作为提示列出。
SUGGESTED = {
    "lobby": ["lobby/btn_adventure", "lobby/btn_hero", "lobby/btn_sanctuary",
              "lobby/btn_mail", "lobby/btn_shop"],
    "battle": ["battle/btn_auto_off", "battle/btn_auto_on",
               "battle/btn_speed_x1", "battle/btn_speed_x2", "battle/icon_pause",
               "battle/btn_start_battle", "battle/btn_retry", "battle/btn_confirm_result"],
    "common": ["common/btn_ok", "common/btn_confirm", "common/btn_close", "common/btn_cancel",
               "common/icon_loading", "common/popup_no_stamina", "common/popup_inventory_full"],
    "shop": ["shop/btn_refresh", "shop/btn_refresh_confirm", "shop/btn_buy",
             "shop/btn_buy_confirm", "shop/item_covenant_bookmark", "shop/item_mystic_medal"],
    "inventory": ["inventory/btn_filter", "inventory/btn_sell", "inventory/btn_sell_confirm",
                  "inventory/icon_locked"],
}


# --------------------------------------------------------------------------- #
# 框选覆盖层
# --------------------------------------------------------------------------- #


class RectPicker(tk.Toplevel):
    """盖在游戏窗口上的框选层。

    显示的是**脚本刚抓到的真实帧**（不是半透明玻璃），所以"你框到的"就是
    "脚本看到的"，不存在坐标偏差。
    """

    MIN_SIZE = 6

    def __init__(self, master: tk.Misc, frame: np.ndarray, origin: tuple[int, int]):
        super().__init__(master)
        self.frame = frame
        self.result: Optional[tuple[int, int, int, int]] = None
        self._start: Optional[tuple[int, int]] = None
        self._rect_id: Optional[int] = None
        self._hint_id: Optional[int] = None

        h, w = frame.shape[:2]
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.geometry(f"{w}x{h}+{origin[0]}+{origin[1]}")

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        self._photo = ImageTk.PhotoImage(Image.fromarray(rgb))

        self.canvas = tk.Canvas(self, width=w, height=h, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)
        self.canvas.create_text(
            12, 14, anchor="nw", fill="#00ff88", font=("Consolas", 13, "bold"),
            text="拖动框选模板区域   |   ESC 取消   |   右键取消",
            tags="hud",
        )

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<ButtonPress-3>", lambda _e: self._cancel())
        self.bind("<Escape>", lambda _e: self._cancel())

        self.update_idletasks()
        self.grab_set()
        self.focus_force()

    # -- 事件 -------------------------------------------------------------- #

    def _on_press(self, event: tk.Event) -> None:
        self._start = (event.x, event.y)
        if self._rect_id:
            self.canvas.delete(self._rect_id)
        self._rect_id = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="#00ff88", width=2
        )

    def _on_drag(self, event: tk.Event) -> None:
        if not self._start or not self._rect_id:
            return
        x0, y0 = self._start
        self.canvas.coords(self._rect_id, x0, y0, event.x, event.y)
        if self._hint_id:
            self.canvas.delete(self._hint_id)
        w, h = abs(event.x - x0), abs(event.y - y0)
        self._hint_id = self.canvas.create_text(
            min(x0, event.x), min(y0, event.y) - 10, anchor="sw", fill="#ffff00",
            font=("Consolas", 12, "bold"), text=f"{w} x {h}",
        )

    def _on_release(self, event: tk.Event) -> None:
        if not self._start:
            return
        x0, y0 = self._start
        x1, y1 = event.x, event.y
        left, top = min(x0, x1), min(y0, y1)
        w, h = abs(x1 - x0), abs(y1 - y0)
        if w < self.MIN_SIZE or h < self.MIN_SIZE:
            self._cancel()
            return
        self.result = (left, top, w, h)
        self._close()

    def _cancel(self) -> None:
        self.result = None
        self._close()

    def _close(self) -> None:
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


# --------------------------------------------------------------------------- #
# 主控制界面
# --------------------------------------------------------------------------- #


class CaptureApp:
    def __init__(self, cfg: Config):
        enable_dpi_awareness()
        self.cfg = cfg
        self.root_dir = cfg.template_root()
        self.profile = str(cfg.get("templates.profile", "default"))
        self.lib = TemplateLibrary(self.root_dir, self.profile)
        self.matcher = Matcher(self.lib)

        self.window = find_window(
            title_patterns=cfg.get("window.title_patterns", []),
            exe_patterns=cfg.get("window.exe_patterns", []),
            min_size=tuple(cfg.get("window.min_size", (800, 450))),
        )
        if self.window is None:
            raise GameNotFound(
                "没找到第七史诗窗口，请先启动游戏（Steam 版或 Demo 均可）。"
            )

        from e7bot.winutil import monitor_index_of

        self.grabber = ScreenGrabber(
            hwnd=self.window.hwnd,
            preferred=list(cfg.get("capture.backends", [])),
            monitor_index=monitor_index_of(self.window.hwnd),
            target_fps=30,
            verbose=False,
        )

        self.root = tk.Tk()
        self.root.title(f"e7bot 模板采集器 — {window_scale_note(self.window.client)}")
        self.root.geometry("+40+40")
        self.root.attributes("-topmost", True)
        enable_dpi_awareness()  # Tk 可能改过 DPI 设置，这里再确认一次

        self._build_ui()
        self._refresh_list()

    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        pad = {"padx": 6, "pady": 4}
        info = tk.Frame(self.root)
        info.pack(fill="x", **pad)
        tk.Label(info, text=f"窗口: {self.window.title}", anchor="w",
                 font=("Microsoft YaHei UI", 9, "bold")).pack(fill="x")
        tk.Label(info, text=f"客户区: {window_scale_note(self.window.client)}   模板目录: "
                            f"{self.root_dir / self.profile}",
                 anchor="w", fg="#555").pack(fill="x")

        # 名称输入
        name_row = tk.Frame(self.root)
        name_row.pack(fill="x", **pad)
        tk.Label(name_row, text="模板名:").pack(side="left")
        self.name_var = tk.StringVar(value="battle/btn_retry")
        entry = ttk.Entry(name_row, textvariable=self.name_var, width=42)
        entry.pack(side="left", fill="x", expand=True, padx=4)

        # 建议名下拉
        sug_row = tk.Frame(self.root)
        sug_row.pack(fill="x", **pad)
        tk.Label(sug_row, text="建议:").pack(side="left")
        self.suggest_var = tk.StringVar()
        sug = ttk.Combobox(
            sug_row, textvariable=self.suggest_var, width=40, state="readonly",
            values=[n for names in SUGGESTED.values() for n in names],
        )
        sug.pack(side="left", fill="x", expand=True, padx=4)
        sug.bind("<<ComboboxSelected>>", lambda _e: self.name_var.set(self.suggest_var.get()))

        # 按钮
        btns = tk.Frame(self.root)
        btns.pack(fill="x", **pad)
        ttk.Button(btns, text="① 框选新模板", command=self.capture_new).pack(side="left", padx=3)
        ttk.Button(btns, text="② 验证全部模板", command=self.verify_all).pack(side="left", padx=3)
        ttk.Button(btns, text="③ 打开模板目录", command=self.open_dir).pack(side="left", padx=3)

        # 选项
        opts = tk.Frame(self.root)
        opts.pack(fill="x", **pad)
        self.auto_region = tk.BooleanVar(value=True)
        self.auto_threshold = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="自动推导搜索区域(加速)", variable=self.auto_region).pack(side="left")
        ttk.Checkbutton(opts, text="自动调阈值", variable=self.auto_threshold).pack(side="left", padx=8)
        tk.Label(opts, text="阈值:").pack(side="left")
        self.thr_var = tk.StringVar(value="0.86")
        ttk.Entry(opts, textvariable=self.thr_var, width=6).pack(side="left")

        # 列表
        list_frame = tk.Frame(self.root)
        list_frame.pack(fill="both", expand=True, **pad)
        cols = ("name", "size", "ref", "thr")
        self.tree = ttk.Treeview(list_frame, columns=cols, show="headings", height=14)
        for c, w in zip(cols, (240, 90, 90, 60)):
            self.tree.heading(c, text={"name": "模板", "size": "尺寸",
                                       "ref": "参考分辨率", "thr": "阈值"}[c])
            self.tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _e: self.name_var.set(self._selected_name() or ""))

        self.status = tk.Label(self.root, text="就绪", anchor="w", fg="#006600",
                               font=("Consolas", 9))
        self.status.pack(fill="x", **pad)

    # ------------------------------------------------------------------ #

    def _set_status(self, text: str, color: str = "#006600") -> None:
        self.status.configure(text=text, fg=color)
        self.root.update_idletasks()

    def _selected_name(self) -> Optional[str]:
        sel = self.tree.selection()
        return self.tree.item(sel[0], "values")[0] if sel else None

    def _refresh_list(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for name in self.lib.names():
            t = self.lib.get(name)
            self.tree.insert("", "end", values=(
                name, f"{t.size[0]}x{t.size[1]}",
                f"{t.meta.ref_size[0]}x{t.meta.ref_size[1]}",
                f"{t.meta.threshold:.2f}",
            ))

    def _grab(self) -> tuple[np.ndarray, Rect]:
        rect = client_rect_screen(self.window.hwnd)
        return self.grabber.grab(rect), rect

    # ------------------------------------------------------------------ #

    def capture_new(self) -> None:
        name = self.name_var.get().strip().replace("\\", "/")
        if not name:
            messagebox.showwarning("缺少名称", "请先填写模板名，例如 battle/btn_retry")
            return
        if not name.lower().endswith(".png"):
            name += ".png"

        try:
            frame, rect = self._grab()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("截图失败", str(exc))
            return

        self.root.withdraw()
        self.root.update()
        try:
            picker = RectPicker(self.root, frame, (rect.left, rect.top))
            self.root.wait_window(picker)
            result = picker.result
        finally:
            self.root.deiconify()
            self.root.attributes("-topmost", True)

        if not result:
            self._set_status("已取消框选", "#aa6600")
            return

        left, top, w, h = result
        crop = frame[top: top + h, left: left + w]
        if crop.size == 0:
            messagebox.showerror("裁剪失败", "框选区域为空")
            return

        out = self.root_dir / self.profile / name
        out.parent.mkdir(parents=True, exist_ok=True)
        ok, buf = cv2.imencode(".png", crop)
        if not ok:
            messagebox.showerror("保存失败", "PNG 编码失败")
            return
        buf.tofile(str(out))

        meta: dict = {
            "ref_size": [rect.width, rect.height],
            "note": f"采集自 {rect.width}x{rect.height}，客户区 ({left},{top}) {w}x{h}",
        }
        if self.auto_region.get():
            # 以模板为中心放大 3 倍（最小 8% 边长），作为默认搜索区域
            cx = (left + w / 2) / rect.width
            cy = (top + h / 2) / rect.height
            rw = max(0.08, min(1.0, (w * 3) / rect.width))
            rh = max(0.08, min(1.0, (h * 3) / rect.height))
            meta["region"] = [
                round(max(0.0, min(1.0 - rw, cx - rw / 2)), 4),
                round(max(0.0, min(1.0 - rh, cy - rh / 2)), 4),
                round(rw, 4), round(rh, 4),
            ]
        if self.auto_threshold.get():
            thr = float(self.thr_var.get() or 0.86)
        else:
            thr = float(self.thr_var.get() or 0.86)
        meta["threshold"] = thr
        (out.with_suffix(".json")).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        self.lib.reload()
        self.matcher.clear_cache()
        self._refresh_list()

        # 立刻回验：再抓一帧，看能不能匹配上（顺便给出实际分数，便于调阈值）
        score = self._verify_one(name, quiet=True)
        if score is None:
            self._set_status(f"已保存 {name}，但立即回验未匹配上（画面变了吗？）", "#aa6600")
        else:
            self._set_status(f"已保存 {name}  回验分数 {score:.3f}  "
                             f"{'✓ 可用' if score >= thr else '⚠ 偏低，建议降阈值或重采'}")
        self.name_var.set(name[:-4] if name.endswith(".png") else name)

    def _verify_one(self, name: str, quiet: bool = False) -> Optional[float]:
        try:
            frame, rect = self._grab()
            m = self.matcher.find(name, frame, rect, opts=MatchOptions(threshold=0.0))
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                self._set_status(f"验证 {name} 失败: {exc}", "#cc0000")
            return None
        return m.score if m else 0.0

    def verify_all(self) -> None:
        names = self.lib.names()
        if not names:
            messagebox.showinfo("没有模板", "模板库是空的，先框选采集。")
            return
        try:
            frame, rect = self._grab()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("截图失败", str(exc))
            return

        good, weak, missing = [], [], []
        for name in names:
            t = self.lib.get(name)
            try:
                m = self.matcher.find(name, frame, rect, opts=MatchOptions(threshold=0.0))
            except Exception:  # noqa: BLE001
                m = None
            if m is None:
                missing.append(name)
            elif m.score >= t.meta.threshold:
                good.append((name, m.score))
            else:
                weak.append((name, m.score))

        report = [
            f"当前画面 {rect.width}x{rect.height}",
            f"通过 {len(good)} / 偏低 {len(weak)} / 未找到 {len(missing)}",
            "",
        ]
        if good:
            report.append("— 通过 —")
            report += [f"  {n}  {s:.3f}" for n, s in sorted(good, key=lambda x: -x[1])]
        if weak:
            report.append("")
            report.append("— 分数低于阈值（建议调低阈值或重新采集）—")
            report += [f"  {n}  {s:.3f}" for n, s in sorted(weak, key=lambda x: -x[1])]
        if missing:
            report.append("")
            report.append("— 当前画面完全没有（正常：不在该界面）—")
            report += [f"  {n}" for n in missing[:30]]

        text = "\n".join(report)
        print(text)
        messagebox.showinfo("模板验证结果", text[:3000])
        self._set_status(f"验证完成：通过 {len(good)}，偏低 {len(weak)}，未找到 {len(missing)}")

    def open_dir(self) -> None:
        d = self.root_dir / self.profile
        d.mkdir(parents=True, exist_ok=True)
        import os

        os.startfile(str(d))  # noqa: S606

    # ------------------------------------------------------------------ #

    def run(self) -> int:
        self.root.mainloop()
        try:
            self.grabber.close()
        except Exception:
            pass
        return 0


def run_capture_ui(cfg: Config) -> int:
    try:
        app = CaptureApp(cfg)
    except Exception as exc:  # noqa: BLE001
        print(f"[capture] 启动失败: {exc}")
        return 1
    return app.run()


if __name__ == "__main__":  # pragma: no cover
    from e7bot.config import Config as _C

    raise SystemExit(run_capture_ui(_C.load(Path(__file__).resolve().parents[1] / "config" / "default.toml")))
