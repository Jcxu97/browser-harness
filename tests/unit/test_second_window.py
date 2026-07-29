"""detect_second_window() must never hand back the user's main window.

This is the load-bearing invariant of the whole second-window fork: every agent
action is routed at whatever window this function returns, so picking the main
window means inserting tabs into the window the user is actively looking at.

2026-07-30 regression: `if not candidates: candidates = list(cdp_windows.items())`
put the extension-identified main window back into contention. It then only had
to survive the USER_DOMAINS guard — a blocklist that is never complete — and a
tab landed in the user's main window (tabs were m365 + a Tailscale-addressed
sub2api panel, neither of which was listed at the time).
"""
import contextlib
import os
import sys
import time
import types

import pytest

from browser_harness import second_window as sw


@contextlib.contextmanager
def _null_mutex(*a, **k):
    """Stand-in for _state_mutex: these tests are single-process."""
    yield


@pytest.fixture(autouse=True)
def _clean_snapshot_flag(monkeypatch):
    """_LAST_LIST_DROPPED is module state; keep tests from leaking it into each
    other. Default is 0 = complete snapshot."""
    monkeypatch.setattr(sw, "_LAST_LIST_DROPPED", 0)

MAIN, SECOND = 111, 222

USER_TABS = [("t1", "https://www.huya.com/l"), ("t2", "https://www.bilibili.com/")]
CLEAN_TABS = [("t3", "about:blank")]
# Tabs that matched NOTHING in USER_DOMAINS before the fix — the exact shape that
# let the main window pass the guard.
BLOCKLIST_EVADING_TABS = [
    ("t1", "https://m365.cloud.microsoft/chat"),
    ("t2", "http://100.86.104.62:8090/admin/dashboard"),
]


@pytest.fixture
def detect(monkeypatch):
    """Drive detect_second_window() against fake windows and a fake extension."""

    def _run(cdp_windows, ext_focused, pinned=None):
        monkeypatch.setattr(sw, "_list_windows", lambda: cdp_windows)
        monkeypatch.setattr(
            sw, "_load_state",
            lambda: {"pinned_second_window_id": pinned} if pinned else {})
        monkeypatch.setattr(sw, "_save_state", lambda state: None)

        # detect_second_window does `from . import bh_extension_client as ext`
        # inside the function body, so patch the module in sys.modules.
        fake = types.ModuleType("browser_harness.bh_extension_client")
        fake.start_server_if_needed = lambda *a, **k: None
        fake.is_available = lambda: True
        fake.send_command = lambda cmd, **kw: (
            [{"id": w, "focused": (w == ext_focused)} for w in cdp_windows]
            if cmd == "list_windows" else None
        )
        monkeypatch.setitem(sys.modules, "browser_harness.bh_extension_client", fake)

        return sw.detect_second_window()

    return _run


def test_prefers_clean_window_over_user_window(detect):
    wid, _ = detect({MAIN: USER_TABS, SECOND: CLEAN_TABS}, ext_focused=MAIN)
    assert wid == SECOND


def test_pin_short_circuits_heuristics(detect):
    wid, _ = detect({MAIN: USER_TABS, SECOND: CLEAN_TABS},
                    ext_focused=MAIN, pinned=SECOND)
    assert wid == SECOND


def test_refuses_when_every_window_holds_user_content(detect):
    """Better to raise and let the caller spawn than to pollute."""
    with pytest.raises(RuntimeError, match="Refusing to spawn"):
        detect({MAIN: BLOCKLIST_EVADING_TABS, SECOND: BLOCKLIST_EVADING_TABS},
               ext_focused=MAIN)


def test_never_returns_extension_identified_main_window(detect):
    """Extension says MAIN is focused, so MAIN is authoritatively the user's."""
    result = detect({MAIN: BLOCKLIST_EVADING_TABS}, ext_focused=MAIN)
    assert result[0] != MAIN, "detect handed back the user's main window"


def test_main_window_not_picked_when_it_is_the_only_other_candidate(detect):
    """The actual 2026-07-30 regression.

    Two windows exist. The extension flags MAIN as focused, so the only other
    candidate is SECOND — but here SECOND is *also* full of user content, which
    is what the incident looked like (both windows had the sub2api panel open).
    detect must raise rather than pick either one; the caller spawns a clean
    window. Before the fix the Tailscale-addressed panel was absent from
    USER_DOMAINS, both windows scored as work, and a tab was inserted.
    """
    with pytest.raises(RuntimeError, match="Refusing to spawn"):
        detect({MAIN: BLOCKLIST_EVADING_TABS, SECOND: BLOCKLIST_EVADING_TABS},
               ext_focused=MAIN)


def test_single_window_declines_early(detect):
    """One window total means there is no second window to find."""
    assert detect({MAIN: CLEAN_TABS}, ext_focused=MAIN) == (None, None)


def test_dead_pin_is_dropped_without_falling_back_to_main(detect):
    stale = 999
    result = detect({MAIN: BLOCKLIST_EVADING_TABS}, ext_focused=MAIN, pinned=stale)
    assert result[0] != MAIN


@pytest.mark.parametrize("url", [
    "http://100.86.104.62:8090/admin/dashboard",
    "http://127.0.0.1:8090/admin",
    "https://www.bilibili.com/video/BV1",
    "https://www.huya.com/l",
])
def test_user_surfaces_are_recognised(url):
    """The sub2api panel is the user's, over loopback AND over Tailscale."""
    assert sw._count_user_hits([url]) > 0, f"{url} not recognised as user content"


# ---------- _assert_landed_in: the one check that does not rely on guessing ----------

@pytest.fixture
def fake_cdp(monkeypatch):
    """Stub cdp() and record every call, so we can assert on cleanup."""
    calls = []

    def _install(window_for_target, closes_ok=True):
        def fake(method, **kw):
            calls.append((method, kw))
            if method == "Browser.getWindowForTarget":
                if isinstance(window_for_target, Exception):
                    raise window_for_target
                return {"windowId": window_for_target}
            if method == "Target.closeTarget":
                if not closes_ok:
                    raise RuntimeError("close failed")
                return {}
            return {}
        monkeypatch.setattr(sw, "cdp", fake)
        return calls

    return _install


def test_assert_landed_in_accepts_correct_window(fake_cdp):
    calls = fake_cdp(window_for_target=SECOND)
    sw._assert_landed_in("tid-1", SECOND)
    assert not [c for c in calls if c[0] == "Target.closeTarget"], \
        "must not close a tab that landed correctly"


def test_assert_landed_in_closes_and_raises_on_wrong_window(fake_cdp):
    """A tab in the user's window must be closed, not merely reported."""
    calls = fake_cdp(window_for_target=MAIN)
    with pytest.raises(RuntimeError, match="landed in window"):
        sw._assert_landed_in("tid-stray", SECOND)
    closed = [kw["targetId"] for m, kw in calls if m == "Target.closeTarget"]
    assert closed == ["tid-stray"], "stray tab left behind in the user's window"


def test_assert_landed_in_treats_unverifiable_as_failure(fake_cdp):
    """If Chrome won't say where the tab is, assume the worst and clean up."""
    calls = fake_cdp(window_for_target=RuntimeError("cdp down"))
    with pytest.raises(RuntimeError, match="cannot confirm"):
        sw._assert_landed_in("tid-unknown", SECOND)
    assert [kw["targetId"] for m, kw in calls if m == "Target.closeTarget"] == ["tid-unknown"]


def test_assert_landed_in_still_raises_when_cleanup_fails(fake_cdp):
    fake_cdp(window_for_target=MAIN, closes_ok=False)
    with pytest.raises(RuntimeError, match="landed in window"):
        sw._assert_landed_in("tid-stray", SECOND)


# ---------- prune_agent_tabs must never close the user's tabs ----------

def test_prune_only_reaps_tabs_carrying_our_marker(monkeypatch):
    """The second window may double as a window the user works in.

    Regression: prune used to treat "in the pinned window, not in state, not
    about:blank" as reapable — i.e. exactly the user's own tabs. With more than
    max_n of them open it silently closed the overflow.
    """
    pinned = SECOND
    user_tabs = [(f"user-{i}", f"https://news.example.com/{i}", "news") for i in range(6)]
    ours = [("ours-1", f"https://example.com/?{sw.AGENT_TAB_MARKER}=1&bh-nonce=x", "")]
    monkeypatch.setattr(sw, "_list_windows", lambda: {pinned: user_tabs + ours})
    monkeypatch.setattr(sw, "_load_state",
                        lambda: {"pinned_second_window_id": pinned, "agent_tabs": []})
    monkeypatch.setattr(sw, "_save_state", lambda s: None)
    monkeypatch.setattr(sw, "_state_mutex", _null_mutex)

    closed = []
    monkeypatch.setattr(sw, "cdp", lambda method, **kw: (
        closed.append(kw.get("targetId")) if method == "Target.closeTarget" else None) or {})

    sw.prune_agent_tabs(max_n=2)  # 7 tabs vs cap 2 → old code would close 5

    assert all(t.startswith("ours-") for t in closed), \
        f"prune closed the user's tabs: {[t for t in closed if not t.startswith('ours-')]}"


# ---------- lock ownership: a slow holder must not lose its lock ----------

def test_lock_holder_alive_reads_the_recorded_pid(tmp_path, monkeypatch):
    lock = tmp_path / "x.lock"
    lock.write_text(f"{os.getpid()}\n{time.time()}", encoding="utf-8")
    assert sw._lock_holder_alive(lock) is True

    lock.write_text("999999999\n0", encoding="utf-8")
    monkeypatch.setattr(sw, "_pid_alive", lambda pid: pid == os.getpid())
    assert sw._lock_holder_alive(lock) is False, \
        "a provably dead holder should be reapable"


def test_unreadable_lock_is_treated_as_held(tmp_path):
    """Can't verify → don't steal. Age gating already limits the damage."""
    lock = tmp_path / "x.lock"
    lock.write_text("not-a-pid", encoding="utf-8")
    assert sw._lock_holder_alive(lock) is True


def test_release_leaves_a_reassigned_lock_alone(tmp_path):
    """The bug this prevents: our finally: deleting somebody else's lock.

    Sequence: we hold the lock, we stall, a reaper decides we're dead and takes
    it, then we wake up and run our finally. Deleting it there would let a third
    process in while the reaper is still inside its critical section.
    """
    lock = tmp_path / "x.lock"
    lock.write_text("424242\n0", encoding="utf-8")  # someone else's pid
    sw._release_lock_if_mine(lock)
    assert lock.exists(), "released a lock that had been reassigned"


def test_release_removes_our_own_lock(tmp_path):
    lock = tmp_path / "x.lock"
    lock.write_text(f"{os.getpid()}\n{time.time()}", encoding="utf-8")
    sw._release_lock_if_mine(lock)
    assert not lock.exists()


# ---------- _pid_alive: uncertainty must read as alive ----------

def test_pid_alive_self():
    assert sw._pid_alive(os.getpid()) is True


def test_pid_alive_rejects_falsy():
    assert sw._pid_alive(0) is False
    assert sw._pid_alive(None) is False


def test_pid_alive_says_alive_when_it_cannot_tell(monkeypatch):
    """Docstring promised conservative-on-uncertainty; implementation didn't.

    A live process at a different integrity level denies OpenProcess, and the old
    code read that denial as "dead" — which let callers steal its lock and
    reclaim its tabs.
    """
    if os.name != "nt":
        pytest.skip("windows-specific path")

    import ctypes
    ERROR_ACCESS_DENIED = 5

    class FakeK32:
        def SetLastError(self, _): pass
        def OpenProcess(self, *a): return 0          # denied
        def GetLastError(self): return ERROR_ACCESS_DENIED
    monkeypatch.setattr(ctypes, "windll",
                        types.SimpleNamespace(kernel32=FakeK32()))
    assert sw._pid_alive(4) is True, "access-denied must not read as dead"


# ---------- fail-closed on incomplete data ----------

def test_detect_refuses_to_score_an_incomplete_snapshot(detect, monkeypatch):
    """One CDP hiccup must not walk past the blocklist.

    _list_windows drops tabs whose window it can't resolve. If the dropped tab
    was the bilibili one, the user's main window scores as user-content-free and
    becomes an eligible candidate. So a partial snapshot disqualifies scoring.
    """
    monkeypatch.setattr(sw, "_LAST_LIST_DROPPED", 2)
    assert detect({MAIN: USER_TABS, SECOND: CLEAN_TABS}, ext_focused=MAIN) == (None, None)


def test_list_windows_reports_dropped_tabs(monkeypatch):
    def fake_cdp(method, **kw):
        if method == "Target.getTargets":
            return {"targetInfos": [
                {"type": "page", "targetId": "ok", "url": "https://a.example", "title": ""},
                {"type": "page", "targetId": "bad", "url": "https://b.example", "title": ""},
            ]}
        if method == "Browser.getWindowForTarget":
            if kw["targetId"] == "bad":
                raise RuntimeError("cannot resolve")
            return {"windowId": SECOND}
        return {}

    monkeypatch.setattr(sw, "cdp", fake_cdp)
    windows = sw._list_windows()
    assert windows == {SECOND: [("ok", "https://a.example", "")]}
    assert sw._LAST_LIST_DROPPED == 1, "unplaceable tab not reported"


def test_orphan_in_unknown_window_is_not_adopted(monkeypatch):
    """`second_window_tids is None` means "couldn't check", not "empty window".

    Behavioural check: an unclaimed record whose tab is live but whose window
    membership cannot be established must NOT be adopted. The old guard
    (`not second_window_tids or tid in second_window_tids`) evaporated in exactly
    that case, degrading into "adopt an orphan from any window" — the user's
    included. Here _list_windows raises, so membership is unknowable; the orphan
    must be passed over and a fresh spawn attempted instead.
    """
    orphan_tid = "orphan-1"
    state = {"pinned_second_window_id": SECOND,
             "agent_tabs": [{"tid": orphan_tid, "nonce": "n"}]}  # no claim → orphan

    monkeypatch.setattr(sw, "detect_second_window", lambda: (SECOND, CLEAN_TABS))
    monkeypatch.setattr(sw, "_load_state", lambda: state)
    monkeypatch.setattr(sw, "_save_state", lambda s: None)
    monkeypatch.setattr(sw, "_state_mutex", _null_mutex)
    monkeypatch.setattr(sw, "_capture_user_main_window", lambda: None)
    monkeypatch.setattr(sw, "_smart_focus_main_window", lambda snap: None)
    monkeypatch.setattr(sw, "prune_agent_tabs", lambda **k: 0)
    monkeypatch.setattr(sw, "_gc_orphan_claims", lambda *a, **k: None)

    def boom():
        raise RuntimeError("cannot list windows")
    monkeypatch.setattr(sw, "_list_windows", boom)

    # The orphan's tab is live, so only the membership guard can rule it out.
    def fake_cdp(method, **kw):
        if method == "Target.getTargets":
            return {"targetInfos": [{"type": "page", "targetId": orphan_tid,
                                     "url": "https://example.com/?bh-agent-tab=1"}]}
        raise RuntimeError("spawn path not exercised in this test")
    monkeypatch.setattr(sw, "cdp", fake_cdp)

    # Must not return the orphan. Any other outcome (exception from the spawn
    # path we deliberately broke) is acceptable — the point is it wasn't adopted.
    try:
        got = sw.ensure_agent_tab(prefer_extension=False)
    except Exception:
        got = None
    assert got != orphan_tid, "adopted an orphan whose window membership was unknown"
