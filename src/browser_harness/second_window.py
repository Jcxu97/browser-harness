"""Agent tabs in a Chrome window that BH owns.

BH creates one minimized window in the user's Chrome (through the companion
extension) and keeps every agent tab in it. The agent window is the window
that holds the anchor tab with our window nonce in its URL. BH never picks a
window by looking at the user's content.

Each process leases the tab it works on, so two processes never drive the
same tab. Later calls from the same Claude session reuse that session's tabs.

In safe mode (the default), helpers._send runs every request through
request_policy(). A page-level CDP call without a session goes to the agent
tab of this process. A call that would create, focus or close a tab outside
the agent window is refused.

Key APIs:
    ensure_agent_tab()                  lease an agent tab for this process
    bound_tab()                         the tab that session-less calls use
    navigate_agent(tid, url)
    snapshot_agent(tid)                 innerText
    snapshot_tree_agent(tid)            accessibility tree with @e refs
    click_ref_agent(tid, "@e3") / fill_ref_agent(tid, "@e5", "text")
    screenshot_agent(tid, path) / save_as_pdf_agent(tid, path)
    evaluate_agent(tid, expr)
    click_at_agent / key_type_agent / send_keys_agent / hotkey_agent
    fill_agent(tid, selector, value) / upload_agent(tid, selector, paths)
    new_agent_tab(url) / list_agent_tabs() / find_agent_tab(s) / close_agent_tab(tid)
    show_window() / hide_window()       only for a login the user must type
"""

import base64, contextlib, json, os, pathlib, sys, time, uuid

from .helpers import _raw_cdp as cdp, _raw_send


STATE_DIR = pathlib.Path.home() / ".browser-harness"
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / "second-window-state.json"
REF_DIR = STATE_DIR / "refs"

AGENT_TAB_MARKER = "bh-agent-tab"
AGENT_SPAWN_URL = f"https://example.com/?{AGENT_TAB_MARKER}=1"
AGENT_WINDOW_KEY = "bh-agent-window"
DEFAULT_MAX_AGENT_TABS = 15
# A lease is void when its holder is dead or idle this long. The idle limit
# covers PID reuse.
LEASE_IDLE_SECONDS = 1800
PLACEHOLDER_REAP_AGE_SECONDS = 60
# Keeps links and window.open() of agent pages in the same tab. When a page
# opens a new tab, Chrome shows and activates its window (AddNewContents uses
# kShowWindow), so the minimized agent window would jump over the user's app.
SAME_TAB_JS = r"""(() => {
  if (window.__bhSameTab) return;
  window.__bhSameTab = true;
  window.open = function (url) {
    if (url) location.assign(new URL(String(url), location.href).href);
    return null;
  };
  const same = (e) => {
    const el = e.target && e.target.closest && e.target.closest("a[target], area[target], form[target]");
    if (el && !["_self", "_top", "_parent"].includes(el.target.toLowerCase())) el.target = "_self";
  };
  document.addEventListener("click", same, true);
  document.addEventListener("submit", same, true);
})();"""
SPAWN_FOCUS_WARNING = (
    "[bh.second_window] The companion extension is not connected. The new agent "
    "tab opens in a minimized window of its own."
)


# ---------- state file ----------

def _load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"agent_tabs": []}


def _save_state(s):
    """Write tmp, then os.replace. Retry, because on Windows a reader's open
    handle makes os.replace fail with ERROR_ACCESS_DENIED."""
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(s, indent=2), encoding="utf-8")
    last_err = None
    for delay in (0, 0.05, 0.1, 0.2, 0.4):
        if delay:
            time.sleep(delay)
        try:
            os.replace(str(tmp), str(STATE_FILE))
            return
        except PermissionError as e:
            last_err = e
    try:
        tmp.unlink()
    except Exception:
        pass
    raise last_err


# ---------- cross-process locks ----------

_STATE_LOCK_PATH = STATE_DIR / "second-window-state.lock"
_SPAWN_LOCK_PATH = STATE_DIR / "second-window-spawn.lock"


def _pid_alive(pid):
    """Is this PID running? Returns True when we cannot tell.

    A false "dead" breaks a live session (a stolen lock or tab). A false
    "alive" only costs a wait or one extra tab. Only an explicit "no such
    process" answer counts as dead.
    """
    if not pid:
        return False
    try:
        if os.name == "nt":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            ERROR_INVALID_PARAMETER = 87  # OpenProcess on a pid that does not exist
            k32 = ctypes.windll.kernel32
            k32.SetLastError(0)
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return k32.GetLastError() != ERROR_INVALID_PARAMETER
            try:
                code = ctypes.c_ulong()
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return True
                return code.value == STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    except Exception:
        return True


def _lock_pid(lock_path):
    try:
        return int(lock_path.read_text(encoding="utf-8").splitlines()[0].strip())
    except Exception:
        return None


def _lock_holder_alive(lock_path):
    """Unreadable or malformed lock file counts as held."""
    pid = _lock_pid(lock_path)
    if pid is None:
        return True
    return _pid_alive(pid)


def _release_lock_if_mine(lock_path):
    pid = _lock_pid(lock_path)
    if pid is not None and pid != os.getpid():
        return
    try:
        lock_path.unlink()
    except Exception:
        pass


@contextlib.contextmanager
def _file_lock(lock_path, timeout, stale_after=30.0):
    """O_EXCL lock file. Take over a lock only when it is older than
    stale_after AND its holder is dead: a slow holder is not a dead one."""
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, f"{os.getpid()}\n{time.time()}".encode())
            finally:
                os.close(fd)
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > stale_after and not _lock_holder_alive(lock_path):
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.time() >= deadline:
                raise RuntimeError(f"{lock_path.name} held for more than {timeout}s")
            time.sleep(0.02)
    try:
        yield
    finally:
        _release_lock_if_mine(lock_path)


def _state_mutex(timeout=15.0):
    return _file_lock(_STATE_LOCK_PATH, timeout)


# ---------- owner identity and leases ----------
#
# claimed_by = the Claude session (or pid outside Claude). It gives continuity:
# the next heredoc of the same session gets its old tab back.
# lease_pid = the process that drives the tab now. Subagents inherit the
# session id, so the lease is what keeps two parallel processes apart.

def _get_owner_id():
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid:
        return f"session:{sid}"
    return f"pid:{os.getpid()}"


def _owner_alive(owner):
    if not owner:
        return False
    if isinstance(owner, int):
        return _pid_alive(owner)
    if not isinstance(owner, str):
        return False
    if owner.startswith("pid:"):
        try:
            return _pid_alive(int(owner[4:]))
        except ValueError:
            return False
    if owner.startswith("session:"):
        # A session is alive while its transcript changed in the last 6 hours.
        sid = owner[8:]
        try:
            projects = pathlib.Path.home() / ".claude" / "projects"
            if not projects.exists():
                return True
            for proj in projects.iterdir():
                jsonl = proj / f"{sid}.jsonl"
                if jsonl.exists():
                    return time.time() - jsonl.stat().st_mtime < 6 * 3600
            return False
        except Exception:
            return True
    return False


def _read_claim(record):
    v = record.get("claimed_by")
    if v:
        return v
    pid = record.get("claimed_by_pid")
    return f"pid:{pid}" if pid else None


def _write_claim(record, owner):
    record["claimed_by"] = owner
    record.pop("claimed_by_pid", None)


def _clear_claim(record):
    record.pop("claimed_by", None)
    record.pop("claimed_by_pid", None)


def _gc_orphan_claims(state):
    for r in state.get("agent_tabs", []):
        owner = _read_claim(r)
        if owner and not _owner_alive(owner):
            _clear_claim(r)


def _lease_busy(record, now=None):
    """Is another live process driving this tab?"""
    pid = record.get("lease_pid")
    if not pid or pid == os.getpid():
        return False
    if (now or time.time()) - record.get("last_access", 0) > LEASE_IDLE_SECONDS:
        return False
    return _pid_alive(pid)


def _take(record, owner):
    _write_claim(record, owner)
    record["lease_pid"] = os.getpid()
    record["last_access"] = time.time()


def _record_access(tid, claim=False, nonce=None):
    """Update last_access of tid. claim=True also claims and leases it."""
    owner = _get_owner_id()
    with _state_mutex():
        state = _load_state()
        tabs = state.setdefault("agent_tabs", [])
        rec = next((r for r in tabs if r.get("tid") == tid), None)
        if rec is None:
            rec = {"tid": tid, "created_at": time.time()}
            tabs.append(rec)
        rec["last_access"] = time.time()
        if claim:
            _take(rec, owner)
        if nonce and not rec.get("nonce"):
            rec["nonce"] = nonce
        _save_state(state)


_LAST_TOUCH = {}


def _touch(tid):
    """Throttled _record_access: at most one state write per tab every 10s."""
    now = time.time()
    if now - _LAST_TOUCH.get(tid, 0) < 10:
        return
    _LAST_TOUCH[tid] = now
    try:
        _record_access(tid)
    except Exception:
        pass


# ---------- CDP basics ----------

def _attach(tid):
    return cdp("Target.attachToTarget", targetId=tid, flatten=True).get("sessionId")


def _detach(sid):
    try:
        cdp("Target.detachFromTarget", sessionId=sid)
    except Exception:
        pass


def _page_targets():
    return [t for t in cdp("Target.getTargets").get("targetInfos", []) if t.get("type") == "page"]


def _window_of(tid):
    try:
        return cdp("Browser.getWindowForTarget", targetId=tid).get("windowId")
    except Exception:
        return None


def _extension(wait=5.0, patient=False):
    """The extension client when the companion extension is connected, else None.
    patient: when the extension polled in the last 2 minutes, wait up to 35 s
    for its next poll. Chrome may have stopped its worker, and the 30 s alarm
    starts it again."""
    try:
        from . import bh_extension_client as ext
        was_up = ext.server_is_up()
        if not was_up and not ext.start_server_if_needed():
            return None
        if was_up:
            age = ext.last_poll_age() if patient else None
            wait = 35.0 if age is not None and age < 120 else 1.0
        deadline = time.time() + wait
        while not ext.is_available():
            if time.time() >= deadline:
                return None
            time.sleep(0.25)
        return ext
    except Exception:
        return None


def _new_nonce():
    return f"{_get_owner_id().replace(':', '-')}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"


def _nonce_age(url):
    """Seconds since the nonce in this URL was made. Unknown format = new."""
    try:
        nonce = url.split("bh-nonce=", 1)[1].split("&", 1)[0]
        return time.time() - int(nonce.rsplit("-", 2)[1]) / 1000.0
    except Exception:
        return 0.0


def _resolve_nonce(nonce, timeout):
    deadline = time.time() + timeout
    while True:
        for t in _page_targets():
            if nonce in (t.get("url") or ""):
                return t["targetId"]
        if time.time() >= deadline:
            return None
        time.sleep(0.2)


# ---------- the agent window ----------

def find_agent_window(pages=None, state=None):
    """(window id, anchor tid) of the agent window, or (None, None)."""
    state = state if state is not None else _load_state()
    nonce = (state.get("agent_window") or {}).get("nonce")
    if not nonce:
        return None, None
    needle = f"{AGENT_WINDOW_KEY}={nonce}"
    for t in (pages if pages is not None else _page_targets()):
        if needle in (t.get("url") or ""):
            wid = _window_of(t["targetId"])
            if wid is not None:
                return wid, t["targetId"]
    return None, None


def ensure_agent_window():
    wid, _ = find_agent_window()
    if wid is not None:
        return wid
    with _file_lock(_SPAWN_LOCK_PATH, timeout=45.0, stale_after=60.0):
        wid, _ = find_agent_window()
        if wid is not None:
            return wid
        return _spawn_agent_window()


def _spawn_agent_window():
    nonce = _new_nonce()
    url = f"https://example.com/?{AGENT_WINDOW_KEY}={nonce}"
    with _state_mutex():
        state = _load_state()
        state["agent_window"] = {"nonce": nonce, "created_at": time.time()}
        state.pop("pinned_second_window_id", None)
        _save_state(state)
    # Not the extension: chrome.windows.create with focused:false shows the
    # window inactive but not minimized, on top of the user's app (seen
    # 2026-10-01). CDP shows it inactive and minimizes it in the same step.
    cdp("Target.createTarget", url=url, newWindow=True, background=True, windowState="minimized")
    deadline = time.time() + 10
    while time.time() < deadline:
        wid, _ = find_agent_window()
        if wid is not None:
            try:
                cdp("Browser.setWindowBounds", windowId=wid, bounds={"windowState": "minimized"})
            except Exception:
                pass
            return wid
        time.sleep(0.2)
    raise RuntimeError("the agent window did not appear after spawn")


def _assert_landed_in(tid, expected_wid):
    """Close the new tab and raise when it is not in the agent window.

    Asks Chrome where the tab is. An unverifiable answer counts as wrong.
    """
    try:
        actual = cdp("Browser.getWindowForTarget", targetId=tid).get("windowId")
    except Exception as e:
        try:
            cdp("Target.closeTarget", targetId=tid)
        except Exception:
            pass
        raise RuntimeError(
            f"agent tab {tid}: cannot confirm which window it is in ({e}); closed it"
        )
    if actual != expected_wid:
        try:
            cdp("Target.closeTarget", targetId=tid)
        except Exception:
            pass
        raise RuntimeError(
            f"agent tab opened in window {actual}, not in the agent window {expected_wid}; closed it"
        )


def _spawn_agent_tab(wid, use_extension=True):
    """Open a placeholder agent tab. Returns (tid, nonce, window): window is None
    for a tab in the agent window, else the window that holds only this tab."""
    nonce = _new_nonce()
    url = f"{AGENT_SPAWN_URL}&bh-nonce={nonce}"
    ext = _extension(patient=True) if use_extension else None
    if ext is None:
        # CDP cannot add a tab to the minimized agent window without focus:
        # window.open() made Chrome restore and activate it (seen 2026-10-01).
        # A new minimized window opens without focus.
        print(SPAWN_FOCUS_WARNING, file=sys.stderr)
        tid = cdp("Target.createTarget", url=url, newWindow=True, background=True,
                  windowState="minimized")["targetId"]
        window = _window_of(tid)
        if window is None:
            try:
                cdp("Target.closeTarget", targetId=tid)
            except Exception:
                pass
            raise RuntimeError(f"agent tab {tid}: cannot confirm which window it is in; closed it")
        _SPAWNED.add(tid)
        return tid, nonce, window
    r = ext.send_command("create_tab", windowId=wid, url=url, active=False, timeout=10)
    if not r or "tabId" not in r:
        raise RuntimeError(f"extension could not create the agent tab: {r}")
    tid = _resolve_nonce(nonce, 8.0)
    if tid is None:
        raise RuntimeError("agent tab spawn failed: the new tab did not show up in CDP")
    _assert_landed_in(tid, wid)
    _SPAWNED.add(tid)
    return tid, nonce, None


def _new_record(tid, nonce, window):
    rec = {"tid": tid, "nonce": nonce, "created_at": time.time()}
    if window is not None:
        rec["window"] = window
    return rec


def _in_agent_window(record, wid):
    """Is the tab of record in the agent window, or still in the window that the
    CDP fallback opened for it?"""
    w = _window_of(record["tid"])
    return w is not None and w in (wid, record.get("window"))


def _own_windows():
    """Windows that the CDP fallback opened for single tabs, while the tab is in them."""
    return [r["window"] for r in _load_state().get("agent_tabs", [])
            if r.get("window") and _window_of(r["tid"]) == r["window"]]


# ---------- agent tabs ----------

_SPAWNED = set()      # tabs this process created
_NAVIGATED = set()    # tabs this process navigated


def ensure_agent_tab(max_tabs=DEFAULT_MAX_AGENT_TABS, prefer_extension=True, prefer_url=None):
    """Lease an agent tab for this process and return its target id.

    Order: a tab this process already holds, then a free tab of this session,
    then a free tab nobody claims, then a new tab. prefer_url moves tabs whose
    URL contains it to the front.
    """
    wid = ensure_agent_window()
    owner = _get_owner_id()
    chosen = None
    with _state_mutex():
        state = _load_state()
        pages = {t["targetId"]: t for t in _page_targets()}
        records = [r for r in state.get("agent_tabs", [])
                   if r.get("tid") in pages and _in_agent_window(r, wid)]
        state["agent_tabs"] = records
        _gc_orphan_claims(state)
        tiers = [
            [r for r in records if r.get("lease_pid") == os.getpid()],
            [r for r in records if _read_claim(r) == owner and not _lease_busy(r)],
            [r for r in records if _read_claim(r) is None and not _lease_busy(r)],
        ]
        for tier in tiers:
            tier.sort(key=lambda r: r.get("last_access", 0), reverse=True)
        if prefer_url:
            for tier in tiers:
                hit = [r for r in tier if prefer_url in (pages[r["tid"]].get("url") or "")]
                if hit:
                    chosen = hit[0]
                    break
        if chosen is None:
            chosen = next((tier[0] for tier in tiers if tier), None)
        if chosen is not None:
            _take(chosen, owner)
        _save_state(state)
    if chosen is None:
        tid, nonce, window = _spawn_agent_tab(wid, use_extension=prefer_extension)
        with _state_mutex():
            state = _load_state()
            chosen = _new_record(tid, nonce, window)
            _take(chosen, owner)
            state.setdefault("agent_tabs", []).append(chosen)
            _save_state(state)
    _register_exit()
    prune_agent_tabs(max_n=max_tabs)
    return chosen["tid"]


def new_agent_tab(url=None):
    """Open one more agent tab leased to this process. Does not bind it."""
    wid = ensure_agent_window()
    tid, nonce, window = _spawn_agent_tab(wid)
    with _state_mutex():
        state = _load_state()
        rec = _new_record(tid, nonce, window)
        _take(rec, _get_owner_id())
        state.setdefault("agent_tabs", []).append(rec)
        _save_state(state)
    _register_exit()
    prune_agent_tabs()
    if url and url != "about:blank":
        navigate_agent(tid, url)
    return tid


def prune_agent_tabs(max_n=DEFAULT_MAX_AGENT_TABS):
    """Close the least recently used free agent tabs above max_n, and placeholder
    tabs in the agent window that lost their record. Returns how many closed."""
    victims = []
    with _state_mutex():
        state = _load_state()
        pages = {t["targetId"]: t for t in _page_targets()}
        wid, anchor = find_agent_window(pages=list(pages.values()), state=state)
        if wid is None:
            return 0
        records = [r for r in state.get("agent_tabs", []) if r.get("tid") in pages]
        excess = len(records) - max_n
        if excess > 0:
            free = sorted((r for r in records
                           if r.get("lease_pid") != os.getpid() and not _lease_busy(r)),
                          key=lambda r: r.get("last_access", 0))
            victims = [r["tid"] for r in free[:excess]]
            records = [r for r in records if r["tid"] not in victims]
        state["agent_tabs"] = records
        _save_state(state)
        known = {r["tid"] for r in records} | set(victims) | {anchor}
    for tid, t in pages.items():
        url = t.get("url") or ""
        if tid in known or "bh-nonce=" not in url:
            continue
        if _nonce_age(url) > PLACEHOLDER_REAP_AGE_SECONDS and _window_of(tid) == wid:
            victims.append(tid)
    for tid in victims:
        try:
            cdp("Target.closeTarget", targetId=tid)
        except Exception:
            pass
        _forget_tab(tid)
    return len(victims)


def is_agent_tab(tid):
    """Is tid a tab in the agent window other than the anchor, or a tab in the
    window that the CDP fallback opened for it?"""
    if not tid:
        return False
    wid, anchor = find_agent_window()
    if tid == anchor:
        return False
    rec = next((r for r in _load_state().get("agent_tabs", []) if r.get("tid") == tid), {"tid": tid})
    return _in_agent_window(rec, wid)


def is_placeholder(tid):
    """Does tid still show the placeholder page it was opened with?"""
    try:
        url = cdp("Target.getTargetInfo", targetId=tid).get("targetInfo", {}).get("url") or ""
    except Exception:
        return False
    return "bh-nonce=" in url


# ---------- sessions and the bound tab ----------

_SESSIONS = {}           # tid -> the session this process keeps on it
_EXTRA_SESSIONS = set()  # sessions that callers attached themselves
_BOUND = {"tid": None}   # the tab that session-less calls use
_DAEMON_REPOINTED = [False]
_EXIT_REGISTERED = [False]

_SESSION_GONE = ("Session with given id not found", "No session with given id")
_TARGET_GONE = ("No target with given id", "Target closed", "Inspected target navigated or closed")
# Errors that relay a page's own JS error contain this text. The page controls
# what follows it, so never scan that part for the markers above.
_PAGE_ERROR_MARKER = "JavaScript evaluation failed"


def _own_text(exc):
    msg = str(exc)
    idx = msg.find(_PAGE_ERROR_MARKER)
    return msg if idx == -1 else msg[:idx]


def is_session_gone(exc):
    return any(n in _own_text(exc) for n in _SESSION_GONE)


def is_target_gone(exc):
    return any(n in _own_text(exc) for n in _TARGET_GONE)


def _session_for(tid):
    sid = _SESSIONS.get(tid)
    if sid:
        return sid
    sid = _attach(tid)
    for domain in ("Page", "DOM", "Network"):
        try:
            cdp(f"{domain}.enable", session_id=sid)
        except Exception:
            pass
    try:
        # Pages in the minimized agent window then behave as if they have focus.
        cdp("Emulation.setFocusEmulationEnabled", session_id=sid, enabled=True)
    except Exception:
        pass
    try:
        cdp("Page.addScriptToEvaluateOnNewDocument", session_id=sid, source=SAME_TAB_JS)
        cdp("Runtime.evaluate", session_id=sid, expression=SAME_TAB_JS)
    except Exception:
        pass
    _SESSIONS[tid] = sid
    _register_exit()
    return sid


def _call(tid, method, _response_timeout=None, **params):
    """CDP call on this process's session for tid. When only the session is
    gone, attach again and retry once: it is the same tab, so this is safe."""
    kw = {} if _response_timeout is None else {"_response_timeout": _response_timeout}
    try:
        return cdp(method, session_id=_session_for(tid), **kw, **params)
    except RuntimeError as e:
        if not is_session_gone(e):
            raise
        _SESSIONS.pop(tid, None)
        return cdp(method, session_id=_session_for(tid), **kw, **params)


def _forget_tab(tid, drop_record=True):
    sid = _SESSIONS.pop(tid, None)
    if sid:
        _detach(sid)
    if _BOUND["tid"] == tid:
        _BOUND["tid"] = None
    if not drop_record:
        return
    try:
        with _state_mutex():
            state = _load_state()
            state["agent_tabs"] = [r for r in state.get("agent_tabs", []) if r.get("tid") != tid]
            _save_state(state)
    except Exception:
        pass


def bound_tab():
    """The agent tab of this process. Leases one on first use."""
    tid = _BOUND["tid"]
    if tid is None:
        tid = ensure_agent_tab()
        _BOUND["tid"] = tid
        _repoint_daemon_default()
    return tid


def bind(tid):
    """Make tid the tab for session-less calls. tid must be a free agent tab."""
    if not is_agent_tab(tid):
        raise RuntimeError(f"{tid} is not a tab in the agent window")
    rec = next((r for r in _load_state().get("agent_tabs", []) if r.get("tid") == tid), None)
    if rec is not None and _lease_busy(rec):
        raise RuntimeError(f"agent tab {tid} is in use by process {rec.get('lease_pid')}")
    _record_access(tid, claim=True)
    _BOUND["tid"] = tid
    _register_exit()
    return tid


def _repoint_daemon_default():
    """Move the daemon's default session off the user's tab (the daemon attaches
    to the first real page when it starts) and onto our anchor tab. Once per process."""
    if _DAEMON_REPOINTED[0]:
        return
    _DAEMON_REPOINTED[0] = True
    try:
        wid, anchor = find_agent_window()
        if anchor is None:
            return
        cur = _raw_send({"meta": "current_tab"}).get("targetId")
        if cur == anchor or (cur and _window_of(cur) == wid):
            return
        old = _raw_send({"meta": "session"}).get("session_id")
        sid = _attach(anchor)
        _raw_send({"meta": "set_session", "session_id": sid, "target_id": anchor})
        if old:
            _detach(old)
    except Exception:
        pass


def _register_exit():
    if not _EXIT_REGISTERED[0]:
        _EXIT_REGISTERED[0] = True
        import atexit
        atexit.register(_exit_cleanup)


def _exit_cleanup():
    """Release this process's leases, close the placeholder tabs it never used,
    detach its sessions. BH_KEEP_PLACEHOLDERS=1 keeps the placeholders."""
    try:
        keep = os.environ.get("BH_KEEP_PLACEHOLDERS") == "1"
        pages = {t["targetId"]: t for t in _page_targets()}
        to_close = []
        with _state_mutex(timeout=5.0):
            state = _load_state()
            kept = []
            for r in state.get("agent_tabs", []):
                if r.get("lease_pid") == os.getpid():
                    r["lease_pid"] = None
                    url = (pages.get(r["tid"]) or {}).get("url") or ""
                    placeholder = bool(r.get("nonce")) and r["nonce"] in url
                    unused = (r["tid"] in _SPAWNED and r["tid"] not in _NAVIGATED
                              and url in ("", "about:blank"))
                    # A CDP fallback tab has a window of its own; close it so
                    # these windows do not pile up in the taskbar.
                    if r["tid"] in pages and (r.get("window") or (not keep and (placeholder or unused))):
                        to_close.append(r["tid"])
                        continue
                kept.append(r)
            state["agent_tabs"] = kept
            _save_state(state)
        for sid in list(_SESSIONS.values()) + list(_EXTRA_SESSIONS):
            _detach(sid)
        _SESSIONS.clear()
        _EXTRA_SESSIONS.clear()
        for tid in to_close:
            try:
                cdp("Target.closeTarget", targetId=tid)
            except Exception:
                pass
    except Exception:
        pass


# ---------- safe-mode request policy (installed by helpers) ----------

_REFUSED = {
    "Target.activateTarget": "Focus changes are blocked in safe mode. When the user must see "
                             "the agent window (to type a password), call show_window().",
    "Browser.close": "Closing Chrome is blocked: it is the user's daily browser.",
    "Browser.crash": "Crashing Chrome is blocked: it is the user's daily browser.",
    "Browser.crashGpuProcess": "Crashing Chrome is blocked: it is the user's daily browser.",
}
_IGNORED = {"Page.bringToFront"}
_WINDOW_CACHE = {}


def _window_of_cached(tid, ttl=5.0):
    hit = _WINDOW_CACHE.get(tid)
    if hit and time.time() - hit[1] < ttl:
        return hit[0]
    wid = _window_of(tid)
    _WINDOW_CACHE[tid] = (wid, time.time())
    return wid


def _require_agent_target(tid, action):
    try:
        info = cdp("Target.getTargetInfo", targetId=tid).get("targetInfo", {})
    except Exception:
        return  # a bad id: let Chrome report it
    if info.get("type") == "page" and not is_agent_tab(tid):
        raise RuntimeError(
            f"{action} refused: {tid} is not a tab in the agent window. BH only drives "
            "tabs in its own window. To read a page the user has open, open its URL with goto()."
        )


def _filter_targets(response):
    res = response.get("result") or {}
    infos = res.get("targetInfos")
    if not isinstance(infos, list):
        return response
    wid, anchor = find_agent_window(pages=[t for t in infos if t.get("type") == "page"])
    own = {r.get("tid"): r.get("window") for r in _load_state().get("agent_tabs", [])}

    def visible(t):
        tid = t.get("targetId")
        if t.get("type") != "page":
            return True
        w = _window_of_cached(tid)
        return tid != anchor and w is not None and w in (wid, own.get(tid))
    return {**response, "result": {**res, "targetInfos": [t for t in infos if visible(t)]}}


def request_policy(req, forward):
    """Rewrite or refuse one daemon request. forward(req) sends it unchanged."""
    meta = req.get("meta")
    if meta:
        return _meta_policy(req, forward)
    method = req.get("method") or ""
    params = req.get("params") or {}
    if method in _REFUSED:
        raise RuntimeError(_REFUSED[method])
    if method in _IGNORED:
        return {"result": {}}
    if method == "Target.createTarget":
        return {"result": {"targetId": new_agent_tab(params.get("url"))}}
    if method == "Target.closeTarget":
        tid = params.get("targetId")
        if not is_agent_tab(tid):
            raise RuntimeError(f"Target.closeTarget refused: {tid} is not a tab in the agent window")
        response = forward(req)
        _forget_tab(tid)
        return response
    if method == "Target.attachToTarget":
        _require_agent_target(params.get("targetId"), "Target.attachToTarget")
        response = forward(req)
        sid = (response.get("result") or {}).get("sessionId")
        if sid:
            _EXTRA_SESSIONS.add(sid)
        return response
    if method == "Target.getTargets":
        return _filter_targets(forward(req))
    if method == "Browser.setWindowBounds":
        wid, _ = find_agent_window()
        if wid is None or params.get("windowId") != wid:
            raise RuntimeError("Browser.setWindowBounds refused: only the agent window can change")
        return forward(req)
    if method.startswith("Target.") or req.get("session_id"):
        return forward(req)
    return _on_bound_tab(req, method, forward)


def _meta_policy(req, forward):
    meta = req["meta"]
    if meta == "current_tab":
        for attempt in (1, 2):
            tid = bound_tab()
            try:
                info = cdp("Target.getTargetInfo", targetId=tid).get("targetInfo", {})
                return {"targetId": tid, "url": info.get("url", ""), "title": info.get("title", "")}
            except RuntimeError as e:
                if attempt == 2 or not is_target_gone(e):
                    raise
                _forget_tab(tid)
    if meta == "session":
        return {"session_id": _session_for(bound_tab())}
    if meta == "set_session":
        bind(req.get("target_id"))
        return {"session_id": req.get("session_id")}
    return forward(req)


def _forward_on(tid, req, forward):
    try:
        return forward({**req, "session_id": _session_for(tid)})
    except RuntimeError as e:
        if not is_session_gone(e):
            raise
        _SESSIONS.pop(tid, None)
        # Raises "No target" when the tab itself is gone.
        return forward({**req, "session_id": _session_for(tid)})


def _on_bound_tab(req, method, forward):
    tid = bound_tab()
    try:
        response = _forward_on(tid, req, forward)
    except RuntimeError as e:
        if not is_target_gone(e):
            raise
        _forget_tab(tid)
        if method != "Page.navigate":
            raise RuntimeError(
                f"the agent tab closed during {method}. The call was not repeated on a "
                f"new tab, because page state does not carry over. Call goto() again. ({e})"
            ) from e
        tid = bound_tab()
        response = _forward_on(tid, req, forward)
    if method == "Page.navigate":
        _NAVIGATED.add(tid)
    _touch(tid)
    return response


# ---------- agent operations ----------

def _eval_value(agent_tid, expression):
    r = _call(agent_tid, "Runtime.evaluate", expression=expression, returnByValue=True)
    return (r.get("result") or {}).get("value")


def navigate_agent(agent_tid, url, timeout=15):
    """Navigate and wait until the new document is complete.
    False on timeout or when Chrome reports a load error."""
    try:
        before = _eval_value(agent_tid, "performance.timeOrigin")
    except Exception:
        before = None
    r = _call(agent_tid, "Page.navigate", url=url)
    _NAVIGATED.add(agent_tid)
    _touch(agent_tid)
    if r.get("errorText"):
        return False
    if not r.get("loaderId"):
        return True  # same-document navigation
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            v = _eval_value(agent_tid, "[performance.timeOrigin, document.readyState]")
        except RuntimeError as e:
            if is_target_gone(e):
                raise
            v = None  # the old context went away during the navigation
        if v and v[0] != before and v[1] == "complete":
            return True
        time.sleep(0.2)
    return False


def snapshot_agent(agent_tid, max_chars=10000):
    _touch(agent_tid)
    return (_eval_value(agent_tid, "document.body ? document.body.innerText : ''") or "")[:max_chars]


def screenshot_agent(agent_tid, path, full=False):
    r = _call(agent_tid, "Page.captureScreenshot", _response_timeout=60.0,
              format="png", captureBeyondViewport=full)
    data = base64.b64decode(r.get("data", ""))
    pathlib.Path(path).write_bytes(data)
    _touch(agent_tid)
    return len(data)


def save_as_pdf_agent(agent_tid, path):
    r = _call(agent_tid, "Page.printToPDF", _response_timeout=60.0,
              printBackground=True, preferCSSPageSize=True)
    data = base64.b64decode(r.get("data", ""))
    pathlib.Path(path).write_bytes(data)
    _touch(agent_tid)
    return len(data)


def evaluate_agent(agent_tid, expression, await_promise=True):
    """Same semantics as helpers.js(): values come back by value, promises are
    awaited, a top-level `return` is retried inside a function, JS errors raise."""
    from .helpers import _is_illegal_return_error, _runtime_value, _wrap_js_function

    def run(expr):
        r = _call(agent_tid, "Runtime.evaluate", expression=expr,
                  returnByValue=True, awaitPromise=await_promise)
        return _runtime_value(r, expr)

    try:
        value = run(expression)
    except RuntimeError as e:
        if not _is_illegal_return_error(e):
            raise
        value = run(_wrap_js_function(expression))
    _touch(agent_tid)
    return value


def click_at_agent(agent_tid, x, y, button="left"):
    _call(agent_tid, "Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
    _call(agent_tid, "Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button=button, clickCount=1)
    _call(agent_tid, "Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button=button, clickCount=1)
    _touch(agent_tid)
    return True


mouse_click_agent = click_at_agent


_VK_MAP = {
    "Enter": 13, "Return": 13, "Tab": 9, "Escape": 27, "Esc": 27,
    "Backspace": 8, "Delete": 46, "Space": 32,
    "ArrowUp": 38, "ArrowDown": 40, "ArrowLeft": 37, "ArrowRight": 39,
    "Home": 36, "End": 35, "PageUp": 33, "PageDown": 34,
    "F1": 112, "F2": 113, "F3": 114, "F4": 115, "F5": 116, "F12": 123,
}


def key_type_agent(agent_tid, text):
    for ch in text:
        _call(agent_tid, "Input.dispatchKeyEvent", type="keyDown", text=ch)
        _call(agent_tid, "Input.dispatchKeyEvent", type="keyUp", text=ch)
    _touch(agent_tid)


def send_keys_agent(agent_tid, keys):
    """keys: a list such as ["Tab", "a", "Enter"], or a string. A string that
    names a key ("Enter") is that key; any other string is typed per character."""
    if isinstance(keys, str):
        keys = [keys] if keys in _VK_MAP else list(keys)
    for k in keys:
        if len(k) == 1:
            _call(agent_tid, "Input.dispatchKeyEvent", type="keyDown", text=k)
            _call(agent_tid, "Input.dispatchKeyEvent", type="keyUp", text=k)
        else:
            vk = _VK_MAP.get(k, 0)
            text = "\r" if k in ("Enter", "Return") else None
            down = dict(type="rawKeyDown", code=k, key=k, windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk)
            _call(agent_tid, "Input.dispatchKeyEvent", **down)
            if text:
                _call(agent_tid, "Input.dispatchKeyEvent", type="char", text=text)
            _call(agent_tid, "Input.dispatchKeyEvent", **{**down, "type": "keyUp"})
    _touch(agent_tid)


# CDP Input.dispatchKeyEvent modifier bits: (bit, vk, code, key)
_CHORD_MODS = {
    "alt": (1, 18, "AltLeft", "Alt"),
    "ctrl": (2, 17, "ControlLeft", "Control"),
    "control": (2, 17, "ControlLeft", "Control"),
    "meta": (4, 91, "MetaLeft", "Meta"),
    "cmd": (4, 91, "MetaLeft", "Meta"),
    "command": (4, 91, "MetaLeft", "Meta"),
    "win": (4, 91, "MetaLeft", "Meta"),
    "shift": (8, 16, "ShiftLeft", "Shift"),
}


def _resolve_chord_key(tok):
    if len(tok) == 1:
        ch = tok.upper()
        if "A" <= ch <= "Z":
            return ord(ch), "Key" + ch, tok
        if "0" <= ch <= "9":
            return ord(ch), "Digit" + ch, tok
        return 0, tok, tok
    return _VK_MAP.get(tok, 0), tok, tok


def hotkey_agent(agent_tid, chord):
    """Press a chord such as "Control+End" or "Shift+ArrowRight" with the
    modifiers held down."""
    parts = [p.strip() for p in chord.split("+") if p.strip()]
    if not parts:
        return
    *mod_names, final = parts
    mods = []
    for name in mod_names:
        m = _CHORD_MODS.get(name.lower())
        if m is None:
            raise ValueError(f"unknown modifier {name!r} in chord {chord!r}")
        mods.append(m)
    mask = 0
    for bit, *_ in mods:
        mask |= bit
    fvk, fcode, fkey = _resolve_chord_key(final)
    held = 0
    for bit, vk, code, key in mods:
        held |= bit
        _call(agent_tid, "Input.dispatchKeyEvent", type="rawKeyDown", code=code, key=key,
              windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk, modifiers=held)
    # No `text` on the final key: with a modifier held it would type a control
    # character instead of firing the shortcut.
    for kind in ("rawKeyDown", "keyUp"):
        _call(agent_tid, "Input.dispatchKeyEvent", type=kind, code=fcode, key=fkey,
              windowsVirtualKeyCode=fvk, nativeVirtualKeyCode=fvk, modifiers=mask)
    for bit, vk, code, key in reversed(mods):
        held &= ~bit
        _call(agent_tid, "Input.dispatchKeyEvent", type="keyUp", code=code, key=key,
              windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk, modifiers=held)
    _touch(agent_tid)


def upload_agent(agent_tid, selector, file_paths):
    if isinstance(file_paths, (str, os.PathLike)):
        file_paths = [file_paths]
    root = _call(agent_tid, "DOM.getDocument").get("root", {}).get("nodeId")
    node_id = _call(agent_tid, "DOM.querySelector", nodeId=root, selector=selector).get("nodeId")
    if not node_id:
        raise RuntimeError(f"upload: no element matched selector {selector!r}")
    _call(agent_tid, "DOM.setFileInputFiles", files=[str(p) for p in file_paths], nodeId=node_id)
    _touch(agent_tid)
    return f"uploaded {len(file_paths)} file(s)"


# Sets the value through the prototype setter, so React and Vue see the change.
_FILL_FN = """function(v) {
  this.scrollIntoView({block: 'center'});
  this.focus();
  if (this.isContentEditable) {
    document.execCommand('selectAll', false, null);
    document.execCommand('insertText', false, v);
    return 'filled';
  }
  if (!('value' in this)) return 'not-fillable';
  const proto = this instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
    : this instanceof HTMLSelectElement ? HTMLSelectElement.prototype
    : HTMLInputElement.prototype;
  const desc = Object.getOwnPropertyDescriptor(proto, 'value');
  if (desc && desc.set) desc.set.call(this, v); else this.value = v;
  this.dispatchEvent(new Event('input', {bubbles: true}));
  this.dispatchEvent(new Event('change', {bubbles: true}));
  return 'filled';
}"""


def fill_agent(agent_tid, selector, value):
    expr = (f"(() => {{ const el = document.querySelector({json.dumps(selector)});"
            f" if (!el) return 'no-element'; return ({_FILL_FN}).call(el, {json.dumps(value)}); }})()")
    result = evaluate_agent(agent_tid, expr)
    if result == "no-element":
        raise RuntimeError(f"fill: no element matched selector {selector!r}")
    if result != "filled":
        raise RuntimeError(f"fill: {selector!r} is {result}")
    return result


# ---------- accessibility snapshot with @e refs ----------
#
# snapshot_tree_agent() saves its refs in REF_DIR/<tid>.json, so a later
# heredoc can still use click_ref("@e3"). A ref is valid while the document
# that made it is still loaded.

_INTERACTIVE_ROLES = {
    "button", "link", "checkbox", "radio", "textbox", "searchbox", "combobox",
    "listbox", "option", "menuitem", "menuitemcheckbox", "menuitemradio", "tab",
    "switch", "slider", "spinbutton", "treeitem", "heading",
}
_VALUE_ROLES = {"textbox", "searchbox", "combobox", "spinbutton", "slider"}
_STATE_PROPS = ("focused", "checked", "selected", "disabled", "expanded", "required")
_SKIP_ROLES = {"generic", "none", "InlineTextBox", "LineBreak", "RootWebArea"}


def _ax_value(field):
    return (field or {}).get("value") if isinstance(field, dict) else None


def _ref_path(tid):
    return REF_DIR / f"{tid}.json"


def snapshot_tree_agent(agent_tid, interactive_only=True, roles=None, max_chars=12000):
    """Accessibility tree as text lines like `@e3 button "Sign in"`."""
    r = _call(agent_tid, "Accessibility.getFullAXTree", _response_timeout=30.0)
    nodes = r.get("nodes") or []
    by_id = {n.get("nodeId"): n for n in nodes}
    roles = set(roles) if roles else None
    lines, refs = [], {}

    def keep(node, role, name):
        if roles is not None:
            return role in roles
        if role in _INTERACTIVE_ROLES:
            return bool(name) or role in _VALUE_ROLES
        return not interactive_only and bool(name) and role not in _SKIP_ROLES

    def walk(node, depth):
        role = _ax_value(node.get("role")) or ""
        name = (_ax_value(node.get("name")) or "").strip()
        shown = not node.get("ignored") and node.get("backendDOMNodeId") and keep(node, role, name)
        if shown:
            key = f"e{len(refs) + 1}"
            refs[key] = {"b": node["backendDOMNodeId"], "role": role, "name": name[:200]}
            line = f'{"  " * depth}@{key} {role} {json.dumps(name[:80], ensure_ascii=False)}'
            value = _ax_value(node.get("value"))
            if role in _VALUE_ROLES and value not in (None, ""):
                line += f" value={json.dumps(str(value)[:60], ensure_ascii=False)}"
            for p in node.get("properties") or []:
                pname, pval = p.get("name"), _ax_value(p.get("value"))
                if pname in _STATE_PROPS and pval not in (None, False, "false"):
                    line += f" [{pname}]" if pval is True or pval == "true" else f" [{pname}={pval}]"
            lines.append(line)
        for cid in node.get("childIds") or []:
            child = by_id.get(cid)
            if child is not None:
                walk(child, depth + 1 if shown else depth)

    root = next((n for n in nodes if not n.get("parentId")), None)
    if root is not None:
        walk(root, 0)
    REF_DIR.mkdir(parents=True, exist_ok=True)
    _ref_path(agent_tid).write_text(json.dumps({
        "document": _eval_value(agent_tid, "performance.timeOrigin"),
        "refs": refs,
    }), encoding="utf-8")
    _touch(agent_tid)
    text, total = "", 0
    for i, line in enumerate(lines):
        if total + len(line) + 1 > max_chars:
            text += f"... ({len(lines) - i} more lines; pass roles= or max_chars= to see them)\n"
            break
        text += line + "\n"
        total += len(line) + 1
    return text.rstrip("\n")


def _ref_node(agent_tid, ref):
    key = str(ref).lstrip("@")
    try:
        data = json.loads(_ref_path(agent_tid).read_text(encoding="utf-8"))
    except Exception:
        data = {}
    node = (data.get("refs") or {}).get(key)
    if node is None:
        raise RuntimeError(f"unknown ref {ref}: call snapshot() first")
    if data.get("document") != _eval_value(agent_tid, "performance.timeOrigin"):
        raise RuntimeError(f"ref {ref} is stale: the page loaded a new document. Call snapshot() again.")
    return node


def _resolve_ref(agent_tid, ref):
    node = _ref_node(agent_tid, ref)
    try:
        obj = _call(agent_tid, "DOM.resolveNode", backendNodeId=node["b"]).get("object", {})
    except RuntimeError as e:
        raise RuntimeError(f"ref {ref} is stale: its element is gone ({e}). Call snapshot() again.") from e
    return node, obj.get("objectId")


def ref_for_agent(agent_tid, query, role=None):
    """First ref whose name matches query (exact match first, then substring).
    query can also be a function of (role, name)."""
    try:
        refs = json.loads(_ref_path(agent_tid).read_text(encoding="utf-8")).get("refs") or {}
    except Exception:
        return None
    items = [(k, n) for k, n in refs.items() if role is None or n.get("role") == role]
    if callable(query):
        return next((f"@{k}" for k, n in items if query(n.get("role"), n.get("name") or "")), None)
    q = str(query).strip().lower()
    for exact in (True, False):
        for k, n in items:
            name = (n.get("name") or "").lower()
            if (name == q) if exact else (q in name):
                return f"@{k}"
    return None


def click_ref_agent(agent_tid, ref):
    node, obj = _resolve_ref(agent_tid, ref)
    try:
        _call(agent_tid, "DOM.scrollIntoViewIfNeeded", backendNodeId=node["b"])
    except RuntimeError:
        pass
    try:
        quads = _call(agent_tid, "DOM.getContentQuads", backendNodeId=node["b"]).get("quads") or []
    except RuntimeError:
        quads = []
    if quads:
        q = quads[0]
        x, y = sum(q[0::2]) / 4, sum(q[1::2]) / 4
        click_at_agent(agent_tid, x, y)
        return {"clicked": ref, "x": round(x), "y": round(y)}
    # No box (zero size or hidden): click through the DOM instead.
    _call(agent_tid, "Runtime.callFunctionOn", objectId=obj, functionDeclaration="function(){this.click()}")
    _touch(agent_tid)
    return {"clicked": ref, "via": "dom"}


def fill_ref_agent(agent_tid, ref, value, submit=False):
    _, obj = _resolve_ref(agent_tid, ref)
    r = _call(agent_tid, "Runtime.callFunctionOn", objectId=obj, functionDeclaration=_FILL_FN,
              arguments=[{"value": value}], returnByValue=True)
    result = (r.get("result") or {}).get("value")
    if result != "filled":
        raise RuntimeError(f"fill_ref {ref}: element is {result}")
    if submit:
        send_keys_agent(agent_tid, ["Enter"])
    _touch(agent_tid)
    return result


# ---------- tab list ----------

def list_agent_tabs():
    state = _load_state()
    pages = {t["targetId"]: t for t in _page_targets()}
    out = []
    for r in state.get("agent_tabs", []):
        info = pages.get(r.get("tid"))
        if info:
            out.append({
                "tid": r["tid"], "url": info.get("url", ""), "title": info.get("title", ""),
                "last_access": r.get("last_access"),
                "mine": r.get("lease_pid") == os.getpid(), "busy": _lease_busy(r),
            })
    return out


def find_agent_tab(url_substring):
    for t in list_agent_tabs():
        if url_substring in t["url"] and not t["busy"]:
            return t["tid"]
    return None


def close_agent_tab(agent_tid):
    if not is_agent_tab(agent_tid):
        return False
    try:
        cdp("Target.closeTarget", targetId=agent_tid)
    except Exception:
        return False
    _forget_tab(agent_tid)
    return True


def close_agent_tabs_matching(url_substring, keep=None):
    """Close free agent tabs whose URL contains url_substring, except keep."""
    closed = 0
    for t in list_agent_tabs():
        if t["tid"] != keep and not t["busy"] and url_substring in t["url"]:
            closed += bool(close_agent_tab(t["tid"]))
    return closed


# ---------- showing the window for a login ----------

def show_window(tid=None):
    """Bring the window that holds tid to the front so the user can type a
    password. Call hide_window() when the user says they are done."""
    wid = ensure_agent_window()
    tid = tid or _BOUND["tid"]
    rec = next((r for r in _load_state().get("agent_tabs", []) if r.get("tid") == tid), None)
    if rec and rec.get("window") and _window_of(tid) == rec["window"]:
        wid = rec["window"]
    ext = _extension(wait=2.0)
    if ext is not None:
        ext.send_command("update_window", windowId=wid, state="normal", focused=True, timeout=5)
    else:
        cdp("Browser.setWindowBounds", windowId=wid, bounds={"windowState": "normal"})
    if tid:
        cdp("Target.activateTarget", targetId=tid)
    return wid


def hide_window():
    """Minimize the agent window and the windows of CDP fallback tabs."""
    wid, _ = find_agent_window()
    windows = ([wid] if wid is not None else []) + _own_windows()
    if not windows:
        return None
    ext = _extension(wait=2.0)
    for w in windows:
        if ext is not None:
            ext.send_command("update_window", windowId=w, state="minimized", timeout=5)
        else:
            cdp("Browser.setWindowBounds", windowId=w, bounds={"windowState": "minimized"})
    return wid
