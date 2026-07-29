from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Iterator


from app.core.primitives import code_to_table, normalize_code

KNOWN_NAMES = {
    "000001.SZ": "平安银行",
    "000002.SZ": "万科A",
    "000333.SZ": "美的集团",
    "000651.SZ": "格力电器",
    "000858.SZ": "五粮液",
    "002415.SZ": "海康威视",
    "002594.SZ": "比亚迪",
    "300059.SZ": "东方财富",
    "300124.SZ": "汇川技术",
    "300308.SZ": "中际旭创",
    "300750.SZ": "宁德时代",
    "600000.SH": "浦发银行",
    "600036.SH": "招商银行",
    "600276.SH": "恒瑞医药",
    "600519.SH": "贵州茅台",
    "600900.SH": "长江电力",
    "601318.SH": "中国平安",
    "601398.SH": "工商银行",
    "601899.SH": "紫金矿业",
    "603259.SH": "药明康德",
    "688041.SH": "海光信息",
    "688111.SH": "金山办公",
    "688256.SH": "寒武纪",
    "688981.SH": "中芯国际",
}


class StockRepository:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        if not db_path.exists():
            raise FileNotFoundError(f"行情数据库不存在: {db_path}")
        self._tables = self._load_tables()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        uri = self.db_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _load_tables(self) -> set[str]:
        with sqlite3.connect(self.db_path) as conn:
            return {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = ? AND name LIKE 'stock_%'",
                    ("table",),
                )
            }

    @property
    def security_count(self) -> int:
        return len(self._tables)

    @property
    def security_codes(self) -> list[str]:
        return [
            table.removeprefix("stock_").replace("_", ".")
            for table in sorted(self._tables)
        ]

    def exists(self, code: str) -> bool:
        return code_to_table(code) in self._tables

    def list_securities(self, query: str = "", limit: int = 12) -> list[dict]:
        needle = query.strip().upper()
        matches: list[dict] = []
        for table in sorted(self._tables):
            code = table.removeprefix("stock_").replace("_", ".")
            name = KNOWN_NAMES.get(code, "")
            if needle and needle not in code and needle not in name.upper():
                continue
            matches.append({"code": code, "name": name or code, "has_name": bool(name)})
            if len(matches) >= max(1, min(limit, 50)):
                break
        return matches

    def get_history(
        self,
        code: str,
        start: str | None = None,
        end: str | None = None,
        limit: int = 1800,
    ) -> list[dict]:
        normalized = normalize_code(code)
        table = code_to_table(normalized)
        if table not in self._tables:
            raise KeyError(f"数据库中不存在 {normalized}")
        conditions: list[str] = []
        params: list[str | int] = []
        if start:
            date.fromisoformat(start)
            conditions.append("time >= ?")
            params.append(start)
        if end:
            date.fromisoformat(end)
            conditions.append("time <= ?")
            params.append(end)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = (
            f'SELECT time, open, high, low, close, vwap, volume FROM "{table}" '
            f"{where} ORDER BY time DESC LIMIT ?"
        )
        params.append(max(30, min(limit, 3000)))
        with self.connect() as conn:
            rows = [dict(row) for row in conn.execute(sql, params)]
        rows.reverse()
        return rows

    def iter_histories(
        self,
        *,
        end: str,
        limit: int = 320,
    ) -> Iterator[tuple[str, list[dict]]]:
        date.fromisoformat(end)
        row_limit = max(30, min(limit, 3000))
        with self.connect() as conn:
            for table in sorted(self._tables):
                code = table.removeprefix("stock_").replace("_", ".")
                rows = [
                    dict(row)
                    for row in conn.execute(
                        f"""
                        SELECT time, open, high, low, close, vwap, volume
                        FROM "{table}" WHERE time <= ?
                        ORDER BY time DESC LIMIT ?
                        """,
                        (end, row_limit),
                    )
                ]
                rows.reverse()
                yield code, rows

    def date_range(self, code: str) -> tuple[str, str, int]:
        table = code_to_table(code)
        if table not in self._tables:
            raise KeyError(code)
        with self.connect() as conn:
            row = conn.execute(
                f'SELECT MIN(time), MAX(time), COUNT(*) FROM "{table}"'
            ).fetchone()
        return row[0], row[1], row[2]

    def display_name(self, code: str) -> str:
        normalized = normalize_code(code)
        return KNOWN_NAMES.get(normalized, normalized)
