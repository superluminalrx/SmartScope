# Remote API

Lets devices outside the network use the SmartScope web app and REST API over
[iroh](https://iroh.computer). Staff pair a device once. Pairing only opens the
connection: every request through it still authenticates as a user, with that
user's own API token or session, exactly as on the local network. Nothing is
exposed until `REMOTE_API_ENABLED=true`.

## Server

| Setting | Default | Meaning |
|---|---|---|
| `REMOTE_API_ENABLED` | `false` | Turns on the listener and the *Remote devices* page. |
| `REMOTE_API_RELAYS` | empty | Empty uses the public iroh relays, `disabled` uses none, otherwise a comma-separated list of relay URLs. |
| `REMOTE_API_BIND` | empty | Fixed `host:port` for direct connections. |
| `REMOTE_API_UPSTREAM` | `nginx:80` | Where requests are forwarded. |
| `REMOTE_API_KEY_FILE` | `/opt/auth/remote_api_key` | Secret key of the listener. Devices address the listener by it, so keep it. |

The listener is a separate process, `smartscope.py remote_api`. The entrypoint
of the `smartscope` container starts it beside the web server when the setting
is on and restarts it if it exits, so enabling it needs no change to the compose
file.

Staff pair and remove devices under *Remote devices* in the user menu, or in the
admin site. A device can be one person's laptop or a service that many users
reach SmartScope through.

## Protocol

A device is an iroh endpoint with its own secret key. The key never leaves the
device; SmartScope stores the public half.

### Pairing, ALPN `smartscope/pair/1`

The page gives a staff user a link, also shown as a QR code:

    smartscope://pair?ticket=<endpoint ticket>&code=<one-time code>

1. Connect to the address in the ticket.
2. Open a bidirectional stream, send `{"code": "<code>", "name": "<device name>"}`, finish the stream.
3. Read the reply until the stream ends: `{"ok": true, "device": "..."}`
   or `{"ok": false, "error": "..."}`.

A code works once and expires after 10 minutes. Keep the ticket for later connections.

### Requests, ALPN `smartscope/http/1`

Only paired devices are accepted. Others are closed with code 403.

- Each bidirectional stream carries one HTTP/1.1 request and its response.
  Write the request, then read the response until the stream ends.
- Authenticate as on the local network: `Authorization: Token <API token>`, or a
  session cookie. A user gets their token from `POST /api/api-token-auth/` with
  `username` and `password`. A request without credentials is answered as the
  web app answers any anonymous request.
- The listener changes two headers only. `Connection` is set so that the
  request is the only one on its stream, and `Host` is replaced when it is not
  a name the web app accepts.
- The request head must use CRLF line endings and fit in 64 kB. A malformed
  head is answered with `400`, an unreachable web app with `502`.
- Up to 64 streams may be open at once on a connection.
- Removing a device closes its connections with code 403 within about 10 seconds.
- WebSocket upgrades are passed through. SmartScope checks their `Origin`
  against its allowed hosts as it does for a browser.

`client.py` is a reference client: it pairs, then opens a local port whose
connections are each forwarded on one stream, so any HTTP client can be used.
