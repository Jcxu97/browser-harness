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
import json
import queue
import threading
import time
import uuid
import sys

DEFAULT_PORT = 9223
POLL_HOLD_SECONDS = 25  # long-poll hold; <30 to avoid proxy/firewall timeouts
COMMAND_TIMEOUT_SECONDS = 30

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
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._respond(204, {})

    def do_GET(self):
        if self.path.split("?")[0] == "/status":
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
            # Extension polls for next command (long poll)
            LAST_POLL_AT[0] = time.time()
            try:
                cmd = CMD_QUEUE.get(timeout=POLL_HOLD_SECONDS)
                self._respond(200, {"commands": [cmd]})
            except queue.Empty:
                self._respond(200, {"commands": []})

        elif path == "/result":
            cmd_id = body.get("id")
            with PENDING_LOCK:
                rec = PENDING.get(cmd_id)
                if rec:
                    rec["result"] = body.get("result")
                    rec["event"].set()
            self._respond(200, {"ok": True})

        elif path == "/command":
            # BH client submitting command for extension execution
            cmd = dict(body)
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
