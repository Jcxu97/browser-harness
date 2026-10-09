# Stripe account Test mode and sandboxes

Stripe's new-account onboarding can expose two distinct test environments:

- The account's classic Test mode keeps the main account ID and uses URLs shaped
  like `https://dashboard.stripe.com/{account_id}/test/...`.
- Choosing **Go to sandbox** during onboarding can create an additional sandbox
  with its own account ID and API keys.

Do not assume that the environment opened by the onboarding flow is the main
account's Test mode. Before creating products, prices, or webhooks:

1. Read the account ID from the URL.
2. Open `/{main_account_id}/test/apikeys`.
3. Use the intended Test mode secret key for an authenticated `GET https://api.stripe.com/v1/account` request.
   Send the key in `Authorization: Bearer <test_secret_key>`. Compare the response `id` with the intended main account ID.
   Keep the key in memory through an authorized secret source. Do not print it or include it in command history.

Useful stable routes:

- `/{account_id}/test/apikeys` — Test mode API keys
- `/{account_id}/test/settings/account` — account display name
- `/{account_id}/test/settings/payment_methods` — payment method configuration
- `/{account_id}/test/settings/tax` — Stripe Tax settings

The initial **Business name** onboarding field can replace the display name
shown in the account switcher. Use account IDs to distinguish sibling accounts. Change the display name only when the user requests that change. Use **Settings → Business → Account details** for an authorized change.
