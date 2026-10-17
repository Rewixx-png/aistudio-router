"""Parsing and normalisation of Google AI Studio cookie exports."""

from __future__ import annotations

import json
import re
from typing import Dict, List, Tuple


def detect_and_parse_cookies(raw_data: str) -> Tuple[Dict[str, str], str]:
    """Parse Cookie-Editor JSON, cURL, Netscape, or a Cookie header string."""
    raw_data = (raw_data or "").strip()
    cookies: Dict[str, str] = {}
    if not raw_data:
        return {}, "empty"

    # 1. JSON (Cookie-Editor / EditThisCookie / profile dump).
    if raw_data.startswith("[") or raw_data.startswith("{"):
        try:
            parsed = json.loads(raw_data)
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict) and "name" in item and "value" in item:
                        cookies[str(item["name"])] = str(item["value"])
                if cookies:
                    return cookies, "JSON (Cookie-Editor)"
            elif isinstance(parsed, dict):
                if isinstance(parsed.get("cookies"), dict):
                    return (
                        {str(k): str(v) for k, v in parsed["cookies"].items()},
                        "Router Profile JSON",
                    )
                for key, value in parsed.items():
                    if isinstance(key, str):
                        cookies[key] = str(value)
                if cookies:
                    return cookies, "JSON Dict"
        except json.JSONDecodeError:
            pass

    # 2. cURL command.
    if "curl " in raw_data.lower():
        matches = re.findall(
            r"(?:-H\s+['\"]Cookie:\s*|-b\s+['\"])([^'\"]+)['\"]",
            raw_data,
            re.IGNORECASE,
        )
        if matches:
            for pair in "; ".join(matches).split(";"):
                if "=" in pair:
                    key, value = pair.strip().split("=", 1)
                    cookies[key.strip()] = value.strip()
            if cookies:
                return cookies, "cURL Command"

    # 3. Netscape cookie file.
    lines = raw_data.splitlines()
    if "\t" in raw_data or any(line.startswith("#HttpOnly_") for line in lines):
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_") :]
            elif line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                cookies[parts[5].strip()] = parts[6].strip()
        if cookies:
            return cookies, "Netscape Format"

    # 4. Plain header string.
    if raw_data.lower().startswith("cookie:"):
        raw_data = raw_data[7:].strip()
    for pair in raw_data.split(";"):
        if "=" in pair:
            key, value = pair.strip().split("=", 1)
            if key.strip():
                cookies[key.strip()] = value.strip()
    if cookies:
        return cookies, "Header String"

    return {}, "unknown"


def playwright_cookies(cookies: Dict[str, str]) -> List[dict]:
    """Convert a name/value map into Playwright's cookie objects."""
    result: List[dict] = []
    for name, value in cookies.items():
        result.append(
            {
                "name": name,
                "value": value,
                "domain": ".google.com",
                "path": "/",
                "secure": True,
                "httpOnly": False,
            }
        )
    return result


def cookie_header(cookies: Dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items())
