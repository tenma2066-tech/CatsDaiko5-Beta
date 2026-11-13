# Beta: original UI + PayPay checkout integration

## Goal

Keep the original CatsDaiko5 customer-facing operation interface and worker behavior. The only intended customer-flow differences are:
- Customers do not create or use a site account.
- Every order must pass the PayPay receipt-confirmation step before credentials are accepted and processing can start.
- The admin area remains protected separately.

The stable `CatsDaiko5` repository is not modified by this work.

## Customer flow

1. `/` (and the legacy `/shop` URL) displays the original full interface: items, quantity settings, character management, subs, other options, safe mode, dry-run, clone and integrity-check controls.
2. The original UI selection is posted to `POST /api/shop/operation-orders`. The server stores the operation payload encrypted; it does not trust a client-supplied amount or unit price.
3. The server creates a private order URL with a high-entropy access token. The order page shows the amount and accepts the PayPay send-money link.
4. `POST /api/shop/order/<id>/paypay` verifies and receives the PayPay payment. The existing PayPay gateway remains the receipt authority. Uncertain or duplicate outcomes remain in manual review and are never retried automatically.
5. Only after the order state is `paid` can the customer submit the transfer code and PIN.
6. The server decrypts the stored operation and credentials, then reuses the existing `/run` validation, queue, concurrency controls and worker path internally. Customers cannot call `/run` directly.
7. The order token scopes the customer's order status and worker progress. It does not grant access to other orders.

## Pricing

The server enforces these fixed prices:
- `character-specific`: 50 JPY per selected character.
- `other-service`: 25 JPY per requested character.
- Quantity: 1–100 characters.
- Amount is calculated server-side as unit price × quantity. Browser-supplied prices are ignored.

When the customer has selected character numbers in the original character-management field, that count is the billable quantity for the 50 JPY category. For other operations, the original interface asks for the billable character count; that billing count is kept separate from the actual operation payload.

## Data and execution safeguards

- Operation payload and transfer credentials are encrypted at rest using the configured Fernet key.
- Public order responses never expose the encrypted payload, transfer code, PIN, or payment-link digest.
- Credential submission is one-time and execution is claimed atomically to prevent duplicate starts.
- The internal worker owner is bound to the order ID; customer progress access requires the matching order token.
- A failed job is moved to manual review; a completed job updates the order state to completed.
- Legacy direct `/run`, `/status` and `/cancel` remain login-gated for the legacy console.
- Read-only catalog/state endpoints needed by the original UI are public. Warmup/catalog refresh still use the existing in-process locks.

## Deployment gate

`SHOP_LIVE_ORDERS_ENABLED=0` must remain in place until the full flow has passed CI and a controlled end-to-end test:
- create order and verify both fixed price categories;
- reject payment attempts before order ownership checks;
- confirm wrong-amount, expired, duplicate and uncertain PayPay links fail closed;
- confirm credentials are unavailable before payment;
- confirm the saved original operation is exactly what the existing worker receives;
- verify order-token isolation, duplicate-submit protection, queueing, job status and restart behavior.

Do not enable live order intake merely because the app deploys or the unit tests pass.

## Runtime constraints

The GitHub runtime store is a commit-backed encrypted snapshot store, not a mounted filesystem or a transactional distributed database. Keep the current single Render instance / single Gunicorn worker assumption. Do not scale horizontally or enable multiple workers without redesigning job coordination and persistence.

## Current implementation boundary

The original UI and worker are now wired to the new PayPay order flow on this feature branch. The legacy simplified SKU storefront is no longer the public landing page. The stable repository and Beta `main` remain unchanged until review and explicit merge.
