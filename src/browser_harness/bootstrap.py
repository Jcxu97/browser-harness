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
import pathlib
import time as _time

from . import second_window as _sw
from .helpers import cdp as _cdp


_SPAWN_LOCK_PATH = pathlib.Path.home() / ".browser-harness" / "second-window-spawn.lock"


def ensure_pinned_second_window(wait=30.0):
    """Always return a wid for a valid pinned second window.

    Resolution order:
      1. Existing pinned wid still alive -> return it.
      2. detect_second_window() finds a non-user candidate -> pin and return.
      3. Otherwise spawn_second_window() then pin and return.

    Never returns None. Never returns the user's main window (detect_second_window
    raises rather than picking a polluted candidate; we catch and spawn fresh).

    Concurrent BH clients on cold start used to each enter step 3 and spawn
    their OWN window — user reported 6 new windows during 6-parallel soak
    (2026-05-24). File-lock so only ONE client spawns; others wait for that
    spawn's pin to land in STATE_FILE, then return it. Stale lock (>60s
    mtime) is reaped automatically.
    """
    state = _sw._load_state()
    pinned = state.get("pinned_second_window_id")
    live = _sw._list_windows()
    if pinned and pinned in live:
        return pinned

    held_lock = False
    try:
        try:
            _SPAWN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(_SPAWN_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()}\n{_time.time()}".encode())
            os.close(fd)
            held_lock = True
        except FileExistsError:
            # Someone else is spawning — wait for their pin to land.
            deadline = _time.time() + wait
            while _time.time() < deadline:
                state = _sw._load_state()
                pinned = state.get("pinned_second_window_id")
                live = _sw._list_windows()
                if pinned and pinned in live:
                    return pinned
                # Stale lock reaping — holder probably crashed mid-spawn.
                try:
                    age = _time.time() - _SPAWN_LOCK_PATH.stat().st_mtime
                except FileNotFoundError:
                    age = 0  # released — pin should appear momentarily
                if age > 60:
                    try: _SPAWN_LOCK_PATH.unlink()
                    except FileNotFoundError: pass
                    try:
                        fd = os.open(str(_SPAWN_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                        os.write(fd, f"{os.getpid()}\n{_time.time()}".encode())
                        os.close(fd)
                        held_lock = True
                        break
                    except FileExistsError:
                        pass
                _time.sleep(0.3)
            if not held_lock:
                # Timed out — last-chance read, otherwise raise.
                state = _sw._load_state()
                pinned = state.get("pinned_second_window_id")
                live = _sw._list_windows()
                if pinned and pinned in live:
                    return pinned
                raise RuntimeError(f"second-window spawn lock held past {wait}s and no pin appeared")

        # We hold the lock — actually do the spawn.
        # Re-check inside the lock (another holder may have just released).
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
    finally:
        if held_lock:
            try: _SPAWN_LOCK_PATH.unlink()
            except FileNotFoundError: pass


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

    SESSION-SCOPED: we only close tabs claimed by our own owner id in the state
    file. Concurrent BH sessions (main thread + sub-agent in parallel) each clean
    up only their own placeholders; we never close another session's tab — see
    `reference_browser_session_isolation` in user memory for why this matters
    (the 2026-05-20 doubao-vs-Nexus tab-poaching incident).

    Scoping is by session, NOT by pid: every `browser-harness <<'PY'` heredoc is
    a fresh short-lived process, so a pid-scoped claim never matches on the next
    call (that's why c867cf5 moved to `_get_owner_id()`).

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
        # Must go through _read_claim/_get_owner_id, not r["claimed_by_pid"]:
        # c867cf5 switched _write_claim to store `claimed_by` (session id) and
        # actively pops `claimed_by_pid`, so the old int comparison matched zero
        # records and this whole cleanup silently no-op'd — leaving the tab cap
        # as the only GC, which the cap comment explicitly says it shouldn't be.
        my_owner = _sw._get_owner_id()
        state = _sw._load_state()
        my_records = {
            r["tid"]: r.get("nonce")
            for r in state.get("agent_tabs", [])
            if _sw._read_claim(r) == my_owner and r.get("tid")
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


# Errors that mean "the targetId we cached is gone" — close_tab(), tab navigated
# away on its own, Chrome reloaded, etc. When we see one of these, we rebind to
# a fresh agent_tab and retry the call once. Any other error surfaces.
_DEAD_TID_NEEDLES = (
    "No target with given id found",
    "Session with given id not found",
    "Target closed",
    "Inspected target navigated or closed",
)


def _is_dead_tid(exc):
    msg = str(exc)
    return any(needle in msg for needle in _DEAD_TID_NEEDLES)


def safe_globals():
    """Bind a fresh agent tab to convenience helpers, return as dict for globals().update().

    The agent_tab targetId is held in a mutable box rather than frozen into each
    closure — when a call hits a "No target with given id" error (because the user
    or another task closed the tab), we rebind to a fresh agent_tab and retry
    once. Without this self-heal, batched runs that include close_tab() poison
    every subsequent helper call (1999-soak: 113/1600 = 7% failures from this).
    """
    global _atexit_registered
    ensure_pinned_second_window()
    # Pre-spawn prune — without this, ensure_agent_tab only prunes AFTER it
    # adds a tab, so peak tab count = N + concurrent_clients before getting
    # squeezed back to cap. Pre-prune to cap-1 so adding our own keeps total
    # at cap. User reported 2026-05-24: tab count visibly exceeded the
    # DEFAULT_MAX_AGENT_TABS=15 cap during 6-parallel soak.
    try:
        _sw.prune_agent_tabs(max_n=max(1, _sw.DEFAULT_MAX_AGENT_TABS - 1))
    except Exception:
        pass  # best-effort; don't block bootstrap on a CDP hiccup
    tab_box = [_sw.ensure_agent_tab()]
    if not _atexit_registered:
        atexit.register(_close_placeholder_tabs)
        _atexit_registered = True

    def _with_self_heal(fn):
        """Run fn(tab); if the tid is dead, rebind once and retry."""
        try:
            return fn(tab_box[0])
        except Exception as e:
            if not _is_dead_tid(e):
                raise
            tab_box[0] = _sw.ensure_agent_tab()
            return fn(tab_box[0])

    def goto(url, timeout=15):
        return _with_self_heal(lambda t: _sw.navigate_agent(t, url, timeout=timeout))

    def eval_js(expression):
        return _with_self_heal(lambda t: _sw.evaluate_agent(t, expression))

    def snap(max_chars=10000):
        return _with_self_heal(lambda t: _sw.snapshot_agent(t, max_chars=max_chars))

    def shot(path):
        return _with_self_heal(lambda t: _sw.screenshot_agent(t, path))

    def click_at(x, y, button="left"):
        return _with_self_heal(lambda t: _sw.click_at_agent(t, x, y, button=button))

    def type_text(text):
        return _with_self_heal(lambda t: _sw.key_type_agent(t, text))

    def send_keys(keys):
        return _with_self_heal(lambda t: _sw.send_keys_agent(t, keys))

    def hotkey(chord):
        return _with_self_heal(lambda t: _sw.hotkey_agent(t, chord))

    def fill(selector, value):
        return _with_self_heal(lambda t: _sw.fill_agent(t, selector, value))

    def upload(selector, file_paths):
        return _with_self_heal(lambda t: _sw.upload_agent(t, selector, file_paths))

    def close_tab():
        # Intentionally not self-healed: closing a tab and immediately rebinding
        # would defeat the user's explicit intent. The next *other* call (goto,
        # eval_js, ...) will rebind via _with_self_heal.
        result = _sw.close_agent_tab(tab_box[0])
        # Mark dead so the next call definitely rebinds (some BH builds return
        # success even when CDP closeTarget already lost the session).
        tab_box[0] = None
        return result

    def _ensure():
        if tab_box[0] is None:
            tab_box[0] = _sw.ensure_agent_tab()
        return tab_box[0]

    # Wrap every helper so the post-close_tab() rebind kicks in on first reuse.
    def _wrap(fn_inner):
        def wrapped(*a, **kw):
            _ensure()
            return fn_inner(*a, **kw)
        return wrapped

    return {
        # bound state — initial tid; user code that captures this won't see
        # post-close rebinds, but the helpers themselves always self-heal.
        "agent_tab": tab_box[0],
        # safe primary API — what agents should write
        "goto": _wrap(goto),
        "eval_js": _wrap(eval_js),
        "snap": _wrap(snap),
        "shot": _wrap(shot),
        "click_at": _wrap(click_at),
        "type_text": _wrap(type_text),
        "send_keys": _wrap(send_keys),
        "hotkey": _wrap(hotkey),
        "fill": _wrap(fill),
        "upload": _wrap(upload),
        "close_tab": close_tab,
        # Legacy names: raise loudly instead of silently changing semantics.
        "new_tab": _legacy_new_tab,
        "goto_url": _legacy_goto_url,
    }
