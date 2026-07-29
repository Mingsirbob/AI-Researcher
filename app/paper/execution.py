from __future__ import annotations

from .context import *  # noqa: F403
from .context import _normalize_paper_quote_code


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

    def _execution_quote(self, code: str, proposal_as_of: str, execution_date: str, side: str) -> tuple[tuple[str, float] | None, str | None]:
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
                if side == "buy" and open_price > previous_close:
                    last_reason = "one_price_limit_up"
                    previous_close = close
                    continue
                if side == "sell" and open_price < previous_close:
                    last_reason = "one_price_limit_down"
                    previous_close = close
                    continue
            return (row["time"], open_price), None
        return None, last_reason

    def _realtime_scope(self, account: dict) -> dict[str, list[str]]:
        scope: dict[str, list[str]] = {
            code: ["benchmark_comparison"] for code in PAPER_BENCHMARKS
        }
        scope.setdefault(account["benchmark_code"], []).append("account_benchmark")
        with self.paper_store.connect() as conn:
            positions = conn.execute(
                "SELECT security_code FROM paper_position WHERE account_id=? AND quantity>0",
                (account["account_id"],),
            ).fetchall()
            pending = conn.execute(
                "SELECT DISTINCT security_code FROM paper_order WHERE account_id=? AND status='approved'",
                (account["account_id"],),
            ).fetchall()
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

    def refresh_realtime_quotes(self, account_id: str | None = None) -> dict:
        if self.quote_provider is None:
            raise RuntimeError("实时行情服务未配置")
        account = self.account(account_id) if account_id else self.default_account()
        scope = self._realtime_scope(account)
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
            payload = {**quote, "account_id": account["account_id"], "purpose": purposes}
            snapshot_hash = canonical_hash(payload, compact=False)
            snapshot_id = snapshot_hash
            with self.paper_store.connect() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO paper_realtime_quote
                    (snapshot_id, account_id, security_code, quote_time, trading_date, received_at,
                     open, latest, high, low, volume, amount, previous_close, source,
                     purpose_json, snapshot_hash, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (snapshot_id, account["account_id"], code, quote["quote_time"], quote["trading_date"],
                     received_at, quote["open"], quote["latest"], quote["high"], quote["low"],
                     quote["volume"], quote["amount"], quote["previous_close"], quote["source"],
                     json.dumps(purposes, ensure_ascii=False), snapshot_hash,
                     json.dumps(raw_quote, ensure_ascii=False, sort_keys=True)),
                )
            saved.append({**quote, "snapshot_id": snapshot_id, "purpose": purposes})
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

    def _latest_realtime_quotes(self, account_id: str, codes: list[str] | None = None) -> list[dict]:
        params: list[Any] = [account_id]
        code_filter = ""
        if codes:
            code_filter = f" AND q.security_code IN ({','.join('?' for _ in codes)})"
            params.extend(codes)
        with self.paper_store.connect() as conn:
            rows = conn.execute(
                f"""SELECT q.* FROM paper_realtime_quote q
                WHERE q.account_id=? {code_filter}
                  AND q.received_at=(SELECT MAX(q2.received_at) FROM paper_realtime_quote q2
                                     WHERE q2.account_id=q.account_id AND q2.security_code=q.security_code)
                ORDER BY q.security_code""",
                params,
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["purpose"] = json.loads(item.pop("purpose_json"))
            item.pop("raw_json", None)
            items.append(item)
        return items

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
        nav = account["cash"] + market_value
        with self.paper_store.connect() as conn:
            prior = conn.execute(
                "SELECT * FROM paper_nav_snapshot WHERE account_id=? AND trading_date<? ORDER BY trading_date DESC LIMIT 1",
                (account["account_id"], trading_date),
            ).fetchone()
            peak_row = conn.execute(
                "SELECT MAX(nav) FROM paper_nav_snapshot WHERE account_id=? AND trading_date<?",
                (account["account_id"], trading_date),
            ).fetchone()
            previous_nav = prior["nav"] if prior else account["initial_cash"]
            peak = max(float(peak_row[0] or account["initial_cash"]), nav)
            daily_return = nav / previous_nav - 1 if previous_nav else None
            cumulative_return = nav / account["initial_cash"] - 1
            drawdown = nav / peak - 1 if peak else 0.0
            conn.execute(
                """INSERT INTO paper_nav_snapshot
                (account_id, trading_date, cash, market_value, nav, daily_return,
                 cumulative_return, benchmark_return, excess_return, drawdown,
                 gross_exposure, turnover, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
                ON CONFLICT(account_id, trading_date) DO UPDATE SET
                cash=excluded.cash, market_value=excluded.market_value, nav=excluded.nav,
                daily_return=excluded.daily_return, cumulative_return=excluded.cumulative_return,
                drawdown=excluded.drawdown, gross_exposure=excluded.gross_exposure,
                turnover=paper_nav_snapshot.turnover + excluded.turnover,
                created_at=excluded.created_at""",
                (account["account_id"], trading_date, account["cash"], market_value, nav,
                 daily_return, cumulative_return, drawdown, market_value / nav if nav else 0,
                 turnover, utc_now()),
            )
        return {"trading_date": trading_date, "cash": account["cash"], "market_value": market_value,
                "nav": nav, "daily_return": daily_return, "cumulative_return": cumulative_return,
                "drawdown": drawdown, "gross_exposure": market_value / nav if nav else 0,
                "turnover": turnover, "benchmark_return": None, "excess_return": None}

    def _run_for_date(self, account_id: str, as_of: str, strategy_version: str) -> dict | None:
        with self.paper_store.connect() as conn:
            row = conn.execute(
                "SELECT run_id FROM paper_daily_run WHERE account_id=? AND as_of=? AND strategy_version=?",
                (account_id, as_of, strategy_version),
            ).fetchone()
        return self.run(row["run_id"]) if row else None

    def _supersede_prior_runs(
        self, account_id: str, as_of: str, active_run_id: str, strategy_version: str
    ) -> None:
        now = utc_now()
        with self.paper_store.connect() as conn:
            conn.execute(
                """UPDATE paper_order SET status='cancelled',
                   review_note=COALESCE(review_note || '；', '') || ?,
                   reviewed_at=COALESCE(reviewed_at, ?)
                   WHERE account_id=? AND run_id<>? AND status IN ('proposed','approved')
                     AND fill_date IS NULL
                     AND run_id IN (SELECT run_id FROM paper_daily_run WHERE account_id=? AND as_of=?)""",
                (f'已由 {strategy_version} 同日批次替代', now, account_id, active_run_id, account_id, as_of),
            )
            conn.execute(
                """UPDATE paper_daily_run SET status='superseded'
                   WHERE account_id=? AND as_of=? AND run_id<>? AND status<>'completed'""",
                (account_id, as_of, active_run_id),
            )

    def run(self, run_id: str) -> dict:
        with self.paper_store.connect() as conn:
            row = conn.execute("SELECT * FROM paper_daily_run WHERE run_id=?", (run_id,)).fetchone()
            orders = conn.execute(
                """SELECT * FROM paper_order WHERE run_id=?
                   ORDER BY CASE side WHEN 'sell' THEN 0 ELSE 1 END, created_at""",
                (run_id,),
            ).fetchall()
        if row is None:
            raise KeyError("模拟研究批次不存在")
        metadata = self._security_metadata([order["security_code"] for order in orders])
        item = self._decode_json(dict(row), "config", "market_summary")
        item["orders"] = []
        for order in orders:
            decoded = self._decode_json(dict(order), "reason")
            decoded["security_name"] = (
                metadata.get(decoded["security_code"], {}).get("security_name")
                or decoded["security_code"]
            )
            item["orders"].append(decoded)
        return item

    def review_order(self, order_id: str, decision: str, reviewer: str, note: str) -> dict:
        status = "approved" if decision == "approve" else "rejected"
        with self.paper_store.connect() as conn:
            row = conn.execute("SELECT * FROM paper_order WHERE order_id=?", (order_id,)).fetchone()
            if row is None:
                raise KeyError("模拟订单不存在")
            if row["status"] != "proposed":
                raise ValueError("该订单已经完成审批")
            conn.execute(
                "UPDATE paper_order SET status=?, reviewer=?, review_note=?, reviewed_at=? WHERE order_id=?",
                (status, reviewer, note, utc_now(), order_id),
            )
        self._refresh_run_status(row["run_id"])
        return self.run(row["run_id"])

    def _refresh_run_status(self, run_id: str) -> None:
        with self.paper_store.connect() as conn:
            run = conn.execute("SELECT status FROM paper_daily_run WHERE run_id=?", (run_id,)).fetchone()
            if run is None or run["status"] == "superseded":
                return
            statuses = [row[0] for row in conn.execute(
                "SELECT status FROM paper_order WHERE run_id=?", (run_id,)
            ).fetchall()]
            if "proposed" in statuses:
                status = "awaiting_review"
            elif "approved" in statuses:
                status = "approved_waiting_execution"
            else:
                status = "completed"
            conn.execute("UPDATE paper_daily_run SET status=? WHERE run_id=?", (status, run_id))

    def _fill_order(self, account_id: str, order: dict, fill_date: str, open_price: float,
                    execution_quote_id: str | None = None) -> tuple[dict | None, str | None]:
        fill_price = open_price * (1 + SLIPPAGE_RATE if order["side"] == "buy" else 1 - SLIPPAGE_RATE)
        quantity = int(order["quantity"])
        gross = fill_price * quantity
        fees = max(MIN_COMMISSION, gross * COMMISSION_RATE)
        if order["side"] == "sell":
            fees += gross * SELL_STAMP_DUTY_RATE
        with self.paper_store.connect() as conn:
            current = conn.execute(
                "SELECT * FROM paper_position WHERE account_id=? AND security_code=?",
                (account_id, order["security_code"]),
            ).fetchone()
            fresh_account = conn.execute(
                "SELECT * FROM paper_account WHERE account_id=?", (account_id,)
            ).fetchone()
            if order["side"] == "buy":
                config_row = conn.execute(
                    "SELECT config_json FROM paper_daily_run WHERE run_id=?", (order["run_id"],)
                ).fetchone()
                config = json.loads(config_row["config_json"]) if config_row else {}
                max_positions = int(config.get("max_positions") or 0)
                is_new_position = current is None or int(current["quantity"] or 0) <= 0
                position_count = conn.execute(
                    "SELECT COUNT(*) FROM paper_position WHERE account_id=? AND quantity>0",
                    (account_id,),
                ).fetchone()[0]
                if is_new_position and max_positions and position_count >= max_positions:
                    return None, "持仓数量上限尚未通过卖出释放"
                total = gross + fees
                if fresh_account["cash"] + 1e-8 < total:
                    return None, "可用现金不足"
                old_qty = current["quantity"] if current else 0
                old_cost = current["average_cost"] if current else 0
                new_qty = old_qty + quantity
                avg_cost = (old_qty * old_cost + total) / new_qty
                conn.execute(
                    "UPDATE paper_account SET cash=cash-?, updated_at=? WHERE account_id=?",
                    (total, utc_now(), account_id),
                )
                conn.execute(
                    """INSERT INTO paper_position
                    (account_id, security_code, quantity, available_quantity, average_cost,
                     realized_pnl, last_buy_date, updated_at)
                    VALUES (?, ?, ?, 0, ?, 0, ?, ?)
                    ON CONFLICT(account_id, security_code) DO UPDATE SET
                    quantity=?, average_cost=?, last_buy_date=?, updated_at=?""",
                    (account_id, order["security_code"], quantity, avg_cost, fill_date, utc_now(),
                     new_qty, avg_cost, fill_date, utc_now()),
                )
            else:
                if current is None or current["available_quantity"] < quantity:
                    return None, "可卖数量不足"
                proceeds = gross - fees
                realized = (fill_price - current["average_cost"]) * quantity - fees
                conn.execute(
                    "UPDATE paper_account SET cash=cash+?, updated_at=? WHERE account_id=?",
                    (proceeds, utc_now(), account_id),
                )
                conn.execute(
                    """UPDATE paper_position SET quantity=quantity-?,
                    available_quantity=available_quantity-?, realized_pnl=realized_pnl+?,
                    updated_at=? WHERE account_id=? AND security_code=?""",
                    (quantity, quantity, realized, utc_now(), account_id, order["security_code"]),
                )
            conn.execute(
                """UPDATE paper_order SET status='filled', fill_date=?, fill_price=?,
                gross_amount=?, fees=?, execution_quote_id=? WHERE order_id=?""",
                (fill_date, fill_price, gross, fees, execution_quote_id, order["order_id"]),
            )
        self._refresh_run_status(order["run_id"])
        return {
            "order_id": order["order_id"], "security_code": order["security_code"],
            "side": order["side"], "quantity": quantity, "fill_date": fill_date,
            "fill_price": fill_price, "gross_amount": gross, "fees": fees,
            "execution_quote_id": execution_quote_id,
        }, None

    def settle(self, *, execution_date: str, account_id: str | None = None) -> dict:
        date.fromisoformat(execution_date)
        account = self.account(account_id) if account_id else self.default_account()
        filled, skipped, turnover = [], [], 0.0
        with self.paper_store.connect() as conn:
            conn.execute(
                "UPDATE paper_position SET available_quantity=quantity, updated_at=? WHERE account_id=? AND COALESCE(last_buy_date, '')<?",
                (utc_now(), account["account_id"], execution_date),
            )
            rows = conn.execute(
                """SELECT o.*, r.as_of FROM paper_order o JOIN paper_daily_run r ON r.run_id=o.run_id
                WHERE o.account_id=? AND o.status='approved' AND r.as_of<?
                ORDER BY CASE o.side WHEN 'sell' THEN 0 ELSE 1 END, o.created_at""",
                (account["account_id"], execution_date),
            ).fetchall()
        for raw in rows:
            order = dict(raw)
            price_item, execution_reason = self._execution_quote(
                order["security_code"], order["as_of"], execution_date, order["side"]
            )
            if not price_item:
                skipped.append({"order_id": order["order_id"], "reason": execution_reason})
                continue
            fill_date, open_price = price_item
            fill, reason = self._fill_order(account["account_id"], order, fill_date, open_price)
            if reason:
                skipped.append({"order_id": order["order_id"], "reason": reason})
                continue
            turnover += fill["gross_amount"]
            filled.append(fill)
        account = self.account(account["account_id"])
        nav = self._mark_nav(account, execution_date, turnover)
        return {"account": account, "filled": filled, "skipped": skipped, "nav": nav}

    def settle_realtime(self, account_id: str | None = None) -> dict:
        refresh = self.refresh_realtime_quotes(account_id)
        account = self.account(refresh["account_id"])
        quotes = {item["security_code"]: item for item in refresh["quotes"]}
        trading_dates = [quote["trading_date"] for quote in quotes.values()]
        execution_date = max(trading_dates) if trading_dates else date.today().isoformat()
        filled, skipped, turnover = [], [], 0.0
        with self.paper_store.connect() as conn:
            conn.execute(
                "UPDATE paper_position SET available_quantity=quantity, updated_at=? "
                "WHERE account_id=? AND COALESCE(last_buy_date, '')<?",
                (utc_now(), account["account_id"], execution_date),
            )
            rows = conn.execute(
                """SELECT o.*, r.as_of FROM paper_order o
                JOIN paper_daily_run r ON r.run_id=o.run_id
                WHERE o.account_id=? AND o.status='approved'
                ORDER BY CASE o.side WHEN 'sell' THEN 0 ELSE 1 END, o.created_at""",
                (account["account_id"],),
            ).fetchall()
        for raw in rows:
            order = dict(raw)
            quote = quotes.get(order["security_code"])
            if not quote:
                skipped.append({"order_id": order["order_id"], "reason": "missing_realtime_quote"})
                continue
            fill_date = quote["trading_date"]
            if fill_date <= order["as_of"]:
                skipped.append({"order_id": order["order_id"], "reason": "no_later_trading_day"})
                continue
            market_open = datetime.fromisoformat(f"{fill_date}T09:30:00+08:00")
            reviewed_at = datetime.fromisoformat(order["reviewed_at"])
            if reviewed_at >= market_open:
                skipped.append({"order_id": order["order_id"], "reason": "approved_after_market_open"})
                continue
            if quote["volume"] <= 0:
                skipped.append({"order_id": order["order_id"], "reason": "suspended_or_preopen_quote"})
                continue
            one_price = abs(quote["high"] - quote["low"]) <= max(abs(quote["latest"]), 1.0) * 1e-8
            if one_price and order["side"] == "buy" and quote["open"] > quote["previous_close"]:
                skipped.append({"order_id": order["order_id"], "reason": "one_price_limit_up"})
                continue
            if one_price and order["side"] == "sell" and quote["open"] < quote["previous_close"]:
                skipped.append({"order_id": order["order_id"], "reason": "one_price_limit_down"})
                continue
            fill, reason = self._fill_order(
                account["account_id"], order, fill_date, quote["open"], quote["snapshot_id"]
            )
            if reason:
                skipped.append({"order_id": order["order_id"], "reason": reason})
                continue
            turnover += fill["gross_amount"]
            filled.append(fill)
        account = self.account(account["account_id"])
        nav = self._mark_nav(account, execution_date, turnover, quotes)
        return {"account": account, "filled": filled, "skipped": skipped, "nav": nav, "realtime": refresh}
