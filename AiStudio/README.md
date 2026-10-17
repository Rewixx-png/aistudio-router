# AiStudio direct chat router

The router sends generation requests to the **Google AI Studio web chat**
endpoint (`MakerSuiteService.GenerateContent`) using the cookies from a logged-in
AI Studio session. It does **not** use a personal `AIza...` API key.

The AI Studio front-end signs every chat request with a BotGuard token bound to
`SHA-256(prompt)`. A headless Chromium page is used only to mint that token; the
actual generation is sent by the Python RPC client with the model and thinking
level selected in the router.

## Install

```bash
pip install -r AiStudio/requirements.txt
# Either use an existing Chrome/Chromium or install Playwright's browser:
python -m playwright install chromium
```

Set `AI_STUDIO_CHROME=/path/to/chrome` if auto-detection fails.

## Run

From `/root` (next to `router_profile.json`):

```bash
python AiStudio/main.py
python AiStudio/main.py --prompt "Reply with exactly OK"
python AiStudio/main.py --gateway --port 20128
```

From inside the package directory:

```bash
cd AiStudio
python main.py
```

Alternative module entry point:

```bash
python -m AiStudio
```

### Termux / limited terminals

On Termux, SSH sessions, and non-UTF-8 stdout the CLI switches to English labels
and ASCII boxes, so Cyrillic, bullets, and emoji cannot turn into `_`.

Force a mode explicitly:

```bash
AI_STUDIO_UI=ascii python AiStudio/main.py   # English + ASCII
AI_STUDIO_UI=unicode python AiStudio/main.py # Russian + Unicode
```

An explicit choice is saved to `~/.aistudio_ui`; later runs use it without the
environment variable.

The model answers themselves are printed as-is; the UI fallback only changes the
router's own labels and boxes.

The CLI, gateway, and Telegram bot share a SQLite database:
`AiStudio/aistudio.db` by default, or `AI_STUDIO_DB`. It stores accounts and
cookies, the active model/effort, gateway API keys, Telegram settings, and
generation history.

On first start the legacy `router_profile.json` is imported into the database
once. After that the database is the source of truth and the JSON file is not
rewritten. Cookies are the only AI Studio credential; the web page key is
extracted automatically when needed.

## OpenAI gateway

```bash
python AiStudio/main.py --gateway --port 20128
```

The gateway binds to `0.0.0.0` by default, prints a **public base URL**, and
generates a fresh **session API key** on every start. The key is valid until the
process exits and is required by every `/v1/*` request.

```bash
curl http://<public-ip>:20128/v1/models \
  -H "Authorization: Bearer sk-aistudio-..."
```

The key is accepted as `Authorization: Bearer ...`, `x-api-key`, `x-goog-api-key`,
or `?key=`. Point an OpenAI client at `http://<public-ip>:20128/v1` and use the
printed key.

Environment overrides:

- `AI_STUDIO_PUBLIC_URL` — exact public base URL (for example
  `https://ai.example.com`; `/v1` is appended if missing). Also available as
  `--public-url`.
- `AI_STUDIO_GATEWAY_KEY` — fixed API key instead of a random session key.

If the server is behind NAT, forward the port or set the public URL explicitly.
For internet exposure prefer HTTPS via a reverse proxy; the gateway itself speaks
HTTP. Do not expose it without the session key.

## Telegram bot

Install the dependencies from `requirements.txt` (including `aiogram>=3.13`),
open the main menu, and choose **Telegram-бот**. Enter the owner's numeric
Telegram ID and a token from `@BotFather`. The token is checked with Telegram
before it is stored in the local database. Only that owner can use the bot.

Bot features:

- send prompts through the active AI Studio profile;
- add, replace, and delete cookie profiles;
- choose model and thinking level;
- create and revoke persistent gateway API keys;
- view the active profile, cookie count, and key count;
- optional **Показать мысли** button; reasoning is never mixed into the answer.

Standalone mode:

```bash
python -m AiStudio.bot
python AiStudio/main.py --telegram
```

The CLI menu starts polling in a background thread; the process must stay open.
Only one polling instance per token can run.

## Storage details

- Database: `AiStudio/aistudio.db` (or `AI_STUDIO_DB`).
- The bot token, cookies, and API key hashes live there. Keep it private.
- The database and `router_profile.json` are ignored by git.
- Gateway keys created in the bot work immediately and survive gateway restarts.
- The session key printed by the gateway remains valid until that process exits.

## Security

Cookies are equivalent to a logged-in Google account. The bot token and gateway
keys grant access to that session. Never commit the database, profile JSON, bot
token, or API keys. This is an unofficial AI Studio endpoint; use it at your own
risk.

