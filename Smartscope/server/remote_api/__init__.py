"""Remote access to the web app and API over iroh.

A listener process (`smartscope.py remote_api`, started beside the web server
when REMOTE_API_ENABLED=true) accepts connections from devices whose key staff
have registered in the admin site, and forwards their HTTP requests to the web
app. A registered device is only a way in: its requests authenticate as a user
exactly as on the local network.

Devices connect with ALPN `smartscope/http/1`; others are closed with code 403.
Each stream carries one HTTP/1.1 request and its response, with whatever
credentials the device sends. WebSocket upgrades are passed through.
"""
