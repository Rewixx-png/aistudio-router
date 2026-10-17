"""High-level direct AI Studio chat client."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .auth import AistudioAuth
from .botguard import BrowserTokenProvider
from .config import FALLBACK_MODELS
from .payloads import build_generate_payload, extract_response, format_rpc_error
from .profile import Profile


@dataclass
class GenerationResult:
    ok: bool
    answer: str = ""
    thoughts: str = ""
    error: str = ""
    status: int = 0


class AistudioChat:
    """Generate through the AI Studio web chat RPC, not a personal API key."""

    def __init__(self, profile: Profile, chrome_path: Optional[str] = None):
        self.profile = profile
        self.auth = AistudioAuth(
            cookies=profile.cookies,
            web_api_key=profile.web_api_key,
            profile_path=profile.path,
        )
        self.tokens = BrowserTokenProvider(
            cookies=profile.cookies,
            chrome_path=chrome_path,
        )
        self._generation_lock = threading.Lock()
        self._discovered_models: Optional[List[Dict[str, Any]]] = None

    # ------------------------------------------------------------------ models
    def list_models(self) -> List[Dict[str, Any]]:
        if not self.profile.cookies or not self.auth.bootstrap():
            models = self._fallback_models()
        else:
            discovered = self.auth.list_models()
            models = discovered or self._fallback_models()
        self._discovered_models = models
        return models

    def _resolve_model_id(self, requested_model: str) -> str:
        model_id = (
            requested_model
            if requested_model.startswith("models/")
            else f"models/{requested_model}"
        )
        if "argon" not in model_id.lower():
            return model_id
        models = self._discovered_models
        if models is None:
            models = self.list_models()
        for item in models:
            candidate = str(item.get("id") or "")
            if "argon" in candidate.lower():
                return candidate
        return "models/gemini-4-argon"

    @staticmethod
    def _fallback_models() -> List[Dict[str, Any]]:
        result = []
        for item in FALLBACK_MODELS:
            result.append(
                {
                    **item,
                    "thinking_mode": "level",
                    "levels": ["minimal", "low", "medium", "high"],
                }
            )
        return result

    # -------------------------------------------------------------- generation
    def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> GenerationResult:
        prompt = (prompt or "").strip()
        if not prompt:
            return GenerationResult(False, error="Пустой промпт")

        with self._generation_lock:
            if not self.profile.cookies:
                return GenerationResult(
                    False,
                    error="Нет кук AI Studio. Обнови авторизацию в профиле.",
                )
            if not self.auth.web_api_key and not self.auth.bootstrap():
                return GenerationResult(
                    False,
                    error=(
                        "Не удалось получить web_api_key AI Studio. "
                        "Проверь куки и доступ к aistudio.google.com."
                    ),
                )

            model_id = self._resolve_model_id(model or self.profile.model)
            effort_id = effort or self.profile.effort
            last_error = ""
            for attempt in range(2):
                try:
                    base_payload = self.tokens.mint_payload(
                        prompt, model=model_id
                    )
                except Exception as exc:
                    return GenerationResult(
                        False, error=f"BotGuard: {exc}"
                    )

                token = base_payload[4]
                browser_cookies = self.tokens.get_browser_cookies()
                if browser_cookies:
                    self.auth.cookies.update(browser_cookies)
                    self.profile.cookies.update(browser_cookies)
                    self.tokens.cookies.update(browser_cookies)
                self.auth.set_browser_headers(
                    self.tokens.get_request_headers()
                )
                self.auth._save_rotated_cookies()

                payload = build_generate_payload(
                    prompt=prompt,
                    model=model_id,
                    effort=effort_id,
                    token=token,
                    base_payload=base_payload,
                )
                status, data = self.auth.generate(payload)
                if status == 200:
                    thoughts, answer = extract_response(data)
                    if answer or thoughts:
                        return GenerationResult(
                            True,
                            answer=answer,
                            thoughts=thoughts,
                            status=status,
                        )
                    return GenerationResult(
                        False,
                        error="AI Studio вернул пустой ответ",
                        status=status,
                    )

                last_error = format_rpc_error(status, data)
                if status == 403 and "argon" in model_id.lower():
                    last_error += (
                        "\nПодсказка: если Gemini 4 Argon недоступен в аккаунте, "
                        "выбери models/gemini-3.8-flash."
                    )
                if status == 403 and attempt == 0:
                    # The first page load can produce a stale BotGuard state.
                    self.tokens.reload()
                    continue
                return GenerationResult(False, error=last_error, status=status)

            return GenerationResult(False, error=last_error)

    # --------------------------------------------------------------- lifecycle
    def close(self) -> None:
        self.tokens.close()

    def __enter__(self) -> "AistudioChat":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
