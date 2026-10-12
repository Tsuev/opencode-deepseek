# GLM browserless access: current blockers

Checked October 10, 2026. The existing `glm-web` adapter still needs a signed-in
Safari tab and is excluded by Harness `--without-browser`. No direct GLM account
adapter is enabled or claimed to work.

Two official access routes were inspected:

- **Z.ai web chat:** its public configuration enables CAPTCHA, and the current
  frontend requires the normal verification result for the v2 completion
  request. Copying a login token is insufficient to qualify a browserless
  transport. The bridge does not bypass the challenge.
- **ZCode 3.7.7 / CLI 0.16.3:** the connected Z.ai account's Model settings showed
  `Disabled` and `Not subscribed, enabled after subscription`. No model request
  was sent and no plan was purchased. This is an observation for this account,
  not a claim that every ZCode account requires a subscription. The
  [official configuration guide](https://zcode.z.ai/en/docs/configuration)
  describes account plans and limited new-user trials; actual eligibility must
  be checked in the client.

The [official API pricing](https://docs.z.ai/guides/overview/pricing) lists
`GLM-4.7-Flash` and `GLM-4.5-Flash` as free, while `GLM-5.3-Flash` is paid.
Using that API requires its own API key and is a different product/model choice
from the signed-in web chat. Web-session tokens are not API keys; models are
never silently substituted. Availability and rate limits still apply to free
models.
