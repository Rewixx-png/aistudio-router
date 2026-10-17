"""Mint AI Studio BotGuard tokens through a real Chromium page.

The AI Studio web client signs every GenerateContent request with a BotGuard
snapshot whose binding is SHA-256(prompt). The token is created in the page
before the request is sent, so we intercept that request, take the token, and
abort the browser-side request. The actual generation is then sent by the RPC
client with the model/thinking settings selected in this router.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import ORIGIN, USER_AGENT
from .cookies import playwright_cookies

INPUT_SELECTOR = "textarea, [contenteditable='true']"

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => false });
window.chrome = { runtime: {} };
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
"""


def find_chromium(explicit: Optional[str] = None) -> Optional[str]:
    """Locate a Chromium executable without downloading one."""
    candidates: List[str] = []
    if explicit:
        candidates.append(explicit)
    env_path = os.environ.get("AI_STUDIO_CHROME")
    if env_path:
        candidates.append(env_path)
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    prefix = os.environ.get("PREFIX")
    if prefix:
        for name in ("chromium", "chromium-browser", "google-chrome"):
            path = Path(prefix) / "bin" / name
            if path.is_file() and os.access(path, os.X_OK):
                candidates.append(str(path))

    home = Path.home()
    patterns = [
        ".cache/ms-playwright/chromium-*/chrome-linux/chrome",
        ".cache/ms-playwright/chromium-*/chrome-linux64/chrome",
    ]
    for pattern in patterns:
        for path in sorted(home.glob(pattern)):
            candidates.append(str(path))

    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


class BrowserTokenProvider:
    def __init__(
        self,
        cookies: Dict[str, str],
        chrome_path: Optional[str] = None,
        headless: bool = True,
        warmup_seconds: float = 7.0,
        mint_timeout: float = 25.0,
        retries: int = 4,
    ):
        self.cookies = dict(cookies)
        self.chrome_path = chrome_path
        self.headless = headless
        self.warmup_seconds = warmup_seconds
        self.mint_timeout = mint_timeout
        self.retries = retries

        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._captured: Optional[str] = None
        self._captured_headers: Dict[str, str] = {}
        self._current_model_slug: Optional[str] = None
        self._started = False
        self._lock = threading.RLock()

    # --------------------------------------------------------------- lifecycle
    def start(self, model_slug: Optional[str] = None) -> None:
        if self._started:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Не установлен playwright. Выполни: pip install playwright"
            ) from exc

        executable = find_chromium(self.chrome_path)
        launch_kwargs = {
            "headless": self.headless,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        }
        if executable:
            launch_kwargs["executable_path"] = executable

        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(**launch_kwargs)
        except Exception as exc:
            self.close()
            text = str(exc).strip()
            reason = ""
            for line in text.splitlines():
                if "[err]" in line:
                    reason = line.strip()
                    break
            if not reason:
                reason = (
                    text.splitlines()[0]
                    if text
                    else exc.__class__.__name__
                )
            browser = executable or "Playwright bundled Chromium"
            raise RuntimeError(
                "Не удалось запустить Chromium для AI Studio.\n"
                f"Браузер: {browser}\n"
                f"Причина: {reason[:500]}\n"
                "Termux: pkg install chromium и "
                "AI_STUDIO_CHROME=$(command -v chromium). "
                "Linux: apt install chromium. "
                "Либо: python -m playwright install chromium."
            ) from exc

        self._context = self._browser.new_context(user_agent=USER_AGENT)
        self._context.add_init_script(STEALTH_JS)
        self._context.add_cookies(playwright_cookies(self.cookies))
        self._page = self._context.new_page()
        self._page.route("**/*", self._handle_route)
        try:
            self._load_chat(model_slug)
        except Exception:
            self.close()
            raise
        self._started = True

    def _diagnose_chat_page(self) -> Dict[str, str]:
        try:
            url = self._page.url
        except Exception:
            url = "unknown"
        try:
            title = self._page.title()
        except Exception:
            title = "unknown"
        try:
            body = " ".join(
                self._page.inner_text("body").split()
            )[:600]
        except Exception:
            body = "no body text"

        lower = f"{url}\n{title}\n{body}".lower()
        hint = ""
        if (
            "accounts.google.com" in lower
            or "/welcome" in lower
            or "sign in" in lower
            or "войти" in lower
        ):
            hint = (
                "Похоже, куки не авторизуют AI Studio. "
                "Добавь свежие куки."
            )
        elif "before you continue" in lower or "consent" in lower:
            hint = (
                "Google показывает consent/privacy экран. "
                "Открой AI Studio в обычном браузере и подтверди его."
            )
        elif "update your browser" in lower or "unsupported" in lower:
            hint = "Chromium слишком старый или не поддерживается."
        return {
            "url": url,
            "title": title,
            "body": body,
            "hint": hint,
        }

    def _load_chat(self, model_slug: Optional[str] = None) -> None:
        if self._page is None:
            return
        self._current_model_slug = model_slug
        bare_url = f"{ORIGIN}/prompts/new_chat"
        urls = []
        if model_slug:
            urls.append(f"{bare_url}?model={model_slug}")
        urls.append(bare_url)

        last_error: Optional[Exception] = None
        for index, url in enumerate(urls):
            try:
                self._page.goto(
                    url, wait_until="domcontentloaded", timeout=60000
                )
                self._page.wait_for_selector(
                    INPUT_SELECTOR, state="visible", timeout=60000
                )
                # A bare-page fallback still mints a valid prompt-bound token.
                # Keep the requested model for the RPC payload.
                self._current_model_slug = model_slug
                time.sleep(self.warmup_seconds)
                return
            except Exception as exc:
                last_error = exc
                if index + 1 < len(urls):
                    continue

        diag = self._diagnose_chat_page()
        hint = f"\n{hint}" if (hint := diag.get("hint")) else ""
        raise RuntimeError(
            "AI Studio не показал поле ввода.\n"
            f"URL: {diag['url']}\n"
            f"Заголовок: {diag['title']}\n"
            f"Куки: {len(self.cookies)}\n"
            f"Страница: {diag['body']}"
            f"{hint}"
        ) from last_error

    def reload(self) -> None:
        """Reload the page after a rejected token, keeping the active model."""
        with self._lock:
            if not self._started or self._page is None:
                return
            try:
                self._load_chat(self._current_model_slug)
            except Exception:
                # A later mint attempt will surface the real error.
                pass

    def get_request_headers(self) -> Dict[str, str]:
        """Headers from the GenerateContent request that produced the token."""
        return dict(self._captured_headers)

    def get_browser_cookies(self) -> Dict[str, str]:
        """Return the live Chromium cookie jar after Google rotates cookies."""
        if self._context is None:
            return {}
        try:
            return {
                str(cookie["name"]): str(cookie["value"])
                for cookie in self._context.cookies()
                if "name" in cookie and "value" in cookie
            }
        except Exception:
            return {}

    def close(self) -> None:
        for resource in (self._context, self._browser):
            try:
                if resource is not None:
                    resource.close()
            except Exception:
                pass
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        self._context = None
        self._browser = None
        self._playwright = None
        self._page = None
        self._started = False

    def __enter__(self) -> "BrowserTokenProvider":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------ routes
    def _handle_route(self, route) -> None:
        request = route.request
        try:
            if (
                request.method == "POST"
                and "MakerSuiteService/GenerateContent" in request.url
            ):
                self._captured = request.post_data
                self._captured_headers = dict(request.headers)
                route.abort()
                return
            route.continue_()
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass

    # ------------------------------------------------------------------- mint
    def _fill_input(self, value: str) -> None:
        try:
            self._page.fill("textarea", value)
        except Exception:
            self._page.fill('[contenteditable="true"]', value)

    def _click_run_fallback(self) -> None:
        for selector in (
            'button.ctrl-enter-submits',
            'button[aria-label*="Run"], button[type="submit"]',
            'button:has-text("Run")',
        ):
            try:
                locator = self._page.locator(selector)
                if locator.count() > 0:
                    locator.first.click(timeout=1500)
                    return
            except Exception:
                continue

    def mint_payload(
        self, prompt: str, model: Optional[str] = None
    ) -> List[Any]:
        """Return the full browser GenerateContent payload for a prompt."""
        if not prompt.strip():
            raise ValueError("Пустой промпт")
        model_slug = model.replace("models/", "") if model else None

        with self._lock:
            self.start(model_slug)
            if model_slug and model_slug != self._current_model_slug:
                self._load_chat(model_slug)

            last_error: Optional[str] = None
            for attempt in range(self.retries):
                self._captured = None
                try:
                    self._fill_input("")
                    self._fill_input(prompt)
                    time.sleep(0.8)
                    self._page.keyboard.down("Control")
                    self._page.keyboard.press("Enter")
                    self._page.keyboard.up("Control")
                except Exception as exc:
                    last_error = str(exc)
                    time.sleep(1.0)
                    if attempt >= 1:
                        self.reload()
                    continue

                # Ctrl+Enter is the normal path; some UI states require a click.
                submit_deadline = time.time() + 3.0
                while time.time() < submit_deadline and not self._captured:
                    time.sleep(0.15)
                if not self._captured:
                    self._click_run_fallback()

                deadline = time.time() + self.mint_timeout
                while time.time() < deadline and not self._captured:
                    time.sleep(0.25)

                if self._captured:
                    try:
                        payload = json.loads(self._captured)
                        if (
                            isinstance(payload, list)
                            and len(payload) >= 5
                            and isinstance(payload[4], str)
                            and len(payload[4]) > 100
                        ):
                            return payload
                        last_error = (
                            "AI Studio вернул неполный GenerateContent payload"
                        )
                    except (ValueError, TypeError) as exc:
                        last_error = f"Некорректный запрос AI Studio: {exc}"
                else:
                    last_error = "AI Studio не отправил GenerateContent"

                time.sleep(1.5)
                if attempt >= 1:
                    self.reload()

            raise RuntimeError(
                "Не удалось получить BotGuard-токен. "
                "Проверь авторизацию AI Studio и обнови куки. "
                f"Последняя ошибка: {last_error}"
            )

    def mint(self, prompt: str) -> str:
        """Backward-compatible token-only helper."""
        return self.mint_payload(prompt)[4]
