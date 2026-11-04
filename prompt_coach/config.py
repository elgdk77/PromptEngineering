"""Runtime settings, read from environment variables."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Model that runs the coaching agent itself.
AGENT_MODEL = os.environ.get("PROMPT_COACH_MODEL", "claude-opus-5-5")
# Model that the agent's test runs (run_prompt / count_tokens) target. Set this to
# the model your prompt will actually ship on.
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
# Upper bounds on a single run_prompt call.
MAX_TEST_INPUTS = 10
TEST_CONCURRENCY = 5
TEST_MAX_TOKENS = 8000
