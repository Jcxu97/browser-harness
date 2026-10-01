"""
Client for the BH companion Chrome extension. Talks to bh_extension_server
on 127.0.0.1:9223.

    is_available()                   extension connected?
    start_server_if_needed()         spawn the server daemon
    send_command(action, **params)   one command, answered before its deadline
"""

import http.client
import json
import secrets
import subprocess
import sys
import time

PORT = 9223
SERVER_HOST = "127.0.0.1"


def _auth_header(method, path, body):
    from .bh_extension_server import load_or_create_token, sign
    ts, nonce = str(int(time.time() * 1000)), secrets.token_hex(12)
    mac = sign(load_or_create_token(), "client", f"{method} {path}\n{ts}\n{nonce}\n{body}")
    return f"{ts}.{nonce}.{mac}"


def _http_request(method, path, body=None, timeout=2.0, headers=None):
    raw = json.dumps(body) if body is not None else ""
    c = http.client.HTTPConnection(SERVER_HOST, PORT, timeout=timeout)
    hdrs = {"X-BH-Auth": _auth_header(method, path, raw)}
    hdrs.update(headers or {})
    if raw:
        hdrs["Content-Type"] = "application/json"
    try:
        c.request(method, path, body=raw.encode("utf-8") if raw else None, headers=hdrs)
        r = c.getresponse()
        data = r.read()
        try:
            return r.status, (json.loads(data) if data else {})
        except ValueError:
            return r.status, {}
    finally:
        try:
            c.close()
        except Exception:
            pass


def _status():
    try:
        return _http_request("GET", "/status", timeout=0.5)
    except Exception:
        return None, None


def is_available():
    """True when our server runs AND the extension polled in the last 35 s."""
    status, data = _status()
    return status == 200 and bool((data or {}).get("extension_connected"))


def server_is_up():
    status, data = _status()
    return status == 200 and (data or {}).get("protocol") == 2


def last_poll_age():
    """Seconds since the extension last polled our server, or None."""
    status, data = _status()
    return (data or {}).get("last_poll_age_s") if status == 200 else None


def _stop_legacy_server():
    """A server from before protocol 2 answers 401 to the new auth. It took the
    raw token from <config dir>/extension-bridge.token; use that to stop it."""
    try:
        from . import paths
        legacy = (paths.config_dir() / "extension-bridge.token").read_text(encoding="utf-8").strip()
    except Exception:
        return
    try:
        _http_request("POST", "/shutdown", body={"token": legacy}, timeout=2.0,
                      headers={"X-BH-Token": legacy})
    except Exception:
        pass
    time.sleep(0.5)


def start_server_if_needed():
    """Spawn bh_extension_server as a detached daemon. True when it answers."""
    status, data = _status()
    if status == 200 and (data or {}).get("protocol") == 2:
        return True
    if status == 401 and (data or {}).get("protocol") != 2:
        _stop_legacy_server()
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sys.platform == "win32":
        DETACHED_PROCESS, CREATE_NO_WINDOW = 0x00000008, 0x08000000
        kwargs.update(creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW, close_fds=True)
    else:
        kwargs.update(start_new_session=True)
    subprocess.Popen([sys.executable, "-m", "browser_harness.bh_extension_server"], **kwargs)
    for _ in range(20):
        time.sleep(0.15)
        if server_is_up():
            return True
    return False


def send_command(action, timeout=10, **params):
    """Run one extension action. The server drops it when timeout passes."""
    payload = {"action": action, "timeout": timeout, **params}
    status, data = _http_request("POST", "/command", body=payload, timeout=timeout + 5)
    if status != 200:
        raise RuntimeError(f"extension command {action} failed: HTTP {status} {data}")
    result = (data or {}).get("result")
    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError(f"extension command {action} failed: {result['error']}")
    return result


def stop_server():
    try:
        _http_request("POST", "/shutdown", body={}, timeout=2)
    except Exception:
        pass
