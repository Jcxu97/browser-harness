"""
Client for the BH companion Chrome extension. Talks to bh_extension_server
running on 127.0.0.1:9223.

Public API:
    is_available()                       # extension connected?
    start_server_if_needed()             # spawn server daemon
    send_command(action, **params)       # generic RPC
    ensure_agent_tab_via_extension()     # zero-focus-steal agent tab spawn
"""

import http.client
import json
import os
import pathlib
import subprocess
import sys
import time

PORT = 9223
SERVER_HOST = "127.0.0.1"


def _http_request(method, path, body=None, timeout=2.0):
    c = http.client.HTTPConnection(SERVER_HOST, PORT, timeout=timeout)
    headers = {"Content-Type": "application/json"} if body else {}
    raw = json.dumps(body).encode("utf-8") if body else None
    c.request(method, path, body=raw, headers=headers)
    r = c.getresponse()
    data = r.read()
    return r.status, (json.loads(data) if data else {})


def is_available():
    """True if bh-ext-server is running AND extension polled within 35s."""
    try:
        status, data = _http_request("GET", "/status", timeout=0.5)
        if status != 200:
            return False
        return bool(data.get("extension_connected"))
    except Exception:
        return False


def server_is_up():
    """Just checks if local server responds (extension may not be connected yet)."""
    try:
        status, _ = _http_request("GET", "/status", timeout=0.5)
        return status == 200
    except Exception:
        return False


def start_server_if_needed():
    """
    Spawn bh_extension_server as detached background daemon if not running.
    Returns True when server is reachable.
    """
    if server_is_up():
        return True
    # Spawn detached
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NO_WINDOW = 0x08000000
        subprocess.Popen(
            [sys.executable, "-m", "browser_harness.bh_extension_server"],
            creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    else:
        subprocess.Popen(
            [sys.executable, "-m", "browser_harness.bh_extension_server"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    for _ in range(20):
        time.sleep(0.15)
        if server_is_up():
            return True
    return False


def send_command(action, timeout=30, **params):
    """Send a command to the extension and wait for the result."""
    payload = {"action": action, **params}
    status, data = _http_request("POST", "/command", body=payload, timeout=timeout + 5)
    if status != 200:
        raise RuntimeError(f"extension command failed: HTTP {status} {data}")
    return data.get("result")


def spawn_agent_tab_in_window(window_id, url=None):
    """
    Create an agent tab in the specified existing window using
    chrome.tabs.create({active:false, windowId:X}). True zero focus steal.
    Returns CDP targetId (hex), or None on failure.

    Caller must have already verified `window_id` exists. This function does
    NOT detect or create windows — it just creates a tab in a given window.

    Concurrency note: two parallel callers must NOT receive the same targetId
    (else they'll fight over one tab). We tag the URL with a unique nonce per
    call so the scan-back step matches only OUR new tab, not someone else's
    concurrent example.com agent tab.
    """
    if not is_available():
        return None
    import os, time as _time, uuid as _uuid
    from .second_window import AGENT_SPAWN_URL
    base_url = (url or AGENT_SPAWN_URL).split("#")[0]
    # Append unique nonce so concurrent calls don't collide on URL match
    nonce = f"bh-nonce={os.getpid()}-{int(_time.time()*1000)}-{_uuid.uuid4().hex[:8]}"
    sep = "&" if "?" in base_url else "?"
    target_url = f"{base_url}{sep}{nonce}"
    result = send_command("create_tab",
                          windowId=window_id,
                          url=target_url,
                          active=False,
                          timeout=10)
    if not result or "tabId" not in result:
        return None

    # Map chrome integer tabId → CDP hex targetId by matching the unique nonce.
    from .helpers import cdp
    _time.sleep(0.4)
    targets = cdp("Target.getTargets").get("targetInfos", [])
    for t in targets:
        if t.get("type") != "page":
            continue
        if nonce in (t.get("url") or ""):
            return t.get("targetId")
    # Nonce not yet in URL (slow page load); fall back to a brief retry loop
    for _ in range(8):
        _time.sleep(0.25)
        targets = cdp("Target.getTargets").get("targetInfos", [])
        for t in targets:
            if t.get("type") == "page" and nonce in (t.get("url") or ""):
                return t.get("targetId")
    return None


# Back-compat alias (older code may still import this name).
def ensure_agent_tab_via_extension():
    """DEPRECATED — use spawn_agent_tab_in_window after second-window detection."""
    if not start_server_if_needed() or not is_available():
        return None
    ext_windows = send_command("list_windows", timeout=5)
    if not ext_windows or len(ext_windows) < 2:
        return None
    real = []
    for w in ext_windows:
        tabs = [t for t in w.get("tabs", [])
                if not (t.get("url", "") or "").startswith(("chrome://", "chrome-extension://", "devtools://"))]
        if tabs:
            real.append((w["id"], tabs))
    if len(real) < 2:
        return None
    real.sort(key=lambda kv: len(kv[1]))
    return spawn_agent_tab_in_window(real[0][0])


def stop_server():
    """Admin: stop the daemon."""
    try:
        _http_request("POST", "/shutdown", body={}, timeout=2)
    except Exception:
        pass
