from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings
from app.paper.run_store import validate_account_name


V2_SCHEMA = """
CREATE TABLE paper_account (account_id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
 initial_cash REAL NOT NULL CHECK(initial_cash>0), current_cash REAL NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('active','archived')), strategy_name TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE paper_proposal (proposal_id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
 run_key TEXT NOT NULL, proposal_date TEXT NOT NULL, security_code TEXT NOT NULL,
 side TEXT NOT NULL CHECK(side IN ('buy','sell')), quantity INTEGER NOT NULL CHECK(quantity>0),
 reference_price REAL NOT NULL CHECK(reference_price>0),
 status TEXT NOT NULL CHECK(status IN ('proposed','approved','rejected','cancelled','filled')),
 created_at TEXT NOT NULL, reviewed_at TEXT, FOREIGN KEY(account_id) REFERENCES paper_account(account_id));
CREATE INDEX idx_paper_proposal_account_status ON paper_proposal(account_id,status,proposal_date DESC,proposal_id DESC);
CREATE INDEX idx_paper_proposal_run ON paper_proposal(run_key,proposal_id);
CREATE TABLE paper_trade (trade_id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
 security_code TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('buy','sell')),
 quantity INTEGER NOT NULL CHECK(quantity>0), price REAL NOT NULL CHECK(price>0),
 fees REAL NOT NULL CHECK(fees>=0), traded_at TEXT NOT NULL,
 FOREIGN KEY(account_id) REFERENCES paper_account(account_id));
CREATE INDEX idx_paper_trade_account_time ON paper_trade(account_id,traded_at DESC,trade_id DESC);
CREATE TABLE paper_position (account_id TEXT NOT NULL, security_code TEXT NOT NULL,
 quantity INTEGER NOT NULL CHECK(quantity>0), available_quantity INTEGER NOT NULL CHECK(available_quantity>=0),
 average_cost REAL NOT NULL CHECK(average_cost>0), last_buy_date TEXT, updated_at TEXT NOT NULL,
 PRIMARY KEY(account_id,security_code), FOREIGN KEY(account_id) REFERENCES paper_account(account_id));
CREATE TABLE paper_nav_snapshot (account_id TEXT NOT NULL, trading_date TEXT NOT NULL,
 current_cash REAL NOT NULL, market_value REAL NOT NULL, total_equity REAL NOT NULL,
 daily_return REAL, cumulative_return REAL NOT NULL, drawdown REAL NOT NULL,
 gross_exposure REAL NOT NULL, turnover REAL NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(account_id,trading_date), FOREIGN KEY(account_id) REFERENCES paper_account(account_id));
"""


def tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def strategy_names(source: sqlite3.Connection, quant: sqlite3.Connection) -> dict[str, str]:
    quant.row_factory = sqlite3.Row
    names = {row["strategy_id"]: row["name"] for row in quant.execute(
        "SELECT strategy_id,name FROM quant_strategy WHERE status='active'"
    )}
    builtin = {}
    if "paper_strategy" in tables(source):
        builtin = {row["strategy_id"]: row["name"] for row in source.execute("SELECT strategy_id,name FROM paper_strategy")}
    deployments = {}
    if "paper_strategy_deployment" in tables(source):
        deployments = {row["account_id"]: row["strategy_version_id"] for row in source.execute(
            "SELECT account_id,strategy_version_id FROM paper_strategy_deployment WHERE status='active'"
        )}
    result = {}
    for account in source.execute("SELECT account_id,strategy_id FROM paper_account"):
        version_id = deployments.get(account["account_id"])
        name = names.get(version_id) if version_id else builtin.get(account["strategy_id"])
        if not name:
            raise ValueError(f"账户 {account['account_id']} 无法映射到活动 quant_strategy")
        result[account["account_id"]] = name
    return result


def next_run_key(root: Path, account_name: str, trading_date: str, counters: dict[tuple[str, str], int]) -> tuple[str, Path]:
    key = (account_name, trading_date)
    counters[key] = counters.get(key, 0) + 1
    run_key = f"{account_name}/{trading_date}/run-{counters[key]:03d}"
    return run_key, root / Path(run_key)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def inspect_source(source: sqlite3.Connection, quant: sqlite3.Connection) -> dict:
    source_tables = tables(source)
    required = {"paper_account", "paper_order", "paper_position", "paper_nav_snapshot"}
    if not required.issubset(source_tables):
        raise ValueError(f"不是可迁移的 V1 数据库，缺少：{sorted(required-source_tables)}")
    accounts = rows(source, "SELECT * FROM paper_account ORDER BY created_at")
    seen = set()
    for account in accounts:
        name = validate_account_name(account["name"])
        if name.casefold() in seen:
            raise ValueError(f"账户名称重复：{name}")
        seen.add(name.casefold())
    mappings = strategy_names(source, quant)
    counts = {
        "accounts": len(accounts),
        "proposals": source.execute("SELECT COUNT(*) FROM paper_order").fetchone()[0],
        "trades": source.execute("SELECT COUNT(*) FROM paper_order WHERE status='filled'").fetchone()[0],
        "positions": source.execute("SELECT COUNT(*) FROM paper_position WHERE quantity>0").fetchone()[0],
        "nav_snapshots": source.execute("SELECT COUNT(*) FROM paper_nav_snapshot").fetchone()[0],
        "daily_runs": source.execute("SELECT COUNT(*) FROM paper_daily_run").fetchone()[0] if "paper_daily_run" in source_tables else 0,
        "daily_batches": source.execute("SELECT COUNT(*) FROM paper_daily_batch").fetchone()[0] if "paper_daily_batch" in source_tables else 0,
    }
    return {"accounts": accounts, "strategy_names": mappings, "counts": counts}


def migrate(source_path: Path, quant_path: Path, target_path: Path, run_root: Path) -> dict:
    with closing(sqlite3.connect(source_path)) as source, closing(sqlite3.connect(quant_path)) as quant:
        source.row_factory = sqlite3.Row
        quant.row_factory = sqlite3.Row
        inspected = inspect_source(source, quant)
        if target_path.exists():
            target_path.unlink()
        with closing(sqlite3.connect(target_path)) as target:
            target.execute("PRAGMA foreign_keys=ON")
            target.executescript(V2_SCHEMA)
            for account in inspected["accounts"]:
                target.execute(
                    "INSERT INTO paper_account VALUES (?,?,?,?,?,?,?,?)",
                    (account["account_id"], account["name"], account["initial_cash"], account["cash"],
                     account["status"], inspected["strategy_names"][account["account_id"]],
                     account["created_at"], account["updated_at"]),
                )
            counters: dict[tuple[str, str], int] = {}
            run_keys: dict[str, str] = {}
            accounts_by_id = {item["account_id"]: item for item in inspected["accounts"]}
            for run in rows(source, "SELECT * FROM paper_daily_run ORDER BY created_at"):
                account = accounts_by_id[run["account_id"]]
                run_key, directory = next_run_key(run_root, account["name"], run["as_of"], counters)
                run_keys[run["run_id"]] = run_key
                manifest = {
                    "run_key": run_key, "legacy_run_id": run["run_id"], "account_id": run["account_id"],
                    "account_name": account["name"], "trading_date": run["as_of"], "status": run["status"],
                    "strategy": {"legacy_version": run["strategy_version"]},
                    "config": json.loads(run["config_json"]), "market_summary": json.loads(run["market_summary_json"]),
                    "factor_snapshot_id": run["factor_snapshot_id"], "shadow_snapshot_id": run["shadow_snapshot_id"],
                    "snapshot_hash": run["snapshot_hash"], "created_at": run["created_at"], "proposal_ids": [],
                }
                write_json(directory / "manifest.json", manifest)
            if "paper_daily_batch" in tables(source):
                for batch in rows(source, "SELECT * FROM paper_daily_batch ORDER BY created_at"):
                    account = accounts_by_id[batch["account_id"]]
                    run_key, directory = next_run_key(run_root, account["name"], batch["as_of"], counters)
                    step_rows = rows(
                        source,
                        "SELECT * FROM paper_daily_batch_step WHERE batch_id=? ORDER BY ordinal",
                        (batch["batch_id"],),
                    )
                    steps = []
                    for step in step_rows:
                        result = json.loads(step["result_json"]) if step["result_json"] else None
                        steps.append({
                            "step_name": step["step_name"], "ordinal": step["ordinal"],
                            "status": step["status"], "result": result, "error": step["error"],
                            "started_at": step["started_at"], "finished_at": step["finished_at"],
                        })
                        if result is not None:
                            write_json(directory / f'{step["step_name"]}.json', result)
                    write_json(directory / "manifest.json", {
                        "run_key": run_key, "batch_id": batch["batch_id"],
                        "account_id": batch["account_id"], "account_name": account["name"],
                        "trading_date": batch["as_of"], "batch_version": batch["batch_version"],
                        "config_hash": batch["config_hash"], "config": json.loads(batch["config_json"]),
                        "status": batch["status"], "current_step": batch["current_step"],
                        "error": batch["error"], "created_at": batch["created_at"],
                        "started_at": batch["started_at"], "finished_at": batch["finished_at"],
                        "updated_at": batch["updated_at"], "steps": steps, "proposal_ids": [],
                    })
            proposal_ids: dict[str, int] = {}
            for order in rows(source, "SELECT * FROM paper_order ORDER BY created_at"):
                run_key = run_keys[order["run_id"]]
                status = order["status"] if order["status"] in {"proposed","approved","rejected","cancelled","filled"} else "cancelled"
                cursor = target.execute(
                    """INSERT INTO paper_proposal(account_id,run_key,proposal_date,security_code,side,quantity,
                    reference_price,status,created_at,reviewed_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (order["account_id"], run_key, run_key.split("/")[-2], order["security_code"], order["side"],
                     order["quantity"], order["reference_price"], status, order["created_at"], order["reviewed_at"]),
                )
                proposal_ids[order["order_id"]] = cursor.lastrowid
                if status == "filled":
                    target.execute(
                        "INSERT INTO paper_trade(account_id,security_code,side,quantity,price,fees,traded_at) VALUES (?,?,?,?,?,?,?)",
                        (order["account_id"], order["security_code"], order["side"], order["quantity"],
                         order["fill_price"], order["fees"] or 0, order["fill_date"]),
                    )
            for legacy_run_id, run_key in run_keys.items():
                orders = rows(source, "SELECT * FROM paper_order WHERE run_id=? ORDER BY CASE side WHEN 'sell' THEN 0 ELSE 1 END, created_at", (legacy_run_id,))
                write_json(run_root / Path(run_key) / "order_proposals.json", {"orders": [
                    {"proposal_id": proposal_ids[item["order_id"]], "security_code": item["security_code"],
                     "side": item["side"], "quantity": item["quantity"], "reference_price": item["reference_price"],
                     "target_weight": item["target_weight"], "reason": json.loads(item["reason_json"])} for item in orders
                ]})
                manifest_path = run_root / Path(run_key) / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["proposal_ids"] = [proposal_ids[item["order_id"]] for item in orders]
                write_json(manifest_path, manifest)
            for row in rows(source, "SELECT * FROM paper_position WHERE quantity>0"):
                target.execute("INSERT INTO paper_position VALUES (?,?,?,?,?,?,?)", (
                    row["account_id"], row["security_code"], row["quantity"], row["available_quantity"],
                    row["average_cost"], row["last_buy_date"], row["updated_at"],
                ))
            for row in rows(source, "SELECT * FROM paper_nav_snapshot"):
                target.execute("INSERT INTO paper_nav_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                    row["account_id"], row["trading_date"], row["cash"], row["market_value"], row["nav"],
                    row["daily_return"], row["cumulative_return"], row["drawdown"], row["gross_exposure"],
                    row["turnover"], row["created_at"],
                ))
            target.commit()
            if target.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("V2 外键检查失败")
            actual = {name: target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for name, table in (
                ("accounts","paper_account"),("proposals","paper_proposal"),("trades","paper_trade"),
                ("positions","paper_position"),("nav_snapshots","paper_nav_snapshot"))}
            for name, count in actual.items():
                if count != inspected["counts"][name]:
                    raise RuntimeError(f"迁移数量不一致：{name} {count} != {inspected['counts'][name]}")
    return inspected["counts"]


def main() -> None:
    parser = argparse.ArgumentParser(description="迁移 paper_trading.db 到五表 V2 账本")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    with closing(sqlite3.connect(settings.paper_db)) as source, closing(sqlite3.connect(settings.quant_db)) as quant:
        source.row_factory = sqlite3.Row
        quant.row_factory = sqlite3.Row
        report = inspect_source(source, quant)
    print(json.dumps({"database": str(settings.paper_db), **report["counts"]}, ensure_ascii=False, indent=2))
    if args.dry_run:
        return
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = settings.paper_db.with_suffix(".v2.tmp")
    staging_runs = settings.paper_run_root.with_name(f"paper.v2-{timestamp}.tmp")
    backup = settings.paper_db.with_name(f"paper_trading.v1-{timestamp}.db")
    if staging_runs.exists():
        shutil.rmtree(staging_runs)
    staging_runs.mkdir(parents=True)
    try:
        migrate(settings.paper_db, settings.quant_db, target, staging_runs)
        settings.paper_run_root.mkdir(parents=True, exist_ok=True)
        conflicts = [
            settings.paper_run_root / account_dir.name
            for account_dir in staging_runs.iterdir()
            if (settings.paper_run_root / account_dir.name).exists()
        ]
        if conflicts:
            raise RuntimeError(f"运行目录已存在，拒绝覆盖：{conflicts}")
        os.replace(settings.paper_db, backup)
        os.replace(target, settings.paper_db)
        for account_dir in staging_runs.iterdir():
            destination = settings.paper_run_root / account_dir.name
            os.replace(account_dir, destination)
        staging_runs.rmdir()
    except Exception:
        target.unlink(missing_ok=True)
        shutil.rmtree(staging_runs, ignore_errors=True)
        if backup.exists():
            failed_v2 = settings.paper_db.with_suffix(".v2.failed")
            if settings.paper_db.exists():
                os.replace(settings.paper_db, failed_v2)
            os.replace(backup, settings.paper_db)
        raise
    print(f"V2 migration complete; backup={backup}")


if __name__ == "__main__":
    main()
