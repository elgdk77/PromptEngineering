"""A small JSON-file-backed library of saved prompts, with version history."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

MAX_NAME_LENGTH = 80


@dataclass
class PromptVersion:
    version: int
    system_prompt: str
    user_template: str
    notes: str
    saved_at: str


@dataclass
class SavedPrompt:
    name: str
    versions: list[PromptVersion] = field(default_factory=list)

    @property
    def latest(self) -> PromptVersion:
        return self.versions[-1]

    def summary(self) -> dict:
        latest = self.latest
        return {
            "name": self.name,
            "version": latest.version,
            "saved_at": latest.saved_at,
            "notes": latest.notes,
        }

    def to_dict(self, all_versions: bool = False) -> dict:
        data = {"name": self.name, **asdict(self.latest)}
        if all_versions:
            data["history"] = [asdict(v) for v in self.versions]
        return data


def normalize_name(name: str) -> str:
    cleaned = " ".join(name.split())
    if not cleaned:
        raise ValueError("Prompt name must not be empty.")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise ValueError(f"Prompt name must be at most {MAX_NAME_LENGTH} characters.")
    return cleaned


class PromptLibrary:
    """Thread-safe store. Names are case-insensitive keys; display case is kept."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._prompts: dict[str, SavedPrompt] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        for item in raw.get("prompts", []):
            prompt = SavedPrompt(
                name=item["name"],
                versions=[PromptVersion(**v) for v in item["versions"]],
            )
            self._prompts[prompt.name.casefold()] = prompt

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "prompts": [
                {"name": p.name, "versions": [asdict(v) for v in p.versions]}
                for p in self._prompts.values()
            ]
        }
        # Write atomically so a crash mid-write can't corrupt the library.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def list(self) -> list[dict]:
        with self._lock:
            prompts = sorted(
                self._prompts.values(), key=lambda p: p.latest.saved_at, reverse=True
            )
            return [p.summary() for p in prompts]

    def get(self, name: str) -> SavedPrompt | None:
        with self._lock:
            return self._prompts.get(normalize_name(name).casefold())

    def save(
        self, name: str, system_prompt: str, user_template: str = "", notes: str = ""
    ) -> SavedPrompt:
        name = normalize_name(name)
        if not system_prompt.strip() and not user_template.strip():
            raise ValueError("Provide a system prompt, a user template, or both.")
        with self._lock:
            key = name.casefold()
            prompt = self._prompts.get(key) or SavedPrompt(name=name)
            prompt.versions.append(
                PromptVersion(
                    version=len(prompt.versions) + 1,
                    system_prompt=system_prompt,
                    user_template=user_template,
                    notes=notes,
                    saved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                )
            )
            self._prompts[key] = prompt
            self._write()
            return prompt

    def delete(self, name: str) -> bool:
        with self._lock:
            removed = self._prompts.pop(normalize_name(name).casefold(), None)
            if removed:
                self._write()
            return removed is not None
