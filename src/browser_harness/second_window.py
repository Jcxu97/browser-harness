"""
Second-window-respecting agent tab management.

Hard rule: agent tabs MUST live in the user's existing second Chrome window,
spawned via window.open from an existing tab in that window — NEVER abuse
Target.createTarget which lands in active window or steals focus.

Key APIs:
    ensure_agent_tab()             # auto-detect + auto-spawn + persistent reuse
    navigate_agent(tid, url)
    snapshot_agent(tid)            # innerText
    screenshot_agent(tid, path)
    save_as_pdf_agent(tid, path)
    evaluate_agent(tid, expr)
    fill_agent(tid, selector, value)
    click_at_agent(tid, x, y)      # alias: mouse_click_agent
    key_type_agent(tid, text)
    send_keys_agent(tid, [keys])
    upload_agent(tid, selector, [file_paths])
    list_agent_tabs()
    find_agent_tab(url_substring)
    close_agent_tab(tid)
    prune_agent_tabs(max_n=25)

Internal:
    detect_second_window()         # heuristic: window with fewest tabs
    spawn_second_window()          # chrome.exe --new-window (steals focus once)

Optional acceleration: if `bh_extension_client.is_available()` is True (the
companion Chrome extension is installed and connected), spawn goes through
chrome.tabs.create({active:false, windowId:X}) for ZERO focus steal. Fallback
is the CDP `window.open` path with focus-restore mitigation.
"""

import json, time, pathlib, subprocess, base64, os
from collections import defaultdict

from .helpers import cdp


# ---------- config ----------

STATE_DIR = pathlib.Path.home() / ".browser-harness"
STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "second-window-state.json"

# Locate chrome.exe (Windows). Override via BH_CHROME_EXE env var if needed.
def _find_chrome_exe():
    env = os.environ.get("BH_CHROME_EXE")
    if env and pathlib.Path(env).exists():
        return env
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if pathlib.Path(c).exists():
            return c
    return None

CHROME_EXE = _find_chrome_exe()

AGENT_TAB_MARKER = "bh-agent-tab"
AGENT_SPAWN_URL = f"https://example.com/?{AGENT_TAB_MARKER}=1"
DEFAULT_MAX_AGENT_TABS = 25  # cap chosen by user 2026-05-20 — prefer reuse, prune oldest beyond
SPAWN_FOCUS_WARNING = (
    "[bh.second_window] No second window detected. "
    "Spawning chrome --new-window (OS will steal focus once — unavoidable)."
)


# ---------- state ----------

def _load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"agent_tabs": []}


def _save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2))


def _pid_alive(pid):
    """Check if a PID is still running. Returns True on uncertainty (be conservative)."""
    if not pid:
        return False
    try:
        if os.name == "nt":
            # tasklist is slow; use Windows API via ctypes
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
                return bool(ok) and code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False


def _record_access(tid, claim=False):
    """Update last_access for tid. If claim=True, also claim it for self pid."""
    my_pid = os.getpid()
    state = _load_state()
    tabs = state.get("agent_tabs", [])
    for r in tabs:
        if r["tid"] == tid:
            r["last_access"] = time.time()
            if claim:
                r["claimed_by_pid"] = my_pid
            _save_state(state)
            return
    rec = {"tid": tid, "last_access": time.time()}
    if claim:
        rec["claimed_by_pid"] = my_pid
    tabs.append(rec)
    state["agent_tabs"] = tabs
    _save_state(state)


def _gc_orphan_claims(state):
    """Mutate state in place: drop claimed_by_pid for dead processes (mark orphan)."""
    for r in state.get("agent_tabs", []):
        pid = r.get("claimed_by_pid")
        if pid and not _pid_alive(pid):
            r.pop("claimed_by_pid", None)


# ---------- CDP attach helpers (no focus steal) ----------

def _attach(tid):
    return cdp("Target.attachToTarget", targetId=tid, flatten=True).get("sessionId")


def _detach(sid):
    try: cdp("Target.detachFromTarget", sessionId=sid)
    except Exception: pass


# ---------- focus-steal mitigation ----------

def _detect_main_window():
    """The window with most tabs is user's daily browsing → main."""
    windows = _list_windows()
    if not windows:
        return None
    return max(windows.items(), key=lambda kv: len(kv[1]))[0]


def _capture_main_active_tab():
    """Pick a main-window tab to re-activate after spawn (best-effort restore)."""
    main_wid = _detect_main_window()
    if main_wid is None:
        return None
    return _list_windows().get(main_wid, [(None,)])[0][0]


def _restore_main_focus(tid):
    if not tid:
        return
    try:
        cdp("Target.activateTarget", targetId=tid)
    except Exception:
        pass


# ---------- window/tab discovery ----------

def _list_windows():
    """{windowId: [(tid, url, title), ...]} for all real page tabs."""
    targets = cdp("Target.getTargets").get("targetInfos", [])
    by_window = defaultdict(list)
    for t in targets:
        if t.get("type") != "page": continue
        url = t.get("url", "")
        if url.startswith(("chrome://", "chrome-extension://", "devtools://")):
            continue
        tid = t.get("targetId")
        try:
            wid = cdp("Browser.getWindowForTarget", targetId=tid).get("windowId")
            by_window[wid].append((tid, url, t.get("title", "")))
        except Exception:
            pass
    return dict(by_window)


def detect_second_window():
    """
    Returns (windowId, [(tid,url,title),...]) for user's secondary window,
    or (None, None) if only 1 window with real tabs exists.
    Heuristic: fewest non-empty tabs.
    """
    windows = _list_windows()
    if len(windows) < 2:
        return None, None
    sorted_w = sorted(windows.items(), key=lambda kv: len(kv[1]))
    second_wid, second_tabs = sorted_w[0]
    return second_wid, second_tabs


def spawn_second_window(timeout=10):
    """Launch new chrome window. Steals focus once (unavoidable). Returns windowId."""
    if not CHROME_EXE:
        raise RuntimeError("chrome.exe not found — set BH_CHROME_EXE env var")
    print(SPAWN_FOCUS_WARNING)
    main_focus_tid = _capture_main_active_tab()
    initial = set(_list_windows().keys())
    subprocess.Popen(
        [CHROME_EXE, "--new-window", "about:blank"],
        creationflags=0x08000000,  # CREATE_NO_WINDOW
    )
    deadline = time.time() + timeout
    new_wid = None
    while time.time() < deadline:
        time.sleep(0.5)
        new_wids = set(_list_windows().keys()) - initial
        if new_wids:
            new_wid = new_wids.pop()
            break
    _restore_main_focus(main_focus_tid)
    if new_wid is None:
        raise RuntimeError("spawn_second_window: timeout")
    return new_wid


# ---------- LRU pruning ----------

def prune_agent_tabs(max_n=DEFAULT_MAX_AGENT_TABS):
    """Close oldest agent tabs over max_n. Only prunes tabs claimed by THIS process
    (or unclaimed orphans whose owner died). Never poaches another live session's tabs."""
    my_pid = os.getpid()
    state = _load_state()
    _gc_orphan_claims(state)
    records = state.get("agent_tabs", [])
    targets = cdp("Target.getTargets").get("targetInfos", [])
    live_tids = {t.get("targetId") for t in targets if t.get("type") == "page"}
    live_records = [r for r in records if r["tid"] in live_tids]
    # Keep all records that aren't ours (other sessions own them, orphans are tracked but not closed by us)
    not_mine = [r for r in live_records if r.get("claimed_by_pid") not in (my_pid, None)]
    mine_or_orphan = [r for r in live_records if r.get("claimed_by_pid") in (my_pid, None)]
    closed = 0
    while len(mine_or_orphan) > max_n:
        mine_or_orphan.sort(key=lambda r: r["last_access"])
        oldest = mine_or_orphan.pop(0)
        try:
            cdp("Target.closeTarget", targetId=oldest["tid"])
            closed += 1
        except Exception:
            pass
    state["agent_tabs"] = not_mine + mine_or_orphan
    _save_state(state)
    return closed


# ---------- core: ensure agent tab ----------

def ensure_agent_tab(max_tabs=DEFAULT_MAX_AGENT_TABS, prefer_extension=True):
    """
    Ensure an agent tab exists in the user's second Chrome window.
    Order of operations (HARD RULE: check before create):
      1. Detect second window. If exists, USE IT.
      2. If a previously-spawned agent tab in that second window is still
         alive (per state file), REUSE it. Don't create a new tab.
      3. Only if no existing agent tab: spawn a new one in the existing
         second window. Prefer extension path (silent), fallback to CDP
         window.open with focus restore.
      4. Only if no second window AT ALL: spawn a new chrome window via
         chrome.exe --new-window. This is the only path that may steal focus
         (Windows OS limitation).

    Returns the agent tab targetId.
    """
    # 1. Detect — DO NOT create yet
    second_wid, tabs = detect_second_window()
    if not second_wid:
        # No second window exists — must spawn one. Steals focus once.
        second_wid = spawn_second_window()
        tabs = _list_windows().get(second_wid, [])
    if not tabs:
        raise RuntimeError(f"second window {second_wid} has no usable tabs")

    # 2. Try to reuse a tab CLAIMED BY THIS PROCESS only — never poach another
    # session's tab (would cause concurrent sessions to fight over one tab,
    # leading to navigation hijacking. See 2026-05-20 doubao-vs-Nexus incident.)
    # Use browser-wide live tids (not just second_wid's tabs): detect_second_window
    # is a flaky heuristic that flips when tab counts shift, so a tab spawned in
    # round 1 may not appear "in second window" in round 2 even though it's still
    # live and usable. Filtering by browser-wide live keeps reuse stable.
    all_targets = cdp("Target.getTargets").get("targetInfos", [])
    browser_live_tids = {t.get("targetId") for t in all_targets if t.get("type") == "page"}
    state = _load_state()
    _gc_orphan_claims(state)  # release tabs whose owner died
    my_pid = os.getpid()
    mine = [r for r in state.get("agent_tabs", [])
            if r["tid"] in browser_live_tids and r.get("claimed_by_pid") == my_pid]
    if mine:
        mine.sort(key=lambda r: r["last_access"], reverse=True)
        tid = mine[0]["tid"]
        _record_access(tid, claim=True)
        prune_agent_tabs(max_n=max_tabs)
        return tid

    # 3. No tab claimed by us — must spawn a new one (don't poach others' tabs).
    # 3a. Prefer extension path (silent, zero focus steal)
    if prefer_extension:
        try:
            from . import bh_extension_client as ext
            if ext.is_available():
                tid = ext.spawn_agent_tab_in_window(second_wid)
                if tid:
                    _record_access(tid, claim=True)
                    prune_agent_tabs(max_n=max_tabs)
                    return tid
        except Exception:
            pass  # fall through

    # 3b. CDP fallback: window.open from seed tab + focus restore.
    # Tag window.open URL with a per-call nonce so concurrent callers don't both
    # match the same "first new agent tab" (2026-05-20 test: two threads racing
    # on the same window.open both returned the same tid).
    import uuid as _uuid
    nonce = f"{my_pid}-{int(time.time()*1000)}-{_uuid.uuid4().hex[:8]}"
    spawn_url = f"{AGENT_SPAWN_URL}&bh-nonce={nonce}"
    main_focus_tid = _capture_main_active_tab()
    seed_tid = tabs[0][0]
    before_tids = {t[0] for t in tabs}
    sid = _attach(seed_tid)
    try:
        cdp("Runtime.evaluate", session_id=sid,
            expression=f"window.open({json.dumps(spawn_url)}, '_blank', 'noopener')",
            userGesture=True)
    finally:
        _detach(sid)
    _restore_main_focus(main_focus_tid)
    time.sleep(1.0)
    _restore_main_focus(main_focus_tid)

    # Match by nonce, not by AGENT_TAB_MARKER (which is shared across calls).
    # Look browser-wide because window.open may land in main window not second.
    tid = None
    for _ in range(8):
        all_targets = cdp("Target.getTargets").get("targetInfos", [])
        for t in all_targets:
            if t.get("type") == "page" and nonce in (t.get("url") or ""):
                tid = t.get("targetId")
                break
        if tid:
            break
        time.sleep(0.3)
    if not tid:
        raise RuntimeError("Failed to spawn agent tab — nonce never appeared (popup blocker?)")
    _record_access(tid, claim=True)
    prune_agent_tabs(max_n=max_tabs)
    return tid


# ---------- agent operations ----------

def navigate_agent(agent_tid, url, timeout=15):
    sid = _attach(agent_tid)
    try:
        cdp("Page.enable", session_id=sid)
        cdp("Page.navigate", session_id=sid, url=url)
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = cdp("Runtime.evaluate", session_id=sid, expression="document.readyState")
            if r.get("result", {}).get("value") == "complete":
                _record_access(agent_tid)
                return True
            time.sleep(0.3)
        _record_access(agent_tid)
        return False
    finally:
        _detach(sid)


def snapshot_agent(agent_tid, max_chars=10000):
    sid = _attach(agent_tid)
    try:
        r = cdp("Runtime.evaluate", session_id=sid, expression="document.body.innerText")
        _record_access(agent_tid)
        return (r.get("result", {}).get("value") or "")[:max_chars]
    finally:
        _detach(sid)


def screenshot_agent(agent_tid, path):
    sid = _attach(agent_tid)
    try:
        r = cdp("Page.captureScreenshot", session_id=sid, format="png")
        data = base64.b64decode(r.get("data", ""))
        pathlib.Path(path).write_bytes(data)
        _record_access(agent_tid)
        return len(data)
    finally:
        _detach(sid)


def save_as_pdf_agent(agent_tid, path):
    sid = _attach(agent_tid)
    try:
        r = cdp("Page.printToPDF", session_id=sid, printBackground=True, preferCSSPageSize=True)
        data = base64.b64decode(r.get("data", ""))
        pathlib.Path(path).write_bytes(data)
        _record_access(agent_tid)
        return len(data)
    finally:
        _detach(sid)


def evaluate_agent(agent_tid, expression):
    sid = _attach(agent_tid)
    try:
        r = cdp("Runtime.evaluate", session_id=sid, expression=expression)
        _record_access(agent_tid)
        return r.get("result", {}).get("value")
    finally:
        _detach(sid)


def click_at_agent(agent_tid, x, y, button="left"):
    sid = _attach(agent_tid)
    try:
        cdp("Input.dispatchMouseEvent", session_id=sid,
            type="mousePressed", x=x, y=y, button=button, clickCount=1)
        cdp("Input.dispatchMouseEvent", session_id=sid,
            type="mouseReleased", x=x, y=y, button=button, clickCount=1)
        _record_access(agent_tid)
        return True
    finally:
        _detach(sid)


# Kimi-compat alias
mouse_click_agent = click_at_agent


_VK_MAP = {
    "Enter": 13, "Return": 13, "Tab": 9, "Escape": 27, "Esc": 27,
    "Backspace": 8, "Delete": 46, "Space": 32,
    "ArrowUp": 38, "ArrowDown": 40, "ArrowLeft": 37, "ArrowRight": 39,
    "Home": 36, "End": 35, "PageUp": 33, "PageDown": 34,
    "F1": 112, "F2": 113, "F3": 114, "F4": 115, "F5": 116, "F12": 123,
}


def key_type_agent(agent_tid, text):
    sid = _attach(agent_tid)
    try:
        for ch in text:
            cdp("Input.dispatchKeyEvent", session_id=sid, type="keyDown", text=ch)
            cdp("Input.dispatchKeyEvent", session_id=sid, type="keyUp", text=ch)
        _record_access(agent_tid)
    finally:
        _detach(sid)


def send_keys_agent(agent_tid, keys):
    sid = _attach(agent_tid)
    try:
        for k in keys:
            if len(k) == 1:
                cdp("Input.dispatchKeyEvent", session_id=sid, type="keyDown", text=k)
                cdp("Input.dispatchKeyEvent", session_id=sid, type="keyUp", text=k)
            else:
                vk = _VK_MAP.get(k, 0)
                cdp("Input.dispatchKeyEvent", session_id=sid, type="rawKeyDown",
                    code=k, key=k, windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk)
                cdp("Input.dispatchKeyEvent", session_id=sid, type="keyUp",
                    code=k, key=k, windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk)
        _record_access(agent_tid)
    finally:
        _detach(sid)


def upload_agent(agent_tid, selector, file_paths):
    sid = _attach(agent_tid)
    try:
        doc = cdp("DOM.getDocument", session_id=sid)
        root_id = doc.get("root", {}).get("nodeId")
        node = cdp("DOM.querySelector", session_id=sid, nodeId=root_id, selector=selector)
        node_id = node.get("nodeId")
        if not node_id:
            return "no-element"
        cdp("DOM.setFileInputFiles", session_id=sid, files=list(file_paths), nodeId=node_id)
        _record_access(agent_tid)
        return f"uploaded {len(file_paths)} file(s)"
    finally:
        _detach(sid)


def fill_agent(agent_tid, selector, value):
    sid = _attach(agent_tid)
    try:
        expr = f"""(()=>{{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return 'no-element';
            el.focus();
            el.value = {json.dumps(value)};
            el.dispatchEvent(new Event('input', {{bubbles:true}}));
            el.dispatchEvent(new Event('change', {{bubbles:true}}));
            return 'filled';
        }})()"""
        r = cdp("Runtime.evaluate", session_id=sid, expression=expr)
        _record_access(agent_tid)
        return r.get("result", {}).get("value")
    finally:
        _detach(sid)


def list_agent_tabs():
    state = _load_state()
    targets = cdp("Target.getTargets").get("targetInfos", [])
    live = {t.get("targetId"): t for t in targets if t.get("type") == "page"}
    out = []
    for r in state.get("agent_tabs", []):
        info = live.get(r["tid"])
        if info:
            out.append({
                "tid": r["tid"],
                "url": info.get("url", ""),
                "title": info.get("title", ""),
                "last_access": r["last_access"],
            })
    return out


def find_agent_tab(url_substring):
    for t in list_agent_tabs():
        if url_substring in t["url"]:
            return t["tid"]
    return None


def close_agent_tab(agent_tid):
    try:
        cdp("Target.closeTarget", targetId=agent_tid)
        state = _load_state()
        state["agent_tabs"] = [r for r in state.get("agent_tabs", []) if r["tid"] != agent_tid]
        _save_state(state)
        return True
    except Exception:
        return False
