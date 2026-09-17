"""Process-safe shared hard cap for subsequent model and review calls."""
from pathlib import Path
import sqlite3
import time

from agent.evaluation.context_history_strategies_v1_20260905.subscription import SubscriptionClient

ROOT = Path("D:/agent-experiments/memory-natural-v1-20260907")


class BudgetClient(SubscriptionClient):
    async def create(self, **request):
        path = str(self.output / f"call-{len(self.calls)+1:03d}")
        # Include pre-broker pilot/component calls, and reservations whose
        # native subprocess has not yet created a result or even a directory.
        existing = list(ROOT.glob("*/model_calls/call-*"))
        with sqlite3.connect(ROOT / "campaign-budget.sqlite", timeout=10) as database:
            database.execute("CREATE TABLE IF NOT EXISTS reservations(path TEXT PRIMARY KEY, at REAL NOT NULL)")
            database.execute("BEGIN IMMEDIATE")
            reserved = {row[0] for row in database.execute("SELECT path FROM reservations")}
            count = len(reserved | {str(item) for item in existing})
            started = min([item.stat().st_mtime for item in existing] + [time.time()])
            if count >= 400 or time.time()-started >= 4*3600:
                raise RuntimeError("first_edition_campaign_budget_exhausted")
            database.execute("INSERT INTO reservations VALUES (?, ?)", (path, time.time()))
        return await super().create(**request)
