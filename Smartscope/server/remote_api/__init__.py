"""Remote access to the web app and API over iroh.

A listener process (`smartscope.py remote_api`) accepts connections from devices
that users have paired with their account and forwards their HTTP requests to
the web app, authenticated as the device's owner.

Protocol, by ALPN:

smartscope/pair/1
    Open to any endpoint. The device opens one stream, sends
    `{"code": <pairing code>, "name": <device name>}` and finishes the stream.
    The reply is `{"ok": true, "user": ..., "device": ...}` or
    `{"ok": false, "error": ...}`. A code works once.

smartscope/http/1
    Paired devices only; others are closed with code 403. Each stream carries
    one HTTP/1.1 request and its response. The listener sets the Authorization
    header to the owner's API token, so the device never holds a credential
    other than its own key. WebSocket upgrades are passed through.
"""
