import sqlite3
from datetime import date

import pandas as pd

from app.market.qfq_sync import ResumableQfqBuilder


def test_resumable_qfq_builder_publishes_stock_data_compatible_tables(tmp_path):
    target = tmp_path / "stock_data_qfq.db"
    pool_db = tmp_path / "stock_pool.db"
    pool_db.touch()
    universe = pd.DataFrame([
        {"security_code": "000001.SZ", "security_name": "平安银行"},
        {"security_code": "920002.BJ", "security_name": "万达轴承"},
    ])
    calls = []

    def fetch(codes, start, end):
        calls.append((codes, start, end))
        return pd.DataFrame([
            {
                "time": "2020-01-02", "thscode": code,
                "open": 10, "high": 11, "low": 9, "close": 10.5,
                "vwap": 10.2, "volume": 1000,
            }
            for code in codes
        ])

    builder = ResumableQfqBuilder(
        target_db=target,
        pool_db=pool_db,
        universe=universe,
        pool_id="all_a_non_st",
        pool_as_of="2026-07-31",
        fetch=fetch,
    )
    result = builder.build(
        start_date=date(2020, 1, 1), end_date=date(2020, 1, 2), batch_size=2
    )

    assert result["published"] is True
    assert calls[0][0] == ["000001.SZ", "920002.BJ"]
    with sqlite3.connect(target) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(stock_920002_BJ)")]
        assert columns == ["time", "open", "high", "low", "close", "vwap", "volume"]
        assert conn.execute("SELECT COUNT(*) FROM stock_920002_BJ").fetchone()[0] == 1
        metadata = conn.execute(
            "SELECT adjustment, ifind_params, universe_name, security_count "
            "FROM price_database_metadata"
        ).fetchone()
        assert metadata == ("forward", "CPS:2", "all_a_non_st", 2)


def test_resumable_qfq_builder_drops_blank_rows_but_keeps_suspensions(tmp_path):
    target = tmp_path / "stock_data_qfq.db"
    pool_db = tmp_path / "stock_pool.db"
    pool_db.touch()
    universe = pd.DataFrame([
        {"security_code": "000001.SZ", "security_name": "平安银行"},
    ])

    def fetch(codes, start, end):
        return pd.DataFrame([
            {
                "time": "2020-01-01", "thscode": "000001.SZ",
                "open": None, "high": None, "low": None, "close": None,
                "vwap": None, "volume": None,
            },
            {
                "time": "2020-01-02", "thscode": "000001.SZ",
                "open": 10, "high": 10, "low": 10, "close": 10,
                "vwap": None, "volume": None,
            },
        ])

    result = ResumableQfqBuilder(
        target_db=target, pool_db=pool_db, universe=universe,
        pool_id="all_a_non_st", pool_as_of="2026-07-31", fetch=fetch,
    ).build(start_date=date(2020, 1, 1), end_date=date(2020, 1, 2), batch_size=1)

    assert result["published"] is True
    with sqlite3.connect(target) as conn:
        rows = conn.execute(
            "SELECT time,close,vwap,volume FROM stock_000001_SZ ORDER BY time"
        ).fetchall()
    assert rows == [("2020-01-02", 10.0, None, None)]


def test_resumable_qfq_builder_keeps_partial_work_and_resumes(tmp_path):
    target = tmp_path / "stock_data_qfq.db"
    pool_db = tmp_path / "stock_pool.db"
    pool_db.touch()
    universe = pd.DataFrame([
        {"security_code": "000001.SZ", "security_name": "平安银行"},
        {"security_code": "920002.BJ", "security_name": "万达轴承"},
    ])
    failed_once = {"value": False}

    def fetch(codes, start, end):
        if "920002.BJ" in codes and not failed_once["value"]:
            failed_once["value"] = True
            raise RuntimeError("temporary")
        return pd.DataFrame([
            {
                "time": "2020-01-02", "thscode": code,
                "open": 10, "high": 11, "low": 9, "close": 10.5,
                "vwap": 10.2, "volume": 1000,
            }
            for code in codes
        ])

    builder = ResumableQfqBuilder(
        target_db=target,
        pool_db=pool_db,
        universe=universe,
        pool_id="all_a_non_st",
        pool_as_of="2026-07-31",
        fetch=fetch,
    )
    partial = builder.build(
        start_date=date(2020, 1, 1), end_date=date(2020, 1, 2),
        batch_size=1, max_failures=1,
    )
    completed = builder.build(
        start_date=date(2020, 1, 1), end_date=date(2020, 1, 2),
        batch_size=1, max_failures=1,
    )

    assert partial["status"] == "partial"
    assert builder.work_db.exists() is False
    assert completed["published"] is True
    with sqlite3.connect(target) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='qfq_security_progress'"
        ).fetchone() is None


def test_resumable_qfq_builder_reconciles_excluded_pool_members(tmp_path):
    target = tmp_path / "stock_data_qfq.db"
    pool_db = tmp_path / "stock_pool.db"
    pool_db.touch()
    original = pd.DataFrame([
        {"security_code": "000001.SZ", "security_name": "平安银行"},
        {"security_code": "920038.BJ", "security_name": "森合高科"},
    ])

    def first_fetch(codes, start, end):
        if "920038.BJ" in codes:
            raise RuntimeError("尚未上市")
        return pd.DataFrame([{
            "time": "2020-01-02", "thscode": "000001.SZ",
            "open": 10, "high": 11, "low": 9, "close": 10.5,
            "vwap": 10.2, "volume": 1000,
        }])

    first = ResumableQfqBuilder(
        target_db=target, pool_db=pool_db, universe=original,
        pool_id="all_a_non_st", pool_as_of="2026-07-31", fetch=first_fetch,
    )
    partial = first.build(
        start_date=date(2020, 1, 1), end_date=date(2020, 1, 2),
        batch_size=1, max_failures=1,
    )
    reduced = original.loc[original["security_code"] != "920038.BJ"].copy()
    resumed = ResumableQfqBuilder(
        target_db=target, pool_db=pool_db, universe=reduced,
        pool_id="all_a_non_st", pool_as_of="2026-07-31",
        fetch=lambda *_: (_ for _ in ()).throw(AssertionError("不应再次下载")),
    ).build(
        start_date=date(2020, 1, 1), end_date=date(2020, 1, 2), batch_size=1
    )

    assert partial["status"] == "partial"
    assert resumed["published"] is True
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT COUNT(*) FROM price_universe_membership").fetchone()[0] == 1
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_920038_BJ'"
        ).fetchone() is None
