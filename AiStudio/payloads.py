"""AI Studio GenerateContent wire format and response extraction."""

from __future__ import annotations

import copy
import json
from typing import Any, List, Optional, Tuple

from .config import EFFORT_TO_LEVEL


def _synthetic_config(level: int, max_output_tokens: int) -> List[Any]:
    # Field 4 of the RPC message is a typed generation config. Index 16 holds
    # the candidate settings; index 3 of that list is the thinking level.
    return [
        None,
        None,
        None,
        max_output_tokens,
        1,
        0.95,
        64,
        None,
        None,
        None,
        None,
        None,
        None,
        1,
        None,
        None,
        [1, None, None, level],
    ]


def build_generate_payload(
    prompt: str,
    model: str,
    effort: str,
    token: str,
    max_output_tokens: int = 65536,
    base_payload: Optional[List[Any]] = None,
) -> List[Any]:
    """Build the web RPC body, preferring the browser's native payload.

    Frontier models such as Gemini 4 Argon validate the generation config
    against the model context initialized by the AI Studio page. When a full
    browser payload is supplied, preserve its config and history and only
    replace the model and the thinking level. The synthetic config remains as
    a fallback for callers that only have a token.
    """
    model_id = model if model.startswith("models/") else f"models/{model}"
    level = EFFORT_TO_LEVEL.get(effort, 2)
    history = [[[[None, prompt]], "user"]]

    if (
        isinstance(base_payload, list)
        and len(base_payload) >= 5
    ):
        payload = copy.deepcopy(base_payload)
        payload[0] = model_id
        if not payload[1]:
            payload[1] = history
        config = payload[3]
        if (
            isinstance(config, list)
            and len(config) > 16
            and isinstance(config[16], list)
            and len(config[16]) > 3
        ):
            config[16][3] = level
        elif not isinstance(config, list):
            payload[3] = _synthetic_config(level, max_output_tokens)
        payload[4] = token
        return payload

    return [
        model_id,
        history,
        None,
        _synthetic_config(level, max_output_tokens),
        token,
    ]


def extract_response(data: Any) -> Tuple[str, str]:
    """Return (thoughts, answer) from the nested protobuf-JSON response."""
    thoughts: List[str] = []
    answers: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            if (
                len(node) >= 2
                and node[0] is None
                and isinstance(node[1], str)
                and node[1].strip()
            ):
                text = node[1]
                # The web RPC marks a thought part with field 13 == 1
                # (index 12 in the JSON array); some responses also expose a
                # boolean thought flag. Answer chunks are plain [null, text].
                is_thought = (
                    any(value is True for value in node[2:])
                    or (
                        len(node) > 12
                        and isinstance(node[12], int)
                        and node[12] != 0
                    )
                )
                (thoughts if is_thought else answers).append(text)
            for item in node:
                walk(item)

    walk(data)
    # Preserve order but remove exact duplicates caused by nested wrappers.
    thoughts = list(dict.fromkeys(thoughts))
    answers = list(dict.fromkeys(answers))
    return "\n".join(thoughts), "\n".join(answers)


def format_rpc_error(status: int, data: Any) -> str:
    if status == 0:
        return f"Сетевая ошибка: {data}"
    detail = data
    if isinstance(data, str):
        detail = data.strip()
    else:
        detail = json.dumps(data, ensure_ascii=False)
    if status == 403:
        return (
            "HTTP 403: AI Studio отклонил запрос. Обычно это означает, что "
            "куки истекли или BotGuard-токен не был получен. Обнови куки и повтори.\n"
            f"Ответ: {detail[:600]}"
        )
    if status == 429:
        return f"HTTP 429: превышена квота AI Studio. Попробуй позже.\n{detail[:600]}"
    return f"HTTP {status}: {detail[:800]}"
