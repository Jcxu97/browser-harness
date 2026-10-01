"""Safe-mode globals for `browser-harness <<'PY'` scripts (run.py calls safe_globals()).

The isolation itself lives in second_window.request_policy, which helpers._send
applies to every request. Upstream helpers such as js(), page_info(),
click_at_xy() and wait_for_load() therefore already act on this process's agent
tab. This module adds the fork's short names (goto, eval_js, snap, snapshot,
click_ref, ...) and replaces the two upstream helpers whose meaning changes
here: new_tab() and close_tab().
"""
from __future__ import annotations

from . import second_window as _sw


def _with_self_heal(fn, *, replayable):
    """Run fn(tab). When the tab is gone, bind a new one.

    Only goto() is replayed on the new tab, because it fully defines the page.
    Anything else would run on a different page than the caller expects.
    """
    tid = _sw.bound_tab()
    try:
        return fn(tid)
    except Exception as e:
        if not _sw.is_target_gone(e):
            raise
        _sw._forget_tab(tid)
        if replayable:
            return fn(_sw.bound_tab())
        raise RuntimeError(
            "the agent tab closed during this call, so it was not repeated on a new "
            f"tab. Call goto() again, then retry. Original error: {e}"
        ) from e


def safe_globals():
    agent_tab = _sw.bound_tab()

    def goto(url, timeout=15):
        return _with_self_heal(lambda t: _sw.navigate_agent(t, url, timeout=timeout), replayable=True)

    def eval_js(expression):
        return _with_self_heal(lambda t: _sw.evaluate_agent(t, expression), replayable=False)

    def snap(max_chars=10000):
        return _with_self_heal(lambda t: _sw.snapshot_agent(t, max_chars=max_chars), replayable=False)

    def snapshot(interactive_only=True, roles=None, max_chars=12000):
        return _with_self_heal(lambda t: _sw.snapshot_tree_agent(
            t, interactive_only=interactive_only, roles=roles, max_chars=max_chars), replayable=False)

    def ref_for(query, role=None):
        return _sw.ref_for_agent(_sw.bound_tab(), query, role=role)

    def click_ref(ref):
        return _with_self_heal(lambda t: _sw.click_ref_agent(t, ref), replayable=False)

    def fill_ref(ref, value, submit=False):
        return _with_self_heal(lambda t: _sw.fill_ref_agent(t, ref, value, submit=submit), replayable=False)

    def shot(path, full=False):
        return _with_self_heal(lambda t: _sw.screenshot_agent(t, path, full=full), replayable=False)

    def click_at(x, y, button="left"):
        return _with_self_heal(lambda t: _sw.click_at_agent(t, x, y, button=button), replayable=False)

    def send_keys(keys):
        return _with_self_heal(lambda t: _sw.send_keys_agent(t, keys), replayable=False)

    def hotkey(chord):
        return _with_self_heal(lambda t: _sw.hotkey_agent(t, chord), replayable=False)

    def fill(selector, value):
        return _with_self_heal(lambda t: _sw.fill_agent(t, selector, value), replayable=False)

    def upload(selector, file_paths):
        return _with_self_heal(lambda t: _sw.upload_agent(t, selector, file_paths), replayable=False)

    def new_tab(url="about:blank"):
        """Open url in an agent tab and make it current. The current tab is
        reused while it still shows its placeholder page."""
        tid = _sw.bound_tab()
        if not _sw.is_placeholder(tid):
            tid = _sw.new_agent_tab()
            _sw.bind(tid)
        if url and url != "about:blank":
            _sw.navigate_agent(tid, url)
        return tid

    def close_tab(target=None):
        """Close target (default: the current agent tab). Returns False when
        there is nothing to close. The next helper call binds a new tab."""
        if isinstance(target, dict):
            target = target.get("targetId") or target.get("target_id")
        tid = target or _sw._BOUND["tid"]
        if tid is None:
            return False
        return _sw.close_agent_tab(tid)

    def show_window():
        return _sw.show_window(_sw.bound_tab())

    def hide_window():
        return _sw.hide_window()

    return {
        "agent_tab": agent_tab,
        "goto": goto, "eval_js": eval_js, "snap": snap, "snapshot": snapshot,
        "ref_for": ref_for, "click_ref": click_ref, "fill_ref": fill_ref,
        "shot": shot, "click_at": click_at, "send_keys": send_keys, "hotkey": hotkey,
        "fill": fill, "upload": upload, "new_tab": new_tab, "close_tab": close_tab,
        "show_window": show_window, "hide_window": hide_window,
    }
