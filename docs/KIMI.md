# Kimi direct transport (experimental)

The direct transport uses Kimi's existing account session, without browser tabs
while answering. It is opt-in and exposes explicit model selections:

| Public ID | Account model | Mode | Live qualification, October 10, 2026 |
| --- | --- | --- | --- |
| `kimi-instant` | `k2d6-chat` | Instant | Direct answer, token refresh and Harness `read` passed |
| `kimi-k3` | `k3-agent` | K3, High, standard context | One request refused: free-account server capacity unavailable |
| `kimi-k2.8` | `k28-agent-preview` | K2.8 Preview, High, standard context | Catalog confirmed; completion not attempted after the K3 refusal |

The separate `kimi-web` adapter uses the model selected in a browser tab.
K3/K2.8 use the site's normal agent scenario, with an empty optional-tool list
and plugins disabled. The bridge does not select Swarm, extra-long context or
paid capabilities. This is not a guarantee about internal provider execution.
The response must confirm the requested model and scenario before any answer
is returned; no fallback model is substituted.

**K3/K2.8 are implemented but not qualified end to end.** The account catalog
advertised both models. The sole K3 probe returned a Connect
`resource_exhausted` error with reason `SERVER_OVERLOADED_FOR_FREE_USER` (117).
The bridge reports `capacity_unavailable`, separately from a quota error, and
pauses the entire Kimi provider. It does not try K2.8 or Instant as a workaround.
This does not establish that the account's quota is exhausted. A successful
future completion and Harness tool round trip are still needed for each model.

On 2026-10-10 one direct Instant completion returned the exact synthetic marker
with the owned Safari tab closed. A subsequent explicit refresh returned HTTP
200, rotated both credentials and granted 900 seconds of access-token lifetime.
The response uses lowerCamelCase Protobuf JSON fields. This one live exchange
qualifies the refresh protocol; the automatic 60-second threshold, concurrency
and failures are covered by offline tests. Native Harness 0.2.0-rc.2 then
completed a controlled `read` and exact answer in two model steps, 16 seconds,
using that refreshed session with the Kimi tab closed. Resuming a direct
conversation id has offline coverage but has not been qualified live.

## Manual initial capture

Install Safari Userscripts from its official distribution and enable it for
`https://www.kimi.ai` (or `https://www.kimi.com`) and local loopback, as described
in [the browser setup](../browser/README.md#safari). Sign in on the normal site.
Read the actual **Save Location** in the Userscripts application; do not infer
the directory from another bridge's generated files.

```sh
python -m integrations.kimi_session --script-directory '/actual/Userscripts/save/folder'
```

Reload the signed-in Kimi tab, then click **Save Kimi session locally**. This
explicit action copies that account's access and refresh tokens to
`session/kimi/session.json` on this computer. The helper has a one-time nonce,
expires after ten minutes and deletes its private userscript on exit. It sends
no model request and never clears an existing provider pause. Close the Kimi
tab after capture to avoid competing browser and bridge refreshes.

The credential file has mode `0600` in a `0700` directory on POSIX. Never commit,
paste or upload it. Exported scripts contain temporary pairing credentials and
must likewise remain private.

## Request-time renewal

To opt in, enable `KIMI_ENABLED=1`, `KIMI_TRANSPORT=direct` and,
separately, `KIMI_RENEW_SESSION=1`. Restart the bridge and reinstall the Harness
catalog with `python -m integrations.harness --install desktop --without-browser`.
The filter includes the three explicitly advertised direct Kimi models.
A catalog entry is not evidence of current account availability; provider
pauses remain in force after installation.

Before a new request, a token with less than 60 seconds remaining is renewed
once through Kimi's normal `AuthService/RefreshToken` endpoint. A local file lock
serializes captures and exchanges. The response must contain a fresh token for
the same account, and the new pair must be atomically persisted before sending
the prompt. This is local account/expiry checking, not offline JWT signature
verification. The server never runs a browser, periodic keepalive or silent
login to maintain access.

An HTTP refusal, uncertain timeout, invalid response or failed write pauses
Kimi. There is no retry, automatic pause reset or replay of the model request.
If the session is revoked, sign in and capture again manually. Inspect the
normal site before explicitly resuming with `python -m providers.access resume kimi`.
Free account quotas and service limits still apply.

The endpoint and protocol come from Kimi's published frontend; this is not an
official or guaranteed third-party API. Frontend changes can stop the adapter.


## Protocol sources

Model IDs and scenarios were read from the signed-in account's normal
`ConfigService/GetAvailableModels` response on October 10, 2026. Request options
and typed error reasons were checked against Kimi's published frontend bundle.
The [official agentic-chat guide](https://www.kimi.ai/help/getting-started/agentic-chat)
describes the K3 reasoning modes. The account catalog and website protocol can
change independently of this adapter; unknown models and response shapes fail
closed.
