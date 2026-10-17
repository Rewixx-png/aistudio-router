"""Command-line interface and interactive dashboard."""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

from .i18n import (
    ASCII_UI,
    BOX_DOUBLE,
    BOX_ROUNDED,
    BOX_SIMPLE,
    DOT,
    SEP,
    SPINNER,
    level_label,
    tr,
)

import pyperclip
import questionary
import requests
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
        tr(
            f"Cookies{SEP}BotGuard{SEP}Gemini web chat{SEP}OpenAI /v1 gateway",
            "Cookies | BotGuard | Gemini web chat | OpenAI /v1 gateway",
        ),
        style="italic #8a8a8a",
    )
    console.print(
        Panel(
            banner,
            border_style="#ff5f87",
            box=BOX_DOUBLE,
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
            return False, f"aiogram {version} ({tr('нужен 3.x', 'requires 3.x')})"
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
            + tr(
                "[dim]Установи: pip install 'aiogram>=3.13'[/]",
                "[dim]Install: pip install 'aiogram>=3.13'[/]",
            )
        )
        return False

    from . import bot as bot_module

    def target() -> None:
        try:
            asyncio.run(bot_module.run())
        except Exception as exc:
            console.print(
                f"[bold red]{tr('Telegram-бот остановлен', 'Telegram bot stopped')}: {exc}[/]"
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
            + tr(
                "[dim]Установи: pip install 'aiogram>=3.13'[/]",
                "[dim]Install: pip install 'aiogram>=3.13'[/]",
            )
        )
        return False

    owner_raw = _ask(
        questionary.text(
            tr("Telegram ID владельца:", "Telegram owner ID:"),
            validate=lambda value: (
                value.strip().isdigit()
                or tr("ID должен быть числом", "ID must be numeric")
            ),
            style=CUSTOM_STYLE,
        )
    )
    if not owner_raw:
        return False
    token = _ask(
        questionary.password(
            tr(
                "Токен бота от @BotFather:",
                "Bot token from @BotFather:",
            ),
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
            tr(
                "[bold red]Не удалось проверить токен. "
                "Проверь сеть и токен.[/]",
                "[bold red]Could not validate the token. "
                "Check the network and token.[/]",
            )
        )
        return False
    if not payload.get("ok"):
        console.print(
            f"[bold red]{payload.get('description') or tr('Telegram отклонил токен.', 'Telegram rejected the token.')}[/]"
        )
        return False

    result = payload.get("result") or {}
    username = str(result.get("username") or "bot")
    storage = get_storage()
    storage.set_setting("telegram_bot_token", token)
    storage.set_setting("telegram_owner_id", owner_raw.strip())
    storage.set_setting("telegram_bot_username", username)
    console.print(
        f"[bold #00d7af]{tr(f'Бот @{username} подключён.', f'Bot @{username} connected.')}[/]"
    )
    if _start_bot_thread():
        console.print(
            tr(
                "[dim]Бот работает, пока открыт софт. "
                "Для постоянного запуска: python -m AiStudio.bot[/]",
                "[dim]The bot runs while this app is open. "
                "For standalone polling: python -m AiStudio.bot[/]",
            )
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
            f"{tr('Бот:', 'Bot:')} [bold white]@{username}[/]\n"
            f"{tr('Владелец:', 'Owner:')} [bold white]{owner}[/]\n"
            f"{tr('Статус:', 'Status:')} "
            + (
                tr("[bold #00d7af]работает[/]", "[bold #00d7af]running[/]")
                if running
                else tr("[yellow]остановлен[/]", "[yellow]stopped[/]")
            ),
            title="Telegram",
            border_style="#00d7af",
            box=BOX_ROUNDED,
            expand=False,
        )
    )

    choices = []
    if not running:
        choices.append(
            questionary.Choice(tr("Запустить бота", "Start the bot"), value="start")
        )
        choices.append(
            questionary.Choice(tr("Переподключить", "Reconnect"), value="reconnect")
        )
    choices.append(questionary.Choice(tr("Назад", "Back"), value="back"))
    action = _ask(
        questionary.select(
            tr("Telegram-бот:", "Telegram bot:"),
            choices=choices,
            style=CUSTOM_STYLE,
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
                    title=tr(
                        f"Вставить куки из буфера ({clip_format}: "
                        f"{len(clip_cookies)} шт.)",
                        f"Paste cookies from clipboard ({clip_format}: "
                        f"{len(clip_cookies)})",
                    ),
                    value="clipboard",
                )
            )
        else:
            choices.append(
                questionary.Choice(
                    title=tr(
                        "Прочитать куки из буфера обмена",
                        "Read cookies from clipboard",
                    ),
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
                    title=tr(
                        f"Выбрать файл с куками ({len(local_files)} шт.)",
                        f"Choose a cookie file ({len(local_files)})",
                    ),
                    value="local_file",
                )
            )
        choices.extend(
            [
                questionary.Choice(
                    title=tr(
                        "Ввести куки вручную "
                        "(JSON / cURL / Netscape / Header)",
                        "Enter cookies manually "
                        "(JSON / cURL / Netscape / Header)",
                    ),
                    value="manual",
                ),
                questionary.Choice(
                    title=tr("Выход", "Exit"), value="exit"
                ),
            ]
        )

        method = _ask(
            questionary.select(
                tr(
                    "Авторизация в Google AI Studio:",
                    "Google AI Studio authentication:",
                ),
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
            back_label = tr("Назад", "Back")
            filename = _ask(
                questionary.select(
                    tr("Выбери файл:", "Choose a file:"),
                    choices=[path.name for path in local_files]
                    + [back_label],
                    style=CUSTOM_STYLE,
                )
            )
            if not filename or filename == back_label:
                continue
            raw_input = Path(filename).read_text(encoding="utf-8", errors="ignore")
        elif method == "manual":
            console.print(
                Panel(
                    tr(
                        "Вставь куки ([bold cyan]JSON[/], [bold cyan]cURL[/], "
                        "[bold cyan]Netscape[/] или [bold cyan]Cookie:[/]).\n"
                        "Пустая строка завершает ввод.",
                        "Paste cookies ([bold cyan]JSON[/], [bold cyan]cURL[/], "
                        "[bold cyan]Netscape[/], or [bold cyan]Cookie:[/]).\n"
                        "An empty line finishes input.",
                    ),
                    border_style="cyan",
                    box=BOX_ROUNDED,
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
            console.print(
                tr(
                    "[bold red]Куки не распознаны. Попробуй ещё раз.[/]",
                    "[bold red]Could not parse the cookies. Try again.[/]",
                )
            )
            continue

        profile.cookies = parsed
        # A new cookie set can belong to another account; re-read the web key.
        profile.web_api_key = None
        profile.save()
        console.print(
            f"[bold #00d7af]{tr(f'Загружено {len(parsed)} кук', f'Loaded {len(parsed)} cookies')}[/] "
            f"[dim]({fmt})[/]"
        )
        return True


def print_models(models: List[dict]) -> None:
    table = Table(
        title=tr("Модели Google AI Studio", "Google AI Studio models"),
        box=BOX_SIMPLE,
        border_style="#ff5f87",
    )
    table.add_column("#", style="dim", width=3)
    table.add_column(tr("Модель", "Model"), style="bold #00d7af")
    table.add_column(
        tr("Описание", "Description"), style="dim white", overflow="fold"
    )
    for index, model in enumerate(models, 1):
        desc = str(model.get("desc", ""))
        if ASCII_UI and any(ord(char) > 127 for char in desc):
            desc = "AI Studio model"
        table.add_row(
            str(index),
            f"{model.get('name', model['id'])}\n[dim]{model['id']}[/]",
            desc,
        )
    console.print(table)


def select_model(chat: AistudioChat, profile: Profile) -> bool:
    models = chat.list_models()
    if not models:
        console.print(
            tr(
                "[bold red]Не удалось получить список моделей.[/]",
                "[bold red]Could not load the model list.[/]",
            )
        )
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
        questionary.Choice(
            title=tr("Ввести ID модели вручную", "Enter a model ID manually"),
            value="custom",
        )
    )
    selected = _ask(
        questionary.select(
            tr("Выбери модель:", "Choose a model:"),
            choices=choices,
            style=CUSTOM_STYLE,
        )
    )
    if not selected:
        return False
    if selected == "custom":
        model_id = _ask(
            questionary.text(
                tr(
                    "ID модели (например, models/gemini-4-argon):",
                    "Model ID (for example, models/gemini-4-argon):",
                ),
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
            title=level_label(
                level, LEVEL_DESCRIPTIONS.get(level, level.upper())
            ),
            value=level,
        )
        for level in levels
        if level in EFFORT_TO_LEVEL
    ]
    if not choices:
        choices = [
            questionary.Choice(
                title=level_label(level, LEVEL_DESCRIPTIONS[level]),
                value=level,
            )
            for level in ("minimal", "low", "medium", "high")
        ]
    selected = _ask(
        questionary.select(
            tr(
                f"Thinking effort для {profile.model}:",
                f"Thinking effort for {profile.model}:",
            ),
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
            tr(
                f"[bold #00d7af]AI Studio генерирует ответ "
                f"({chat.profile.model}, {chat.profile.effort.upper()})...[/]",
                f"[bold #00d7af]AI Studio is generating "
                f"({chat.profile.model}, {chat.profile.effort.upper()})...[/]",
            ),
            spinner=SPINNER,
        ):
            result = chat.generate(prompt)
    finally:
        # Playwright's sync API leaves an asyncio loop running in this thread.
        # Close it before the next questionary prompt, otherwise questionary
        # fails with "asyncio.run() cannot be called from a running event loop".
        chat.close()
    if not result.ok:
        console.print(
            Panel(
                result.error,
                title=tr("Ошибка", "Error"),
                border_style="red",
                box=BOX_ROUNDED,
            )
        )
        return
    if result.thoughts:
        console.print(
            Panel(
                result.thoughts,
                title="Thinking",
                border_style="magenta",
                box=BOX_ROUNDED,
            )
        )
    console.print(
        Panel(
            result.answer or tr("(пустой ответ)", "(empty answer)"),
            title=tr(
                f"Ответ {chat.profile.model}",
                f"Answer {chat.profile.model}",
            ),
            border_style="green",
            box=BOX_ROUNDED,
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
        grid.add_row(
            tr("Модель:", "Model:"),
            f"[#00d7af]{profile.model}[/]",
        )
        grid.add_row(
            "Thinking:", f"[yellow]{profile.effort.upper()}[/]"
        )
        grid.add_row(
            tr("Куки:", "Cookies:"),
            tr(
                f"{len(profile.cookies)} шт. "
                "(ротация сессионных кук включена)",
                f"{len(profile.cookies)} cookies "
                "(session rotation enabled)",
            ),
        )
        grid.add_row(
            tr("Канал:", "Channel:"),
            "AI Studio Web Chat (BotGuard)",
        )
        console.print(
            Panel(
                grid,
                title="AI Studio Router",
                border_style="#00d7af",
                box=BOX_ROUNDED,
                expand=False,
            )
        )

        action = _ask(
            questionary.select(
                tr("Действие:", "Action:"),
                choices=[
                    questionary.Choice(
                        tr("Отправить тестовый промпт", "Send a test prompt"),
                        value="test",
                    ),
                    questionary.Choice(
                        tr(
                            "Запустить OpenAI /v1 сервер (порт 20128)",
                            "Start OpenAI /v1 server (port 20128)",
                        ),
                        value="serve",
                    ),
                    questionary.Choice(
                        tr("Сменить модель", "Change model"), value="model"
                    ),
                    questionary.Choice(
                        tr(
                            "Изменить Thinking effort",
                            "Change thinking effort",
                        ),
                        value="effort",
                    ),
                    questionary.Choice(
                        tr("Обновить куки", "Update cookies"),
                        value="cookies",
                    ),
                    questionary.Choice(
                        tr("Telegram-бот", "Telegram bot"),
                        value="telegram",
                    ),
                    questionary.Choice(
                        tr("Выход", "Exit"), value="exit"
                    ),
                ],
                style=CUSTOM_STYLE,
            )
        )

        if action in (None, "exit"):
            return chat
        if action == "test":
            prompt = _ask(
                questionary.text(
                    tr("Сообщение:", "Message:"),
                    default=tr(
                        "Привет! Напиши кратко, какая ты модель.",
                        "Hello! Briefly describe which model you are.",
                    ),
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
                        tr(
                            "[yellow]Список моделей недоступен, "
                            "использую сохранённый профиль.[/]",
                            "[yellow]Model list unavailable; "
                            "using the saved profile.[/]",
                        )
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
                    + tr(
                        "[dim]Установи: pip install 'aiogram>=3.13'[/]",
                        "[dim]Install: pip install 'aiogram>=3.13'[/]",
                    )
                )
                return 1
            from .bot import run as bot_run

            try:
                asyncio.run(bot_run())
            except KeyboardInterrupt:
                console.print(
                    tr(
                        "\n[bold red]Telegram-бот остановлен.[/]",
                        "\n[bold red]Telegram bot stopped.[/]",
                    )
                )
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
                        box=BOX_ROUNDED,
                    )
                )
            console.print(
                Panel(
                    result.answer,
                    title=tr("Ответ AI Studio", "AI Studio answer"),
                    border_style="green",
                    box=BOX_ROUNDED,
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
        console.print(tr("\n[bold red]Выход.[/]", "\n[bold red]Exit.[/]"))
        return 130
    except Exception as exc:
        console.print(
            f"[bold red]{tr('Ошибка', 'Error')}: {exc}[/]"
        )
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
