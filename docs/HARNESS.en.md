# DeepSeek Harness

The bridge connects to the installed [DeepSeek Harness](https://www.deepseek.com/en/harness/)
through its standard `openai-completions` adapter. Website credentials, request
queues, and durable provider pauses remain in the bridge.

## Setup

Open the official Harness app once. Its welcome screen offers **Add API Key →
Set up later**. Configure your chosen bridge providers using the
[setup guide](SETUP.en.md) and start `python app.py` unless it is already running.
From the bridge checkout with the virtualenv activated:

```bash
python -m pip install -r requirements.txt
python -m integrations.harness --install desktop --default-model qwen3.8-omni-flash
```

Setup reads only `GET http://127.0.0.1:8000/v1/models`, backs up the exact previous
configuration, and updates `~/.dsh/profiles/desktop/cordis.patch.yml`. Enabled
bridge models appear under **Local account bridge**. A requested default absent
from that catalog fails before writing. Harness reloads the configuration;
restart the app if necessary. Select a workspace and start a new session.
Existing sessions may retain their previous model.

Omit `--default-model` to preserve your current default. Use `--install web` for
the initialized Web UI profile or `--install headless` for the initialized CLI
profile. `--home` / `DSH_HOME` selects another Harness home; `--base-url` selects
another **loopback** bridge endpoint.

All bridge adapters share this protocol: DeepSeek, Qwen Flash/Max, GLM, Kimi,
Grok, Mistral, and Gemini models configured through `GEMINI_MODELS`. Their actual
availability and qualification differ: see [provider status](../README.en.md).

### Run without an open browser

To include only adapters that do not need a browser while serving requests:

```bash
python -m integrations.harness --install desktop --without-browser --default-model qwen3.8-omni-flash
```

The filter retains enabled Qwen, DeepSeek, Gemini and the explicitly enabled
experimental `kimi-instant`, `kimi-k3` and `kimi-k2.8` models
([qualification and capacity limits](KIMI.md)). It excludes
GLM, `kimi-web`, Grok and Mistral because those adapters require a connected tab.
It does not move web sessions to another transport, enable providers, or clear
pauses. This selects the catalog at installation time; pass `--without-browser`
again when updating the profile. Other Harness providers and existing sessions
are preserved. If the previous default is excluded, supply a new
`--default-model`; setup refuses to write until you choose one.

Qwen passed live Harness verification with the browser closed. Initial sign-in
and sign-in after the refresh cookie expires still need a browser. On October 10,
2026, DeepSeek Chat completed a `read` and answer task in Harness 0.2.0-rc.2 in
12 seconds with its browser closed. Normal website access was checked first;
the client was fixed to accept both create-chat response shapes. Expert later
passed a bounded native Harness task: `read`, `write`, and a final answer in
three generations / 24.5 seconds, with the browser closed. Written multiline
text, quotes and backslashes matched byte for byte. No automatic continuation
or retry was used. This qualifies that small file task, not an unrestricted
long-running goal. Gemini uses the official CLI, which rejected our test by
region. Not needing a tab does not establish account availability.

Mistral hit its quota during continuation, and Grok's response capture did not
complete; neither belongs in a verified working set. Kimi has frequency limits
and a [shared credit pool](https://www.kimi.ai/help/membership/membership-overview),
but this account's allowance has not been established. On October 10, 2026,
Qwen3.8-Max returned a daily-limit notice as normal answer text, allowing Harness
to continue its goal repeatedly. The bridge now classifies this known notice as
`quota_exceeded` and durably pauses all Qwen requests instead of reporting success.
Resume only after manually checking access; Flash limits and the exact reset
time remain unknown. A lack of quota refusals from GLM does not prove unlimited access. The official Z.ai API is a separate,
key-based access method: its [pricing](https://docs.z.ai/guides/overview/pricing)
distinguishes free GLM-4.7-Flash / GLM-4.5-Flash from paid GLM-5.3-Flash.
Setup never substitutes another model or paid API for the selected web model.

### DeepSeek session

`python -m deepseek.auth` navigates once and waits up to five minutes for manual
sign-in. Use the website's sign-in button if needed. A missing token no longer
forces a navigation to `/sign_in`, which could interrupt an in-progress check.
This fixes the helper's navigation behavior; it is not a confirmed fix for
repeating hCaptcha challenges. Stop if the website keeps rejecting the check.

`python -m deepseek.auth --keep-open --channel chromium` leaves the manual
sign-in window open until it is closed or interrupted with Ctrl+C. Use it to
inspect restrictions on the normal website. Changed credentials are saved
privately; token capture does not confirm access or clear a pause. Use
`--channel chrome` for installed Chrome. This inspection mode never falls
back to another browser channel.

Harness requests use the saved cache only. Cache age alone does not require sign-in. Token rejection (`40002`/`40003`)
persists a pause and stops without retry. Resume requires manual sign-in and
explicitly clearing the pause; DeepSeek has no automatic renewal. Failure
logs contain only the protocol phase, HTTP status and fixed error category,
without credentials, response bodies or prompts.

### Qwen session

Sign in with `python -m qwen.auth`. Website tokens are short-lived (about 15
minutes in the observed session). For longer use, separately set
`QWEN_RENEW_SESSION=1` in `.env` and restart the bridge. It is off by default.

Only before a new user request and before creating a chat, the bridge may call
Qwen's normal renewal endpoint once with the existing sign-in cookie. It checks
account continuity, advanced expiry, and an unchanged local session. Renewal
belongs to the same guarded attempt: cancellation, refusal, uncertain outcome,
or persistence failure stops the request. Rejected messages are never replayed,
pauses are never cleared, and no browser starts. There are no background timers.
When the sign-in cookie expires too, manual sign-in is required. For a persistent
pause, inspect the normal website and follow ACCOUNT_SAFETY.md first.

Without this option, the server reads cached sessions only and `login_required`
requires manual sign-in. The optional `python -m qwen.auth --keep-open` observes
an open website window but does not guarantee renewal while idle. Use it as a
manual mode, separately from server renewal. Selecting a model in the menu does
not verify authentication.

On October 10, 2026, live requests in Harness **0.2.0-rc.2** passed after the
safety changes: Max returned a control answer; Flash executed `read` on a small
owned fixture and returned its contents. A later Flash test started with a
genuinely expired cached session and the browser closed. One guarded HTTP renewal
advanced the expiry, then Flash read `probe.txt` and returned its exact contents
in two steps over 15 seconds; the Qwen guard remained clear. The renewal used the
normal frontend `Timezone` header derived from actual host time and UTC offset.
Max then returned `QWEN_MAX_READY` in five seconds using the renewed session.
These results qualify the tested account and session only; they do not guarantee
future renewal or access. An open window alone does not guarantee renewal while
idle.

## Behavior and limits

- Harness retries are explicitly disabled with `retryPolicy: {mode: normal,
  maxRetries: 0}`; its standard SDK also disables internal retries.
- The profile's `session-title-llm` is disabled to avoid a separate model request
  for chat titles. Additional user-installed plugins may make their own calls.
- Other ordinary provider settings and comments are preserved. Custom bundle,
  root, or home overrides are refused instead of being silently overwritten.
  `!!js` is never evaluated by setup. Export for manual integration with
  `python -m integrations.harness --output session/harness.patch.yml`.
  The exported `llm-pi-ai.config` replaces that entry's entire configuration;
  merge existing `providers` before applying it to a custom profile.
- This integration supports text and emulated tool calls through Chat Completions.
  Images, Responses API, and Harness controls for thinking/search are unsupported.
- The 32768 context / 4096 output values are conservative client budgets, not
  website guarantees. The bridge does not forward token caps or sampling options.
- `Authorization: Bearer <bridge API token>` from `session/api-token`: the /v1
  access control, not a provider account token. Rerun the installer after
  upgrading from the old `Bearer unused` profile. Keep the bridge port local.

Setup never enables providers, clears pauses, or sends an account verification
prompt. An expired restriction date does not prove restored access. Resume only
after a manual website check; see [account safety](ACCOUNT_SAFETY.md). All
providers have account limits, including Chinese models. Tool continuations can
consume another message.

Defaults remain `TOOLCALL_FORMAT_REPAIR=0` and `TOOLCALL_MAX_CONTINUATIONS=0`. The bridge resumes saved conversations and sends only new results; unknown history stops without creating a new chat. Rolling budgets and a configurable conversation cap (20 accepted replies by default) still apply. Historical checks below do not establish readiness for unlimited tasks.

For unattended DeepSeek goals, optionally set `TOOLCALL_FORMAT_REPAIR=1` in
the bridge process environment and restart the bridge. After a normally
completed malformed JSON reply, commentary mixed with a tool block, or unsupported
DSML, the bridge asks once for a newly formatted response in the same conversation.
The original calls are not executed and argument values are never guessed.
A second invalid reply is terminal; quota/account refusals, cancellation and
output-limit replies do not trigger correction. The additional generation costs
quota and observes the normal request interval. This setting defaults to off.

Create the goal in the repository's own workspace: Workspace Write permissions
apply to that directory. A successful text response alone does not establish
that tools ran or the goal completed.

On October 10, 2026, live DeepSeek Expert goals were tested in Harness 0.2.0-rc.2
with this option enabled. A regression-test coding goal finished in 4 minutes
5 seconds and 23 steps: the model fixed its failing assertion and completed the
goal itself. An intermediate task exposed two further response-handling defects:
a missing outer `]` and a false positive on a protocol mention in the final text.
Both were reproduced offline and fixed. The final fresh control goal on the
fixed bridge finished in 64 seconds and 7 steps: 67 tests, diff validation,
goal completion and a final answer. Neither successful run received manual
steering after its initial task; no subagents or runtime browser were used.
This qualifies bounded tasks, not error-free execution of every possible goal.

## Offline verification

The current suite was verified with installed Harness **0.2.0-rc.2**: 14 real CLI scenarios and setup
tests. For an owned HTTP 503 fixture, the default policy made six requests;
the bridge configuration made one. No real accounts were used.

```bash
python -m unittest tests.test_harness -v
HARNESS_COMMAND='["/absolute/path/to/dsh"]' RUN_HARNESS_FIXTURES=1 \
  python -m unittest tests.test_harness_runtime -v
```

The second command uses the real Harness CLI with temporary HOME/DSH_HOME and
workspace, the real local FastAPI server, and a replaced provider transport.
It checks SSE, a native file-read tool round trip, durable refusal handling,
and no retry after HTTP 503. This does not qualify live accounts.

Reference: [official Harness model configuration](https://deepseek-harness.github.io/deepseek-harness/en/guide/providers).

[GLM browserless access blockers](GLM.md).

## Local limits and errors

`Local request budget exhausted` (`local_budget_exceeded`) can mean a rolling
DeepSeek budget, the saved-conversation cap (20 accepted replies by default), or a prompt above
128 KiB UTF-8. Rolling capacity returns as events age out, not at calendar-hour
or midnight boundaries. Restart and `resume` preserve usage. `providers.access
status` reports pauses rather than remaining budgets: an empty result does not
rule out budget exhaustion.

For longer tasks, explicitly configure the single bridge process environment:

```dotenv
DEEPSEEK_MAX_GENERATIONS_PER_HOUR=60
DEEPSEEK_MAX_GENERATIONS_PER_DAY=300
DEEPSEEK_MAX_CHATS_PER_HOUR=6
DEEPSEEK_MAX_CHATS_PER_DAY=24
DEEPSEEK_MAX_HTTP_PER_HOUR=200
DEEPSEEK_MAX_HTTP_PER_DAY=1000
CONVERSATION_MAX_REPLIES=200
```

This is an expanded local budget example, not DeepSeek's allowance. Defaults
remain 12/60 generations, 3/6 new chats and 40/200 HTTP requests. Values must be
integers from 1 to 2000; each daily ceiling must be at least its hourly ceiling.
Invalid settings stop startup. HTTP usage includes chat creation, challenge and
generation; preflight checks space for the whole sequence. Changing limits
preserves usage, pauses and pacing.

Restart after changing `.env`. If a LaunchAgent sets these variables in
`EnvironmentVariables`, change them there and reload the LaunchAgent; `kickstart`
alone does not reread an edited plist. [Autostart](SETUP.en.md) starts the process
at macOS login but does not renew DeepSeek authentication.

If Harness renders tool JSON as text, optional `TOOLCALL_FORMAT_REPAIR=1` allows
one correction after normal completion. A generic fenced JSON list of known
tools is only a correction signal, never directly executable. An invalid second
reply stops. Correction consumes another generation and does not remove website
or conversation limits. Keep Harness automatic retries disabled.

October 11: live Chat passed a reply and conversation-memory check; the service
SSE `finish` fix has regression coverage. Generic JSON correction passed three
format variants using the real Harness CLI and a local mocked provider. A new
large live Expert task has not been qualified after these changes.

`CONVERSATION_MAX_REPLIES` separately controls the conversation cap (default 20, range 1–1000). It counts accepted replies, not all upstream generations: format correction also consumes the rolling budget. After raising it and restarting, exact saved history can continue in the same conversation. Counters and one-use protection are preserved; unknown or edited histories remain rejected.

An empty or whitespace-only tool-mode reply is rejected instead of reported as success. With `TOOLCALL_FORMAT_REPAIR=1`, normal completion permits one correction in the same conversation; a second empty reply returns an explicit error. Output limits, refusals and `tool_choice=none` never trigger correction.

### Local budget commands

In the bridge directory with the virtualenv activated, run the following.
It creates a private token (0600), preserves an existing token, and installs the
plugin into your initialized Harness profile. No provider requests are made.

```bash
python - <<'PYTOKEN'
import os
from pathlib import Path
import secrets
path = Path("session/budget-admin.token").resolve()
path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
if not path.exists():
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(secrets.token_hex(32) + "\n")
path.chmod(0o600)
print(path)  # path only; never print the token
PYTOKEN
python -m integrations.harness_budget --profile desktop --token-file "$PWD/session/budget-admin.token"
```

Add `BRIDGE_BUDGET_TOKEN_FILE=/absolute/path/session/budget-admin.token` to `.env`,
using the printed path. For a LaunchAgent-managed bridge, set the variable in its
environment and reload the service. Restart the bridge; restart Harness if the
profile change has not appeared. For a custom Harness directory, pass
`--home /path/to/.dsh` explicitly: the budget installer does not read `DSH_HOME`.
The token is for local budget administration, not a provider credential. Do not
publish it.

`/bridge-budget` displays all six rolling counters and next availability times.
`/bridge-budget-reset` previews the counters. Confirm explicitly with
`/bridge-budget-reset CONFIRM` in the same session within 60 seconds. Changes
between preview and confirmation reject the reset. Commands run without an LLM.
Reset clears only local usage, preserving provider pauses, pacing and conversation
history. Active or unresolved requests prevent reset. The authenticated loopback
admin endpoint rejects browser Origin requests.

The accepted-reply cap per conversation is displayed separately from rolling
budgets. `/bridge-budget-reset` does not reset it. The command does not show
the current conversation count; reaching the cap reports it in the error.
The bridge never creates a replacement conversation automatically.

An explicit `40002/40003` challenge response before generation preserves the
continuation for manual session recovery. The provider pause and all request
charges remain; there is no automatic retry. Timeouts, ambiguous failures and
failures after generation dispatch do not release history. Transferring history
to a new token requires explicit confirmation of the same account; ordinary
session capture never does this automatically.

### Update the session through Safari

Install [DeepSeek session transfer](../browser/deepseek-session-transfer.user.js)
once in Userscripts' actual Save Location, granting access only to
`chat.deepseek.com` and `127.0.0.1`. Update the local budget plugin and restart
Harness. This supports macOS with the bridge at `http://127.0.0.1:8000` and
does not require `BROWSER_BRIDGE_ENABLED` or the chat controller.

Select **deepseek-login** in Harness' command menu, or run `/deepseek-login`.
Safari opens. If the token was rejected, sign out and back into the same account.
Confirm the same account on the first transfer; later transfers compare the
stored account identifier. The opened tab transfers the session after sign-in;
no inspector or token copying is needed. The one-time request expires in ten
minutes. `/deepseek-login-status` shows the result.

No model request is sent and goals are never resumed automatically. History,
usage and pacing remain unchanged. Only an auth pause with no uncertain
generation can be cleared (explicit challenge `40002/40003`). Account restrictions,
quotas, active requests and uncertain failures require separate investigation.
If local state changes during sign-in, start a new transfer.

The script sends the token, account id, User-Agent and page-visible cookies only
to loopback. Userscripts cannot read HttpOnly cookies; local save success does
not verify upstream acceptance. An ordinary unarmed tab never transfers credentials.

### Private stream diagnostics

Enable `DEEPSEEK_STREAM_DIAGNOSTICS=1` in the bridge environment and restart it.
Authenticated local `GET /admin/budget` reports `stream_diagnostics.enabled`.
`TOOLCALL_DIAGNOSTICS` independently controls failed tool-call reply capture.

On SSE failure, diagnostics save received decoded HTTP bytes, EOF/truncation
flags, an internal error code and the conversation/parent response binding.
HTTP headers, cookies, tokens and the outgoing prompt are not separately captured;
provider output may still contain sensitive data. Files have mode `0600`; a newly
created directory has mode `0700`. Storage stops at four files of up to 8 MiB raw
stream bytes each (about 11 MiB base64 JSON). Existing evidence is never deleted
automatically. Oversize streams retain a prefix marked truncated and cannot recover.

```bash
python -m deepseek.stream_diagnostics session/failed-streams/<id>.json
```

This command uses no network. It verifies integrity, EOF, no truncation, then
reparses the stream and requires normal completion, nonempty text and a fresh
response ID. Success creates a private `.review.json` beside the capture;
existing output is not overwritten. Invalid JSON, unknown completion markers,
incomplete replies, stale IDs and data after `[DONE]` are refused.

This reconstructs text for review; it **does not deliver a reply to Harness**.
A checksum proves byte integrity, not provenance or lack of previous delivery.
History repair still requires checking request ownership, tool schemas and
whether any tools already ran. Pauses, history, counters and goals stay unchanged.
Missing bytes from past failures cannot be reconstructed offline. Checking provider
history would require a separately authorized request; no new generation is used.

Offline checks: `python -m unittest tests.test_stream_capture tests.test_deepseek_protocol tests.test_stream`.
This feature has not been tested against a live account.

### DeepSeek conversation context exhausted

An SSE `hint` with `type=error` and `finish_reason=context_length_exceeded`
means the provider conversation has reached its context limit. The bridge retains
a `conversation_context_exceeded` pause. Signing in again, resetting local
budgets or increasing the reply cap does not resolve the cause.

For future refusals, set `DEEPSEEK_CONTEXT_ROLLOVER=1` in `.env` and restart the
bridge. Harness keeps the same chat and goal; only the DeepSeek conversation
changes. An exact refusal with `clear_response=true`, before any answer text or
terminal marker, permits a private full-request checkpoint, a durable single-use
migration claim and one request in a new conversation. The old consumed step is
never released. Usage, pacing and accepted-reply counts are preserved. Any new
error stops; format repair and truncated-output continuation are disabled for
the migration request.

The packet preserves exact user/system instructions, every completed call ID
and abbreviated results. A result is not assumed successful. The model must
read the archive when details are missing and avoid repeating completed actions;
a prompt cannot guarantee model compliance. Oversized instructions/ledgers,
unresolved calls, partial streams and write failures stop migration. Limits:
32 KiB instructions, 64 KiB packet, 16 MiB archive, at most 64 archives and
64 MiB total. Archives are not automatically deleted.

Proof belongs to the current in-memory attempt. Existing pauses and crashes
during migration never authorize a replay; those require separate local
diagnostic review. Offline checks:
`python -m unittest tests.test_context_rollover tests.test_deepseek_protocol`.
No live accounts were used.
