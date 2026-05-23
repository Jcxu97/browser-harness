"""
Real-world daily-use BH task pool for 1999-iteration soak test.

Each task is a dict with:
  name:       short id
  inline:     python source injected as BH stdin (uses safe-globals: goto/eval_js/snap/...)
  expect:     a substring or callable that the stdout must contain/satisfy for PASS
  category:   nav | eval | snap | shot | dom | legacy | error | unicode | misc

The runner picks tasks at random (or weighted) and runs ~1999 of them, either
all in one BH process (in-process loop -> tests cap+gc) or each in a fresh BH
process (tests bootstrap/atexit lifecycle).
"""
from __future__ import annotations

import os, random, tempfile

TMPDIR = tempfile.gettempdir()


def _shot_path(idx: int) -> str:
    return os.path.join(TMPDIR, f"bh_soak_{idx}.png").replace("\\", "/")


# Each task's `inline` is run inside a BH stdin block. Print "PASS" on success
# or anything containing the expected substring; raise on failure.
TASKS = [
    # --- navigation ---
    {"name": "nav_example_org", "category": "nav", "expect": "EXAMPLE.ORG_OK", "inline": """
goto("https://example.org/")
href = eval_js("location.href")
assert "example.org" in href, href
print("EXAMPLE.ORG_OK", href)
"""},

    {"name": "nav_data_url", "category": "nav", "expect": "DATA_URL_OK", "inline": """
goto("data:text/html,<h1 id=t>hello</h1>")
val = eval_js("document.querySelector('#t').textContent")
assert val == "hello", val
print("DATA_URL_OK")
"""},

    {"name": "nav_chained", "category": "nav", "expect": "CHAINED_OK", "inline": """
goto("data:text/html,<title>A</title>")
goto("data:text/html,<title>B</title>")
title = eval_js("document.title")
assert title == "B", title
print("CHAINED_OK")
"""},

    {"name": "nav_about_blank", "category": "nav", "expect": "BLANK_OK", "inline": """
goto("about:blank")
print("BLANK_OK", eval_js("location.href"))
"""},

    # --- eval ---
    {"name": "eval_arith", "category": "eval", "expect": "ARITH=42", "inline": """
goto("about:blank")
v = eval_js("6*7")
assert v == 42, v
print(f"ARITH={v}")
"""},

    {"name": "eval_json_roundtrip", "category": "eval", "expect": "JSON_OK", "inline": """
goto("about:blank")
out = eval_js("JSON.stringify({x: 1, y: [2,3], z: 'ok'})")
import json
assert json.loads(out) == {"x":1, "y":[2,3], "z":"ok"}
print("JSON_OK")
"""},

    {"name": "eval_useragent", "category": "eval", "expect": "UA_OK", "inline": """
goto("about:blank")
ua = eval_js("navigator.userAgent")
assert "Chrome" in ua or "Safari" in ua, ua
print("UA_OK", ua[:30])
"""},

    {"name": "eval_long_string", "category": "eval", "expect": "LONG_OK", "inline": """
goto("about:blank")
out = eval_js("'x'.repeat(5000)")
assert len(out) == 5000
print("LONG_OK", len(out))
"""},

    # --- snap ---
    {"name": "snap_example", "category": "snap", "expect": "SNAP_OK", "inline": """
goto("data:text/html,<h1>Sample</h1><p>body text</p>")
text = snap(max_chars=2000)
assert "Sample" in text, text[:100]
print("SNAP_OK", len(text))
"""},

    {"name": "snap_zero", "category": "snap", "expect": "SNAP_ZERO_OK", "inline": """
goto("data:text/html,<p>x</p>")
text = snap(max_chars=0)
print("SNAP_ZERO_OK", repr(text))
"""},

    # --- shot ---
    {"name": "shot_tmp", "category": "shot", "expect": "SHOT_OK", "inline": f"""
import os
goto("data:text/html,<h1>shot</h1>")
p = "{TMPDIR.replace(chr(92), '/')}/bh_soak_shot.png"
shot(p)
assert os.path.getsize(p) > 100, os.path.getsize(p)
print("SHOT_OK", os.path.getsize(p))
"""},

    # --- DOM interaction ---
    {"name": "dom_fill", "category": "dom", "expect": "FILL_OK", "inline": """
goto('data:text/html,<input id=q value="">')
fill("#q", "hello-soak")
v = eval_js("document.getElementById('q').value")
assert v == "hello-soak", v
print("FILL_OK")
"""},

    {"name": "dom_type_text", "category": "dom", "expect": "TYPE_OK", "inline": """
goto('data:text/html,<input id=q autofocus>')
eval_js("document.getElementById('q').focus()")
type_text("typed")
v = eval_js("document.getElementById('q').value")
assert "typed" in v, v
print("TYPE_OK", v)
"""},

    {"name": "dom_click_at", "category": "dom", "expect": "CLICK_OK", "inline": """
goto('''data:text/html,<button id=b style="position:absolute;left:50px;top:50px;width:200px;height:80px" onclick="this.textContent='clicked'">go</button>''')
import time
time.sleep(0.3)
click_at(150, 90)
time.sleep(0.3)
v = eval_js("document.getElementById('b').textContent")
assert v == "clicked", v
print("CLICK_OK")
"""},

    {"name": "dom_localstorage_persist_in_page", "category": "dom", "expect": "LS_OK", "inline": """
goto("https://example.com")
eval_js("localStorage.setItem('soak', 'v1')")
v = eval_js("localStorage.getItem('soak')")
assert v == "v1", v
print("LS_OK")
"""},

    # --- legacy / shadow ---
    {"name": "legacy_new_tab_raises", "category": "legacy", "expect": "RAISES_OK", "inline": """
try:
    new_tab("https://example.com")
    print("FAIL: new_tab did not raise")
except RuntimeError as e:
    assert "disabled" in str(e), str(e)
    print("RAISES_OK")
"""},

    {"name": "legacy_goto_url_raises", "category": "legacy", "expect": "RAISES_OK", "inline": """
try:
    goto_url("https://example.com")
    print("FAIL: goto_url did not raise")
except RuntimeError as e:
    assert "disabled" in str(e), str(e)
    print("RAISES_OK")
"""},

    {"name": "legacy_cdp_direct", "category": "legacy", "expect": "CDP_OK", "inline": """
from browser_harness.helpers import cdp
out = cdp("Target.getTargets")
assert "targetInfos" in out
print("CDP_OK", len(out["targetInfos"]))
"""},

    {"name": "legacy_js_helper", "category": "legacy", "expect": "JS_HELPER_OK", "inline": """
from browser_harness.helpers import js
goto("about:blank")
v = js("2+2")
assert v == 4 or v == "4", v
print("JS_HELPER_OK", v)
"""},

    # --- error / boundary ---
    {"name": "error_eval_syntax", "category": "error", "expect": "SYNTAX_RAISES", "inline": """
goto("about:blank")
try:
    eval_js("syntax @#")
    print("FAIL: did not raise")
except Exception as e:
    print("SYNTAX_RAISES", type(e).__name__)
"""},

    {"name": "error_eval_runtime", "category": "error", "expect": "RUNTIME_RAISES", "inline": """
goto("about:blank")
try:
    eval_js("undefined.x")
    print("FAIL: did not raise")
except Exception as e:
    print("RUNTIME_RAISES", type(e).__name__)
"""},

    # --- unicode / URL boundaries ---
    {"name": "unicode_chinese_data_url", "category": "unicode", "expect": "CN_OK", "inline": """
goto("data:text/html;charset=utf-8,<h1 id=t>测试中文</h1>")
v = eval_js("document.getElementById('t').textContent")
assert v == "测试中文", v
print("CN_OK")
"""},

    {"name": "unicode_emoji_data", "category": "unicode", "expect": "EMOJI_OK", "inline": """
goto("data:text/html;charset=utf-8,<p id=t>🎉🐍</p>")
v = eval_js("document.getElementById('t').textContent")
assert "🎉" in v
print("EMOJI_OK")
"""},

    # --- agent_tab lifecycle ---
    {"name": "lifecycle_close_then_reuse", "category": "lifecycle", "expect": "REUSE_OK", "inline": """
from browser_harness.second_window import ensure_agent_tab
old = agent_tab
close_tab()
new = ensure_agent_tab()
assert new != old, (old, new)
print("REUSE_OK", old[:8], "->", new[:8])
"""},

    {"name": "lifecycle_multiple_goto_same_tab", "category": "lifecycle", "expect": "STABLE_TID", "inline": """
old = agent_tab
goto("https://example.com")
goto("https://example.org")
goto("about:blank")
print("STABLE_TID", old[:8])
"""},

    # --- misc ---
    {"name": "misc_print_only", "category": "misc", "expect": "PRINT_OK", "inline": """
print("PRINT_OK no-browser-call")
"""},

    {"name": "misc_immediate_exit", "category": "misc", "expect": "IMM_OK", "inline": """
print("IMM_OK", agent_tab[:8])
"""},
]


def pick(seed=None):
    if seed is not None:
        random.seed(seed)
    return random.choice(TASKS)
