"""The coaching agent: a streaming tool-use loop over the Claude Messages API.

`Agent.run_turn` takes one user message, drives the model/tool loop until the
model finishes, and yields UI events (plain dicts) as it goes. Conversation
history is append-only: assistant turns are stored exactly as the API returned
them, which keeps thinking blocks valid and the prompt cache warm.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import anthropic

from . import config
from .prompts import SYSTEM_PROMPT
from .tools import TOOLS, ToolExecutor

Event = dict[str, Any]

# Block types that must not be echoed back when they precede a fallback boundary.
_PRE_FALLBACK_DROP = {"thinking", "redacted_thinking", "tool_use", "server_tool_use"}


@dataclass
class Session:
    messages: list[Any] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def content_for_history(content: list[Any]) -> list[Any]:
    """Prepare an assistant turn's content for the history.

    After a mid-output refusal fallback, thinking and tool_use blocks that came
    before the last `fallback` block belong to the declined model and must not be
    echoed back. Everything else is kept unchanged.
    """
    last_fallback = max(
        (i for i, b in enumerate(content) if b.type == "fallback"), default=-1
    )
    if last_fallback < 0:
        return list(content)
    return [
        b
        for i, b in enumerate(content)
        if i > last_fallback or b.type not in _PRE_FALLBACK_DROP
    ]


def _usage_dict(usage: Any) -> dict[str, int]:
    keys = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )
    return {k: getattr(usage, k, 0) or 0 for k in keys}


class Agent:
    def __init__(self, client: anthropic.AsyncAnthropic, executor: ToolExecutor):
        self.client = client
        self.executor = executor

    def _request_params(self, messages: list[Any]) -> dict[str, Any]:
        return {
            "model": config.AGENT_MODEL,
            "max_tokens": 64000,
            # Frozen system prompt, cached; top-level cache_control also caches the
            # growing conversation so each turn re-reads the prefix cheaply.
            "system": [
                {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
            ],
            "cache_control": {"type": "ephemeral"},
            "tools": TOOLS,
            "messages": messages,
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": config.AGENT_EFFORT},
            "betas": [config.FALLBACK_BETA],
            "fallbacks": "default",
        }

    async def run_turn(self, session: Session, user_text: str) -> AsyncIterator[Event]:
        session.messages.append({"role": "user", "content": user_text})
        totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        json_retries = 0
        iterations = 0

        while True:
            iterations += 1
            if iterations > config.MAX_AGENT_ITERATIONS:
                yield {
                    "type": "notice",
                    "message": "Stopped after too many steps in one turn. Send a message to continue.",
                }
                break

            try:
                message = None
                async with self.client.beta.messages.stream(
                    **self._request_params(session.messages)
                ) as stream:
                    async for event in stream:
                        for ui_event in self._translate(event):
                            yield ui_event
                    message = await stream.get_final_message()
                json_retries = 0
            except ValueError:
                # A tool input the SDK could not parse at all. There is no complete
                # tool_use block to answer, so re-issue the request (bounded).
                json_retries += 1
                if json_retries > 2:
                    yield {"type": "error", "message": "The model produced malformed tool input repeatedly."}
                    break
                yield {"type": "notice", "message": "Retrying after a malformed tool call."}
                continue

            for k, v in _usage_dict(message.usage).items():
                totals[k] += v

            if any(b.type == "fallback" for b in message.content):
                yield {
                    "type": "notice",
                    "message": f"A safety classifier declined part of this response; {message.model} continued it.",
                }

            if message.stop_reason == "refusal":
                category = getattr(message.stop_details, "category", None)
                yield {
                    "type": "error",
                    "message": "The model declined this request"
                    + (f" (category: {category})." if category else ".")
                    + " Try rephrasing it.",
                }
                # Not appended: the declined turn is discarded and the user's message
                # stays in history, so their next message continues from it.
                break

            history_content = content_for_history(message.content)
            session.messages.append({"role": "assistant", "content": history_content})

            if message.stop_reason == "pause_turn":
                continue

            tool_uses = [b for b in history_content if b.type == "tool_use"]
            if not tool_uses:
                if message.stop_reason == "max_tokens":
                    yield {"type": "notice", "message": "The response hit the length limit."}
                break

            if message.stop_reason == "max_tokens":
                # A truncated tool input parses as a valid-looking partial object;
                # never run it. Answer it with errors so the history stays valid.
                session.messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": b.id,
                                "content": "Not run: tool input was truncated at max_tokens.",
                                "is_error": True,
                            }
                            for b in tool_uses
                        ],
                    }
                )
                yield {"type": "error", "message": "The response was cut off mid tool call. Send a message to continue."}
                break

            for b in tool_uses:
                yield {"type": "tool_call", "id": b.id, "name": b.name, "input": b.input}

            outcomes = await asyncio.gather(
                *(self.executor.execute(b.name, b.input) for b in tool_uses)
            )
            results = []
            for b, outcome in zip(tool_uses, outcomes):
                yield {
                    "type": "tool_result",
                    "id": b.id,
                    "name": b.name,
                    "is_error": outcome.is_error,
                    "content": outcome.content,
                    "data": outcome.data,
                }
                if outcome.library_changed:
                    yield {"type": "library_changed"}
                result: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": outcome.content,
                }
                if outcome.is_error:
                    result["is_error"] = True
                results.append(result)
            # All results for one assistant turn go back in a single user message.
            session.messages.append({"role": "user", "content": results})

        yield {"type": "done", "usage": totals}

    @staticmethod
    def _translate(event: Any) -> list[Event]:
        """Map raw stream events to the small set the browser UI renders."""
        if event.type == "content_block_start":
            block = event.content_block
            if block.type == "thinking":
                return [{"type": "thinking_start"}]
            if block.type == "text":
                return [{"type": "text_start"}]
            if block.type == "tool_use":
                return [{"type": "tool_pending", "id": block.id, "name": block.name}]
            return []
        if event.type == "content_block_delta":
            delta = event.delta
            if delta.type == "thinking_delta" and delta.thinking:
                return [{"type": "thinking_delta", "text": delta.thinking}]
            if delta.type == "text_delta":
                return [{"type": "text_delta", "text": delta.text}]
        return []


def describe_api_error(exc: Exception) -> str:
    """A user-facing message for an exception raised out of the agent loop."""
    if isinstance(exc, anthropic.AuthenticationError):
        return "The API rejected the key in ANTHROPIC_API_KEY. Check it and restart the server."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "This API key does not have access to the requested model."
    if isinstance(exc, anthropic.NotFoundError):
        return f"Model not found: {config.AGENT_MODEL}. Check PROMPT_COACH_MODEL."
    if isinstance(exc, anthropic.RateLimitError):
        return "Rate limited by the API. Wait a moment and try again."
    if isinstance(exc, anthropic.BadRequestError):
        return f"The API rejected the request: {exc.message}"
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500:
            return f"The API had a server error ({exc.status_code}). Try again shortly."
        return f"API error ({exc.status_code}): {exc.message}"
    if isinstance(exc, anthropic.APIConnectionError):
        return "Could not reach the Anthropic API. Check your network connection."
    return f"Unexpected error: {exc}"


def dumps_event(event: Event) -> str:
    return json.dumps(event, ensure_ascii=False, default=str)
