from __future__ import annotations

import sqlite3
import math
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from app.core.primitives import SECURITY_CODE_RE


MAJOR_INDEX_POOLS = (
    {
        "pool_id": "sse_composite",
        "name": "上证指数",
        "index_code": "000001.SH",
        "query_text": "上证指数成分股",
    },
    {
        "pool_id": "csi300",
        "name": "沪深300",
        "index_code": "000300.SH",
        "query_text": "沪深300成分股",
        "expected_member_count": 300,
    },
    {
        "pool_id": "csi500",
        "name": "中证500",
        "index_code": "000905.SH",
        "query_text": "中证500成分股",
        "expected_member_count": 500,
    },
    {
        "pool_id": "szse_component",
        "name": "深证成指",
        "index_code": "399001.SZ",
        "query_text": "深证成指成分股",
        "expected_member_count": 500,
    },
    {
        "pool_id": "chinext",
        "name": "创业板指",
        "index_code": "399006.SZ",
        "query_text": "创业板指成分股",
        "expected_member_count": 100,
    },
    {
        "pool_id": "star50",
        "name": "科创50",
        "index_code": "000688.SH",
        "query_text": "科创50成分股",
        "expected_member_count": 50,
    },
    {
        "pool_id": "csi_a500",
        "name": "中证A500",
        "index_code": "000510.CSI",
        "query_text": "中证A500指数成分股",
        "expected_member_count": 500,
    },
)

MARKET_POOLS = (
    {
        "pool_id": "all_a_non_st",
        "name": "全部A股（非ST）",
        "pool_type": "market",
        "index_code": None,
        "query_text": "全部A股（非ST）",
        "minimum_member_count": 4000,
        "maximum_member_count": 6000,
    },
)

STOCK_POOLS = MAJOR_INDEX_POOLS + MARKET_POOLS

MAJOR_INDEX_BENCHMARKS = {
    "000001.SH": "上证指数",
    "000300.SH": "沪深300",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000688.SH": "科创50",
    "000510.CSI": "中证A500",
}

_INDEX_CODE_RE = re.compile(r"^(\d{6})\.(SH|SZ|CSI)$", re.IGNORECASE)


def index_price_table(index_code: str) -> str:
    match = _INDEX_CODE_RE.fullmatch(str(index_code).strip().upper())
    if match is None:
        raise ValueError(f"非法指数代码：{index_code}")
    return f"index_{match.group(1)}_{match.group(2).upper()}"


class StockPoolStore:
    """Dated market, index, sector and ETF constituent snapshots."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._create_schema()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _create_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS stock_pool (
                    pool_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    pool_type TEXT NOT NULL,
                    index_code TEXT,
                    source TEXT NOT NULL,
                    query_text TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_snapshot_date TEXT,
                    last_member_count INTEGER,
                    last_refreshed_at TEXT,
                    last_error TEXT
                );

                CREATE TABLE IF NOT EXISTS stock_pool_member (
                    pool_id TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    security_code TEXT NOT NULL,
                    security_name TEXT,
                    weight REAL,
                    source TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (pool_id, as_of, security_code),
                    FOREIGN KEY (pool_id) REFERENCES stock_pool(pool_id)
                );

                CREATE TABLE IF NOT EXISTS stock_pool_exclusion (
                    pool_id TEXT NOT NULL,
                    security_code TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (pool_id, security_code),
                    FOREIGN KEY (pool_id) REFERENCES stock_pool(pool_id)
                );

                CREATE INDEX IF NOT EXISTS idx_stock_pool_member_lookup
                ON stock_pool_member(pool_id, as_of, security_code);

                CREATE INDEX IF NOT EXISTS idx_stock_pool_member_security
                ON stock_pool_member(security_code, as_of, pool_id);
                """
            )
            for code in MAJOR_INDEX_BENCHMARKS:
                table = index_price_table(code)
                conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS "{table}" (
                        time TEXT PRIMARY KEY,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL NOT NULL,
                        vwap REAL,
                        volume REAL,
                        source TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                """)

    def save_index_prices(
        self,
        rows: list[dict],
        *,
        source: str = "iFinD THS_HD",
    ) -> dict:
        normalized: dict[str, list[tuple]] = {
            code: [] for code in MAJOR_INDEX_BENCHMARKS
        }
        seen: set[tuple[str, str]] = set()
        updated_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
        for row in rows:
            code = str(row.get("thscode") or "").strip().upper().replace("_", ".")
            if code not in MAJOR_INDEX_BENCHMARKS:
                raise ValueError(f"不支持的指数行情：{code}")
            trading_date = str(row.get("time") or "")[:10]
            date.fromisoformat(trading_date)
            close = self._finite_price(row.get("close"))
            if close is None or close <= 0:
                raise ValueError(f"指数 {code} 在 {trading_date} 的收盘价无效")
            key = (code, trading_date)
            if key in seen:
                raise ValueError(f"指数行情包含重复日期：{code} {trading_date}")
            seen.add(key)
            normalized[code].append((
                trading_date,
                self._finite_price(row.get("open")),
                self._finite_price(row.get("high")),
                self._finite_price(row.get("low")),
                close,
                self._finite_price(row.get("vwap")),
                self._finite_price(row.get("volume")),
                source,
                updated_at,
            ))
        with self.connect() as conn:
            for code, values in normalized.items():
                if not values:
                    continue
                table = index_price_table(code)
                conn.executemany(
                    f"""
                    INSERT INTO "{table}"
                    (time, open, high, low, close, vwap, volume, source, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(time) DO UPDATE SET
                        open=excluded.open, high=excluded.high, low=excluded.low,
                        close=excluded.close, vwap=excluded.vwap,
                        volume=excluded.volume, source=excluded.source,
                        updated_at=excluded.updated_at
                    """,
                    values,
                )
        return {
            "rows_written": len(seen),
            "benchmark_count": sum(bool(values) for values in normalized.values()),
            "source": source,
        }

    def index_prices(self, index_code: str, *, end_date: str | None = None) -> list[dict]:
        table = index_price_table(index_code)
        sql = f'SELECT * FROM "{table}"'
        parameters: tuple[str, ...] = ()
        if end_date:
            date.fromisoformat(end_date)
            sql += " WHERE time<=?"
            parameters = (end_date,)
        sql += " ORDER BY time"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, parameters).fetchall()]

    def index_latest_date(self) -> str | None:
        latest: list[str] = []
        with self.connect() as conn:
            for code in MAJOR_INDEX_BENCHMARKS:
                table = index_price_table(code)
                value = conn.execute(f'SELECT MAX(time) FROM "{table}"').fetchone()[0]
                if value:
                    latest.append(value)
        return min(latest) if len(latest) == len(MAJOR_INDEX_BENCHMARKS) else None

    @staticmethod
    def _finite_price(value) -> float | None:
        if value is None or pd.isna(value):
            return None
        number = float(value)
        return number if math.isfinite(number) else None

    def register(self, definition: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO stock_pool (
                    pool_id, name, pool_type, index_code, source, query_text, enabled
                ) VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(pool_id) DO UPDATE SET
                    name=excluded.name,
                    pool_type=excluded.pool_type,
                    index_code=excluded.index_code,
                    source=excluded.source,
                    query_text=excluded.query_text,
                    enabled=1
                """,
                (
                    definition["pool_id"],
                    definition["name"],
                    definition.get("pool_type", "index"),
                    definition.get("index_code"),
                    definition.get("source", "iFinD THS_WCQuery"),
                    definition.get("query_text"),
                ),
            )

    def save_snapshot(self, pool_id: str, as_of: date, members: pd.DataFrame) -> dict:
        required = {"security_code", "security_name"}
        if not isinstance(members, pd.DataFrame) or not required.issubset(members.columns):
            raise ValueError("股池快照缺少 security_code 或 security_name")
        normalized = members.copy()
        normalized["security_code"] = normalized["security_code"].astype(str).str.upper()
        normalized["security_name"] = normalized["security_name"].fillna("").astype(str).str.strip()
        if normalized.empty or normalized["security_code"].duplicated().any():
            raise ValueError("股池快照为空或包含重复证券")
        fetched_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
        snapshot_date = as_of.isoformat()
        with self.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM stock_pool WHERE pool_id=?", (pool_id,)
            ).fetchone()
            if not exists:
                raise KeyError(f"股池未注册：{pool_id}")
            excluded = {
                row[0]
                for row in conn.execute(
                    "SELECT security_code FROM stock_pool_exclusion WHERE pool_id=?",
                    (pool_id,),
                )
            }
            normalized = normalized.loc[
                ~normalized["security_code"].isin(excluded)
            ].copy()
            if normalized.empty:
                raise ValueError("股池快照应用排除规则后为空")
            rows = [
                (
                    pool_id,
                    snapshot_date,
                    row.security_code,
                    row.security_name,
                    None if pd.isna(getattr(row, "weight", None)) else float(row.weight),
                    "iFinD THS_WCQuery",
                    fetched_at,
                )
                for row in normalized.itertuples(index=False)
            ]
            conn.execute(
                "DELETE FROM stock_pool_member WHERE pool_id=? AND as_of=?",
                (pool_id, snapshot_date),
            )
            conn.executemany(
                """
                INSERT INTO stock_pool_member (
                    pool_id, as_of, security_code, security_name, weight, source, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.execute(
                """
                UPDATE stock_pool SET
                    last_snapshot_date=?, last_member_count=?, last_refreshed_at=?, last_error=NULL
                WHERE pool_id=?
                """,
                (snapshot_date, len(rows), fetched_at, pool_id),
            )
        return {"pool_id": pool_id, "as_of": snapshot_date, "member_count": len(rows)}

    def exclude_members(
        self, pool_id: str, security_codes: list[str], *, reason: str
    ) -> dict:
        codes = sorted({str(code).strip().upper() for code in security_codes if code})
        if not codes or any(SECURITY_CODE_RE.fullmatch(code) is None for code in codes):
            raise ValueError("排除列表包含非法A股代码")
        normalized_reason = str(reason).strip()
        if not normalized_reason:
            raise ValueError("排除原因不能为空")
        created_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
        placeholders = ",".join("?" for _ in codes)
        with self.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM stock_pool WHERE pool_id=?", (pool_id,)
            ).fetchone()
            if not exists:
                raise KeyError(f"股池未注册：{pool_id}")
            conn.executemany(
                """
                INSERT INTO stock_pool_exclusion VALUES (?, ?, ?, ?)
                ON CONFLICT(pool_id, security_code) DO UPDATE SET
                    reason=excluded.reason, created_at=excluded.created_at
                """,
                [(pool_id, code, normalized_reason, created_at) for code in codes],
            )
            before = conn.total_changes
            conn.execute(
                f"DELETE FROM stock_pool_member WHERE pool_id=? "
                f"AND security_code IN ({placeholders})",
                (pool_id, *codes),
            )
            removed_count = conn.total_changes - before
            latest = conn.execute(
                "SELECT last_snapshot_date FROM stock_pool WHERE pool_id=?", (pool_id,)
            ).fetchone()[0]
            member_count = conn.execute(
                "SELECT COUNT(*) FROM stock_pool_member WHERE pool_id=? AND as_of=?",
                (pool_id, latest),
            ).fetchone()[0]
            conn.execute(
                "UPDATE stock_pool SET last_member_count=? WHERE pool_id=?",
                (member_count, pool_id),
            )
        return {
            "pool_id": pool_id,
            "excluded_codes": codes,
            "removed_count": removed_count,
            "member_count": member_count,
        }

    def record_failure(self, pool_id: str, error: Exception) -> None:
        refreshed_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
        with self.connect() as conn:
            conn.execute(
                "UPDATE stock_pool SET last_refreshed_at=?, last_error=? WHERE pool_id=?",
                (refreshed_at, f"{type(error).__name__}: {error}", pool_id),
            )

    def list_pools(self) -> list[dict]:
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute("SELECT * FROM stock_pool ORDER BY pool_id").fetchall()
            ]

    def resolve(self, pool_id: str, as_of: date) -> dict:
        with self.connect() as conn:
            snapshot = conn.execute(
                """
                SELECT MAX(as_of) AS as_of FROM stock_pool_member
                WHERE pool_id=? AND as_of<=?
                """,
                (pool_id, as_of.isoformat()),
            ).fetchone()
            snapshot_date = snapshot["as_of"] if snapshot else None
            if not snapshot_date:
                raise LookupError(f"{pool_id} 在 {as_of.isoformat()} 前没有股池快照")
            definition = conn.execute(
                "SELECT * FROM stock_pool WHERE pool_id=?", (pool_id,)
            ).fetchone()
            members = conn.execute(
                """
                SELECT security_code, security_name, weight FROM stock_pool_member
                WHERE pool_id=? AND as_of=? ORDER BY security_code
                """,
                (pool_id, snapshot_date),
            ).fetchall()
        return {
            "pool_id": pool_id,
            "name": definition["name"],
            "as_of": snapshot_date,
            "source": definition["source"],
            "member_count": len(members),
            "members": [dict(row) for row in members],
        }
