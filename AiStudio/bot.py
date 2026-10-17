"""Telegram bot for AiStudio.

The bot is intentionally plain: short messages, inline controls, no decorative
emoji spam, and no artificial "AI assistant" filler. It shares the SQLite
storage with the CLI and gateway.
"""

from __future__ import annotations

import asyncio
import html
import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .chat import AistudioChat, GenerationResult
from .config import EFFORT_TO_LEVEL, FALLBACK_MODELS, LEVEL_DESCRIPTIONS
from .cookies import detect_and_parse_cookies
from .profile import Profile
from .storage import get_storage

logger = logging.getLogger(__name__)
MAX_CHUNK = 3800
_thoughts: "OrderedDict[int, str]" = OrderedDict()


def _remember_thought(message_id: int, text: str) -> None:
    _thoughts[message_id] = text
    _thoughts.move_to_end(message_id)
    while len(_thoughts) > 100:
        _thoughts.popitem(last=False)


def _esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=False)


def _chunks(text: str, limit: int = MAX_CHUNK) -> List[str]:
    text = text or ""
    chunks: List[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current or not chunks:
        chunks.append(current)
    return chunks


def _effort_label(effort: str) -> str:
    return LEVEL_DESCRIPTIONS.get(effort, effort.upper())


def _active_account() -> Optional[Dict[str, Any]]:
    return get_storage().get_active_account()


def _owner_id() -> Optional[int]:
    value = get_storage().get_setting("telegram_owner_id")
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


class BotStates(StatesGroup):
    prompt = State()
    cookie_name = State()
    cookie_text = State()
    cookie_replace = State()
    api_key_name = State()


class OwnerOnly(BaseMiddleware):
    def __init__(self, owner_id: int):
        self.owner_id = owner_id

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user and user.id == self.owner_id:
            return await handler(event, data)
        if isinstance(event, Message):
            await event.answer("Доступ только для владельца.")
        elif isinstance(event, CallbackQuery):
            await event.answer("Доступ только для владельца.", show_alert=True)
        return None


class BotService:
    def __init__(self):
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="aistudio-bot"
        )

    def _generate_sync(self, prompt: str) -> GenerationResult:
        profile = Profile.load()
        if not profile.cookies:
            return GenerationResult(
                False, error="Нет кук. Добавь профиль в разделе «Куки»."
            )
        chat = AistudioChat(profile)
        try:
            result = chat.generate(
                prompt, profile.model, profile.effort
            )
        finally:
            chat.close()
        get_storage().log_generation(
            "bot",
            profile.account_id,
            profile.model,
            profile.effort,
            len(prompt),
            result.ok,
            result.error,
        )
        return result

    async def generate(self, prompt: str) -> GenerationResult:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, self._generate_sync, prompt
        )

    def _models_sync(self) -> List[Dict[str, Any]]:
        profile = Profile.load()
        chat = AistudioChat(profile)
        try:
            return chat.list_models()
        finally:
            chat.close()

    async def models(self) -> List[Dict[str, Any]]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._models_sync)

    def close(self) -> None:
        self._executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------
def _menu_text(account: Optional[Dict[str, Any]]) -> str:
    if not account:
        return (
            "<b>AiStudio</b>\n\n"
            "Активного профиля нет. Добавь куки, чтобы отправлять запросы."
        )
    model = str(account.get("model") or "").replace("models/", "")
    return (
        "<b>AiStudio</b>\n"
        f"Аккаунт: <b>{_esc(account['name'])}</b>\n"
        f"Куки: <b>{account['cookie_count']}</b> · "
        f"Модель: <code>{_esc(model)}</code> · "
        f"Thinking: <b>{_esc(account['effort'].upper())}</b>"
    )


def _menu_kb(account: Optional[Dict[str, Any]]):
    builder = InlineKeyboardBuilder()
    if not account:
        builder.button(text="Добавить куки", callback_data="cookie_add")
        builder.button(text="Статус", callback_data="status")
        builder.adjust(1)
        return builder.as_markup()
    builder.button(text="Запрос", callback_data="ask")
    builder.button(text="Модель", callback_data="model")
    builder.button(text="Thinking", callback_data="effort")
    builder.button(text="Куки", callback_data="cookies")
    builder.button(text="API-ключи", callback_data="keys")
    builder.button(text="Статус", callback_data="status")
    builder.adjust(2, 2, 2)
    return builder.as_markup()


def _back_kb() -> Any:
    builder = InlineKeyboardBuilder()
    builder.button(text="Назад", callback_data="menu")
    return builder.as_markup()


def _cancel_kb() -> Any:
    builder = InlineKeyboardBuilder()
    builder.button(text="Отмена", callback_data="menu")
    return builder.as_markup()


async def _edit(callback: CallbackQuery, text: str, keyboard: Any) -> None:
    try:
        await callback.message.edit_text(
            text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except TelegramBadRequest:
        await callback.message.answer(
            text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    await callback.answer()


async def _send_plain(message: Message, text: str) -> Optional[Message]:
    last: Optional[Message] = None
    for chunk in _chunks(text):
        last = await message.answer(
            chunk or "…",
            parse_mode=None,
            disable_web_page_preview=True,
        )
    return last


def _cookies_text(account: Optional[Dict[str, Any]]) -> str:
    if not account:
        return "<b>Куки</b>\n\nАктивного профиля нет. Добавь куки."
    return (
        "<b>Куки</b>\n"
        f"Аккаунт: <b>{_esc(account['name'])}</b>\n"
        f"Количество: <b>{account['cookie_count']}</b>\n\n"
        "Добавить новый профиль, заменить куки активного или удалить профиль."
    )


def _cookies_kb(account: Optional[Dict[str, Any]]):
    builder = InlineKeyboardBuilder()
    builder.button(text="Добавить", callback_data="cookie_add")
    if account:
        builder.button(
            text="Заменить активные", callback_data="cookie_replace"
        )
        builder.button(text="Удалить", callback_data="cookie_delete")
    builder.button(text="Назад", callback_data="menu")
    builder.adjust(1)
    return builder.as_markup()


def _keys_text() -> str:
    keys = get_storage().list_api_keys()
    if not keys:
        return (
            "<b>API-ключи</b>\n\n"
            "Активных ключей нет. Ключи используются для OpenAI-совместимого "
            "gateway."
        )
    lines = ["<b>API-ключи</b>", ""]
    for key in keys:
        created = time.strftime("%d.%m.%Y", time.localtime(key["created_at"]))
        lines.append(
            f"<code>{_esc(key['prefix'])}…</code> · {_esc(key['name'])} · {created}"
        )
    return "\n".join(lines)


def _keys_kb():
    builder = InlineKeyboardBuilder()
    builder.button(text="Создать", callback_data="key_new")
    if get_storage().list_api_keys():
        builder.button(text="Отозвать", callback_data="key_revoke")
    builder.button(text="Назад", callback_data="menu")
    builder.adjust(1)
    return builder.as_markup()


async def _read_cookies(message: Message) -> Optional[str]:
    if message.document:
        if message.document.file_size and message.document.file_size > 2_000_000:
            return None
        try:
            buffer = await message.bot.download(message.document)
            if buffer is None:
                return None
            return buffer.read().decode("utf-8", errors="ignore")
        except Exception:
            return None
    return (message.text or "").strip()


def _valid_cookies(cookies: Dict[str, str]) -> bool:
    return bool(
        cookies
        and (
            cookies.get("SAPISID")
            or cookies.get("__Secure-3PAPISID")
            or cookies.get("__Secure-1PAPISID")
        )
    )


def _status_text(account: Optional[Dict[str, Any]]) -> str:
    owner = _owner_id()
    keys = len(get_storage().list_api_keys())
    lines = ["<b>Статус</b>", ""]
    if account:
        model = str(account.get("model") or "").replace("models/", "")
        lines.extend(
            [
                f"Аккаунт: <b>{_esc(account['name'])}</b>",
                f"Куки: <b>{account['cookie_count']}</b>",
                f"Модель: <code>{_esc(model)}</code>",
                f"Thinking: <b>{_esc(account['effort'].upper())}</b>",
            ]
        )
    else:
        lines.append("Активного профиля нет.")
    lines.extend(
        [
            f"API-ключей: <b>{keys}</b>",
            f"Владелец: <code>{owner or '—'}</code>",
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
def build_router(service: BotService) -> Router:
    router = Router()

    @router.message(CommandStart())
    @router.message(Command("menu"))
    async def cmd_start(message: Message, state: FSMContext):
        await state.clear()
        account = _active_account()
        await message.answer(
            _menu_text(account), reply_markup=_menu_kb(account)
        )

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext):
        await state.clear()
        account = _active_account()
        await message.answer(
            "Отменено.", reply_markup=_menu_kb(account)
        )

    @router.callback_query(F.data == "menu")
    async def cb_menu(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        account = _active_account()
        await _edit(callback, _menu_text(account), _menu_kb(account))

    @router.callback_query(F.data == "status")
    async def cb_status(callback: CallbackQuery):
        await _edit(
            callback, _status_text(_active_account()), _back_kb()
        )

    @router.callback_query(F.data == "ask")
    async def cb_ask(callback: CallbackQuery, state: FSMContext):
        account = _active_account()
        if not account or not account.get("cookies"):
            await callback.answer(
                "Сначала добавь куки.", show_alert=True
            )
            return
        await state.set_state(BotStates.prompt)
        await _edit(
            callback,
            "Отправь текст запроса одним сообщением.",
            _cancel_kb(),
        )

    @router.message(BotStates.prompt)
    async def on_prompt(message: Message, state: FSMContext):
        prompt = (message.text or "").strip()
        if not prompt:
            await message.answer(
                "Нужен текст запроса.", reply_markup=_cancel_kb()
            )
            return
        await state.clear()
        status = await message.answer("Генерирую…")
        result = await service.generate(prompt)
        try:
            await status.delete()
        except TelegramBadRequest:
            pass
        if not result.ok:
            await message.answer(
                "<b>Ошибка</b>\n" + _esc(result.error[:3000]),
                parse_mode=ParseMode.HTML,
            )
            return
        last = await _send_plain(
            message, result.answer or "(пустой ответ)"
        )
        if result.thoughts and last is not None:
            _remember_thought(last.message_id, result.thoughts)
            builder = InlineKeyboardBuilder()
            builder.button(
                text="Показать мысли",
                callback_data=f"thoughts:{last.message_id}",
            )
            try:
                await last.edit_reply_markup(
                    reply_markup=builder.as_markup()
                )
            except TelegramBadRequest:
                pass

    @router.callback_query(F.data.startswith("thoughts:"))
    async def cb_thoughts(callback: CallbackQuery):
        try:
            message_id = int((callback.data or "").split(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("Нет данных.", show_alert=True)
            return
        text = _thoughts.get(message_id)
        if not text:
            await callback.answer(
                "Мысли уже недоступны.", show_alert=True
            )
            return
        await _send_plain(callback.message, text)
        await callback.answer()

    # ------------------------------------------------------------------ cookies
    @router.callback_query(F.data == "cookies")
    async def cb_cookies(callback: CallbackQuery):
        account = _active_account()
        await _edit(
            callback, _cookies_text(account), _cookies_kb(account)
        )

    @router.callback_query(F.data == "cookie_add")
    async def cb_cookie_add(callback: CallbackQuery, state: FSMContext):
        await state.set_state(BotStates.cookie_name)
        await _edit(
            callback,
            "Название профиля. Например: main",
            _cancel_kb(),
        )
        await callback.answer()

    @router.message(BotStates.cookie_name)
    async def on_cookie_name(message: Message, state: FSMContext):
        name = (message.text or "").strip()[:64] or "default"
        await state.update_data(name=name)
        await state.set_state(BotStates.cookie_text)
        await message.answer(
            "Отправь куки текстом или файлом .json/.txt.\n"
            "Подойдёт JSON Cookie-Editor, cURL, Netscape или Cookie:.",
            reply_markup=_cancel_kb(),
        )

    @router.callback_query(F.data == "cookie_replace")
    async def cb_cookie_replace(
        callback: CallbackQuery, state: FSMContext
    ):
        if not _active_account():
            await callback.answer(
                "Нет активного профиля.", show_alert=True
            )
            return
        await state.set_state(BotStates.cookie_replace)
        await _edit(
            callback,
            "Отправь новые куки для активного профиля.",
            _cancel_kb(),
        )
        await callback.answer()

    @router.message(BotStates.cookie_text)
    @router.message(BotStates.cookie_replace)
    async def on_cookie_input(message: Message, state: FSMContext):
        raw = await _read_cookies(message)
        if not raw:
            await message.answer(
                "Нужны куки текстом или файлом .json/.txt.",
                reply_markup=_cancel_kb(),
            )
            return
        cookies, fmt = detect_and_parse_cookies(raw)
        if not _valid_cookies(cookies):
            await message.answer(
                "Не нашёл SAPISID. Проверь формат кук.",
                reply_markup=_cancel_kb(),
            )
            return
        current = await state.get_state()
        data = await state.get_data()
        if current == BotStates.cookie_text.state:
            name = str(data.get("name") or "default")
            account = get_storage().add_account(
                name, cookies, set_active=True
            )
            text = (
                f"Профиль <b>{_esc(account['name'])}</b> сохранён: "
                f"{account['cookie_count']} кук."
            )
        else:
            account = get_storage().update_active_cookies(cookies)
            text = (
                f"Куки активного профиля обновлены: "
                f"{account['cookie_count']} шт."
            )
        await state.clear()
        await message.answer(
            text,
            reply_markup=_menu_kb(account),
            parse_mode=ParseMode.HTML,
        )

    @router.callback_query(F.data == "cookie_delete")
    async def cb_cookie_delete(callback: CallbackQuery):
        accounts = get_storage().list_accounts()
        if not accounts:
            await callback.answer(
                "Профилей нет.", show_alert=True
            )
            return
        builder = InlineKeyboardBuilder()
        for account in accounts:
            mark = " · активный" if account["is_active"] else ""
            builder.button(
                text=(
                    f"{account['name'][:30]} "
                    f"({account['cookie_count']}){mark}"
                )[:60],
                callback_data=f"cookie_del:{account['id']}",
            )
        builder.button(text="Назад", callback_data="cookies")
        builder.adjust(1)
        await _edit(
            callback,
            "<b>Удалить профиль</b>\n\nВыбери профиль.",
            builder.as_markup(),
        )

    @router.callback_query(F.data.startswith("cookie_del:"))
    async def cb_cookie_delete_confirm(callback: CallbackQuery):
        try:
            account_id = int((callback.data or "").split(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("Ошибка.", show_alert=True)
            return
        account = get_storage().get_account(account_id)
        if not account:
            await callback.answer("Профиль не найден.", show_alert=True)
            return
        builder = InlineKeyboardBuilder()
        builder.button(
            text="Удалить",
            callback_data=f"cookie_del_yes:{account_id}",
        )
        builder.button(text="Отмена", callback_data="cookies")
        builder.adjust(1)
        await _edit(
            callback,
            f"Удалить профиль <b>{_esc(account['name'])}</b> "
            f"и его куки?",
            builder.as_markup(),
        )

    @router.callback_query(F.data.startswith("cookie_del_yes:"))
    async def cb_cookie_delete_yes(callback: CallbackQuery):
        try:
            account_id = int((callback.data or "").split(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("Ошибка.", show_alert=True)
            return
        deleted = get_storage().delete_account(account_id)
        account = _active_account()
        text = "Профиль удалён." if deleted else "Профиль не найден."
        await _edit(
            callback,
            text + "\n\n" + _menu_text(account),
            _menu_kb(account),
        )

    # ------------------------------------------------------------------ models
    @router.callback_query(F.data == "model")
    async def cb_model(callback: CallbackQuery):
        account = _active_account()
        if not account:
            await callback.answer(
                "Сначала добавь куки.", show_alert=True
            )
            return
        await _edit(
            callback, "Загружаю модели…", _back_kb()
        )
        models = await service.models()
        if not models:
            models = [
                {
                    "id": item["id"],
                    "name": item.get("name", item["id"]),
                }
                for item in FALLBACK_MODELS
            ]
        builder = InlineKeyboardBuilder()
        for model in models[:20]:
            mark = (
                " ·"
                if model["id"] == account.get("model")
                else ""
            )
            builder.button(
                text=f"{model.get('name', model['id'])}{mark}"[:60],
                callback_data=f"model_set:{model['id']}",
            )
        builder.button(text="Назад", callback_data="menu")
        builder.adjust(1)
        await _edit(
            callback,
            "<b>Модель</b>\n\nВыбери модель.",
            builder.as_markup(),
        )

    @router.callback_query(F.data.startswith("model_set:"))
    async def cb_model_set(callback: CallbackQuery):
        model_id = (callback.data or "").split(":", 1)[1]
        account = get_storage().update_active_model(model_id)
        if not account:
            await callback.answer(
                "Нет активного профиля.", show_alert=True
            )
            return
        await _edit(
            callback,
            f"Модель: <code>{_esc(model_id.replace('models/', ''))}</code>\n\n"
            + _menu_text(account),
            _menu_kb(account),
        )

    @router.callback_query(F.data == "effort")
    async def cb_effort(callback: CallbackQuery):
        account = _active_account()
        if not account:
            await callback.answer(
                "Сначала добавь куки.", show_alert=True
            )
            return
        builder = InlineKeyboardBuilder()
        for level in ("minimal", "low", "medium", "high", "max"):
            mark = " ·" if level == account.get("effort") else ""
            builder.button(
                text=f"{level.upper()}{mark}",
                callback_data=f"effort_set:{level}",
            )
        builder.button(text="Назад", callback_data="menu")
        builder.adjust(2)
        await _edit(
            callback,
            "<b>Thinking</b>\n\nВыбери уровень.",
            builder.as_markup(),
        )

    @router.callback_query(F.data.startswith("effort_set:"))
    async def cb_effort_set(callback: CallbackQuery):
        level = (callback.data or "").split(":", 1)[1]
        if level not in EFFORT_TO_LEVEL:
            await callback.answer("Неизвестный уровень.", show_alert=True)
            return
        account = _active_account()
        if not account:
            await callback.answer(
                "Нет активного профиля.", show_alert=True
            )
            return
        account = get_storage().update_active_model(
            account["model"], level
        )
        await _edit(
            callback,
            f"Thinking: <b>{level.upper()}</b>\n\n"
            + _menu_text(account),
            _menu_kb(account),
        )

    # ------------------------------------------------------------------ api keys
    @router.callback_query(F.data == "keys")
    async def cb_keys(callback: CallbackQuery):
        await _edit(callback, _keys_text(), _keys_kb())

    @router.callback_query(F.data == "key_new")
    async def cb_key_new(callback: CallbackQuery, state: FSMContext):
        await state.set_state(BotStates.api_key_name)
        await _edit(
            callback,
            "Название ключа. Например: cursor",
            _cancel_kb(),
        )

    @router.message(BotStates.api_key_name)
    async def on_key_name(message: Message, state: FSMContext):
        name = (message.text or "").strip()[:64] or "api"
        key = get_storage().create_api_key(name)
        await state.clear()
        await message.answer(
            "<b>API-ключ создан</b>\n\n"
            f"<code>{_esc(key['key'])}</code>\n\n"
            "Показывается один раз. Используй его в "
            "<code>Authorization: Bearer …</code>.",
            reply_markup=_back_kb(),
            parse_mode=ParseMode.HTML,
        )

    @router.callback_query(F.data == "key_revoke")
    async def cb_key_revoke(callback: CallbackQuery):
        keys = get_storage().list_api_keys()
        if not keys:
            await callback.answer("Активных ключей нет.", show_alert=True)
            return
        builder = InlineKeyboardBuilder()
        for key in keys:
            builder.button(
                text=f"{key['name'][:20]} · {key['prefix']}…"[:60],
                callback_data=f"key_del:{key['id']}",
            )
        builder.button(text="Назад", callback_data="keys")
        builder.adjust(1)
        await _edit(
            callback,
            "<b>Отозвать ключ</b>\n\nВыбери ключ.",
            builder.as_markup(),
        )

    @router.callback_query(F.data.startswith("key_del:"))
    async def cb_key_delete_confirm(callback: CallbackQuery):
        try:
            key_id = int((callback.data or "").split(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("Ошибка.", show_alert=True)
            return
        builder = InlineKeyboardBuilder()
        builder.button(
            text="Отозвать", callback_data=f"key_del_yes:{key_id}"
        )
        builder.button(text="Отмена", callback_data="keys")
        builder.adjust(1)
        await _edit(
            callback,
            "Отозвать этот ключ? Он перестанет работать сразу.",
            builder.as_markup(),
        )

    @router.callback_query(F.data.startswith("key_del_yes:"))
    async def cb_key_delete_yes(callback: CallbackQuery):
        try:
            key_id = int((callback.data or "").split(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("Ошибка.", show_alert=True)
            return
        revoked = get_storage().revoke_api_key(key_id)
        await _edit(
            callback,
            "Ключ отозван." if revoked else "Ключ не найден.",
            _keys_kb(),
        )

    return router


def build_dispatcher(owner_id: int, service: BotService) -> Dispatcher:
    dispatcher = Dispatcher(storage=MemoryStorage())
    router = build_router(service)
    router.message.outer_middleware(OwnerOnly(owner_id))
    router.callback_query.outer_middleware(OwnerOnly(owner_id))
    dispatcher.include_router(router)

    @dispatcher.errors()
    async def on_error(event):
        logger.exception(
            "Bot handler failed: %s", getattr(event, "exception", None)
        )
        return True

    return dispatcher


async def run() -> None:
    storage = get_storage()
    token = storage.get_setting("telegram_bot_token")
    owner = storage.get_setting("telegram_owner_id")
    if not token or not owner:
        raise RuntimeError(
            "Telegram-бот не настроен. Подключи его в меню AiStudio."
        )
    try:
        owner_id = int(owner)
    except ValueError as exc:
        raise RuntimeError("Некорректный Telegram owner id.") from exc

    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    service = BotService()
    try:
        dispatcher = build_dispatcher(owner_id, service)
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Меню"),
                BotCommand(command="cancel", description="Отмена"),
            ]
        )
        await dispatcher.start_polling(
            bot,
            handle_signals=(
                threading.current_thread() is threading.main_thread()
            ),
        )
    finally:
        service.close()
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(run())
