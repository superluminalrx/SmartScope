# Remote API

Lets devices outside the network use the SmartScope web app and REST API over
[iroh](https://iroh.computer). Staff register the key of a device once. The key
only opens the connection: every request through it still authenticates as a
user, with that user's own API token or session, exactly as on the local
network. Nothing is exposed until `REMOTE_API_ENABLED=true`.

## Server

| Setting | Default | Meaning |
|---|---|---|
| `REMOTE_API_ENABLED` | `false` | Turns on the listener. |
| `REMOTE_API_RELAYS` | empty | Empty uses the public iroh relays, `disabled` uses none, otherwise a comma-separated list of relay URLs. |
| `REMOTE_API_BIND` | empty | Fixed `host:port` for direct connections. |
| `REMOTE_API_UPSTREAM` | `nginx:80` | Where requests are forwarded. |
| `REMOTE_API_KEY_FILE` | `/opt/auth/remote_api_key` | Secret key of the listener. Devices address the listener by its public half, so keep it. |

The listener is a separate process, `smartscope.py remote_api`. The entrypoint
of the `smartscope` container starts it beside the web server when the setting
is on and restarts it if it exits, so enabling it needs no change to the compose
file.

## Pairing

Pairing is an exchange of public keys, done by staff in the admin site under
*Remote devices*:

1. The device shows its key, 64 hexadecimal characters. Add a remote device with
   a name and that key.
2. The same form shows the *SmartScope key*. Enter it in the device.

The form also shows the *SmartScope ticket*, which holds the key together with
the relay and addresses the listener is reachable at. A device that cannot look
up a key through iroh discovery, for instance when the listener uses its own
relays, takes the ticket instead.

Deleting the entry ends the device's access. A device can be one person's
laptop or a service that many users reach SmartScope through.

## Protocol

A device is an iroh endpoint with its own secret key, which never leaves it. It
connects to the SmartScope key with ALPN `smartscope/http/1`. A device whose
key is not registered is closed with code 403.

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
- Deleting a device closes its connections with code 403 within about 10 seconds.
- WebSocket upgrades are passed through. SmartScope checks their `Origin`
  against its allowed hosts as it does for a browser.

`client.py` is a reference client: it prints its key, and opens a local port
whose connections are each forwarded on one stream, so any HTTP client can be
used.
