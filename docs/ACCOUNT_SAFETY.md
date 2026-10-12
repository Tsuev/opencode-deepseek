# Account safety and terminal refusals

This project automates normal signed-in website accounts. It is not an official
provider API. Website automation can restrict an account, even without an
observed quota warning. Our DeepSeek account returned HTTP 200 JSON containing
`data.biz_code=5` / `user is muted`; the website showed a restriction until
2026-10-04 22:17, with no timezone established. The exact trigger is unknown.
On October 10, normal website access was checked manually. DeepSeek Chat then
passed a bounded native Harness task with its browser closed: one file read
and an exact answer in two model steps. An earlier local parsing failure was
fixed before that run. The historical restriction's cause remains unknown;
this control does not guarantee continued access or qualify the Expert model.

## Defaults and migration

`DEEPSEEK_CONTEXT_ROLLOVER=1` is a separate opt-in exception for an explicit
empty context refusal (`clear_response=true`) observed in the current attempt.
It archives the full history privately before retiring only that attempt's
context pause, then permits one new conversation request. It preserves usage,
pacing, accepted-reply counts and the old consumed history link. Any new error
stops. Partial answers, session/auth refusals, old persisted pauses and unknown
outcomes never authorize migration. A failed/oversized checkpoint also stops.
Only offline fixtures have verified this path. See the Harness guide for limits.

All providers are disabled by default, including DeepSeek (`DEEPSEEK_ENABLED=0`).
Enabling a provider is an explicit decision. Existing `.env` settings cannot
enable automatic server browser login or background session capture anymore.
By default, missing or expired sessions return `401 login_required` before streaming starts,
or a terminal `login_required` SSE error after headers have been sent; sign in manually with
`python -m deepseek.auth` or `python -m qwen.auth`. Qwen reloads the local snapshot
on the next request without a server restart.
Authentication does not remove a pause.

For an explicit normal-site check,
`python -m deepseek.auth --keep-open --channel chromium`
keeps the dedicated sign-in window open until it is closed
or interrupted. It captures changed credentials only from the exact DeepSeek
HTTPS origin, never switches browser channels on failure, and does not clear
pauses or verify account eligibility. The API still loads the local cache only;
DeepSeek's six-hour cache limit requires a manual capture afterward.

DeepSeek failures log a fixed protocol phase, HTTP status and failure category.
No exception text, upstream body, token or prompt is included in these diagnostics.

`QWEN_RENEW_SESSION=1` is a separate, default-off opt-in for one normal cookie
renewal before a new request, only when the local Qwen access token has expired
or is within its 60-second usability margin. It requires an existing refresh
cookie and the same account identity. The renewal is journaled as an upstream
dispatch, with no redirects or retries; cancellation or an uncertain result
persists a pause. A concurrent manual sign-in cannot be overwritten. The server
never renews in response to an upstream rejection, starts a browser, or runs a
background renewal timer. A missing/corrupt cache still requires manual login.

Set both OpenCode `model` and `small_model` deliberately. Titles and other
auxiliary calls consume website messages. Do not assign DeepSeek as an implicit
auxiliary provider for tasks using another account.

## Failure handling

Each request gets one attempt. The server never refreshes and resubmits an
authentication rejection. JSON business errors, SSE error events, HTTP denials,
quotas, unsupported protocols and uncertain transport outcomes stop access to
that provider. Before any provider dispatch, an attempt journal is saved privately in
`session/provider-pauses.json`. Failure to write it prevents dispatch. An
unfinished dispatch remains blocked after a crash or failed pause write; only
a verified completion clears it. Cancellation before dispatch does not create
a pause, but cancellation after dispatch without a verified result does. An account restriction or malformed pause file is not a
reason to create another conversation, switch models, relogin automatically,
rotate accounts, change fingerprints or try another regional route.

A fresh upstream refusal returns HTTP `403` with an allowlisted `code`,
`retryable: false`, and a `message` that names the provider, the mapped pause
reason (never a raw upstream body), states that this bridge enabled a durable
`providers.access` pause, and gives the exact
`python -m providers.access resume PROVIDER` command.

Later requests hit that local pause before any browser job or CLI process is
created and return HTTP `400` (same allowlisted `code`, `retryable: false`) with
an explicit “manual providers.access pause” message and the same resume command.
Using `400` here avoids clients that rewrite every `403` into a generic “API key”
warning. If streaming headers are already sent, the first failure is a terminal
SSE error with the same wording. Invalid local tool responses use HTTP `400`,
and missing sessions use `401` or a terminal SSE error, so standard SDKs cannot
replay a turn on an upstream-shaped `5xx` error. No raw upstream error payload is
published or stored in the pause file.

Attempts, including valid tool continuations, are spaced by 30 seconds by
default (`PROVIDER_MIN_INTERVAL`); the reservation is shared between processes
using the same checkout. This is pacing, not an account quota or a
promise that automation is permitted. Use one server process: multiple workers
or direct client instances do not share an in-flight queue. Direct DeepSeek and
Qwen client streams also use the default guard; custom guard injection is for
transport ownership and isolated offline tests. Default browser/CLI clients
also share attempt ownership. Browser leases check ownership, cancellation and
expiry during pacing and immediately before authorization to submit. Manual
resume revokes the previous attempt; it cannot authorize an old job or let it
pause a new owner. Rejected cached sessions are invalidated
so a manual sign-in plus explicit resume can load the new token.

Browser visibility does not establish whether a request completed. Unknown
post-dispatch outcomes, including capture stalls, retain the pending attempt
and durable pause across restart. Only proven pre-dispatch cancellation may
release safely. There is no exception-attribute override to clear a dispatched
attempt. DOM text is not accepted as a substitute for a network completion.

## Manual recovery

Inspect pauses with:

```bash
python -m providers.access status
```

Stop the task and inspect normal website access and the provider's actual quota
or restriction notice. Only after that manual check, explicitly resume the
provider you verified, for example:

```bash
python -m providers.access resume qwen
```

A different model or successful sign-in cannot silently clear this state. Do
not delete a corrupt pause file merely to repeat a blocked request. Repair its
state from known restrictions; failures to read or save it stop access.

## Offline evidence

Regression tests cover the HTTP 200 muted envelope at session creation, PoW
challenge and completion, SSE errors, the real OpenAI SDK's default retry
policy, persistence and manual resume, failed disk writes, malformed state,
cancellation, tool continuations and provider isolation. A controlled native
OpenCode 1.18.34 run against a loopback-only mocked muted response produced one
API request, one upstream fixture call, one terminal error, and a persisted
pause. Owned browser fixtures never contact providers. These checks prove the
local refusal behavior, not that live website automation is permitted or safe.


## DeepSeek load containment and conversation reuse

The October 10 account restriction showed that bounded task success did not
establish account safety. Keep `TOOLCALL_FORMAT_REPAIR=0` and
`TOOLCALL_MAX_CONTINUATIONS=0`: format errors stop locally, with no additional
model generation. The following changes are qualified using offline transports
and an isolated Harness CLI, not by further requests to the restricted account.

Stateless OpenAI clients continue a saved provider conversation when their
history exactly matches a delivered reply. This applies to DeepSeek and to
opt-in web providers (including Qwen browser tab bridge): only new tool
results/user messages and current tool instructions are sent, so the browser
tab resumes `/c/<id>` instead of opening another chat with the full system
prompt. A private SQLite file in `session/` stores history hashes and provider
conversation IDs, never message bodies.
Links are reserved atomically before dispatch. A reservation is released only
when the current request fails with a proven absence of HTTP dispatch. Once any
HTTP request was dispatched, or after a process crash, the link stays consumed. Unknown, edited, compacted,
already-consumed or ambiguous histories stop locally; they never silently
create another chat. Identical histories from separate chats are considered
ambiguous. Explicit `conversation_id` clients still manage their own history.
One loaded Session snapshot supplies both the HTTP client and the cache scope.
A running request and its stored result remain attached to that snapshot even
if the session file changes; subsequent requests with another snapshot cannot
use its links. Bridge-generated tool IDs are sent with their original arguments
to associate out-of-order results correctly. Text history hashing uses the same
text-part separators as prompt rendering. Successful responses must include a
positive new message ID different from the parent; missing/stale IDs stop the
request without replay or cache advancement.

The snapshot-based cache namespace deliberately does not resume links from the
older file-hash implementation. Such histories stop locally; the migration
does not create a new chat, reset usage, or clear provider pauses. Plain
DeepSeek SSE is buffered so incomplete replies cannot become resumable history.
A stateless conversation defaults to stopping after 20 accepted replies.
`CONVERSATION_MAX_REPLIES` configures this cap (integer 1..1000).
Raising it preserves existing counts and one-use links. This is a safety cap,
not unlimited autonomous operation; continuing a capped/compacted task needs a
separate deliberate workflow, not an automatic new-chat fallback.

Default pacing is 30 seconds between operation starts; DeepSeek HTTP dispatches
within an operation are additionally separated by 3 seconds (or the configured
interval when lower). Requests are serialised. Default rolling limits per local
DeepSeek safety ledger are:

| Charged event | Per hour | Per 24 hours |
| --- | ---: | ---: |
| Generations, including explicitly enabled corrections | 12 | 60 |
| New chats | 3 | 6 |
| All HTTP dispatches, including challenges | 40 | 200 |

Each dispatch is charged before sending, including failures. A request prompt
larger than 128 KiB UTF-8 is rejected locally. A local budget exhaustion is a
non-retryable error, not a waiting/retry loop. Restart and manual provider resume
do not refund counters. Provider restrictions remain independent and persistent;
expiry of a local rolling window does not clear an account pause. Counters are
shared only by processes using the same safety file; separate clones/devices
are not an account-wide quota service. Do not delete or reset safety files to
circumvent these limits.

These ceilings are engineering limits, **not DeepSeek-approved quotas**. They do
not guarantee that website automation is permitted or will avoid restrictions.
Do not rotate to another account to test around an existing restriction.

### Explicit budget configuration

`DEEPSEEK_MAX_{GENERATIONS,CHATS,HTTP}_PER_{HOUR,DAY}` configures the six
ceilings above. Each value must be an integer 1..1000 with day >= hour for each
category; invalid settings fail startup. Restart the server to apply changes.
All processes sharing a ledger must use the same configuration. A changed
ceiling retains charged events and does not clear provider pauses. Raising a
local ceiling does not raise a website quota. The 1000-entry ledger bound is
compatible with the maximum permitted daily HTTP ceiling. See
[Harness examples and error distinctions](HARNESS.en.md#local-limits-and-errors).
