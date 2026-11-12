#!/usr/bin/env python3
"""Local server for the Board & Chassis Traceability dashboard.

Serves the files in this folder and relays Webex messages, because Webex's API
rejects direct calls from browser pages (no CORS on its preflight responses).

    python3 webex_relay.py            # then open http://127.0.0.1:8765/

POST /relay/webex/messages is forwarded to https://webexapis.com/v1/messages.
POST /relay/webex/file takes {toPersonEmail, markdown, type, name, file} where
type is png, jpg, pdf or html and file is base64, and posts it to the same API as a
file attachment. POST /relay/webex/snapshot is the older PNG-only form
({toPersonEmail, markdown, image}).

Temporary space redirect: start with WEBEX_SPACE set to a space title to send
every message to that space instead of the person, for example

    WEBEX_SPACE=SEMI_TRACE python3 webex_relay.py

The bot must be a member of the space. The intended recipient is noted at the top
of each message. GET /relay/webex/config tells the dashboard which space is active.
The bot token comes from the dashboard's Notify dialog in the Authorization
header. It is never stored or logged here and is sent only to webexapis.com.
"""
import base64
import binascii
import http.server
import json
import os
import re
import secrets
import ssl
import time
import urllib.error
import urllib.request

HOST, PORT = "127.0.0.1", int(os.environ.get("PORT", "8765"))
UPSTREAM = "https://webexapis.com/v1/messages"
ROOMS = "https://webexapis.com/v1/rooms?type=group&max=1000"
SPACE = os.environ.get("WEBEX_SPACE", "").strip()
MAX_BODY = 64 * 1024
MAX_SNAP_BODY = 12 * 1024 * 1024
MAX_IMAGE = 8 * 1024 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
FILE_TYPES = {
    "png": ("image/png", lambda b: b.startswith(PNG_MAGIC)),
    "jpg": ("image/jpeg", lambda b: b.startswith(b"\xff\xd8\xff")),
    "pdf": ("application/pdf", lambda b: b.startswith(b"%PDF-")),
    "html": ("text/html; charset=utf-8", lambda b: b[:15].lower() == b"<!doctype html>"),
}
TLS = ssl.create_default_context()
ALLOWED_ORIGINS = {f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=os.path.dirname(os.path.abspath(__file__)), **kw)

    def _reply(self, code, payload, extra=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/relay/webex/config":
            return self._reply(200, {"space": SPACE or None})
        return super().do_GET()

    @staticmethod
    def _space_id(auth):
        """Room ID of the WEBEX_SPACE space the bot belongs to, or an (code, message) error."""
        req = urllib.request.Request(ROOMS, headers={"Authorization": auth})
        try:
            with urllib.request.urlopen(req, timeout=20, context=TLS) as r:
                items = json.loads(r.read()).get("items", [])
        except urllib.error.HTTPError as e:
            return None, (e.code, "Could not list the bot's spaces")
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            return None, (502, f"Could not reach webexapis.com: {getattr(e, 'reason', e)}")
        hits = [i for i in items if i.get("title") == SPACE] or \
               [i for i in items if str(i.get("title", "")).casefold() == SPACE.casefold()]
        if not hits:
            return None, (404, f"The bot is not a member of a space named {SPACE}. Add the bot to the space in Webex.")
        return hits[0]["id"], None

    def do_POST(self):
        routes = {"/relay/webex/messages": MAX_BODY, "/relay/webex/snapshot": MAX_SNAP_BODY,
                  "/relay/webex/file": MAX_SNAP_BODY}
        if self.path not in routes:
            return self._reply(404, {"message": "Not found"})
        origin = self.headers.get("Origin")
        if origin and origin not in ALLOWED_ORIGINS:
            return self._reply(403, {"message": "Origin not allowed"})
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or len(auth) < 30:
            return self._reply(401, {"message": "Missing bot access token"})
        if "application/json" not in self.headers.get("Content-Type", ""):
            return self._reply(415, {"message": "Expected application/json"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > routes[self.path]:
            return self._reply(413, {"message": "Message too large or empty"})
        raw = self.rfile.read(n)
        try:
            msg = json.loads(raw)
        except ValueError:
            return self._reply(400, {"message": "Invalid JSON"})
        if not isinstance(msg, dict):
            return self._reply(400, {"message": "Invalid JSON"})
        out = {k: msg[k] for k in ("toPersonEmail", "markdown", "text") if isinstance(msg.get(k), str)}
        if not 3 <= len(out.get("toPersonEmail", "")) <= 320:
            return self._reply(400, {"message": "toPersonEmail is required"})
        extra = {}
        if SPACE:
            room, err = self._space_id(auth)
            if err:
                return self._reply(err[0], {"message": err[1]})
            who = out.pop("toPersonEmail")
            out["roomId"] = room
            note = f"Intended for {who}, redirected to {SPACE}"
            if "markdown" in out:
                out["markdown"] = f"*{note}*\n\n{out['markdown']}"
            if "text" in out:
                out["text"] = f"{note}\n\n{out['text']}"
            extra["X-Relay-Space"] = SPACE
            self.log_message("redirecting to space %s", SPACE)
        if self.path != "/relay/webex/messages":
            snap = self.path == "/relay/webex/snapshot"
            kind = "png" if snap else msg.get("type")
            if kind not in FILE_TYPES:
                return self._reply(400, {"message": "type must be png, jpg, pdf or html"})
            enc = msg.get("image" if snap else "file")
            try:
                blob = base64.b64decode(enc, validate=True) if isinstance(enc, str) else b""
            except (binascii.Error, ValueError):
                blob = b""
            mime, valid = FILE_TYPES[kind]
            if not valid(blob):
                return self._reply(400, {"message": f"file is not a valid {kind.upper()}"})
            if len(blob) > MAX_IMAGE:
                return self._reply(413, {"message": "File is larger than 8 MB"})
            data, ctype = self._multipart(out, blob, kind, mime, msg.get("name"))
            self.log_message("forwarding %s attachment, %d bytes", kind, len(blob))
        else:
            data, ctype = json.dumps(out).encode(), "application/json"
        req = urllib.request.Request(UPSTREAM, data=data, method="POST",
                                     headers={"Authorization": auth, "Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=20, context=TLS) as r:
                body = r.read()
                try:
                    self.log_message("Webex message id %s", json.loads(body).get("id"))
                except (ValueError, AttributeError):
                    pass
                return self._reply(r.status, body, extra)
        except urllib.error.HTTPError as e:
            return self._reply(e.code, e.read() or b"{}")
        except (urllib.error.URLError, TimeoutError) as e:
            return self._reply(502, {"message": f"Could not reach webexapis.com: {getattr(e, 'reason', e)}"})

    @staticmethod
    def _multipart(fields, blob, kind, mime, hint):
        boundary = "dash" + secrets.token_hex(16)
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_", hint.rsplit(".", 1)[0] if isinstance(hint, str) else "").strip("_-")[:80]
        name = f"{stem or time.strftime('dashboard-%Y%m%d-%H%M%S')}.{kind}"
        parts = []
        for k, v in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
                         + v.encode() + b"\r\n")
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n'
                     f"Content-Type: {mime}\r\n\r\n".encode() + blob + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        return b"".join(parts), f"multipart/form-data; boundary={boundary}"


if __name__ == "__main__":
    print(f"Serving dashboard and Webex relay on http://{HOST}:{PORT}/")
    if SPACE:
        print(f"All Webex messages are redirected to the space {SPACE}")
    http.server.ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
