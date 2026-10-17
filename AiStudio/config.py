"""Application constants and shared UI configuration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

from questionary import Style
from rich.console import Console

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent

ORIGIN = "https://aistudio.google.com"
RPC_BASE = (
    "https://alkalimakersuite-pa.clients6.google.com/$rpc/"
    "google.internal.alkali.applications.makersuite.v1.MakerSuiteService"
)

# The web front-end key is extracted from the AI Studio page when a profile
# does not contain one. It is not a personal API key.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)

DEFAULT_MODEL = "models/gemini-3.8-flash"
DEFAULT_EFFORT = "medium"

# AI Studio's web RPC encodes the thinking level as a small integer in the
# generation config. The exact meaning was verified against the live endpoint.
EFFORT_TO_LEVEL: Dict[str, int] = {
    "none": 0,
    "minimal": 1,
    "low": 1,
    "medium": 2,
    "high": 3,
    "max": 3,
}

LEVEL_DESCRIPTIONS = {
    "none": "NONE    | Без Thinking (0 токенов)",
    "minimal": "MINIMAL | Минимальная задержка",
    "low": "LOW     | Быстрый анализ",
    "medium": "MEDIUM  | Сбалансированный режим",
    "high": "HIGH    | Глубокий ризонинг",
    "max": "MAX     | Максимальный уровень",
}

FALLBACK_MODELS: List[Dict[str, Any]] = [
    {
        "id": "models/gemini-4-argon",
        "name": "Gemini 4 Argon",
        "desc": "Флагман Gemini 4 (1M контекст, агентный кодинг)",
        "default_effort": "high",
    },
    {
        "id": "models/gemini-3.8-flash",
        "name": "Gemini 3.8 Flash",
        "desc": "Скоростная модель для агентных задач",
        "default_effort": "medium",
    },
    {
        "id": "models/gemini-3.7-flash",
        "name": "Gemini 3.7 Flash",
        "desc": "Высокая точность кодинга",
        "default_effort": "medium",
    },
    {
        "id": "models/gemini-3.5-flash-lite",
        "name": "Gemini 3.5 Flash-Lite",
        "desc": "Ультра-быстрая модель для сабтасков",
        "default_effort": "low",
    },
    {
        "id": "models/gemini-3.1-pro-preview",
        "name": "Gemini 3.1 Pro",
        "desc": "Глубокий архитектурный ризонинг",
        "default_effort": "high",
    },
    {
        "id": "models/gemini-2.5-pro",
        "name": "Gemini 2.5 Pro",
        "desc": "Классическая Pro-модель",
        "default_effort": "high",
    },
    {
        "id": "models/gemini-2.5-flash",
        "name": "Gemini 2.5 Flash",
        "desc": "Классическая Flash-модель",
        "default_effort": "medium",
    },
    {
        "id": "models/gemini-2.5-flash-lite",
        "name": "Gemini 2.5 Flash-Lite",
        "desc": "Лёгкая Flash-модель",
        "default_effort": "low",
    },
]

# Web RPC is a chat endpoint; these model families are not text chat models.
NON_CHAT_MARKERS = (
    "embedding",
    "embed-content",
    "aqa",
    "imagen",
    "image",
    "banana",
    "veo",
    "lyria",
    "tts",
    "transcribe",
    "live",
    "robotics",
    "omni",
    "learnlm",
    "gemma",
    "thorin",
    "talker",
    "thinker",
)

MODEL_PRIORITY = {
    "models/gemini-4-argon": 0,
    "models/gemini-3.8-flash": 1,
    "models/gemini-3.7-flash": 2,
    "models/gemini-3.6-flash": 3,
    "models/gemini-3.5-flash": 4,
    "models/gemini-3.5-flash-lite": 5,
    "models/gemini-3.1-pro-preview": 6,
    "models/gemini-2.5-pro": 7,
    "models/gemini-2.5-flash": 8,
    "models/gemini-2.5-flash-lite": 9,
}

console = Console()

CUSTOM_STYLE = Style(
    [
        ("qmark", "fg:#ff5f87 bold"),
        ("question", "fg:#ffffff bold"),
        ("answer", "fg:#00d7af bold"),
        ("pointer", "fg:#ff5f87 bold"),
        ("highlighted", "fg:#ff5f87 bold"),
        ("selected", "fg:#00d7af"),
        ("separator", "fg:#6c6c6c"),
        ("instruction", "fg:#8a8a8a italic"),
    ]
)


def default_profile_path() -> Path:
    """Prefer AI_STUDIO_PROFILE, then ./router_profile.json, then the launcher dir."""
    env_path = os.environ.get("AI_STUDIO_PROFILE")
    if env_path:
        return Path(env_path).expanduser()
    cwd_path = Path.cwd() / "router_profile.json"
    if cwd_path.exists():
        return cwd_path
    return PROJECT_ROOT / "router_profile.json"
