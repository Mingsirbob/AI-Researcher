import sqlite3

from app.market.qfq_cleanup import delete_prelisting_blank_rows


def test_cleanup_deletes_only_prelisting_all_null_rows():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE stock_000001_SZ (
            time TEXT PRIMARY KEY, open REAL, high REAL, low REAL,
            close REAL, vwap REAL, volume REAL
        )
        """
    )
    conn.executemany(
        "INSERT INTO stock_000001_SZ VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("2020-01-02", None, None, None, None, None, None),
            ("2020-01-03", 10, 10, 10, 10, None, None),
            ("2020-01-06", None, None, None, None, None, None),
            ("2020-01-07", 10, 11, 9, 10.5, 10.2, 1000),
        ],
    )

    stats = delete_prelisting_blank_rows(conn, ["stock_000001_SZ"])

    assert stats.rows_deleted == 1
    assert stats.suspended_rows_before == stats.suspended_rows_after == 1
    assert conn.execute(
        "SELECT time FROM stock_000001_SZ ORDER BY time"
    ).fetchall() == [("2020-01-03",), ("2020-01-06",), ("2020-01-07",)]
