# Remote API

Lets devices outside the network use the SmartScope web app and REST API over
[iroh](https://iroh.computer). A device is paired once with a user account and
from then on acts as that user. Nothing is exposed until `REMOTE_API_ENABLED=true`.

## Server

| Setting | Default | Meaning |
|---|---|---|
| `REMOTE_API_ENABLED` | `false` | Turns on the listener and the *Remote devices* page. |
| `REMOTE_API_RELAYS` | empty | Empty uses the public iroh relays, `disabled` uses none, otherwise a comma-separated list of relay URLs. |
| `REMOTE_API_BIND` | empty | Fixed `host:port` for direct connections. |
| `REMOTE_API_UPSTREAM` | `nginx:80` | Where requests are forwarded. |
| `REMOTE_API_KEY_FILE` | `/opt/auth/remote_api_key` | Secret key of the listener. Devices address the listener by it, so keep it. |

The listener runs as its own process, `smartscope.py remote_api`, the
`smartscope_remote` service of the compose file.

Users pair and remove their devices under *Remote devices* in the user menu.
Staff can see and remove every device there or in the admin site.

## Protocol

A device is an iroh endpoint with its own secret key. The key never leaves the
device; SmartScope stores the public half.

### Pairing, ALPN `smartscope/pair/1`

The page gives the user a link, also shown as a QR code:

    smartscope://pair?ticket=<endpoint ticket>&code=<one-time code>

1. Connect to the address in the ticket.
2. Open a bidirectional stream, send `{"code": "<code>", "name": "<device name>"}`, finish the stream.
3. Read the reply until the stream ends: `{"ok": true, "user": "...", "device": "..."}`
   or `{"ok": false, "error": "..."}`.

A code works once and expires after 10 minutes. Keep the ticket for later connections.

### Requests, ALPN `smartscope/http/1`

Only paired devices are accepted. Others are closed with code 403.

- Each bidirectional stream carries one HTTP/1.1 request and its response.
  Write the request, then read the response until the stream ends.
- Send no credentials. The listener sets `Authorization` to the API token of the
  device's owner and discards one sent by the device.
- The request head must use CRLF line endings and fit in 64 kB. A malformed
  head is answered with `400`, an unreachable web app with `502`.
- Up to 64 streams may be open at once on a connection.
- Removing a device or deactivating its owner closes its connections with code
  403 within about 10 seconds.
- WebSocket upgrades are passed through. SmartScope checks their `Origin`
  against its allowed hosts as it does for a browser.

`client.py` is a reference client: it pairs, then opens a local port whose
connections are each forwarded on one stream, so any HTTP client can be used.
