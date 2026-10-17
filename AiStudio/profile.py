"""Persistent router profile (cookies, selected model, thinking effort)."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .config import (
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    EFFORT_TO_LEVEL,
    FALLBACK_MODELS,
    LEVEL_DESCRIPTIONS,
    default_profile_path,
)


def _fallback_meta(model_id: str) -> Dict[str, Any]:
    for item in FALLBACK_MODELS:
        if item["id"] == model_id:
            return {
                **item,
                "levels": ["minimal", "low", "medium", "high"],
                "thinking_mode": "level",
            }
    return {
        "id": model_id,
        "name": model_id.replace("models/", ""),
        "desc": "Модель из аккаунта AI Studio",
        "default_effort": "medium",
        "levels": ["minimal", "low", "medium", "high"],
        "thinking_mode": "level",
    }


class Profile:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path or default_profile_path())
        self.model: str = DEFAULT_MODEL
        self.model_meta: Dict[str, Any] = _fallback_meta(DEFAULT_MODEL)
        self.effort: str = DEFAULT_EFFORT
        self.web_api_key: Optional[str] = None
        self.cookies: Dict[str, str] = {}
        self.updated_at: int = 0

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Profile":
        profile = cls(path)
        if not profile.path.exists():
            return profile
        try:
            data = json.loads(profile.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return profile

        profile.model = data.get("model") or DEFAULT_MODEL
        profile.effort = data.get("effort") or DEFAULT_EFFORT
        if profile.effort not in EFFORT_TO_LEVEL:
            profile.effort = DEFAULT_EFFORT
        meta = data.get("model_meta")
        profile.model_meta = meta if isinstance(meta, dict) else _fallback_meta(profile.model)
        profile.model_meta.setdefault("levels", ["minimal", "low", "medium", "high"])
        profile.model_meta.setdefault("thinking_mode", "level")
        profile.web_api_key = data.get("web_api_key") or None
        cookies = data.get("cookies")
        profile.cookies = (
            {str(k): str(v) for k, v in cookies.items()}
            if isinstance(cookies, dict)
            else {}
        )
        profile.updated_at = int(data.get("updated_at") or 0)
        return profile

    def save(self) -> None:
        self.updated_at = int(time.time())
        data = {
            "model": self.model,
            "model_meta": self.model_meta,
            "effort": self.effort,
            "web_api_key": self.web_api_key,
            "cookies": self.cookies,
            "updated_at": self.updated_at,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
            os.replace(temp_name, self.path)
        finally:
            try:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            except OSError:
                pass

    def set_model(self, model_id: str, meta: Optional[Dict[str, Any]] = None) -> None:
        if not model_id.startswith("models/"):
            model_id = f"models/{model_id}"
        self.model = model_id
        self.model_meta = meta or _fallback_meta(model_id)
        levels = self.model_meta.get("levels") or ["minimal", "low", "medium", "high"]
        if self.effort not in levels:
            self.effort = self.model_meta.get("default_effort", "medium")
            if self.effort not in levels:
                self.effort = levels[0]

    def set_effort(self, effort: str) -> None:
        if effort in EFFORT_TO_LEVEL:
            self.effort = effort

    def effort_label(self) -> str:
        return LEVEL_DESCRIPTIONS.get(self.effort, self.effort.upper())

    @property
    def thinking_level(self) -> int:
        return EFFORT_TO_LEVEL.get(self.effort, 2)
