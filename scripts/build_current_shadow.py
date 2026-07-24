from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings
from app.current_shadow import (
    CurrentShadowPipeline,
    align_forward_adjusted_fields,
    validate_current_data,
)
from app.research_store import ResearchStore
from app.updater import IFindDailyClient
from app.updater import code_to_table


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="复用已注册 Qlib/LightGBM 模型，执行滚动 OOS 门禁并生成当前 Shadow Signal"
    )
    parser.add_argument("--run-id", help="已注册模型运行 ID；默认使用最近导入运行")
    parser.add_argument("--as-of", type=date.fromisoformat, help="信号截止日；默认使用本地行情最新日")
    parser.add_argument("--lookback-days", type=int, default=240, help="前复权行情自然日回看长度")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--validate-only", action="store_true", help="只执行历史滚动 OOS 验证")
    parser.add_argument("--diagnose-data", action="store_true", help="抓取当前数据并输出质量问题，不执行模型推理")
    parser.add_argument("--trust-pickle", action="store_true", help="确认模型 pickle 来源可信")
    return parser.parse_args()


def load_unadjusted_history(
    codes: list[str], start_date: date, as_of: date
) -> tuple[pd.DataFrame, list[str]]:
    records: list[dict] = []
    missing_codes: list[str] = []
    with sqlite3.connect(settings.stock_db) as conn:
        existing_tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
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
                    "time": row[0],
                    "thscode": code,
                    "open": row[1],
                    "high": row[2],
                    "low": row[3],
                    "close": row[4],
                    "vwap": row[5],
                    "volume": row[6],
                }
                for row in rows
            )
    return (
        pd.DataFrame(records, columns=("time", "thscode", "open", "high", "low", "close", "vwap", "volume")),
        missing_codes,
    )


def main() -> int:
    args = parse_args()
    if args.lookback_days < 120 or args.batch_size < 1:
        raise ValueError("lookback-days 至少为 120，batch-size 必须大于 0")
    store = ResearchStore(settings.state_db, settings.document_root)
    latest = store.latest_model_run()
    run_id = args.run_id or (latest or {}).get("model_run_id")
    if not run_id:
        raise ValueError("尚无已注册模型运行")
    pipeline = CurrentShadowPipeline(store, settings.current_shadow_root / "providers")
    model, validation = pipeline.validate_model(run_id, trust_pickle=args.trust_pickle)
    if args.validate_only:
        print(json.dumps({"model_run_id": run_id, "validation": validation}, ensure_ascii=False, indent=2))
        return 0 if validation["status"] == "passed" else 2

    market_data_end = store.market_data_end()
    if not args.as_of and not market_data_end:
        raise ValueError("证券主数据尚未记录本地行情截止日")
    as_of = args.as_of or date.fromisoformat(market_data_end)
    start_date = as_of - timedelta(days=args.lookback_days)
    client = IFindDailyClient(settings, adjustment="forward")
    client.login()
    try:
        universe = client.fetch_csi300_universe()
        frames: list[pd.DataFrame] = []
        codes = universe["security_code"].tolist()
        for offset in range(0, len(codes), args.batch_size):
            frames.append(
                client.fetch(codes[offset : offset + args.batch_size], start_date, as_of)
            )
    finally:
        client.logout()
    frame = pd.concat(frames, ignore_index=True)
    raw_frame, missing_raw_codes = load_unadjusted_history(codes, start_date, as_of)
    if missing_raw_codes:
        raw_client = IFindDailyClient(settings, adjustment="unadjusted")
        raw_client.login()
        try:
            missing_frames = [
                raw_client.fetch(
                    missing_raw_codes[offset : offset + args.batch_size], start_date, as_of
                )
                for offset in range(0, len(missing_raw_codes), args.batch_size)
            ]
        finally:
            raw_client.logout()
        raw_frame = pd.concat([raw_frame, *missing_frames], ignore_index=True)
    frame, alignment = align_forward_adjusted_fields(frame, raw_frame)
    if args.diagnose_data:
        contract = validate_current_data(
            frame,
            universe,
            start_date=start_date,
            as_of=as_of,
        )
        print(
            json.dumps(
                {
                    "status": contract["status"],
                    "coverage": contract["coverage"],
                    "gates": contract["gates"],
                    "issues": contract["issues"],
                    "alignment": alignment,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if contract["status"] == "passed" else 2
    for item in universe.itertuples(index=False):
        store.upsert_security_name(item.security_code, item.security_name, source="iFinD_WCQuery")
    snapshot = pipeline.predict(
        model=model,
        validation=validation,
        frame=frame,
        universe=universe,
        start_date=start_date,
        as_of=as_of,
        trust_pickle=args.trust_pickle,
        alignment=alignment,
    )
    print(
        json.dumps(
            {
                "model_run_id": run_id,
                "validation": validation,
                "current_shadow": snapshot,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if snapshot["status"] == "current_shadow_ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
