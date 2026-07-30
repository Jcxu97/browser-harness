"""The extension bridge must not be an open door.

It listens on TCP loopback, which has no chmod equivalent, so before 2026-07-30
any local process could read the URL and title of every open tab and drive
chrome.tabs.* at will — confirmed with a single unauthenticated curl. _ipc.py
had solved this for the CDP socket long before; this bridge was simply missed.
"""
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from browser_harness import bh_extension_server as srv


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    """Run the real handler on an ephemeral port with a known token."""
    import http.server

    token = "deadbeef" * 4
    monkeypatch.setattr(srv, "_TOKEN", token)
    monkeypatch.setattr(srv, "TOKEN_PATH", tmp_path / "tok")

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base, token
    finally:
        httpd.shutdown()
        httpd.server_close()


def _post(base, path, payload=None, token=None, timeout=5):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json",
                 **({"X-BH-Token": token} if token else {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), dict(e.headers)


def _get(base, path, token=None, timeout=5):
    req = urllib.request.Request(
        base + path, headers={**({"X-BH-Token": token} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), dict(e.headers)


def test_status_requires_token(bridge):
    base, token = bridge
    code, _, _ = _get(base, "/status")
    assert code == 401, "unauthenticated /status leaked bridge state"
    code, data, _ = _get(base, "/status", token=token)
    assert code == 200 and "extension_connected" in data


def test_command_requires_token(bridge):
    """The dangerous one: /command drives chrome.tabs.*."""
    base, _ = bridge
    code, _, _ = _post(base, "/command", {"action": "list_windows"})
    assert code == 401, "unauthenticated process could drive the browser"


def test_shutdown_requires_token(bridge):
    """Otherwise anyone can permanently downgrade us to the CDP fallback."""
    base, token = bridge
    code, _, _ = _post(base, "/shutdown")
    assert code == 401
    code, _, _ = _get(base, "/status", token=token)
    assert code == 200, "server died despite rejecting the shutdown"


def test_result_requires_token(bridge):
    """Without this a local process could impersonate the extension and feed
    fabricated results to a waiting caller."""
    base, _ = bridge
    code, _, _ = _post(base, "/result", {"id": "x", "result": {"spoofed": True}})
    assert code == 401


def test_poll_hands_out_token_then_demands_it(bridge):
    """The extension can't read files, so it bootstraps over the wire.

    An unauthenticated poll must return the token and NO commands — handing over
    queued commands would let an impostor act as the extension.
    """
    base, token = bridge
    code, data, _ = _post(base, "/poll", {})
    assert code == 200
    assert data.get("token") == token
    assert data.get("commands") == [], \
        "queued commands leaked to an unauthenticated poller"


def test_no_cors_wildcard(bridge):
    base, token = bridge
    _, _, headers = _get(base, "/status", token=token)
    assert "Access-Control-Allow-Origin" not in headers, \
        "wildcard CORS lets web pages read bridge responses"


def test_options_preflight_is_refused(bridge):
    base, _ = bridge
    req = urllib.request.Request(base + "/command", method="OPTIONS")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    assert code == 405, "successful preflight re-enables cross-origin access"


def test_token_is_not_forwarded_to_the_extension(bridge):
    """A queued command must not carry the secret."""
    base, token = bridge
    seen = {}

    threading.Thread(
        target=lambda: _post(base, "/command",
                             {"action": "list_windows", "token": token}, timeout=8),
        daemon=True).start()

    deadline = time.time() + 5
    while time.time() < deadline:
        code, data, _ = _post(base, "/poll", {"token": token}, timeout=6)
        if code == 200 and data.get("commands"):
            seen = data["commands"][0]
            break
    assert seen, "command never reached the poller"
    assert "token" not in seen, "bridge token forwarded to the extension"


def test_load_or_create_token_is_stable(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "_TOKEN", None)
    monkeypatch.setattr(srv, "TOKEN_PATH", tmp_path / "tok")
    a = srv.load_or_create_token()
    monkeypatch.setattr(srv, "_TOKEN", None)  # force a re-read from disk
    b = srv.load_or_create_token()
    assert a == b and len(a) == 32
