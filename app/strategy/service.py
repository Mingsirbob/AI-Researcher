from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from app.core.migrations import apply_migration
from app.core.primitives import canonical_hash
from app.research.store import utc_now

from .code_runtime import validate_strategy_source
from .compiler import StrategyCompiler
from .registry import ComponentRegistry
from .runtime import StrategyRuntime


class StrategyService:
    def __init__(
        self,
        quant_store,
        *,
        strategy_run_root: Path | None = None,
        generator: Any | None = None,
        stock_pool_store: Any | None = None,
    ) -> None:
        self.store = quant_store
        self.registry = ComponentRegistry()
        self.compiler = StrategyCompiler(self.registry)
        self.strategy_run_root = Path(strategy_run_root) if strategy_run_root else None
        self.generator = generator
        self.stock_pool_store = stock_pool_store
        apply_migration(self.store.connect, "0033_compact_quant_strategy", self._create_schema)
        self.system_lightgbm_version_id = self._ensure_system_lightgbm_strategy()
        self._ensure_published_artifacts()

    def _create_schema(self) -> None:
        with self.store.connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS quant_strategy (
                strategy_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                definition_json TEXT NOT NULL,
                compiled_plan_json TEXT NOT NULL DEFAULT '{}',
                compiled_hash TEXT NOT NULL,
                strategy_kind TEXT NOT NULL DEFAULT 'python_code',
                source_code TEXT,
                source_path TEXT,
                requirement TEXT NOT NULL DEFAULT '',
                generation_meta_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_quant_strategy_status
            ON quant_strategy(status, updated_at DESC);
            """)
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "strategy_version" in tables:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(strategy_version)")}
                kind = "strategy_kind" if "strategy_kind" in columns else "'structured'"
                source = "source_code" if "source_code" in columns else "NULL"
                path = "source_path" if "source_path" in columns else "NULL"
                meta = "generation_meta_json" if "generation_meta_json" in columns else "'{}'"
                conn.execute(f"""
                    INSERT OR IGNORE INTO quant_strategy
                    SELECT strategy_version_id, name, description, definition_json,
                           compiled_plan_json, compiled_hash, {kind}, {source}, {path},
                           '', {meta}, 'active', published_at, published_at
                    FROM strategy_version WHERE status='published'
                """)
            if "strategy_draft" in tables:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(strategy_draft)")}
                kind = "strategy_kind" if "strategy_kind" in columns else "'structured'"
                source = "source_code" if "source_code" in columns else "NULL"
                requirement = "generation_prompt" if "generation_prompt" in columns else "''"
                meta = "generation_meta_json" if "generation_meta_json" in columns else "'{}'"
                conn.execute(f"""
                    INSERT OR IGNORE INTO quant_strategy
                    SELECT draft_id, name, description, definition_json, '{{}}',
                           'draft:' || draft_id, {kind}, {source}, NULL, {requirement},
                           {meta}, status, created_at, updated_at
                    FROM strategy_draft WHERE status!='published'
                """)

    @staticmethod
    def _decode(row, *fields: str) -> dict:
        item = dict(row)
        for field in fields:
            item[field] = json.loads(item.pop(f"{field}_json"))
        if "generation_meta_json" in item:
            item["generation_meta"] = json.loads(item.pop("generation_meta_json") or "{}")
        strategy_id = item["strategy_id"]
        item.update({
            "strategy_version_id": strategy_id,
            "draft_id": strategy_id,
            "version": 1,
            "revision": 1,
            "published_at": item["updated_at"],
            "generation_prompt": item.get("requirement", ""),
        })
        if item["status"] == "active":
            item["status"] = "published"
        return item

    def components(self) -> dict:
        pools = self.stock_pool_store.list_pools() if self.stock_pool_store else []
        return {
            "items": self.registry.list(),
            "templates": self.registry.templates(),
            "pools": pools,
            "filter_pipelines": [
                {"filter_pipeline_id": "factor_quality_passed", "name": "因子质量筛选"}
            ],
            "llm_configured": self.generator is not None,
        }

    def create_draft(self, *, name: str, description: str, template_id: str) -> dict:
        definition = self.registry.template(template_id)
        definition.update({"name": name, "description": description})
        draft_id, now = str(uuid.uuid4()), utc_now()
        with self.store.connect() as conn:
            conn.execute(
                """INSERT INTO quant_strategy
                   (strategy_id, name, description, definition_json, compiled_hash,
                    strategy_kind, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'structured', 'draft', ?, ?)""",
                (draft_id, name, description, json.dumps(definition, ensure_ascii=False, sort_keys=True), f"draft:{draft_id}", now, now),
            )
        return self.draft(draft_id)

    async def generate_draft(
        self,
        *,
        requirement: str,
        name: str,
        pool_id: str,
        filter_pipeline_id: str,
        params: dict,
    ) -> dict:
        if self.generator is None:
            raise RuntimeError("LLM策略生成尚未配置")
        if self.stock_pool_store:
            known_pools = {item["pool_id"] for item in self.stock_pool_store.list_pools()}
            if pool_id not in known_pools:
                raise KeyError("股票池不存在或尚未同步")
        generated = await self.generator.generate(
            requirement=requirement,
            name=name,
            pool_id=pool_id,
            filter_pipeline_id=filter_pipeline_id,
            params=params,
        )
        validation = validate_strategy_source(generated["source_code"])
        if validation["status"] != "valid":
            raise ValueError("生成代码未通过校验：" + "；".join(
                item["message"] for item in validation["errors"]
            ))
        definition = self.registry.template("python_code")
        definition.update({
            "name": generated["name"],
            "description": generated["description"],
        })
        definition["modules"]["universe"]["config"] = {
            "pool_id": pool_id,
            "filter_pipeline_id": filter_pipeline_id,
        }
        definition["modules"]["signal"]["config"] = {"strategy_params": params}
        draft_id, now = str(uuid.uuid4()), utc_now()
        with self.store.connect() as conn:
            conn.execute(
                """INSERT INTO quant_strategy
                   (strategy_id, name, description, definition_json, compiled_hash,
                    strategy_kind, source_code, requirement, generation_meta_json,
                    status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'python_code', ?, ?, ?, 'draft', ?, ?)""",
                (
                    draft_id, generated["name"], generated["description"],
                    json.dumps(definition, ensure_ascii=False, sort_keys=True), f"draft:{draft_id}",
                    generated["source_code"], requirement,
                    json.dumps(generated["generation_meta"], ensure_ascii=False, sort_keys=True),
                    now, now,
                ),
            )
        return self.draft(draft_id)

    def drafts(self) -> list[dict]:
        with self.store.connect() as conn:
            rows = conn.execute("SELECT * FROM quant_strategy WHERE status!='active' ORDER BY updated_at DESC").fetchall()
        return [self._decode(row, "definition", "compiled_plan") for row in rows]

    def draft(self, draft_id: str) -> dict:
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM quant_strategy WHERE strategy_id=?", (draft_id,)).fetchone()
        if row is None:
            raise KeyError("策略草稿不存在")
        return self._decode(row, "definition", "compiled_plan")

    def update_draft(self, draft_id: str, definition: dict) -> dict:
        current = self.draft(draft_id)
        if current["status"] == "published":
            raise ValueError("已启用策略不可修改，请创建新策略")
        name = str(definition.get("name") or current["name"])
        description = str(definition.get("description") or "")
        with self.store.connect() as conn:
            conn.execute("UPDATE quant_strategy SET name=?, description=?, definition_json=?, status='draft', updated_at=? WHERE strategy_id=?", (name, description, json.dumps(definition, ensure_ascii=False, sort_keys=True), utc_now(), draft_id))
        return self.draft(draft_id)

    def update_code(self, draft_id: str, source_code: str) -> dict:
        current = self.draft(draft_id)
        if current["status"] == "published":
            raise ValueError("已启用策略不可修改，请重新生成策略")
        if current.get("strategy_kind") != "python_code":
            raise ValueError("该草稿不是代码策略")
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE quant_strategy SET source_code=?, status='draft',
                   updated_at=? WHERE strategy_id=?""",
                (source_code, utc_now(), draft_id),
            )
        return self.draft(draft_id)

    def compile(self, draft_id: str) -> dict:
        draft = self.draft(draft_id)
        result = self.compiler.compile(draft["definition"])
        if draft.get("strategy_kind") == "python_code":
            code_result = validate_strategy_source(draft.get("source_code") or "")
            result["errors"] = [*result["errors"], *code_result["errors"]]
            result["status"] = "valid" if not result["errors"] else "invalid"
            result["source_hash"] = code_result["source_hash"]
            result["compiled_hash"] = canonical_hash({
                "compiled_plan": result["compiled_plan"],
                "source_hash": code_result["source_hash"],
            })
        if result["status"] == "valid":
            with self.store.connect() as conn:
                conn.execute(
                    """UPDATE quant_strategy SET compiled_plan_json=?, compiled_hash=?,
                       status='compiled', updated_at=? WHERE strategy_id=?""",
                    (json.dumps(result["compiled_plan"], ensure_ascii=False, sort_keys=True), result["compiled_hash"], utc_now(), draft_id),
                )
        return result

    def publish(self, draft_id: str) -> dict:
        compiled = self.compile(draft_id)
        if compiled["status"] != "valid":
            messages = "；".join(item["message"] for item in compiled["errors"])
            raise ValueError(f"策略编译失败：{messages}")
        draft = self.draft(draft_id)
        version_record = {
            **draft,
            "strategy_version_id": draft_id,
            "draft_id": draft_id,
            "version": 1,
            "compiled_plan": compiled["compiled_plan"],
            "compiled_hash": compiled["compiled_hash"],
        }
        source_path = self._write_version_artifact(version_record)
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE quant_strategy SET compiled_plan_json=?, compiled_hash=?,
                   source_path=?, status='active', updated_at=? WHERE strategy_id=?""",
                (json.dumps(compiled["compiled_plan"], ensure_ascii=False, sort_keys=True), compiled["compiled_hash"], source_path, utc_now(), draft_id),
            )
        return self.version(draft_id)

    def _artifact_path(self, version: dict) -> Path | None:
        if self.strategy_run_root is None:
            return None
        filename = "strategy.py" if version.get("strategy_kind") == "python_code" else "strategy.json"
        return self.strategy_run_root / version["draft_id"] / f"v{version['version']}" / filename

    def _write_version_artifact(self, version: dict) -> str | None:
        target = self._artifact_path(version)
        if target is None:
            return None
        if version.get("strategy_kind") == "python_code":
            source = version.get("source_code")
            if not source:
                return None
            content = source.rstrip() + "\n"
        else:
            artifact = {
                "schema_version": 1,
                "strategy_version_id": version["strategy_version_id"],
                "draft_id": version["draft_id"],
                "version": version["version"],
                "name": version["name"],
                "description": version.get("description", ""),
                "strategy_kind": version.get("strategy_kind", "structured"),
                "compiled_hash": version["compiled_hash"],
                "definition": version["definition"],
                "compiled_plan": version["compiled_plan"],
            }
            content = json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return str(target)

    def _ensure_published_artifacts(self) -> None:
        if self.strategy_run_root is None:
            return
        for version in self.versions():
            expected = self._artifact_path(version)
            if expected is None:
                continue
            if version.get("source_path") == str(expected) and expected.is_file():
                continue
            source_path = self._write_version_artifact(version)
            if source_path:
                with self.store.connect() as conn:
                    conn.execute(
                        "UPDATE quant_strategy SET source_path=? WHERE strategy_id=?",
                        (source_path, version["strategy_version_id"]),
                    )

    def _load_structured_artifact(self, version: dict) -> dict:
        source_path = version.get("source_path")
        if not source_path:
            return version
        target = Path(source_path)
        expected = self._artifact_path(version)
        if expected is not None and target.resolve() != expected.resolve():
            raise ValueError("策略文件路径与版本目录不一致")
        try:
            artifact = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("策略文件无法读取或格式无效") from exc
        if not isinstance(artifact, dict) or artifact.get("schema_version") != 1:
            raise ValueError("策略文件版本不受支持")
        identity_fields = ("strategy_version_id", "draft_id", "version")
        if any(artifact.get(field) != version[field] for field in identity_fields):
            raise ValueError("策略文件与已发布版本不匹配")
        compiled_plan = artifact.get("compiled_plan")
        if (
            artifact.get("compiled_hash") != version["compiled_hash"]
            or canonical_hash(compiled_plan) != version["compiled_hash"]
        ):
            raise ValueError("策略文件编译哈希校验失败")
        loaded = dict(version)
        loaded.update({
            "name": artifact.get("name", version["name"]),
            "description": artifact.get("description", version.get("description", "")),
            "definition": artifact.get("definition", version["definition"]),
            "compiled_plan": compiled_plan,
        })
        return loaded

    def version(self, version_id: str) -> dict:
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM quant_strategy WHERE strategy_id=?", (version_id,)).fetchone()
        if row is None:
            raise KeyError("策略版本不存在")
        return self._decode(row, "definition", "compiled_plan")

    def versions(self) -> list[dict]:
        with self.store.connect() as conn:
            rows = conn.execute("SELECT * FROM quant_strategy WHERE status='active' ORDER BY updated_at DESC").fetchall()
        return [self._decode(row, "definition", "compiled_plan") for row in rows]

    def runtime_strategy(self, version_id: str) -> dict:
        version = self.version(version_id)
        if version.get("strategy_kind", "structured") == "structured":
            version = self._load_structured_artifact(version)
        return StrategyRuntime.paper_strategy(version)

    def _ensure_system_lightgbm_strategy(self) -> str:
        definition = self.registry.template("lightgbm_research")
        definition.update({
            "name": "LightGBM 动态仓位模拟策略",
            "description": "沪深300 LightGBM 排名、公告研究、排名缓冲退出与A股模拟执行。",
        })
        compiled = self.compiler.compile(definition)
        if compiled["status"] != "valid":
            raise RuntimeError("系统 LightGBM 策略无法通过编译")
        with self.store.connect() as conn:
            existing = conn.execute(
                "SELECT strategy_id FROM quant_strategy WHERE compiled_hash=? AND status='active'",
                (compiled["compiled_hash"],),
            ).fetchone()
        if existing:
            return existing["strategy_id"]

        draft = self.create_draft(
            name=definition["name"],
            description=definition["description"],
            template_id="lightgbm_research",
        )
        self.update_draft(draft["draft_id"], definition)
        return self.publish(draft["draft_id"])["strategy_version_id"]
