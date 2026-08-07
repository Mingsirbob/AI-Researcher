from datetime import date

import pandas as pd

from app.market.stock_pool import MAJOR_INDEX_BENCHMARKS, StockPoolStore
from scripts.backfill_index_prices import SOURCE, backfill


class FakeIndexData:
    def __init__(self, failed_code: str | None = None):
        self.failed_code = failed_code
        self.calls: list[dict] = []

    def get_daily_prices(
        self,
        codes,
        start,
        end,
        *,
        adjustment,
        max_attempts,
    ) -> pd.DataFrame:
        code = codes[0]
        self.calls.append({
            "codes": codes,
            "start": start,
            "end": end,
            "adjustment": adjustment,
            "max_attempts": max_attempts,
        })
        if code == self.failed_code:
            raise RuntimeError("provider failed")
        return pd.DataFrame([{
            "time": "2026-07-31",
            "thscode": code,
            "open": 100.0,
            "high": 102.0,
            "low": 99.0,
            "close": 101.0,
            "vwap": 100.5,
            "volume": 1000.0,
        }])


def test_backfill_writes_all_indices_as_unadjusted_prices(tmp_path):
    store = StockPoolStore(tmp_path / "stock_pool.db")
    data = FakeIndexData()

    results = backfill(
        data,
        store,
        index_codes=list(MAJOR_INDEX_BENCHMARKS),
        start_date=date(2020, 1, 1),
        end_date=date(2026, 7, 31),
    )

    assert all(item["status"] == "success" for item in results)
    assert all(call["adjustment"] == "unadjusted" for call in data.calls)
    assert all(call["codes"] and len(call["codes"]) == 1 for call in data.calls)
    for code in MAJOR_INDEX_BENCHMARKS:
        rows = store.index_prices(code)
        assert len(rows) == 1
        assert rows[0]["source"] == SOURCE


def test_backfill_keeps_successful_indices_when_one_fails(tmp_path):
    codes = list(MAJOR_INDEX_BENCHMARKS)
    failed_code = codes[1]
    store = StockPoolStore(tmp_path / "stock_pool.db")

    results = backfill(
        FakeIndexData(failed_code),
        store,
        index_codes=codes,
        start_date=date(2020, 1, 1),
        end_date=date(2026, 7, 31),
    )

    assert sum(item["status"] == "failed" for item in results) == 1
    assert store.index_prices(failed_code) == []
    assert all(store.index_prices(code) for code in codes if code != failed_code)
