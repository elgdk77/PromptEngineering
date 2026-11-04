"""Tool definitions for the coaching agent, plus validation and execution.

Tools are declared with `eager_input_streaming`, which means the API does not
validate their inputs, so every input is validated here before it runs.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import anthropic

from . import config
from .library import PromptLibrary

INPUT_PLACEHOLDER = "{{input}}"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

TOOLS: list[dict[str, Any]] = [
    {
        "name": "run_prompt",
        "description": (
            "Run a candidate prompt against the target Claude model and return the "
            "actual outputs, one per test input. Use it to test a draft (or get a "
            "baseline for an existing prompt) before recommending it. Each test input "
            "becomes the user message; if user_template is given, each input is "
            "substituted for {{input}} in the template instead. Inputs run in "
            "parallel. Every run costs real API tokens: prefer 2-5 representative "
            "inputs including an edge case."
        ),
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": "Short name for this run shown to the user, e.g. 'v2 with examples'.",
                },
                "system_prompt": {
                    "type": "string",
                    "description": "System prompt to test. Empty string for none.",
                },
                "user_template": {
                    "type": "string",
                    "description": (
                        "Optional user message template containing the literal "
                        "placeholder {{input}}. Omit to send each test input as-is."
                    ),
                },
                "test_inputs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": config.MAX_TEST_INPUTS,
                    "description": "Realistic inputs to run the prompt on.",
                },
                "effort": {
                    "type": "string",
                    "enum": list(EFFORT_LEVELS),
                    "description": (
                        "Optional effort level for the test model, to match how the "
                        "prompt will be deployed. Omit to use the model default."
                    ),
                },
            },
            "required": ["label", "system_prompt", "test_inputs"],
            "additionalProperties": False,
        },
    },
    {
        "name": "count_tokens",
        "description": (
            "Count the input tokens a prompt uses on the target model. Useful when "
            "cost or latency matters, or to compare the size of two versions."
        ),
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "system_prompt": {"type": "string"},
                "user_message": {"type": "string"},
            },
            "required": ["system_prompt", "user_message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "save_prompt",
        "description": (
            "Save a prompt to the user's library. Saving an existing name adds a new "
            "version. Only call this when the user asks or agrees to save."
        ),
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short descriptive name."},
                "system_prompt": {"type": "string"},
                "user_template": {
                    "type": "string",
                    "description": "User message template, may contain {{input}}.",
                },
                "notes": {
                    "type": "string",
                    "description": "What this version changed and why; test results.",
                },
            },
            "required": ["name", "system_prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_prompts",
        "description": "List the prompts saved in the user's library.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_prompt",
        "description": "Fetch a saved prompt by name, including its version history.",
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
]

TOOL_NAMES = {t["name"] for t in TOOLS}


class ToolInputError(ValueError):
    """The model sent a tool input that does not match the tool's schema."""


def _require_str(args: dict, key: str, *, required: bool = True, default: str = "") -> str:
    if key not in args:
        if required:
            raise ToolInputError(f"Missing required field '{key}'.")
        return default
    value = args[key]
    if not isinstance(value, str):
        raise ToolInputError(f"Field '{key}' must be a string.")
    return value


def validate_input(name: str, args: Any) -> dict[str, Any]:
    """Return a cleaned copy of `args`, or raise ToolInputError."""
    if name not in TOOL_NAMES:
        raise ToolInputError(f"Unknown tool '{name}'.")
    if not isinstance(args, dict):
        raise ToolInputError("Tool input must be a JSON object.")
    schema = next(t for t in TOOLS if t["name"] == name)["input_schema"]
    unknown = set(args) - set(schema["properties"])
    if unknown:
        raise ToolInputError(f"Unexpected field(s): {', '.join(sorted(unknown))}.")

    if name == "run_prompt":
        inputs = args.get("test_inputs")
        if not isinstance(inputs, list) or not inputs:
            raise ToolInputError("'test_inputs' must be a non-empty array of strings.")
        if len(inputs) > config.MAX_TEST_INPUTS:
            raise ToolInputError(f"At most {config.MAX_TEST_INPUTS} test inputs per run.")
        if not all(isinstance(i, str) and i.strip() for i in inputs):
            raise ToolInputError("Every test input must be a non-empty string.")
        template = _require_str(args, "user_template", required=False)
        if template and INPUT_PLACEHOLDER not in template:
            raise ToolInputError(f"'user_template' must contain {INPUT_PLACEHOLDER}.")
        effort = args.get("effort")
        if effort is not None and effort not in EFFORT_LEVELS:
            raise ToolInputError(f"'effort' must be one of {', '.join(EFFORT_LEVELS)}.")
        return {
            "label": _require_str(args, "label").strip() or "Test run",
            "system_prompt": _require_str(args, "system_prompt"),
            "user_template": template,
            "test_inputs": inputs,
            "effort": effort,
        }
    if name == "count_tokens":
        return {
            "system_prompt": _require_str(args, "system_prompt"),
            "user_message": _require_str(args, "user_message"),
        }
    if name == "save_prompt":
        return {
            "name": _require_str(args, "name"),
            "system_prompt": _require_str(args, "system_prompt"),
            "user_template": _require_str(args, "user_template", required=False),
            "notes": _require_str(args, "notes", required=False),
        }
    if name == "get_prompt":
        return {"name": _require_str(args, "name")}
    return {}  # list_prompts


@dataclass
class ToolOutcome:
    content: str  # what goes back to the model in the tool_result
    is_error: bool = False
    data: Any = None  # structured payload for the UI
    library_changed: bool = False


def _response_text(message: Any) -> str:
    return "".join(b.text for b in message.content if b.type == "text")


class ToolExecutor:
    def __init__(self, client: anthropic.AsyncAnthropic, library: PromptLibrary):
        self.client = client
        self.library = library
        self._semaphore = asyncio.Semaphore(config.TEST_CONCURRENCY)

    async def execute(self, name: str, raw_args: Any) -> ToolOutcome:
        try:
            args = validate_input(name, raw_args)
        except ToolInputError as e:
            return ToolOutcome(content=f"Invalid input: {e}", is_error=True)
        try:
            handler = getattr(self, f"_{name}")
            return await handler(**args)
        except ValueError as e:  # library validation errors
            return ToolOutcome(content=str(e), is_error=True)
        except anthropic.APIError as e:
            return ToolOutcome(content=f"API error while running tool: {e}", is_error=True)

    async def _run_one(self, system_prompt: str, user_message: str, effort: str | None) -> dict:
        params: dict[str, Any] = {
            "model": config.TARGET_MODEL,
            "max_tokens": config.TEST_MAX_TOKENS,
            "messages": [{"role": "user", "content": user_message}],
            "betas": [config.FALLBACK_BETA],
            "fallbacks": "default",
        }
        if system_prompt.strip():
            params["system"] = system_prompt
        if effort:
            params["output_config"] = {"effort": effort}
        async with self._semaphore:
            try:
                async with self.client.beta.messages.stream(**params) as stream:
                    message = await stream.get_final_message()
            except anthropic.APIStatusError as e:
                return {"error": f"{e.status_code}: {e.message}"}
            except anthropic.APIConnectionError as e:
                return {"error": f"connection error: {e}"}
        result = {
            "output": _response_text(message),
            "stop_reason": message.stop_reason,
            "output_tokens": message.usage.output_tokens,
            "model": message.model,
        }
        if message.stop_reason == "refusal":
            details = message.stop_details
            result["refusal_category"] = getattr(details, "category", None)
        return result

    async def _run_prompt(
        self,
        label: str,
        system_prompt: str,
        user_template: str,
        test_inputs: list[str],
        effort: str | None,
    ) -> ToolOutcome:
        messages = [
            user_template.replace(INPUT_PLACEHOLDER, item) if user_template else item
            for item in test_inputs
        ]
        results = await asyncio.gather(
            *(self._run_one(system_prompt, m, effort) for m in messages)
        )
        runs = [{"input": item, **res} for item, res in zip(test_inputs, results)]
        payload = {"label": label, "model": config.TARGET_MODEL, "runs": runs}
        all_failed = all("error" in r for r in runs)
        return ToolOutcome(
            content=json.dumps(payload, ensure_ascii=False),
            is_error=all_failed,
            data=payload,
        )

    async def _count_tokens(self, system_prompt: str, user_message: str) -> ToolOutcome:
        params: dict[str, Any] = {
            "model": config.TARGET_MODEL,
            "messages": [{"role": "user", "content": user_message or "(empty)"}],
        }
        if system_prompt.strip():
            params["system"] = system_prompt
        result = await self.client.messages.count_tokens(**params)
        payload = {"model": config.TARGET_MODEL, "input_tokens": result.input_tokens}
        return ToolOutcome(content=json.dumps(payload), data=payload)

    async def _save_prompt(
        self, name: str, system_prompt: str, user_template: str, notes: str
    ) -> ToolOutcome:
        prompt = self.library.save(name, system_prompt, user_template, notes)
        payload = prompt.summary()
        return ToolOutcome(
            content=f"Saved '{prompt.name}' as version {prompt.latest.version}.",
            data=payload,
            library_changed=True,
        )

    async def _list_prompts(self) -> ToolOutcome:
        items = self.library.list()
        if not items:
            return ToolOutcome(content="The library is empty.", data=[])
        return ToolOutcome(content=json.dumps(items, ensure_ascii=False), data=items)

    async def _get_prompt(self, name: str) -> ToolOutcome:
        prompt = self.library.get(name)
        if prompt is None:
            names = ", ".join(p["name"] for p in self.library.list()) or "none"
            return ToolOutcome(
                content=f"No saved prompt named '{name}'. Saved prompts: {names}.",
                is_error=True,
            )
        data = prompt.to_dict(all_versions=True)
        return ToolOutcome(content=json.dumps(data, ensure_ascii=False), data=data)
