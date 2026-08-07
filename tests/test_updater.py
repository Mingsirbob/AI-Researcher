import sqlite3
from datetime import date, datetime
import pandas as pd

from app.market.updater import (
    StockDataUpdater,
    default_end_date,
    normalize_daily_frame,
    validate_daily_frame,
)


def create_stock_db(path):
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
                ("2026-07-17", 10.0, 11.0, 9.0, 10.5, 10.4, 1000.0),
            )


def test_default_end_date_waits_until_evening():
    morning = datetime(2026, 7, 21, 9, 0)
    evening = datetime(2026, 7, 21, 19, 0)
    assert default_end_date(morning) == date(2026, 7, 20)
    assert default_end_date(evening) == date(2026, 7, 21)


def test_update_plan_reports_pending_securities_and_market_trading_day_gap(tmp_path):
    db_path = tmp_path / "stocks.db"
    create_stock_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            'INSERT INTO "stock_600000_SH" VALUES (?, ?, ?, ?, ?, ?, ?)',
            ("2026-07-20", 10.5, 11.5, 10.0, 11.0, 10.9, 1200.0),
        )

    plan = StockDataUpdater(db_path, lambda *_: pd.DataFrame()).plan(
        date(2026, 7, 21),
        trading_dates=[date(2026, 7, 20), date(2026, 7, 21)],
    )

    assert plan.pending_security_count == 2
    assert plan.missing_trading_dates == (date(2026, 7, 21),)
    assert plan.as_dict()["missing_trading_day_count"] == 1


def test_normalize_daily_frame_requires_contract():
    frame = pd.DataFrame([{"time": "2026-07-20"}])
    try:
        normalize_daily_frame(frame)
    except Exception as exc:
        assert "缺少字段" in str(exc)
    else:
        raise AssertionError("缺少字段时应拒绝更新")


def test_incremental_update_is_append_only_and_idempotent(tmp_path):
    db_path = tmp_path / "stocks.db"
    create_stock_db(db_path)

    def fake_fetch(codes, start, end):
        rows = []
        for code in codes:
            rows.extend(
                [
                    {
                        "time": "2026-07-20",
                        "thscode": code,
                        "open": 10.5,
                        "high": 11.5,
                        "low": 10.0,
                        "close": 11.0,
                        "vwap": 10.9,
                        "volume": 1200,
                    },
                ]
            )
        return normalize_daily_frame(pd.DataFrame(rows))

    updater = StockDataUpdater(db_path, fake_fetch)
    result = updater.update(date(2026, 7, 20), batch_size=2, retry_delay=0)
    assert result["status"] == "success"
    assert result["rows_inserted"] == 2

    with sqlite3.connect(db_path) as conn:
        original = conn.execute(
            'SELECT close FROM "stock_000001_SZ" WHERE time = ?',
            ("2026-07-17",),
        ).fetchone()[0]
        count = conn.execute('SELECT COUNT(*) FROM "stock_000001_SZ"').fetchone()[0]
        journal = conn.execute("SELECT status, adjustment FROM data_update_runs").fetchone()
        sync_status = conn.execute(
            "SELECT latest_date, status FROM market_data_status WHERE security_code='000001.SZ'"
        ).fetchone()
    assert original == 10.5
    assert count == 2
    assert journal == ("success", "unadjusted")
    assert sync_status == ("2026-07-20", "ready")

    repeated = updater.update(date(2026, 7, 20), batch_size=2, retry_delay=0)
    assert repeated["status"] == "up_to_date"
    assert repeated["rows_inserted"] == 0


def test_quality_gate_quarantines_jump_and_persists_issue(tmp_path):
    db_path = tmp_path / "stocks.db"
    create_stock_db(db_path)

    def fake_fetch(codes, start, end):
        return normalize_daily_frame(
            pd.DataFrame(
                [
                    {
                        "time": "2026-07-20",
                        "thscode": code,
                        "open": 21.0,
                        "high": 23.0,
                        "low": 20.0,
                        "close": 22.0,
                        "vwap": 21.5,
                        "volume": 1200,
                    }
                    for code in codes
                ]
            )
        )

    result = StockDataUpdater(db_path, fake_fetch).update(
        date(2026, 7, 20), batch_size=2, retry_delay=0
    )

    assert result["status"] == "partial"
    assert result["rows_inserted"] == 0
    assert result["quality_issue_count"] == 2
    assert result["quality_failed_codes"] == ["000001.SZ", "600000.SH"]
    with sqlite3.connect(db_path) as conn:
        issue_rules = conn.execute(
            "SELECT rule, COUNT(*) FROM data_quality_issues GROUP BY rule"
        ).fetchall()
        row_count = conn.execute('SELECT COUNT(*) FROM "stock_000001_SZ"').fetchone()[0]
    assert issue_rules == [("close_jump", 2)]
    assert row_count == 1


def test_contract_change_fails_run_without_writing(tmp_path):
    db_path = tmp_path / "stocks.db"
    create_stock_db(db_path)

    def fake_fetch(codes, start, end):
        return normalize_daily_frame(
            pd.DataFrame(
                [
                    {
                        "time": "2026-07-20",
                        "thscode": codes[0],
                        "open": 10.5,
                        "high": 11.5,
                        "low": 10.0,
                        "close": 11.0,
                        "vwap": 10.9,
                        "volume": 1200,
                        "new_sdk_field": "unexpected",
                    }
                ]
            )
        )

    try:
        StockDataUpdater(db_path, fake_fetch).update(date(2026, 7, 20), retry_delay=0)
    except Exception as exc:
        assert "未声明字段" in str(exc)
    else:
        raise AssertionError("SDK 字段变化必须终止更新")

    with sqlite3.connect(db_path) as conn:
        status, category = conn.execute(
            "SELECT status, error_category FROM data_update_runs"
        ).fetchone()
        row_count = conn.execute('SELECT COUNT(*) FROM "stock_000001_SZ"').fetchone()[0]
    assert (status, category) == ("failed", "contract")
    assert row_count == 1


def test_quality_gate_detects_ohlc_and_vwap_bounds():
    frame = normalize_daily_frame(
        pd.DataFrame(
            [
                {
                    "time": "2026-07-20",
                    "thscode": "000001.SZ",
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.5,
                    "close": 11.0,
                    "vwap": 12.0,
                    "volume": 1000,
                }
            ]
        )
    )
    issues = validate_daily_frame(
        frame,
        requested_codes={"000001.SZ"},
        start=date(2026, 7, 20),
        end=date(2026, 7, 20),
        previous_closes={"000001.SZ": 10.5},
    )
    assert {issue.rule for issue in issues} == {"ohlc_bounds", "vwap_bounds"}


def test_quality_gate_accepts_flat_suspension_placeholder_and_vwap_rounding():
    frame = normalize_daily_frame(
        pd.DataFrame(
            [
                {
                    "time": "2026-07-20",
                    "thscode": "000001.SZ",
                    "open": 10.0,
                    "high": 10.0,
                    "low": 10.0,
                    "close": 10.0,
                    "vwap": None,
                    "volume": None,
                },
                {
                    "time": "2026-07-21",
                    "thscode": "600000.SH",
                    "open": 64.34,
                    "high": 64.34,
                    "low": 64.34,
                    "close": 64.34,
                    "vwap": 64.34000015878,
                    "volume": 1000,
                },
            ]
        )
    )

    issues = validate_daily_frame(
        frame,
        requested_codes={"000001.SZ", "600000.SH"},
        start=date(2026, 7, 20),
        end=date(2026, 7, 21),
        previous_closes={"000001.SZ": 10.0, "600000.SH": 64.34},
    )

    assert issues == []


def test_quality_gate_still_rejects_nonflat_row_without_volume():
    frame = normalize_daily_frame(
        pd.DataFrame(
            [
                {
                    "time": "2026-07-20",
                    "thscode": "000001.SZ",
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.5,
                    "close": 10.0,
                    "vwap": 10.0,
                    "volume": None,
                }
            ]
        )
    )

    issues = validate_daily_frame(
        frame,
        requested_codes={"000001.SZ"},
        start=date(2026, 7, 20),
        end=date(2026, 7, 20),
        previous_closes={"000001.SZ": 10.0},
    )

    assert {issue.rule for issue in issues} == {"complete_ohlcv"}
