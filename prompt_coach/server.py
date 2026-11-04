"""FastAPI server: serves the web UI and streams agent turns over SSE."""

from __future__ import annotations

import sys
import uuid
from collections.abc import AsyncIterator

import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config
from .agent import Agent, Session, describe_api_error, dumps_event
from .library import PromptLibrary
from .tools import ToolExecutor

STATIC_DIR = config.PROJECT_ROOT / "static"

app = FastAPI(title="Prompt Coach")

# The API key comes from the ANTHROPIC_API_KEY environment variable (or .env).
# Refuse to start without it rather than failing on the first chat message.
try:
    API_KEY = config.get_api_key()
except config.MissingAPIKeyError as e:
    sys.exit(f"Error: {e}")

client = anthropic.AsyncAnthropic(api_key=API_KEY)
library = PromptLibrary(config.LIBRARY_PATH)
agent = Agent(client, ToolExecutor(client, library))

# In-memory conversations, keyed by an id the browser keeps. They reset when the
# server restarts; the prompt library is what persists.
sessions: dict[str, Session] = {}
MAX_SESSIONS = 200


def get_session(session_id: str) -> Session:
    session = sessions.get(session_id)
    if session is None:
        if len(sessions) >= MAX_SESSIONS:
            sessions.pop(next(iter(sessions)))  # drop the oldest
        session = sessions[session_id] = Session()
    return session


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=64)
    message: str = Field(min_length=1, max_length=200_000)


class SavePromptRequest(BaseModel):
    name: str
    system_prompt: str = ""
    user_template: str = ""
    notes: str = ""


@app.get("/api/config")
async def get_config() -> dict:
    return {
        "agent_model": config.AGENT_MODEL,
        "target_model": config.TARGET_MODEL,
        "effort": config.AGENT_EFFORT,
    }


@app.post("/api/session")
async def new_session() -> dict:
    return {"session_id": uuid.uuid4().hex}


@app.post("/api/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    session = get_session(req.session_id)
    if session.lock.locked():
        raise HTTPException(409, "A response is already in progress for this conversation.")

    async def event_stream() -> AsyncIterator[str]:
        async with session.lock:
            try:
                async for event in agent.run_turn(session, req.message):
                    yield f"data: {dumps_event(event)}\n\n"
            except Exception as exc:  # surface any failure to the UI, then end the stream
                yield f"data: {dumps_event({'type': 'error', 'message': describe_api_error(exc)})}\n\n"
                yield f"data: {dumps_event({'type': 'done', 'usage': None})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.delete("/api/session/{session_id}")
async def reset_session(session_id: str) -> dict:
    sessions.pop(session_id, None)
    return {"ok": True}


@app.get("/api/prompts")
async def list_prompts() -> list[dict]:
    return library.list()


@app.get("/api/prompts/{name}")
async def get_prompt(name: str) -> dict:
    prompt = library.get(name)
    if prompt is None:
        raise HTTPException(404, "Prompt not found.")
    return prompt.to_dict(all_versions=True)


@app.post("/api/prompts")
async def save_prompt(req: SavePromptRequest) -> dict:
    try:
        prompt = library.save(req.name, req.system_prompt, req.user_template, req.notes)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return prompt.summary()


@app.delete("/api/prompts/{name}")
async def delete_prompt(name: str) -> dict:
    if not library.delete(name):
        raise HTTPException(404, "Prompt not found.")
    return {"ok": True}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
