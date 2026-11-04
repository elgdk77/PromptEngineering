"""Runtime settings, read from environment variables."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load variables from a .env file in the project root, if there is one. Variables
# already set in the real environment take precedence over the file.
load_dotenv(PROJECT_ROOT / ".env", override=False)


class MissingAPIKeyError(RuntimeError):
    """ANTHROPIC_API_KEY is not set."""


def get_api_key() -> str:
    """Return the Anthropic API key from the ANTHROPIC_API_KEY environment variable."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise MissingAPIKeyError(
            "ANTHROPIC_API_KEY is not set. Export it in your shell "
            "(export ANTHROPIC_API_KEY=sk-ant-...) or add it to a .env file in "
            f"{PROJECT_ROOT}, then start the server again."
        )
    return key


# Model that runs the coaching agent itself.
AGENT_MODEL = os.environ.get("PROMPT_COACH_MODEL", "claude-opus-5-5")
# Model that count_tokens measures against. Set this to the model your prompt
# will actually ship on.
TARGET_MODEL = os.environ.get("PROMPT_COACH_TARGET_MODEL", AGENT_MODEL)
# Effort for the agent's own reasoning (low | medium | high | xhigh | max).
AGENT_EFFORT = os.environ.get("PROMPT_COACH_EFFORT", "high")

# Server-side refusal fallback: if a safety classifier declines a request, the API
# re-runs it on Anthropic's recommended fallback model inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

DATA_DIR = Path(os.environ.get("PROMPT_COACH_DATA_DIR", PROJECT_ROOT / "data"))
LIBRARY_PATH = DATA_DIR / "prompts.json"

# Safety valve on the agent loop: model requests per user turn.
MAX_AGENT_ITERATIONS = 25
