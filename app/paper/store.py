from __future__ import annotations

from .context import *  # noqa: F403
from .context import _normalize_paper_quote_code


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
        self._initialize()

    def _initialize(self) -> None:
        apply_migration(self.paper_store.connect, "0003_paper_trading", self._create_schema)
        apply_migration(self.paper_store.connect, "0007_paper_benchmarks", self._create_benchmark_schema)
        apply_migration(self.paper_store.connect, "0013_paper_strategies", self._create_strategy_schema)
        apply_migration(
            self.paper_store.connect,
            "0023_paper_realtime_quote_lookup",
            self._create_realtime_quote_lookup_index,
        )
        apply_migration(
            self.paper_store.connect,
            "0025_strategy_deployment",
            self._create_strategy_deployment_schema,
        )
        migrate_legacy_tables(
            target=self.paper_store,
            source=self.store,
            migration_id="0014_split_paper_trading_database",
            tables=PAPER_TRADING_TABLES,
            replace_tables=("paper_strategy",),
        )
        apply_migration(
            self.paper_store.connect,
            "0028_global_index_prices",
            self._migrate_global_index_prices,
        )
        apply_migration(
            self.paper_store.connect,
            "0029_deploy_system_lightgbm_strategy",
            self._deploy_system_lightgbm_strategy,
        )
        apply_migration(
            self.paper_store.connect,
            "0030_manual_paper_approval",
            self._require_manual_approval,
        )
        self._recover_run_statuses()

    def _require_manual_approval(self) -> None:
        with self.paper_store.connect() as conn:
            conn.execute(
                """UPDATE paper_order
                   SET status='proposed', reviewer=NULL, review_note=NULL, reviewed_at=NULL
                   WHERE status='approved' AND fill_date IS NULL
                     AND reviewer='paper-auto-scheduler'"""
            )

    def _deploy_system_lightgbm_strategy(self) -> None:
        version_id = getattr(self.strategy_service, "system_lightgbm_version_id", None)
        if not version_id:
            return
        version = self.strategy_service.version(version_id)
        now = utc_now()
        with self.paper_store.connect() as conn:
            accounts = conn.execute(
                "SELECT account_id FROM paper_account WHERE strategy_id=? AND status='active'",
                (LIGHTGBM_SHADOW_STRATEGY_ID,),
            ).fetchall()
            for account in accounts:
                active = conn.execute(
                    "SELECT 1 FROM paper_strategy_deployment WHERE account_id=? AND status='active'",
                    (account["account_id"],),
                ).fetchone()
                if active:
                    continue
                conn.execute(
                    "INSERT INTO paper_strategy_deployment VALUES (?, ?, ?, ?, 'active', ?, NULL)",
                    (str(uuid.uuid4()), account["account_id"], version_id, version["compiled_hash"], now),
                )

    def _migrate_global_index_prices(self) -> None:
        with self.paper_store.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='paper_benchmark_price'"
            ).fetchone()
            if not exists:
                return
            rows = [dict(row) for row in conn.execute(
                """
                SELECT benchmark_code AS thscode, trading_date AS time,
                       close, source
                FROM paper_benchmark_price
                ORDER BY fetched_at, account_id
                """
            ).fetchall()]
        deduplicated: dict[tuple[str, str], dict] = {}
        for row in rows:
            key = (row["thscode"], row["time"])
            previous = deduplicated.get(key)
            if previous is not None and abs(float(previous["close"]) - float(row["close"])) > 1e-8:
                raise ValueError(f"账户间指数行情冲突：{key[0]} {key[1]}")
            deduplicated[key] = row
        by_source: dict[str, list[dict]] = {}
        for row in deduplicated.values():
            by_source.setdefault(row.pop("source"), []).append(row)
        for source, source_rows in by_source.items():
            self.stock_pool_store.save_index_prices(source_rows, source=source)
        with self.paper_store.connect() as conn:
            conn.execute("DROP TABLE paper_benchmark_price")

    def _create_strategy_deployment_schema(self) -> None:
        with self.paper_store.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_strategy_deployment (
                    deployment_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    strategy_version_id TEXT NOT NULL,
                    compiled_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    deployed_at TEXT NOT NULL,
                    retired_at TEXT,
                    FOREIGN KEY (account_id) REFERENCES paper_account(account_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_strategy_deployment_active
                ON paper_strategy_deployment(account_id) WHERE status='active';
                """
            )

    def _create_realtime_quote_lookup_index(self) -> None:
        with self.paper_store.connect() as conn:
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_paper_realtime_quote_latest
                   ON paper_realtime_quote(account_id, security_code, received_at DESC)"""
            )

    def _create_strategy_schema(self) -> None:
        with self.paper_store.connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS paper_strategy (
                    strategy_id TEXT PRIMARY KEY,
                    version TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    signal_source TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            now = utc_now()
            conn.executemany(
                """INSERT OR IGNORE INTO paper_strategy
                   (strategy_id, version, name, description, signal_source,
                    config_json, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'active', ?)""",
                [
                    (
                        item["strategy_id"], item["version"], item["name"],
                        item["description"], item["signal_source"],
                        json.dumps(item["config"], ensure_ascii=False, sort_keys=True), now,
                    )
                    for item in PAPER_STRATEGIES
                ],
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(paper_account)")}
            if "strategy_id" not in columns:
                conn.execute("ALTER TABLE paper_account ADD COLUMN strategy_id TEXT")
            conn.execute(
                "UPDATE paper_account SET strategy_id=? WHERE strategy_id IS NULL OR strategy_id=''",
                (DEFAULT_PAPER_STRATEGY_ID,),
            )

    def _create_benchmark_schema(self) -> None:
        with self.paper_store.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_benchmark_price (
                    account_id TEXT NOT NULL,
                    benchmark_code TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    close REAL NOT NULL,
                    source TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, benchmark_code, trading_date),
                    FOREIGN KEY (account_id) REFERENCES paper_account(account_id)
                );
                CREATE INDEX IF NOT EXISTS idx_paper_benchmark_account_date
                ON paper_benchmark_price(account_id, trading_date, benchmark_code);
                """
            )

    def _create_schema(self) -> None:
        with self.paper_store.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_account (
                    account_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    initial_cash REAL NOT NULL,
                    cash REAL NOT NULL,
                    benchmark_code TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_daily_run (
                    run_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    factor_snapshot_id TEXT NOT NULL,
                    shadow_snapshot_id TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    market_summary_json TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    UNIQUE(account_id, as_of, strategy_version),
                    FOREIGN KEY (account_id) REFERENCES paper_account(account_id)
                );
                CREATE TABLE IF NOT EXISTS paper_order (
                    order_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    security_code TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    reference_price REAL NOT NULL,
                    target_weight REAL NOT NULL,
                    reason_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reviewer TEXT,
                    review_note TEXT,
                    reviewed_at TEXT,
                    fill_date TEXT,
                    fill_price REAL,
                    gross_amount REAL,
                    fees REAL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES paper_daily_run(run_id),
                    FOREIGN KEY (account_id) REFERENCES paper_account(account_id)
                );
                CREATE INDEX IF NOT EXISTS idx_paper_order_account_status
                ON paper_order(account_id, status, created_at DESC);
                CREATE TABLE IF NOT EXISTS paper_position (
                    account_id TEXT NOT NULL,
                    security_code TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    available_quantity INTEGER NOT NULL,
                    average_cost REAL NOT NULL,
                    realized_pnl REAL NOT NULL,
                    last_buy_date TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, security_code),
                    FOREIGN KEY (account_id) REFERENCES paper_account(account_id)
                );
                CREATE TABLE IF NOT EXISTS paper_nav_snapshot (
                    account_id TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    cash REAL NOT NULL,
                    market_value REAL NOT NULL,
                    nav REAL NOT NULL,
                    daily_return REAL,
                    cumulative_return REAL NOT NULL,
                    benchmark_return REAL,
                    excess_return REAL,
                    drawdown REAL NOT NULL,
                    gross_exposure REAL NOT NULL,
                    turnover REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, trading_date),
                    FOREIGN KEY (account_id) REFERENCES paper_account(account_id)
                );
                CREATE TABLE IF NOT EXISTS paper_realtime_quote (
                    snapshot_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    security_code TEXT NOT NULL,
                    quote_time TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    open REAL NOT NULL,
                    latest REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    volume REAL NOT NULL,
                    amount REAL NOT NULL,
                    previous_close REAL NOT NULL,
                    source TEXT NOT NULL,
                    purpose_json TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL UNIQUE,
                    raw_json TEXT NOT NULL,
                    FOREIGN KEY (account_id) REFERENCES paper_account(account_id)
                );
                CREATE INDEX IF NOT EXISTS idx_paper_realtime_quote_account_code_time
                ON paper_realtime_quote(account_id, security_code, quote_time DESC);
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(paper_order)")}
            if "execution_quote_id" not in columns:
                conn.execute("ALTER TABLE paper_order ADD COLUMN execution_quote_id TEXT")
            conn.execute(
                """UPDATE paper_daily_run SET status=CASE
                    WHEN EXISTS (SELECT 1 FROM paper_order o WHERE o.run_id=paper_daily_run.run_id AND o.status='proposed')
                        THEN 'awaiting_review'
                    WHEN EXISTS (SELECT 1 FROM paper_order o WHERE o.run_id=paper_daily_run.run_id AND o.status='approved')
                        THEN 'approved_waiting_execution'
                    ELSE 'completed' END
                WHERE status<>'superseded'"""
            )

    def _recover_run_statuses(self) -> None:
        with self.paper_store.connect() as conn:
            conn.execute(
                """UPDATE paper_daily_run SET status=CASE
                    WHEN EXISTS (SELECT 1 FROM paper_order o WHERE o.run_id=paper_daily_run.run_id AND o.status='proposed')
                        THEN 'awaiting_review'
                    WHEN EXISTS (SELECT 1 FROM paper_order o WHERE o.run_id=paper_daily_run.run_id AND o.status='approved')
                        THEN 'approved_waiting_execution'
                    ELSE 'completed' END
                WHERE status<>'superseded'"""
            )

    @staticmethod
    def _decode_json(item: dict, *keys: str) -> dict:
        for key in keys:
            item[key] = json.loads(item.pop(f"{key}_json"))
        return item

    def _strategy(self, strategy_id: str) -> dict:
        with self.paper_store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM paper_strategy WHERE strategy_id=? AND status='active'",
                (strategy_id,),
            ).fetchone()
        if row is None:
            raise KeyError("模拟策略不存在或未启用")
        return self._decode_json(dict(row), "config")

    def list_strategies(self) -> list[dict]:
        with self.paper_store.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_strategy WHERE status='active' ORDER BY name, strategy_id"
            ).fetchall()
        return [self._decode_json(dict(row), "config") for row in rows]

    def _with_strategy(self, account: dict) -> dict:
        deployment = None
        with self.paper_store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM paper_strategy_deployment WHERE account_id=? AND status='active'",
                (account["account_id"],),
            ).fetchone()
            deployment = dict(row) if row else None
        if deployment and self.strategy_service:
            strategy = self.strategy_service.runtime_strategy(deployment["strategy_version_id"])
            return {**account, "strategy": strategy, "strategy_deployment": deployment}
        return {**account, "strategy": self._strategy(account["strategy_id"]), "strategy_deployment": None}

    def deploy_strategy(self, account_id: str, strategy_version_id: str) -> dict:
        self.account(account_id)
        if self.strategy_service is None:
            raise RuntimeError("策略版本服务未配置")
        version = self.strategy_service.version(strategy_version_id)
        deployment_id, now = str(uuid.uuid4()), utc_now()
        with self.paper_store.connect() as conn:
            conn.execute(
                "UPDATE paper_strategy_deployment SET status='retired', retired_at=? WHERE account_id=? AND status='active'",
                (now, account_id),
            )
            conn.execute(
                "INSERT INTO paper_strategy_deployment VALUES (?, ?, ?, ?, 'active', ?, NULL)",
                (deployment_id, account_id, strategy_version_id, version["compiled_hash"], now),
            )
        return self.account(account_id)["strategy_deployment"]

    def create_account(
        self, name: str, initial_cash: float, benchmark_code: str,
        strategy_id: str = DEFAULT_PAPER_STRATEGY_ID,
        strategy_version_id: str | None = None,
    ) -> dict:
        self._strategy(strategy_id)
        if strategy_version_id:
            if self.strategy_service is None:
                raise RuntimeError("策略版本服务未配置")
            self.strategy_service.version(strategy_version_id)
        now = utc_now()
        account_id = str(uuid.uuid4())
        with self.paper_store.connect() as conn:
            conn.execute(
                """INSERT INTO paper_account
                (account_id, name, initial_cash, cash, benchmark_code, status,
                 created_at, updated_at, strategy_id)
                VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
                (account_id, name, initial_cash, initial_cash, benchmark_code, now, now, strategy_id),
            )
        if (
            strategy_version_id
            and self.strategy_service is not None
        ):
            self.deploy_strategy(account_id, strategy_version_id)
        elif (
            strategy_id == LIGHTGBM_SHADOW_STRATEGY_ID
            and getattr(self.strategy_service, "system_lightgbm_version_id", None)
        ):
            self.deploy_strategy(
                account_id, self.strategy_service.system_lightgbm_version_id
            )
        return self.account(account_id)

    def list_accounts(self) -> list[dict]:
        with self.paper_store.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_account WHERE status='active' ORDER BY created_at, account_id"
            ).fetchall()
        return [self._with_strategy(dict(row)) for row in rows]

    def default_account(self) -> dict:
        with self.paper_store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM paper_account WHERE status='active' ORDER BY created_at LIMIT 1"
            ).fetchone()
        return self._with_strategy(dict(row)) if row else self.create_account(
            "每日模拟组合", 1_000_000, "000300.SH", DEFAULT_PAPER_STRATEGY_ID
        )

    def account(self, account_id: str) -> dict:
        with self.paper_store.connect() as conn:
            row = conn.execute("SELECT * FROM paper_account WHERE account_id=?", (account_id,)).fetchone()
        if row is None:
            raise KeyError("模拟账户不存在")
        return self._with_strategy(dict(row))
