# Detailed setup

[Home](../README.en.md) · [Русский](SETUP.ru.md)

This reference covers installation, API usage, agents and maintenance. See the home page for provider status and demos. Follow the [separate guide](../browser/README.md) for GLM/Kimi browser tabs and experimental adapters.

The detailed path below is for DeepSeek. For a first run with currently
verified Qwen, use the [quick start](../README.en.md#quick-start). As of
2026-10-02 our DeepSeek account returns `user is muted`; signing in again
did not resolve it.

All providers are now disabled by default. **The instructions below describe
an explicit DeepSeek opt-in (`DEEPSEEK_ENABLED=1` in `.env`) with account
restriction risk.** Do not run them on a restricted account. Use the README's
Qwen path for first-time setup. Failures persist a pause in
`session/provider-pauses.json`; the server never refreshes sessions or replays
requests. Inspect: `python -m providers.access status`. After manually confirming
normal website access: `python -m providers.access resume qwen` (choose the
actual provider). Sign-in does not clear a pause. Provider attempts, including
tool continuations, are spaced by 10 seconds by default.

## Requirements

- **Python 3.9+** (3.11/3.12 recommended)
- **OpenCode** — follow the [official installation guide](https://opencode.ai/docs/). Node.js is only needed for the npm installation method.
- **A DeepSeek account** (free, the same one as for chat.deepseek.com)
- OS: Windows, macOS, Linux

---

## Single deployment path (copy in full)

> Below is the whole path from zero to a working agent. Run the blocks in order.
> Replace `~/projects` with a folder of your choice.

### macOS / Linux

```bash
# 0. Prerequisites: Python 3.9+, opencode
python3 --version && opencode --version

# 1. Clone the project
mkdir -p ~/projects && cd ~/projects
git clone https://github.com/Tsuev/opencode-deepseek.git
cd opencode-deepseek

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
# Explicit optional DeepSeek path; see account restriction warning above.
python -c "from pathlib import Path; p=Path('.env'); p.write_text(p.read_text().replace('DEEPSEEK_ENABLED=0','DEEPSEEK_ENABLED=1'))"

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
python --version; opencode --version

mkdir $HOME\projects; cd $HOME\projects
git clone https://github.com/Tsuev/opencode-deepseek.git
cd opencode-deepseek

python -m venv venv
.\venv\Scripts\Activate.ps1

pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium

python -m deepseek.auth
Copy-Item .env.example .env
(Get-Content .env) -replace 'DEEPSEEK_ENABLED=0', 'DEEPSEEK_ENABLED=1' | Set-Content .env
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
git clone https://github.com/Tsuev/opencode-deepseek.git
cd opencode-deepseek
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
reused. Sessions are not refreshed automatically; sign in manually after expiry.

### 5. `.env` configuration

```bash
cp .env.example .env
# Explicit optional DeepSeek path; see account restriction warning above.
python -c "from pathlib import Path; p=Path('.env'); p.write_text(p.read_text().replace('DEEPSEEK_ENABLED=0','DEEPSEEK_ENABLED=1'))"
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
uvicorn server.api:app --host 0.0.0.0 --port 8080 --no-proxy-headers
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
        <string>/Users/YOU/projects/opencode-deepseek/venv/bin/python</string>
        <string>app.py</string>
    </array>
    <key>WorkingDirectory</key><string>/Users/YOU/projects/opencode-deepseek</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOST</key><string>127.0.0.1</string>
        <key>PORT</key><string>8000</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/Users/YOU/projects/opencode-deepseek/logs/server.log</string>
    <key>StandardErrorPath</key><string>/Users/YOU/projects/opencode-deepseek/logs/server.err</string>
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

Ready-made examples: [examples/](../examples/) (`01_*` — directly from Python, `04_*`–`06_*` — through the server).

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

### 2. Main and auxiliary models

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "local-qwen/qwen3.8-omni-flash",
  "small_model": "local-qwen/qwen3.8-omni-flash",
  "enabled_providers": [
    "local-qwen"
  ],
  "provider": {
    "local-qwen": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Qwen (local bridge)",
      "options": {
        "baseURL": "http://127.0.0.1:8000/v1",
        "apiKey": "unused"
      },
      "models": {
        "qwen3.8-omni-flash": {
          "name": "Qwen3.8 Omni Flash",
          "tool_call": true
        }
      }
    }
  }
}
```

Enable `QWEN_ENABLED=1` and sign in to Qwen. `small_model` sends requests too
(for example titles). Do not silently assign it to DeepSeek.

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
fenced `tool_calls` block containing a JSON array") and then parses the reply back
([server/openai_format.py](../server/openai_format.py)). If the model writes prose
instead of a call, opencode simply prints the text and **executes nothing**.

What was done for reliability:

- only one complete `tool_calls` block occupying the entire reply becomes
  calls; JSON in prose, XML/DSML, and pseudo-calls remain ordinary text;
- the entire array is validated before any call is returned: unknown tools,
  invalid arguments, or a violated `tool_choice` produce `502 invalid_tool_response`;
- truncated blocks are continued in the same conversation; partial calls are never returned;
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
action instead of calling it. The plugin adds a late system instruction requiring
a complete `tool_calls` block, which the bridge converts into API calls. The
file is auto-discovered by opencode with no config changes:

- globally: `~/.config/opencode/plugin/deepseek-tool-discipline.js`
- in the project: `.opencode/plugin/deepseek-tool-discipline.js` (lives in the repo)

```js
export const DeepSeekToolDiscipline = async () => ({
  "experimental.chat.system.transform": async (input, output) => {
    if (input?.model?.providerID !== "local-deepseek") return;
    output.system.push(
      "To perform an action, your ENTIRE reply must be exactly one fenced " +
      "```tool_calls JSON array in the format specified by the bridge. " +
      "Do not add prose, XML, DSML, or pseudo-calls."
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

## Qwen Chat

Qwen uses the same `/v1` endpoint through a separate `local-qwen` provider.
It uses your [chat.qwen.ai](https://chat.qwen.ai/) account and its limits;
this is neither the official Alibaba Cloud API nor Qwen Code authentication.
The web service determines model access and quotas. Support is disabled by default.

1. Run `python -m qwen.auth` in the active Python environment and sign in manually
   in the opened window. If Google rejects the automated browser, use the email-code
   sign-in option available for your account on the login page.
2. Add `QWEN_ENABLED=1` to `.env` and restart the server.
3. Add this provider inside your existing opencode `provider` object, restart
   opencode, and select a model with `/models`.

```json
"local-qwen": {
  "npm": "@ai-sdk/openai-compatible",
  "name": "Qwen Chat (local bridge)",
  "options": {
    "baseURL": "http://127.0.0.1:8000/v1",
    "apiKey": "unused"
  },
  "models": {
    "qwen3.8-omni-flash": {
      "name": "Qwen3.8 Omni Flash",
      "tool_call": true,
      "reasoning": false,
      "limit": { "context": 64000, "output": 8192 }
    },
    "qwen3.8-max": {
      "name": "Qwen3.8 Max",
      "tool_call": true,
      "reasoning": false,
      "limit": { "context": 64000, "output": 8192 }
    }
  }
}
```

These limits are a conservative client budget, not a web-chat quota guarantee.
The `.opencode/plugin/deepseek-tool-discipline.js` plugin supports both providers;
update any previously installed copy. Tool calls use the same strict `tool_calls`
parser. Live replies, conversation continuation and OpenCode `read` execution
were checked on both models before the safety changes. Automatic headless
session refresh is now disabled.

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.8-omni-flash","messages":[{"role":"user","content":"Hello"}]}'

opencode run --model local-qwen/qwen3.8-max "Explain this project"
```

Qwen uses separate private state: `session/qwen/session.json` and
`session/qwen/profile/`. The server loads a usable cache only. Missing or expired
sessions return `401 login_required`, or a terminal `login_required` error in an
already-open SSE stream: run `python -m qwen.auth` manually, then
restart the server. Upstream rejection persists a pause; no automatic refresh
or replay occurs.

Flash and Max share one Qwen account queue, independent of DeepSeek's queue.
Qwen's `conversation_id` includes the `qwen:` prefix and original model;
cross-provider conversations are rejected before accessing account credentials.
This integration supports text; images, audio and video are not uploaded despite
the Omni model name. The unofficial web protocol may change.

---

## Environment variables

The repository's `.env` (a copy of `.env.example`) loads before settings are
read by `app.py`, `uvicorn`, `chat.py`, `deepseek.auth`, or `qwen.auth`. Existing environment
variables take precedence.

| Variable | Default | Purpose |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | Listen address |
| `PORT` | `8000` | Port |
| `RATE_LIMIT_PER_MINUTE` | `30` | Requests/min per client IP (`/healthz` not counted) |
| `DEEPSEEK_PROFILE_DIR` | — | Reuse an existing Chrome profile with an active session |
| `DEEPSEEK_ENABLED` | `0` | Explicit DeepSeek opt-in; account restriction risk |
| `PROVIDER_MIN_INTERVAL` | `10` | Seconds between provider attempts/continuations; not a quota guarantee |
| `QWEN_ENABLED` | `0` | Enable Qwen Chat models on the same `/v1` endpoint |
| `QWEN_PROFILE_DIR` | `session/qwen/profile` | Separate persistent Qwen Chat profile |
| `TOOLCALL_MAX_CONTINUATIONS` | `3` | How many times to ask the model to "continue" when a tool call is cut off by the output limit |
| `DEBUG_TOOLCALLS` | — | `1` — log the raw model reply when a call can't be parsed |

Example:

```bash
HOST=0.0.0.0 PORT=8080 RATE_LIMIT_PER_MINUTE=60 python app.py
```

---

## Limitations and important caveats

- **Request serialization.** The server runs shared-account requests **one at
  a time per provider**, including streams, retries, and continuations (`server/api.py`).
  Waiting in the queue does not occupy worker threads. A direct `DeepSeekClient`
  also serializes generations within one instance. Run one server process:
  multiple workers or separate clients do not share a queue.
- **Tool calling is emulated.** The bridge buffers the reply and parses it into
  tool calls (`server/openai_format.py`, `parse_tool_calls`). One complete
  `tool_calls` block is required; other forms are not executed. `auto`, `none`,
  `required`, and forcing an advertised function are supported. Invalid results
  return an error; SSE responses emit an `error` object before `[DONE]`.
- **Interrupted output.** EOF without a terminal marker and DeepSeek errors
  return errors; the upstream output limit becomes `finish_reason: "length"`.
- **No real token accounting.** `usage` is a rough estimate of ~4 chars/token.
- **Most OpenAI parameters are ignored** (`temperature`, `top_p`, `max_tokens`,
  etc.). Only `model`, `messages`, `stream`, `conversation_id`, `thinking`,
  `search`, `tools`, and `tool_choice` take effect.
- **Vision is not supported** (no image upload).
- **Conversation id.** `conversation_id` fixes the model when a thread is
  created; on continuation `model` is ignored.
- **Limits.** The local rate limit and website quota are independent. Stop on
  exhausted website allowance; SDK retries can repeat requests. Use
  `max_retries=0` for manual diagnosis.
- **Don't hammer the account.** This is your regular DeepSeek account; mass
  automated requests may get it blocked.

---

## Maintenance and troubleshooting

**Session expired / `401 login_required`**

```bash
python -m deepseek.auth   # log in again
```

Sign in manually beforehand and restart the server. Sign-in does not clear a
provider pause; inspect normal website access before explicitly resuming.

**`Playwright Sync API inside the asyncio loop`**

Don't call sync Playwright from the event loop — the bridge already wraps calls
in `run_in_threadpool`. This message usually means a non-standard code
modification.

**PoW / wasmtime errors**

```bash
pip install --upgrade wasmtime
```

On macOS, Xcode's Python 3.9 can terminate with `EXC_GUARD` while loading
WASM, without a Python traceback. If this happens, create a new environment
with a separately installed Python 3.12 and reinstall the dependencies. The
saved login in `session/` can be reused. Check that the module loads before
starting the server:

```bash
python -c "from deepseek.pow import DeepSeekPow; DeepSeekPow()"
```


**`429 Too Many Requests`**

Check the source. The local limiter returns `type: rate_limit_error` and
`Retry-After`: wait for that interval. Website quota is a separate account
allowance; increasing `RATE_LIMIT_PER_MINUTE` cannot change it. Stop requests
on a website quota notice and check the plan/reset time.
[Details](../browser/README.md#usage-limits).

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
| `settings.py` | Loads `.env` before settings are read |
| `deepseek/` | Core: `DeepSeekClient`, login (`auth.py`), HTTP driver (`client.py`), PoW (`pow.py`) |
| `qwen/` | Optional Qwen Chat: separate login, HTTP API v2 and SSE |
| `chat_protocol.py` | Shared SSE event boundaries and completed chat reply |
| `server/` | FastAPI OpenAI-compatible server (`api.py`, `config.py`, `openai_format.py` — tool-call parser, `ratelimit.py`, `schemas.py`) |
| `.opencode/plugin/` | Tool-discipline plugin for opencode |
| `examples/` | Runnable examples (direct Python and through the server) |
| `session/` | Saved session (cookies + token), **git-ignored** |
| `logs/` | launchd service logs, **git-ignored** |
| `.env.example` | Config template |
| `requirements.txt` | Python dependencies |
| `tests/` | Offline regressions for auth, tools, SSE, API, CLI, and rate limiting |

Checks without a DeepSeek account or browser:

```bash
python -m unittest discover -s tests -t . -v
python -c "from deepseek.pow import DeepSeekPow; DeepSeekPow()"
bash -n ds bin/ds-chat
node --check .opencode/plugin/deepseek-tool-discipline.js
```

GitHub Actions runs these checks on Python 3.9 and 3.12.

---

## Security

- Everything in `session/` (cookies + bearer token) stays **on your machine**
  and is excluded from git (`.gitignore`). Never commit `session/`.
- On POSIX, session/chat state files are replaced atomically with `0600`
  permissions; their directories and browser profile use `0700`. Each provider
  captures only its own cookies, preserving domain, path, expiry, and HTTPS restrictions.
- Legacy caches containing a cookie dictionary are recaptured from the browser
  profile. If that fails, run `python -m deepseek.auth` and restart the server.
- Passwords/secrets are not stored in `.env` — login is done manually in the
  browser.
- With `HOST=0.0.0.0` the bridge is reachable on the network with no
  authentication. Bind only to `127.0.0.1` or protect it with a firewall/proxy.
- `app.py` does not trust forwarded headers; rate limiting uses the ASGI peer
  address. When launching via uvicorn, use `--no-proxy-headers`. Behind a trusted
  reverse proxy, enable proxy headers only for its IP using
  `--forwarded-allow-ips`; never use `*`.
- Do not publish `session/` or `.env` in public repositories.

---

## License

[MIT License](../LICENSE). This is an unofficial project; you are responsible for
complying with DeepSeek's terms of use.

**Original:** <https://github.com/sums001/Deepseek-API>
