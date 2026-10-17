"""Terminal UI language and glyph fallbacks.

Termux and SSH clients often use fonts that lack Cyrillic, bullets, and emoji
even when the terminal uses UTF-8. In those environments the CLI switches to
plain English and ASCII boxes. Local desktop terminals keep the Russian UI.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_termux() -> bool:
    return bool(
        os.environ.get("TERMUX_VERSION")
        or "com.termux" in os.environ.get("PREFIX", "")
        or os.path.exists("/data/data/com.termux")
    )


def _utf8_stream() -> bool:
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    return "utf" in encoding


_ORIGINAL_UTF8 = _utf8_stream()
_SSH_SESSION = bool(
    os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT")
)


def ensure_utf8() -> None:
    """Reconfigure stdio before rich/prompt_toolkit capture the streams."""
    if _ORIGINAL_UTF8 and not is_termux():
        return
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")


ensure_utf8()

from rich import box  # noqa: E402

_UI_FILE = Path.home() / ".aistudio_ui"
_choice = os.environ.get("AI_STUDIO_UI", "").strip().lower()

if _choice and _choice != "auto":
    # Remember an explicit choice, so a one-off AI_STUDIO_UI=ascii run
    # persists for SSH/limited-font sessions.
    try:
        if _choice in ("ascii", "en", "english"):
            _UI_FILE.write_text("ascii", encoding="utf-8")
        elif _choice in ("unicode", "ru", "russian"):
            _UI_FILE.write_text("unicode", encoding="utf-8")
        _UI_FILE.chmod(0o600)
    except OSError:
        pass
elif not _choice:
    try:
        _saved = _UI_FILE.read_text(encoding="utf-8").strip().lower()
        if _saved in ("ascii", "unicode"):
            _choice = _saved
    except OSError:
        pass

if _choice in ("ascii", "en", "english"):
    ASCII_UI = True
elif _choice in ("unicode", "ru", "russian"):
    ASCII_UI = False
else:
    ASCII_UI = is_termux() or _SSH_SESSION or not _ORIGINAL_UTF8

BOX_DOUBLE = box.ASCII if ASCII_UI else box.DOUBLE_EDGE
BOX_ROUNDED = box.ASCII if ASCII_UI else box.ROUNDED
BOX_SIMPLE = box.ASCII if ASCII_UI else box.SIMPLE_HEAVY
SEP = " - " if ASCII_UI else " • "
DOT = " | " if ASCII_UI else " · "
SPINNER = "line" if ASCII_UI else "dots"
POINTER = ">" if ASCII_UI else "»"


def tr(ru: str, en: str) -> str:
    return en if ASCII_UI else ru


EN_LEVELS = {
    "none": "NONE    | Thinking disabled",
    "minimal": "MINIMAL | Minimum latency",
    "low": "LOW     | Fast analysis",
    "medium": "MEDIUM  | Balanced",
    "high": "HIGH    | Deep reasoning",
    "max": "MAX     | Maximum level",
}


def level_label(level: str, russian: str) -> str:
    return EN_LEVELS.get(level, level.upper()) if ASCII_UI else russian
