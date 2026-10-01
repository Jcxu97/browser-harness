"""The extension bridge must not be an open door.

It listens on TCP loopback, which has no chmod equivalent. Before 2026-07-30 any
local process could drive chrome.tabs.* with one unauthenticated curl. The
2026-07-30 fix handed the token to any poller, so the first local process to
poll got it. Protocol 2 never sends the token: both sides read it from a file
and sign each message with HMAC-SHA256.
"""
import json
import secrets
import threading
import time
import urllib.error
import urllib.request

import pytest

from browser_harness import bh_extension_client as client
from browser_harness import bh_extension_server as srv

TOKEN = "ab" * 32


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    """Run the real handler on an ephemeral port with a known token."""
    import http.server

    monkeypatch.setattr(srv, "_TOKEN", TOKEN)
    monkeypatch.setattr(srv, "TOKEN_PATH", tmp_path / "bridge-token.txt")
    monkeypatch.setattr(srv, "POLL_HOLD_SECONDS", 1)
    monkeypatch.setattr(srv, "NONCES", srv._Nonces())
    monkeypatch.setattr(srv, "CMD_QUEUE", srv.queue.Queue())
    monkeypatch.setattr(srv, "PENDING", {})
    monkeypatch.setattr(srv, "LAST_POLL_AT", [0.0])

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    monkeypatch.setattr(client, "PORT", port)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _request(base, method, path, payload=None, headers=None, timeout=5):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), dict(e.headers)


def _client_auth(method, path, raw="", ts=None, nonce=None):
    ts = ts or str(int(time.time() * 1000))
    nonce = nonce or secrets.token_hex(8)
    mac = srv.sign(TOKEN, "client", f"{method} {path}\n{ts}\n{nonce}\n{raw}")
    return {"X-BH-Auth": f"{ts}.{nonce}.{mac}"}


def _poll(base, token=TOKEN, timeout=5):
    ts, nonce = str(int(time.time() * 1000)), secrets.token_hex(8)
    body = {"ts": ts, "nonce": nonce, "mac": srv.sign(token, "poll", f"{ts}\n{nonce}")}
    return _request(base, "POST", "/poll", body, timeout=timeout)


def _fake_extension(base, answer, seen):
    """Poll like background.js: check the command signature, then sign the result."""
    deadline = time.time() + 5
    while time.time() < deadline:
        code, data, _ = _poll(base)
        for item in (data or {}).get("commands", []):
            seen.append(item)
            assert item["sig"] == srv.sign(TOKEN, "cmd", item["payload"])
            cmd = json.loads(item["payload"])
            payload = json.dumps({"id": cmd["id"], "result": answer(cmd)})
            _request(base, "POST", "/result", {"payload": payload, "mac": srv.sign(TOKEN, "result", payload)})
            return


def test_status_requires_auth(bridge):
    code, data, _ = _request(bridge, "GET", "/status")
    assert code == 401 and data.get("protocol") == 2
    code, data, _ = _request(bridge, "GET", "/status", headers=_client_auth("GET", "/status"))
    assert code == 200 and data["protocol"] == 2 and "extension_connected" in data


def test_replayed_request_is_refused(bridge):
    headers = _client_auth("GET", "/status")
    assert _request(bridge, "GET", "/status", headers=headers)[0] == 200
    assert _request(bridge, "GET", "/status", headers=headers)[0] == 401


def test_stale_timestamp_is_refused(bridge):
    old = str(int((time.time() - 600) * 1000))
    code, _, _ = _request(bridge, "GET", "/status", headers=_client_auth("GET", "/status", ts=old))
    assert code == 401


def test_signature_covers_the_body(bridge):
    """A captured signature must not authorize a different command."""
    signed = json.dumps({"action": "ping"})
    headers = _client_auth("POST", "/command", raw=signed)
    code, _, _ = _request(bridge, "POST", "/command", {"action": "create_tab"}, headers=headers)
    assert code == 401


def test_command_and_shutdown_require_auth(bridge):
    assert _request(bridge, "POST", "/command", {"action": "create_tab"})[0] == 401
    assert _request(bridge, "POST", "/shutdown", {})[0] == 401
    code, _, _ = _request(bridge, "GET", "/status", headers=_client_auth("GET", "/status"))
    assert code == 200, "server died despite refusing the shutdown"


def test_poll_without_the_token_gets_nothing(bridge):
    """Old servers gave the token to any poller. Now a wrong mac gets a 401 and no secret."""
    code, data, _ = _request(bridge, "POST", "/poll", {})
    assert code == 401 and TOKEN not in json.dumps(data)
    code, data, _ = _poll(bridge, token="cd" * 32)
    assert code == 401 and TOKEN not in json.dumps(data)


def test_result_requires_a_valid_mac(bridge):
    payload = json.dumps({"id": "x", "result": {"spoofed": True}})
    code, _, _ = _request(bridge, "POST", "/result", {"payload": payload, "mac": "0" * 64})
    assert code == 401


def test_round_trip_through_the_client(bridge):
    seen = []
    worker = threading.Thread(target=_fake_extension, daemon=True,
                              args=(bridge, lambda cmd: {"echo": cmd["action"], "url": cmd["url"]}, seen))
    worker.start()
    result = client.send_command("create_tab", url="https://example.com/", timeout=5)
    worker.join(5)
    assert result == {"echo": "create_tab", "url": "https://example.com/"}
    cmd = json.loads(seen[0]["payload"])
    assert cmd["deadline"] > time.time() * 1000
    assert "timeout" not in cmd
    assert TOKEN not in seen[0]["payload"], "bridge token sent to the extension"


def test_extension_error_raises(bridge):
    threading.Thread(target=_fake_extension, daemon=True,
                     args=(bridge, lambda cmd: {"error": "No window with id: 7."}, [])).start()
    with pytest.raises(RuntimeError, match="No window with id"):
        client.send_command("update_window", windowId=7, timeout=5)


def test_expired_command_is_never_delivered(bridge):
    """A caller that gave up must not have its command run later, for example a
    create_tab that arrives after the caller already used the CDP fallback."""
    with pytest.raises(RuntimeError, match="HTTP 504"):
        client.send_command("create_tab", windowId=5, url="https://example.com/", timeout=0.3)
    code, data, _ = _poll(bridge)
    assert code == 200 and data["commands"] == []


def test_no_cors_and_no_preflight(bridge):
    _, _, headers = _request(bridge, "GET", "/status", headers=_client_auth("GET", "/status"))
    assert "Access-Control-Allow-Origin" not in headers
    code, _, _ = _request(bridge, "OPTIONS", "/command")
    assert code == 405


def test_server_is_up_needs_protocol_2(bridge):
    assert client.server_is_up() is True
    assert client.is_available() is False  # no extension polled yet
    assert _poll(bridge)[0] == 200
    assert client.is_available() is True


def test_token_is_stable_and_long(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "_TOKEN", None)
    monkeypatch.setattr(srv, "TOKEN_PATH", tmp_path / "bridge-token.txt")
    a = srv.load_or_create_token()
    monkeypatch.setattr(srv, "_TOKEN", None)  # force a re-read from disk
    b = srv.load_or_create_token()
    assert a == b and len(a) == 64
    assert (tmp_path / "bridge-token.txt").read_text(encoding="utf-8") == a
    assert not list(tmp_path.glob("*.tmp.*"))


def test_second_server_cannot_share_the_port():
    """On Windows, 3 servers listened on 9223 at once, and each request went to
    a random one."""
    first = srv._Server(("127.0.0.1", 0), srv.Handler)
    try:
        with pytest.raises(OSError):
            srv._Server(("127.0.0.1", first.server_address[1]), srv.Handler)
    finally:
        first.server_close()


# background.js itself, run in Node with chrome.* stubbed. Skipped without Node.

_BACKGROUND_JS = __import__("pathlib").Path(__file__).resolve().parents[2] / "extension" / "background.js"

_HARNESS = r"""
import { pathToFileURL } from "node:url";
const [port, token, bg] = process.argv.slice(2);
const log = (x) => console.log(JSON.stringify(x));
globalThis.chrome = {
  runtime: { getURL: (p) => `chrome-extension://bh/${p}`, getManifest: () => ({ version: "test" }),
             getPlatformInfo: async () => ({ os: "test" }),
             onInstalled: { addListener() {} }, onStartup: { addListener() {} }, reload() {} },
  alarms: { create() {}, onAlarm: { addListener() {} } },
  tabs: { create: async (o) => { log(["create_tab", o]); return { id: 11, windowId: o.windowId }; } },
  windows: {
    create: async (o) => { log(["create_window", o]); return { id: 22, state: o.state }; },
    update: async (id, o) => { log(["update_window", id, o]); return { id, state: o.state }; },
  },
};
const realFetch = globalThis.fetch;
globalThis.fetch = async (url, opts) => url.startsWith("chrome-extension://")
  ? new Response(token)
  : realFetch(url.replace("127.0.0.1:9223", `127.0.0.1:${port}`), opts);
await import(pathToFileURL(bg).href);
"""


def _node():
    import shutil
    return shutil.which("node")


def _run_background_js(tmp_path, port, token):
    import subprocess
    harness = tmp_path / "harness.mjs"
    harness.write_text(_HARNESS, encoding="utf-8")
    return subprocess.Popen([_node(), str(harness), str(port), token, str(_BACKGROUND_JS)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _chrome_calls(proc):
    proc.terminate()
    out, _ = proc.communicate(timeout=5)
    return [json.loads(line) for line in out.splitlines() if line.startswith("[")]


@pytest.mark.skipif(not _node(), reason="node not installed")
def test_background_js_round_trip(bridge, tmp_path):
    proc = _run_background_js(tmp_path, client.PORT, TOKEN)
    try:
        assert client.send_command("create_tab", windowId=5, url="https://example.com/", timeout=8) == \
            {"tabId": 11, "windowId": 5}
        assert client.send_command("ping", timeout=5)["pong"] is True
        for action in ("create_window", "list_windows"):
            with pytest.raises(RuntimeError, match="unknown action"):
                client.send_command(action, timeout=5)
    finally:
        calls = _chrome_calls(proc)
    assert calls == [["create_tab", {"url": "https://example.com/", "windowId": 5, "active": False}]]


@pytest.mark.skipif(not _node(), reason="node not installed")
def test_background_js_ignores_forged_and_late_commands(tmp_path):
    """A fake server on the port must not get the extension to act, and a
    command past its deadline must not run."""
    import http.server

    now_ms = int(time.time() * 1000)
    forged = json.dumps({"id": "f", "action": "create_tab", "url": "https://forged/", "deadline": now_ms + 60000})
    late = json.dumps({"id": "l", "action": "create_tab", "url": "https://late/", "deadline": now_ms - 1})
    good = json.dumps({"id": "g", "action": "create_tab", "url": "https://good/", "deadline": now_ms + 60000})
    replies = [[{"payload": forged, "sig": srv.sign("ef" * 32, "cmd", forged)}],
               [{"payload": late, "sig": srv.sign(TOKEN, "cmd", late)}],
               [{"payload": good, "sig": srv.sign(TOKEN, "cmd", good)}]]
    results = []

    class Fake(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            if self.path == "/result":
                results.append(json.loads(raw)["payload"])
                body = b"{}"
            else:
                time.sleep(0.1)
                body = json.dumps({"commands": replies.pop(0) if replies else []}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    proc = _run_background_js(tmp_path, httpd.server_address[1], TOKEN)
    try:
        deadline = time.time() + 8
        while time.time() < deadline and not results:
            time.sleep(0.1)
    finally:
        calls = _chrome_calls(proc)
        httpd.shutdown()
        httpd.server_close()
    assert [c[0] for c in calls] == ["create_tab"], calls
    assert [json.loads(r)["id"] for r in results] == ["g"]
