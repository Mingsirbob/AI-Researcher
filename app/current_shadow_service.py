from __future__ import annotations

import sqlite3
import threading
from datetime import date, timedelta

import pandas as pd

from .config import Settings
from .current_shadow import CurrentShadowPipeline, align_forward_adjusted_fields
from .research_store import ResearchStore
from .updater import IFindDailyClient, code_to_table


class CurrentShadowService:
    def __init__(self, settings: Settings, store: ResearchStore):
        self.settings = settings
        self.store = store
        self.pipeline = CurrentShadowPipeline(store, settings.current_shadow_root / "providers")
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
            client = IFindDailyClient(self.settings, adjustment="forward")
            client.login()
            try:
                universe = client.fetch_csi300_universe()
                codes = universe["security_code"].tolist()
                frames = [
                    client.fetch(codes[offset : offset + batch_size], start_date, as_of)
                    for offset in range(0, len(codes), batch_size)
                ]
            finally:
                client.logout()
            frame = pd.concat(frames, ignore_index=True)
            raw_frame, missing_raw_codes = self._load_unadjusted_history(codes, start_date, as_of)
            if missing_raw_codes:
                raw_client = IFindDailyClient(self.settings, adjustment="unadjusted")
                raw_client.login()
                try:
                    missing_frames = [
                        raw_client.fetch(
                            missing_raw_codes[offset : offset + batch_size], start_date, as_of
                        )
                        for offset in range(0, len(missing_raw_codes), batch_size)
                    ]
                finally:
                    raw_client.logout()
                raw_frame = pd.concat([raw_frame, *missing_frames], ignore_index=True)
            frame, alignment = align_forward_adjusted_fields(frame, raw_frame)
            for item in universe.itertuples(index=False):
                self.store.upsert_security_name(
                    item.security_code, item.security_name, source="iFinD_WCQuery"
                )
            snapshot = self.pipeline.predict(
                model=model,
                validation=validation,
                frame=frame,
                universe=universe,
                start_date=start_date,
                as_of=as_of,
                trust_pickle=trust_pickle,
                alignment=alignment,
            )
            return {**snapshot, "reused": False}
