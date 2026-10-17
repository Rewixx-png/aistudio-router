# AiStudio Router

Direct Google AI Studio web-chat router with an OpenAI-compatible gateway and a
Telegram bot.

The router sends generation requests to the same
`MakerSuiteService/GenerateContent` endpoint used by the Google AI Studio web
app. It does **not** use a personal Gemini API key. A headless Chromium page
mints the BotGuard token bound to `SHA-256(prompt)`; the Python client then sends
the request with the selected model and thinking level.

## Features

- Direct AI Studio web chat, authenticated with browser cookies.
- BotGuard token minting through Playwright.
- Model-specific page context, including **Gemini 4 Argon**.
- Shared SQLite database for the CLI, gateway, and Telegram bot.
- OpenAI-compatible `/v1` gateway with a session key and persistent bot-created keys.
- Telegram bot with inline controls:
  - add, replace, and delete cookie profiles;
  - send prompts and choose model/thinking level;
  - create and revoke gateway API keys;
  - optional reasoning view, never mixed into the answer.
- No personal `AIza...` API key and no hardcoded credentials in source.

## Requirements

- Python 3.10+
- A Chromium/Chrome binary (auto-detected or `AI_STUDIO_CHROME`)
- A logged-in Google AI Studio session (cookies)
- A Telegram bot token from `@BotFather` for the bot

## Install

```bash
git clone https://github.com/Rewixx-png/aistudio-router.git
cd aistudio-router
pip install -r AiStudio/requirements.txt
python -m playwright install chromium
```

## Data and profile

The CLI, gateway, and bot share `AiStudio/aistudio.db` (override with
`AI_STUDIO_DB`). It stores cookie profiles, the active model/effort, API keys,
Telegram settings, and generation history.

On first start the legacy `router_profile.json` is imported once. After that the
database is the source of truth and the JSON file is not rewritten.

Add cookies via the CLI menu (**Обновить куки**) or the bot (**Куки**).

**Never commit `aistudio.db` or `router_profile.json`.** Both are ignored by git.

## Run

```bash
# interactive CLI
python AiStudio/main.py

# one-shot
python AiStudio/main.py --prompt "Reply with exactly OK"

# OpenAI-compatible gateway
python AiStudio/main.py --gateway --port 20128

# Telegram bot (standalone polling)
python AiStudio/main.py --telegram
# or
python -m AiStudio.bot
```

From inside the package directory:

```bash
cd AiStudio
python main.py
```

## Telegram bot

Open the CLI menu and choose **Telegram-бот**. Enter:

- the numeric Telegram ID of the owner;
- a bot token from `@BotFather`.

The token is checked with Telegram before it is saved. Only the configured owner
can use the bot. The CLI can run the bot in a background thread, or use
`--telegram`/`python -m AiStudio.bot` for standalone polling. Only one polling
instance per token can run.

## OpenAI gateway

The gateway binds to `0.0.0.0` by default and prints a public base URL. It
requires a key on every `/v1/*` request:

- a random session key generated at startup (valid until the process exits);
- persistent keys created in the bot under **API-ключи** (survive restarts).

```bash
curl http://<public-host>:20128/v1/models \
  -H "Authorization: Bearer sk-aistudio-..."
```

Keys are accepted as `Authorization: Bearer ...`, `x-api-key`, `x-goog-api-key`,
or `?key=`. Point an OpenAI-compatible client at
`http://<public-host>:20128/v1`.

Environment overrides:

| Variable | Purpose |
| --- | --- |
| `AI_STUDIO_DB` | Path to the SQLite database. |
| `AI_STUDIO_PUBLIC_URL` | Exact public base URL (for example `https://ai.example.com`). |
| `AI_STUDIO_GATEWAY_KEY` | Fixed gateway session key instead of a random one. |
| `AI_STUDIO_CHROME` | Path to the Chromium/Chrome executable. |
| `AI_STUDIO_PROFILE` | Legacy JSON profile to import once. |
| `AI_STUDIO_UI` | `ascii` for English + ASCII boxes, `unicode` for Russian + Unicode. Auto-detected on Termux/SSH. |

For internet exposure, open/forward the port and prefer HTTPS through a reverse
proxy. The gateway itself speaks HTTP. Do not expose it without a key.

## Security

- Cookies are equivalent to a logged-in Google account.
- The SQLite database contains cookies, the Telegram bot token, and API key hashes.
- Gateway keys grant access to your AI Studio session.
- This is an unofficial, undocumented endpoint. It may change or stop working,
  and using it may violate Google's terms. Use at your own risk.

## License

MIT. See [LICENSE](LICENSE).
