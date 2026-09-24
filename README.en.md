# Full Free DeepSeek API → opencode (agent)

A local **OpenAI-compatible bridge** to the free DeepSeek web chat
([chat.deepseek.com](https://chat.deepseek.com)), which you can plug into
**opencode** as a regular model provider and use as a full-fledged agent
(reading/editing files, bash, code search, etc.) — **with no API key and no
payment**.

> **Original project:** <https://github.com/sums001/Deepseek-API>
> This is an unofficial project, not affiliated with DeepSeek. You use your
> regular DeepSeek account and are responsible for complying with its terms.

---

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Single deployment path (copy in full)](#single-deployment-path-copy-in-full)
- [Step by step, with explanations](#step-by-step-with-explanations)
- [Health checks](#health-checks)
- [Where to send requests: URL and API key](#where-to-send-requests-url-and-api-key)
- [opencode integration](#opencode-integration)
- [Switching models, DeepThink and web search](#switching-models-deepthink-and-web-search)
- [Environment variables](#environment-variables)
- [Limitations and important caveats](#limitations-and-important-caveats)
- [Maintenance and troubleshooting](#maintenance-and-troubleshooting)
- [Project structure](#project-structure)
- [Security](#security)
- [License](#license)

---

## How it works

```
┌──────────────┐   OpenAI API    ┌──────────────────────┐   internal    ┌──────────────────┐
│   opencode   │ ───────────────▶│  server/api.py       │ ── protocol ─▶│  chat.deepseek   │
│ (agent/CLI)  │  localhost:8000 │  (FastAPI, /v1/...)  │  + PoW/WASM   │  .com (your acct)│
└──────────────┘                 └──────────────────────┘               └──────────────────┘
                                        │
                                        ├── deepseek/auth.py   (browser login, session in session/)
                                        └── deepseek/pow.py    (proof-of-work via wasmtime)
```

- **opencode** thinks it is talking to an ordinary OpenAI provider.
- The **bridge** translates requests into the DeepSeek web chat, solves the PoW
  challenge (`sha3_wasm_bg.wasm` inside the `wasmtime` sandbox) and streams the
  tokens back.
- The login session is stored in `session/` (not tracked by git) and is
  refreshed automatically about every 5 hours.

---

## Requirements

- **Python 3.9+** (3.11/3.12 recommended)
- **Node.js 18+** — only needed for opencode (not for the bridge)
- **A DeepSeek account** (free, the same one as for chat.deepseek.com)
- **opencode** — <https://opencode.ai>
- OS: Windows, macOS, Linux

---

## Single deployment path (copy in full)

> Below is the whole path from zero to a working agent. Run the blocks in order.
> Replace `~/projects` with a folder of your choice.

### macOS / Linux

```bash
# 0. Prerequisites: Python 3.9+, Node 18+, opencode
python3 --version && node --version && opencode --version

# 1. Clone the project
mkdir -p ~/projects && cd ~/projects
git clone https://github.com/sums001/Deepseek-API.git
cd Deepseek-API

# 2. Virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Dependencies + browser for Playwright
pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium

# 4. One-time login to your DeepSeek account (a browser window opens)
python -m deepseek.auth

# 5. Config (defaults are fine for local use)
cp .env.example .env

# 6. Start the server (keep the terminal open)
python app.py
# -> http://127.0.0.1:8000
```

Check (in a **second** terminal):

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/v1/models
```

### Windows (PowerShell)

```powershell
python --version; node --version; opencode --version

mkdir $HOME\projects; cd $HOME\projects
git clone https://github.com/sums001/Deepseek-API.git
cd Deepseek-API

python -m venv venv
.\venv\Scripts\Activate.ps1

pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium

python -m deepseek.auth
Copy-Item .env.example .env
python app.py
```

> If PowerShell blocks venv activation:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
> (or activate via `venv\Scripts\activate.bat` in `cmd.exe`).

### Registering the provider and agent in opencode

The contents of `opencode.json` and `.opencode/agent/*.md` are in the
[opencode integration](#opencode-integration) section. After creating them,
**restart opencode**.

---

## Step by step, with explanations

### 1. Clone

```bash
git clone https://github.com/sums001/Deepseek-API.git
cd Deepseek-API
```

### 2. Virtual environment

Isolates the project's dependencies from the system Python.

```bash
python3 -m venv venv
source venv/bin/activate        # macOS/Linux
# .\venv\Scripts\Activate.ps1   # Windows PowerShell
```

### 3. Dependencies

```bash
pip install -r requirements.txt
playwright install chromium     # one-time browser install
```

`requirements.txt` pulls in: `playwright` (login + PoW), `httpx`, `fastapi`,
`uvicorn`, `pydantic`, `python-dotenv`, `wasmtime`, `openai`.

### 4. Log in to DeepSeek (once)

```bash
python -m deepseek.auth
```

A real browser opens — sign in to your account and pass the captcha
(human-check). After that the token and cookies are saved in `session/` and
reused. The session is refreshed automatically; you only need to log in again if
it has fully expired.

### 5. `.env` configuration

```bash
cp .env.example .env
```

Defaults are fine for local use. No passwords are stored in `.env` — login is
done manually in the browser.

### 6. Start the server

```bash
python app.py
# -> DeepSeek OpenAI-compatible API on http://127.0.0.1:8000
```

Alternative via uvicorn with a different address:

```bash
HOST=0.0.0.0 PORT=8080 python app.py
# or
uvicorn server.api:app --host 0.0.0.0 --port 8080
```

### 7. Auto-start at login (macOS, optional)

To have the server come up on its own and restart if it crashes, create a
LaunchAgent at `~/Library/LaunchAgents/com.deepseek.api.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.deepseek.api</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOU/root/other/Deepseek-API/venv/bin/python</string>
        <string>app.py</string>
    </array>
    <key>WorkingDirectory</key><string>/Users/YOU/root/other/Deepseek-API</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOST</key><string>127.0.0.1</string>
        <key>PORT</key><string>8000</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/Users/YOU/root/other/Deepseek-API/logs/server.log</string>
    <key>StandardErrorPath</key><string>/Users/YOU/root/other/Deepseek-API/logs/server.err</string>
</dict>
</plist>
```

```bash
mkdir -p logs
launchctl load  ~/Library/LaunchAgents/com.deepseek.api.plist   # enable
launchctl kickstart -k "gui/$(id -u)/com.deepseek.api"          # restart after code changes
launchctl unload ~/Library/LaunchAgents/com.deepseek.api.plist  # disable
```

> Important: with `reload=False` (as in `app.py`), code changes are only picked
> up after a restart — use `kickstart -k`. Check the logs in `logs/server.log`
> and `logs/server.err`.

---

## Health checks

```bash
# 1) Liveness
curl http://127.0.0.1:8000/healthz
# {"status":"ok"}

# 2) Model list
curl http://127.0.0.1:8000/v1/models

# 3) Simple chat
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"Hello!"}]}'
```

Python SDK:

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="unused")
r = client.chat.completions.create(
    model="deepseek-chat",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(r.choices[0].message.content)
```

Ready-made examples: [examples/](examples/) (`01_*` — directly from Python, `04_*`–`06_*` — through the server).

---

## Where to send requests: URL and API key

The base address is `http://localhost:8000/v1` (or `http://127.0.0.1:8000/v1`).
The port changes via the `PORT` variable in `.env` / launchd.

| Method | URL | Purpose |
| --- | --- | --- |
| `POST` | `http://localhost:8000/v1/chat/completions` | Chat; streaming via `"stream": true` |
| `GET` | `http://localhost:8000/v1/models` | Model list (`deepseek-chat`, `deepseek-expert`) |
| `GET` | `http://localhost:8000/healthz` | Liveness check (no rate limit) |

**API key:** any value. The bridge does **not** check or read `Authorization`
(`server/api.py` never looks at the header), so in SDKs where the key is
syntactically required you pass a placeholder — historically `api_key="unused"`.
With plain HTTP via `curl`, the `Authorization` header is not needed at all.

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="unused")
```

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"Hi"}]}'
```

Non-standard fields (outside the OpenAI schema) are passed via `extra_body`:
`thinking` (DeepThink), `search` (web search), `conversation_id` (continue a
conversation; on resume the model is ignored). The limit is
`RATE_LIMIT_PER_MINUTE` (30/min per IP by default); `/healthz` is not counted.

---

## opencode integration

opencode connects to the bridge as an **OpenAI-compatible provider**.

### 1. Provider in `opencode.json`

Create `opencode.json` in the root of your working project (or in
`~/.config/opencode/opencode.json` for global settings):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "local-deepseek": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "DeepSeek (local bridge)",
      "options": {
        "baseURL": "http://127.0.0.1:8000/v1",
        "apiKey": "unused"
      },
      "models": {
        "deepseek-chat": {
          "name": "DeepSeek Chat (Instant)",
          "tool_call": true,
          "reasoning": false,
          "limit": { "context": 64000, "output": 8192 }
        },
        "deepseek-expert": {
          "name": "DeepSeek Expert",
          "tool_call": true,
          "reasoning": false,
          "limit": { "context": 64000, "output": 8192 }
        }
      }
    }
  }
}
```

- `npm: "@ai-sdk/openai-compatible"` — the universal OpenAI-compatible driver.
- `apiKey` is required by the SDK, but the bridge ignores it.
- In opencode, `model` is specified as `local-deepseek/deepseek-chat` or
  `local-deepseek/deepseek-expert`.
- `limit.context` is approximate; reduce it if you get context-overflow errors.

### 2. (Optional) Make DeepSeek the default model

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "local-deepseek/deepseek-chat",
  "small_model": "local-deepseek/deepseek-chat",
  "provider": { "local-deepseek": { "npm": "@ai-sdk/openai-compatible", "name": "DeepSeek (local bridge)", "options": { "baseURL": "http://127.0.0.1:8000/v1", "apiKey": "unused" }, "models": { "deepseek-chat": { "name": "DeepSeek Chat (Instant)", "tool_call": true }, "deepseek-expert": { "name": "DeepSeek Expert", "tool_call": true } } } }
}
```

> `small_model` is used for auxiliary tasks (title generation, etc.).

### 3. Agent

File `.opencode/agent/deepseek.md` in the root of your working project:

```markdown
---
description: An engineering agent on DeepSeek through the local bridge
mode: primary
model: local-deepseek/deepseek-expert
temperature: 0.3
permission:
  edit: allow
  webfetch: allow
  bash:
    "git *": allow
    "*": ask
---

You are a careful engineering agent. First study the code, then propose minimal,
precise changes. Do not add comments unless asked. After changes, run the
project's linter and tests if they exist.
```

- `mode: primary` — the agent is available as a primary one (switchable in the
  TUI / via `default_agent`). For an auxiliary agent use `mode: subagent`.
- The body of the file is the agent's system prompt.

To make it the default agent, add to `opencode.json`:

```json
{ "default_agent": "deepseek" }
```

### 4. Restart

opencode reads the config once at startup. **Fully close and restart opencode**
after creating/editing `opencode.json` and agent files.

Check: open the model picker in opencode — you should see
`DeepSeek Chat (Instant)` and `DeepSeek Expert` under the
`DeepSeek (local bridge)` provider.

### 5. Why the model doesn't make edits (emulated tool calling)

The DeepSeek web chat has **no function-calling channel**, so the bridge emulates
it in text: it injects an instruction into the prompt ("reply with exactly one
```` ```tool_calls ```` JSON block") and then parses the reply back
([server/openai_format.py](server/openai_format.py)). If the model writes prose
instead of a call, opencode simply prints the text and **executes nothing**.

What was done for reliability:

- the parser accepts fences, JSON embedded in prose, and DeepSeek's native
  XML/DSML (`<tool_call>`, `function<|tool_sep|>name`);
- the instruction was hardened (no prose + an example + a reminder at the end);
- a discipline plugin adds a late system instruction.

Diagnostics: start the server with `DEBUG_TOOLCALLS=1` — on an unrecognized call
the raw model reply is written to the log (`logs/server.log` or stdout).

```bash
DEBUG_TOOLCALLS=1 python app.py
```

Test the bridge directly (without opencode):

```bash
curl http://127.0.0.1:8000/v1/chat/completions -H "Content-Type: application/json" -d '{
  "model": "deepseek-chat",
  "messages": [{"role":"user","content":"Create a file /tmp/x.txt with hello"}],
  "tools": [{"type":"function","function":{"name":"write","parameters":{"type":"object","properties":{"filePath":{"type":"string"},"content":{"type":"string"}}}}}]
}'
```

Success — `"finish_reason": "tool_calls"` and a populated `message.tool_calls`.

---

## Switching models, DeepThink and web search

| What | Value in opencode | Note |
| --- | --- | --- |
| Fast model | `local-deepseek/deepseek-chat` | Default |
| Expert model | `local-deepseek/deepseek-expert` | Stronger, slower |
| DeepThink (reasoning) | — | Via `extra_body`, see below |
| Web search | — | Via `extra_body`, see below |

The model is set in opencode as usual (model picker or the `model` field in an
agent). `thinking` (DeepThink) and `search` (web search) are **not** models but
additional request-body flags that opencode does not pass through directly. You
can enable them with a plugin (see below) or via `curl`/SDK requests:

```python
resp = client.chat.completions.create(
    model="deepseek-expert",
    messages=[{"role": "user", "content": "What's new in the world?"}],
    extra_body={"thinking": True, "search": True},
)
```

### Tool-discipline plugin (important for the agent)

Because tool calling is emulated (see above), the model tends to "describe" the
action instead of calling it. The plugin adds a late system instruction that
requires real tool calls. The file is auto-discovered by opencode with no config
changes:

- globally: `~/.config/opencode/plugin/deepseek-tool-discipline.js`
- in the project: `.opencode/plugin/deepseek-tool-discipline.js` (lives in the repo)

```js
export const DeepSeekToolDiscipline = async () => ({
  "experimental.chat.system.transform": async (input, output) => {
    if (input?.model?.providerID !== "local-deepseek") return;
    output.system.push(
      "CRITICAL: to act you MUST emit real tool calls; never describe the " +
      "action, never print JSON/XML or a tool_calls block as visible text."
    );
  },
});
```

> The `experimental.*` hooks are marked experimental (verified on opencode
> 1.18.x). If they are renamed, the plugin simply stops firing and does not
> break startup.

### DeepThink and web search (optional)

`thinking`/`search` are non-standard request-body fields; opencode does not pass
them through directly. You can add them with the `chat.params` hook
(`output.options.thinking = true`), but **do not enable reasoning by default** —
it hurts compliance with the tool-call format. Via `curl`/SDK they work right
away:

```python
resp = client.chat.completions.create(
    model="deepseek-expert",
    messages=[{"role": "user", "content": "What's new in the world?"}],
    extra_body={"thinking": True, "search": True},
)
```

---

## Environment variables

File `.env` (a copy of `.env.example`):

| Variable | Default | Purpose |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | Listen address |
| `PORT` | `8000` | Port |
| `RATE_LIMIT_PER_MINUTE` | `30` | Requests/min per client IP (`/healthz` not counted) |
| `DEEPSEEK_PROFILE_DIR` | — | Reuse an existing Chrome profile with an active session |
| `SERVER_INTERACTIVE_LOGIN` | `1` | Open a browser window when there is no session; `0` — return `503` (for headless) |
| `SESSION_REFRESH_ENABLED` | `1` | Background session refresh |
| `SESSION_REFRESH_INTERVAL` | `18000` (5 h) | Refresh interval, sec (less than `SESSION_MAX_AGE` = 6 h) |
| `REFRESH_BROWSER_CHANNEL` | `chromium-headless-shell` | Playwright channel for headless refresh (less RAM) |
| `REFRESH_BROWSER_CHANNEL_FALLBACK` | `chrome` | Fallback channel if headless can't read cookies; empty — disable |
| `TOOLCALL_MAX_CONTINUATIONS` | `3` | How many times to ask the model to "continue" when a tool call is cut off by the output limit |
| `DEBUG_TOOLCALLS` | — | `1` — log the raw model reply when a call can't be parsed |

Example:

```bash
HOST=0.0.0.0 PORT=8080 RATE_LIMIT_PER_MINUTE=60 python app.py
```

---

## Limitations and important caveats

- **Request serialization.** A single shared account and the non-reentrant
  `wasmtime` PoW store mean upstream calls run **one at a time** (see
  `server/api.py`). Concurrent requests queue up — avoid running several
  agents/sessions at once.
- **Tool calling is emulated.** The bridge buffers the reply and parses it into
  tool calls (`server/openai_format.py`, `parse_tool_calls`). The parser is
  tolerant of prose, fences, and DeepSeek XML/DSML, but this is still less
  reliable than a native API: failures are possible on long multi-step chains.
  The discipline plugin and `DEBUG_TOOLCALLS=1` for diagnostics help.
- **No real token accounting.** `usage` is a rough estimate of ~4 chars/token.
- **Most OpenAI parameters are ignored** (`temperature`, `top_p`, `max_tokens`,
  etc.). Only `model`, `messages`, `stream`, `conversation_id`, `thinking`,
  `search` take effect.
- **Vision is not supported** (no image upload).
- **Conversation id.** `conversation_id` fixes the model when a thread is
  created; on continuation `model` is ignored.
- **Rate limit.** On `429`, use exponential backoff — the official `openai` SDK
  does this automatically.
- **Don't hammer the account.** This is your regular DeepSeek account; mass
  automated requests may get it blocked.

---

## Maintenance and troubleshooting

**Session expired / `503 login_required`**

```bash
python -m deepseek.auth   # log in again
```

Make sure `SERVER_INTERACTIVE_LOGIN=0` on a headless deployment, and that the
login window was completed beforehand.

**`Playwright Sync API inside the asyncio loop`**

Don't call sync Playwright from the event loop — the bridge already wraps calls
in `run_in_threadpool`. This message usually means a non-standard code
modification.

**PoW / wasmtime errors**

```bash
pip install --upgrade wasmtime
```

**Empty headless session (macOS, Chrome profile)**

The fallback to `chrome` is on by default. Keep
`REFRESH_BROWSER_CHANNEL_FALLBACK=chrome` or set `DEEPSEEK_PROFILE_DIR`.

**`429 Too Many Requests`**

Raise `RATE_LIMIT_PER_MINUTE` and/or add retries with backoff.

**Port in use**

```bash
PORT=8080 python app.py
```

and point `baseURL` of the opencode provider at `http://127.0.0.1:8080/v1`.

**The agent replies with text but doesn't change files (no tool calls)**

1. In `opencode.json` the models must have `"tool_call": true`.
2. Test the bridge directly with a `curl` request carrying `tools` (see
   [opencode integration](#opencode-integration)): you should get
   `"finish_reason": "tool_calls"` and a non-empty `message.tool_calls`.
3. Install the `deepseek-tool-discipline.js` plugin (globally or in the project).
4. Start the server with `DEBUG_TOOLCALLS=1` and inspect the raw model reply in
   the log.
5. Increase the agent's `steps` and use `deepseek-expert`.

**Code changes not applied after editing (launchd)**

The server runs under launchd with `reload=False` — restart it:
`launchctl kickstart -k "gui/$(id -u)/com.deepseek.api"`.

**Changes to `opencode.json`/agents not applied**

Restart opencode — the config is not reloaded on the fly.

---

## Project structure

| Path | Purpose |
| --- | --- |
| `app.py` | Entry point — starts the server |
| `deepseek/` | Core: `DeepSeekClient`, login (`auth.py`), HTTP driver (`client.py`), PoW (`pow.py`) |
| `server/` | FastAPI OpenAI-compatible server (`api.py`, `config.py`, `openai_format.py` — tool-call parser, `ratelimit.py`, `schemas.py`) |
| `.opencode/plugin/` | Tool-discipline plugin for opencode |
| `examples/` | Runnable examples (direct Python and through the server) |
| `session/` | Saved session (cookies + token), **git-ignored** |
| `logs/` | launchd service logs, **git-ignored** |
| `.env.example` | Config template |
| `requirements.txt` | Python dependencies |

---

## Security

- Everything in `session/` (cookies + bearer token) stays **on your machine**
  and is excluded from git (`.gitignore`). Never commit `session/`.
- Passwords/secrets are not stored in `.env` — login is done manually in the
  browser.
- With `HOST=0.0.0.0` the bridge is reachable on the network with no
  authentication. Bind only to `127.0.0.1` or protect it with a firewall/proxy.
- Do not publish `session/` or `.env` in public repositories.

---

## License

[MIT License](LICENSE). This is an unofficial project; you are responsible for
complying with DeepSeek's terms of use.

**Original:** <https://github.com/sums001/Deepseek-API>
