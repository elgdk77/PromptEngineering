# Prompt Coach

An AI agent that helps you write, debug and improve prompts. Paste a prompt that
misbehaves, describe a task you need a prompt for, or ask about a technique. The
agent diagnoses the problems, drafts an improved version, **runs the draft against
Claude on realistic test inputs**, reads the real outputs, and iterates until the
prompt does the job.

Built on the [Claude API](https://platform.claude.com/docs) with the official
`anthropic` Python SDK, FastAPI, and a dependency-free web UI.

## What the agent can do

| Tool | Purpose |
| --- | --- |
| `run_prompt` | Run a system prompt and/or user template (with an `{{input}}` placeholder) on up to 10 test inputs in parallel, and return the actual outputs |
| `count_tokens` | Measure a prompt's input token count on the target model |
| `save_prompt` / `list_prompts` / `get_prompt` | Keep a versioned library of your prompts on disk |

The UI streams the agent's reasoning summary, its responses, and each test run
(expand a run to see the input and output side by side). Every code block has
**Copy** and **Save** buttons, and the library panel shows saved prompts with
their version history.

## Run it

Requires Python 3.10+ and an Anthropic API key.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/uvicorn prompt_coach.server:app --port 8000
```

Then open http://localhost:8000.

Instead of `ANTHROPIC_API_KEY`, you can sign in with `ant auth login`; the SDK
picks up that profile automatically.

## Configuration

All settings are environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `PROMPT_COACH_MODEL` | `claude-opus-5-5` | Model that runs the coaching agent |
| `PROMPT_COACH_TARGET_MODEL` | same as above | Model that test runs and token counts target; set it to the model your prompt will ship on |
| `PROMPT_COACH_EFFORT` | `high` | The agent's reasoning effort (`low` to `max`) |
| `PROMPT_COACH_DATA_DIR` | `./data` | Where the prompt library (`prompts.json`) is stored |

## Costs

The agent's own turns and every `run_prompt` call use your API credits. The
agent is instructed to test with 2–5 inputs per run unless you ask for more, and
each run is capped at 10 inputs and 8,000 output tokens per input. The system
prompt and conversation are prompt-cached, so long sessions re-read earlier
context at the cached rate. Token usage for each agent turn is shown under the
response (test runs are not included in that figure).

## How it works

- `prompt_coach/agent.py` runs a streaming tool-use loop on the Messages API with
  adaptive thinking. Conversation history is append-only, so earlier turns stay
  cached and thinking blocks stay valid.
- Requests opt into server-side refusal fallbacks (`fallbacks: "default"`), so if
  a safety classifier declines a request, the API retries it on Anthropic's
  recommended fallback model instead of failing.
- Tool inputs stream as they are generated (`eager_input_streaming`), so every
  input is validated in `prompt_coach/tools.py` before a tool runs.
- `prompt_coach/prompts.py` holds the agent's system prompt, which carries its
  prompt engineering playbook.
- `prompt_coach/server.py` serves the UI and streams each agent turn to the
  browser as server-sent events. Conversations are held in memory and reset when
  the server restarts. The prompt library persists on disk.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The tests run offline: the agent loop is exercised with a fake API client.
