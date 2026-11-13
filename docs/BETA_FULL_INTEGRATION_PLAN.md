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
- The server must derive billable character count and total from validated selections. It must not trust a submitted amount, unit price, or arbitrary SKU label.
- Before implementation, map each original UI operation to the pricing unit explicitly. Do not assume that an item count, quantity value, or selected character count are interchangeable.

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
