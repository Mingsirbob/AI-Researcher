from __future__ import annotations

from .context import *  # noqa: F403
from .benchmarks import PaperBenchmarkMixin
from .dashboard import PaperDashboardMixin
from .execution import PaperExecutionMixin
from .store import PaperStoreMixin


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
    ) -> None:
        super().__init__(repository, store, quote_provider, paper_store, quant_store)
        self.portfolio_decision = portfolio_decision or PortfolioDecisionService(
            repository, quant_store or store, store
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
        _, shadow = self.portfolio_decision.sources(as_of)
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
    ) -> dict:
        date.fromisoformat(as_of)
        account = self.account(account_id) if account_id else self.default_account()
        strategy = account["strategy"]
        defaults = strategy["config"]
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
                "SELECT MAX(fill_date) FROM paper_order WHERE account_id=? AND status='filled'",
                (account["account_id"],),
            ).fetchone()[0]
        if latest_fill and as_of < latest_fill:
            raise ValueError(
                f"研究日 {as_of} 早于账户最近成交日 {latest_fill}，禁止回溯重算当前账户"
            )
        _, shadow = self.portfolio_decision.sources(as_of)
        strategy_version = f'{strategy["strategy_id"]}@{strategy["version"]}'
        existing = self._run_for_date(account["account_id"], as_of, strategy_version)
        if existing:
            self._supersede_prior_runs(
                account["account_id"], as_of, existing["run_id"], strategy_version
            )
            return {**existing, "reused": True}

        positions = self._positions(account["account_id"], as_of, shadow["snapshot_id"])
        run_id, now = str(uuid.uuid4()), utc_now()
        plan = self.portfolio_decision.build_plan(
            account=account,
            positions=positions,
            as_of=as_of,
            run_id=run_id,
            created_at=now,
            top_n=top_n,
            hold_rank_buffer=hold_rank_buffer,
            target_gross_exposure=target_gross_exposure,
            max_position_weight=max_position_weight,
            max_industry_weight=max_industry_weight,
            max_pair_correlation=max_pair_correlation,
            strategy=strategy,
        )
        with self.paper_store.connect() as conn:
            conn.execute(
                """INSERT INTO paper_daily_run
                (run_id, account_id, as_of, factor_snapshot_id, shadow_snapshot_id, strategy_version,
                 status, config_json, market_summary_json, snapshot_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'awaiting_review', ?, ?, ?, ?)""",
                (
                    run_id,
                    account["account_id"],
                    as_of,
                    plan["factor_snapshot_id"],
                    plan["shadow_snapshot_id"],
                    plan.get("strategy_version", strategy_version),
                    json.dumps(plan["config"], ensure_ascii=False, sort_keys=True),
                    json.dumps(plan["market_summary"], ensure_ascii=False, sort_keys=True),
                    plan["snapshot_hash"],
                    now,
                ),
            )
            conn.executemany(
                """INSERT INTO paper_order
                (order_id, run_id, account_id, security_code, side, quantity, reference_price,
                 target_weight, reason_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?)""",
                [
                    (
                        order["order_id"],
                        run_id,
                        account["account_id"],
                        order["security_code"],
                        order["side"],
                        order["quantity"],
                        order["reference_price"],
                        order["target_weight"],
                        json.dumps(order["reason"], ensure_ascii=False, sort_keys=True),
                        now,
                    )
                    for order in plan["orders"]
                ],
            )
        self._supersede_prior_runs(account["account_id"], as_of, run_id, strategy_version)
        self._refresh_run_status(run_id)
        self._mark_nav(account, as_of)
        return {**self.run(run_id), "reused": False}


class PaperTradingService(PaperExecutionService):
    """Backward-compatible facade used by the existing API and scripts."""

    pass
