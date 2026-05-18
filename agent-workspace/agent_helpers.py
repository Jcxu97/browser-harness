"""Snapshot-driven helpers — Kimi-WebBridge-style accessibility tree + @e refs.

Loaded into browser_harness.helpers globals by _load_agent_helpers().

agent_window() + agent_new_tab() share a SINGLE hidden window across every
`browser-harness -c` invocation and every Claude session. State lives in
~/.browser-harness-agent-window.json. Use these instead of new_window for
any task — never spawn a fresh window per request.

This file restores the agent_window pattern (lost when the local
browser-harness repo was deleted) and adds P0/P1/P2/P3 helpers learned
from vercel-labs/agent-browser:

P0 (must-have):
    wait_for(text=, url=, fn=, load=, ms=, ref=) — semantic waits
    find_text/find_role/find_label/find_placeholder/find_testid — semantic locators

P1 (high-value):
    snapshot_compact(interactive=True, max_depth=N) — context-efficient snapshot
    state_save / state_load — portable cookies+localStorage JSON

P2 (nice-to-have):
    screenshot_annotated — numbered overlays for visual sites
    save_pdf / upload_file — API completeness
    is_visible / is_enabled / is_checked / get_count / get_box — predicates

P3 (security):
    auth_save / auth_get / auth_login — credentials vault (basic obfuscation)
"""
import json as _json
import os as _os
import time as _time
import base64 as _base64
from pathlib import Path as _Path

from browser_harness.helpers import (
    cdp, click_at_xy, current_tab, drain_events, goto_url,
    list_tabs, press_key, switch_tab, wait_for_load,
    capture_screenshot,
)


# ============================================================================
# agent_window: ONE persistent hidden window for all agent traffic
# ============================================================================

_AGENT_STATE = _Path.home() / ".browser-harness-agent-window.json"
_HIDDEN_BOUNDS = {"windowState": "normal", "left": -32000, "top": -32000,
                  "width": 1280, "height": 800}


def _read_state():
    try:
        return _json.loads(_AGENT_STATE.read_text())
    except Exception:
        return {}


def _write_state(d):
    try:
        _AGENT_STATE.write_text(_json.dumps(d))
    except Exception:
        pass


def _window_alive(win_id):
    try:
        cdp("Browser.getWindowBounds", windowId=win_id)
        return True
    except Exception:
        return False


def _force_offscreen(win_id):
    """Push the agent window off-screen. Idempotent and tolerant of clamping."""
    try:
        b = cdp("Browser.getWindowBounds", windowId=win_id).get("bounds", {})
        state = b.get("windowState")
        left, top = b.get("left", 0), b.get("top", 0)
        if state == "normal" and left < -10000 and top < -10000:
            return
        if state != "normal":
            cdp("Browser.setWindowBounds", windowId=win_id,
                bounds={"windowState": "normal"})
        cdp("Browser.setWindowBounds", windowId=win_id,
            bounds={"left": -32000, "top": -32000,
                    "width": 1280, "height": 800})
    except Exception:
        pass


def _find_anchor_in_window(win_id):
    for t in cdp("Target.getTargets")["targetInfos"]:
        if t["type"] != "page":
            continue
        try:
            if cdp("Browser.getWindowForTarget", targetId=t["targetId"]).get("windowId") == win_id:
                return t["targetId"]
        except Exception:
            pass
    return None


def agent_window():
    """Persistent hidden agent window. Returns (windowId, anchorTargetId).

    Created on first use, reused across every -c invocation and every Claude
    session via ~/.browser-harness-agent-window.json. Idempotent.
    """
    st = _read_state()
    win_id = st.get("windowId")
    if win_id and _window_alive(win_id):
        anchor = _find_anchor_in_window(win_id)
        if anchor:
            _force_offscreen(win_id)
            _write_state({"windowId": win_id, "anchorTargetId": anchor})
            return win_id, anchor
    tid = cdp("Target.createTarget", url="about:blank", newWindow=True)["targetId"]
    win_id = cdp("Browser.getWindowForTarget", targetId=tid)["windowId"]
    try:
        cdp("Browser.setWindowBounds", windowId=win_id, bounds=_HIDDEN_BOUNDS)
    except Exception:
        pass
    _write_state({"windowId": win_id, "anchorTargetId": tid})
    return win_id, tid


AGENT_TAB_CAP = 15


def _agent_window_tabs(win_id, anchor):
    out = []
    for t in cdp("Target.getTargets")["targetInfos"]:
        if t["type"] != "page" or t["targetId"] == anchor:
            continue
        try:
            if cdp("Browser.getWindowForTarget", targetId=t["targetId"]).get("windowId") == win_id:
                out.append(t["targetId"])
        except Exception:
            pass
    return out


def _enforce_tab_cap(win_id, anchor, cap=AGENT_TAB_CAP):
    tabs = _agent_window_tabs(win_id, anchor)
    excess = len(tabs) - (cap - 1)
    if excess <= 0:
        return 0
    closed = 0
    for tid in tabs[:excess]:
        try:
            cdp("Target.closeTarget", targetId=tid)
            closed += 1
        except Exception:
            pass
    return closed


def agent_new_tab(url="about:blank"):
    """Open a tab inside the agent window. The standard task-opener.

    Replaces new_window(url) for all routine browsing — guarantees the tab
    lands inside the single shared agent window. Returns the new targetId.
    Waits for load if a real URL is passed. Auto-closes oldest tabs over CAP.
    """
    win_id, anchor = agent_window()
    _enforce_tab_cap(win_id, anchor)
    cdp("Target.activateTarget", targetId=anchor)
    _force_offscreen(win_id)
    tid = cdp("Target.createTarget", url="about:blank", newWindow=False)["targetId"]
    _force_offscreen(win_id)
    try:
        actual_win = cdp("Browser.getWindowForTarget", targetId=tid).get("windowId")
    except Exception:
        actual_win = None
    if actual_win != win_id:
        try:
            cdp("Target.closeTarget", targetId=tid)
        except Exception:
            pass
        switch_tab(anchor)
        cdp("Runtime.evaluate",
            expression=f"window.open({_json.dumps(url)}, '_blank')")
        _time.sleep(0.5)
        new_tid = None
        for t in cdp("Target.getTargets")["targetInfos"]:
            if t["type"] != "page" or t["targetId"] == anchor:
                continue
            try:
                if cdp("Browser.getWindowForTarget", targetId=t["targetId"]).get("windowId") == win_id:
                    new_tid = t["targetId"]
            except Exception:
                pass
        if not new_tid:
            raise RuntimeError("agent_new_tab: could not place new tab in agent window")
        switch_tab(new_tid)
        _force_offscreen(win_id)
        if url != "about:blank":
            wait_for_load()
        return new_tid
    switch_tab(tid)
    _force_offscreen(win_id)
    if url != "about:blank":
        goto_url(url)
        wait_for_load()
    return tid


def agent_close_all_tabs(except_anchor=True):
    """Close every non-anchor tab in the agent window. Anchor + window survive."""
    win_id, anchor = agent_window()
    closed = 0
    for tid in _agent_window_tabs(win_id, anchor):
        try:
            cdp("Target.closeTarget", targetId=tid)
            closed += 1
        except Exception:
            pass
    _force_offscreen(win_id)
    return closed


# ============================================================================
# P0 — wait_for: semantic waits
# ============================================================================

def wait_for(*, text=None, url=None, fn=None, load=None, ms=None, ref=None,
             timeout=25.0, poll=0.3):
    """Wait until one condition is true. Pass exactly one of:
        text: text appears in body.innerText
        url: glob match for current URL (e.g. '**/dashboard')
        fn: JS expression evaluates truthy
        load: 'networkidle' / 'domcontentloaded' / 'load'
        ms: dumb sleep ms (last resort)
        ref: CSS selector — element appears
    """
    import fnmatch as _fnmatch
    if ms is not None:
        _time.sleep(ms / 1000.0)
        return
    if load is not None:
        wait_for_load()
        return
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        try:
            if text is not None:
                expr = f"document.body && document.body.innerText.includes({_json.dumps(text)})"
                r = cdp("Runtime.evaluate", expression=expr, returnByValue=True)
                if r.get("result", {}).get("value"):
                    return
            elif url is not None:
                r = cdp("Runtime.evaluate", expression="location.href", returnByValue=True)
                cur = r.get("result", {}).get("value", "")
                if _fnmatch.fnmatch(cur, url):
                    return
            elif fn is not None:
                r = cdp("Runtime.evaluate", expression=fn, returnByValue=True)
                if r.get("result", {}).get("value"):
                    return
            elif ref is not None:
                sel = ref.lstrip("@") if ref.startswith("@") else ref
                expr = f"!!document.querySelector({_json.dumps(sel)})"
                r = cdp("Runtime.evaluate", expression=expr, returnByValue=True)
                if r.get("result", {}).get("value"):
                    return
        except Exception:
            pass
        _time.sleep(poll)
    raise TimeoutError(f"wait_for timeout: text={text!r} url={url!r} fn={fn!r} ref={ref!r}")


# ============================================================================
# P0 — Semantic locators
# ============================================================================

def _eval_truthy(expr):
    r = cdp("Runtime.evaluate", expression=expr, returnByValue=True)
    return bool(r.get("result", {}).get("value"))


def _click_via_js(selector_expr):
    js = f"""(function() {{
        const el = {selector_expr};
        if (!el) return false;
        el.scrollIntoView({{block: 'center'}});
        el.click();
        return true;
    }})()"""
    return _eval_truthy(js)


def _fill_via_js(selector_expr, value):
    js = f"""(function() {{
        const el = {selector_expr};
        if (!el) return false;
        el.focus();
        el.value = {_json.dumps(value)};
        el.dispatchEvent(new Event('input', {{bubbles: true}}));
        el.dispatchEvent(new Event('change', {{bubbles: true}}));
        return true;
    }})()"""
    return _eval_truthy(js)


def find_text(text, action="click", exact=False):
    """Find element containing visible text and perform action.

    Walks document AND all shadow DOM roots — modern sites (Nexus, etc.) often
    use web components where buttons live in shadowRoot.
    """
    text_json = _json.dumps(text)
    if exact:
        pred = f"(t === {text_json})"
    else:
        pred = f"t.includes({text_json})"
    sel = f"""(function() {{
        function walk(root, out) {{
            const tw = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT, null);
            let n;
            while ((n = tw.nextNode())) {{
                const t = (n.textContent || '').trim();
                if (t.length < 200 && /^(A|BUTTON|INPUT|LABEL|SPAN|DIV|H[1-6])$/i.test(n.tagName) && {pred}) {{
                    out.push(n);
                }}
                if (n.shadowRoot) walk(n.shadowRoot, out);
            }}
        }}
        const out = [];
        walk(document, out);
        // Prefer A/BUTTON/[role=button]; pick smallest matching (most specific)
        out.sort((a, b) => {{
            const score = el => (el.tagName === 'BUTTON' || el.tagName === 'A' ? 0 : 1)
                + (el.textContent || '').length / 1000;
            return score(a) - score(b);
        }});
        return out[0] || null;
    }})()"""
    if action == "click":
        if not _click_via_js(sel):
            raise RuntimeError(f"find_text: text={text!r} not found (exact={exact})")
    else:
        raise ValueError(f"find_text action={action!r} not supported")


def find_role(role, action="click", name=None):
    """Find element by ARIA role + optional accessible name."""
    if name:
        sel = (f"""Array.from(document.querySelectorAll('[role="{role}"], {role}')).find(el => """
               f"""(el.textContent || el.getAttribute('aria-label') || '').includes({_json.dumps(name)}))""")
    else:
        sel = f"""document.querySelector('[role="{role}"], {role}')"""
    if action == "click":
        if not _click_via_js(sel):
            raise RuntimeError(f"find_role: role={role!r} name={name!r} not found")


def find_label(label, action="click", value=None):
    """Find input/textarea/select by associated <label>."""
    sel = (f"""(function() {{
        const lbl = Array.from(document.querySelectorAll('label')).find(l => """
        f"""(l.textContent || '').includes({_json.dumps(label)}));
        if (!lbl) return null;
        const id = lbl.getAttribute('for');
        return id ? document.getElementById(id) : lbl.querySelector('input, textarea, select');
    }})()""")
    if action == "click":
        if not _click_via_js(sel):
            raise RuntimeError(f"find_label: label={label!r} not found")
    elif action == "fill":
        if not _fill_via_js(sel, value or ""):
            raise RuntimeError(f"find_label fill: label={label!r} not found")


def find_placeholder(placeholder, action="click", value=None):
    sel = f"""document.querySelector('input[placeholder*={_json.dumps(placeholder)}], textarea[placeholder*={_json.dumps(placeholder)}]')"""
    if action == "click":
        if not _click_via_js(sel):
            raise RuntimeError(f"find_placeholder: {placeholder!r} not found")
    elif action == "fill":
        if not _fill_via_js(sel, value or ""):
            raise RuntimeError(f"find_placeholder fill: {placeholder!r} not found")


def find_testid(testid, action="click"):
    sel = f"""document.querySelector('[data-testid={_json.dumps(testid)}]')"""
    if action == "click":
        if not _click_via_js(sel):
            raise RuntimeError(f"find_testid: testid={testid!r} not found")


def find_first(css, action="click"):
    sel = f"""document.querySelector({_json.dumps(css)})"""
    if action == "click":
        if not _click_via_js(sel):
            raise RuntimeError(f"find_first: css={css!r} not found")


def find_nth(n, css, action="click"):
    """1-indexed nth match of CSS selector."""
    sel = f"""document.querySelectorAll({_json.dumps(css)})[{int(n) - 1}]"""
    if action == "click":
        if not _click_via_js(sel):
            raise RuntimeError(f"find_nth: n={n} css={css!r} out of range")


# ============================================================================
# P1 — Snapshot improvements (interactive_only + max_depth)
# ============================================================================

_INTERACTIVE_ROLES = {
    "button", "link", "checkbox", "radio", "textbox", "combobox",
    "searchbox", "menuitem", "tab", "option", "switch", "slider",
    "heading", "menubutton", "treeitem",
}


def snapshot_compact(interactive_only=True, max_depth=None, scope=None):
    """Compact accessibility-tree snapshot for LLM context efficiency.

    interactive_only=True: keep only buttons/links/inputs/etc., drop
                           generic structural nodes
    max_depth=N: cap tree depth at N levels
    scope=CSS: scope to subtree (advanced)

    Returns text similar to agent-browser's `snapshot -i`:
        @e1 [link] 'Sign in'
        @e2 [textbox] 'Email'

    Refs go stale on any page change. Re-snapshot before next interaction.
    """
    res = cdp("Accessibility.getFullAXTree")
    nodes = res.get("nodes", [])
    if not nodes:
        return ""
    by_id = {n.get("nodeId"): n for n in nodes}
    out = []
    ref_to_node = {}

    def render(node, depth=0):
        if max_depth is not None and depth > max_depth:
            return
        role = node.get("role", {}).get("value", "")
        name = node.get("name", {}).get("value", "")
        keep = True
        if interactive_only and role not in _INTERACTIVE_ROLES and not name.strip():
            keep = False
        if keep:
            ref = f"e{len(ref_to_node) + 1}"
            ref_to_node[ref] = node
            indent = "  " * depth
            out.append(f"{indent}@{ref} [{role}] {name!r}")
        for cid in node.get("childIds", []):
            child = by_id.get(cid)
            if child:
                render(child, depth + 1 if keep else depth)

    for n in nodes:
        if not n.get("parentId"):
            render(n)
            break

    try:
        snap_file = _Path.home() / ".browser-harness-last-snapshot.json"
        snap_file.write_text(_json.dumps({
            ref: {
                "backendDOMNodeId": node.get("backendDOMNodeId"),
                "role": node.get("role", {}).get("value", ""),
                "name": node.get("name", {}).get("value", ""),
            }
            for ref, node in ref_to_node.items()
        }))
    except Exception:
        pass
    return "\n".join(out)


# ============================================================================
# P1 — state save/load: portable cookies + storage to JSON
# ============================================================================

def state_save(path):
    """Save cookies + localStorage + sessionStorage of current tab to JSON.

    Portable across machines (unlike chrome's per-profile encrypted store).
    """
    try:
        cookies = cdp("Network.getAllCookies").get("cookies", [])
    except Exception:
        cookies = []
    try:
        ls_r = cdp("Runtime.evaluate",
                   expression="JSON.stringify(Object.fromEntries(Object.entries(localStorage)))",
                   returnByValue=True)
        ls = _json.loads(ls_r.get("result", {}).get("value", "{}"))
    except Exception:
        ls = {}
    try:
        ss_r = cdp("Runtime.evaluate",
                   expression="JSON.stringify(Object.fromEntries(Object.entries(sessionStorage)))",
                   returnByValue=True)
        ss = _json.loads(ss_r.get("result", {}).get("value", "{}"))
    except Exception:
        ss = {}
    state = {
        "version": 1,
        "saved_at": _time.time(),
        "cookies": cookies,
        "localStorage": ls,
        "sessionStorage": ss,
    }
    p = _Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_json.dumps(state, indent=2))
    return str(p.resolve())


def state_load(path):
    """Restore cookies + localStorage + sessionStorage from JSON file."""
    state = _json.loads(_Path(path).read_text())
    restored = {"cookies": 0, "localStorage": 0, "sessionStorage": 0}
    for c in state.get("cookies", []):
        try:
            params = {k: v for k, v in c.items() if k in {
                "name", "value", "url", "domain", "path", "secure",
                "httpOnly", "sameSite", "expires"
            }}
            cdp("Network.setCookie", **params)
            restored["cookies"] += 1
        except Exception:
            pass
    ls = state.get("localStorage", {})
    if ls:
        js = "; ".join(f"localStorage.setItem({_json.dumps(k)}, {_json.dumps(v)})" for k, v in ls.items())
        try:
            cdp("Runtime.evaluate", expression=js)
            restored["localStorage"] = len(ls)
        except Exception:
            pass
    ss = state.get("sessionStorage", {})
    if ss:
        js = "; ".join(f"sessionStorage.setItem({_json.dumps(k)}, {_json.dumps(v)})" for k, v in ss.items())
        try:
            cdp("Runtime.evaluate", expression=js)
            restored["sessionStorage"] = len(ss)
        except Exception:
            pass
    return restored


# ============================================================================
# P2 — screenshot_annotated, save_pdf, upload_file, predicates
# ============================================================================

def screenshot_annotated(path="annotated.png", roles=None):
    """Screenshot with red numbered labels on interactive elements."""
    if roles is None:
        roles = ["button", "a", "input", "textarea", "select"]
    css = ", ".join(roles + ['[role="button"]', '[role="link"]'])
    js_inject = f"""(function() {{
        window.__bh_labels = [];
        const els = document.querySelectorAll({_json.dumps(css)});
        let i = 0;
        for (const el of els) {{
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) continue;
            const cs = window.getComputedStyle(el);
            if (cs.display === 'none' || cs.visibility === 'hidden') continue;
            i++;
            const label = document.createElement('div');
            label.style.cssText = `
                position: absolute;
                z-index: 99999;
                top: ${{r.top + window.scrollY}}px;
                left: ${{r.left + window.scrollX}}px;
                background: red;
                color: white;
                padding: 2px 6px;
                font-size: 12px;
                font-family: monospace;
                font-weight: bold;
                border-radius: 3px;
                pointer-events: none;
            `;
            label.textContent = i;
            document.body.appendChild(label);
            window.__bh_labels.push(label);
            el.setAttribute('data-bh-label', i);
        }}
        return i;
    }})()"""
    try:
        r = cdp("Runtime.evaluate", expression=js_inject, returnByValue=True)
        count = r.get("result", {}).get("value", 0)
    except Exception:
        count = 0
    capture_screenshot(path)
    js_clean = """(function() {
        for (const l of (window.__bh_labels || [])) l.remove();
        window.__bh_labels = [];
        for (const el of document.querySelectorAll('[data-bh-label]'))
            el.removeAttribute('data-bh-label');
    })()"""
    try:
        cdp("Runtime.evaluate", expression=js_clean)
    except Exception:
        pass
    return path, count


def save_pdf(path="page.pdf", landscape=False, scale=1.0):
    """Save current page as PDF via CDP Page.printToPDF."""
    r = cdp("Page.printToPDF", landscape=landscape, scale=scale)
    p = _Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(_base64.b64decode(r["data"]))
    return str(p.resolve())


def upload_file(selector, *file_paths):
    """Upload file(s) to <input type=file> matching selector."""
    doc = cdp("DOM.getDocument", depth=-1)
    root_id = doc["root"]["nodeId"]
    res = cdp("DOM.querySelector", nodeId=root_id, selector=selector)
    node_id = res.get("nodeId")
    if not node_id:
        raise RuntimeError(f"upload_file: selector {selector!r} not found")
    abs_paths = [str(_Path(p).resolve()) for p in file_paths]
    cdp("DOM.setFileInputFiles", nodeId=node_id, files=abs_paths)
    return abs_paths


def is_visible(selector):
    js = f"""(function() {{
        const el = document.querySelector({_json.dumps(selector)});
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const s = window.getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.display !== 'none'
            && s.visibility !== 'hidden' && parseFloat(s.opacity || '1') > 0;
    }})()"""
    return _eval_truthy(js)


def is_enabled(selector):
    js = f"""(function() {{
        const el = document.querySelector({_json.dumps(selector)});
        return !!(el && !el.disabled && el.getAttribute('aria-disabled') !== 'true');
    }})()"""
    return _eval_truthy(js)


def is_checked(selector):
    js = f"""(function() {{
        const el = document.querySelector({_json.dumps(selector)});
        return !!(el && el.checked);
    }})()"""
    return _eval_truthy(js)


def get_count(selector):
    js = f"document.querySelectorAll({_json.dumps(selector)}).length"
    r = cdp("Runtime.evaluate", expression=js, returnByValue=True)
    return int(r.get("result", {}).get("value") or 0)


def get_box(selector):
    js = f"""(function() {{
        const el = document.querySelector({_json.dumps(selector)});
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return JSON.stringify({{x: r.x, y: r.y, width: r.width, height: r.height}});
    }})()"""
    r = cdp("Runtime.evaluate", expression=js, returnByValue=True)
    val = r.get("result", {}).get("value")
    return _json.loads(val) if val else None


# ============================================================================
# P3 — auth vault (XOR obfuscation, NOT real crypto)
# ============================================================================

_AUTH_VAULT = _Path.home() / ".browser-harness-auth-vault.json"
_VAULT_KEY = "browser-harness-vault-XOR-2026"


def _xor_obfuscate(s):
    out = "".join(chr(ord(c) ^ ord(_VAULT_KEY[i % len(_VAULT_KEY)])) for i, c in enumerate(s))
    return _base64.b64encode(out.encode("latin-1")).decode("ascii")


def _xor_deobfuscate(s):
    raw = _base64.b64decode(s).decode("latin-1")
    return "".join(chr(ord(c) ^ ord(_VAULT_KEY[i % len(_VAULT_KEY)])) for i, c in enumerate(raw))


def auth_save(name, *, url, username, password):
    vault = {}
    if _AUTH_VAULT.exists():
        try:
            vault = _json.loads(_AUTH_VAULT.read_text())
        except Exception:
            pass
    vault[name] = {"url": url, "username": username, "password_b64": _xor_obfuscate(password)}
    _AUTH_VAULT.write_text(_json.dumps(vault, indent=2))
    try:
        _AUTH_VAULT.chmod(0o600)
    except Exception:
        pass
    return name


def auth_get(name):
    if not _AUTH_VAULT.exists():
        raise RuntimeError("Auth vault is empty.")
    vault = _json.loads(_AUTH_VAULT.read_text())
    e = vault.get(name)
    if not e:
        raise RuntimeError(f"auth_get: name={name!r} not in vault")
    return {"url": e["url"], "username": e["username"],
            "password": _xor_deobfuscate(e["password_b64"])}


def auth_login(name, *, user_field=None, password_field=None, submit_text="Submit"):
    creds = auth_get(name)
    agent_new_tab(creds["url"])
    wait_for_load()
    user_sels = [user_field] if user_field else [
        "input[name=email]", "input[name=username]",
        "input[type=email]", "input[autocomplete=username]",
    ]
    pwd_sels = [password_field] if password_field else [
        "input[name=password]", "input[type=password]",
        "input[autocomplete=current-password]",
    ]
    for s in user_sels:
        if s and is_visible(s):
            _fill_via_js(f"document.querySelector({_json.dumps(s)})", creds["username"])
            break
    else:
        raise RuntimeError("auth_login: no username field")
    for s in pwd_sels:
        if s and is_visible(s):
            _fill_via_js(f"document.querySelector({_json.dumps(s)})", creds["password"])
            break
    else:
        raise RuntimeError("auth_login: no password field")
    find_text(submit_text, "click")


if __name__ == "__main__":
    print("agent_helpers.py — public API:")
    public = [n for n in globals() if not n.startswith("_") and callable(globals().get(n))]
    for name in sorted(public):
        print(f"  {name}")
