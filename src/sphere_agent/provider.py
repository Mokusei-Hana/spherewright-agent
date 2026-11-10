"""Provider interface and OpenAI-compatible tool-calling implementation."""

import os
from typing import Protocol

import httpx


class Provider(Protocol):
    async def decide(self, messages: list, tools: list) -> dict: ...


class CompatibleProvider:
    def __init__(self, config: dict):
        if config["type"] != "openai-compatible":
            raise ValueError("MVP supports type=openai-compatible; add a Provider adapter for other APIs")
        self.config = config
        self.key = os.environ.get(config["api_key_env"])
        if not self.key:
            raise ValueError(f"Set environment variable {config['api_key_env']}")

    async def decide(self, messages: list, tools: list) -> dict:
        c = self.config
        # No automatic POST retry: errors stop the bounded run.
        async with httpx.AsyncClient(timeout=c["timeout_seconds"]) as client:
            response = await client.post(
                c["base_url"].rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {self.key}"},
                json={
                    "model": c["model"],
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "max_tokens": c["max_tokens"],
                },
            )
            response.raise_for_status()
            data = response.json()
        message = data["choices"][0]["message"]
        if not isinstance(message, dict):
            raise ValueError("Provider returned an invalid assistant message")
        return message
