from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

from app.research.store import utc_now


_INVALID_ACCOUNT_NAME = re.compile(r'[<>:"/\\|?*]|[. ]$')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def validate_account_name(name: str) -> str:
    value = name.strip()
    if not value or _INVALID_ACCOUNT_NAME.search(value):
        raise ValueError("账户名称包含 Windows 路径不支持的字符")
    if value.upper() in _RESERVED_NAMES:
        raise ValueError("账户名称是 Windows 保留名称")
    return value


class PaperRunStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.recover_interrupted()

    @staticmethod
    def _atomic_json(path: Path, value: dict | list) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def create(self, *, account: dict, trading_date: str, manifest: dict) -> dict:
        account_name = validate_account_name(account["name"])
        date_root = self.root / account_name / trading_date
        with self._lock:
            numbers = []
            if date_root.exists():
                for item in date_root.iterdir():
                    match = re.fullmatch(r"run-(\d{3})", item.name)
                    if item.is_dir() and match:
                        numbers.append(int(match.group(1)))
            run_name = f"run-{max(numbers, default=0) + 1:03d}"
            run_key = f"{account_name}/{trading_date}/{run_name}"
            run_root = date_root / run_name
            run_root.mkdir(parents=True, exist_ok=False)
            now = utc_now()
            item = {
                "run_key": run_key,
                "account_id": account["account_id"],
                "account_name": account_name,
                "trading_date": trading_date,
                "status": "queued",
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "error": None,
                "proposal_ids": [],
                **manifest,
            }
            self._atomic_json(run_root / "manifest.json", item)
        return item

    def path(self, run_key: str) -> Path:
        target = (self.root / Path(run_key)).resolve()
        root = self.root.resolve()
        if target == root or root not in target.parents:
            raise ValueError("运行目录越界")
        return target

    def manifest(self, run_key: str) -> dict:
        path = self.path(run_key) / "manifest.json"
        if not path.is_file():
            raise KeyError("模拟盘运行记录不存在")
        return json.loads(path.read_text(encoding="utf-8"))

    def update_manifest(self, run_key: str, **changes: Any) -> dict:
        with self._lock:
            item = self.manifest(run_key)
            item.update(changes)
            self._atomic_json(self.path(run_key) / "manifest.json", item)
        return item

    def write_result(self, run_key: str, filename: str, result: dict) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]*\.json", filename):
            raise ValueError("运行结果文件名无效")
        self._atomic_json(self.path(run_key) / filename, result)

    def all_manifests(self) -> list[dict]:
        items = []
        for path in self.root.glob("*/*/run-*/manifest.json"):
            try:
                items.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(items, key=lambda item: item.get("created_at", ""), reverse=True)

    def find(self, *, batch_id: str | None = None, account_id: str | None = None,
             trading_date: str | None = None) -> list[dict]:
        return [
            item for item in self.all_manifests()
            if (batch_id is None or item.get("batch_id") == batch_id)
            and (account_id is None or item.get("account_id") == account_id)
            and (trading_date is None or item.get("trading_date") == trading_date)
        ]

    def recover_interrupted(self) -> None:
        for item in self.all_manifests():
            if item.get("status") in {"queued", "running"}:
                self.update_manifest(
                    item["run_key"], status="interrupted",
                    error="服务重启中断，可重新运行", finished_at=utc_now(),
                )
