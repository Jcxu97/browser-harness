"""
HTTP long-polling bridge between BH Python helpers and the BH companion
Chrome extension.

    BH client -> POST /command -> queue -> extension polls /poll
    extension runs chrome.windows/tabs -> POST /result -> BH client gets the answer

Auth: one shared token in <extension dir>/bridge-token.txt. The extension
reads it with fetch(chrome.runtime.getURL(...)); Python reads the file. The
token never goes over the wire. Each request carries an HMAC-SHA256 of its
content, and each command to the extension carries an HMAC too, so the
extension ignores a fake server on this port.

Run as daemon (auto-spawned by bh_extension_client.start_server_if_needed):
    python -m browser_harness.bh_extension_server [port]
"""

import collections
import hashlib
import hmac
import http.server
import json
import os
import pathlib
import queue
import secrets
import socket
import sys
import threading
import time
import uuid

DEFAULT_PORT = 9223
PROTOCOL = 2
POLL_HOLD_SECONDS = 25  # under 30 to stay below proxy and firewall timeouts
MAX_COMMAND_SECONDS = 30
MAX_SKEW_SECONDS = 120


def extension_dir():
    env = os.environ.get("BH_EXTENSION_DIR")
    if env:
        return pathlib.Path(env)
    return pathlib.Path(__file__).resolve().parents[2] / "extension"


TOKEN_PATH = extension_dir() / "bridge-token.txt"
_TOKEN = None


def load_or_create_token():
    global _TOKEN
    if _TOKEN:
        return _TOKEN
    try:
        _TOKEN = TOKEN_PATH.read_text(encoding="utf-8").strip() or None
    except Exception:
        _TOKEN = None
    if not _TOKEN:
        _TOKEN = secrets.token_hex(32)
        tmp = TOKEN_PATH.with_suffix(f".tmp.{os.getpid()}")
        try:
            tmp.write_text(_TOKEN, encoding="utf-8")
            os.replace(str(tmp), str(TOKEN_PATH))
        except Exception:
            pass  # the in-memory token still protects this run
    return _TOKEN


def sign(token, kind, payload):
    return hmac.new(token.encode("utf-8"), f"{kind}\n{payload}".encode("utf-8"),
                    hashlib.sha256).hexdigest()


class _Nonces:
    """Recently seen request nonces, so a captured request cannot be replayed."""

    def __init__(self):
        self._seen = collections.OrderedDict()
        self._lock = threading.Lock()

    def fresh(self, nonce):
        now = time.time()
        with self._lock:
            while self._seen and next(iter(self._seen.values())) < now - MAX_SKEW_SECONDS:
                self._seen.popitem(last=False)
            if not nonce or nonce in self._seen:
                return False
            self._seen[nonce] = now
            return True


NONCES = _Nonces()
CMD_QUEUE = queue.Queue()
PENDING = {}  # id -> {"event", "result", "deadline"}
PENDING_LOCK = threading.Lock()
LAST_POLL_AT = [0.0]


def _fresh(ts_ms, nonce):
    try:
        ok_time = abs(time.time() - int(ts_ms) / 1000.0) < MAX_SKEW_SECONDS
    except (TypeError, ValueError):
        return False
    return ok_time and NONCES.fresh(str(nonce))


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a, **kw):
        pass

    def _respond(self, code, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        # No Access-Control-Allow-Origin: web pages must not read these answers.
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _deny(self):
        self._respond(401, {"error": "bad auth", "protocol": PROTOCOL})

    def _client_ok(self, raw):
        """X-BH-Auth: <ts_ms>.<nonce>.<hmac over method, path, ts, nonce, body>."""
        try:
            ts, nonce, mac = (self.headers.get("X-BH-Auth") or "").split(".", 2)
        except ValueError:
            return False
        want = sign(load_or_create_token(), "client",
                    f"{self.command} {self.path}\n{ts}\n{nonce}\n{raw}")
        return hmac.compare_digest(mac, want) and _fresh(ts, nonce)

    def do_OPTIONS(self):
        self._respond(405, {"error": "method not allowed"})

    def do_GET(self):
        if self.path.split("?")[0] != "/status":
            self._respond(404, {"error": "not found"})
            return
        if not self._client_ok(""):
            self._deny()
            return
        age = time.time() - LAST_POLL_AT[0] if LAST_POLL_AT[0] else None
        self._respond(200, {
            "protocol": PROTOCOL,
            "pending_commands": CMD_QUEUE.qsize(),
            "last_poll_age_s": age,
            "extension_connected": age is not None and age < 35,
        })

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {}
        if path == "/poll":
            self._poll(body)
        elif path == "/result":
            self._result(body)
        elif path == "/command":
            if self._client_ok(raw):
                self._command(body)
            else:
                self._deny()
        elif path == "/shutdown":
            if not self._client_ok(raw):
                self._deny()
                return
            self._respond(200, {"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.2), self.server.shutdown()), daemon=True).start()
        else:
            self._respond(404, {"error": "not found"})

    def _poll(self, body):
        token = load_or_create_token()
        ts, nonce = body.get("ts"), body.get("nonce")
        mac = str(body.get("mac") or "")
        if not (hmac.compare_digest(mac, sign(token, "poll", f"{ts}\n{nonce}")) and _fresh(ts, nonce)):
            self._deny()
            return
        LAST_POLL_AT[0] = time.time()
        hold_until = time.time() + POLL_HOLD_SECONDS
        while True:
            try:
                cmd = CMD_QUEUE.get(timeout=max(0.05, hold_until - time.time()))
            except queue.Empty:
                self._respond(200, {"commands": []})
                return
            with PENDING_LOCK:
                live = cmd["id"] in PENDING
            # A command whose caller already gave up must never run late.
            if live and cmd["deadline"] > time.time() * 1000:
                payload = json.dumps(cmd)
                self._respond(200, {"commands": [{"payload": payload, "sig": sign(token, "cmd", payload)}]})
                return

    def _result(self, body):
        payload = str(body.get("payload") or "")
        if not hmac.compare_digest(str(body.get("mac") or ""), sign(load_or_create_token(), "result", payload)):
            self._deny()
            return
        try:
            data = json.loads(payload)
        except Exception:
            self._respond(400, {"error": "bad payload"})
            return
        with PENDING_LOCK:
            rec = PENDING.get(data.get("id"))
            if rec:
                rec["result"] = data.get("result")
                rec["event"].set()
        self._respond(200, {"ok": True})

    def _command(self, body):
        try:
            timeout = min(float(body.pop("timeout", 10)), MAX_COMMAND_SECONDS)
        except (TypeError, ValueError):
            timeout = 10.0
        cmd = dict(body)
        cmd["id"] = str(uuid.uuid4())
        cmd["deadline"] = int((time.time() + timeout) * 1000)
        event = threading.Event()
        with PENDING_LOCK:
            PENDING[cmd["id"]] = {"event": event, "result": None}
        CMD_QUEUE.put(cmd)
        done = event.wait(timeout=timeout)
        with PENDING_LOCK:
            rec = PENDING.pop(cmd["id"], None)
        if done and rec is not None:
            self._respond(200, {"result": rec["result"]})
        else:
            self._respond(504, {"error": "extension did not answer before the deadline"})


class _Server(http.server.ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR lets a second server listen on a port in use, and
    # each connection then goes to one of them (seen 2026-10-01: 3 servers on
    # 9223 with 2 tokens). The extension polled one, BH asked another.
    if sys.platform == "win32":
        allow_reuse_address = False

        def server_bind(self):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()


def serve(port=DEFAULT_PORT):
    load_or_create_token()
    httpd = _Server(("127.0.0.1", port), Handler)
    print(f"[bh-ext-server] listening on 127.0.0.1:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT)
