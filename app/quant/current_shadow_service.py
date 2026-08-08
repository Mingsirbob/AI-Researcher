from __future__ import annotations

import sqlite3
import threading
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from app.core.config import Settings
from app.quant.current_shadow import CurrentShadowPipeline, align_forward_adjusted_fields
from app.research.store import ResearchStore
from app.quant.store import QuantStore
from app.core.primitives import code_to_table
from app.data.ifind import IFindDataLayer
from app.market.stock_pool import StockPoolStore


class CurrentShadowService:
    def __init__(
        self,
        settings: Settings,
        store: QuantStore,
        master_store: ResearchStore | None = None,
        ifind: IFindDataLayer | None = None,
        stock_pool_store: StockPoolStore | None = None,
        provider_root: Path | None = None,
    ):
        self.settings = settings
        self.store = store
        self.master_store = master_store or store
        if ifind is None:
            raise ValueError("CurrentShadowService 必须使用共享 IFindDataLayer")
        if stock_pool_store is None:
            raise ValueError("CurrentShadowService 必须使用本地股池数据库")
        self.ifind = ifind
        self.stock_pool_store = stock_pool_store
        self.pipeline = CurrentShadowPipeline(
            store, provider_root or settings.current_shadow_root / "providers"
        )
        self._lock = threading.Lock()

    def _load_unadjusted_history(
        self, codes: list[str], start_date: date, as_of: date
    ) -> tuple[pd.DataFrame, list[str]]:
        records: list[dict] = []
        missing_codes: list[str] = []
        with sqlite3.connect(self.settings.stock_db) as conn:
            existing_tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for code in codes:
                table = code_to_table(code)
                if table not in existing_tables:
                    missing_codes.append(code)
                    continue
                rows = conn.execute(
                    f'''SELECT time, open, high, low, close, vwap, volume
                        FROM "{table}" WHERE time BETWEEN ? AND ? ORDER BY time''',
                    (start_date.isoformat(), as_of.isoformat()),
                ).fetchall()
                records.extend(
                    {
                        "time": row[0], "thscode": code, "open": row[1], "high": row[2],
                        "low": row[3], "close": row[4], "vwap": row[5], "volume": row[6],
                    }
                    for row in rows
                )
        columns = ("time", "thscode", "open", "high", "low", "close", "vwap", "volume")
        return pd.DataFrame(records, columns=columns), missing_codes

    def generate(
        self,
        *,
        as_of: date,
        model_run_id: str | None = None,
        lookback_days: int = 240,
        batch_size: int = 20,
        trust_pickle: bool = True,
        workspace: Path | None = None,
    ) -> dict:
        if lookback_days < 120 or batch_size < 1:
            raise ValueError("lookback_days 至少为 120，batch_size 必须大于 0")
        with self._lock:
            latest_model = self.store.latest_model_run()
            run_id = model_run_id or (latest_model or {}).get("model_run_id")
            if not run_id:
                raise ValueError("尚无已注册模型运行")
            existing = self.store.current_shadow_for_date(as_of.isoformat(), run_id)
            if existing and existing["status"] == "current_shadow_ready":
                return {**existing, "reused": True}

            model, validation = self.pipeline.validate_model(run_id, trust_pickle=trust_pickle)
            start_date = as_of - timedelta(days=lookback_days)
            pool_snapshot = self.stock_pool_store.resolve("csi300", as_of)
            universe = pd.DataFrame(pool_snapshot["members"])
            codes = universe["security_code"].tolist()
            frames = [
                self.ifind.get_daily_prices(
                    codes[offset : offset + batch_size],
                    start_date,
                    as_of,
                    adjustment="forward",
                )
                for offset in range(0, len(codes), batch_size)
            ]
            frame = pd.concat(frames, ignore_index=True)
            raw_frame, missing_raw_codes = self._load_unadjusted_history(codes, start_date, as_of)
            if missing_raw_codes:
                missing_frames = [
                    self.ifind.get_daily_prices(
                        missing_raw_codes[offset : offset + batch_size],
                        start_date,
                        as_of,
                        adjustment="unadjusted",
                    )
                    for offset in range(0, len(missing_raw_codes), batch_size)
                ]
                raw_frame = pd.concat([raw_frame, *missing_frames], ignore_index=True)
            frame, alignment = align_forward_adjusted_fields(frame, raw_frame)
            for item in universe.itertuples(index=False):
                self.master_store.upsert_security_name(
                    item.security_code, item.security_name, source=pool_snapshot["source"]
                )
            if hasattr(self.store, "sync_security_projection"):
                self.store.sync_security_projection()
            pipeline = (
                CurrentShadowPipeline(self.store, workspace / "providers")
                if workspace is not None
                else self.pipeline
            )
            snapshot = pipeline.predict(
                model=model,
                validation=validation,
                frame=frame,
                universe=universe,
                start_date=start_date,
                as_of=as_of,
                trust_pickle=trust_pickle,
                alignment=alignment,
                score_csv_path=(workspace / "lightgbm_scores.csv") if workspace else None,
            )
            return {**snapshot, "reused": False}
