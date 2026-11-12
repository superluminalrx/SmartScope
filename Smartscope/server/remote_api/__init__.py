"""Remote access to the web app and API over iroh.

A listener process (`smartscope.py remote_api`, started beside the web server
when REMOTE_API_ENABLED=true) accepts connections from devices that staff have
paired and forwards their HTTP requests to the web app. A paired device is only
a way in: its requests authenticate as a user exactly as on the local network.

Protocol, by ALPN:

smartscope/pair/1
    Open to any endpoint. The device opens one stream, sends
    `{"code": <pairing code>, "name": <device name>}` and finishes the stream.
    The reply is `{"ok": true, "device": ...}` or `{"ok": false, "error": ...}`.
    A code works once.

smartscope/http/1
    Paired devices only; others are closed with code 403. Each stream carries
    one HTTP/1.1 request and its response, with whatever credentials the
    device sends. WebSocket upgrades are passed through.
"""
