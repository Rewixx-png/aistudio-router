"""Command-line interface and interactive dashboard."""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

import pyperclip
import questionary
import requests
from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .chat import AistudioChat
from .config import (
    CUSTOM_STYLE,
    EFFORT_TO_LEVEL,
    LEVEL_DESCRIPTIONS,
    console,
)
from .cookies import detect_and_parse_cookies
from .gateway import run_gateway
from .profile import Profile
from .storage import get_storage


def render_banner() -> None:
    console.clear()
    banner = Text()
    banner.append("AI STUDIO ", style="bold #ff5f87")
    banner.append("DIRECT CHAT ROUTER\n", style="bold #00d7af")
    banner.append(
        "Cookies • BotGuard • Gemini web chat • OpenAI /v1 gateway",
        style="italic #8a8a8a",
    )
    console.print(
        Panel(
            banner,
            border_style="#ff5f87",
            box=box.DOUBLE_EDGE,
            padding=(1, 2),
            expand=False,
        )
    )


def _ask(question):
    """Ask a questionary prompt without colliding with Playwright's loop.

    Playwright's sync API leaves an asyncio loop running in the thread that
    started it. A later questionary prompt would then fail with
    "asyncio.run() cannot be called from a running event loop". Run the
    prompt in a worker thread when that happens.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return question.ask()

    result: List[object] = []

    def target() -> None:
        try:
            result.append(question.ask())
        except BaseException as exc:  # re-raised in the caller thread
            result.append(exc)

    thread = threading.Thread(
        target=target, name="aistudio-question", daemon=True
    )
    thread.start()
    try:
        thread.join()
    except KeyboardInterrupt:
        return None
    if not result:
        return None
    value = result[0]
    if isinstance(value, BaseException):
        raise value
    return value


_bot_thread: Optional[threading.Thread] = None


def _bot_dependency() -> tuple[bool, str]:
    try:
        import aiogram

        version = str(getattr(aiogram, "__version__", "0"))
        try:
            major = int(version.split(".")[0])
        except ValueError:
            major = 0
        if major < 3:
            return False, f"aiogram {version} (нужен 3.x)"
        return True, f"aiogram {version}"
    except Exception as exc:
        return False, str(exc)


def _start_bot_thread() -> bool:
    global _bot_thread
    if _bot_thread is not None and _bot_thread.is_alive():
        return True
    ok, info = _bot_dependency()
    if not ok:
        console.print(
            f"[bold red]Telegram: {info}[/]\n"
            "[dim]Установи: pip install 'aiogram>=3.13'[/]"
        )
        return False

    from . import bot as bot_module

    def target() -> None:
        try:
            asyncio.run(bot_module.run())
        except Exception as exc:
            console.print(
                f"[bold red]Telegram-бот остановлен: {exc}[/]"
            )

    _bot_thread = threading.Thread(
        target=target, name="aistudio-telegram", daemon=True
    )
    _bot_thread.start()
    time.sleep(1.0)
    return bool(_bot_thread.is_alive())


def _connect_telegram() -> bool:
    ok, info = _bot_dependency()
    if not ok:
        console.print(
            f"[bold red]Telegram: {info}[/]\n"
            "[dim]Установи: pip install 'aiogram>=3.13'[/]"
        )
        return False

    owner_raw = _ask(
        questionary.text(
            "Telegram ID владельца:",
            validate=lambda value: (
                value.strip().isdigit() or "ID должен быть числом"
            ),
            style=CUSTOM_STYLE,
        )
    )
    if not owner_raw:
        return False
    token = _ask(
        questionary.password(
            "Токен бота от @BotFather:",
            style=CUSTOM_STYLE,
        )
    )
    if not token:
        return False
    token = token.strip()

    try:
        response = requests.get(
            f"https://api.telegram.org/bot{token}/getMe", timeout=10
        )
        payload = response.json()
    except Exception:
        console.print(
            "[bold red]Не удалось проверить токен. "
            "Проверь сеть и токен.[/]"
        )
        return False
    if not payload.get("ok"):
        console.print(
            f"[bold red]{payload.get('description') or 'Telegram отклонил токен.'}[/]"
        )
        return False

    result = payload.get("result") or {}
    username = str(result.get("username") or "bot")
    storage = get_storage()
    storage.set_setting("telegram_bot_token", token)
    storage.set_setting("telegram_owner_id", owner_raw.strip())
    storage.set_setting("telegram_bot_username", username)
    console.print(
        f"[bold #00d7af]Бот @{username} подключён.[/]"
    )
    if _start_bot_thread():
        console.print(
            "[dim]Бот работает, пока открыт софт. "
            "Для постоянного запуска: python -m AiStudio.bot[/]"
        )
    return True


def telegram_menu() -> None:
    storage = get_storage()
    token = storage.get_setting("telegram_bot_token")
    owner = storage.get_setting("telegram_owner_id")
    username = storage.get_setting("telegram_bot_username") or "bot"
    running = _bot_thread is not None and _bot_thread.is_alive()

    if not token or not owner:
        _connect_telegram()
        return

    console.print(
        Panel(
            f"Бот: [bold white]@{username}[/]\n"
            f"Владелец: [bold white]{owner}[/]\n"
            f"Статус: "
            + (
                "[bold #00d7af]работает[/]"
                if running
                else "[yellow]остановлен[/]"
            ),
            title="Telegram",
            border_style="#00d7af",
            expand=False,
        )
    )

    choices = []
    if not running:
        choices.append(
            questionary.Choice("Запустить бота", value="start")
        )
        choices.append(
            questionary.Choice("Переподключить", value="reconnect")
        )
    choices.append(questionary.Choice("Назад", value="back"))
    action = _ask(
        questionary.select(
            "Telegram-бот:", choices=choices, style=CUSTOM_STYLE
        )
    )
    if action == "start":
        _start_bot_thread()
    elif action == "reconnect":
        _connect_telegram()


def prompt_for_cookies(profile: Profile) -> bool:
    """Ask for new cookies and store them in the profile."""
    while True:
        clip_text = ""
        try:
            clip_text = pyperclip.paste() or ""
        except Exception:
            pass

        clip_cookies, clip_format = detect_and_parse_cookies(clip_text)
        clip_has_sapisid = any(
            key in clip_cookies for key in ("SAPISID", "__Secure-3PAPISID")
        )

        choices = []
        if clip_has_sapisid:
            choices.append(
                questionary.Choice(
                    title=(
                        f"Вставить куки из буфера ({clip_format}: "
                        f"{len(clip_cookies)} шт.)"
                    ),
                    value="clipboard",
                )
            )
        else:
            choices.append(
                questionary.Choice(
                    title="Прочитать куки из буфера обмена",
                    value="clipboard",
                )
            )

        local_files = [
            path
            for path in Path(".").iterdir()
            if path.is_file()
            and path.suffix.lower() in (".txt", ".json", ".cookies")
            and path.resolve() != profile.path.resolve()
        ]
        if local_files:
            choices.append(
                questionary.Choice(
                    title=f"Выбрать файл с куками ({len(local_files)} шт.)",
                    value="local_file",
                )
            )
        choices.extend(
            [
                questionary.Choice(
                    title="Ввести куки вручную (JSON / cURL / Netscape / Header)",
                    value="manual",
                ),
                questionary.Choice(title="Выход", value="exit"),
            ]
        )

        method = _ask(
            questionary.select(
                "Авторизация в Google AI Studio:",
                choices=choices,
                style=CUSTOM_STYLE,
            )
        )
        if method in (None, "exit"):
            return False

        raw_input = ""
        if method == "clipboard":
            raw_input = clip_text
        elif method == "local_file":
            filename = _ask(
                questionary.select(
                    "Выбери файл:",
                    choices=[path.name for path in local_files] + ["Назад"],
                    style=CUSTOM_STYLE,
                )
            )
            if not filename or filename == "Назад":
                continue
            raw_input = Path(filename).read_text(encoding="utf-8", errors="ignore")
        elif method == "manual":
            console.print(
                Panel(
                    "Вставь куки ([bold cyan]JSON[/], [bold cyan]cURL[/], "
                    "[bold cyan]Netscape[/] или [bold cyan]Cookie:[/]).\n"
                    "Пустая строка завершает ввод.",
                    border_style="cyan",
                )
            )
            lines: List[str] = []
            while True:
                try:
                    line = input()
                except EOFError:
                    break
                if not line.strip() and lines:
                    break
                if line.strip():
                    lines.append(line)
                    if (
                        "SAPISID=" in line
                        and "\t" not in line
                        and not line.strip().startswith("[")
                    ):
                        break
            raw_input = "\n".join(lines)

        parsed, fmt = detect_and_parse_cookies(raw_input)
        if not parsed:
            console.print("[bold red]Куки не распознаны. Попробуй ещё раз.[/]")
            continue

        profile.cookies = parsed
        # A new cookie set can belong to another account; re-read the web key.
        profile.web_api_key = None
        profile.save()
        console.print(
            f"[bold #00d7af]Загружено {len(parsed)} кук[/] "
            f"[dim]({fmt})[/]"
        )
        return True


def print_models(models: List[dict]) -> None:
    table = Table(
        title="Модели Google AI Studio",
        box=box.SIMPLE_HEAVY,
        border_style="#ff5f87",
    )
    table.add_column("#", style="dim", width=3)
    table.add_column("Модель", style="bold #00d7af")
    table.add_column("Описание", style="dim white", overflow="fold")
    for index, model in enumerate(models, 1):
        table.add_row(
            str(index),
            f"{model.get('name', model['id'])}\n[dim]{model['id']}[/]",
            model.get("desc", ""),
        )
    console.print(table)


def select_model(chat: AistudioChat, profile: Profile) -> bool:
    models = chat.list_models()
    if not models:
        console.print("[bold red]Не удалось получить список моделей.[/]")
        return False

    print_models(models)
    choices = [
        questionary.Choice(
            title=f"{model.get('name', model['id']):<28} | {model['id']}",
            value=model,
        )
        for model in models
    ]
    choices.append(
        questionary.Choice(title="Ввести ID модели вручную", value="custom")
    )
    selected = _ask(
        questionary.select(
            "Выбери модель:", choices=choices, style=CUSTOM_STYLE
        )
    )
    if not selected:
        return False
    if selected == "custom":
        model_id = _ask(
            questionary.text(
                "ID модели (например, models/gemini-4-argon):",
                default=profile.model,
                style=CUSTOM_STYLE,
            )
        )
        if not model_id or not model_id.strip():
            return False
        profile.set_model(model_id.strip())
    else:
        profile.set_model(selected["id"], selected)
    profile.save()
    return True


def select_effort(profile: Profile) -> bool:
    levels = profile.model_meta.get("levels") or [
        "minimal",
        "low",
        "medium",
        "high",
    ]
    choices = [
        questionary.Choice(
            title=LEVEL_DESCRIPTIONS.get(level, level.upper()),
            value=level,
        )
        for level in levels
        if level in EFFORT_TO_LEVEL
    ]
    if not choices:
        choices = [
            questionary.Choice(
                title=LEVEL_DESCRIPTIONS[level], value=level
            )
            for level in ("minimal", "low", "medium", "high")
        ]
    selected = _ask(
        questionary.select(
            f"Thinking effort для {profile.model}:",
            choices=choices,
            default=profile.effort
            if profile.effort in levels
            else levels[0],
            style=CUSTOM_STYLE,
        )
    )
    if not selected:
        return False
    profile.set_effort(selected)
    profile.save()
    return True


def print_result(chat: AistudioChat, prompt: str) -> None:
    try:
        with console.status(
            f"[bold #00d7af]AI Studio генерирует ответ "
            f"({chat.profile.model}, {chat.profile.effort.upper()})...[/]"
        ):
            result = chat.generate(prompt)
    finally:
        # Playwright's sync API leaves an asyncio loop running in this thread.
        # Close it before the next questionary prompt, otherwise questionary
        # fails with "asyncio.run() cannot be called from a running event loop".
        chat.close()
    if not result.ok:
        console.print(
            Panel(result.error, title="Ошибка", border_style="red")
        )
        return
    if result.thoughts:
        console.print(
            Panel(
                result.thoughts,
                title="Thinking",
                border_style="magenta",
            )
        )
    console.print(
        Panel(
            result.answer or "(пустой ответ)",
            title=f"Ответ {chat.profile.model}",
            border_style="green",
        )
    )


def dashboard(
    chat: AistudioChat,
    profile: Profile,
    chrome_path: Optional[str],
) -> AistudioChat:
    while True:
        fresh = Profile.load(profile.path)
        if (
            chat.profile.account_id != fresh.account_id
            or chat.profile.updated_at != fresh.updated_at
        ):
            chat.close()
            chat = AistudioChat(fresh, chrome_path)
            profile = fresh
        console.print()
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold #ff5f87", justify="right")
        grid.add_column(style="bold white")
        grid.add_row("Модель:", f"[#00d7af]{profile.model}[/]")
        grid.add_row("Thinking:", f"[yellow]{profile.effort.upper()}[/]")
        grid.add_row(
            "Куки:",
            f"{len(profile.cookies)} шт. "
            "(ротация сессионных кук включена)",
        )
        grid.add_row("Канал:", "AI Studio Web Chat (BotGuard)")
        console.print(
            Panel(
                grid,
                title="AI Studio Router",
                border_style="#00d7af",
                expand=False,
            )
        )

        action = _ask(
            questionary.select(
                "Действие:",
                choices=[
                    questionary.Choice(
                        "Отправить тестовый промпт", value="test"
                    ),
                    questionary.Choice(
                        "Запустить OpenAI /v1 сервер (порт 20128)",
                        value="serve",
                    ),
                    questionary.Choice("Сменить модель", value="model"),
                    questionary.Choice(
                        "Изменить Thinking effort", value="effort"
                    ),
                    questionary.Choice("Обновить куки", value="cookies"),
                    questionary.Choice("Telegram-бот", value="telegram"),
                    questionary.Choice("Выход", value="exit"),
                ],
                style=CUSTOM_STYLE,
            )
        )

        if action in (None, "exit"):
            return chat
        if action == "test":
            prompt = _ask(
                questionary.text(
                    "Сообщение:",
                    default="Привет! Напиши кратко, какая ты модель.",
                    style=CUSTOM_STYLE,
                )
            )
            if prompt:
                print_result(chat, prompt)
        elif action == "serve":
            chat.close()
            run_gateway(
                profile.path,
                host="0.0.0.0",
                port=20128,
                chrome_path=chrome_path,
            )
            return chat
        elif action == "model":
            if select_model(chat, profile):
                select_effort(profile)
        elif action == "effort":
            select_effort(profile)
        elif action == "cookies":
            if prompt_for_cookies(profile):
                chat.close()
                chat = AistudioChat(profile, chrome_path)
                if not chat.list_models():
                    console.print(
                        "[yellow]Список моделей недоступен, "
                        "использую сохранённый профиль.[/]"
                    )
        elif action == "telegram":
            telegram_menu()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="AiStudio",
        description="Direct Google AI Studio chat router",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=None,
        help="Legacy router_profile.json to import once",
    )
    parser.add_argument(
        "--prompt",
        type=str,
        default=None,
        help="Generate one prompt and exit (non-interactive)",
    )
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument(
        "--effort",
        choices=sorted(EFFORT_TO_LEVEL.keys()),
        default=None,
    )
    parser.add_argument(
        "--chrome",
        type=str,
        default=None,
        help="Path to a Chromium/Chrome executable",
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="Print the live model catalog and exit",
    )
    parser.add_argument(
        "--gateway",
        action="store_true",
        help="Run the OpenAI-compatible server with a session API key",
    )
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="Run the configured Telegram bot (polling)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Gateway bind address (default: 0.0.0.0)",
    )
    parser.add_argument("--port", type=int, default=20128)
    parser.add_argument(
        "--public-url",
        default=None,
        help=(
            "Explicit public base URL, e.g. https://ai.example.com. "
            "Env: AI_STUDIO_PUBLIC_URL"
        ),
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    profile = Profile.load(args.profile)
    chrome_path = args.chrome

    try:
        if args.telegram:
            ok, info = _bot_dependency()
            if not ok:
                console.print(
                    f"[bold red]Telegram: {info}[/]\n"
                    "[dim]Установи: pip install 'aiogram>=3.13'[/]"
                )
                return 1
            from .bot import run as bot_run

            try:
                asyncio.run(bot_run())
            except KeyboardInterrupt:
                console.print("\n[bold red]Telegram-бот остановлен.[/]")
                return 130
            except Exception as exc:
                console.print(f"[bold red]Telegram: {exc}[/]")
                return 1
            return 0

        if args.prompt:
            chat = AistudioChat(profile, chrome_path)
            try:
                result = chat.generate(
                    args.prompt,
                    model=args.model or profile.model,
                    effort=args.effort or profile.effort,
                )
            finally:
                chat.close()
            if not result.ok:
                console.print(f"[bold red]{result.error}[/]")
                return 1
            if result.thoughts:
                console.print(
                    Panel(
                        result.thoughts,
                        title="Thinking",
                        border_style="magenta",
                    )
                )
            console.print(
                Panel(
                    result.answer,
                    title="Ответ AI Studio",
                    border_style="green",
                )
            )
            return 0

        if args.list_models:
            chat = AistudioChat(profile, chrome_path)
            try:
                models = chat.list_models()
            finally:
                chat.close()
            print_models(models)
            return 0

        if args.gateway:
            run_gateway(
                profile.path,
                host=args.host,
                port=args.port,
                chrome_path=chrome_path,
                public_url=args.public_url,
            )
            return 0

        render_banner()
        if not profile.cookies:
            if not prompt_for_cookies(profile):
                return 1

        chat = AistudioChat(profile, chrome_path)
        try:
            chat = dashboard(chat, profile, chrome_path)
        finally:
            chat.close()
        return 0
    except KeyboardInterrupt:
        console.print("\n[bold red]Выход.[/]")
        return 130
    except Exception as exc:
        console.print(f"[bold red]Ошибка: {exc}[/]")
        return 1


def run(argv: Optional[List[str]] = None) -> int:
    """Run the CLI, including from a process that already has an event loop.

    questionary/prompt_toolkit and uvicorn both use asyncio.run() internally.
    When the launcher is embedded in a running loop, delegate the blocking CLI
    to a worker thread instead of failing with "asyncio.run() cannot be called
    from a running event loop".
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return main(argv)

    result: List[object] = []

    def target() -> None:
        try:
            result.append(main(argv))
        except BaseException as exc:  # re-raised in the caller thread
            result.append(exc)

    thread = threading.Thread(
        target=target, name="aistudio-cli", daemon=True
    )
    thread.start()
    try:
        thread.join()
    except KeyboardInterrupt:
        return 130

    if not result:
        return 1
    value = result[0]
    if isinstance(value, SystemExit):
        return int(value.code or 0)
    if isinstance(value, BaseException):
        raise value
    return int(value)


if __name__ == "__main__":
    sys.exit(run())
