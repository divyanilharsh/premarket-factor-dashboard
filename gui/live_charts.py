"""Per-category real-time charts for the pre-market dashboard.

Instead of one unreadable "all factors" chart, every data section gets its own
compact live line chart directly beneath it, plotting that category's numeric
factors over time (normalised to % change from the first sample so different
scales are comparable). History is persisted to ``data/live_history.json`` so
the trajectories survive an app restart and continue across refresh cycles.
"""

import json
import os
import time
from datetime import datetime

import tkinter as tk
from tkinter import ttk

from config import DATA_DIR
from utils.helpers import extract_number
from gui.widgets import (BG_DARK, BG_CARD, FG_PRIMARY, FG_SECONDARY, FG_ACCENT)

# Imported lazily so the dashboard still boots if matplotlib is missing.
try:
    import matplotlib
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    import matplotlib.dates as mdates
    HAVE_MPL = True
except Exception:  # pragma: no cover
    HAVE_MPL = False

HISTORY_FILE = os.path.join(DATA_DIR, "live_history.json")
MAX_POINTS = 300
CHART_HEIGHT_IN = 1.25


class LiveHistoryStore:
    """Append-only, state-persistent time-series buffer, grouped by category."""

    def __init__(self, file: str = HISTORY_FILE, max_points: int = MAX_POINTS):
        self.file = file
        self.max_points = max_points
        self.history: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if os.path.exists(self.file):
            try:
                with open(self.file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
            except Exception:
                pass
        return []

    def _save(self):
        try:
            with open(self.file, "w", encoding="utf-8") as f:
                json.dump(self.history, f, ensure_ascii=False)
        except Exception:
            pass

    def append(self, all_results: dict, ts_label: str = ""):
        """Group every numeric factor value by its category and append a point."""
        cats = {}
        for cat_name, results in all_results.items():
            if not isinstance(results, dict):
                continue
            vals = {}
            for r in results.values():
                num = extract_number(getattr(r, "value", None))
                if num is not None:
                    vals[r.label] = num
            if vals:
                cats[cat_name] = vals
        if not cats:
            return
        self.history.append({"t": time.time(), "label": ts_label, "cats": cats})
        if len(self.history) > self.max_points:
            self.history = self.history[-self.max_points:]
        self._save()

    def series_for(self, cat_name: str) -> list:
        """Return [(epoch, {label: value})] for the given category."""
        out = []
        for e in self.history:
            cat = e.get("cats", {}).get(cat_name)
            if cat:
                out.append((e["t"], cat))
        return out

    def point_count(self) -> int:
        return len(self.history)

    def clear(self):
        self.history = []
        self._save()


class CategorySparkChart(ttk.LabelFrame):
    """A compact live line chart for a single category, placed under its section."""

    def __init__(self, parent, category_name: str, store: LiveHistoryStore):
        title = f"Live chart · {category_name}"
        super().__init__(parent, text=title, style="Section.TLabelframe")
        self.category_name = category_name
        self.store = store
        self.dpi = 100
        self._resize_job = None

        if not HAVE_MPL:
            tip = tk.Label(self, text="matplotlib required for charts",
                           fg="#ff9800", bg=BG_CARD, font=("Segoe UI", 8))
            tip.pack(fill=tk.X, padx=4, pady=4)
            return

        self._build()
        self.redraw()

    def _build(self):
        self._figure = Figure(figsize=(5.0, CHART_HEIGHT_IN), dpi=self.dpi,
                              facecolor=BG_CARD)
        self._ax = self._figure.add_subplot(111)
        self._ax.set_facecolor(BG_CARD)
        self._canvas = FigureCanvasTkAgg(self._figure, master=self)
        widget = self._canvas.get_tk_widget()
        widget.configure(bg=BG_CARD, highlightthickness=0)
        widget.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        widget.bind("<Configure>", self._on_resize)

    def _on_resize(self, event):
        if event.width < 40:
            return
        if self._resize_job is not None:
            try:
                self.after_cancel(self._resize_job)
            except Exception:
                pass
        self._resize_job = self.after(
            60, lambda: self._apply_size(event.width))

    def _apply_size(self, width_px: int):
        self._figure.set_size_inches(width_px / self.dpi, CHART_HEIGHT_IN)
        self.redraw()

    def redraw(self):
        if not HAVE_MPL:
            return
        ax = self._ax
        ax.clear()
        ax.set_facecolor(BG_CARD)
        for spine in ax.spines.values():
            spine.set_color("#333355")
        ax.tick_params(colors=FG_SECONDARY)

        timeline = self.store.series_for(self.category_name)
        if not timeline:
            ax.text(0.5, 0.5, "refresh to start tracking",
                    transform=ax.transAxes, ha="center", va="center",
                    color=FG_SECONDARY, fontsize=7)
            self._canvas.draw_idle()
            return

        labels = set()
        for _, cat in timeline:
            labels.update(cat.keys())
        labels = sorted(labels)
        cmap = matplotlib.colormaps["tab10"]

        plotted = []
        for i, lbl in enumerate(labels):
            base = None
            xs, ys = [], []
            for t, cat in timeline:
                v = cat.get(lbl)
                if v is None:
                    continue
                if base is None:
                    base = v
                xs.append(datetime.fromtimestamp(t))
                ys.append((v / base - 1.0) * 100.0 if base else 0.0)
            if len(xs) > 1:
                ax.plot(xs, ys, lw=1.1, color=cmap(i % cmap.N), label=lbl)
                plotted.append(lbl)

        if plotted and len(plotted) <= 5:
            ax.legend(loc="best", fontsize=6, framealpha=0.4,
                      facecolor=BG_CARD, edgecolor="#333355", ncol=1)
        ax.set_title(f"{self.category_name} — % change over time",
                     color=FG_ACCENT, fontsize=7)

        locator = mdates.AutoDateLocator()
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        for lbl in ax.get_xticklabels():
            lbl.set_rotation(20)
            lbl.set_fontsize(6)
        for lbl in ax.get_yticklabels():
            lbl.set_fontsize(6)
        ax.set_ylabel("% chg", color=FG_SECONDARY, fontsize=6)
        ax.grid(True, color="#333355", linewidth=0.4, alpha=0.5)
        self._figure.tight_layout()
        self._canvas.draw_idle()
