# AiStudio Router

Direct Google AI Studio web-chat router with an OpenAI-compatible gateway.

This project sends generation requests to the same
`MakerSuiteService/GenerateContent` endpoint used by the Google AI Studio web
app. It does **not** use a personal Gemini API key. A headless Chromium page
mints the BotGuard token bound to `SHA-256(prompt)`; the Python RPC client then
sends the request with the selected model and thinking level.

## Features

- Direct AI Studio web chat, authenticated with browser cookies.
- BotGuard token minting through Playwright.
- Model-specific page context, including **Gemini 4 Argon**.
- Cookie rotation and persistent profile.
- OpenAI-compatible `/v1` gateway with a per-session API key and a public base URL.
- Interactive CLI, one-shot prompts, and gateway modes.
- No personal `AIza...` API key and no hardcoded credentials in the source.

## Requirements

- Python 3.10+
- A Chromium/Chrome binary (auto-detected, or set `AI_STUDIO_CHROME`)
- A logged-in Google AI Studio session (cookies)

## Install

```bash
git clone https://github.com/Rewixx-png/aistudio-router.git
cd aistudio-router
pip install -r AiStudio/requirements.txt
python -m playwright install chromium
```

## Profile

The router reads `router_profile.json` from the current directory, the project
root, or `AI_STUDIO_PROFILE`. It stores cookies, the selected model, and the
thinking effort.

On first run, choose **Update cookies** and paste a Cookie-Editor JSON export, a
cURL command, a Netscape cookie file, or a `Cookie:` header.

**Never commit `router_profile.json`.** It contains live Google session cookies.
It is listed in `.gitignore`.

## Run

From the repository root:

```bash
python AiStudio/main.py
python AiStudio/main.py --prompt "Reply with exactly OK"
python AiStudio/main.py --gateway --port 20128
```

From the package directory:

```bash
cd AiStudio
python main.py
```

Module entry point:

```bash
python -m AiStudio
```

## OpenAI gateway

```bash
python AiStudio/main.py --gateway --port 20128
```

The gateway:

- binds to `0.0.0.0` by default;
- prints a **public base URL**;
- generates a fresh **session API key** on every start;
- requires the key for all `/v1/*` requests.

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
| `AI_STUDIO_PUBLIC_URL` | Exact public base URL (for example `https://ai.example.com`). `/v1` is appended if missing. |
| `AI_STUDIO_GATEWAY_KEY` | Fixed API key instead of a random session key. |
| `AI_STUDIO_CHROME` | Path to the Chromium/Chrome executable. |
| `AI_STUDIO_PROFILE` | Path to `router_profile.json`. |

For internet exposure, open/forward the port and prefer HTTPS through a reverse
proxy. The gateway itself speaks HTTP. Do not expose it without the session key.

## Security

- Cookies are equivalent to a logged-in Google account. Treat them as secrets.
- The gateway API key grants access to your AI Studio session. Do not publish it.
- This is an unofficial, undocumented AI Studio endpoint. It may change or stop
  working at any time, and using it may violate Google's terms. Use at your own
  risk.

## License

MIT. See [LICENSE](LICENSE).
