# 6. Browser notifications (Web Push)

Produces: a VAPID key pair, so the jobs can send browser notifications to people who chose "A notification in this browser" (account D-12, UC-REG-15).

Optional. Without it, the browser choice is hidden at setup and email still goes.

Code: [`api/cairn_api/webpush.py`](../../api/cairn_api/webpush.py).

## How it works

- When someone says yes, the browser subscribes with Cairn's public key and gives the client an endpoint at its vendor's push service (Google, Mozilla, Apple, or Microsoft). The API stores that endpoint in `notification_preferences`.
- The `outbound` job sends an **empty** push, signed with the private key (VAPID, RFC 8292). The push carries no text at all, so nothing about the person, the person who died, or a task passes through the vendor's push service. The service worker shows a fixed, private line from Cairn's own copy when it wakes.
- Because there's no payload, there's nothing to encrypt for the push service (RFC 8291 applies only to payloads).
- A 404 or 410 from the push service means permission was revoked. The job removes browser from that person's channels and clears the endpoint.

## 6.1 Make the key pair

```bash
openssl ecparam -name prime256v1 -genkey -noout | openssl pkcs8 -topk8 -nocrypt > vapid-private.pem
```

Get the public key in the format browsers need (base64url, uncompressed point), with the repository's own helper:

```bash
.venv/bin/python -c "import sys; sys.path.insert(0, 'api'); from cairn_api.webpush import public_key_from_pem; print(public_key_from_pem(open('vapid-private.pem').read()))"
```

Move `vapid-private.pem` into your secret manager and delete the local copy after step 7.

## 6.2 Where each half goes

| Value | Kind | Goes to |
|---|---|---|
| `CAIRN_VAPID_PUBLIC_KEY` | Not secret | `vars` in `wrangler.jsonc`. The API gives it to the browser |
| `CAIRN_VAPID_PRIVATE_KEY` | Secret | `npx wrangler secret put CAIRN_VAPID_PRIVATE_KEY < vapid-private.pem`. Jobs container only |
| `CAIRN_VAPID_SUBJECT` | Not secret | A `mailto:` or `https:` contact the push services can reach. Set it in `vars` or as a secret |

## Rotating the key

Every browser subscription is tied to the public key it was made with. A new key pair means existing subscriptions stop working, and people have to turn browser notifications on again. Rotate only if the private key is exposed. See [Secrets](../security/secrets.md#rotation).

Next: [7. Cloudflare](07-cloudflare.md).
