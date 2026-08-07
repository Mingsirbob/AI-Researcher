from __future__ import annotations

import asyncio
from datetime import datetime, time
from typing import Awaitable, Callable


AccountRunner = Callable[[dict, str], Awaitable[dict]]


class PaperDailyScheduler:
    """Runs deployed paper strategies once after their configured daily time."""

    def __init__(self, paper_service, ifind_service) -> None:
        self.paper_service = paper_service
        self.ifind_service = ifind_service
        self.last_result: dict = {"status": "idle"}
        self._completed: set[tuple[str, str]] = set()
        self._non_trading_dates: set[str] = set()

    @staticmethod
    def _run_time(strategy: dict) -> time:
        value = str((strategy.get("config") or {}).get("run_time", "18:10"))
        hours, minutes = (int(item) for item in value.split(":"))
        return time(hours, minutes)

    def eligible_accounts(self, now: datetime) -> list[dict]:
        eligible = []
        for account in self.paper_service.list_accounts():
            config = account["strategy"].get("config") or {}
            if account.get("strategy_deployment") is None:
                continue
            if not config.get("auto_run") or config.get("schedule") != "daily":
                continue
            if now.time().replace(tzinfo=None) < self._run_time(account["strategy"]):
                continue
            eligible.append(account)
        return eligible

    async def run_once(self, now: datetime, runner: AccountRunner) -> dict:
        trading_date = now.date().isoformat()
        accounts = self.eligible_accounts(now)
        pending = [
            account for account in accounts
            if (trading_date, account["account_id"]) not in self._completed
        ]
        if not pending:
            self.last_result = {
                "status": "waiting" if not accounts else "up_to_date",
                "trading_date": trading_date,
                "account_count": len(accounts),
            }
            return self.last_result
        if trading_date in self._non_trading_dates:
            return self.last_result
        is_trading_day = await asyncio.to_thread(
            self.ifind_service.is_trading_day, now.date()
        )
        if not is_trading_day:
            self._non_trading_dates.add(trading_date)
            self.last_result = {
                "status": "skipped_non_trading_day",
                "trading_date": trading_date,
                "account_count": len(pending),
            }
            return self.last_result

        items = []
        for account in pending:
            try:
                result = await runner(account, trading_date)
                status = result.get("status", "completed")
                items.append({
                    "account_id": account["account_id"],
                    "status": status,
                    "result": result,
                })
                if status == "completed":
                    self._completed.add((trading_date, account["account_id"]))
            except Exception as exc:
                items.append({
                    "account_id": account["account_id"],
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                })
        failed = sum(item["status"] == "failed" for item in items)
        self.last_result = {
            "status": "completed" if failed == 0 else "partial",
            "trading_date": trading_date,
            "account_count": len(items),
            "failed_count": failed,
            "items": items,
        }
        return self.last_result
