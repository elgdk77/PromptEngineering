"""Offline tests: the library, tool validation, and the agent loop with a fake client."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from prompt_coach import config
from prompt_coach.agent import Agent, Session, content_for_history
from prompt_coach.library import PromptLibrary
from prompt_coach.tools import ToolExecutor, ToolInputError, validate_input


# ---------- library ----------

def test_library_versions_and_persistence(tmp_path):
    path = tmp_path / "prompts.json"
    lib = PromptLibrary(path)
    lib.save("Support Bot", "v1 system", notes="first")
    lib.save("support  bot", "v2 system")  # same name, different case/spacing
    assert [p["version"] for p in lib.list()] == [2]

    reloaded = PromptLibrary(path)
    prompt = reloaded.get("SUPPORT BOT")
    assert prompt.name == "Support Bot"
    assert [v.system_prompt for v in prompt.versions] == ["v1 system", "v2 system"]
    assert reloaded.delete("support bot") and reloaded.list() == []


def test_library_rejects_empty(tmp_path):
    lib = PromptLibrary(tmp_path / "p.json")
    with pytest.raises(ValueError):
        lib.save("x", "", "")
    with pytest.raises(ValueError):
        lib.save("   ", "system")


# ---------- API key ----------

def test_api_key_read_from_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  sk-ant-test  ")
    assert config.get_api_key() == "sk-ant-test"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_api_key_raises(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_API_KEY", value)
    with pytest.raises(config.MissingAPIKeyError, match="ANTHROPIC_API_KEY"):
        config.get_api_key()


# ---------- validation ----------

def test_validate_run_prompt():
    args = validate_input(
        "run_prompt",
        {"label": "v1", "system_prompt": "", "test_inputs": ["a"], "user_template": "Q: {{input}}"},
    )
    assert args["effort"] is None and args["user_template"] == "Q: {{input}}"

    bad = [
        {"label": "v1", "system_prompt": "", "test_inputs": []},
        {"label": "v1", "system_prompt": "", "test_inputs": ["a"] * (config.MAX_TEST_INPUTS + 1)},
        {"label": "v1", "system_prompt": "", "test_inputs": ["a"], "user_template": "no placeholder"},
        {"label": "v1", "system_prompt": 3, "test_inputs": ["a"]},
        {"label": "v1", "system_prompt": "", "test_inputs": ["a"], "effort": "extreme"},
        {"label": "v1", "system_prompt": "", "test_inputs": ["a"], "extra": 1},
        "not an object",
    ]
    for args in bad:
        with pytest.raises(ToolInputError):
            validate_input("run_prompt", args)


def test_content_for_history_drops_pre_fallback_internal_blocks():
    content = [
        NS(type="thinking"), NS(type="text"), NS(type="tool_use"),
        NS(type="fallback"),
        NS(type="thinking"), NS(type="tool_use"),
    ]
    kept = [b.type for b in content_for_history(content)]
    assert kept == ["text", "fallback", "thinking", "tool_use"]
    plain = [NS(type="thinking"), NS(type="text")]
    assert content_for_history(plain) == plain


# ---------- agent loop with a fake client ----------

def _msg(content, stop_reason):
    usage = NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return NS(content=content, stop_reason=stop_reason, usage=usage, model="fake", stop_details=None)


class FakeStream:
    def __init__(self, message, events):
        self.message, self.events = message, events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            for e in self.events:
                yield e
        return gen()

    async def get_final_message(self):
        return self.message


class FakeClient:
    """Agent requests get scripted replies; run_prompt test calls get an echo."""

    def __init__(self, script):
        self.script = list(script)
        self.agent_requests = []
        self.beta = NS(messages=NS(stream=self._stream))

    def _stream(self, **params):
        if "tools" in params:  # an agent request
            # Snapshot: the agent keeps appending to the same history list.
            self.agent_requests.append({**params, "messages": list(params["messages"])})
            return self.script.pop(0)
        text = params["messages"][0]["content"]
        reply = _msg([NS(type="text", text=f"echo: {text}")], "end_turn")
        return FakeStream(reply, [])


def test_agent_runs_tool_then_answers(tmp_path):
    tool_block = NS(
        type="tool_use", id="tu_1", name="run_prompt",
        input={"label": "v1", "system_prompt": "Be brief.", "user_template": "Q: {{input}}",
               "test_inputs": ["hi", "bye"]},
    )
    first = FakeStream(
        _msg([NS(type="thinking", thinking="plan"), tool_block], "tool_use"),
        [NS(type="content_block_start", content_block=NS(type="tool_use", id="tu_1", name="run_prompt"))],
    )
    final_text = NS(type="text", text="Looks good.")
    second = FakeStream(
        _msg([final_text], "end_turn"),
        [NS(type="content_block_start", content_block=NS(type="text")),
         NS(type="content_block_delta", delta=NS(type="text_delta", text="Looks good."))],
    )
    client = FakeClient([first, second])
    agent = Agent(client, ToolExecutor(client, PromptLibrary(tmp_path / "p.json")))
    session = Session()

    async def collect():
        return [e async for e in agent.run_turn(session, "test my prompt")]

    events = asyncio.run(collect())
    types = [e["type"] for e in events]
    assert types == ["tool_pending", "tool_call", "tool_result", "text_start", "text_delta", "done"]

    result = next(e for e in events if e["type"] == "tool_result")
    assert not result["is_error"]
    assert [r["output"] for r in result["data"]["runs"]] == ["echo: Q: hi", "echo: Q: bye"]

    # History: user, assistant(tool_use), user(tool_result), assistant(text)
    roles = [m["role"] for m in session.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    tool_results = session.messages[2]["content"]
    assert tool_results[0]["tool_use_id"] == "tu_1"
    assert json.loads(tool_results[0]["content"])["label"] == "v1"

    # Second request carried the full history and the fallback opt-in.
    req = client.agent_requests[1]
    assert req["fallbacks"] == "default" and config.FALLBACK_BETA in req["betas"]
    assert req["thinking"]["type"] == "adaptive"
    assert len(req["messages"]) == 3


def test_agent_answers_invalid_tool_input_with_error(tmp_path):
    bad = NS(type="tool_use", id="tu_x", name="run_prompt", input={"label": "v1"})
    first = FakeStream(_msg([bad], "tool_use"), [])
    second = FakeStream(_msg([NS(type="text", text="ok")], "end_turn"), [])
    client = FakeClient([first, second])
    agent = Agent(client, ToolExecutor(client, PromptLibrary(tmp_path / "p.json")))
    session = Session()

    async def collect():
        return [e async for e in agent.run_turn(session, "go")]

    events = asyncio.run(collect())
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["is_error"] and "Invalid input" in result["content"]
    assert session.messages[2]["content"][0]["is_error"] is True
