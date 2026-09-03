import tkinter as tk
from tkinter import ttk
import json
import os
import re

from config import FACTORS_JSON, FACTORS_FILE
from agents.base_agent import FactorResult
from utils.helpers import is_market_open
from gui.live_charts import LiveChartPanel
from gui.widgets import (
    CategorySection, SentimentMeter, StatusBar,
    BG_DARK, BG_CARD, FG_PRIMARY, FG_SECONDARY, FG_ACCENT,
)


def parse_factors_txt(filepath: str) -> list[dict]:
    categories = []
    current_cat = None
    item_id = 0
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip()
            if not line:
                continue
            if line.startswith("# ") and not line.startswith("## "):
                continue
            if line.startswith("## "):
                current_cat = {
                    "name": line[3:].strip(),
                    "items": [],
                }
                categories.append(current_cat)
                continue
            if current_cat is not None and line.startswith("- [ ]"):
                content = line[5:].strip()
                bracket_match = re.search(r"\[(.*?)\]", content)
                details = bracket_match.group(1) if bracket_match else ""
                label = re.sub(r"\s*\[.*?\]", "", content).strip()
                item_id += 1
                fid = f"factor_{item_id}"
                current_cat["items"].append({
                    "id": fid,
                    "label": label,
                    "details": details,
                })
    return categories


GLOBAL_MARKETS_GROUPING = [
    ("US Indices Overnight", ["dow", "sp500", "nasdaq"]),
    ("Asian Markets This Morning", ["nikkei", "hang_seng", "shanghai"]),
    ("Global Risk Mood", ["recession_fears"]),
    ("SGX / GIFT Nifty Futures", ["sgx_nifty"]),
    ("US Fed Rate Decision", ["fed_decision"]),
    ("Global Currency Interventions", ["currency_interventions"]),
]


def _merge_results(results: list, label: str = "") -> FactorResult:
    if len(results) == 1 and not label:
        return results[0]
    if not results:
        return FactorResult(label=label or "Unavailable", value="N/A", status="error")
    first = results[0]
    parts = []
    for r in results:
        name = r.label.split("(")[0].strip()
        parts.append(f"{name}: {r.value} ({r.change})" if r.change else f"{name}: {r.value}")
    directions = [r.direction for r in results]
    bull = directions.count("bullish")
    bear = directions.count("bearish")
    direction = "bullish" if bull > bear else "bearish" if bear > bull else "neutral"
    status = "ok" if all(r.status == "ok" for r in results) else "warning"
    details = " | ".join(r.details for r in results if r.details)
    return FactorResult(
        label=label or first.label,
        value="; ".join(parts),
        direction=direction,
        status=status,
        details=details,
        source=first.source,
        last_updated=first.last_updated,
    )


def _map_results_to_items(cat_name: str, items: list, results: dict) -> dict:
    mapped = {}
    if cat_name == "4. GLOBAL MARKETS":
        for i, (label, keys) in enumerate(GLOBAL_MARKETS_GROUPING):
            if i < len(items):
                merged = _merge_results([results[k] for k in keys if k in results], label=label)
                if merged.status == "error" and not any(k in results for k in keys):
                    merged = FactorResult(label=label, value="N/A", status="error",
                                          details="Agent returned no data")
                mapped[items[i]["id"]] = merged
        return mapped
    result_keys = list(results.keys())
    for i, item in enumerate(items):
        if i < len(result_keys):
            mapped[item["id"]] = results[result_keys[i]]
        else:
            mapped[item["id"]] = FactorResult(
                label=item["label"], value="N/A", status="error",
                details="Agent returned no data for this factor")
    return mapped


class Dashboard:
    def __init__(self, root):
        self.root = root
        self.root.title("Pre-Market Factor Dashboard")
        self.root.geometry("520x800")
        self.root.configure(bg=BG_DARK)
        self.root.minsize(480, 700)

        self._setup_theme()
        self.categories = parse_factors_txt(FACTORS_FILE)
        self.sections: dict[str, CategorySection] = {}
        self.all_results: dict[str, dict] = {}
        self._build_ui()

    def _setup_theme(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background=BG_DARK, foreground=FG_PRIMARY,
                        fieldbackground=BG_DARK)
        style.configure("Card.TFrame", background=BG_CARD)
        style.configure("Section.TLabelframe", background=BG_DARK, borderwidth=1,
                        relief="solid")
        style.configure("Status.TFrame", background=BG_CARD)

    def _build_ui(self):
        banner = tk.Frame(self.root, bg=BG_DARK, height=90)
        banner.pack(fill=tk.X, padx=8, pady=(8, 4))
        banner.pack_propagate(False)

        title = tk.Label(banner, text="PRE-MARKET FACTOR DASHBOARD",
                         fg=FG_ACCENT, bg=BG_DARK,
                         font=("Segoe UI", 14, "bold"))
        title.pack(anchor=tk.W)

        self._time_lbl = tk.Label(banner, text="Last updated: --",
                                  fg=FG_SECONDARY, bg=BG_DARK,
                                  font=("Segoe UI", 8))
        self._time_lbl.pack(anchor=tk.W)

        self._market_status = tk.Label(banner, text="", fg=FG_SECONDARY, bg=BG_DARK,
                                       font=("Segoe UI", 8))
        self._market_status.pack(anchor=tk.W)

        top_row = tk.Frame(banner, bg=BG_DARK)
        top_row.pack(fill=tk.X, pady=(4, 0))
        self._sentiment_meter = SentimentMeter(top_row, width=300, height=22)
        self._sentiment_meter.pack(side=tk.LEFT, padx=(0, 8))

        self._export_btn = tk.Button(top_row, text="Export CSV",
                                     bg=BG_CARD, fg=FG_PRIMARY,
                                     font=("Segoe UI", 8),
                                     bd=0, relief=tk.FLAT, padx=8, pady=3,
                                     activebackground="#0f3460",
                                     activeforeground=FG_ACCENT,
                                     command=self.export_csv)
        self._export_btn.pack(side=tk.RIGHT, padx=(6, 0))

        self._refresh_btn = tk.Button(top_row, text="⟳ Refresh All",
                                      bg=BG_CARD, fg=FG_PRIMARY,
                                      font=("Segoe UI", 9, "bold"),
                                      bd=0, relief=tk.FLAT, padx=12, pady=3,
                                      activebackground="#0f3460",
                                      activeforeground=FG_ACCENT)
        self._refresh_btn.pack(side=tk.RIGHT)

        sep = tk.Frame(self.root, bg="#333355", height=1)
        sep.pack(fill=tk.X, padx=8)

        canvas_frame = tk.Frame(self.root, bg=BG_DARK)
        canvas_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 0))

        self._canvas = tk.Canvas(canvas_frame, bg=BG_DARK, highlightthickness=0,
                                 bd=0)
        scrollbar = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL,
                                  command=self._canvas.yview)
        self._scroll_frame = tk.Frame(self._canvas, bg=BG_DARK)

        self._scroll_frame.bind("<Configure>",
                                lambda e: self._canvas.configure(
                                    scrollregion=self._canvas.bbox("all")))
        self._canvas.create_window((0, 0), window=self._scroll_frame,
                                   anchor=tk.NW, tags="inner")
        self._canvas.configure(yscrollcommand=scrollbar.set)

        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self._canvas.bind("<Enter>", self._bind_mousewheel)
        self._canvas.bind("<Leave>", self._unbind_mousewheel)

        self._status_bar = StatusBar(self.root)
        self._status_bar.pack(fill=tk.X, padx=8, pady=(4, 6))

        self._build_sections()

    def _bind_mousewheel(self, event):
        self._canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_mousewheel(self, event):
        self._canvas.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event):
        self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _build_sections(self):
        placeholder = FactorResult(label="Loading...", value="--", direction="neutral",
                                   status="ok")

        # Real-time chart of all data points, plotted at every refresh.
        self._live_chart = LiveChartPanel(self._scroll_frame,
                                          "REAL-TIME CHARTS (ALL DATA)")
        self._live_chart.pack(fill=tk.X, pady=(0, 4), padx=2)

        for cat in self.categories:
            items = [(item["id"], placeholder) for item in cat["items"]]
            section = CategorySection(self._scroll_frame, cat["name"], items)
            section.pack(fill=tk.X, pady=4, padx=2)
            self.sections[cat["name"]] = section

        load_btn = tk.Button(self._scroll_frame, text="Load Checklist",
                             bg=BG_CARD, fg=FG_PRIMARY,
                             font=("Segoe UI", 9), bd=0, relief=tk.FLAT,
                             activebackground="#0f3460",
                             activeforeground=FG_ACCENT,
                             command=self._load_check_states)
        load_btn.pack(pady=6)

    def set_on_refresh(self, callback):
        self._refresh_btn.configure(command=callback)
        self._on_refresh_callback = callback

    def start_auto_refresh(self, interval_minutes: int = 10,
                           off_hours_minutes: int = 60):
        self._auto_interval_ms = interval_minutes * 60 * 1000
        self._off_hours_interval_ms = off_hours_minutes * 60 * 1000
        self._schedule_auto_refresh()

    def _schedule_auto_refresh(self):
        try:
            self.root.after(self._auto_interval_ms, self._auto_refresh_tick)
        except Exception:
            pass

    def _auto_refresh_tick(self):
        if self._on_refresh_callback:
            if is_market_open():
                self._status_bar.set_text("Auto-refresh triggered (market hours)")
                self._on_refresh_callback()
                self._schedule_auto_refresh()
            else:
                self._status_bar.set_text(
                    f"Market closed — next auto-refresh in "
                    f"{self._off_hours_interval_ms // 60000} min (overnight US data)")
                self.root.after(self._off_hours_interval_ms, self._auto_refresh_tick)

    def export_csv(self):
        try:
            import csv
            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y%m%d_%H%M")
            path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                "data", f"report_{now}.csv")
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["Category", "Factor", "Value", "Change", "Direction",
                                 "Status", "Details", "Checked"])
                for cat_name, section in self.sections.items():
                    for fid, item in section.items.items():
                        r = item.factor_result
                        writer.writerow([
                            cat_name, r.label, r.value, r.change, r.direction,
                            r.status, r.details, "X" if item.checked.get() else "",
                        ])
            self._status_bar.set_text(f"Report exported: {path}")
        except Exception as e:
            self._status_bar.set_text(f"Export failed: {e}")

    def update_results(self, all_results: dict[str, dict[str, FactorResult]],
                       timestamp: str = "", market_open: bool = False):
        self.all_results = all_results
        self._time_lbl.configure(text=f"Last updated: {timestamp}" if timestamp else "")

        status_text = "⬤ MARKET OPEN" if market_open else "○ MARKET CLOSED"
        status_color = "#00c853" if market_open else "#757575"
        self._market_status.configure(text=status_text, fg=status_color)

        for cat in self.categories:
            cat_name = cat["name"]
            section = self.sections.get(cat_name)
            if section and cat_name in all_results:
                mapped = _map_results_to_items(cat_name, cat["items"], all_results[cat_name])
                section.update_results(mapped)

        self._update_sentiment()
        self._update_live_chart(all_results, timestamp)

    def _update_live_chart(self, all_results: dict, timestamp: str = ""):
        try:
            if hasattr(self, "_live_chart"):
                self._live_chart.update_series(all_results, timestamp)
        except Exception:
            pass

    def _update_sentiment(self):
        bullish = 0
        bearish = 0
        for cat_name, results in self.all_results.items():
            for r in results.values():
                if r.direction == "bullish":
                    bullish += 1
                elif r.direction == "bearish":
                    bearish += 1
        total = bullish + bearish
        if total == 0:
            self._sentiment_meter.set_value(50)
        else:
            self._sentiment_meter.set_value((bullish / total) * 100)

    def set_agent_progress(self, completed: int, total: int):
        self._status_bar.set_progress(completed, total)
        self._status_bar.set_text(f"Fetching data... {completed}/{total} agents")

    def set_ready(self, error_count: int = 0, total: int = 10):
        if error_count > 0:
            self._status_bar.set_text(f"Ready — {error_count} agent(s) had errors")
        else:
            self._status_bar.set_text("All agents completed")
        self._status_bar.set_progress(total, total)

    def set_error(self, msg: str = "", total: int = 10):
        self._status_bar.set_text(f"Error: {msg}")
        self._status_bar.set_progress(0, total)

    def _load_check_states(self):
        for cat_name, section in self.sections.items():
            section.load_check_states()
        self._status_bar.set_text("Checklist states loaded from saved file")
