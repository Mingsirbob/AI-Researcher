from __future__ import annotations

import sqlite3

from .context import *  # noqa: F403
from .run_store import PaperRunStore, validate_account_name


V2_TABLES = {
    "paper_account", "paper_proposal", "paper_trade", "paper_position",
    "paper_nav_snapshot",
}


class PaperStoreMixin:
    def __init__(
        self,
        repository: StockRepository,
        store: ResearchStore,
        quote_provider: Any | None = None,
        paper_store: SQLiteStore | ResearchStore | None = None,
        quant_store: QuantStore | None = None,
        strategy_service: Any | None = None,
        stock_pool_store: StockPoolStore | None = None,
        run_store: PaperRunStore | None = None,
    ):
        self.repository = repository
        self.store = store
        self.paper_store = paper_store or store
        self.quant_store = quant_store or store
        self.quote_provider = quote_provider
        self.strategy_service = strategy_service
        self.stock_pool_store = stock_pool_store or StockPoolStore(
            repository.db_path.with_name("stock_pool.db")
        )
        self.run_store = run_store or PaperRunStore(repository.db_path.parent / "paper")
        self._initialize()

    def _initialize(self) -> None:
        with self.paper_store.connect() as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if tables.intersection({
                "paper_order", "paper_daily_run", "paper_strategy",
                "paper_strategy_deployment", "paper_daily_batch",
                "paper_daily_batch_step",
            }):
                raise RuntimeError(
                    "paper_trading.db 仍是 V1，请先运行 scripts/migrate_paper_trading_v2.py"
                )
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_account (
                    account_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    initial_cash REAL NOT NULL CHECK(initial_cash > 0),
                    current_cash REAL NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','archived')),
                    strategy_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_proposal (
                    proposal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    run_key TEXT NOT NULL,
                    proposal_date TEXT NOT NULL,
                    security_code TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('buy','sell')),
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    reference_price REAL NOT NULL CHECK(reference_price > 0),
                    status TEXT NOT NULL CHECK(status IN ('proposed','approved','rejected','cancelled','filled')),
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    FOREIGN KEY (account_id) REFERENCES paper_account(account_id)
                );
                CREATE INDEX IF NOT EXISTS idx_paper_proposal_account_status
                ON paper_proposal(account_id, status, proposal_date DESC, proposal_id DESC);
                CREATE INDEX IF NOT EXISTS idx_paper_proposal_run
                ON paper_proposal(run_key, proposal_id);
                CREATE TABLE IF NOT EXISTS paper_trade (
                    trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    security_code TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('buy','sell')),
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    price REAL NOT NULL CHECK(price > 0),
                    fees REAL NOT NULL CHECK(fees >= 0),
                    traded_at TEXT NOT NULL,
                    FOREIGN KEY (account_id) REFERENCES paper_account(account_id)
                );
                CREATE INDEX IF NOT EXISTS idx_paper_trade_account_time
                ON paper_trade(account_id, traded_at DESC, trade_id DESC);
                CREATE TABLE IF NOT EXISTS paper_position (
                    account_id TEXT NOT NULL,
                    security_code TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    available_quantity INTEGER NOT NULL CHECK(available_quantity >= 0),
                    average_cost REAL NOT NULL CHECK(average_cost > 0),
                    last_buy_date TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, security_code),
                    FOREIGN KEY (account_id) REFERENCES paper_account(account_id)
                );
                CREATE TABLE IF NOT EXISTS paper_nav_snapshot (
                    account_id TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    current_cash REAL NOT NULL,
                    market_value REAL NOT NULL,
                    total_equity REAL NOT NULL,
                    daily_return REAL,
                    cumulative_return REAL NOT NULL,
                    drawdown REAL NOT NULL,
                    gross_exposure REAL NOT NULL,
                    turnover REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, trading_date),
                    FOREIGN KEY (account_id) REFERENCES paper_account(account_id)
                );
                """
            )

    def _strategy(self, strategy_name: str) -> dict:
        if self.strategy_service is None:
            matches = [item for item in PAPER_STRATEGIES if item["name"] == strategy_name or item["strategy_id"] == strategy_name]
            if not matches:
                raise KeyError("策略不存在或未发布")
            return matches[0]
        matches = [item for item in self.strategy_service.versions() if item["name"] == strategy_name]
        if not matches:
            raise KeyError("策略不存在或未发布")
        # versions() is newest first; the concrete version is frozen in each run manifest.
        return self.strategy_service.runtime_strategy(matches[0]["strategy_version_id"])

    def list_strategies(self) -> list[dict]:
        if self.strategy_service is None:
            return list(PAPER_STRATEGIES)
        versions = self.strategy_service.versions()
        unique: dict[str, dict] = {}
        for item in versions:
            unique.setdefault(item["name"], item)
        return [self.strategy_service.runtime_strategy(item["strategy_version_id"]) for item in unique.values()]

    def _with_strategy(self, account: dict) -> dict:
        strategy = self._strategy(account["strategy_name"])
        return {**account, "cash": account["current_cash"], "benchmark_code": "000300.SH",
                "strategy_id": strategy["strategy_id"], "strategy": strategy,
                "strategy_deployment": None}

    def create_account(self, name: str, initial_cash: float, strategy_name: str,
                       legacy_strategy_id: str | None = None) -> dict:
        name = validate_account_name(name)
        if legacy_strategy_id is not None:
            strategy_name = legacy_strategy_id
        strategy = self._strategy(strategy_name)
        strategy_name = strategy["name"]
        now = utc_now()
        account_id = str(uuid.uuid4())
        try:
            with self.paper_store.connect() as conn:
                conn.execute(
                    """INSERT INTO paper_account
                    (account_id, name, initial_cash, current_cash, status, strategy_name,
                     created_at, updated_at) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)""",
                    (account_id, name, initial_cash, initial_cash, strategy_name, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("模拟账户名称已存在，请更换名称") from exc
        return self.account(account_id)

    def list_accounts(self) -> list[dict]:
        with self.paper_store.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_account WHERE status='active' ORDER BY created_at, account_id"
            ).fetchall()
        return [self._with_strategy(dict(row)) for row in rows]

    def archive_account(self, account_id: str) -> bool:
        with self.paper_store.connect() as conn:
            cursor = conn.execute(
                "UPDATE paper_account SET status='archived', updated_at=? WHERE account_id=? AND status='active'",
                (utc_now(), account_id),
            )
        return cursor.rowcount > 0

    def default_account(self) -> dict:
        with self.paper_store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM paper_account WHERE status='active' ORDER BY created_at LIMIT 1"
            ).fetchone()
        if row:
            return self._with_strategy(dict(row))
        strategies = self.list_strategies()
        if not strategies:
            raise KeyError("没有可用于创建模拟账户的已发布策略")
        return self.create_account("每日模拟组合", 1_000_000, strategies[0]["name"])

    def account(self, account_id: str) -> dict:
        with self.paper_store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM paper_account WHERE account_id=? AND status='active'", (account_id,)
            ).fetchone()
        if row is None:
            raise KeyError("模拟账户不存在或已停用")
        return self._with_strategy(dict(row))
