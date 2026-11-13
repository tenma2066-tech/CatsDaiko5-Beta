# Beta full original-flow integration

## Goal

Keep the original CatsDaiko5/Discord-derived service UI and processing capabilities intact, while changing only the customer access and payment flow:

- Customers do not create accounts or log in.
- Customers must complete PayPay payment before submitting transfer credentials / starting fulfillment.
- The administrator area remains authenticated.
- Existing service menus, selectable options, character catalog/search, per-character settings, safety checks, account-copy mode, integrity-check mode, job progress, and result presentation must remain available.
- This work stays in CatsDaiko5-Beta. Do not modify the stable CatsDaiko5 repository.

## Existing beta architecture findings

- `/` currently redirects to the simplified `/shop` catalog.
- `/shop` currently exposes only the two generic SKUs from `shop_store.DEFAULT_CATALOG`.
- The original full UI is still rendered by `MAIN_HTML` at `/console`, but `/console` and its supporting APIs (`/api/cats`, `/api/warmup`, `/run`, `/status/<job_id>`, etc.) still depend on the legacy `_logged_in()` session gate.
- The worker implementation already supports the original five groups: items, quantity, character management, subs, and others, plus clone and integrity-only modes.
- The current public order payload stores only SKU and quantity; it cannot represent the original UI's full selection/configuration. This must be extended before linking checkout to the original UI.
- `SHOP_LIVE_ORDERS_ENABLED` is currently disabled and must remain disabled until the payment flow, persistent storage, and encryption configuration have been independently verified.

## Required customer journey

1. Open the public site without a customer login.
2. Use the original full interface, including all five menu groups and character search/configuration.
3. Review the exact selected operation and server-calculated price.
4. Create an order with a random, unguessable customer access token. Store only its hash.
5. Complete and verify PayPay payment for the server-calculated amount.
6. Only after verified payment, collect the transfer code and PIN through the order's private URL.
7. Start the existing worker with the exact saved operation payload. Never accept a client-supplied price or a client assertion that payment succeeded.
8. Let the customer view only that order's progress/result through its private token. Admin retains full order and credential visibility.

## Pricing requirements

- Character-specific selection: 50 JPY per selected character.
- All other service selections: 25 JPY per selected character.
- Quantity is limited to 1–100 where quantity applies.
- The billable unit is the order's requested character quantity (1–100), not the number of checkboxes, menu options, or item quantities selected in the original UI.
- The server selects the unit rate from the service category (character-specific selection = 50 JPY; every other service = 25 JPY) and calculates `amount = character_quantity × unit_rate`.
- The checkout summary must show the service category, requested character quantity, unit rate, and total. It must not trust a submitted amount, unit price, or arbitrary SKU label.
- Preserve the original UI selections separately as the operation payload; do not use those checkbox counts as the billing quantity.

## Implementation sequence

1. Preserve the original `MAIN_HTML` and worker semantics; add tests/fixtures for all original selection payload fields.
2. Add a versioned, validated order payload schema to `shop_store`, including migration for existing order databases.
3. Add server-side pricing for each original operation and a checkout summary that mirrors the original UI selection.
4. Replace the customer login gate with order-token authorization for customer-facing APIs. Keep admin routes protected.
5. Bind payment state to an immutable order payload and enforce payment-before-credentials and payment-before-worker-start.
6. Bind job ownership/status/results to the order token, preserving current job queue and concurrency protections.
7. Test all five menus, character search/catalog, per-character settings, clone, integrity-only, safe mode, quantity limits, duplicate payment, uncertain payment, and unauthorized order/job access.
8. Deploy beta with live orders disabled; perform safe end-to-end checks before enabling real PayPay receipt.

## Security and operational constraints

- Do not expose transfer codes or PINs in customer-facing responses or logs.
- Do not let a customer call `/run` directly without a paid order token.
- Do not allow changing the operation payload after payment.
- Keep idempotency/concurrency protections around PayPay acceptance and worker launch.
- Keep the existing one-instance / one-Gunicorn-worker constraint unless the job and payment coordination are redesigned for distributed workers.
- GitHub Contents API runtime storage is a commit-backed snapshot store, not a mounted POSIX disk; keep the low-volume beta limitation documented.
- Do not replace the existing PayPay encryption key without a planned data re-encryption migration.
- Never enable `SHOP_LIVE_ORDERS_ENABLED=1` as part of an unverified UI-only change.

## Acceptance criteria

- The customer-facing page matches the original service UI and retains its capabilities.
- The only customer-facing product-flow differences are no customer login and required PayPay checkout before fulfillment.
- Admin authentication remains separate and enforced.
- All order, payment, credential, and job state transitions are server-authoritative.
- The stable repository is untouched.


## Route-level integration map (source inspection)

| Existing route | Current gate / behavior | Required beta behavior |
|---|---|---|
| `/` | Redirects to `/shop` | Render the original full service UI in public draft mode |
| `/console` | Requires legacy customer password | Keep as an administrator/maintenance entry or redirect appropriately; do not make it the public customer route |
| `/api/state`, `/api/warmup` | Require legacy login | Allow public read/warmup only with abuse limits; do not expose admin data |
| `/api/cats*` | Require legacy login | Public catalog/search endpoints, rate-limited |
| `/run` | Requires legacy login and accepts mutable browser payload | Replace customer invocation with a paid-order-only start endpoint that loads the immutable payload from storage |
| `/status/<job_id>`, `/cancel/<job_id>` | Bound to the legacy session's `job_owner` | Bound to the order access token and only the job associated with that order |
| `/admin*` | Separate admin session/password | Keep protected and separate from customer access |

### Important source-level details

- The current `MAIN_HTML` contains all five original tabs (input, items, quantity, character management, subs, others, status), character lookup, per-character level/form/talent settings, clone, integrity check, safety mode, and result download. Keep this template and its worker semantics rather than rebuilding a reduced product picker.
- Its JavaScript currently submits the entire selection and transfer credentials together to `POST /run`. The customer-facing version must split this into (a) immutable quote/order creation, (b) PayPay verification, (c) credential submission, and (d) paid-order worker start. A front-end-only payment gate is insufficient.
- `shop_store.orders` currently stores SKU, quantity, amount, encrypted credentials, payment state, and access-token hash, but no versioned operation payload or job association. A schema migration is required before the original UI can be safely connected.
- Existing job ownership is a session `job_owner`; the order flow needs explicit order-to-job ownership, including the queued-job cancellation path.
- The original worker accepts transfer/PIN, items, subs, others, quantities, character IDs/settings, dry/safe/clone/integrity flags. Validate and normalize this exact payload once before quote creation, persist it, and start from that stored copy only.

## Outstanding implementation gates

1. **Pricing mapping:** The requested rates are clear (50 JPY per character for character-specific selection, 25 JPY per character for other service), but the original UI contains account-wide item/sub/other operations as well as character selections. Before charging, the billable-unit rule for each non-character operation must be explicitly mapped; silently treating every checkbox or item quantity as a character would mischarge.
2. **Payment state:** Existing PayPay receipt code is behind `SHOP_LIVE_ORDERS_ENABLED`. Keep it disabled until order payload persistence, payment idempotency, and end-to-end test coverage are complete.
3. **Encryption:** The app already has `SECRET_KEY` generated persistently by `render.yaml`; do not rely on the random in-process fallback if deploying outside that Blueprint. Existing PayPay/order encryption data must not be invalidated by key rotation.
4. **Runtime persistence:** GitHub-backed SQLite snapshots must be tested for serialized writes and restart recovery before any live order acceptance. This is not a real mounted disk and does not provide multi-instance transactional locking.
