from __future__ import annotations

from .context import *  # noqa: F403
from .context import _normalize_paper_quote_code
from zoneinfo import ZoneInfo


def _market_rules(config: dict | None = None) -> ChinaAMarketRules:
    values = config or {}
    return ChinaAMarketRules(ChinaAConfig(
        commission_rate=float(values.get("commission_rate", COMMISSION_RATE)),
        minimum_commission=float(
            values.get("minimum_commission", values.get("min_commission", MIN_COMMISSION))
        ),
        stamp_duty_rate=float(
            values.get("stamp_duty_rate", values.get("sell_stamp_duty_rate", SELL_STAMP_DUTY_RATE))
        ),
        transfer_fee_rate=float(values.get("transfer_fee_rate", 0.00001)),
        slippage_rate=float(values.get("slippage_rate", SLIPPAGE_RATE)),
        lot_size=int(values.get("lot_size", 100)),
        max_volume_participation=float(values.get("max_volume_participation", 0.1)),
    ))


class PaperExecutionMixin:
    def position_codes(self, account_id: str | None = None) -> list[str]:
        account = self.account(account_id) if account_id else self.default_account()
        with self.paper_store.connect() as conn:
            rows = conn.execute(
                """SELECT security_code FROM paper_position
                   WHERE account_id=? AND quantity>0 ORDER BY security_code""",
                (account["account_id"],),
            ).fetchall()
        return [row["security_code"] for row in rows]

    def _price(self, code: str, as_of: str, field: str = "close", *, strictly_after: str | None = None) -> tuple[str, float] | None:
        rows = self.repository.get_history(code, start=strictly_after, end=as_of, limit=3000)
        for row in reversed(rows):
            if strictly_after and row["time"] <= strictly_after:
                continue
            value = row.get(field)
            if value is not None and float(value) > 0:
                return row["time"], float(value)
        return None

    def _execution_quote(
        self, code: str, proposal_as_of: str, execution_date: str, side: str,
        config: dict | None = None,
    ) -> tuple[tuple[str, float] | None, str | None]:
        rules = _market_rules(config)
        try:
            rows = self.repository.get_history(code, start=proposal_as_of, end=execution_date, limit=3000)
        except KeyError:
            return None, "missing_local_price_table"
        previous_close = None
        last_reason = "no_later_trading_day"
        for row in rows:
            close = float(row["close"]) if row.get("close") is not None else None
            if row["time"] <= proposal_as_of:
                if close and close > 0:
                    previous_close = close
                continue
            try:
                open_price = float(row["open"])
                high = float(row["high"])
                low = float(row["low"])
                volume = float(row["volume"])
            except (TypeError, ValueError):
                last_reason = "suspended_or_incomplete_quote"
                if close and close > 0:
                    previous_close = close
                continue
            if min(open_price, high, low, close or 0) <= 0 or volume <= 0:
                last_reason = "suspended_or_incomplete_quote"
                if close and close > 0:
                    previous_close = close
                continue
            one_price = abs(high - low) <= max(abs(close or open_price), 1.0) * 1e-8
            if one_price and previous_close:
                blocked = rules.tradability_reason(
                    security_code=code,
                    side=side,
                    row=dict(row),
                    previous_close=previous_close,
                    trading_date=row["time"],
                )
                if blocked in {"limit_up", "limit_down"}:
                    last_reason = f"one_price_{blocked}"
                    previous_close = close
                    continue
            return (row["time"], open_price), None
        return None, last_reason

    def _realtime_scope(
        self, account: dict, approved_run_id: str | None = None
    ) -> dict[str, list[str]]:
        scope: dict[str, list[str]] = {
            code: ["benchmark_comparison"] for code in PAPER_BENCHMARKS
        }
        with self.paper_store.connect() as conn:
            positions = conn.execute(
                "SELECT security_code FROM paper_position WHERE account_id=? AND quantity>0",
                (account["account_id"],),
            ).fetchall()
            pending_sql = (
                "SELECT DISTINCT security_code FROM paper_proposal "
                "WHERE account_id=? AND status='approved'"
            )
            pending_params: list[str] = [account["account_id"]]
            if approved_run_id:
                pending_sql += " AND run_key=?"
                pending_params.append(approved_run_id)
            pending = conn.execute(pending_sql, pending_params).fetchall()
        for row in positions:
            scope.setdefault(row["security_code"], []).append("position")
        for row in pending:
            scope.setdefault(row["security_code"], []).append("approved_order")
        return scope

    @staticmethod
    def _validate_realtime_quote(quote: dict) -> tuple[dict | None, list[str]]:
        issues = []
        quote_time = quote.get("quote_time")
        try:
            parsed_time = datetime.fromisoformat(str(quote_time))
        except (TypeError, ValueError):
            parsed_time = None
            issues.append("invalid_quote_time")
        numeric: dict[str, float | None] = {}
        for field in ("open", "latest", "high", "low", "volume", "amount", "previous_close"):
            try:
                value = float(quote.get(field))
                numeric[field] = value if math.isfinite(value) else None
            except (TypeError, ValueError):
                numeric[field] = None
        for field in ("open", "latest", "high", "low", "previous_close"):
            if numeric[field] is None or numeric[field] <= 0:
                issues.append(f"invalid_{field}")
        for field in ("volume", "amount"):
            if numeric[field] is None or numeric[field] < 0:
                issues.append(f"invalid_{field}")
        if numeric["high"] and numeric["low"] and numeric["high"] < numeric["low"]:
            issues.append("high_below_low")
        if issues:
            return None, issues
        normalized = {
            "security_code": _normalize_paper_quote_code(quote["security_code"]),
            "quote_time": parsed_time.isoformat(sep=" "),
            "trading_date": parsed_time.date().isoformat(),
            **{field: numeric[field] for field in numeric},
            "source": "iFinD THS_RQ",
        }
        return normalized, []

    def refresh_realtime_quotes(
        self, account_id: str | None = None, *, approved_run_id: str | None = None
    ) -> dict:
        if self.quote_provider is None:
            raise RuntimeError("实时行情服务未配置")
        account = self.account(account_id) if account_id else self.default_account()
        scope = self._realtime_scope(account, approved_run_id)
        received_at = utc_now()
        raw_quotes = self.quote_provider.get_realtime_quotes(list(scope))
        saved, invalid = [], []
        returned_codes = set()
        for raw_quote in raw_quotes:
            code = _normalize_paper_quote_code(raw_quote.get("security_code", ""))
            returned_codes.add(code)
            quote, issues = self._validate_realtime_quote(raw_quote)
            if quote is None:
                invalid.append({"security_code": code, "issues": issues})
                continue
            purposes = sorted(set(scope.get(code, ["unknown"])))
            saved.append({**quote, "purpose": purposes})
        missing = sorted(set(scope) - returned_codes)
        return {
            "account_id": account["account_id"],
            "received_at": received_at,
            "requested_codes": list(scope),
            "quotes": saved,
            "missing_codes": missing,
            "invalid_quotes": invalid,
            "source": "iFinD THS_RQ",
        }

    def _security_metadata(self, codes: list[str]) -> dict[str, dict]:
        unique_codes = sorted(set(codes))
        if not unique_codes:
            return {}
        placeholders = ",".join("?" for _ in unique_codes)
        with self.store.connect() as conn:
            rows = conn.execute(
                f"""SELECT security_code, security_name, industry_l1
                    FROM security_master WHERE security_code IN ({placeholders})""",
                unique_codes,
            ).fetchall()
        return {row["security_code"]: dict(row) for row in rows}

    def _positions(self, account_id: str, as_of: str, shadow_snapshot_id: str | None = None,
                   price_overrides: dict[str, dict] | None = None) -> list[dict]:
        with self.paper_store.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM paper_position
                   WHERE account_id=? AND quantity>0 ORDER BY security_code""",
                (account_id,),
            ).fetchall()
        metadata = self._security_metadata([row["security_code"] for row in rows])
        items = []
        for row in rows:
            item = dict(row)
            profile = metadata.get(item["security_code"], {})
            item["security_name"] = profile.get("security_name") or item["security_code"]
            item["industry_l1"] = profile.get("industry_l1")
            realtime = (price_overrides or {}).get(item["security_code"])
            price = None if realtime else self._price(item["security_code"], as_of)
            close = realtime["latest"] if realtime else (price[1] if price else None)
            previous_close = realtime.get("previous_close") if realtime else None
            if not realtime and price:
                previous_day = (date.fromisoformat(price[0]) - timedelta(days=1)).isoformat()
                previous_price = self._price(item["security_code"], previous_day)
                previous_close = previous_price[1] if previous_price else None
            item["close"] = close
            item["previous_close"] = previous_close
            item["price_source"] = realtime["source"] if realtime else "local_daily_close"
            item["quote_time"] = realtime["quote_time"] if realtime else (price[0] if price else None)
            item["market_value"] = close * item["quantity"] if close else None
            item["unrealized_pnl"] = (close - item["average_cost"]) * item["quantity"] if close else None
            item["unrealized_return"] = (
                close / item["average_cost"] - 1 if close and item["average_cost"] else None
            )
            item["daily_pnl"] = (
                (close - previous_close) * item["quantity"]
                if close and previous_close else None
            )
            signal = self.quant_store.current_shadow_signal(
                shadow_snapshot_id, item["security_code"]
            ) if shadow_snapshot_id else None
            item["shadow_rank"] = signal["cross_section_rank"] if signal else None
            items.append(item)
        return items

    def _mark_nav(self, account: dict, trading_date: str, turnover: float = 0.0,
                  price_overrides: dict[str, dict] | None = None) -> dict:
        positions = self._positions(account["account_id"], trading_date, price_overrides=price_overrides)
        market_value = sum(item["market_value"] or 0 for item in positions)
        nav = account["current_cash"] + market_value
        with self.paper_store.connect() as conn:
            prior = conn.execute(
                "SELECT * FROM paper_nav_snapshot WHERE account_id=? AND trading_date<? ORDER BY trading_date DESC LIMIT 1",
                (account["account_id"], trading_date),
            ).fetchone()
            peak_row = conn.execute(
                "SELECT MAX(total_equity) FROM paper_nav_snapshot WHERE account_id=? AND trading_date<?",
                (account["account_id"], trading_date),
            ).fetchone()
            previous_nav = prior["total_equity"] if prior else account["initial_cash"]
            peak = max(float(peak_row[0] or account["initial_cash"]), nav)
            daily_return = nav / previous_nav - 1 if previous_nav else None
            cumulative_return = nav / account["initial_cash"] - 1
            drawdown = nav / peak - 1 if peak else 0.0
            conn.execute(
                """INSERT INTO paper_nav_snapshot
                (account_id, trading_date, current_cash, market_value, total_equity, daily_return,
                 cumulative_return, drawdown,
                 gross_exposure, turnover, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, trading_date) DO UPDATE SET
                current_cash=excluded.current_cash, market_value=excluded.market_value,
                total_equity=excluded.total_equity,
                daily_return=excluded.daily_return, cumulative_return=excluded.cumulative_return,
                drawdown=excluded.drawdown, gross_exposure=excluded.gross_exposure,
                turnover=paper_nav_snapshot.turnover + excluded.turnover,
                created_at=excluded.created_at""",
                (account["account_id"], trading_date, account["current_cash"], market_value, nav,
                 daily_return, cumulative_return, drawdown, market_value / nav if nav else 0,
                 turnover, utc_now()),
            )
        return {"trading_date": trading_date, "current_cash": account["current_cash"],
                "cash": account["current_cash"], "market_value": market_value,
                "total_equity": nav, "nav": nav, "daily_return": daily_return, "cumulative_return": cumulative_return,
                "drawdown": drawdown, "gross_exposure": market_value / nav if nav else 0,
                "turnover": turnover}

    def _latest_active_run_id(self, account_id: str) -> str | None:
        with self.paper_store.connect() as conn:
            row = conn.execute(
                """SELECT run_key FROM paper_proposal WHERE account_id=?
                   ORDER BY proposal_date DESC, proposal_id DESC LIMIT 1""",
                (account_id,),
            ).fetchone()
        return row["run_key"] if row else None

    def run(self, run_id: str) -> dict:
        manifest = self.run_store.manifest(run_id)
        with self.paper_store.connect() as conn:
            orders = conn.execute(
                """SELECT * FROM paper_proposal WHERE run_key=?
                   ORDER BY CASE side WHEN 'sell' THEN 0 ELSE 1 END, proposal_id""",
                (run_id,),
            ).fetchall()
        metadata = self._security_metadata([order["security_code"] for order in orders])
        result_path = self.run_store.path(run_id) / "order_proposals.json"
        details = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {"orders": []}
        detail_rows = details.get("orders", [])
        item = {
            **manifest, "run_id": run_id, "as_of": manifest["trading_date"],
            "strategy_version": f'{manifest.get("strategy", {}).get("strategy_id", "unknown")}@{manifest.get("strategy", {}).get("version", "")}',
            "orders": [],
        }
        for index, order in enumerate(orders):
            decoded = dict(order)
            decoded["order_id"] = decoded["proposal_id"]
            if index < len(detail_rows):
                decoded["target_weight"] = detail_rows[index].get("target_weight")
                decoded["reason"] = detail_rows[index].get("reason", {})
            decoded["security_name"] = (
                metadata.get(decoded["security_code"], {}).get("security_name")
                or decoded["security_code"]
            )
            item["orders"].append(decoded)
        return item

    def order_ledger(self, account_id: str | None = None, limit: int = 500) -> dict:
        account = self.account(account_id) if account_id else self.default_account()
        with self.paper_store.connect() as conn:
            latest_run = conn.execute(
                """SELECT run_key, proposal_date FROM paper_proposal WHERE account_id=?
                   ORDER BY proposal_date DESC, proposal_id DESC LIMIT 1""",
                (account["account_id"],),
            ).fetchone()
            rows = conn.execute(
                """SELECT * FROM paper_proposal WHERE account_id=?
                   ORDER BY proposal_date DESC, proposal_id DESC LIMIT ?""",
                (account["account_id"], limit),
            ).fetchall()
            trades = conn.execute(
                """SELECT * FROM paper_trade WHERE account_id=?
                   ORDER BY traded_at DESC, trade_id DESC LIMIT ?""",
                (account["account_id"], limit),
            ).fetchall()
        metadata = self._security_metadata(
            [row["security_code"] for row in rows] + [row["security_code"] for row in trades]
        )
        items = []
        for row in rows:
            item = dict(row)
            item.update({"order_id": item["proposal_id"], "run_id": item["run_key"],
                         "as_of": item["proposal_date"], "run_status": self.run(item["run_key"])["status"]})
            item["security_name"] = (
                metadata.get(item["security_code"], {}).get("security_name")
                or item["security_code"]
            )
            items.append(item)
        trade_items = []
        for row in trades:
            item = dict(row)
            item["security_name"] = (
                metadata.get(item["security_code"], {}).get("security_name")
                or item["security_code"]
            )
            trade_items.append(item)
        return {
            "account_id": account["account_id"],
            "latest_run_id": latest_run["run_key"] if latest_run else None,
            "latest_as_of": latest_run["proposal_date"] if latest_run else None,
            "items": items, "proposals": items,
            "trades": trade_items,
        }

    def review_order(
        self, order_id: str, decision: str, _reviewer: str = "", _note: str = ""
    ) -> dict:
        status = "approved" if decision == "approve" else "rejected"
        with self.paper_store.connect() as conn:
            row = conn.execute("SELECT * FROM paper_proposal WHERE proposal_id=?", (order_id,)).fetchone()
            if row is None:
                raise KeyError("模拟订单不存在")
            if row["status"] != "proposed":
                raise ValueError("该订单已经完成审批")
            conn.execute(
                "UPDATE paper_proposal SET status=?, reviewed_at=? WHERE proposal_id=?",
                (status, utc_now(), order_id),
            )
        self._refresh_run_status(row["run_key"])
        return self.run(row["run_key"])

    def _refresh_run_status(self, run_id: str) -> None:
        with self.paper_store.connect() as conn:
            statuses = [row[0] for row in conn.execute(
                "SELECT status FROM paper_proposal WHERE run_key=?", (run_id,)
            ).fetchall()]
            if "proposed" in statuses:
                status = "awaiting_review"
            elif "approved" in statuses:
                status = "approved_waiting_execution"
            else:
                status = "completed"
        self.run_store.update_manifest(run_id, status=status)

    def _fill_order(self, account_id: str, order: dict, fill_date: str,
                    open_price: float) -> tuple[dict | None, str | None]:
        config = self.run_store.manifest(order["run_key"]).get("config", {})
        rules = _market_rules(config)
        fill_price = rules.execution_price(order["side"], open_price)
        quantity = int(order["quantity"])
        gross = fill_price * quantity
        fees = rules.fee(order["side"], quantity, fill_price)
        with self.paper_store.connect() as conn:
            current = conn.execute(
                "SELECT * FROM paper_position WHERE account_id=? AND security_code=?",
                (account_id, order["security_code"]),
            ).fetchone()
            fresh_account = conn.execute(
                "SELECT * FROM paper_account WHERE account_id=?", (account_id,)
            ).fetchone()
            if order["side"] == "buy":
                max_positions = int(config.get("max_positions") or 0)
                is_new_position = current is None or int(current["quantity"] or 0) <= 0
                position_count = conn.execute(
                    "SELECT COUNT(*) FROM paper_position WHERE account_id=? AND quantity>0",
                    (account_id,),
                ).fetchone()[0]
                if is_new_position and max_positions and position_count >= max_positions:
                    return None, "持仓数量上限尚未通过卖出释放"
                total = gross + fees
                if fresh_account["current_cash"] + 1e-8 < total:
                    return None, "可用现金不足"
                old_qty = current["quantity"] if current else 0
                old_cost = current["average_cost"] if current else 0
                new_qty = old_qty + quantity
                avg_cost = (old_qty * old_cost + total) / new_qty
                conn.execute(
                    "UPDATE paper_account SET current_cash=current_cash-?, updated_at=? WHERE account_id=?",
                    (total, utc_now(), account_id),
                )
                conn.execute(
                    """INSERT INTO paper_position
                    (account_id, security_code, quantity, available_quantity, average_cost,
                     last_buy_date, updated_at)
                    VALUES (?, ?, ?, 0, ?, ?, ?)
                    ON CONFLICT(account_id, security_code) DO UPDATE SET
                    quantity=?, average_cost=?, last_buy_date=?, updated_at=?""",
                    (account_id, order["security_code"], quantity, avg_cost, fill_date, utc_now(),
                     new_qty, avg_cost, fill_date, utc_now()),
                )
            else:
                if current is None or current["available_quantity"] < quantity:
                    return None, "可卖数量不足"
                proceeds = gross - fees
                conn.execute(
                    "UPDATE paper_account SET current_cash=current_cash+?, updated_at=? WHERE account_id=?",
                    (proceeds, utc_now(), account_id),
                )
                if current["quantity"] == quantity:
                    conn.execute(
                        "DELETE FROM paper_position WHERE account_id=? AND security_code=?",
                        (account_id, order["security_code"]),
                    )
                else:
                    conn.execute(
                        """UPDATE paper_position SET quantity=quantity-?,
                        available_quantity=available_quantity-?, updated_at=?
                        WHERE account_id=? AND security_code=?""",
                        (quantity, quantity, utc_now(), account_id, order["security_code"]),
                    )
            conn.execute(
                """INSERT INTO paper_trade
                (account_id, security_code, side, quantity, price, fees, traded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (account_id, order["security_code"], order["side"], quantity,
                 fill_price, fees, fill_date),
            )
            trade_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "UPDATE paper_proposal SET status='filled' WHERE proposal_id=?",
                (order["proposal_id"],),
            )
        self._refresh_run_status(order["run_key"])
        return {
            "trade_id": trade_id, "order_id": order["proposal_id"], "security_code": order["security_code"],
            "side": order["side"], "quantity": quantity, "fill_date": fill_date,
            "fill_price": fill_price, "gross_amount": gross, "fees": fees,
        }, None

    def settle(self, *, execution_date: str, account_id: str | None = None) -> dict:
        date.fromisoformat(execution_date)
        account = self.account(account_id) if account_id else self.default_account()
        active_run_id = self._latest_active_run_id(account["account_id"])
        filled, skipped, turnover = [], [], 0.0
        with self.paper_store.connect() as conn:
            conn.execute(
                "UPDATE paper_position SET available_quantity=quantity, updated_at=? WHERE account_id=? AND COALESCE(last_buy_date, '')<?",
                (utc_now(), account["account_id"], execution_date),
            )
            rows = conn.execute(
                """SELECT * FROM paper_proposal
                WHERE account_id=? AND run_key=? AND status='approved' AND proposal_date<?
                ORDER BY CASE side WHEN 'sell' THEN 0 ELSE 1 END, proposal_id""",
                (account["account_id"], active_run_id, execution_date),
            ).fetchall()
        for raw in rows:
            order = dict(raw)
            price_item, execution_reason = self._execution_quote(
                order["security_code"], order["proposal_date"], execution_date, order["side"],
                self.run_store.manifest(order["run_key"]).get("config", {}),
            )
            if not price_item:
                skipped.append({"order_id": order["proposal_id"], "reason": execution_reason})
                continue
            fill_date, open_price = price_item
            fill, reason = self._fill_order(account["account_id"], order, fill_date, open_price)
            if reason:
                skipped.append({"order_id": order["proposal_id"], "reason": reason})
                continue
            turnover += fill["gross_amount"]
            filled.append(fill)
        account = self.account(account["account_id"])
        nav = self._mark_nav(account, execution_date, turnover)
        return {
            "account": account, "run_id": active_run_id,
            "filled": filled, "skipped": skipped, "nav": nav,
        }

    def settle_realtime(
        self, account_id: str | None = None, *, now: datetime | None = None
    ) -> dict:
        shanghai_now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
        if shanghai_now.tzinfo is None:
            shanghai_now = shanghai_now.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        else:
            shanghai_now = shanghai_now.astimezone(ZoneInfo("Asia/Shanghai"))
        current_time = shanghai_now.time().replace(tzinfo=None)
        morning_open = (
            datetime.strptime("09:30", "%H:%M").time()
            <= current_time
            <= datetime.strptime("11:30", "%H:%M").time()
        )
        afternoon_open = (
            datetime.strptime("13:00", "%H:%M").time()
            <= current_time
            <= datetime.strptime("15:00", "%H:%M").time()
        )
        if not (morning_open or afternoon_open):
            raise ValueError(
                "实时模拟撮合仅允许交易日 09:30–11:30、13:00–15:00"
            )
        account = self.account(account_id) if account_id else self.default_account()
        active_run_id = self._latest_active_run_id(account["account_id"])
        refresh = self.refresh_realtime_quotes(
            account["account_id"], approved_run_id=active_run_id
        )
        quotes = {item["security_code"]: item for item in refresh["quotes"]}
        if not quotes:
            raise ValueError("实时行情为空，无法执行模拟撮合")
        trading_dates = [quote["trading_date"] for quote in quotes.values()]
        execution_date = max(trading_dates)
        if execution_date != shanghai_now.date().isoformat():
            raise ValueError("当前不是可撮合交易日，实时行情日期与今日不一致")
        filled, skipped, turnover = [], [], 0.0
        with self.paper_store.connect() as conn:
            conn.execute(
                "UPDATE paper_position SET available_quantity=quantity, updated_at=? "
                "WHERE account_id=? AND COALESCE(last_buy_date, '')<?",
                (utc_now(), account["account_id"], execution_date),
            )
            rows = conn.execute(
                """SELECT * FROM paper_proposal
                WHERE account_id=? AND run_key=? AND status='approved'
                ORDER BY CASE side WHEN 'sell' THEN 0 ELSE 1 END, proposal_id""",
                (account["account_id"], active_run_id),
            ).fetchall()
        for raw in rows:
            order = dict(raw)
            quote = quotes.get(order["security_code"])
            if not quote:
                skipped.append({"order_id": order["proposal_id"], "reason": "missing_realtime_quote"})
                continue
            fill_date = quote["trading_date"]
            if fill_date <= order["proposal_date"]:
                skipped.append({"order_id": order["proposal_id"], "reason": "no_later_trading_day"})
                continue
            reviewed_at = datetime.fromisoformat(order["reviewed_at"])
            quote_time = datetime.fromisoformat(quote["quote_time"])
            if reviewed_at > quote_time:
                skipped.append({"order_id": order["proposal_id"], "reason": "quote_before_approval"})
                continue
            if quote["volume"] <= 0:
                skipped.append({"order_id": order["proposal_id"], "reason": "suspended_or_preopen_quote"})
                continue
            rules = _market_rules(self.run_store.manifest(order["run_key"]).get("config", {}))
            one_price = abs(quote["high"] - quote["low"]) <= max(abs(quote["latest"]), 1.0) * 1e-8
            if one_price:
                blocked = rules.tradability_reason(
                    security_code=order["security_code"],
                    side=order["side"],
                    row={"open": quote["open"], "volume": quote["volume"]},
                    previous_close=quote["previous_close"],
                    trading_date=fill_date,
                )
                if blocked in {"limit_up", "limit_down"}:
                    skipped.append({
                        "order_id": order["proposal_id"],
                        "reason": f"one_price_{blocked}",
                    })
                    continue
            fill, reason = self._fill_order(
                account["account_id"], order, fill_date, quote["latest"]
            )
            if reason:
                skipped.append({"order_id": order["proposal_id"], "reason": reason})
                continue
            turnover += fill["gross_amount"]
            filled.append(fill)
        account = self.account(account["account_id"])
        nav = self._mark_nav(account, execution_date, turnover, quotes)
        return {
            "account": account, "run_id": active_run_id,
            "filled": filled, "skipped": skipped, "nav": nav, "realtime": refresh,
        }
