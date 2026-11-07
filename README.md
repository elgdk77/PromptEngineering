https://gdkservices.co.uk
# Prompt Coach

An AI agent that helps you write, debug and improve prompts. Paste a prompt that
misbehaves, describe a task you need a prompt for, or ask about a technique. The
agent diagnoses the problems and writes you a new or improved prompt, ready to
copy, with an explanation of what changed.

The agent only writes prompts. It never runs them or shows what a model would
answer: if you paste "Summarize this article: …", you get a better summarization
prompt back, not a summary.

Built on the [Claude API](https://platform.claude.com/docs) with the official
`anthropic` Python SDK, FastAPI, and a dependency-free web UI.

## What the agent can do

| Tool | Purpose |
| --- | --- |
| `count_tokens` | Measure a prompt's input token count on the target model (size only; the prompt is not run) |
| `save_prompt` / `list_prompts` / `get_prompt` | Keep a versioned library of your prompts on disk |

The UI streams the agent's reasoning summary and its responses. Every code block
has **Copy** and **Save** buttons, and the library panel shows saved prompts with
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

The app reads your key from the `ANTHROPIC_API_KEY` environment variable. Instead
of exporting it in every shell, you can put it in a `.env` file in the project
root (copy [.env.example](.env.example)); `.env` is ignored by git. A variable
set in the shell wins over the same one in `.env`. If no key is set, the server
refuses to start and says so.

## Configuration

All settings are environment variables (or `.env` entries):

| Variable | Default | Meaning |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | required | Your Anthropic API key |
| `PROMPT_COACH_MODEL` | `claude-opus-5-5` | Model that runs the coaching agent |
| `PROMPT_COACH_TARGET_MODEL` | same as above | Model that token counts are measured on; set it to the model your prompt will ship on |
| `PROMPT_COACH_EFFORT` | `high` | The agent's reasoning effort (`low` to `max`) |
| `PROMPT_COACH_DATA_DIR` | `./data` | Where the prompt library (`prompts.json`) is stored |
| `PROMPT_COACH_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | Hostnames the server answers to (comma-separated, ports ignored); add yours if you reach it by another name |

The server has no login, so it only answers requests addressed to the hosts
above. For anything that changes data, it also refuses requests whose `Origin`
header is another site. Together these stop other web pages, including ones
using DNS rebinding, from reading your library or using your API credits
through it.

## Costs

Each agent turn uses your API credits; nothing else calls a model (token
counting is free). The system prompt and conversation are prompt-cached, so long
sessions re-read earlier context at the cached rate. Token usage for each agent
turn is shown under the response.

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
  prompt engineering playbook and the rule that it writes prompts rather than
  carrying them out.
- `prompt_coach/server.py` serves the UI and streams each agent turn to the
  browser as server-sent events. Conversations are held in memory and reset when
  the server restarts. The prompt library persists on disk.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The tests run offline: the agent loop is exercised with a fake API client.
