# Progress: PayPay Japan egress investigation

Date: 2026-11-14
Repository: `tenma2066-tech/CatsDaiko5-Beta`
Branch: `feature/paypay-japan-egress-vpngate`
Base: `main`

## User direction

Proceed toward a robust implementation, but preserve the original site and avoid further unnecessary redesign. Keep the work isolated from stable `CatsDaiko5`, Beta `main`, and Cloud Run/Firestore Draft PR #9.

## Current findings

1. Existing PayPay HTTP traffic is centralized in `paypay_gateway.py` and uses `aiohttp.ClientSession` at three request sites: OAuth token, P2P link check, and P2P link acceptance.
2. Beta `render.yaml` deploys a Python web service in Singapore using the standard Render Python runtime, Gunicorn, and no Dockerfile.
3. Render's public Web Services documentation describes runtime, port binding, regions, environment variables, private networking, and deployment features. It does not document a supported way to grant a web service `/dev/net/tun` or `NET_ADMIN` capabilities. Absence from docs is not proof that every VPN technique is impossible, but a TUN-based OpenVPN client cannot be assumed to work on the current service.
4. VPN Gate is a public volunteer relay service, not an HTTP/SOCKS proxy endpoint. Its current public server list provides OpenVPN profiles, and its official policy says connection logs are retained for at least three months; volunteer relays also retain packet-header logs for at least two weeks. The FAQ says individual relay logging policies vary and servers may disappear.
5. Therefore, passing a VPN Gate `.ovpn` profile to `aiohttp` is technically incorrect. Installing an OpenVPN client inside the existing web service without confirmed TUN/network privileges would be an unverified and potentially disruptive change.

## Safety decision

Do not route PayPay phone numbers, passwords, OTPs, access tokens, session cookies, or payment links through an arbitrary public VPN Gate relay. HTTPS protects payloads in transit, but a public relay remains an untrusted operator and exposes connection metadata; do not disable TLS certificate verification. Do not implement a mechanism intended to evade PayPay's security, location, or fraud controls.

## Compatibility-preserving implementation boundary

- No changes to `app.py`, customer HTML, routes, purchase flow, prices, order state machine, game processing, or persistence.
- No changes to stable `CatsDaiko5`.
- No changes to Beta `main`.
- No changes to PR #9 or its Draft status.
- No Render service, environment variable, or data changes.
- Keep `SHOP_LIVE_ORDERS_ENABLED=0`.
- Do not perform real PayPay login or payment.

## Next safe engineering step

Obtain a supported, trusted Japanese egress endpoint under the user's or a trusted operator's control, using a documented application-layer HTTPS proxy or a hosting environment that explicitly supports the required VPN networking. Only then can a narrowly scoped PayPay transport adapter be considered, with explicit opt-in, TLS verification, no direct-network fallback on proxy failure, strict destination allowlisting, and regression tests. A VPN Gate OpenVPN profile alone is not a supported application-layer proxy.

## Sources checked

- Render Web Services: https://render.com/docs/web-services
- VPN Gate FAQ: https://www.vpngate.net/ja/about_faq.aspx
- VPN Gate anti-abuse/logging policy: https://www.vpngate.net/ja/about_abuse.aspx
- VPN Gate live public relay list: https://www.vpngate.net/en/?no_redirect=true

## Status

Investigation and implementation boundary documented. No application code changed because the required trusted, supported Japanese egress endpoint is not available yet. This avoids shipping a fake or unsafe VPN integration and preserves the original site.

## Additional no-card option checked after confirming RAKULINK is unavailable

- Checked WireCanal's official product and pricing pages. Its free tier is advertised as $0, no credit card, one canal, with access protection. Its agent runs on a Windows/Linux machine inside the Japanese network and opens an outbound tunnel to WireCanal's Japan edge.
- Important limitation: raw TCP canals (the feature needed for a conventional TCP proxy) are Pro and above. The free tier is described as HTTP/MCP, not a general TCP tunnel. A custom HTTP egress bridge might be possible, but it would be a separate service on an always-on Japanese PC and would place request traffic through a third-party TLS-terminating edge. This is not approved as a route for PayPay credentials without a careful vendor/security review.
- WireCanal is a tunnel/publishing service, not a ready-made Japanese outbound proxy. Its Japan relay edge alone does not mean outbound requests from Render will leave through a Japanese residential/ISP IP.
- A self-hosted Japanese home gateway would require an always-on Windows/Linux/Raspberry Pi machine and a secure, supported tunnel path. No such device has been confirmed available. An iPhone app alone is not a reliable always-on server-side egress host.
- VPN Gate remains unsuitable for PayPay secrets because its official anti-abuse policy records connection metadata and retains connection logs for at least three months; relay operators are volunteers. Do not use it for PayPay login/payment credentials.

## Updated decision

No code change. Do not wire WireCanal free HTTP canal, VPN Gate, or an arbitrary public proxy into paypay_gateway.py as if it were a trusted, general-purpose Japan egress route. Keep the site untouched and SHOP_LIVE_ORDERS_ENABLED=0. A real implementation still requires either (a) a user-controlled always-on Japanese Windows/Linux host plus an appropriately secured tunnel/forwarder, or (b) a trusted provider that explicitly supplies a server-side Japanese HTTPS/SOCKS proxy endpoint with suitable security guarantees.
