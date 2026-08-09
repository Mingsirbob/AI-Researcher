from __future__ import annotations

from .context import *  # noqa: F403
from .benchmarks import PaperBenchmarkMixin
from .dashboard import PaperDashboardMixin
from .execution import PaperExecutionMixin
from .store import PaperStoreMixin
from .run_store import PaperRunStore


class PaperExecutionService(
    PaperDashboardMixin, PaperExecutionMixin, PaperBenchmarkMixin, PaperStoreMixin
):
    """Owns paper-account mutations, approvals, fills, positions and NAV."""

    def __init__(
        self,
        repository: StockRepository,
        store: ResearchStore,
        quote_provider: Any | None = None,
        portfolio_decision: PortfolioDecisionService | None = None,
        paper_store: SQLiteStore | ResearchStore | None = None,
        quant_store: QuantStore | None = None,
        strategy_service: Any | None = None,
        stock_pool_store: StockPoolStore | None = None,
        run_store: PaperRunStore | None = None,
    ) -> None:
        super().__init__(
            repository, store, quote_provider, paper_store, quant_store,
            strategy_service, stock_pool_store, run_store,
        )
        self.portfolio_decision = portfolio_decision or PortfolioDecisionService(
            repository, quant_store or store, store, stock_pool_store=self.stock_pool_store
        )

    def research_targets(
        self, *, as_of: str, account_id: str | None = None,
        limit: int = 5, hold_rank_buffer: int = 30,
    ) -> dict:
        account = self.account(account_id) if account_id else self.default_account()
        return self.portfolio_decision.research_targets(
            as_of=as_of,
            strategy=account["strategy"],
            limit=limit,
            hold_rank_buffer=hold_rank_buffer,
        )

    def review_holdings(self, *, account_id: str | None, as_of: str) -> dict:
        account = self.account(account_id) if account_id else self.default_account()
        _, shadow = (
            self.portfolio_decision.sources(as_of, account["strategy"])
            if account["strategy"].get("signal_source") in {"multifactor_linear", "python_code"}
            else self.portfolio_decision.sources(as_of)
        )
        positions = self._positions(account["account_id"], as_of, shadow["snapshot_id"])
        return self.portfolio_decision.review_holdings(
            account_id=account["account_id"],
            positions=positions,
            as_of=as_of,
            strategy=account["strategy"],
        )

    def create_daily_run(
        self,
        *,
        as_of: str,
        account_id: str | None,
        top_n: int,
        hold_rank_buffer: int,
        target_gross_exposure: float | None = None,
        max_position_weight: float | None = None,
        max_industry_weight: float | None = None,
        max_pair_correlation: float | None = None,
        run_key: str | None = None,
    ) -> dict:
        date.fromisoformat(as_of)
        account = self.account(account_id) if account_id else self.default_account()
        strategy = account["strategy"]
        defaults = strategy["config"]
        top_n = int(defaults.get("top_n", top_n))
        hold_rank_buffer = int(defaults.get("hold_rank_buffer", hold_rank_buffer))
        target_gross_exposure = float(
            defaults.get("target_gross_exposure", DEFAULT_TARGET_GROSS_EXPOSURE)
            if target_gross_exposure is None else target_gross_exposure
        )
        max_position_weight = float(
            defaults.get("max_position_weight", DEFAULT_MAX_POSITION_WEIGHT)
            if max_position_weight is None else max_position_weight
        )
        max_industry_weight = float(
            defaults.get("max_industry_weight", DEFAULT_MAX_INDUSTRY_WEIGHT)
            if max_industry_weight is None else max_industry_weight
        )
        max_pair_correlation = float(
            defaults.get("max_pair_correlation", DEFAULT_MAX_PAIR_CORRELATION)
            if max_pair_correlation is None else max_pair_correlation
        )
        with self.paper_store.connect() as conn:
            latest_fill = conn.execute(
                "SELECT MAX(substr(traded_at,1,10)) FROM paper_trade WHERE account_id=?",
                (account["account_id"],),
            ).fetchone()[0]
        if latest_fill and as_of < latest_fill:
            raise ValueError(
                f"研究日 {as_of} 早于账户最近成交日 {latest_fill}，禁止回溯重算当前账户"
            )
        _, shadow = (
            self.portfolio_decision.sources(as_of, strategy)
            if strategy.get("signal_source") in {"multifactor_linear", "python_code"}
            else self.portfolio_decision.sources(as_of)
        )
        positions = self._positions(account["account_id"], as_of, shadow["snapshot_id"])
        now = utc_now()
        strategy_version = f'{strategy["strategy_id"]}@{strategy["version"]}'
        plan = self.portfolio_decision.build_plan(
            account=account,
            positions=positions,
            as_of=as_of,
            run_id=str(uuid.uuid4()),
            created_at=now,
            top_n=top_n,
            hold_rank_buffer=hold_rank_buffer,
            target_gross_exposure=target_gross_exposure,
            max_position_weight=max_position_weight,
            max_industry_weight=max_industry_weight,
            max_pair_correlation=max_pair_correlation,
            strategy=strategy,
        )
        manifest_values = {
                "status": "awaiting_review",
                "strategy": {
                    "strategy_id": strategy["strategy_id"], "name": strategy["name"],
                    "version": strategy["version"], "compiled_hash": strategy.get("compiled_hash"),
                },
                "config": plan["config"],
                "factor_snapshot_id": plan["factor_snapshot_id"],
                "shadow_snapshot_id": plan["shadow_snapshot_id"],
                "snapshot_hash": plan["snapshot_hash"],
                "market_summary": plan["market_summary"],
            }
        if run_key:
            manifest = self.run_store.update_manifest(run_key, **manifest_values)
        else:
            manifest = self.run_store.create(
                account=account, trading_date=as_of, manifest=manifest_values,
            )
        run_key = manifest["run_key"]
        self.run_store.write_result(run_key, "order_proposals.json", {"orders": plan["orders"]})
        try:
            with self.paper_store.connect() as conn:
                conn.execute(
                    """UPDATE paper_proposal SET status='cancelled', reviewed_at=COALESCE(reviewed_at, ?)
                       WHERE account_id=? AND status IN ('proposed','approved')""",
                    (now, account["account_id"]),
                )
                proposal_ids = []
                for order in plan["orders"]:
                    cursor = conn.execute(
                        """INSERT INTO paper_proposal
                        (account_id, run_key, proposal_date, security_code, side, quantity,
                         reference_price, status, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', ?)""",
                        (account["account_id"], run_key, as_of, order["security_code"],
                         order["side"], order["quantity"], order["reference_price"], now),
                    )
                    proposal_ids.append(cursor.lastrowid)
        except Exception as exc:
            self.run_store.update_manifest(run_key, status="failed", error=str(exc), finished_at=utc_now())
            raise
        self.run_store.update_manifest(run_key, proposal_ids=proposal_ids)
        self._refresh_run_status(run_key)
        self._mark_nav(account, as_of)
        return {**self.run(run_key), "reused": False}


class PaperTradingService(PaperExecutionService):
    """Backward-compatible facade used by the existing API and scripts."""

    pass
