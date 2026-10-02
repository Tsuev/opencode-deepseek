<p align="center">
  <img src="docs/media/bridge-hero.svg" alt="OpenCode Web Bridge — your AI accounts, one local API" width="100%">
</p>

<p align="center">
  <a href="README.md">Русский</a> ·
  <a href="#quick-start">Get started</a> ·
  <a href="#provider-status">Providers</a> ·
  <a href="#inside-opencode">Demos</a> ·
  <a href="docs/SETUP.en.md">Full guide</a>
</p>

# Your AI accounts, inside OpenCode

A local bridge connects **DeepSeek, Qwen, GLM and Kimi** web chats to OpenCode
through an OpenAI-compatible API. Sign in to your account, select a model and
work with project files using the agent. These connections do not need a
separate provider API key.

**Experimental adapters are explicit opt-ins. Read the account restrictions
and terminal-refusal settings below before running the bridge.**

[![main checks](https://github.com/Tsuev/opencode-deepseek/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/Tsuev/opencode-deepseek/actions/workflows/tests.yml)

> Free web chat uses **your account's allowance**. This is not an unlimited API:
> each agent turn, including a tool result, can require another website message.
> [Quotas and boundaries](#limits-and-boundaries).

> Automating a normal website account can lead to account restrictions.
> Our DeepSeek account is blocked until October 4, 2026, 22:17 as shown by the
> website; the exact cause is unknown. Demo captures predate the safety changes.
> We did not repeat live qualification on the restricted account.

The server no longer refreshes sign-in in the background or replays rejected
requests. Restrictions, quotas, security denials and uncertain outcomes persist
a provider pause across restarts. HTTP errors use `403` and `retryable: false`.
Once SSE headers are sent, the first failure is an error event; the next attempt
returns `403` before accessing the account. Attempts and tool continuations are
spaced by at least 10 seconds by default; pacing does not guarantee quota or
terms compliance.

Inspect pauses with `python -m providers.access status`. Resume a provider using
`python -m providers.access resume qwen` only after manually checking normal
website access. Sign-in does not clear a pause. Set OpenCode's `small_model`
explicitly: title generation consumes messages too and must not silently use
another account.

## Provider status

As of **2026-10-02**. “Verified” means a live answer, conversation continuation
and a native OpenCode `read` on an owned fixture. It qualifies that account and
session, rather than every future task.

| Provider | Local API model ids | Status | Connection |
| --- | --- | --- | --- |
| **DeepSeek** | `deepseek-chat` / `deepseek-expert` | Previously verified; current account returned `user is muted` | [Login and saved session](docs/SETUP.en.md#4-log-in-to-deepseek-once) |
| **Qwen** | `qwen3.8-omni-flash` / `qwen3.8-max` | Flash and Max verified | [Separate Qwen session](docs/SETUP.en.md#qwen-chat) |
| **GLM / Z.ai** | `glm-web` | Verified; GLM-5.3-Flash selected in the test | [Your Safari tab](browser/README.md#safari) |
| **Kimi** | `kimi-web` | Verified, including tools | [Your Safari tab](browser/README.md#safari) |
| **Mistral / Vibe** | `mistral-web` | New reply passed; continuation hit `429`. Agent unverified | Experimental, off by default |
| **Grok** | `grok-web` | Frontend replies; bridge capture does not complete | Experimental, off by default |
| **Gemini** | Configured from `agy models` | Login passed; completion rejected by region | [Official Antigravity CLI](browser/README.md#gemini), off |

`glm-web` and `kimi-web` use the model **selected on the website**; these names
make no version or plan claim. Qwen and all new adapters are opt-in. A fresh
installation exposes no provider, including DeepSeek. Enable only the chosen adapter.

## Inside OpenCode

Real captures from OpenCode **1.18.34**: the `plan` agent reads a small
`hello.py` and returns its result. Demos use a disposable workspace and include
no personal files or account data.

| Qwen3.8 Omni Flash | Qwen3.8 Max |
| --- | --- |
| ![OpenCode with Qwen Flash](docs/media/qwen-flash-opencode.png) | ![OpenCode with Qwen Max](docs/media/qwen-max-opencode.png) |

| GLM · model selected in Safari | Kimi · model selected in Safari |
| --- | --- |
| ![OpenCode with GLM](docs/media/glm-opencode.png) | ![OpenCode with Kimi](docs/media/kimi-opencode.png) |

Click an image for the full frame. [Capture provenance](docs/media/README.md).
The fresh DeepSeek capture stopped on `user is muted`; an older successful
frame is not substituted. No successful agent demos are claimed for Mistral,
Grok or Gemini.

## Quick start

You need **Python 3.9+** (3.12 recommended), Git, a Qwen account and
[OpenCode installed](https://opencode.ai/docs/). The bridge itself does not need
Node.js. These steps are for a fresh macOS/Linux install;
[Windows and details](docs/SETUP.en.md).

**1. Install the repository.**

```bash
git clone https://github.com/Tsuev/opencode-deepseek.git
cd opencode-deepseek
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
```

**2. Sign in to Qwen and enable it.**

```bash
python -m qwen.auth
```

Change `QWEN_ENABLED=0` to `QWEN_ENABLED=1` in `.env`, then:

```bash
python app.py
```

Complete sign-in yourself in the opened browser. Leave the server running.
In a second terminal:

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/v1/models
```

**3. Connect OpenCode in your project.**

Put the [example `opencode.json`](examples/opencode.json) in your project
folder. If a config already exists, merge its `provider` objects while keeping
your settings. The default port is `8000`, `baseURL` is
`http://127.0.0.1:8000/v1`, and `apiKey: "unused"` is an SDK placeholder.
The example's `limit` fields are conservative client budgets, rather than
verified model context specifications or account quotas.

```bash
cd /path/to/your-project
opencode --agent plan --model local-qwen/qwen3.8-omni-flash
```

Ask it to read and explain a file. Use OpenCode's normal agent selection when
you want edits. [Agent and tool-plugin setup](docs/SETUP.en.md#opencode-integration).

**4. Add the accounts you need.**

- **Qwen Max:** after Qwen sign-in, choose `local-qwen/qwen3.8-max`.
- **DeepSeek (account restriction risk):** deliberately set `DEEPSEEK_ENABLED=1`
  in `.env`, run `python -m deepseek.auth`, restart the API, then choose
  `local-deepseek/deepseek-chat` or `local-deepseek/deepseek-expert`. Stop on
  `user is muted`: another sign-in did not resolve it in our test.
- **GLM / Kimi:** install Safari Userscripts and follow
  [tab pairing](browser/README.md#safari). Set `BROWSER_BRIDGE_ENABLED=1` and
  `GLM_ENABLED=1` / `KIMI_ENABLED=1` in `.env`, then restart the API. OpenCode
  model ids are `local-glm/glm-web` / `local-kimi/kimi-web`. Keep the paired tab
  open; closing or reloading it during a request interrupts the task.

Check the status table before enabling an experimental adapter.

## Architecture

![Architecture: OpenCode executes tools locally; the API routes messages to saved DeepSeek/Qwen sessions, GLM/Kimi browser tabs or the official CLI](docs/media/bridge-architecture.svg)

[Open the full diagram](docs/media/bridge-architecture.svg).

**OpenCode executes tools on your computer.** The model receives your prompt
and permitted tool results. The bridge converts a completed answer into text
or validated `tool_calls`; incomplete and invalid calls are not returned.

**Connections use different paths.** DeepSeek and Qwen use locally saved
sessions and the website HTTP protocol. GLM and Kimi send through a normal
Safari tab paired with a local queue using Userscripts. Their login stays in
the browser. Gemini uses a separate official CLI.

Browser answers are checked for request ownership and transport completion
before release. SSE can send keepalives while the verified answer is being
prepared; token-by-token delivery is not promised for every provider.

## Limits and boundaries

- **Mistral Free limits messages.** In our test, quota stopped continuation
  after one successful reply. [Mistral plans](https://mistral.ai/pricing/).
- **Kimi has frequency and peak-load limits.** Chinese providers are not
  inherently unlimited. [Official chat help](https://www.kimi.com/en/help/others/chat-issues).
- **DeepSeek, Qwen and GLM did not exhaust their allowance in our tests.** We
  have not established a fixed current web-chat quota. Paid API/CLI quotas do
  not describe these connections.
- **DeepSeek rejected the current test account:** `biz_code=5`,
  `user is muted`. The response gives no cause; this does not establish a
  service-wide restriction for every user.
- **Text only.** Images, audio and video are not uploaded, even for Omni models.
- **Website protocols can change.** Stop on `429`, a security challenge or a
  region rejection and check the normal account UI.

[Full limit notes](browser/README.md#usage-limits). The bridge does not rotate
accounts to evade quotas or provide regional access.

## Local data and permissions

The API binds to **`127.0.0.1`** by default. Saved sessions live in `session/`
and settings in `.env`; both are excluded from Git. Personalized Userscripts
contain your bridge pairing key and must not be published.

Prompts and files read by the agent are sent to **the selected provider**.
A local bridge does not make a cloud model local. OpenCode permissions govern
access to files and commands. [Security details](docs/SETUP.en.md#security).

## Documentation and development

- [Full setup, API and troubleshooting](docs/SETUP.en.md)
- [Safari, GLM/Kimi, experimental adapters and Gemini](browser/README.md)
- [OpenCode config for verified connections](examples/opencode.json)
- [Python and HTTP examples](examples/README.md)
- [Standalone DeepSeek terminal chat](docs/TERMINAL_CHAT.md)

CI covers Python 3.9/3.12, real PoW WASM loading, shell/JavaScript syntax and
isolated browser fixtures. [CI runs](https://github.com/Tsuev/opencode-deepseek/actions/workflows/tests.yml).
Run locally with `python -m unittest discover -s tests -t . -v`.

**MIT · unofficial project.** Based on
[Tsuev/opencode-deepseek](https://github.com/Tsuev/opencode-deepseek) and
[sums001/Deepseek-API](https://github.com/sums001/Deepseek-API).
Use your account under the corresponding service's terms. [License](LICENSE).
