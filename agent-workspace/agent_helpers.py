"""Extra helpers for browser-harness scripts.

helpers._load_agent_helpers() loads this file into the helper globals, so a
script can call these names directly. In safe mode every cdp() call here goes
to the agent tab of the calling process (see second_window.request_policy).

    wait_for(text=, url=, fn=, load=, ms=, ref=)    wait for one condition
    find_text / find_role / find_label / find_placeholder / find_testid /
    find_first / find_nth                           click or fill by meaning
    snapshot_compact()                              old name for snapshot()
    screenshot_annotated / save_pdf                 numbered screenshot, PDF
    is_visible / is_enabled / is_checked / get_count / get_box
"""
import json as _json
import time as _time
import base64 as _base64
from pathlib import Path as _Path

from browser_harness.helpers import cdp, capture_screenshot, wait_for_load


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


def snapshot_compact(interactive_only=True, max_depth=None, scope=None):
    """Old name for snapshot(). max_depth and scope have no effect now.

    Returns lines such as `@e1 button "Sign in"`. The refs stay valid across
    browser-harness calls until the page changes. Use click_ref / fill_ref.
    """
    from browser_harness import second_window as _sw
    return _sw.snapshot_tree_agent(_sw.bound_tab(), interactive_only=interactive_only)


# ============================================================================
# P2 — screenshot_annotated, save_pdf, predicates
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


if __name__ == "__main__":
    print("agent_helpers.py — public API:")
    public = [n for n in globals() if not n.startswith("_") and callable(globals().get(n))]
    for name in sorted(public):
        print(f"  {name}")
