"""Cookie-authenticated Google AI Studio RPC client."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from .config import (
    FALLBACK_MODELS,
    MODEL_PRIORITY,
    NON_CHAT_MARKERS,
    ORIGIN,
    RPC_BASE,
    USER_AGENT,
)
from .cookies import cookie_header


class AistudioAuth:
    def __init__(
        self,
        cookies: Dict[str, str],
        web_api_key: Optional[str] = None,
        profile_path: Optional[Path] = None,
    ):
        self.cookies = dict(cookies)
        self.web_api_key = web_api_key
        self.profile_path = profile_path
        self.session = requests.Session()
        self._browser_headers: Dict[str, str] = {}

    # ------------------------------------------------------------------ auth
    def _authorization(self) -> str:
        values = [
            ("SAPISIDHASH", self.cookies.get("SAPISID")),
            ("SAPISID1PHASH", self.cookies.get("__Secure-1PAPISID")),
            ("SAPISID3PHASH", self.cookies.get("__Secure-3PAPISID")),
        ]
        if not any(value for _, value in values):
            raise ValueError("В куках не найден SAPISID")
        timestamp = int(time.time())
        parts = []
        for name, value in values:
            if not value:
                continue
            digest = hashlib.sha1(
                f"{timestamp} {value} {ORIGIN}".encode("utf-8")
            ).hexdigest()
            parts.append(f"{name} {timestamp}_{digest}")
        return " ".join(parts)

    def set_browser_headers(self, headers: Dict[str, str]) -> None:
        """Keep the AI Studio headers that are tied to the minted token.

        Gemini 4 Argon rejects requests without the browser's
        ``X-AIStudio-G1-Tier`` context; the ext/visit IDs are copied as well
        when the front-end provides them.
        """
        allowed = {
            "x-aistudio-g1-tier",
            "x-aistudio-visit-id",
            "x-goog-ext-519733851-bin",
            "x-user-agent",
            "accept",
        }
        selected: Dict[str, str] = {}
        for key, value in (headers or {}).items():
            lower = key.lower()
            if lower in allowed or lower.startswith("sec-ch-ua"):
                selected[key] = value
        self._browser_headers = selected

    def headers(self) -> Dict[str, str]:
        if not self.web_api_key:
            raise RuntimeError("Не задан web_api_key AI Studio")
        headers = {
            "Authorization": self._authorization(),
            "Content-Type": "application/json+protobuf",
            "Origin": ORIGIN,
            "Referer": f"{ORIGIN}/",
            "Accept-Language": "en-US,en;q=0.9",
            "X-Goog-Api-Key": self.web_api_key,
            "X-Goog-AuthUser": "0",
            "X-User-Agent": "grpc-web-javascript/0.1",
            "X-AIStudio-G1-Tier": "TIER1",
            "User-Agent": USER_AGENT,
            "Cookie": cookie_header(self.cookies),
        }
        canonical = {key.lower(): key for key in headers}
        for key, value in self._browser_headers.items():
            existing = canonical.get(key.lower())
            if existing:
                headers[existing] = value
            else:
                headers[key] = value
                canonical[key.lower()] = key
        return headers

    # ------------------------------------------------------------------- HTTP
    def _sync_rotated_cookies(self, response: requests.Response) -> None:
        updated = False
        for name, value in response.cookies.items():
            if self.cookies.get(name) != value:
                self.cookies[name] = value
                updated = True
        if updated:
            self._save_rotated_cookies()

    def _save_rotated_cookies(self) -> None:
        """Persist rotating session cookies without touching unrelated profile data."""
        if not self.profile_path or not self.profile_path.exists():
            return
        try:
            data = json.loads(self.profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        data["cookies"] = self.cookies
        data["updated_at"] = int(time.time())
        try:
            self.profile_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    def rpc(
        self, method: str, payload: Any, timeout: int = 30
    ) -> Tuple[int, Any]:
        url = f"{RPC_BASE}/{method}"
        try:
            response = self.session.post(
                url,
                headers=self.headers(),
                json=payload,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            return 0, str(exc)
        self._sync_rotated_cookies(response)
        try:
            data = response.json()
        except ValueError:
            data = response.text
        return response.status_code, data

    # --------------------------------------------------------------- bootstrap
    def bootstrap(self) -> bool:
        """Extract the page key if the profile does not contain one."""
        if self.web_api_key:
            return True
        try:
            response = self.session.get(
                f"{ORIGIN}/prompts/new_chat",
                headers={
                    "User-Agent": USER_AGENT,
                    "Cookie": cookie_header(self.cookies),
                },
                timeout=20,
            )
        except requests.RequestException:
            return False
        self._sync_rotated_cookies(response)
        if response.status_code != 200:
            return False
        match = re.search(r'"WIu0Nc"\s*:\s*"([^"]+)"', response.text)
        if not match:
            keys = re.findall(r"AIzaSy[A-Za-z0-9_-]{33}", response.text)
            if keys:
                self.web_api_key = keys[0]
                return True
            return False
        self.web_api_key = match.group(1)
        return True

    # ----------------------------------------------------------------- models
    def list_models(self) -> List[Dict[str, Any]]:
        if not self.web_api_key and not self.bootstrap():
            return []
        status, data = self.rpc("ListModels", [])
        if status != 200:
            return []
        return parse_models(data)

    # -------------------------------------------------------------- generation
    def generate(self, payload: Any, timeout: int = 120) -> Tuple[int, Any]:
        return self.rpc("GenerateContent", payload, timeout=timeout)


# ---------------------------------------------------------------------------
# ListModels response parsing
# ---------------------------------------------------------------------------
def _is_chat_model(model_id: str) -> bool:
    lowered = model_id.lower()
    if any(marker in lowered for marker in NON_CHAT_MARKERS):
        return False
    if any(
        marker in lowered
        for marker in ("featured-", "deep-research", "antigravity", "agent")
    ):
        return False
    return True


def _default_effort(model_id: str) -> str:
    if "lite" in model_id:
        return "low"
    if any(marker in model_id for marker in ("gemini-4", "pro")):
        return "high"
    return "medium"


def _display_name(entry: List[Any], model_id: str) -> str:
    for index in (3, 2):
        if len(entry) > index and isinstance(entry[index], str) and entry[index].strip():
            return entry[index].strip()
    return model_id.replace("models/", "")


def _description(entry: List[Any], name: str) -> str:
    for value in entry[4:]:
        if (
            isinstance(value, str)
            and len(value) > 30
            and not value.startswith("http")
            and value.strip() != name
        ):
            return value.strip()
    return "Модель из аккаунта AI Studio"


def parse_models(raw: Any) -> List[Dict[str, Any]]:
    """Extract model metadata from the protobuf-JSON ListModels response."""
    entries: List[List[Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            if (
                node
                and isinstance(node[0], str)
                and node[0].startswith("models/")
                and len(node) >= 5
            ):
                entries.append(node)
                return
            for item in node:
                walk(item)

    walk(raw)

    fallback = {item["id"]: item for item in FALLBACK_MODELS}
    by_id: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        model_id = entry[0]
        if not _is_chat_model(model_id) or model_id in by_id:
            continue
        known = fallback.get(model_id)
        name = known["name"] if known else _display_name(entry, model_id)
        by_id[model_id] = {
            "id": model_id,
            "name": name,
            "desc": known["desc"] if known else _description(entry, name),
            "default_effort": (
                known.get("default_effort", _default_effort(model_id))
                if known
                else _default_effort(model_id)
            ),
            "thinking_mode": "level",
            "levels": ["minimal", "low", "medium", "high"],
        }

    def sort_key(model: Dict[str, Any]) -> Tuple[int, str]:
        return (
            MODEL_PRIORITY.get(model["id"], 100),
            model["id"],
        )

    return sorted(by_id.values(), key=sort_key)
