# Account safety and terminal refusals

This project automates normal signed-in website accounts. It is not an official
provider API. Website automation can restrict an account, even without an
observed quota warning. Our DeepSeek account returned HTTP 200 JSON containing
`data.biz_code=5` / `user is muted`; the website showed a restriction until
2026-10-04 22:17, with no timezone established. The exact trigger is unknown.
No live requalification of the changed code used that restricted account.

## Defaults and migration

All providers are disabled by default, including DeepSeek (`DEEPSEEK_ENABLED=0`).
Enabling a provider is an explicit decision. Existing `.env` settings cannot
enable automatic server browser login or background session capture anymore.
Missing or expired sessions return `401 login_required` before streaming starts,
or a terminal `login_required` SSE error after headers have been sent; sign in manually with
`python -m deepseek.auth` or `python -m qwen.auth`, then restart the server.
Authentication does not remove a pause.

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

A paused provider returns HTTP `403` with an error `code` and `retryable: false`
before any client, browser job or CLI completion is created. If streaming headers
are already sent, the first failure is a terminal SSE error; subsequent attempts
get HTTP `403` before provider access. Invalid local tool responses use HTTP
`400`, and missing sessions use `401` or a terminal SSE error, so standard SDKs cannot replay a turn on
an upstream-shaped `5xx` error. No raw upstream error payload is published or
stored in the pause file.

Attempts, including valid tool continuations, are spaced by 10 seconds by
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
