from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .data_access import normalize_code
from .migrations import apply_migration
from .schemas import ThesisCreate


class ThesisStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        apply_migration(self.connect, "0002_thesis_monitoring", self._create_schema)

    def _create_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS thesis (
                    id TEXT PRIMARY KEY,
                    security_code TEXT NOT NULL,
                    title TEXT NOT NULL,
                    core_claim TEXT NOT NULL,
                    horizon TEXT NOT NULL,
                    status TEXT NOT NULL,
                    invalidating_conditions TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS thesis_monitor_baseline (
                    thesis_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    workflow_version TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    evidence_snapshot_hash TEXT NOT NULL,
                    established_at TEXT NOT NULL,
                    FOREIGN KEY (thesis_id) REFERENCES thesis(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS thesis_claim (
                    claim_id TEXT PRIMARY KEY,
                    thesis_id TEXT NOT NULL,
                    source_claim_id TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    claim_type TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    baseline_run_id TEXT NOT NULL,
                    evidence_ids_json TEXT NOT NULL,
                    baseline_evidence_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    last_verdict TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(thesis_id, source_claim_id),
                    FOREIGN KEY (thesis_id) REFERENCES thesis(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_thesis_claim_thesis
                ON thesis_claim(thesis_id, status, created_at);

                CREATE TABLE IF NOT EXISTS claim_evaluation (
                    evaluation_id TEXT PRIMARY KEY,
                    claim_id TEXT NOT NULL,
                    thesis_id TEXT NOT NULL,
                    baseline_run_id TEXT NOT NULL,
                    current_run_id TEXT NOT NULL,
                    suggested_verdict TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    changes_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    confirmed_verdict TEXT,
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    UNIQUE(claim_id, baseline_run_id, current_run_id),
                    FOREIGN KEY (claim_id) REFERENCES thesis_claim(claim_id) ON DELETE CASCADE,
                    FOREIGN KEY (thesis_id) REFERENCES thesis(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_claim_evaluation_thesis_created
                ON claim_evaluation(thesis_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS thesis_status_history (
                    history_id TEXT PRIMARY KEY,
                    thesis_id TEXT NOT NULL,
                    previous_status TEXT,
                    new_status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    changed_at TEXT NOT NULL,
                    FOREIGN KEY (thesis_id) REFERENCES thesis(id) ON DELETE CASCADE
                );
                """
            )

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["invalidating_conditions"] = json.loads(item["invalidating_conditions"])
        return item

    def list(self, code: str | None = None) -> list[dict]:
        sql = "SELECT * FROM thesis"
        params: list[str] = []
        if code:
            sql += " WHERE security_code = ?"
            params.append(normalize_code(code))
        sql += " ORDER BY updated_at DESC"
        with self.connect() as conn:
            items = [self._decode(row) for row in conn.execute(sql, params)]
            for item in items:
                item["monitor"] = self._monitor_summary(conn, item["id"])
            return items

    def get(self, thesis_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM thesis WHERE id=?", (thesis_id,)).fetchone()
            if not row:
                return None
            item = self._decode(row)
            item["monitor"] = self._monitor_summary(conn, thesis_id)
            return item

    @staticmethod
    def _monitor_summary(conn: sqlite3.Connection, thesis_id: str) -> dict:
        baseline = conn.execute(
            "SELECT * FROM thesis_monitor_baseline WHERE thesis_id=?", (thesis_id,)
        ).fetchone()
        claim_count = conn.execute(
            "SELECT COUNT(*) FROM thesis_claim WHERE thesis_id=? AND status='active'",
            (thesis_id,),
        ).fetchone()[0]
        pending_count = conn.execute(
            "SELECT COUNT(*) FROM claim_evaluation WHERE thesis_id=? AND status='pending'",
            (thesis_id,),
        ).fetchone()[0]
        return {
            "baseline": dict(baseline) if baseline else None,
            "claim_count": claim_count,
            "pending_evaluations": pending_count,
        }

    def create(self, data: ThesisCreate) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        thesis_id = str(uuid.uuid4())
        code = normalize_code(data.code)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO thesis (
                    id, security_code, title, core_claim, horizon, status,
                    invalidating_conditions, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    thesis_id,
                    code,
                    data.title,
                    data.core_claim,
                    data.horizon,
                    data.status,
                    json.dumps(data.invalidating_conditions, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM thesis WHERE id = ?", (thesis_id,)).fetchone()
            conn.execute(
                """
                INSERT INTO thesis_status_history (
                    history_id, thesis_id, previous_status, new_status, source, changed_at
                ) VALUES (?, ?, NULL, ?, 'create', ?)
                """,
                (str(uuid.uuid4()), thesis_id, data.status, now),
            )
        return self._decode(row)

    def update_status(self, thesis_id: str, status: str) -> dict | None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            previous = conn.execute(
                "SELECT status FROM thesis WHERE id=?", (thesis_id,)
            ).fetchone()
            conn.execute(
                "UPDATE thesis SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, thesis_id),
            )
            row = conn.execute("SELECT * FROM thesis WHERE id = ?", (thesis_id,)).fetchone()
            if previous and previous["status"] != status:
                conn.execute(
                    """
                    INSERT INTO thesis_status_history (
                        history_id, thesis_id, previous_status, new_status, source, changed_at
                    ) VALUES (?, ?, ?, ?, 'manual', ?)
                    """,
                    (str(uuid.uuid4()), thesis_id, previous["status"], status, now),
                )
        return self._decode(row) if row else None

    def set_monitor_baseline(
        self,
        *,
        thesis_id: str,
        run: dict,
        claims: list[dict],
        evidence: list[dict],
    ) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        evidence_by_id = {item["id"]: item for item in evidence}
        importable_claims = []
        for claim in claims:
            evidence_ids = [
                item for item in claim.get("evidence_ids", []) if item in evidence_by_id
            ]
            if claim.get("id") and evidence_ids:
                importable_claims.append((claim, evidence_ids))
        if not importable_claims:
            raise ValueError("研究运行中没有可绑定证据的确定性 Claim")

        imported = []
        with self.connect() as conn:
            thesis = conn.execute("SELECT * FROM thesis WHERE id=?", (thesis_id,)).fetchone()
            if not thesis:
                raise KeyError(thesis_id)
            if thesis["security_code"] != run["security_code"]:
                raise ValueError("研究运行与 Thesis 证券不一致")
            previous_baseline = conn.execute(
                "SELECT run_id FROM thesis_monitor_baseline WHERE thesis_id=?", (thesis_id,)
            ).fetchone()
            conn.execute(
                """
                INSERT INTO thesis_monitor_baseline (
                    thesis_id, run_id, workflow_version, as_of,
                    evidence_snapshot_hash, established_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(thesis_id) DO UPDATE SET
                    run_id=excluded.run_id,
                    workflow_version=excluded.workflow_version,
                    as_of=excluded.as_of,
                    evidence_snapshot_hash=excluded.evidence_snapshot_hash,
                    established_at=excluded.established_at
                """,
                (
                    thesis_id,
                    run["run_id"],
                    run["workflow_version"],
                    run["as_of"],
                    run["evidence_snapshot_hash"],
                    now,
                ),
            )
            if previous_baseline and previous_baseline["run_id"] != run["run_id"]:
                conn.execute(
                    """
                    UPDATE claim_evaluation SET status='superseded'
                    WHERE thesis_id=? AND status='pending'
                    """,
                    (thesis_id,),
                )
            conn.execute("UPDATE thesis_claim SET status='inactive' WHERE thesis_id=?", (thesis_id,))
            for claim, evidence_ids in importable_claims:
                snapshots = [evidence_by_id[evidence_id] for evidence_id in evidence_ids]
                existing = conn.execute(
                    "SELECT claim_id, created_at FROM thesis_claim WHERE thesis_id=? AND source_claim_id=?",
                    (thesis_id, claim["id"]),
                ).fetchone()
                claim_id = existing["claim_id"] if existing else str(uuid.uuid4())
                created_at = existing["created_at"] if existing else now
                conn.execute(
                    """
                    INSERT INTO thesis_claim (
                        claim_id, thesis_id, source_claim_id, statement, claim_type,
                        confidence, baseline_run_id, evidence_ids_json,
                        baseline_evidence_json, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                    ON CONFLICT(thesis_id, source_claim_id) DO UPDATE SET
                        statement=excluded.statement,
                        claim_type=excluded.claim_type,
                        confidence=excluded.confidence,
                        baseline_run_id=excluded.baseline_run_id,
                        evidence_ids_json=excluded.evidence_ids_json,
                        baseline_evidence_json=excluded.baseline_evidence_json,
                        status='active',
                        updated_at=excluded.updated_at
                    """,
                    (
                        claim_id,
                        thesis_id,
                        claim["id"],
                        claim["statement"],
                        claim["claim_type"],
                        claim["confidence"],
                        run["run_id"],
                        json.dumps(evidence_ids, ensure_ascii=False),
                        json.dumps(snapshots, ensure_ascii=False, sort_keys=True),
                        created_at,
                        now,
                    ),
                )
                imported.append(claim_id)
        return {"thesis_id": thesis_id, "baseline_run_id": run["run_id"], "claims_imported": len(imported)}

    @staticmethod
    def _decode_claim(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["evidence_ids"] = json.loads(item.pop("evidence_ids_json"))
        item["baseline_evidence"] = json.loads(item.pop("baseline_evidence_json"))
        return item

    def active_claims(self, thesis_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM thesis_claim WHERE thesis_id=? AND status='active' ORDER BY created_at",
                (thesis_id,),
            ).fetchall()
        return [self._decode_claim(row) for row in rows]

    def save_claim_evaluations(
        self,
        *,
        thesis_id: str,
        current_run_id: str,
        evaluations: list[dict],
    ) -> list[dict]:
        now = datetime.now(timezone.utc).isoformat()
        saved = []
        with self.connect() as conn:
            for evaluation in evaluations:
                evaluation_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT OR IGNORE INTO claim_evaluation (
                        evaluation_id, claim_id, thesis_id, baseline_run_id,
                        current_run_id, suggested_verdict, rationale, changes_json,
                        status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        evaluation_id,
                        evaluation["claim_id"],
                        thesis_id,
                        evaluation["baseline_run_id"],
                        current_run_id,
                        evaluation["suggested_verdict"],
                        evaluation["rationale"],
                        json.dumps(evaluation["changes"], ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
                row = conn.execute(
                    """
                    SELECT * FROM claim_evaluation
                    WHERE claim_id=? AND baseline_run_id=? AND current_run_id=?
                    """,
                    (
                        evaluation["claim_id"],
                        evaluation["baseline_run_id"],
                        current_run_id,
                    ),
                ).fetchone()
                saved.append(self._decode_evaluation(row))
        return saved

    @staticmethod
    def _decode_evaluation(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["changes"] = json.loads(item.pop("changes_json"))
        return item

    def confirm_evaluation(self, evaluation_id: str, verdict: str) -> dict | None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT claim_id FROM claim_evaluation WHERE evaluation_id=?",
                (evaluation_id,),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                """
                UPDATE claim_evaluation
                SET status='confirmed', confirmed_verdict=?, confirmed_at=?
                WHERE evaluation_id=?
                """,
                (verdict, now, evaluation_id),
            )
            conn.execute(
                "UPDATE thesis_claim SET last_verdict=?, updated_at=? WHERE claim_id=?",
                (verdict, now, row["claim_id"]),
            )
            updated = conn.execute(
                "SELECT * FROM claim_evaluation WHERE evaluation_id=?", (evaluation_id,)
            ).fetchone()
        return self._decode_evaluation(updated)

    def monitor_detail(self, thesis_id: str) -> dict | None:
        thesis = self.get(thesis_id)
        if thesis is None:
            return None
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT e.*, c.source_claim_id, c.statement
                FROM claim_evaluation e
                JOIN thesis_claim c ON c.claim_id=e.claim_id
                WHERE e.thesis_id=? ORDER BY e.created_at DESC
                """,
                (thesis_id,),
            ).fetchall()
        return {
            "thesis": thesis,
            "claims": self.active_claims(thesis_id),
            "evaluations": [self._decode_evaluation(row) for row in rows],
        }

    def delete(self, thesis_id: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM thesis WHERE id = ?", (thesis_id,))
        return cursor.rowcount > 0
