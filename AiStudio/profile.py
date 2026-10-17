"""Active router profile backed by SQLite, with one-time JSON migration."""

from __future__ import annotations

import json
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
from .storage import get_storage


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
        self.account_id: Optional[int] = None
        self.name: str = "default"
        self.model: str = DEFAULT_MODEL
        self.model_meta: Dict[str, Any] = _fallback_meta(DEFAULT_MODEL)
        self.effort: str = DEFAULT_EFFORT
        self.web_api_key: Optional[str] = None
        self.cookies: Dict[str, str] = {}
        self.updated_at: int = 0

    # ------------------------------------------------------------------ loading
    def _apply_account(self, account: Dict[str, Any]) -> None:
        self.account_id = int(account["id"])
        self.name = str(account["name"])
        self.cookies = dict(account.get("cookies") or {})
        self.web_api_key = account.get("web_api_key")
        self.model = account.get("model") or DEFAULT_MODEL
        self.effort = account.get("effort") or DEFAULT_EFFORT
        if self.effort not in EFFORT_TO_LEVEL:
            self.effort = DEFAULT_EFFORT
        self.model_meta = _fallback_meta(self.model)
        self.updated_at = int(account.get("updated_at") or 0)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Profile":
        profile = cls(path)
        storage = get_storage()
        account = storage.get_active_account()
        if account:
            profile._apply_account(account)
            return profile

        # Import the legacy router_profile.json exactly once. After migration
        # the SQLite database is the source of truth; deleting a bot profile
        # must not resurrect old cookies from the JSON file.
        imported = storage.get_setting("legacy_imported") == "1"
        if profile.path.exists() and not imported:
            try:
                data = json.loads(profile.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = None
            if isinstance(data, dict):
                profile.model = data.get("model") or DEFAULT_MODEL
                profile.effort = data.get("effort") or DEFAULT_EFFORT
                if profile.effort not in EFFORT_TO_LEVEL:
                    profile.effort = DEFAULT_EFFORT
                meta = data.get("model_meta")
                if isinstance(meta, dict):
                    profile.model_meta = meta
                profile.model_meta.setdefault(
                    "levels", ["minimal", "low", "medium", "high"]
                )
                profile.model_meta.setdefault("thinking_mode", "level")
                profile.web_api_key = data.get("web_api_key") or None
                cookies = data.get("cookies")
                profile.cookies = (
                    {str(k): str(v) for k, v in cookies.items()}
                    if isinstance(cookies, dict)
                    else {}
                )
                profile.updated_at = int(data.get("updated_at") or 0)
                storage.set_setting("legacy_imported", "1")
                if profile.cookies:
                    account = storage.import_account(
                        "default",
                        profile.cookies,
                        profile.web_api_key,
                        profile.model,
                        profile.effort,
                        set_active=True,
                    )
                    profile._apply_account(account)
                    return profile
            else:
                storage.set_setting("legacy_imported", "1")

        return profile

    # ------------------------------------------------------------------ saving
    def save(self) -> None:
        """Persist the active account. The legacy JSON is never rewritten."""
        if not self.cookies and self.account_id is None:
            return
        account = get_storage().save_active_account(
            self.cookies,
            self.web_api_key,
            self.model,
            self.effort,
        )
        self._apply_account(account)

    def save_cookies(self, cookies: Dict[str, str]) -> None:
        """Called by AistudioAuth after Google rotates session cookies."""
        self.cookies = dict(cookies)
        storage = get_storage()
        account = storage.update_active_cookies(self.cookies)
        if account is None:
            account = storage.save_active_account(
                self.cookies,
                self.web_api_key,
                self.model,
                self.effort,
            )
        self._apply_account(account)

    # ------------------------------------------------------------------ setters
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
