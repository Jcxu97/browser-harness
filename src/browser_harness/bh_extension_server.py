"""
HTTP long-polling bridge between BH Python helpers and the BH companion
Chrome extension.

Architecture:
    BH client → POST /command   → server queues → extension polls /poll
    extension executes via chrome.tabs.* API → POST /result → BH client unblocked

Run as daemon (auto-spawned by bh_extension_client.start_server_if_needed):
    python -m browser_harness.bh_extension_server [port]
"""

import http.server
import hmac
import json
import os
import queue
import threading
import time
import uuid
import sys

from . import paths

DEFAULT_PORT = 9223
POLL_HOLD_SECONDS = 25  # long-poll hold; <30 to avoid proxy/firewall timeouts
COMMAND_TIMEOUT_SECONDS = 30

# Shared secret for this bridge.
#
# TCP loopback has no chmod equivalent, so without a token ANY local process can
# drive chrome.tabs.* and read the URL + title of every open tab. Verified
# 2026-07-30 with one unauthenticated curl: it returned every window, including
# the sub2api admin panel URL. _ipc.py:29 already documents and solves exactly
# this for the CDP socket; this bridge never got the same treatment.
#
# The token lives in a 0600 file under the config dir, which is how Python-side
# callers get it. The extension cannot read files, so it authenticates by
# echoing back the token handed to it in its first /poll response.
TOKEN_PATH = paths.config_dir() / "extension-bridge.token"
_TOKEN = None


def load_or_create_token():
    """Bridge token, created on first use. Returns a hex string."""
    global _TOKEN
    if _TOKEN:
        return _TOKEN
    try:
        _TOKEN = (TOKEN_PATH.read_text(encoding="utf-8").strip() or None)
    except Exception:
        _TOKEN = None
    if not _TOKEN:
        _TOKEN = uuid.uuid4().hex
        try:
            paths.ensure_private_dir(TOKEN_PATH.parent)
            fd = os.open(str(TOKEN_PATH), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            try:
                os.write(fd, _TOKEN.encode("utf-8"))
            finally:
                os.close(fd)
        except Exception:
            pass  # in-memory token still protects this run
    return _TOKEN

CMD_QUEUE = queue.Queue()
PENDING = {}  # id -> {event, result}
PENDING_LOCK = threading.Lock()
LAST_POLL_AT = [0.0]  # mutable closure


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a, **kw):  # silence access logs
        pass

    def _respond(self, code, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        # Deliberately no Access-Control-Allow-Origin. Callers are local Python
        # and the companion extension; neither needs CORS. Sending "*" (as this
        # did until 2026-07-30) let any web page read the response, leaving
        # Chrome's Private Network Access checks as the only barrier — too soft
        # to lean on as a security boundary.
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _token_ok(self, body=None):
        """Header or body token, compared in constant time."""
        got = self.headers.get("X-BH-Token") or (body or {}).get("token") or ""
        return bool(got) and hmac.compare_digest(str(got), load_or_create_token())

    def do_OPTIONS(self):
        # Preflight only mattered for the cross-origin case we no longer allow.
        self._respond(405, {"error": "method not allowed"})

    def do_GET(self):
        if self.path.split("?")[0] == "/status":
            if not self._token_ok():
                self._respond(401, {"error": "missing or bad token"})
                return
            self._respond(200, {
                "pending_commands": CMD_QUEUE.qsize(),
                "pending_results": len(PENDING),
                "last_poll_age_s": time.time() - LAST_POLL_AT[0] if LAST_POLL_AT[0] else None,
                "extension_connected": (time.time() - LAST_POLL_AT[0] < 35) if LAST_POLL_AT[0] else False,
            })
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {}

        if path == "/poll":
            # The extension cannot read files, so it cannot know the token up
            # front. First poll without one gets the token and nothing else;
            # every later poll must echo it back. That keeps a random local
            # process from *receiving queued commands* (which would let it
            # impersonate the extension and answer /result with fabricated data),
            # while still bootstrapping the real extension.
            if not self._token_ok(body):
                self._respond(200, {"commands": [], "token": load_or_create_token()})
                return
            # Extension polls for next command (long poll)
            LAST_POLL_AT[0] = time.time()
            try:
                cmd = CMD_QUEUE.get(timeout=POLL_HOLD_SECONDS)
                self._respond(200, {"commands": [cmd]})
            except queue.Empty:
                self._respond(200, {"commands": []})

        elif path == "/result":
            if not self._token_ok(body):
                self._respond(401, {"error": "missing or bad token"})
                return
            cmd_id = body.get("id")
            with PENDING_LOCK:
                rec = PENDING.get(cmd_id)
                if rec:
                    rec["result"] = body.get("result")
                    rec["event"].set()
            self._respond(200, {"ok": True})

        elif path == "/command":
            if not self._token_ok(body):
                self._respond(401, {"error": "missing or bad token"})
                return
            # BH client submitting command for extension execution
            cmd = dict(body)
            cmd.pop("token", None)  # never forward the secret to the extension
            cmd["id"] = str(uuid.uuid4())
            event = threading.Event()
            with PENDING_LOCK:
                PENDING[cmd["id"]] = {"event": event, "result": None}
            CMD_QUEUE.put(cmd)
            if event.wait(timeout=COMMAND_TIMEOUT_SECONDS):
                with PENDING_LOCK:
                    result = PENDING.pop(cmd["id"]).get("result")
                self._respond(200, {"result": result})
            else:
                with PENDING_LOCK:
                    PENDING.pop(cmd["id"], None)
                self._respond(504, {"error": "extension timeout (no response from companion)"})

        elif path == "/shutdown":
            if not self._token_ok(body):
                self._respond(401, {"error": "missing or bad token"})
                return
            # admin: stop server
            self._respond(200, {"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.2), self.server.shutdown()), daemon=True).start()

        else:
            self._respond(404, {"error": "not found"})


def serve(port=DEFAULT_PORT):
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[bh-ext-server] listening on 127.0.0.1:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    serve(port)
