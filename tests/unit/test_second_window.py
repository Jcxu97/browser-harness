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
import sys
import types

import pytest

from browser_harness import second_window as sw

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
