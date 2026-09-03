"""Real-time charting panel for the pre-market dashboard.

Maintains a rolling time-series of every numeric factor value seen at each
refresh and redraws a live matplotlib line chart over time. History is
persisted to ``data/live_history.json`` so charts survive an app restart.
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
ALL_LABEL = "ALL (normalized %)"


def _series_key(cat_name: str, label: str) -> str:
    return f"{cat_name} · {label}"


class LiveChartPanel(ttk.LabelFrame):
    """A collapsible section showing live, over-time plots of all data points."""

    def __init__(self, parent, title="REAL-TIME CHARTS (ALL DATA)"):
        super().__init__(parent, text=title, style="Section.TLabelframe")
        self.dpi = 100
        self.history: list[dict] = self._load_history()
        self._selected = tk.StringVar(value=ALL_LABEL)
        self._show_legend = tk.BooleanVar(value=True)
        self._series_names: list[str] = []

        if not HAVE_MPL:
            self._show_missing_notice()
            return

        self._build_controls()
        self._build_canvas()
        self._redraw()

    # --- persistence --------------------------------------------------------

    def _load_history(self) -> list[dict]:
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
            except Exception:
                pass
        return []

    def _save_history(self):
        try:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(self.history, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # --- widget construction ------------------------------------------------

    def _show_missing_notice(self):
        tip = tk.Label(self, text="matplotlib is required for live charts\n"
                                  "pip install matplotlib",
                       fg="#ff9800", bg="#16213e", font=("Segoe UI", 9))
        tip.pack(fill=tk.X, padx=4, pady=6)

    def _build_controls(self):
        controls = ttk.Frame(self, style="Card.TFrame")
        controls.pack(fill=tk.X, padx=3, pady=(3, 0))

        ttk.Label(controls, text="Series:", background=BG_DARK, foreground=FG_PRIMARY).pack(
            side=tk.LEFT, padx=(2, 4))
        self._series_combo = ttk.Combobox(controls, textvariable=self._selected,
                                          state="readonly", width=34)
        self._series_combo.pack(side=tk.LEFT, padx=(0, 6))
        self._series_combo.bind("<<ComboboxSelected>>", lambda e: self._redraw())

        legend_cb = tk.Checkbutton(controls, text="Legend", variable=self._show_legend,
                                   bg=BG_DARK, fg=FG_PRIMARY, selectcolor=BG_DARK,
                                   activebackground=BG_DARK, activeforeground=FG_PRIMARY,
                                   font=("Segoe UI", 8),
                                   command=self._redraw)
        legend_cb.pack(side=tk.LEFT, padx=(0, 6))

        clear_btn = tk.Button(controls, text="Clear", bg=BG_CARD, fg=FG_PRIMARY,
                              font=("Segoe UI", 8), bd=0, relief=tk.FLAT, padx=8, pady=2,
                              activebackground="#0f3460", activeforeground=FG_ACCENT,
                              command=self.clear_history)
        clear_btn.pack(side=tk.RIGHT)

        self._info_lbl = tk.Label(controls, text="", bg=BG_DARK, fg=FG_SECONDARY,
                                  font=("Segoe UI", 8))
        self._info_lbl.pack(side=tk.LEFT, padx=(6, 2))

    def _build_canvas(self):
        self._figure = Figure(
            figsize=(5.2, 3.0), dpi=self.dpi, facecolor="#16213e")
        self._ax = self._figure.add_subplot(111)
        self._ax.set_facecolor("#16213e")
        self._canvas = FigureCanvasTkAgg(self._figure, master=self)
        widget = self._canvas.get_tk_widget()
        widget.configure(bg="#16213e", highlightthickness=0)
        widget.pack(fill=tk.BOTH, expand=True, padx=3, pady=3)
        widget.bind("<Configure>", self._on_resize)

    def _on_resize(self, event):
        w, h = event.width, event.height
        if w > 20 and h > 20:
            self._figure.set_size_inches(w / self.dpi, h / self.dpi)
            self._redraw()

    # --- public API ---------------------------------------------------------

    def update_series(self, all_results: dict, timestamp_label: str = ""):
        """Append the latest numeric values for every factor and redraw."""
        if not all_results:
            return
        values = {}
        for cat_name, results in all_results.items():
            if not isinstance(results, dict):
                continue
            for r in results.values():
                num = extract_number(getattr(r, "value", None))
                if num is not None:
                    values[_series_key(cat_name, r.label)] = num
        if not values:
            return

        self.history.append({
            "t": time.time(),
            "label": timestamp_label,
            "values": values,
        })
        if len(self.history) > MAX_POINTS:
            self.history = self.history[-MAX_POINTS:]
        self._save_history()
        self._refresh_series_list()
        self._redraw()

    def clear_history(self):
        self.history = []
        self._save_history()
        self._refresh_series_list()
        self._redraw()

    def _refresh_series_list(self):
        names = set()
        for entry in self.history:
            names.update(entry.get("values", {}).keys())
        self._series_names = sorted(names)
        values = [ALL_LABEL] + self._series_names
        self._series_combo["values"] = values
        if self._selected.get() not in values:
            self._selected.set(ALL_LABEL)

    # --- plots --------------------------------------------------------------

    def _redraw(self):
        if not HAVE_MPL:
            return
        ax = self._ax
        ax.clear()
        ax.set_facecolor("#16213e")
        for spine in ax.spines.values():
            spine.set_color("#333355")
        ax.tick_params(colors=FG_SECONDARY)

        if not self.history:
            ax.text(0.5, 0.5, "No live data yet — press Refresh to start plotting",
                    transform=ax.transAxes, ha="center", va="center",
                    color=FG_SECONDARY, fontsize=9)
            self._format_axis("Time", "")
            self._canvas.draw_idle()
            return

        selected = self._selected.get()
        if selected == ALL_LABEL:
            self._plot_all(ax)
        else:
            self._plot_one(ax, selected)

        self._canvas.draw_idle()

    def _plot_all(self, ax):
        xs = [datetime.fromtimestamp(e["t"]) for e in self.history]
        base = self.history[0].get("values", {})
        ax.set_ylabel("% change from first sample", color=FG_SECONDARY)
        ax.set_title("All data points over time (normalized)", color=FG_ACCENT,
                     fontsize=9)
        series = [s for s in self._series_names if any(
            extract_number(e.get("values", {}).get(s)) is not None for e in self.history)]

        cmap = matplotlib.colormaps["tab20"]
        n = max(len(series), 1)
        plotted = []
        for i, name in enumerate(series):
            xs_i, ys_i = [], []
            base_val = None
            for e in self.history:
                v = e.get("values", {}).get(name)
                num = extract_number(v)
                if num is None:
                    continue
                if base_val is None:
                    base_val = num
                xs_i.append(datetime.fromtimestamp(e["t"]))
                if base_val == 0:
                    ys_i.append(0.0)
                else:
                    ys_i.append((num / base_val - 1.0) * 100.0)
            if len(xs_i) > 1:
                ax.plot(xs_i, ys_i, lw=1.1, alpha=0.9,
                        color=cmap(i % cmap.N), label=name)
                plotted.append(name)

        if self._show_legend.get() and plotted:
            ax.legend(loc="best", fontsize=6, framealpha=0.4,
                      facecolor="#16213e", edgecolor="#333355", ncol=2)

        self._format_axis("Refresh time", "% change")
        self._info_lbl.configure(text=f"{len(self.history)} points · {len(plotted)} series")

    def _plot_one(self, ax, name):
        xs_i, ys_i = [], []
        for e in self.history:
            v = e.get("values", {}).get(name)
            num = extract_number(v)
            if num is None:
                continue
            xs_i.append(datetime.fromtimestamp(e["t"]))
            ys_i.append(num)
        ax.plot(xs_i, ys_i, lw=1.8, color=FG_ACCENT)
        if ys_i:
            lo, hi = min(ys_i), max(ys_i)
            pad = (hi - lo) * 0.15
            if pad <= 0:
                pad = abs(hi) * 0.05 or 1.0
            ax.set_ylim(lo - pad, hi + pad)
            if len(xs_i) > 1:
                ax.fill_between(xs_i, ys_i, lo - pad, alpha=0.12, color=FG_ACCENT)
            ax.annotate(f"{ys_i[-1]:,.2f}", xy=(xs_i[-1], ys_i[-1]),
                        xytext=(4, 4), textcoords="offset points",
                        color=FG_PRIMARY, fontsize=8)
        ax.set_title(name, color=FG_ACCENT, fontsize=9)
        self._format_axis("Refresh time", "Value")
        self._info_lbl.configure(text=f"{len(xs_i)} points")

    def _format_axis(self, xlabel: str, ylabel: str):
        ax = self._ax
        ax.set_xlabel(xlabel, color=FG_SECONDARY, fontsize=8)
        ax.set_ylabel(ylabel, color=FG_SECONDARY, fontsize=8)
        locator = mdates.AutoDateLocator()
        formatter = mdates.ConciseDateFormatter(locator)
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(formatter)
        for lbl in ax.get_xticklabels():
            lbl.set_rotation(20)
            lbl.set_fontsize(7)
        for lbl in ax.get_yticklabels():
            lbl.set_fontsize(7)
        ax.grid(True, color="#333355", linewidth=0.5, alpha=0.6)
        self._figure.tight_layout()
