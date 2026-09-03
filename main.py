import tkinter as tk
import queue
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from utils.helpers import format_timestamp, is_market_open
from agents.base_agent import BaseAgent, FactorResult
from gui.dashboard import Dashboard

AGENTS_MAP = {
    "1. CRUDE OIL": "agents.crude_agent:CrudeAgent",
    "2. CURRENCY": "agents.currency_agent:CurrencyAgent",
    "3. FOREIGN FLOWS": "agents.flows_agent:FlowsAgent",
    "4. GLOBAL MARKETS": "agents.global_markets_agent:GlobalMarketsAgent",
    "5. US BOND YIELDS / DOLLAR": "agents.bonds_agent:BondsAgent",
    "6. GEOPOLITICS": "agents.geopolitics_agent:GeopoliticsAgent",
    "7. MACRO DATA / POLICY": "agents.macro_agent:MacroAgent",
    "8. EARNINGS / CORPORATE NEWS": "agents.earnings_agent:EarningsAgent",
    "9. SECTOR SIGNALS": "agents.sectors_agent:SectorsAgent",
    "10. SENTIMENT / TECHNICAL": "agents.sentiment_agent:SentimentAgent",
    "11. OPTION CHAIN - NIFTY": "agents.option_chain_agent:OptionChainAgent",
}

AGENT_NAMES = {
    "1. CRUDE OIL": "crude",
    "2. CURRENCY": "currency",
    "3. FOREIGN FLOWS": "flows",
    "4. GLOBAL MARKETS": "global_markets",
    "5. US BOND YIELDS / DOLLAR": "bonds",
    "6. GEOPOLITICS": "geopolitics",
    "7. MACRO DATA / POLICY": "macro",
    "8. EARNINGS / CORPORATE NEWS": "earnings",
    "9. SECTOR SIGNALS": "sectors",
    "10. SENTIMENT / TECHNICAL": "sentiment",
    "11. OPTION CHAIN - NIFTY": "option_chain",
}

N_AGENTS = len(AGENTS_MAP)


def _import_agent(import_path: str):
    mod_path, cls_name = import_path.rsplit(":", 1)
    import importlib
    mod = importlib.import_module(mod_path)
    return getattr(mod, cls_name)


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.dashboard = Dashboard(self.root)
        self.dashboard.set_on_refresh(self.on_refresh)
        self._result_queue = queue.Queue()
        self._error_count = 0
        self._show_cached_on_startup()
        self._poll_queue()

    def _show_cached_on_startup(self):
        from agents.base_agent import BaseAgent
        cached = {}
        any_cached = False
        for cat_name, agent_name in AGENT_NAMES.items():
            results = BaseAgent.results_from_cache(agent_name)
            if results:
                cached[cat_name] = results
                any_cached = True
        if any_cached:
            self.dashboard.update_results(
                cached,
                timestamp="cached data",
                market_open=is_market_open(),
            )
            self.dashboard._status_bar.set_text("Showing cached data — press Refresh for latest")

    def on_refresh(self):
        if getattr(self, "_refreshing", False):
            self.dashboard._status_bar.set_text("Refresh already in progress...")
            return
        self._refreshing = True
        self.dashboard.set_agent_progress(0, N_AGENTS)
        self._error_count = 0
        t = threading.Thread(target=self._run_agents_safe, daemon=True)
        t.start()

    def _run_agents(self):
        agents = {}
        agent_instances = {}
        for cat_name, import_path in AGENTS_MAP.items():
            cls = _import_agent(import_path)
            instance = cls()
            agent_instances[cat_name] = instance

        crude_results = {}
        currency_results = {}
        global_results = {}

        all_instances = list(agent_instances.values())
        futures_map = {}
        with ThreadPoolExecutor(max_workers=10) as executor:
            for agent in all_instances:
                f = executor.submit(agent._fetch_with_retry)
                futures_map[f] = agent
            completed_count = 0
            try:
                for f in as_completed(futures_map, timeout=90):
                    agent = futures_map[f]
                    try:
                        agent.results = f.result()
                    except Exception as e:
                        agent.error = f"{type(e).__name__}: {e}"
                        traceback.print_exc()
                    completed_count += 1
                    self._result_queue.put(("progress", completed_count, agent))
            except TimeoutError:
                for f, agent in futures_map.items():
                    if not f.done():
                        agent.error = "Timeout after 90s"
                self._result_queue.put(("progress", N_AGENTS, all_instances[-1]))

        for cat_name, agent in agent_instances.items():
            if cat_name == "1. CRUDE OIL" and agent.results:
                crude_results = agent.results
            elif cat_name == "2. CURRENCY" and agent.results:
                currency_results = agent.results
            elif cat_name == "4. GLOBAL MARKETS" and agent.results:
                global_results = agent.results

        sectors_agent = agent_instances.get("9. SECTOR SIGNALS")
        if sectors_agent and hasattr(sectors_agent, "set_external_data"):
            sectors_agent.set_external_data(crude_results, currency_results, global_results)
            try:
                sectors_agent.results = sectors_agent._fetch_with_retry()
            except Exception as e:
                sectors_agent.error = f"{type(e).__name__}: {e}"

        final_results = {}
        error_count = 0
        for cat_name, agent in agent_instances.items():
            if agent.error:
                error_count += 1
            final_results[cat_name] = agent.results if agent.results else {}

        self._result_queue.put(("done", final_results, error_count))

    def _run_agents_safe(self):
        try:
            self._run_agents()
        except Exception as e:
            traceback.print_exc()
            self._result_queue.put(("done", {}, N_AGENTS))
            self._result_queue.put(("error", str(e)))

    def _poll_queue(self):
        try:
            while True:
                msg = self._result_queue.get_nowait()
                if msg[0] == "progress":
                    _, completed, agent = msg
                    self.dashboard.set_agent_progress(completed, N_AGENTS)
                elif msg[0] == "done":
                    _, results, error_count = msg
                    self._refreshing = False
                    self.dashboard.update_results(
                        results,
                        timestamp=format_timestamp(),
                        market_open=is_market_open(),
                    )
                    self.dashboard.set_ready(error_count, N_AGENTS)
                elif msg[0] == "error":
                    self._refreshing = False
                    self.dashboard.set_error(msg[1], N_AGENTS)
        except queue.Empty:
            pass
        self.root.after(500, self._poll_queue)

    def run(self):
        self.dashboard.start_auto_refresh(interval_minutes=10, off_hours_minutes=60)
        self.root.mainloop()


def main():
    app = App()
    app.on_refresh()
    app.run()


if __name__ == "__main__":
    main()
