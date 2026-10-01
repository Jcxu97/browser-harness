"""second_window must keep every agent action inside the window that BH created.

The fake browser below stands in for Chrome behind the daemon. USER_WID is the
user's own window; nothing in these tests may open, close, focus or drive a tab
there.
"""
import itertools
import os
import time
import types

import pytest

from browser_harness import second_window as sw

USER_WID = 100
OTHER_PID = 424242


class FakeBrowser:
    def __init__(self):
        self.windows = {USER_WID: ["u1", "u2"]}
        self.pages = {
            "u1": {"url": "https://www.bilibili.com/video/1", "title": "video", "doc": 1},
            "u2": {"url": "https://mail.example.org/", "title": "mail", "doc": 1},
        }
        self.sessions = {}
        self.closed, self.clicks, self.bounds, self.activated = [], [], [], []
        self.keys = []
        self.created, self.window_opens = [], []
        self.ext_calls = []
        self.ax_nodes = []
        self._ids = itertools.count(1)

    # -- topology --
    def add_window(self, urls):
        wid = 1000 + next(self._ids)
        self.windows[wid] = []
        for url in urls:
            self.add_tab(wid, url)
        return wid

    def add_tab(self, wid, url):
        tid = f"t{next(self._ids)}"
        self.windows[wid].append(tid)
        self.pages[tid] = {"url": url, "title": "", "doc": 1}
        return tid

    def window_of(self, tid):
        return next((w for w, tids in self.windows.items() if tid in tids), None)

    def move(self, tid, wid):
        self.windows[self.window_of(tid)].remove(tid)
        self.windows[wid].append(tid)

    def tab_of(self, sid):
        tid = self.sessions.get(sid)
        if tid not in self.pages:
            raise RuntimeError("Session with given id not found")
        return tid

    # -- CDP --
    def cdp(self, method, session_id=None, _response_timeout=None, **p):
        if method == "Target.getTargets":
            return {"targetInfos": [{"targetId": t, "type": "page", "url": v["url"], "title": v["title"]}
                                    for t, v in self.pages.items()]}
        if method in ("Browser.getWindowForTarget", "Target.getTargetInfo", "Target.attachToTarget",
                      "Target.closeTarget", "Target.activateTarget"):
            tid = p.get("targetId")
            if tid not in self.pages:
                raise RuntimeError("No target with given id found")
            if method == "Browser.getWindowForTarget":
                return {"windowId": self.window_of(tid)}
            if method == "Target.getTargetInfo":
                return {"targetInfo": {"targetId": tid, "type": "page", **self.pages[tid]}}
            if method == "Target.attachToTarget":
                sid = f"s{next(self._ids)}"
                self.sessions[sid] = tid
                return {"sessionId": sid}
            if method == "Target.activateTarget":
                self.activated.append(tid)
                return {}
            self.closed.append(tid)
            self.windows[self.window_of(tid)].remove(tid)
            del self.pages[tid]
            return {}
        if method == "Target.detachFromTarget":
            self.sessions.pop(p.get("sessionId"), None)
            return {}
        if method == "Target.createTarget":
            self.created.append(p)
            wid = self.add_window([p["url"]]) if p.get("newWindow") else USER_WID
            if wid == USER_WID:
                self.add_tab(USER_WID, p["url"])
            return {"targetId": self.windows[wid][-1]}
        if method == "Browser.setWindowBounds":
            self.bounds.append((p["windowId"], p["bounds"]))
            return {}
        tid = self.tab_of(session_id) if session_id else None
        page = self.pages.get(tid, {})
        if method == "Page.navigate":
            page.update(url=p["url"], doc=page["doc"] + 1)
            return {"frameId": "f", "loaderId": "l"}
        if method == "Runtime.evaluate":
            expr = p["expression"]
            if expr.startswith("window.open("):
                url = expr.split('"')[1]
                self.window_opens.append(url)
                self.add_tab(self.window_of(tid), url)
                return {"result": {}}
            if expr == "performance.timeOrigin":
                return {"result": {"value": page["doc"]}}
            if expr == "[performance.timeOrigin, document.readyState]":
                return {"result": {"value": [page["doc"], "complete"]}}
            return {"result": {"type": "undefined"}}
        if method == "Input.dispatchMouseEvent":
            self.clicks.append((tid, p["type"], p["x"], p["y"]))
            return {}
        if method == "Input.dispatchKeyEvent":
            self.keys.append((p["type"], p.get("key") or p.get("text")))
            return {}
        if method == "Accessibility.getFullAXTree":
            return {"nodes": self.ax_nodes}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": f"obj-{p['backendNodeId']}"}}
        if method == "DOM.getContentQuads":
            return {"quads": [[10, 20, 30, 20, 30, 40, 10, 40]]}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": "filled"}}
        return {}

    # -- companion extension --
    def ext_send(self, action, timeout=None, **p):
        self.ext_calls.append((action, p))
        if action == "create_window":
            wid = self.add_window([p["url"]])
            return {"ok": True, "windowId": wid}
        if action == "create_tab":
            tid = self.add_tab(p["windowId"], p["url"])
            return {"tabId": int(tid[1:]), "windowId": p["windowId"]}
        return {"ok": True}

    def daemon(self, req, response_timeout=None):
        """Stand-in for helpers._raw_send (daemon metas only)."""
        return {"targetId": "u1"} if req.get("meta") == "current_tab" else {}


@pytest.fixture
def chrome(tmp_path, monkeypatch):
    b = FakeBrowser()
    monkeypatch.setattr(sw, "STATE_DIR", tmp_path)
    monkeypatch.setattr(sw, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(sw, "REF_DIR", tmp_path / "refs")
    monkeypatch.setattr(sw, "_STATE_LOCK_PATH", tmp_path / "state.lock")
    monkeypatch.setattr(sw, "_SPAWN_LOCK_PATH", tmp_path / "spawn.lock")
    monkeypatch.setattr(sw, "cdp", b.cdp)
    monkeypatch.setattr(sw, "_raw_send", b.daemon)
    ext = types.SimpleNamespace(send_command=b.ext_send)
    b.ext = ext
    monkeypatch.setattr(sw, "_extension", lambda wait=5.0: b.ext)
    monkeypatch.setattr(sw, "_owner_alive", lambda owner: True)
    monkeypatch.setattr(sw, "_pid_alive", lambda pid: pid in (os.getpid(), OTHER_PID))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "me")
    for name, value in (("_SESSIONS", {}), ("_EXTRA_SESSIONS", set()), ("_BOUND", {"tid": None}),
                        ("_DAEMON_REPOINTED", [False]), ("_EXIT_REGISTERED", [True]),
                        ("_SPAWNED", set()), ("_NAVIGATED", set()), ("_LAST_TOUCH", {}),
                        ("_WINDOW_CACHE", {})):
        monkeypatch.setattr(sw, name, value)
    return b


def _state():
    return sw._load_state()


def _set_records(*records):
    s = _state()
    s["agent_tabs"] = list(records)
    sw._save_state(s)


# ---------- the agent window ----------

def test_window_is_never_guessed_from_content(chrome):
    """A window that looks like an old agent window is still not ours."""
    lookalike = chrome.add_window([f"{sw.AGENT_SPAWN_URL}&bh-nonce=old"])
    wid = sw.ensure_agent_window()
    assert wid not in (USER_WID, lookalike)
    assert chrome.ext_calls[0] == ("create_window", {
        "url": chrome.pages[chrome.windows[wid][0]]["url"], "state": "minimized", "focused": False})


def test_window_is_reused_while_its_anchor_lives(chrome):
    wid = sw.ensure_agent_window()
    assert sw.ensure_agent_window() == wid
    assert [c for c, _ in chrome.ext_calls].count("create_window") == 1


def test_window_is_found_again_after_its_id_changes(chrome):
    """Chrome gives restored windows new ids; the anchor URL still names ours."""
    wid = sw.ensure_agent_window()
    anchor = chrome.windows[wid][0]
    new_wid = chrome.add_window([])
    chrome.move(anchor, new_wid)
    assert sw.ensure_agent_window() == new_wid


def test_cdp_fallback_minimizes_the_new_window(chrome):
    chrome.ext = None
    wid = sw.ensure_agent_window()
    assert (wid, {"windowState": "minimized"}) in chrome.bounds


# ---------- agent tabs and leases ----------

def test_new_tab_opens_in_the_agent_window_without_activation(chrome):
    tid = sw.ensure_agent_tab()
    wid = sw.ensure_agent_window()
    assert chrome.window_of(tid) == wid
    action, params = chrome.ext_calls[-1]
    assert action == "create_tab" and params["windowId"] == wid and params["active"] is False


def test_cdp_fallback_tab_gets_a_minimized_window_of_its_own(chrome):
    """In real Chrome, window.open() restores and activates the minimized agent
    window (seen 2026-10-01). Without the extension, use a new minimized window."""
    wid = sw.ensure_agent_window()
    chrome.ext = None
    tid = sw.ensure_agent_tab()
    own = chrome.window_of(tid)
    assert own not in (USER_WID, wid) and not chrome.window_opens
    assert chrome.created[-1] == {"url": chrome.pages[tid]["url"], "newWindow": True,
                                  "background": True, "windowState": "minimized"}
    assert sw.is_agent_tab(tid) and sw.ensure_agent_tab() == tid
    infos = _policy(chrome, "Target.getTargets")["result"]["targetInfos"]
    assert [t["targetId"] for t in infos] == [tid]
    assert len(chrome.windows[USER_WID]) == 2


def test_cdp_fallback_window_closes_when_the_process_exits(chrome):
    sw.ensure_agent_window()
    chrome.ext = None
    tid = sw.new_agent_tab("https://site.example/page")
    sw._exit_cleanup()
    assert tid in chrome.closed
    assert not [r for r in _state()["agent_tabs"] if r["tid"] == tid]


def test_show_and_hide_act_on_the_window_that_holds_the_tab(chrome):
    wid = sw.ensure_agent_window()
    chrome.ext = None
    tid = sw.ensure_agent_tab()
    own = chrome.window_of(tid)
    sw.show_window(tid)
    assert chrome.bounds[-1] == (own, {"windowState": "normal"}) and chrome.activated == [tid]
    n = len(chrome.bounds)
    sw.hide_window()
    hidden = chrome.bounds[n:]
    assert (wid, {"windowState": "minimized"}) in hidden and (own, {"windowState": "minimized"}) in hidden


def test_parallel_process_does_not_get_a_busy_tab(chrome):
    wid = sw.ensure_agent_window()
    busy = chrome.add_tab(wid, "https://site.example/a")
    _set_records({"tid": busy, "claimed_by": "session:me", "lease_pid": OTHER_PID,
                  "last_access": time.time()})
    assert sw.ensure_agent_tab() != busy


def test_same_session_gets_its_free_tab_back(chrome):
    wid = sw.ensure_agent_window()
    mine = chrome.add_tab(wid, "https://site.example/page")
    _set_records({"tid": mine, "claimed_by": "session:me", "lease_pid": 999999,
                  "last_access": time.time()})
    assert sw.ensure_agent_tab() == mine
    assert not [c for c, _ in chrome.ext_calls if c == "create_tab"]


def test_other_live_session_keeps_its_idle_tab(chrome):
    wid = sw.ensure_agent_window()
    theirs = chrome.add_tab(wid, "https://site.example/theirs")
    _set_records({"tid": theirs, "claimed_by": "session:other", "lease_pid": None,
                  "last_access": time.time()})
    assert sw.ensure_agent_tab() != theirs


def test_tab_moved_to_the_user_window_is_left_alone(chrome):
    wid = sw.ensure_agent_window()
    tab = chrome.add_tab(wid, "https://site.example/kept")
    _set_records({"tid": tab, "claimed_by": "session:me", "last_access": time.time()})
    chrome.move(tab, USER_WID)
    assert sw.ensure_agent_tab() != tab
    assert tab not in chrome.closed


def test_prefer_url_picks_the_matching_tab(chrome):
    wid = sw.ensure_agent_window()
    a = chrome.add_tab(wid, "https://a.example/")
    b = chrome.add_tab(wid, "https://m365.cloud.microsoft/chat")
    now = time.time()
    _set_records({"tid": a, "claimed_by": "session:me", "last_access": now},
                 {"tid": b, "claimed_by": "session:me", "last_access": now - 100})
    assert sw.ensure_agent_tab(prefer_url="m365.cloud.microsoft") == b


# ---------- pruning ----------

def test_prune_closes_only_free_agent_tabs(chrome):
    wid = sw.ensure_agent_window()
    now = time.time()
    busy = chrome.add_tab(wid, "https://site.example/busy")
    old = chrome.add_tab(wid, "https://site.example/old")
    new = chrome.add_tab(wid, "https://site.example/new")
    _set_records({"tid": busy, "lease_pid": OTHER_PID, "last_access": now - 50},
                 {"tid": old, "last_access": now - 100},
                 {"tid": new, "last_access": now})
    sw.prune_agent_tabs(max_n=1)
    assert old in chrome.closed and busy not in chrome.closed
    assert not set(chrome.closed) & {"u1", "u2"}


def test_prune_reaps_only_old_lost_placeholders_in_the_agent_window(chrome):
    wid = sw.ensure_agent_window()
    old_ms = int((time.time() - 600) * 1000)
    new_ms = int(time.time() * 1000)
    lost_old = chrome.add_tab(wid, f"{sw.AGENT_SPAWN_URL}&bh-nonce=pid-1-{old_ms}-aa")
    lost_new = chrome.add_tab(wid, f"{sw.AGENT_SPAWN_URL}&bh-nonce=pid-1-{new_ms}-bb")
    user_copy = chrome.add_tab(USER_WID, f"{sw.AGENT_SPAWN_URL}&bh-nonce=pid-1-{old_ms}-cc")
    sw.prune_agent_tabs()
    assert lost_old in chrome.closed
    assert lost_new not in chrome.closed and user_copy not in chrome.closed


# ---------- spawn verification ----------

def test_assert_landed_in_closes_a_tab_in_the_wrong_window(chrome):
    stray = chrome.add_tab(USER_WID, "https://x.example/")
    with pytest.raises(RuntimeError, match="not in the agent window"):
        sw._assert_landed_in(stray, 999)
    assert stray in chrome.closed


def test_assert_landed_in_treats_unverifiable_as_failure(chrome):
    with pytest.raises(RuntimeError, match="cannot confirm"):
        sw._assert_landed_in("gone", 999)


# ---------- locks and pids ----------

def test_lock_holder_alive_reads_the_recorded_pid(tmp_path, monkeypatch):
    lock = tmp_path / "x.lock"
    lock.write_text(f"{os.getpid()}\n{time.time()}", encoding="utf-8")
    assert sw._lock_holder_alive(lock) is True
    lock.write_text("999999999\n0", encoding="utf-8")
    monkeypatch.setattr(sw, "_pid_alive", lambda pid: pid == os.getpid())
    assert sw._lock_holder_alive(lock) is False


def test_unreadable_lock_is_treated_as_held(tmp_path):
    lock = tmp_path / "x.lock"
    lock.write_text("not-a-pid", encoding="utf-8")
    assert sw._lock_holder_alive(lock) is True


def test_release_leaves_a_reassigned_lock_alone(tmp_path):
    lock = tmp_path / "x.lock"
    lock.write_text("424242\n0", encoding="utf-8")
    sw._release_lock_if_mine(lock)
    assert lock.exists()


def test_release_removes_our_own_lock(tmp_path):
    lock = tmp_path / "x.lock"
    lock.write_text(f"{os.getpid()}\n{time.time()}", encoding="utf-8")
    sw._release_lock_if_mine(lock)
    assert not lock.exists()


def test_pid_alive_self_and_falsy():
    assert sw._pid_alive(os.getpid()) is True
    assert sw._pid_alive(0) is False and sw._pid_alive(None) is False


def test_pid_alive_says_alive_when_it_cannot_tell(monkeypatch):
    if os.name != "nt":
        pytest.skip("windows-specific path")
    import ctypes

    class FakeK32:
        def SetLastError(self, _): pass
        def OpenProcess(self, *a): return 0
        def GetLastError(self): return 5  # ERROR_ACCESS_DENIED
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(kernel32=FakeK32()))
    assert sw._pid_alive(4) is True


# ---------- safe-mode request policy ----------

def _forward(chrome, sent=None):
    def forward(req):
        if sent is not None:
            sent.append(req)
        if req.get("meta"):
            return chrome.daemon(req)
        return {"result": chrome.cdp(req["method"], session_id=req.get("session_id"), **req["params"])}
    return forward


def _policy(chrome, method, sent=None, **params):
    return sw.request_policy({"method": method, "params": params, "session_id": None},
                             _forward(chrome, sent))


def test_sessionless_page_call_runs_on_the_agent_tab(chrome):
    sent = []
    _policy(chrome, "Input.dispatchMouseEvent", sent, type="mousePressed", x=1, y=2)
    tid = sw._BOUND["tid"]
    assert chrome.window_of(tid) == sw.ensure_agent_window()
    assert chrome.sessions[sent[-1]["session_id"]] == tid
    assert chrome.clicks == [(tid, "mousePressed", 1, 2)]


@pytest.mark.parametrize("method", ["Target.activateTarget", "Browser.close", "Browser.crash"])
def test_focus_changes_and_browser_close_are_refused(chrome, method):
    with pytest.raises(RuntimeError):
        _policy(chrome, method, targetId="u1")
    assert not chrome.activated


def test_bring_to_front_is_ignored(chrome):
    assert _policy(chrome, "Page.bringToFront") == {"result": {}}


def test_create_target_opens_an_agent_tab(chrome):
    tid = _policy(chrome, "Target.createTarget", url="about:blank")["result"]["targetId"]
    assert chrome.window_of(tid) == sw.ensure_agent_window()
    assert len(chrome.windows[USER_WID]) == 2


@pytest.mark.parametrize("method", ["Target.closeTarget", "Target.attachToTarget"])
def test_user_tabs_cannot_be_closed_or_attached(chrome, method):
    with pytest.raises(RuntimeError, match="not a tab in the agent window"):
        _policy(chrome, method, targetId="u1")
    assert "u1" not in chrome.closed


def test_window_bounds_of_the_user_window_are_refused(chrome):
    with pytest.raises(RuntimeError):
        _policy(chrome, "Browser.setWindowBounds", windowId=USER_WID, bounds={"windowState": "minimized"})


def test_target_list_shows_only_agent_tabs(chrome):
    tid = sw.ensure_agent_tab()
    infos = _policy(chrome, "Target.getTargets")["result"]["targetInfos"]
    assert [t["targetId"] for t in infos] == [tid]


def test_current_tab_and_session_meta_name_the_agent_tab(chrome):
    forward = _forward(chrome)
    cur = sw.request_policy({"meta": "current_tab"}, forward)
    assert cur["targetId"] == sw._BOUND["tid"] != "u1"
    sid = sw.request_policy({"meta": "session"}, forward)["session_id"]
    assert chrome.sessions[sid] == cur["targetId"]


def test_switching_to_a_user_tab_is_refused(chrome):
    with pytest.raises(RuntimeError):
        sw.request_policy({"meta": "set_session", "session_id": "x", "target_id": "u1"}, _forward(chrome))


def test_closed_tab_is_replaced_but_only_navigation_is_repeated(chrome):
    first = sw.bound_tab()
    chrome.cdp("Target.closeTarget", targetId=first)
    _policy(chrome, "Page.navigate", url="https://site.example/")
    second = sw._BOUND["tid"]
    assert second != first and chrome.pages[second]["url"] == "https://site.example/"
    chrome.cdp("Target.closeTarget", targetId=second)
    with pytest.raises(RuntimeError, match="not repeated"):
        _policy(chrome, "Input.dispatchMouseEvent", type="mousePressed", x=1, y=1)
    assert not chrome.clicks


def test_page_error_text_is_not_read_as_a_closed_tab():
    assert not sw.is_target_gone(RuntimeError("JavaScript evaluation failed: Target closed"))
    assert sw.is_target_gone(RuntimeError("No target with given id found"))


def test_daemon_default_session_moves_off_the_user_tab(chrome, monkeypatch):
    sent = []

    def daemon(req, response_timeout=None):
        sent.append(req)
        return chrome.daemon(req)
    monkeypatch.setattr(sw, "_raw_send", daemon)
    sw.bound_tab()
    moved = [r for r in sent if r.get("meta") == "set_session"]
    wid, anchor = sw.find_agent_window()
    assert moved and moved[0]["target_id"] == anchor


# ---------- accessibility snapshot refs ----------

def _ax(node_id, role, name, backend, children=(), parent=None):
    n = {"nodeId": node_id, "role": {"value": role}, "name": {"value": name},
         "backendDOMNodeId": backend, "childIds": list(children)}
    if parent:
        n["parentId"] = parent
    return n


def test_snapshot_refs_work_in_a_later_process_until_the_page_changes(chrome, monkeypatch):
    tid = sw.ensure_agent_tab()
    chrome.ax_nodes = [_ax("1", "RootWebArea", "", 1, ["2", "3"]),
                       _ax("2", "button", "Sign in", 42, parent="1"),
                       _ax("3", "generic", "", 43, parent="1")]
    text = sw.snapshot_tree_agent(tid)
    assert text == '@e1 button "Sign in"'
    monkeypatch.setattr(sw, "_SESSIONS", {})  # a new process has no sessions yet
    assert sw.ref_for_agent(tid, "sign in") == "@e1"
    assert sw.click_ref_agent(tid, "@e1") == {"clicked": "@e1", "x": 20, "y": 30}
    sw.navigate_agent(tid, "https://site.example/next")
    with pytest.raises(RuntimeError, match="stale"):
        sw.click_ref_agent(tid, "@e1")


# ---------- exit cleanup ----------

def test_exit_closes_unused_placeholders_and_releases_leases(chrome):
    unused = sw.ensure_agent_tab()
    used = sw.new_agent_tab("https://site.example/kept")
    sw._exit_cleanup()
    assert unused in chrome.closed and used not in chrome.closed
    rec = next(r for r in _state()["agent_tabs"] if r["tid"] == used)
    assert rec["lease_pid"] is None


def test_send_keys_takes_a_named_key_as_one_key(chrome):
    tid = sw.ensure_agent_tab()
    sw.send_keys_agent(tid, "Enter")
    assert [k for k in chrome.keys if k[0] == "rawKeyDown"] == [("rawKeyDown", "Enter")]
    chrome.keys.clear()
    sw.send_keys_agent(tid, "ab")
    assert [k for k in chrome.keys if k[0] == "keyDown"] == [("keyDown", "a"), ("keyDown", "b")]


@pytest.mark.parametrize("first", ["browser_harness.second_window", "browser_harness.helpers",
                                   "browser_harness.image_gen"])
def test_safe_mode_installs_whatever_module_loads_first(first):
    """Under pytest the policy is not installed, so run a real interpreter.
    second_window imports helpers, and helpers installs a policy from
    second_window: importing second_window first must not hit that cycle."""
    import pathlib
    import subprocess
    import sys
    src = str(pathlib.Path(__file__).resolve().parents[2] / "src")
    code = (f"import {first}\n"
            "import browser_harness.helpers as h\n"
            "assert h._REQUEST_POLICY is not None\n")
    env = {**os.environ, "PYTHONPATH": src}
    env.pop("BH_SAFE_MODE", None)
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
