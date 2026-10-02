# Experimental providers using your signed-in browser

Grok, Mistral, Kimi and GLM use the normal site's send handler in an existing
signed-in tab. They do not copy cookies, passwords or Google OAuth tokens into
the bridge. Each provider has a separate queue and scoped conversation ids.
All four integrations are opt-in, use the selected web model, and remain
experimental until verified with the provider's current frontend and account.
Kimi's JSON Connect parser binds root answer blocks to the matching user and
successfully completed assistant message; thinking blocks are excluded.
Only this verified wire text can be parsed as requested OpenCode tool calls;
unsupported snapshots and ambiguous message identities fail the request.

## Safari

The two scripts work with [Userscripts for Safari](https://github.com/quoid/userscripts),
distributed through the developer's App Store link. The observer runs in the
page context without privileged APIs. The controller runs in the isolated
content context and keeps its local pairing key out of page JavaScript.

1. Run `python -m providers.tab_bridge` in this repository. It writes personalized
   scripts to `session/browser-bridge/userscripts/` with private permissions.
   Never upload, commit or share that directory: the controller contains your
   local pairing key. The generic files in `browser/` contain no key.
2. Install Userscripts and add the two exported `.user.js` files to its scripts
   directory. Grant access only to `chat.z.ai`, `grok.com`, `chat.mistral.ai`, and
   `www.kimi.com` (or `www.kimi.ai` for the international site); the bridge does not need access to Google sign-in pages.
   After adding or replacing files, open the Userscripts toolbar popup and wait
   until both script names appear. The extension must rescan externally edited
   files before the next page load, as described in its linked README.
   After upgrading the bridge, export and replace the scripts again. The current
   controller is 0.2.5; older loaded scripts do not include all lease checks.
3. Keep the API bound to `127.0.0.1:8000`. Set `BROWSER_BRIDGE_ENABLED=1` and enable
   the provider you want, e.g. `GLM_ENABLED=1`, then restart the API.
4. Open the provider in your normal signed-in browser and reload once to load
   both scripts. Click **Подключить вкладку к OpenCode**. Keep that tab open.
   Userscripts may separately ask for `127.0.0.1` access for local job exchange;
   grant only that address, without choosing all websites. The controller checks
   that its observer is ready before sending a prompt.
   Do not use it for manual chat while a bridge request is running.
5. Point an OpenAI-compatible client at the same `/v1` URL. Model ids are
   `grok-web`, `mistral-web`, `kimi-web`, and `glm-web`. They deliberately do not
   claim a specific model version; select the desired model in the site's UI.

Requests to the local job endpoints require the private pairing key and a
loopback client address. There is no CORS grant. A claimed job is tied to one
tab, provider, document instance, random id and lease. Sending requires a
one-time server authorization; only explicit navigation can hand an unsubmitted
job to a new document. Cancelled, late and replayed results fail, and the
controller checks lease liveness while waiting for a response.
Unsent drafts cause a failure rather than being overwritten. A reload during
generation fails the job instead of automatically repeating its prompt.

Completed text is buffered and validated before any tool calls are returned.
SSE keepalives protect client timeouts. Quota, security and region rejections
are terminal; the bridge does not rotate accounts, solve captchas, change
fingerprints, or retry through another endpoint.

## Provider pauses

All adapters are opt-in. The API attempts a prompt once; quota, security,
regional or uncertain upstream failures persist a provider pause across
restarts. Later requests return HTTP 403 before any browser job or CLI process
is created. A failure after SSE starts is a terminal error event. This does not
undo account restrictions or guarantee that website automation is permitted.

Inspect `python -m providers.access status`. Only after manually checking normal
website access, use `python -m providers.access resume PROVIDER`. A new sign-in
or a different model cannot silently clear the pause. The default 10-second
interval between attempts and continuations is pacing, not a website allowance.
Use one server process; multiple workers do not share an in-flight queue.

## Usage limits

Free access is subject to the website's account, model and feature allowances.
The bridge does not provide an unlimited API or a separate quota. An OpenCode
task can consume several website messages because tool results require another
model turn. A resumed conversation consumes allowance too.

- **Mistral/Vibe:** the [official Free plan](https://mistral.ai/pricing/)
  limits messages and web searches. During the 2026-10-02 live test a new
  completion succeeded, then the resumed request returned HTTP `429`; the user
  also observed the free allowance warning. Further requests stopped. No fixed
  message count or reset time was established. Website plan allowances are
  distinct from Studio/developer API rate limits.
- **Kimi:** the [official chat FAQ](https://www.kimi.com/en/help/others/chat-issues)
  documents a conversation frequency limit and peak-load throttling. Selected
  membership features/models also consume a [shared credit pool](https://www.kimi.com/en/help/membership/membership-overview).
  Kimi Code limits apply to Kimi Code and must not be presented as this browser
  chat adapter's quota.
- **DeepSeek:** our account received a temporary restriction (`biz_code=5`,
  `user is muted`) on 2026-10-02. The exact trigger is unknown; no live retest
  was attempted after the safety changes. Automated website access can restrict
  an account even without an observed quota warning.
- **Qwen and GLM:** no exhausted web-chat quota was observed in our
  controlled qualification tests. This is not proof of unlimited usage. We have
  not established a fixed current web-chat allowance; API/CLI quota tables must
  not be substituted for website limits.

On `429` or a website quota notice, stop the task and inspect the normal account
UI for the relevant allowance and reset time. The bridge does not automatically
switch accounts or models, create another conversation to evade the quota,
purchase credits or retry the rejected turn. Limits can change; check the
provider's current plan before relying on it for sustained agent work.

## Gemini

Install the [official Antigravity CLI](https://antigravity.google/docs/cli/)
and complete its Google account sign-in yourself. Individual free access to the
old Gemini CLI was retired; do not reuse its OAuth credentials in this bridge.

Run `agy models` and put the account's real slugs into `GEMINI_MODELS`, for
example a JSON object mapping your chosen `gemini-...` public name to the exact
CLI slug. Set `GEMINI_ENABLED=1`. No Gemini models are advertised when the map
is empty. The adapter runs the official CLI in an empty temporary workspace,
with a custom text agent containing `tools: []`, no slash commands and no
permission bypass. OpenCode remains responsible for tool execution.
The CLI's streaming initialization must confirm the selected model, custom
agent, empty tool list and normal permission mode before the prompt is sent.
Only one successful terminal result is accepted; tool/subagent events fail.
An available model list does not prove account eligibility: the CLI checks
regional access again when starting a completion.
The adapter currently requires POSIX process groups and terminates the entire
owned group before deleting its temporary workspace, including descendants
whose parent has already exited.

If the official CLI's token exchange fails specifically on an IPv6 socket,
`python -m providers.antigravity_ipv4` runs the same official CLI through a
loopback CONNECT tunnel using IPv4 on your existing network route. Google TLS
is not decrypted; OAuth traffic is not logged. This optional workaround refuses
to override an existing proxy. It is not a region-access workaround: stop if
Google rejects your account or region. No system, browser, DNS or VPN settings
are modified.

## Verification

`python -m unittest discover -s tests -q` checks routing, authentication, queue
ownership, cancellation, protocol completion and CLI isolation. To also run the
owned browser fixtures, install Playwright Chromium and use
`RUN_BROWSER_FIXTURES=1 python -m unittest tests.test_userscripts -q`. Those
fixtures intercept all requests; they do not authenticate or contact providers.
Passing fixtures are not proof of a successful live Safari/provider integration.

On 2026-10-02, a signed-in Safari account using GLM-5.3-Flash passed a controlled
new completion, resumed SSE with the same conversation id, and an OpenCode
`plan` request with a completed native `read` followed by the exact fixture
contents. This used both 0.1.4 scripts and the matching server protocol. It
qualifies that account/session and test, not all GLM models or arbitrary tasks.

With both 0.2.4 scripts on 2026-10-02, Kimi passed an exact new completion and
resumed SSE with the same conversation id, `stop` and `[DONE]`, followed by an
OpenCode `plan` request with a completed native `read` and the exact owned fixture
contents. This qualifies that account/session and controlled test. Mistral passed a new completion, while continuation was
blocked by HTTP `429`; native agent qualification remains pending. Grok's normal
frontend sent the owned prompt and displayed the exact response, but the bridge
could not capture a completed network result, so its live API qualification
failed. Mistral and Grok's partial checks must not be advertised as working agent integrations.
Official Antigravity login and model listing succeeded, but completion was
rejected by Google's regional eligibility check; further attempts stopped.
