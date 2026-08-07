from datetime import date

import pandas as pd

from app.market.stock_pool import StockPoolStore, index_price_table


def test_stock_pool_saves_and_resolves_latest_snapshot(tmp_path):
    store = StockPoolStore(tmp_path / "stock_pool.db")
    store.register({
        "pool_id": "csi300",
        "name": "沪深300",
        "pool_type": "index",
        "index_code": "000300.SH",
        "source": "iFinD THS_WCQuery",
        "query_text": "沪深300成分股",
    })
    store.save_snapshot(
        "csi300",
        date(2026, 7, 30),
        pd.DataFrame([
            {"security_code": "000001.SZ", "security_name": "平安银行", "weight": None},
            {"security_code": "600000.SH", "security_name": "浦发银行", "weight": None},
        ]),
    )

    snapshot = store.resolve("csi300", date(2026, 7, 31))

    assert snapshot["as_of"] == "2026-07-30"
    assert snapshot["member_count"] == 2
    assert snapshot["members"][0]["security_code"] == "000001.SZ"


def test_failed_refresh_keeps_existing_snapshot(tmp_path):
    store = StockPoolStore(tmp_path / "stock_pool.db")
    store.register({
        "pool_id": "star50", "name": "科创50", "pool_type": "index",
        "index_code": "000688.SH", "source": "iFinD THS_WCQuery",
        "query_text": "科创50成分股",
    })
    store.save_snapshot(
        "star50",
        date(2026, 7, 30),
        pd.DataFrame([{"security_code": "688001.SH", "security_name": "华兴源创"}]),
    )

    store.record_failure("star50", RuntimeError("quota exceeded"))

    assert store.resolve("star50", date(2026, 7, 31))["member_count"] == 1
    assert "quota exceeded" in store.list_pools()[0]["last_error"]


def test_excluded_members_are_removed_and_not_reintroduced(tmp_path):
    store = StockPoolStore(tmp_path / "stock_pool.db")
    store.register({
        "pool_id": "all_a_non_st", "name": "全部A股（非ST）", "pool_type": "market",
        "source": "iFinD THS_WCQuery", "query_text": "全部A股（非ST）",
    })
    members = pd.DataFrame([
        {"security_code": "000001.SZ", "security_name": "平安银行"},
        {"security_code": "920038.BJ", "security_name": "森合高科"},
    ])
    store.save_snapshot("all_a_non_st", date(2026, 7, 31), members)

    excluded = store.exclude_members(
        "all_a_non_st", ["920038.BJ"], reason="尚未上市"
    )
    refreshed = store.save_snapshot("all_a_non_st", date(2026, 8, 1), members)

    assert excluded["removed_count"] == 1
    assert excluded["member_count"] == 1
    assert refreshed["member_count"] == 1
    assert store.resolve("all_a_non_st", date(2026, 8, 1))["members"] == [
        {"security_code": "000001.SZ", "security_name": "平安银行", "weight": None}
    ]


def test_index_prices_use_one_table_per_index_and_global_dates(tmp_path):
    store = StockPoolStore(tmp_path / "stock_pool.db")
    rows = [
        {
            "thscode": code,
            "time": "2026-07-31",
            "open": 100,
            "high": 102,
            "low": 99,
            "close": 101,
            "volume": 1000,
        }
        for code in ("000001.SH", "000300.SH", "399001.SZ", "399006.SZ", "000688.SH", "000510.CSI")
    ]

    result = store.save_index_prices(rows)
    store.save_index_prices([{**rows[0], "close": 102}])

    assert result["rows_written"] == 6
    assert store.index_latest_date() == "2026-07-31"
    assert store.index_prices("000001.SH")[0]["close"] == 102
    with store.connect() as conn:
        assert conn.execute(
            f'SELECT COUNT(*) FROM "{index_price_table("000001.SH")}"'
        ).fetchone()[0] == 1
