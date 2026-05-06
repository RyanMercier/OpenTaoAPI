# Security policy

## Reporting a vulnerability

If you find a security issue, please **do not** open a public GitHub issue.
Email the maintainer at `ryanmercier` on GitHub or open a
[private security advisory](https://github.com/ryanmercier/OpenTaoAPI/security/advisories/new).
I'll respond within a few days.

## Threat model

OpenTaoAPI is designed to be **self-hosted** by a single operator on
infrastructure they trust. The defaults assume:

- The API is behind a reverse proxy you control (nginx, Caddy, Fly, Cloudflare, etc.).
- Write endpoints (`POST /api/v1/webhooks/subscribe`,
  `DELETE /api/v1/webhooks/{id}`) are either not exposed publicly or are
  protected by your proxy's auth (basic auth, bearer tokens, IP allowlists).
- The SQLite database at `data/opentao.db` is not reachable from the network.

## What we guard against in-tree

- **SSRF on webhook subscribe.** `POST /webhooks/subscribe` rejects URLs that
  resolve to loopback, private (RFC1918), link-local, multicast, or cloud
  metadata addresses (`metadata.google.internal`, etc.).
- **XSS in the dashboard.** All untrusted values (subnet names, coldkeys,
  hotkeys, error messages) flow through an `esc()` helper before being
  interpolated into `innerHTML`.
- **Integer overflow on thresholds.** `WebhookSubscribeRequest.threshold` is
  bounded to +/-1e15 and URLs are capped at 2048 chars.
- **DB write races.** The shared aiosqlite connection is serialized through
  an `asyncio.Lock` on every write path so the poller and webhook evaluator
  cannot interleave transactions.

## What we *don't* guard against

- **CORS.** `allow_origins=["*"]` with `allow_credentials=False` is the
  default. Any website can read your data. If you expose write endpoints
  publicly, either tighten CORS, add auth, or block writes at your proxy.
- **Rate limiting.** There is no per-IP rate limit. Front the API with a
  proxy if you need one.
- **Webhook target authentication.** Outbound webhooks POST raw JSON. If you
  need signed payloads, add HMAC verification between here and your receiver;
  pull requests welcome.
- **Secrets in webhook URLs.** The `/webhooks` UI lists subscription URLs in
  plaintext. Anyone with access to that page can see Discord webhook tokens.

## Known sharp edges

- The first page load on a fresh install blocks for 10 to 30 seconds while the
  metagraph syncs. `/health` returns `503` with `stale=true` during this
  window so operator monitoring works correctly.
- `bittensor` is pinned `>=9.1.0,<10.0.0`. Major upgrades may introduce
  breaking SDK changes; test before bumping.
