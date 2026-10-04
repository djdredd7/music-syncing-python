#!/usr/bin/env python3
"""One-time helper: log in to Spotify and print a refresh token for sync.py.

1. Create an app at https://developer.spotify.com/dashboard
2. Add this Redirect URI to it exactly:  http://127.0.0.1:8888/callback
3. Run:  SPOTIFY_CLIENT_ID=xxx SPOTIFY_CLIENT_SECRET=yyy python setup_spotify.py
"""
import os
import secrets
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

CID, SECRET = os.environ["SPOTIFY_CLIENT_ID"], os.environ["SPOTIFY_CLIENT_SECRET"]
REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = "playlist-modify-private playlist-modify-public playlist-read-private"
STATE = secrets.token_urlsafe(16)
result = {}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if q.get("state", [""])[0] == STATE and "code" in q:
            result["code"] = q["code"][0]
            msg = "Done! You can close this tab and go back to the terminal."
        else:
            msg = "Something went wrong -- check the terminal."
        self.send_response(200)
        self.end_headers()
        self.wfile.write(msg.encode())

    def log_message(self, *a):
        pass


url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
    "client_id": CID, "response_type": "code", "redirect_uri": REDIRECT,
    "scope": SCOPES, "state": STATE})
print("Opening browser to log in to Spotify...\nIf it doesn't open, visit:\n" + url)
webbrowser.open(url)
srv = HTTPServer(("127.0.0.1", 8888), Handler)
while "code" not in result:
    srv.handle_request()

r = requests.post("https://accounts.spotify.com/api/token", auth=(CID, SECRET), data={
    "grant_type": "authorization_code", "code": result["code"], "redirect_uri": REDIRECT})
r.raise_for_status()
print("\nYour SPOTIFY_REFRESH_TOKEN is:\n\n" + r.json()["refresh_token"] + "\n")
