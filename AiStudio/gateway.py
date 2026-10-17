"""Local OpenAI-compatible gateway backed by AI Studio web chat."""

import asyncio
import json
import os
import re
import secrets
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.markup import escape
from rich.panel import Panel

from .chat import AistudioChat, GenerationResult
from .config import EFFORT_TO_LEVEL, console
from .profile import Profile
from .storage import get_storage


def _prompt_from_messages(messages: List[Dict[str, Any]]) -> str:
    if not messages:
        return ""
    if len(messages) == 1 and messages[0].get("role") in ("user", "system"):
        return str(messages[0].get("content") or "")

    parts: List[str] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = message.get("content")
        if isinstance(content, list):
            text = "\n".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict)
            )
        else:
            text = str(content or "")
        if text:
            parts.append(f"{role}: {text}")
    return "\n".join(parts)


def resolve_public_url(
    host: str,
    port: int,
    explicit: Optional[str] = None,
) -> str:
    """Return the public /v1 base URL for the gateway."""

    def normalize(value: str) -> Optional[str]:
        value = value.strip().rstrip("/")
        if not value:
            return None
        if not re.match(r"^https?://", value, re.IGNORECASE):
            value = f"http://{value}"
        if not value.endswith("/v1"):
            value = f"{value}/v1"
        return value

    if explicit:
        normalized = normalize(explicit)
        if normalized:
            return normalized

    try:
        import requests

        public_ip = requests.get(
            "https://api.ipify.org", timeout=3
        ).text.strip()
        if public_ip:
            if ":" in public_ip and not public_ip.startswith("["):
                public_ip = f"[{public_ip}]"
            return f"http://{public_ip}:{port}/v1"
    except Exception:
        pass

    if host and host not in ("0.0.0.0", "::", ""):
        if host in ("127.0.0.1", "localhost", "::1"):
            return f"http://127.0.0.1:{port}/v1"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{port}/v1"

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            local_ip = sock.getsockname()[0]
        finally:
            sock.close()
        if local_ip and not local_ip.startswith("127."):
            return f"http://{local_ip}:{port}/v1"
    except Exception:
        pass

    return f"http://127.0.0.1:{port}/v1"


def create_app(
    profile_path: Path,
    chrome_path: Optional[str] = None,
    api_key: Optional[str] = None,
):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse

    app = FastAPI(title="AI Studio Direct Gateway")
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aistudio")
    state: Dict[str, Optional[AistudioChat]] = {"chat": None}

    def provided_api_key(request: Request) -> str:
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            return authorization[7:].strip()
        for header in ("x-api-key", "x-goog-api-key"):
            value = request.headers.get(header)
            if value:
                return value.strip()
        return (request.query_params.get("key") or "").strip()

    def unauthorized() -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "message": "Invalid or missing API key",
                    "type": "invalid_request_error",
                    "code": 401,
                }
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    def auth_error(request: Request):
        supplied = provided_api_key(request)
        if api_key and supplied and secrets.compare_digest(supplied, api_key):
            return None
        if supplied:
            try:
                if get_storage().validate_api_key(supplied):
                    return None
            except Exception:
                pass
        if not api_key and not get_storage().has_api_keys():
            return None
        return unauthorized()

    def get_chat() -> AistudioChat:
        profile = Profile.load(profile_path)
        chat = state.get("chat")
        if chat is not None:
            current = chat.profile
            if (
                current.account_id != profile.account_id
                or current.updated_at != profile.updated_at
            ):
                chat.close()
                chat = None
                state["chat"] = None
        if chat is None:
            chat = AistudioChat(profile, chrome_path)
            state["chat"] = chat
        return chat

    def generate_sync(
        prompt: str, model: Optional[str], effort: Optional[str]
    ) -> GenerationResult:
        return get_chat().generate(prompt, model, effort)

    def models_sync() -> List[Dict[str, Any]]:
        return get_chat().list_models()

    def close_sync() -> None:
        chat = state.get("chat")
        if chat is not None:
            chat.close()
            state["chat"] = None

    @app.get("/v1/models")
    async def list_models(request: Request):
        denied = auth_error(request)
        if denied:
            return denied
        loop = asyncio.get_running_loop()
        models = await loop.run_in_executor(executor, models_sync)
        return {
            "object": "list",
            "data": [
                {
                    "id": model["id"].replace("models/", ""),
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "google-aistudio",
                }
                for model in models
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        denied = auth_error(request)
        if denied:
            return denied
        body = await request.json()
        prompt = _prompt_from_messages(body.get("messages") or [])
        if not prompt.strip():
            return JSONResponse(
                status_code=400,
                content={"error": {"message": "messages contain no text"}},
            )

        profile = Profile.load(profile_path)
        model = body.get("model") or profile.model
        if not str(model).startswith("models/"):
            model = f"models/{model}"
        effort = body.get("reasoning_effort")
        if effort not in EFFORT_TO_LEVEL:
            effort = profile.effort

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            executor, generate_sync, prompt, model, effort
        )
        if not result.ok:
            return JSONResponse(
                status_code=502,
                content={
                    "error": {
                        "message": result.error,
                        "type": "upstream_error",
                        "code": result.status,
                    }
                },
            )

        completion_id = f"chatcmpl-{int(time.time() * 1000)}"
        created = int(time.time())
        if not body.get("stream"):
            message: Dict[str, Any] = {
                "role": "assistant",
                "content": result.answer,
            }
            if result.thoughts:
                message["reasoning_content"] = result.thoughts
            return {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": model.replace("models/", ""),
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "stop",
                    }
                ],
            }

        def event_stream():
            delta: Dict[str, Any] = {"content": result.answer}
            if result.thoughts:
                delta["reasoning_content"] = result.thoughts
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model.replace("models/", ""),
                "choices": [
                    {"index": 0, "delta": delta, "finish_reason": None}
                ],
            }
            done = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model.replace("models/", ""),
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}
                ],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.on_event("shutdown")
    async def shutdown():
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(executor, close_sync)
        finally:
            executor.shutdown(wait=False)

    return app


def run_gateway(
    profile_path: Path,
    host: str = "0.0.0.0",
    port: int = 20128,
    chrome_path: Optional[str] = None,
    public_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> None:
    import uvicorn

    session_key = (
        api_key or os.environ.get("AI_STUDIO_GATEWAY_KEY") or ""
    ).strip()
    if not session_key:
        session_key = f"sk-aistudio-{secrets.token_urlsafe(32)}"
    public_base = resolve_public_url(
        host, port, public_url or os.environ.get("AI_STUDIO_PUBLIC_URL")
    )
    app = create_app(profile_path, chrome_path, api_key=session_key)
    profile = Profile.load(profile_path)
    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning"
    )
    server = uvicorn.Server(config)

    lines = [
        "[bold #00d7af]AI Studio Direct Gateway запущен[/]",
        "",
        f"• Public Base URL: [bold white]{escape(public_base)}[/]",
        f"• Local Base URL:  [dim]http://127.0.0.1:{port}/v1[/dim]",
        f"• Session API Key: [bold yellow]{escape(session_key)}[/]",
        f"• Модель: [bold white]{escape(profile.model)}[/]",
        "",
        "[dim]Ключ действует до перезапуска сервера. Передавай его в "
        "`Authorization: Bearer ...`, `x-api-key` или `?key=`.[/]",
    ]
    if host in ("127.0.0.1", "localhost", "::1"):
        lines.append(
            "[yellow]⚠️ Сервер слушает только localhost. "
            "Для внешнего доступа: --host 0.0.0.0[/]"
        )
    if public_base.startswith("http://"):
        lines.append(
            "[yellow]⚠️ Публичный URL по HTTP. Для интернета лучше "
            "HTTPS/reverse proxy.[/]"
        )
    console.print(Panel("\n".join(lines), border_style="#00d7af"))

    # Run uvicorn in its own thread. Calling asyncio.run()/uvicorn.run() from
    # a thread that already has an event loop (the dashboard can be embedded
    # in one) raises "asyncio.run() cannot be called from a running event loop".
    errors: List[BaseException] = []

    def serve() -> None:
        try:
            server.run()
        except BaseException as exc:  # surfaced in the caller's thread
            errors.append(exc)

    thread = threading.Thread(
        target=serve, name="aistudio-gateway", daemon=True
    )
    thread.start()
    try:
        while thread.is_alive():
            thread.join(timeout=0.5)
    except KeyboardInterrupt:
        server.should_exit = True
        thread.join(timeout=10)
    if errors:
        raise errors[0]
