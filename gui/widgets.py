import tkinter as tk
from tkinter import ttk
import json
import os

from config import FACTORS_JSON
from agents.base_agent import FactorResult

DIRECTION_COLORS = {
    "bullish": "#00c853",
    "bearish": "#ff1744",
    "neutral": "#757575",
    "warning": "#ff9800",
    "error": "#ff1744",
    "ok": "#00c853",
    "manual": "#ff9800",
}

BG_DARK = "#1a1a2e"
BG_CARD = "#16213e"
BG_CARD_ALT = "#0f3460"
FG_PRIMARY = "#e0e0e0"
FG_SECONDARY = "#a0a0a0"
FG_ACCENT = "#e94560"


def _direction_color(d: str) -> str:
    return DIRECTION_COLORS.get(d, "#757575")


class ChecklistItem(ttk.Frame):
    def __init__(self, parent, factor_id: str, factor_result: FactorResult, on_check_cb=None):
        super().__init__(parent, style="Card.TFrame")
        self.factor_id = factor_id
        self.factor_result = factor_result
        self.on_check_cb = on_check_cb
        self.checked = tk.BooleanVar(value=False)
        self._build()

    def _build(self):
        color = _direction_color(self.factor_result.direction)
        if self.factor_result.status in ("error", "warning"):
            color = _direction_color(self.factor_result.status)

        self.indicator = tk.Frame(self, bg=color, width=4)
        self.indicator.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 6))

        content = ttk.Frame(self, style="Card.TFrame")
        content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4, pady=3)

        cb = tk.Checkbutton(
            content, variable=self.checked,
            command=self._on_check,
            bg=BG_CARD, fg=FG_PRIMARY, selectcolor=BG_CARD,
            activebackground=BG_CARD, activeforeground=FG_PRIMARY,
            font=("Segoe UI", 9),
        )
        cb.pack(side=tk.LEFT)

        label_text = self.factor_result.label
        lbl = tk.Label(content, text=label_text, fg=FG_PRIMARY, bg=BG_CARD,
                       font=("Segoe UI", 9, "bold"), anchor=tk.W, wraplength=180)
        lbl.pack(side=tk.LEFT, padx=(4, 8))

        value_text = (f"{self.factor_result.value} "
                      f"{self.factor_result.change}" if self.factor_result.change
                      else self.factor_result.value)
        val_lbl = tk.Label(content, text=value_text, fg=FG_SECONDARY, bg=BG_CARD,
                           font=("Segoe UI", 9), anchor=tk.W)
        val_lbl.pack(side=tk.LEFT, padx=(0, 8))

        # Flexible spacer so right-hand badges anchor to the card edge and the
        # card visually spans the full width on wide windows.
        spacer = tk.Frame(content, bg=BG_CARD)
        spacer.pack(side=tk.LEFT, fill=tk.X, expand=True)

        if self.factor_result.status == "manual":
            manual_badge = tk.Label(content, text="MANUAL", fg="#ff9800", bg=BG_CARD,
                                    font=("Segoe UI", 7, "bold"))
            manual_badge.pack(side=tk.RIGHT, padx=(0, 4))

        if self.factor_result.stale:
            stale_badge = tk.Label(content, text="STALE", fg="#ff1744", bg=BG_CARD,
                                   font=("Segoe UI", 7, "bold"))
            stale_badge.pack(side=tk.RIGHT, padx=(0, 4))

        dir_lbl = tk.Label(content, text=self.factor_result.direction.upper(),
                           fg=color, bg=BG_CARD,
                           font=("Segoe UI", 8, "bold"))
        dir_lbl.pack(side=tk.RIGHT)

        if self.factor_result.details:
            def enter(e):
                self.tooltip.geometry(f"+{e.x_root + 10}+{e.y_root + 10}")
                self.tooltip.deiconify()
                self.tooltip.lift()

            def leave(e):
                self.tooltip.withdraw()

            self.tooltip = tk.Toplevel(self)
            self.tooltip.wm_overrideredirect(True)
            self.tooltip.attributes("-topmost", True)
            self.tooltip.withdraw()
            tooltip_bg = "#2a2a4a"
            tk.Label(self.tooltip, text=self.factor_result.details,
                     bg=tooltip_bg, fg=FG_PRIMARY, font=("Segoe UI", 8),
                     wraplength=300, justify=tk.LEFT,
                     padx=8, pady=4).pack()
            self.bind("<Enter>", enter)
            self.bind("<Leave>", leave)
            lbl.bind("<Enter>", enter)
            lbl.bind("<Leave>", leave)
            cb.bind("<Enter>", enter)
            cb.bind("<Leave>", leave)

    def _on_check(self):
        if self.on_check_cb:
            self.on_check_cb(self.factor_id, self.checked.get())
        self.save_check_state()

    def save_check_state(self):
        try:
            data = {}
            if os.path.exists(FACTORS_JSON):
                with open(FACTORS_JSON, "r", encoding="utf-8") as f:
                    data = json.load(f)
            data.setdefault("checks", {})[self.factor_id] = self.checked.get()
            with open(FACTORS_JSON, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def load_check_state(self):
        try:
            if os.path.exists(FACTORS_JSON):
                with open(FACTORS_JSON, "r", encoding="utf-8") as f:
                    data = json.load(f)
                val = data.get("checks", {}).get(self.factor_id, False)
                self.checked.set(val)
        except Exception:
            pass

    def update_result(self, result: FactorResult):
        self.factor_result = result
        for w in self.winfo_children():
            w.destroy()
        self._build()
        self.load_check_state()


class CategorySection(ttk.LabelFrame):
    def __init__(self, parent, category_name: str, factor_items: list[tuple],
                 on_check_cb=None):
        super().__init__(parent, text="", style="Section.TLabelframe")
        self.category_name = category_name
        self.items: dict[str, ChecklistItem] = {}
        self.on_check_cb = on_check_cb
        self.collapsed = tk.BooleanVar(value=False)
        self._build_header()
        self._content_frame = ttk.Frame(self, style="Card.TFrame")
        self._content_frame.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        for fid, result in factor_items:
            item = ChecklistItem(self._content_frame, fid, result, on_check_cb=on_check_cb)
            item.pack(fill=tk.X, pady=1)
            self.items[fid] = item

    def _build_header(self):
        header = ttk.Frame(self, style="Card.TFrame")
        header.pack(fill=tk.X, padx=2, pady=(2, 0))

        toggle_btn = tk.Button(header, text="▾", bg=BG_DARK, fg=FG_PRIMARY,
                               font=("Segoe UI", 12), bd=0, relief=tk.FLAT,
                               activebackground=BG_CARD, activeforeground=FG_ACCENT,
                               command=self._toggle)
        toggle_btn.pack(side=tk.LEFT, padx=(4, 8))
        self._toggle_btn = toggle_btn

        title_lbl = tk.Label(header, text=self.category_name, fg=FG_ACCENT, bg=BG_DARK,
                             font=("Segoe UI", 11, "bold"), anchor=tk.W)
        title_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self._score_lbl = tk.Label(header, text="", fg=FG_SECONDARY, bg=BG_DARK,
                                   font=("Segoe UI", 9))
        self._score_lbl.pack(side=tk.RIGHT, padx=8)

    def _toggle(self):
        if self.collapsed.get():
            self._content_frame.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
            self._toggle_btn.configure(text="▾")
        else:
            self._content_frame.pack_forget()
            self._toggle_btn.configure(text="▸")
        self.collapsed.set(not self.collapsed.get())

    def update_results(self, results: dict[str, FactorResult]):
        for fid, result in results.items():
            if fid in self.items:
                self.items[fid].update_result(result)
        self._update_score()

    def _update_score(self):
        bullish = 0
        bearish = 0
        total = 0
        for item in self.items.values():
            r = item.factor_result
            if r.status == "error":
                continue
            total += 1
            if r.direction == "bullish":
                bullish += 1
            elif r.direction == "bearish":
                bearish += 1
        if total == 0:
            self._score_lbl.configure(text="")
            return
        bull_pct = bullish / total * 100
        color = "#00c853" if bull_pct > 60 else "#ff1744" if bull_pct < 40 else "#ff9800"
        self._score_lbl.configure(
            text=f"{bullish}B / {bearish}Br",
            fg=color,
        )

    def get_all_check_state(self) -> dict:
        return {fid: item.checked.get() for fid, item in self.items.items()}

    def load_check_states(self):
        for item in self.items.values():
            item.load_check_state()


class SentimentMeter(tk.Canvas):
    def __init__(self, parent, width=300, height=20):
        super().__init__(parent, width=width, height=height,
                         bg=BG_DARK, highlightthickness=0)
        self.width = width
        self.height = height

    def set_value(self, bullish_pct: float):
        self.delete("all")
        bar_width = self.width - 4
        bar_height = self.height - 8
        x0, y0 = 2, 4
        x1, y1 = x0 + bar_width, y0 + bar_height

        self.create_rectangle(x0, y0, x1, y1, fill="#333355", outline="")
        green_width = (bullish_pct / 100) * bar_width
        self.create_rectangle(x0, y0, x0 + green_width, y1,
                              fill="#00c853", outline="")

        mid_x = x0 + bar_width / 2
        self.create_line(mid_x, y0, mid_x, y1, fill="#757575", width=1)

        self.create_text(x0 + bar_width / 2, y0 + bar_height / 2,
                         text=f"{bullish_pct:.0f}% Bullish",
                         fill=FG_PRIMARY, font=("Segoe UI", 9, "bold"))


class StatusBar(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, style="Status.TFrame")
        self._label = tk.Label(self, text="Ready", fg=FG_SECONDARY, bg=BG_CARD,
                               font=("Segoe UI", 9), anchor=tk.W)
        self._label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8, pady=2)

        self._progress = ttk.Progressbar(self, mode="determinate", length=150)
        self._progress.pack(side=tk.RIGHT, padx=8, pady=4)

    def set_text(self, text: str):
        self._label.configure(text=text)

    def set_progress(self, value: int, maximum: int = 10):
        self._progress["maximum"] = maximum
        self._progress["value"] = value
