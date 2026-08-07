import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from app.paper.scheduler import PaperDailyScheduler


class FakePaperService:
    def __init__(self, accounts):
        self.accounts = accounts

    def list_accounts(self):
        return self.accounts


class FakeCalendar:
    def __init__(self, trading_day: bool):
        self.trading_day = trading_day
        self.calls = 0

    def is_trading_day(self, value):
        self.calls += 1
        return self.trading_day


def account(*, deployed=True, auto_run=True, run_time="18:10"):
    return {
        "account_id": "account-1",
        "strategy_deployment": {"deployment_id": "deployment-1"} if deployed else None,
        "strategy": {
            "config": {
                "schedule": "daily",
                "run_time": run_time,
                "auto_run": auto_run,
            }
        },
    }


def shanghai_time(hour: int, minute: int) -> datetime:
    return datetime(2026, 7, 31, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_scheduler_skips_non_trading_day_once():
    calendar = FakeCalendar(False)
    scheduler = PaperDailyScheduler(FakePaperService([account()]), calendar)
    calls = []

    async def runner(item, as_of):
        calls.append((item, as_of))
        return {"status": "completed"}

    first = asyncio.run(scheduler.run_once(shanghai_time(18, 20), runner))
    second = asyncio.run(scheduler.run_once(shanghai_time(19, 20), runner))

    assert first["status"] == "skipped_non_trading_day"
    assert second == first
    assert calendar.calls == 1
    assert calls == []


def test_scheduler_runs_deployed_strategy_once_after_configured_time():
    calendar = FakeCalendar(True)
    scheduler = PaperDailyScheduler(FakePaperService([account()]), calendar)
    calls = []

    async def runner(item, as_of):
        calls.append((item["account_id"], as_of))
        return {"status": "completed", "run_id": "run-1"}

    waiting = asyncio.run(scheduler.run_once(shanghai_time(18, 0), runner))
    completed = asyncio.run(scheduler.run_once(shanghai_time(18, 20), runner))
    reused = asyncio.run(scheduler.run_once(shanghai_time(19, 0), runner))

    assert waiting["status"] == "waiting"
    assert completed["status"] == "completed"
    assert reused["status"] == "up_to_date"
    assert calls == [("account-1", "2026-07-31")]


def test_scheduler_ignores_legacy_account_without_deployment():
    calendar = FakeCalendar(True)
    scheduler = PaperDailyScheduler(
        FakePaperService([account(deployed=False)]), calendar
    )

    async def runner(item, as_of):
        raise AssertionError("legacy account must not run")

    result = asyncio.run(scheduler.run_once(shanghai_time(18, 20), runner))

    assert result["status"] == "waiting"
    assert calendar.calls == 0
