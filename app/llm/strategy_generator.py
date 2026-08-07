from __future__ import annotations

from pathlib import Path

from .provider import OpenAICompatibleProvider


class StrategyGenerator:
    def __init__(self, provider: OpenAICompatibleProvider, prompt_path: Path):
        self.provider = provider
        self.prompt_path = prompt_path

    async def generate(
        self,
        *,
        requirement: str,
        name: str,
        pool_id: str,
        filter_pipeline_id: str,
        params: dict,
    ) -> dict:
        prompt = self.prompt_path.read_text(encoding="utf-8")
        payload, meta = await self.provider.complete_json([
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": (
                    f"策略名称：{name}\n股票池：{pool_id}\n筛选路径：{filter_pipeline_id}\n"
                    f"参数：{params}\n用户需求：{requirement}"
                ),
            },
        ])
        source = str(payload.get("source_code") or "").strip()
        if not source:
            raise ValueError("模型没有返回 source_code")
        return {
            "name": str(payload.get("name") or name),
            "description": str(payload.get("description") or requirement),
            "source_code": source,
            "generation_meta": meta,
        }
