import sqlite3
from datetime import date

import pandas as pd

from app.market.adjusted_data import AdjustedStockDataBuilder, partition_confirmed_market_jumps
from app.market.updater import QualityIssue, normalize_daily_frame


def create_source_db(path):
    with sqlite3.connect(path) as conn:
        for table in ("stock_000001_SZ", "stock_600000_SH"):
            conn.execute(
                f"""
                CREATE TABLE "{table}" (
                    time TEXT PRIMARY KEY,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    vwap REAL,
                    volume REAL
                )
                """
            )
            conn.execute(
                f'INSERT INTO "{table}" VALUES (?, ?, ?, ?, ?, ?, ?)',
                ("2020-01-02", 10.0, 11.0, 9.0, 10.5, 10.4, 1000.0),
            )


def adjusted_rows(codes):
    rows = []
    for code in codes:
        rows.extend(
            [
                {
                    "time": "2020-01-02",
                    "thscode": code,
                    "open": 5.0,
                    "high": 5.5,
                    "low": 4.5,
                    "close": 5.25,
                    "vwap": 5.2,
                    "volume": 1000.0,
                },
                {
                    "time": "2020-01-03",
                    "thscode": code,
                    "open": 5.2,
                    "high": 5.8,
                    "low": 5.0,
                    "close": 5.5,
                    "vwap": 5.4,
                    "volume": 1200.0,
                },
            ]
        )
    return normalize_daily_frame(pd.DataFrame(rows))


def test_forward_adjusted_builder_publishes_separate_database(tmp_path):
    source = tmp_path / "stock_data.db"
    target = tmp_path / "stock_data_qfq.db"
    create_source_db(source)

    builder = AdjustedStockDataBuilder(
        source,
        target,
        lambda codes, start, end: adjusted_rows(codes),
        adjustment="forward",
    )
    result = builder.build(
        start_date=date(2020, 1, 2),
        end_date=date(2020, 1, 3),
        batch_size=2,
        retry_delay=0,
    )

    assert result["status"] == "success"
    assert result["published"] is True
    assert result["rows_inserted"] == 4
    with sqlite3.connect(target) as conn:
        metadata = conn.execute(
            "SELECT adjustment, ifind_params, security_count, row_count "
            "FROM price_database_metadata"
        ).fetchone()
        qfq_close = conn.execute(
            'SELECT close FROM "stock_000001_SZ" WHERE time=?', ("2020-01-02",)
        ).fetchone()[0]
    with sqlite3.connect(source) as conn:
        raw_close = conn.execute(
            'SELECT close FROM "stock_000001_SZ" WHERE time=?', ("2020-01-02",)
        ).fetchone()[0]
    assert metadata == ("forward", "CPS:2", 2, 4)
    assert qfq_close == 5.25
    assert raw_close == 10.5


def test_partial_build_does_not_replace_existing_adjusted_database(tmp_path):
    source = tmp_path / "stock_data.db"
    target = tmp_path / "stock_data_hfq.db"
    create_source_db(source)
    with sqlite3.connect(target) as conn:
        conn.execute("CREATE TABLE marker (value TEXT)")
        conn.execute("INSERT INTO marker VALUES ('keep')")

    def incomplete_fetch(codes, start, end):
        available = [code for code in codes if code == "000001.SZ"]
        if available:
            return adjusted_rows(available)
        return normalize_daily_frame(
            pd.DataFrame(
                columns=("time", "thscode", "open", "high", "low", "close", "vwap", "volume")
            )
        )

    result = AdjustedStockDataBuilder(
        source,
        target,
        incomplete_fetch,
        adjustment="backward",
    ).build(
        start_date=date(2020, 1, 2),
        end_date=date(2020, 1, 3),
        batch_size=2,
        max_retries=1,
        retry_delay=0,
    )

    assert result["status"] == "partial"
    assert result["published"] is False
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT value FROM marker").fetchone()[0] == "keep"


def test_builder_can_freeze_an_explicit_universe(tmp_path):
    source = tmp_path / "stock_data.db"
    target = tmp_path / "stock_data_qfq.db"
    create_source_db(source)
    universe = pd.DataFrame(
        [{"security_code": "000002.SZ", "security_name": "万科A"}]
    )

    result = AdjustedStockDataBuilder(
        source,
        target,
        lambda codes, start, end: adjusted_rows(codes),
        adjustment="forward",
        universe=universe,
        universe_name="csi300_current",
        universe_as_of="2026-07-26",
        universe_source="iFinD THS_WCQuery",
    ).build(
        start_date=date(2020, 1, 2),
        end_date=date(2020, 1, 3),
        retry_delay=0,
    )

    assert result["security_count"] == 1
    with sqlite3.connect(target) as conn:
        stock_tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'stock_%'"
        ).fetchall()
        membership = conn.execute(
            "SELECT security_code, security_name, universe_name, universe_as_of "
            "FROM price_universe_membership"
        ).fetchone()
    assert stock_tables == [("stock_000002_SZ",)]
    assert membership == ("000002.SZ", "万科A", "csi300_current", "2026-07-26")


def test_matching_raw_and_adjusted_market_jump_is_an_audited_exception():
    frame = normalize_daily_frame(
        pd.DataFrame(
            [
                {"time": "2021-08-09", "thscode": "000792.SZ", "open": 8.0,
                 "high": 9.0, "low": 8.0, "close": 8.84, "vwap": 8.5, "volume": 1.0},
                {"time": "2021-08-10", "thscode": "000792.SZ", "open": 35.0,
                 "high": 43.0, "low": 32.0, "close": 35.9, "vwap": 36.0, "volume": 1.0},
            ]
        )
    )
    issue = QualityIssue(
        code="000792.SZ",
        trading_date="2021-08-10",
        rule="close_jump",
        field="close",
        observed="return=3.061086",
        expected="absolute close-to-close return <= 60%",
    )

    blocking, accepted = partition_confirmed_market_jumps([issue], frame, frame)

    assert blocking == []
    assert accepted == [issue]
