"""Provider interface and OpenAI-compatible tool-calling implementation."""

import os
import json
from typing import Protocol

import httpx


class Provider(Protocol):
    async def decide(self, messages: list, tools: list) -> dict: ...


class ProviderError(RuntimeError):
    """Invalid, incomplete or ambiguous tool-calling response; no tool was dispatched."""


def context_window(messages, max_bytes):
    """Keep the initial instructions and goal, and only complete assistant/tool pairs."""
    head, history = messages[:2], messages[2:]
    size = lambda value: len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    if size(head) > max_bytes:
        raise ProviderError("System instructions and goal exceed context budget")
    tail = []
    for index in range(len(history) - 2, -1, -2):
        pair = history[index:index + 2]
        if size(head + pair + tail) > max_bytes:
            break
        tail = pair + tail
    return head + tail


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
                    "messages": context_window(messages, c.get("context_bytes", 64000)
                                               - len(json.dumps(tools).encode("utf-8")) - c["max_tokens"] * 4),
                    "tools": tools,
                    "tool_choice": "auto",
                    "max_tokens": c["max_tokens"],
                },
            )
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError as exc:
                raise ProviderError("Provider response is not JSON; no calls executed") from exc
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Missing assistant choice") from exc
        if choice.get("finish_reason") not in ("stop", "tool_calls"):
            raise ProviderError("Truncated or unsupported completion; no calls executed")
        if not isinstance(message, dict):
            raise ProviderError("Provider returned an invalid assistant message")
        calls = message.get("tool_calls") or []
        if not isinstance(calls, list) or len(calls) > 1:
            raise ProviderError("Multiple tool calls refused in full; no calls executed")
        for call in calls:
            try:
                if not isinstance(call["id"], str) or not call["id"] or call["type"] != "function":
                    raise ValueError()
                arguments = json.loads(call["function"]["arguments"])
                if not isinstance(arguments, dict) or not isinstance(call["function"]["name"], str):
                    raise ValueError()
            except (KeyError, TypeError, ValueError) as exc:
                raise ProviderError("Malformed tool call or non-JSON arguments; no calls executed") from exc
        return message
