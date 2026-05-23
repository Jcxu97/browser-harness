"""
BH safe-mode bootstrap (PreExec hook injected by run.py before exec'ing user code).

Goal: make agent-authored stdin code safe-by-default. Old helpers (new_tab,
goto_url, click_at_xy, etc.) operate on whichever Chrome window happens to be
focused, which is usually the user's main window. The agent has hard rules
("never pollute user main window") but in practice forgets and writes
new_tab(url) out of habit (SKILL.md's first example used new_tab).

This module:
  1. ensure_pinned_second_window() — guarantees a pinned second window exists,
     spawning one if necessary.
  2. safe_globals() — returns a dict of helpers bound to a pinned agent tab.
     Legacy names new_tab/goto_url are intentionally REPLACED with raisers
     rather than silent-shadowed: their behavior under safe-mode would diverge
     from the original (no new tab is created, current agent_tab is reused),
     and silent divergence is worse than a clear error.
  3. atexit hook — when this BH process exits, close any tab THIS PID claimed
     that's still on a BH spawn placeholder URL (agent never navigated away).
     PID-scoped so concurrent BH sessions (e.g. main thread + sub-agent) don't
     trample each other.

Disable with BH_SAFE_MODE=0 (escape hatch for tasks that genuinely need to
operate on the user's main window — currently none known).
Disable just the atexit cleanup with BH_KEEP_PLACEHOLDERS=1.
"""
from __future__ import annotations

import atexit
import os

from . import second_window as _sw
from .helpers import cdp as _cdp


def ensure_pinned_second_window():
    """Always return a wid for a valid pinned second window.

    Resolution order:
      1. Existing pinned wid still alive -> return it.
      2. detect_second_window() finds a non-user candidate -> pin and return.
      3. Otherwise spawn_second_window() then pin and return.

    Never returns None. Never returns the user's main window (detect_second_window
    raises rather than picking a polluted candidate; we catch and spawn fresh).
    """
    state = _sw._load_state()
    pinned = state.get("pinned_second_window_id")
    live = _sw._list_windows()
    if pinned and pinned in live:
        return pinned

    try:
        wid, _tabs = _sw.detect_second_window()
    except RuntimeError:
        wid = None

    if not wid:
        wid = _sw.spawn_second_window()

    _sw.pin_second_window(wid)
    return wid


def _close_placeholder_tabs():
    """Close placeholder tabs that THIS PID claimed but never navigated away from.

    A placeholder is identified by the per-spawn NONCE we baked into the spawn
    URL and persisted to state. If the live URL still contains that nonce, the
    agent never called goto(real_url) — orphan junk, close it.

    NONCE-BASED (not substring-marker): an earlier draft used
    `AGENT_TAB_MARKER in url`, but that matched any URL containing
    'bh-agent-tab' anywhere — so an agent legitimately visiting
    https://example.com/?bh-agent-tab=spoof would get its tab killed at exit
    (test scenario I05). Per-tab nonce match is exact and immune to URL
    spoofing.

    PID-SCOPED: we only close tabs claimed by os.getpid() in the state file.
    Concurrent BH sessions (main thread + sub-agent in parallel) each clean up
    only their own placeholders; we never close another session's tab — see
    `reference_browser_session_isolation` in user memory for why this matters
    (the 2026-05-20 doubao-vs-Nexus tab-poaching incident).

    Tabs the agent navigated to a real URL are LEFT ALONE — they may be useful
    for the next session to reuse, and the user may want to see their final
    state. Tabs without a stored nonce (legacy state from before this fix) are
    also left alone — the cap will eventually evict them.

    Disable with BH_KEEP_PLACEHOLDERS=1.
    """
    if os.environ.get("BH_KEEP_PLACEHOLDERS") == "1":
        return
    try:
        import time as _time
        my_pid = os.getpid()
        state = _sw._load_state()
        my_records = {
            r["tid"]: r.get("nonce")
            for r in state.get("agent_tabs", [])
            if r.get("claimed_by_pid") == my_pid and r.get("tid")
        }
        if not my_records:
            return

        # Brief retry: if BH ran a tiny script (e.g. just `print("hi")`),
        # exec can return BEFORE the placeholder URL finishes loading. The
        # targetInfo url field then doesn't yet contain our nonce, and we'd
        # incorrectly skip closing. Retry up to ~1.5s for the URL to populate.
        to_close = {}  # tid -> nonce
        deadline = _time.time() + 1.5
        while _time.time() < deadline:
            targets = _cdp("Target.getTargets").get("targetInfos", [])
            live_tids = set()
            for t in targets:
                if t.get("type") != "page":
                    continue
                tid = t.get("targetId")
                if tid not in my_records:
                    continue
                live_tids.add(tid)
                nonce = my_records[tid]
                if not nonce:
                    continue  # legacy record (pre-nonce-fix) — leave alone
                url = t.get("url") or ""
                if nonce in url:
                    to_close[tid] = nonce
            # Done if every alive my-tid has either matched (in to_close) or
            # is decided as not-a-placeholder (URL is real and nonce-free).
            unresolved = [tid for tid in (my_records.keys() & live_tids) if tid not in to_close]
            if not unresolved:
                break
            # Some tabs still loading — give them a moment
            unresolved_records = {tid: my_records[tid] for tid in unresolved if my_records[tid]}
            if not unresolved_records:
                break  # only legacy records left
            _time.sleep(0.2)

        for tid in to_close:
            try:
                _cdp("Target.closeTarget", targetId=tid)
            except Exception:
                pass  # best-effort
    except Exception:
        pass  # bootstrap cleanup must never break BH


def _legacy_new_tab(*_a, **_k):
    raise RuntimeError(
        "new_tab() is disabled under safe-mode (BH_SAFE_MODE=1). It used to "
        "create a *new* tab; under safe-mode the agent operates on a single "
        "pinned agent_tab in the second window. Use goto(url) to navigate "
        "agent_tab. If you really need a separate tab, that's a design "
        "smell — bundle the work into one tab, or set BH_SAFE_MODE=0."
    )


def _legacy_goto_url(url, **kw):
    # goto_url's old semantics (navigate the focused tab) are exactly what
    # safe-mode forbids. Redirect to safe goto() — same single-tab outcome,
    # just on agent_tab instead of whatever was focused.
    raise RuntimeError(
        "goto_url() is disabled under safe-mode. Use goto(url) — it navigates "
        "the bound agent_tab in the second window."
    )


_atexit_registered = False


def safe_globals():
    """Bind a fresh agent tab to convenience helpers, return as dict for globals().update()."""
    global _atexit_registered
    ensure_pinned_second_window()
    tab = _sw.ensure_agent_tab()
    if not _atexit_registered:
        atexit.register(_close_placeholder_tabs)
        _atexit_registered = True

    def goto(url, timeout=15):
        return _sw.navigate_agent(tab, url, timeout=timeout)

    def eval_js(expression):
        return _sw.evaluate_agent(tab, expression)

    def snap(max_chars=10000):
        return _sw.snapshot_agent(tab, max_chars=max_chars)

    def shot(path):
        return _sw.screenshot_agent(tab, path)

    def click_at(x, y, button="left"):
        return _sw.click_at_agent(tab, x, y, button=button)

    def type_text(text):
        return _sw.key_type_agent(tab, text)

    def send_keys(keys):
        return _sw.send_keys_agent(tab, keys)

    def fill(selector, value):
        return _sw.fill_agent(tab, selector, value)

    def upload(selector, file_paths):
        return _sw.upload_agent(tab, selector, file_paths)

    def close_tab():
        return _sw.close_agent_tab(tab)

    return {
        # bound state
        "agent_tab": tab,
        # safe primary API — what agents should write
        "goto": goto,
        "eval_js": eval_js,
        "snap": snap,
        "shot": shot,
        "click_at": click_at,
        "type_text": type_text,
        "send_keys": send_keys,
        "fill": fill,
        "upload": upload,
        "close_tab": close_tab,
        # Legacy names: raise loudly instead of silently changing semantics.
        "new_tab": _legacy_new_tab,
        "goto_url": _legacy_goto_url,
    }
