<p align="center">
  <img src="docs/media/bridge-hero.svg" alt="Local Account Bridge — DeepSeek Harness and OpenCode" width="100%">
</p>

<p align="center">
  <a href="README.md">Русский</a> ·
  <a href="#quick-start">Install</a> ·
  <a href="#connect-harness">Harness</a> ·
  <a href="#connect-opencode">OpenCode</a> ·
  <a href="#provider-status">Providers</a> ·
  <a href="docs/HARNESS.en.md">Harness guide</a>
</p>

# Your AI accounts — in DeepSeek Harness and OpenCode

One local server connects **DeepSeek Harness or OpenCode** to your DeepSeek,
Qwen, Kimi and GLM accounts through an OpenAI-compatible API. Your client runs
project tools; the bridge sends messages to the selected provider. Website
adapters do not require a separate provider API key.

**Harness works independently: you do not need OpenCode to use it.**
Experimental adapters are explicit opt-ins. Read the account restrictions and terminal-refusal settings before running the bridge.

**Security update, October 12, 2026:** the local `/v1` API now requires a bridge
token. After updating, reconfigure your client using
[API access token](#api-access-token). Account binding, replay protection,
shell handling and CLI cleanup fixes were verified offline; this update was
not tested against live accounts in Harness, OpenCode or a browser.

[![Main checks](https://github.com/Tsuev/opencode-deepseek/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/Tsuev/opencode-deepseek/actions/workflows/tests.yml)

> This is not an unlimited API. Account quotas and local budgets apply.
> Automating a website chat can lead to restrictions: our DeepSeek account was
> restricted repeatedly, with the exact cause undetermined. A passing test does
> not guarantee the next task. [Account safety](docs/ACCOUNT_SAFETY.md).

## Quick start

You need **Python 3.9+** (3.12 recommended), Git, an account with your selected
provider, and either [DeepSeek Harness](https://www.deepseek.com/en/harness/) or
[OpenCode](https://opencode.ai/docs/). These commands target macOS/Linux;
see [Windows and detailed setup](docs/SETUP.en.md).

### 1. Install the bridge

```bash
git clone https://github.com/Tsuev/opencode-deepseek.git
cd opencode-deepseek
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
```

When updating an existing installation, preserve `.env` and `session/`.
All providers start disabled; enable only the ones you intend to use.

### 2. Connect an account

This first-run example uses **Qwen Flash**:

```bash
python -m qwen.auth
```

Complete sign-in yourself in the opened window and wait for the session to be
saved. Change this setting in `.env`:

```dotenv
QWEN_ENABLED=1
```

Initial sign-in needs a browser; requests do not need open tabs afterward.
Tokens are short-lived. Optionally set `QWEN_RENEW_SESSION=1` to renew before a
new request; renewal runs inside that request's durable attempt and must return
the same account. Conversation links are bound to the Qwen account, so token
rotation keeps Harness goals resumable. Once the sign-in cookie expires, manual
sign-in is required again.
[Renewal details](docs/HARNESS.en.md#qwen-session).

**Using DeepSeek instead?** Run `python -m deepseek.auth`, sign in, and set
`DEEPSEEK_ENABLED=1`. Replace `qwen3.8-omni-flash` in the Harness command below
with `deepseek-chat` or `deepseek-expert`. DeepSeek has no automatic renewal;
saved sessions have no local age cutoff. Token rejection (`40002`/`40003`) stops requests without retry and requires manual sign-in. A new `python -m deepseek.auth` capture keeps saved Harness histories only for the same verified account; another account is refused (use `--replace-account` deliberately), and an unverified previous session needs `--same-account`. Stop if the
normal website shows an account restriction.

For Kimi, follow [HTTP session setup](docs/KIMI.md); for GLM, use the
[Safari tab adapter](browser/README.md#safari).

### 3. Start the server

```bash
python app.py
```

Keep this terminal running. One process serves all enabled providers; do not
start another instance. In a separate terminal:

```bash
curl http://127.0.0.1:8000/healthz
curl -H "Authorization: Bearer $(cat "$(python -m server.api_auth path)")" http://127.0.0.1:8000/v1/models
```

These local checks send no model requests. Every `/v1` request needs the
bridge API token (see [API access token](#api-access-token)). Confirm your model appears in the
catalog, then connect **your client** below.

## API access token

The `/v1` API drives your signed-in provider accounts, so it requires
`Authorization: Bearer <token>` and a local `Host`. The server creates the
private token file `session/api-token` (`0600`) on first use;
`python -m server.api_auth path` prints its path, never the token. Requests
without the token, from a browser page (cross-site or DNS rebinding) or for
another Host are refused before any provider or rate-limit access.

Migration from the keyless API:

1. Update the bridge and restart it.
2. Harness: rerun `python -m integrations.harness --install <profile> ...` with
   your previous options. It replaces the `Bearer unused` header with the token
   in the private profile file. Restart Harness.
3. OpenCode: set `"apiKey": "{file:/absolute/path/session/api-token}"` for each
   local provider ([example](examples/opencode.json)). Other OpenAI SDK clients
   pass the token as `api_key`.
4. Only while migrating, `API_AUTH=legacy-loopback` admits tokenless requests
   from a loopback peer with a loopback Host and no browser headers. Remove it
   once clients send the token.

`HOST` other than loopback is refused unless `API_ALLOW_PUBLIC_BIND=1` is set
(token mode only). Then also list the served names in `API_ALLOWED_HOSTS` and
restrict the port with a firewall. This bind check lives only in `python app.py`:
launching `uvicorn server.api:app --host …` directly bypasses it, while Host and
token checks (except the transitional `legacy-loopback`) still apply to every `/v1`.

## Connect Harness

1. Install and open [official Harness](https://www.deepseek.com/en/harness/)
   once. During initial setup, you can choose **Add API Key → Set up later**.
2. In another terminal, enter the bridge directory, activate
   `source venv/bin/activate`, and run this while the server is running:

```bash
python -m integrations.harness --install desktop --without-browser --default-model qwen3.8-omni-flash
python -m integrations.harness_shell_safety --profile desktop
```

3. Select your **project folder**, create a **new session**, and choose
   **Local account bridge** → your model in Harness. Restart Harness if the
   group has not appeared. Existing sessions may retain their previous model.
4. Start with a small task, such as “Read this project's README and briefly
   explain its purpose.” Check the tool calls and final response.

The installer reads the local catalog, backs up the profile and updates
`~/.dsh/profiles/desktop/cordis.patch.yml`. It disables automatic retries and
model-generated titles. Setup sends no account requests.

| Connection | Harness configuration |
| --- | --- |
| DeepSeek, Qwen, direct HTTP Kimi | Use the command above with `--without-browser` and an available `--default-model` |
| GLM or `kimi-web` through a tab | Set up the tab; run installation **without** `--without-browser`, selecting `glm-web` or `kimi-web` |
| Web UI / CLI | For an initialized profile, replace `--install desktop` with `--install web` / `--install headless` |
| Enabled or disabled another provider | Restart the bridge and reinstall the catalog with your intended flags |

`--without-browser` filters adapters; it does not remove initial sign-in or
turn GLM into a direct HTTP connection. Catalog visibility does not confirm
account availability. [Full Harness guide](docs/HARNESS.en.md).

### Counters and reset inside Harness

After [installing the local budget plugin](docs/HARNESS.en.md#local-budget-commands),
these commands run **without calling a model**:

| Command | Result |
| --- | --- |
| `/bridge-budget` | Six rolling DeepSeek budgets: generations, new chats and HTTP requests per hour/day, with remaining capacity |
| `/bridge-budget-reset` | Preview only; no counters changed |
| `/bridge-budget-reset CONFIRM` | Explicit confirmation in the same session within 60 seconds |

Reset clears local usage only. It does not clear provider pauses, change website
quotas, request intervals or conversation history. Active or unresolved requests,
and usage changes since the preview, prevent reset. The plugin requires its own
private token; installing the model catalog alone does not install these commands.

### Refresh DeepSeek through Safari

On macOS, select **deepseek-login** in the Harness command menu or enter
`/deepseek-login`. Safari opens; sign in to the same account if needed. The
session transfers locally without an inspector or copying tokens. Check the
result with `/deepseek-login-status` and resume your goal manually.

One-time setup: update the local budget plugin, install
[DeepSeek session transfer](browser/deepseek-session-transfer.user.js) in the
Userscripts app's Save Location, and allow access to `chat.deepseek.com` and
`127.0.0.1`. Restart the updated bridge and Harness after active work finishes.
This flow requires macOS and the bridge on `127.0.0.1`. For a `PORT` other than
8000, run `python -m providers.tab_bridge` and install the exported copy from
`session/browser-bridge/userscripts`, and reinstall the budget plugin with
`python -m integrations.harness_budget --base-url http://127.0.0.1:<PORT> ...` (it
defaults to `PORT` from the environment or `.env`, then 8000).
An explicit `--base-url` takes precedence even when `PORT` is invalid; only
a loopback HTTP origin without credentials, a path, query or fragment is allowed.
[Setup and recovery rules](docs/HARNESS.en.md#update-the-session-through-safari).

History, counters and request intervals are preserved; goals never resume
automatically. The first transfer asks you to confirm the same account; later
transfers compare its saved identifier. This is manual session recovery, not
background renewal. Offline checks passed; this new flow has not yet been
verified end to end with a live account.

### Diagnose interrupted DeepSeek responses

Set `DEEPSEEK_STREAM_DIAGNOSTICS=1` and restart the bridge to save failed SSE
streams privately in `session/failed-streams/` (mode `0600`). Storage is limited
to four captures and 8 MiB of received bytes each. Responses may contain private
source code; do not publish these files. Successful responses are not saved.

Offline inspection: `python -m deepseek.stream_diagnostics session/failed-streams/<id>.json`.
Only a complete capture with a recognized completion marker and valid response
identity can produce a private `.review.json` **review candidate**. It is not
permission to execute tools: inspection never changes history, counters or pauses,
and never resumes goals. Missing completion markers or interrupted captures are
refused. An old failure without captured bytes cannot be reconstructed this way.
[Details](docs/HARNESS.en.md#private-stream-diagnostics).

DeepSeek `context_length_exceeded` means the provider conversation has exhausted
its context. It is not a local budget or expired session; resetting counters or
signing in again will not fix it. The bridge preserves history and stops, without
creating a new chat. Migrating the goal requires a separate manual decision.

Disabling `TOOLCALL_FORMAT_REPAIR` prevents an extra format-correction generation;
it does not disable tool-markup validation. Malformed tool blocks stop the step
instead of being returned as successful plain text.
After a normal DeepSeek stop, commentary may precede one explicit terminal
`tool_calls` block. The bridge locally normalizes flat arguments against the
advertised schema and known closing DSML markers without rewriting values. Empty
arguments are accepted only when the schema permits them. Every call undergoes
JSON Schema validation (types, required fields, nested structures and constraints);
one invalid call rejects the entire batch. Duplicate keys, unknown tools,
ambiguous envelopes, trailing text and incomplete data are rejected. Generic JSON
fences and quotations do not become calls. Prose quotations may precede a separate
top-level `tool_calls` block with a blank line before its opening fence. Quoted,
nested and ambiguous code blocks are not executed; `>` markers are never stripped
from code. An explicit `tool_calls` block is an
action protocol: do not use it for explanatory examples. Remote schemas are never
fetched; local `$ref` references work. On update, install `requirements.txt`
(the new dependency is `jsonschema`) and restart the bridge.

A normal stop with a closed fence also permits swapping the final `]}` to `}]`
only when a string-aware scan confirms that arguments are complete and only the
call object and outer array remain open. No values or delimiters are added. The
entire corrected batch must pass JSON Schema validation; damaged strings, nested
arguments and ambiguous endings are rejected.

A normally finished DeepSeek reply with a closed fence may also recover
missing envelope `}` delimiters between calls or before the array end (up to 32),
only after each complete `arguments` object, with exactly
`name` and `arguments` in that call. Both the prefix and entire batch must parse
strictly after insertion. Argument values are never added or guessed. Every call
is validated against its schema; other syntax defects or damage inside arguments
reject the whole batch without partial execution.

Every reply passes one offline contract in `server/tool_protocol.py`: envelope
parsing → schema validation for every call → tool-choice and finish policy →
release of the entire batch. Transport and private regression replay share this
validator. Incomplete arguments and fabricated tool results remain errors.
This emulates calls through web-chat text, not a native tool API; passing offline
tests does not guarantee that the model's next response will be valid.

Opt-in private diagnostics retain up to 32 distinct failures in
`session/toolcall-failures/` (up to 4 MiB each), without deleting old evidence;
`last-toolcall-error.json` still updates. Replies may contain source code: the
directory is private and excluded from Git. Run sanitized shared regressions:
`python -m unittest tests.test_tool_protocol tests.test_api tests.test_diagnostics`.
Replay a private corpus with reviewed expectations and pinned SHA-256 hashes:
`python -m server.offline_corpus session/protocol-corpus.json`.
This manifest is private, prepared during diagnostic review and not shipped in
the repository; parser output alone must not establish the expected result.

An intentional stop after delivering a recovered answer returns a normal message
with `finish_reason=stop`, no error and no new provider call. It finishes the
recovery turn, not the user's goal. Existing historical `Failed` entries are not
rewritten. JSON and SSE behavior is checked offline; automatic goal continuation
in live Harness has not been verified by these tests.
On explicit continuation, the local notice does not replace the upstream reply
ID: the bridge verifies the original binding and all tool results, preserving
the single-use claim. No duplicate binding that could replay actions is created.

The offline audit also fixed Qwen session construction during login and renewal:
`expires_at` is passed by name instead of occupying the conversation-binding
field. Existing session files are not automatically changed; no live login was
performed for this verification.

Before each reply, the model receives a compact required-field checklist derived
from the schemas. Harness `edit` requires a successful native `read` of the same
`file_path` first. Viewing a file through `bash` (`cat`, `grep`, `git diff`, etc.)
does not count. The dependent `edit` must wait for the `read` result in a separate
reply; an already satisfied read must not be repeated without a reason. Harness's
own guard stays enabled; using another writer to bypass it is forbidden. When
instructed to stop on errors, the model must not automatically read and retry.
The contract was checked offline and in one real Harness continuation: README
`read` → successful result → a separate `edit` → normal `Completed`, with no tool
errors. This does not guarantee full plan compliance: the same turn unnecessarily
repeated an already passed test run despite instructions not to rerun unchanged tests.
The verification reuse contract now keeps successful evidence valid until relevant
code, tests, dependencies, configuration or environment change, a concrete new
concern appears, or the user explicitly requests a fresh run. README wording alone,
a new turn or "final verification" is not a reason to repeat tests. Repeats must
not be hidden in commands with `git diff` or `git status`; a justified repeat
states its reason in the command description. Reused results must be identified
as previous evidence, and skipped tests are not passes. This contract is checked
offline and in one live turn: the model reused prior results, ran no tests and
read existing CI results instead. This is guidance, not a semantic blocker for
duplicate test runs. To stop on shell errors, install the local Harness plugin:

```bash
python -m integrations.harness_shell_safety --profile desktop
```

Restart Harness after installation. `/bridge-shell-status` checks the active executor locally, without generation. There is no mandatory runtime gate: if the installed plugin copy in `<DSH_HOME>/plugins/opencode-local-bridge/` is deleted or corrupted, Harness only logs the error and keeps running without the protection. Check `/bridge-shell-status` after every Harness start before resuming a goal. The plugin adds `set -e -o pipefail` to Bash
commands: a successful `tail` no longer hides an earlier pipeline failure.
A nonzero foreground result, timeout or tool error stops the current turn using
native Stop, without asking the model to retry or diagnose it. History, completed
actions and counters are preserved. `if`, `!`, `&&` and `||` retain normal Bash
semantics: an explicitly handled nonzero status need not fail the whole command.
Plain `gh pr checks` (including `--required`/`--repo`) fetches status once and returns JSON: validated failing or pending checks do not stop the goal. With a valid schema and empty stderr, exit `0` means the JSON was retrieved successfully even when checks are `pending` or `fail`; their statuses remain in the response. Nonzero exit codes `1`/`8` are normalized only when they match the check statuses. Network/authentication errors, malformed responses and other pipeline failures still stop the turn. `--watch`, `--web` and custom formatting retain native gh behavior. Requires `python3` on PATH; `/bridge-shell-status` must report `harness-contract-v3`. The `pending` plus exit `0` case that stopped PR #80 check retrieval was fixed and verified offline using the saved JSON and a shell pipeline, without re-querying CI to test the fix. This does not mean the CI checks themselves passed.

When checking whether a branch contains a fix or locating optional markers/sections, no match is a discovery result even when presence is unknown beforehand. The model contract requires `bridge_grep_optional` for this case; plain `grep` remains for mandatory assertions. Use `-E 'marker_a|marker_b'` for alternatives. Producer errors (`git show`, etc.) still stop the pipeline. This is a prompt selection rule, not a guarantee of model behavior; shell safety does not override plain `grep`.

For an optional search, use the `bridge_grep_optional` shell function: it
normalizes only grep's own no-match status `1`. Invalid patterns, unreadable
files (status `2`) and upstream failures remain failures. Quiet mode
(`-q`/`--quiet`/`--silent`) is refused with status `2`: on a match grep would
exit `0` even if another input failed. Use `-c` or `-n` output instead. Early
exit options (`-m`, `-l`) after a large producer make the producer exit `141`
(SIGPIPE); that is a reported failure, never normalized. The function requires
Bash (both Harness executors run `bash -c`); another shell stops with status `2`. Plain `grep` is not
overridden; assertions requiring a match still stop. Never append `|| true` to
the entire pipeline. Prefer `sed -n '1,40p'` to `head` for bounded output while
draining the stream. Offline checks cover empty/matching searches, invalid
patterns, missing files, producer statuses `1`/`17`, and native Harness
continuation versus stopping.

The plugin does not classify output text or promise to catch background-process
failures. It sets `XDG_CACHE_HOME` for `gh` to a private temporary directory
(`0700`), without changing authentication or sandbox permissions. The old cache
is retained; the new cache remains until OS temporary-file cleanup. Offline tests
in the installed Harness verified that pipeline failure cancels the remaining
command and the next call in its batch, with no second model request; cache
writes succeed.

A subsequent live check successfully read the CI log using `gh run view --log-failed`.
The next `gh api | grep | head` pipeline exited with code `1`; Harness automatically
showed Stopped and sent no third generation. The unfiltered stream was not retained,
so the tool result does not establish the cause of that status. `grep` also returns
`1` for no matches: handle an expected case explicitly in the command instead of
disabling protection globally. Prior history and usage records were preserved;
only two generations and two HTTP requests were added.

After the optional-search fix, the next live turn completed without new errors:
five tool calls inspected the saved CI log and local source, without downloading
the log again or rerunning tests. The original grep pattern matched the saved log;
this does not establish the cause of the earlier `gh api` pipeline status. The CI
report identifies a new failure relative to its baseline but lacks the assertion
traceback. Fixing search handling does not mean that the CI failure is fixed.

For the separate PowerShell failure in the test project, the pytest artifact
confirmed a subprocess `TimeoutExpired` after 10 seconds, not an assertion failure.
With PowerShell 7.6.6 installed, its test fixture now bounds startup separately
(30 s) from projection execution (10 s), without retries or baseline changes.
Focused local checks passed, including an injected startup delay; this does not
establish a green CI rerun or native Windows acceptance.

A later turn exposed a separate protocol limitation: the model returned a
`calls` fence as text. Harness displayed Completed but executed no tool. A complete top-level `calls` fence now aliases `tool_calls` only for normally
finished DeepSeek replies, with full advertised-schema validation. No arguments
are inferred; malformed JSON, multiple blocks, quoted or nested code cannot execute.
Invalid framing no longer becomes a successful text response. Offline checks cover
parser boundaries and real Harness execution with a fixture provider. One-use
recovery of previously delivered text preserves its reply ID, history and usage:
it sends no generation, does not increase the conversation reply count, and stops
locally after the tool result.

Live recovery of this case was verified: one saved `bash` call executed
successfully, followed by a local stop. No new DeepSeek generation was sent;
the budget-state file was unchanged, prior history links were retained, and
the recovered link kept the same reply ID and conversation reply count.

The model also receives a reminder to wait for a write result before its verifier.
This is guidance, not a guarantee: missing `file_path` or another required field
still rejects the batch. The bridge never infers a path from content or another
command and does not automatically retry.

The prompt separates past transcript data from the next assistant message and
forbids simulated tool results. Only Harness supplies actual execution results.
A `Tool (...):` suffix after a call block is never stripped: the entire batch is
rejected without execution. Prompt guidance does not guarantee model compliance.

Verified offline against saved replies without another generation. The fix does
not resume a stopped goal, replay an old reply, or reset history or counters.

### If Harness stops

| Message / symptom | Check |
| --- | --- |
| `Local request budget exhausted` | Rolling budgets, conversation reply cap or prompt size. A new chat or restart does not clear rolling usage |
| `MISSING_CREDENTIAL` for `deepseek-official` | The official API route needs a key. To use the bridge, select **Local account bridge** |
| `login_required` | Saved authentication expired; sign in to the selected provider again |
| `quota_exceeded`, `user is muted`, security refusal | Stop and inspect the normal website; do not retry automatically |
| Missing group or model | Check `.env`, the running server and `/v1/models`, then reinstall the catalog |

DeepSeek defaults are **12/60 generations, 3/6 new chats and 40/200 HTTP requests
per hour/day**, with a shared 20 accepted replies per conversation
(`CONVERSATION_MAX_REPLIES`) and a 30-second interval. `DEEPSEEK_MAX_*` budget
ceilings and `CONVERSATION_MAX_REPLIES` belong to the bridge process environment.
Raising a local ceiling does not raise the website's allowance.
[Configuration and error guide](docs/HARNESS.en.md#local-limits-and-errors).

## Connect OpenCode

Use the same bridge and account setup above. Put the example
[`opencode.json`](examples/opencode.json) in your project. For existing configs,
merge settings while preserving your `provider`, `model` and `small_model`.
Set `small_model` explicitly: title generation can also consume messages.

```bash
cd /path/to/your-project
opencode --agent plan --model local-qwen/qwen3.8-omni-flash
```

For DeepSeek, select `local-deepseek/deepseek-chat` or
`local-deepseek/deepseek-expert`. API URL: `http://127.0.0.1:8000/v1`.
`apiKey` must be the bridge API token, e.g. `"{file:/absolute/path/session/api-token}"`
([API access token](#api-access-token)). It is not a provider account key.
[Agents and tool plugin](docs/SETUP.en.md#opencode-integration).

## Provider status

Implementation and historical checks as of **October 11, 2026**. A passing test
on one account does not establish current access or reliable unlimited goals.
Provider documentation retains detailed qualification results.

| Provider / model ID | Browser during requests | Status |
| --- | --- | --- |
| DeepSeek · `deepseek-chat`, `deepseek-expert` | Not needed after manual sign-in | Browserless HTTP; small Harness tasks passed; repeated account restrictions occurred. No automatic renewal |
| Qwen · HTTP via `QWEN_MODELS` / `qwen-web` | HTTP: not needed after sign-in; `qwen-web`: connected tab | HTTP Flash/Max and the browser adapter verified; Max hit a daily limit. `qwen3.7-plus` is catalogued without a separate live check. [Session](docs/HARNESS.en.md#qwen-session) |
| Kimi · `kimi-instant` | Not needed after capture | Experimental HTTP: reply, renewal and Harness `read` passed |
| Kimi · `kimi-k3`, `kimi-k2.8` | Not needed after capture | HTTP implemented, not qualified end to end: K3 refused free-user capacity; K2.8 was not attempted afterward. [Details](docs/KIMI.md) |
| Kimi · `kimi-web` | Connected tab required | Browser adapter verified; large composer pastes (~4KB+, system+tools) become a TXT attachment on the site — confirmed. See `browser/README.md` and `.env.example` |
| GLM · `glm-web` | Connected tab required | Browser adapter verified; browserless access remains incomplete. [Blockers](docs/GLM.md) |
| Mistral · `mistral-web` | Connected tab required | Browser adapter verified — see `browser/README.md` |
| Grok · `grok-web` | Connected tab required | Browser adapter verified — see `browser/README.md` |
| ChatGPT · `chatgpt-web` | Connected tab required | Browser adapter verified — see `browser/README.md` |
| Claude · `claude-web` | Connected tab required | Browser adapter verified (ProseMirror + `/api/*`) — see `browser/README.md` |
| Gemini · `gemini-web` / Antigravity CLI | `gemini-web`: connected tab; CLI: `agy` | Browser adapter verified. CLI was rejected by region in our test |

All connections start disabled. Website versions, quotas and availability may
change. Chinese providers also have limits; the exact Qwen Max daily allowance
and reset time have not been established.

## Architecture

![Harness or OpenCode → local bridge → HTTP session, browser tab or CLI](docs/media/bridge-architecture.svg)

**Harness or OpenCode runs tools on your computer.** Prompts and tool-read data
are sent to the selected provider. The bridge validates responses and
`tool_calls`, saves conversation state and sends new tool results to the existing
chat. Unknown history stops instead of silently creating a new chat.

Refusal pauses survive restart. Client setup, ordinary sign-in and local budget
reset do not clear them. The explicit Safari transfer can clear an authentication
pause after a rejected challenge when there is no uncertain generation; it does
not clear account restrictions or quotas. Inspect pauses with
`python -m providers.access status`.
[Resume rules](docs/ACCOUNT_SAFETY.md).

The API binds to `127.0.0.1` by default (local only) and requires the bridge
API token. Non-loopback `HOST` needs `API_ALLOW_PUBLIC_BIND=1`,
`API_ALLOWED_HOSTS` and a firewall; never expose it without them. `session/` and `.env` are excluded from Git.
Do not publish them, personalized Userscripts or budget credentials.
Supported: text and emulated tool calls. Images, audio and video are not uploaded,
even for models named Omni.

Known limits: the browser observer runs in the provider page, so scripts already
running in that origin can forge its response events; the bridge binds a job to
the tab, not to an authenticated answer source. DeepSeek/Qwen streams stop after
300 seconds total or 15 seconds without bytes; such a stop pauses the provider
and is not replayed. A first request (no assistant message yet) has no history
link, so resending it can open a second chat; continuations are one-use.

## OpenCode demonstrations

Historical OpenCode 1.18.34 captures of a small fixture-file read.
These are **not Harness screenshots** or proof of current account availability.

| Qwen Flash | Qwen Max |
| --- | --- |
| ![OpenCode Qwen Flash](docs/media/qwen-flash-opencode.png) | ![OpenCode Qwen Max](docs/media/qwen-max-opencode.png) |

[GLM/Kimi captures and recording method](docs/media/README.md).

## Documentation and development

- [Harness: setup, sessions, budgets, offline verification](docs/HARNESS.en.md)
- [Full setup, OpenCode, API and autostart](docs/SETUP.en.md)
- [Kimi HTTP](docs/KIMI.md) · [GLM](docs/GLM.md) · [Safari Userscripts](browser/README.md)
- [Account safety](docs/ACCOUNT_SAFETY.md) · [API examples](examples/README.md)
- [Standalone DeepSeek terminal chat](docs/TERMINAL_CHAT.md)

CI covers Python 3.9/3.12, PoW WASM, shell/JavaScript and isolated browser fixtures.
Locally: `python -m unittest discover -s tests -t . -v`.
[Real Harness CLI fixtures with mocked providers](docs/HARNESS.en.md#offline-verification).

### Browser exchange safety

DOM fallback is disabled: an old visible answer cannot complete a new request.
Claude requires a network terminal marker; truncated, partial or malformed answers are
not reported as complete. Uncertain dispatched outcomes retain pending state
and a safety pause across restart. There is no automatic retry.
Verified offline without live accounts. Re-export and update the userscripts
to apply the browser changes (controller 0.3.34).

Mistral accepts only a request-bound stream with a terminal trailer.
Harness `--without-browser` excludes browser models and their aliases.
The browser controller waits for text insertion, preserves user edits and resumes
only explicitly bound conversations. These cases passed 28 isolated browser
fixtures without accounts.

### Model identifiers

`GET /v1/models` lists bare IDs and `provider/model` aliases, for example
`qwen3.8-max` and `qwen/qwen3.8-max`, or `deepseek-chat` and
`deepseek/deepseek-chat`. Both forms work with the API and Harness catalog.
The prefix must match the provider and does not enable disabled models.
DeepSeek aliases share the same history binding and continuation accounting:
changing the spelling neither creates a new chat nor resets replay protection.
Verified offline; this does not verify model access for a live account.

### Harness contract: history and stopping

Before contacting any provider, the bridge validates tool-call history: every
call needs exactly one result with the same ID. Missing, duplicate and unrelated
results are rejected locally. Parallel batch results may arrive out of order.
This also applies to explicit `conversation_id` requests; arguments are not
invented and completed actions are not replayed.

Harness shell safety disarms automatic goal continuation **before** cancelling
a turn on a tool error. The durable goal phase and revision remain unchanged;
resuming requires a separate user action. Install with
`python -m integrations.harness_shell_safety --profile desktop`, then restart
Harness. `/bridge-shell-status` checks shell settings without sending a request;
the current version reports `harness-contract-v2` and `goal disarm before native stop`.
Model reconfiguration preserves the safety plugin.

Verified offline against Harness 0.2.0-rc.2 contract fixtures: SSE, exact call IDs,
existing-conversation continuation and duplicate-step rejection without counter
changes. This is not a live account check or a guarantee that arbitrary goals
will complete.

### Automatic context migration

`DEEPSEEK_CONTEXT_ROLLOVER=1` enables a new DeepSeek conversation within the
same Harness goal after restarting the bridge. Only an explicit
`context_length_exceeded` refusal with an empty, cleared answer permits one
migration. Full history is stored privately in `session/context-checkpoints/`;
the model receives exact instructions, a completed-call ledger and result
excerpts. Missing details can be read from the archive. This is not a semantic
summary. Harness history, usage and accepted-reply counts are preserved.
Any new failure stops without retry; existing pauses are not cleared automatically.
Off by default. Verified offline without account requests.
[Conditions and limits](docs/HARNESS.en.md#deepseek-conversation-context-exhausted).

**MIT · unofficial project.** Based on
[Tsuev/opencode-deepseek](https://github.com/Tsuev/opencode-deepseek) and
[sums001/Deepseek-API](https://github.com/sums001/Deepseek-API).
Use accounts under each service's terms. [License](LICENSE).

Browser adapters (userscripts, tab protocol, ChatGPT / Claude / Gemini / Qwen /
Kimi / GLM / Mistral / Grok providers and related checks) are reworked in the
[SimolSem/opencode-deepseek](https://github.com/SimolSem/opencode-deepseek) fork
and contributed here via pull requests.

Old upstream branches do not imply missing features: `feat/browser-providers`
was reconciled with `main`, with no missing functional patches found.
[Commit mapping and documentation differences](docs/BRANCH_RECONCILIATION_20261012.md).
The offline budget installer test checks the real model-catalog import in an
isolated copy and reports the original child-process failure.
