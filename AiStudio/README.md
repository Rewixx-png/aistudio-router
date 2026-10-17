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

The profile (`router_profile.json`) stores cookies, the selected model, and the
thinking effort. Cookies are the only credential; the web page key is extracted
automatically from AI Studio when needed.

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

