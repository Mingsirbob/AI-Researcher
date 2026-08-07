from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from app.core.config import Settings


class OpenAICompatibleProvider:
    """Small provider boundary for OpenAI-compatible chat APIs."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    async def complete_json(self, messages: list[dict[str, str]]) -> tuple[dict, dict]:
        if not self.settings.llm_configured:
            raise RuntimeError("LLM尚未配置")
        request = {
            "model": self.settings.quick_model,
            "messages": messages,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        timeout = httpx.Timeout(
            connect=self.settings.llm_connect_timeout_seconds,
            read=self.settings.llm_read_timeout_seconds,
            write=min(30.0, self.settings.llm_read_timeout_seconds),
            pool=self.settings.llm_connect_timeout_seconds,
        )
        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
            for attempt in range(1, self.settings.llm_max_attempts + 1):
                try:
                    response = await client.post(
                        self.settings.llm_base_url.rstrip("/") + "/chat/completions",
                        headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
                        json=request,
                    )
                    response.raise_for_status()
                    raw = response.json()
                    content = raw["choices"][0]["message"]["content"].strip()
                    if content.startswith("```"):
                        content = content.strip("`").removeprefix("json").strip()
                    return json.loads(content), {
                        "provider": "openai_compatible",
                        "model": self.settings.quick_model,
                        "usage": raw.get("usage", {}),
                        "attempts": attempt,
                    }
                except Exception as exc:
                    last_error = exc
                    if attempt >= self.settings.llm_max_attempts:
                        break
                    await asyncio.sleep(self.settings.llm_backoff_seconds * attempt)
        raise RuntimeError(f"LLM策略生成失败：{last_error}") from last_error
