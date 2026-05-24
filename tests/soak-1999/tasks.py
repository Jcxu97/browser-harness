"""
Real-world BH task pool for 1000-iteration soak test.

80% real-site tasks (Wikipedia / GitHub / example.* / httpbin / DuckDuckGo /
MDN / Archive / Bing) covering navigation / snap / eval / fill / screenshot
under realistic network + JS conditions.

20% retained synthetic tasks for must-keep regressions:
 - lifecycle close_then_X — closure self-heal in safe_globals (commit f7d72be #1)
 - legacy new_tab/goto_url — must still raise (shadow guarantee)
 - error eval throw / syntax / runtime / invalid selector — eval_agent must raise

Each task: name / category / expect substring (must appear in stdout) / inline
(Python source fed into BH stdin via safe-globals: goto/eval_js/snap/...).

Real-site tasks pick popular, low-bot-block, login-free pages. Expect strings
are deliberately loose (substring match) so minor site redesigns don't tank
pass rate. Network jitter + Cloudflare challenges may cause individual
failures; ≥85% pass is the bar.
"""
from __future__ import annotations

import os, random, tempfile

TMPDIR = tempfile.gettempdir().replace("\\", "/")


TASKS = [
    # =========================================================================
    # WIKIPEDIA — most reliable read-only target
    # =========================================================================
    {"name": "wiki_main_en", "category": "wiki", "expect": "WIKI_EN_OK", "inline": """
goto("https://en.wikipedia.org/wiki/Main_Page", timeout=20)
text = snap(max_chars=3000)
assert "Wikipedia" in text, text[:300]
print("WIKI_EN_OK")
"""},

    {"name": "wiki_main_zh", "category": "wiki", "expect": "WIKI_ZH_OK", "inline": """
goto("https://zh.wikipedia.org/wiki/Wikipedia:%E9%A6%96%E9%A1%B5", timeout=20)
text = snap(max_chars=3000)
assert "维基百科" in text or "Wikipedia" in text, text[:300]
print("WIKI_ZH_OK")
"""},

    {"name": "wiki_main_ja", "category": "wiki", "expect": "WIKI_JA_OK", "inline": """
goto("https://ja.wikipedia.org/wiki/%E3%83%A1%E3%82%A4%E3%83%B3%E3%83%9A%E3%83%BC%E3%82%B8", timeout=20)
text = snap(max_chars=3000)
assert "ウィキペディア" in text or "Wikipedia" in text, text[:300]
print("WIKI_JA_OK")
"""},

    {"name": "wiki_article_python", "category": "wiki", "expect": "WIKI_PY_OK", "inline": """
goto("https://en.wikipedia.org/wiki/Python_(programming_language)", timeout=25)
text = snap(max_chars=4000)
assert "Python" in text and "programming" in text.lower(), text[:300]
print("WIKI_PY_OK")
"""},

    {"name": "wiki_article_us", "category": "wiki", "expect": "WIKI_US_OK", "inline": """
goto("https://en.wikipedia.org/wiki/United_States", timeout=25)
text = snap(max_chars=5000)
assert "United States" in text, text[:300]
print("WIKI_US_OK")
"""},

    {"name": "wiki_long_article_truncate", "category": "wiki", "expect": "WIKI_TRUNC_OK", "inline": """
goto("https://en.wikipedia.org/wiki/World_War_II", timeout=25)
text = snap(max_chars=8000)
assert len(text) <= 9000, len(text)
assert "World War" in text or "1939" in text or "1945" in text, text[:300]
print("WIKI_TRUNC_OK", len(text))
"""},

    {"name": "wiki_eval_title", "category": "wiki", "expect": "WIKI_TITLE_OK", "inline": """
goto("https://en.wikipedia.org/wiki/Main_Page", timeout=20)
title = eval_js("document.title")
assert "Wikipedia" in title, title
print("WIKI_TITLE_OK", title[:60])
"""},

    {"name": "wiki_search_fill", "category": "wiki", "expect": "WIKI_SEARCH_OK", "inline": """
import time
goto("https://en.wikipedia.org/wiki/Main_Page", timeout=20)
fill("#searchInput", "Python programming")
v = eval_js("document.getElementById('searchInput').value")
assert "Python" in v, v
print("WIKI_SEARCH_OK")
"""},

    # =========================================================================
    # GITHUB
    # =========================================================================
    {"name": "github_main", "category": "github", "expect": "GH_MAIN_OK", "inline": """
goto("https://github.com/", timeout=25)
text = snap(max_chars=4000)
assert "GitHub" in text, text[:300]
print("GH_MAIN_OK")
"""},

    {"name": "github_explore", "category": "github", "expect": "GH_EXPLORE_OK", "inline": """
goto("https://github.com/explore", timeout=25)
text = snap(max_chars=4000)
assert "Explore" in text or "GitHub" in text, text[:300]
print("GH_EXPLORE_OK")
"""},

    {"name": "github_anthropic_org", "category": "github", "expect": "GH_ORG_OK", "inline": """
goto("https://github.com/anthropics", timeout=25)
text = snap(max_chars=4000)
assert "anthropic" in text.lower(), text[:300]
print("GH_ORG_OK")
"""},

    {"name": "github_eval_origin", "category": "github", "expect": "GH_ORIGIN_OK", "inline": """
goto("https://github.com/", timeout=25)
o = eval_js("location.origin")
assert o == "https://github.com", o
print("GH_ORIGIN_OK", o)
"""},

    {"name": "github_repo_readme", "category": "github", "expect": "GH_REPO_OK", "inline": """
goto("https://github.com/python/cpython", timeout=25)
text = snap(max_chars=5000)
assert "cpython" in text.lower() or "Python" in text, text[:300]
print("GH_REPO_OK")
"""},

    # =========================================================================
    # example.* — IANA-reserved, ultra-stable
    # =========================================================================
    {"name": "example_com", "category": "example", "expect": "EX_COM_OK", "inline": """
goto("https://example.com/", timeout=15)
text = snap(max_chars=2000)
assert "Example Domain" in text, text[:200]
print("EX_COM_OK")
"""},

    {"name": "example_org", "category": "example", "expect": "EX_ORG_OK", "inline": """
goto("https://example.org/", timeout=15)
text = snap(max_chars=2000)
assert "Example Domain" in text, text[:200]
print("EX_ORG_OK")
"""},

    {"name": "example_net", "category": "example", "expect": "EX_NET_OK", "inline": """
goto("https://example.net/", timeout=15)
text = snap(max_chars=2000)
assert "Example Domain" in text, text[:200]
print("EX_NET_OK")
"""},

    {"name": "example_chained_3", "category": "example", "expect": "EX_CHAIN_OK", "inline": """
goto("https://example.com/", timeout=15)
goto("https://example.org/", timeout=15)
goto("https://example.net/", timeout=15)
href = eval_js("location.href")
assert "example.net" in href, href
print("EX_CHAIN_OK")
"""},

    {"name": "example_localstorage", "category": "example", "expect": "EX_LS_OK", "inline": """
goto("https://example.com/", timeout=15)
eval_js("localStorage.setItem('soak','v1')")
v = eval_js("localStorage.getItem('soak')")
assert v == "v1", v
print("EX_LS_OK")
"""},

    {"name": "example_cookie", "category": "example", "expect": "EX_COOKIE_OK", "inline": """
goto("https://example.com/", timeout=15)
eval_js("document.cookie='soak=ok; path=/'")
v = eval_js("document.cookie")
assert "soak=ok" in v, v
print("EX_COOKIE_OK")
"""},

    {"name": "example_session_storage", "category": "example", "expect": "EX_SESS_OK", "inline": """
goto("https://example.com/", timeout=15)
eval_js("sessionStorage.setItem('k','v')")
v = eval_js("sessionStorage.getItem('k')")
assert v == "v", v
print("EX_SESS_OK")
"""},

    {"name": "example_history_pushstate", "category": "example", "expect": "EX_PUSH_OK", "inline": """
goto("https://example.com/", timeout=15)
eval_js("history.pushState({}, '', '#new')")
h = eval_js("location.hash")
assert h == "#new", h
print("EX_PUSH_OK")
"""},

    # =========================================================================
    # httpbin — JSON/HTTP testing
    # =========================================================================
    {"name": "httpbin_get", "category": "httpbin", "expect": "HB_GET_OK", "inline": """
import json
goto("https://httpbin.org/get", timeout=25)
body = eval_js("document.body.innerText")
data = json.loads(body)
assert "url" in data and "httpbin.org/get" in data["url"], data
print("HB_GET_OK")
"""},

    {"name": "httpbin_user_agent", "category": "httpbin", "expect": "HB_UA_OK", "inline": """
import json
goto("https://httpbin.org/user-agent", timeout=25)
body = eval_js("document.body.innerText")
data = json.loads(body)
assert "user-agent" in data, data
assert "Chrome" in data["user-agent"] or "Mozilla" in data["user-agent"], data
print("HB_UA_OK")
"""},

    {"name": "httpbin_status_200", "category": "httpbin", "expect": "HB_200_OK", "inline": """
goto("https://httpbin.org/status/200", timeout=20)
href = eval_js("location.href")
assert "200" in href, href
print("HB_200_OK")
"""},

    {"name": "httpbin_html", "category": "httpbin", "expect": "HB_HTML_OK", "inline": """
goto("https://httpbin.org/html", timeout=25)
text = snap(max_chars=3000)
# httpbin /html returns a Moby-Dick chapter
assert "Herman Melville" in text or "Moby" in text or "whale" in text.lower(), text[:300]
print("HB_HTML_OK")
"""},

    {"name": "httpbin_redirect_2", "category": "httpbin", "expect": "HB_REDIR_OK", "inline": """
goto("https://httpbin.org/redirect/2", timeout=25)
href = eval_js("location.href")
assert "httpbin.org" in href, href
print("HB_REDIR_OK")
"""},

    # =========================================================================
    # DuckDuckGo (login-free search)
    # =========================================================================
    {"name": "ddg_main", "category": "ddg", "expect": "DDG_MAIN_OK", "inline": """
goto("https://duckduckgo.com/", timeout=25)
text = snap(max_chars=3000)
assert "DuckDuckGo" in text or "duck" in text.lower(), text[:300]
print("DDG_MAIN_OK")
"""},

    {"name": "ddg_search_results", "category": "ddg", "expect": "DDG_SEARCH_OK", "inline": """
goto("https://duckduckgo.com/?q=python+programming", timeout=25)
text = snap(max_chars=4000)
assert "python" in text.lower(), text[:300]
print("DDG_SEARCH_OK")
"""},

    {"name": "ddg_search_form_fill", "category": "ddg", "expect": "DDG_FILL_OK", "inline": """
goto("https://duckduckgo.com/", timeout=25)
v = eval_js("(document.querySelector('input[name=q]')||document.querySelector('input[type=text]'))?.tagName||'NONE'")
assert v == "INPUT", v
print("DDG_FILL_OK", v)
"""},

    # =========================================================================
    # MDN
    # =========================================================================
    {"name": "mdn_main", "category": "mdn", "expect": "MDN_MAIN_OK", "inline": """
goto("https://developer.mozilla.org/en-US/", timeout=25)
text = snap(max_chars=4000)
assert "MDN" in text or "Mozilla" in text or "Web" in text, text[:300]
print("MDN_MAIN_OK")
"""},

    {"name": "mdn_javascript", "category": "mdn", "expect": "MDN_JS_OK", "inline": """
goto("https://developer.mozilla.org/en-US/docs/Web/JavaScript", timeout=25)
text = snap(max_chars=4000)
assert "JavaScript" in text, text[:300]
print("MDN_JS_OK")
"""},

    {"name": "mdn_eval_title", "category": "mdn", "expect": "MDN_TITLE_OK", "inline": """
goto("https://developer.mozilla.org/en-US/docs/Web/HTML", timeout=25)
title = eval_js("document.title")
assert "HTML" in title, title
print("MDN_TITLE_OK", title[:60])
"""},

    # =========================================================================
    # Bing (login-free search)
    # =========================================================================
    {"name": "bing_main", "category": "bing", "expect": "BING_MAIN_OK", "inline": """
goto("https://www.bing.com/", timeout=25)
text = snap(max_chars=3000)
# Bing redirects to localized domain. Chinese variant often omits "Bing" in
# visible text but always has navigation hints "国际版" / "Images" / "搜索".
assert ("Bing" in text or "bing" in text.lower()
        or "搜索" in text or "国际版" in text or "Images" in text), text[:300]
print("BING_MAIN_OK")
"""},

    {"name": "bing_search", "category": "bing", "expect": "BING_SEARCH_OK", "inline": """
goto("https://www.bing.com/search?q=python+language", timeout=25)
text = snap(max_chars=4000)
assert "python" in text.lower(), text[:300]
print("BING_SEARCH_OK")
"""},

    # =========================================================================
    # archive.org / iana / cdnjs — additional stable real-world targets
    # =========================================================================
    # archive.org removed 2026-05-24 — Cloudflare-blocks home page from this
    # network (0/8 pass rate single-thread + multi-thread). Replaced with w3.org
    # which is similarly stable and not behind a challenge wall.
    {"name": "w3_main", "category": "misc_real", "expect": "W3_OK", "inline": """
goto("https://www.w3.org/", timeout=20)
text = snap(max_chars=3000)
assert "W3C" in text or "Web" in text, text[:300]
print("W3_OK")
"""},

    {"name": "iana_root", "category": "misc_real", "expect": "IANA_OK", "inline": """
goto("https://www.iana.org/", timeout=20)
text = snap(max_chars=3000)
assert "IANA" in text or "iana" in text.lower(), text[:300]
print("IANA_OK")
"""},

    {"name": "cdnjs_main", "category": "misc_real", "expect": "CDNJS_OK", "inline": """
goto("https://cdnjs.com/", timeout=25)
text = snap(max_chars=3000)
assert "cdnjs" in text.lower() or "CDN" in text, text[:300]
print("CDNJS_OK")
"""},

    # =========================================================================
    # Screenshots on real sites
    # =========================================================================
    {"name": "shot_example", "category": "shot", "expect": "SHOT_EX_OK", "inline": """
import os
goto("https://example.com/", timeout=15)
p = os.path.join(r\"""" + TMPDIR + """\", "bh_soak_ex.png").replace(chr(92), "/")
shot(p)
sz = os.path.getsize(p)
assert sz > 500, sz
print("SHOT_EX_OK", sz)
"""},

    {"name": "shot_wikipedia", "category": "shot", "expect": "SHOT_WIKI_OK", "inline": """
import os
goto("https://en.wikipedia.org/wiki/Main_Page", timeout=25)
p = os.path.join(r\"""" + TMPDIR + """\", "bh_soak_wiki.png").replace(chr(92), "/")
shot(p)
sz = os.path.getsize(p)
assert sz > 1000, sz
print("SHOT_WIKI_OK", sz)
"""},

    # =========================================================================
    # CRITICAL REGRESSIONS — keep these synthetic, they test BH primitives
    # not site behavior.
    # =========================================================================
    # close_tab self-heal (commit f7d72be #1) — runs against real sites for
    # extra reality; these MUST pass after this round's bootstrap fix.
    {"name": "lifecycle_close_then_wiki", "category": "lifecycle", "expect": "HEAL_WIKI_OK", "inline": """
close_tab()
goto("https://en.wikipedia.org/wiki/Main_Page", timeout=20)
text = snap(max_chars=2000)
assert "Wikipedia" in text, text[:200]
print("HEAL_WIKI_OK")
"""},

    {"name": "lifecycle_close_then_github", "category": "lifecycle", "expect": "HEAL_GH_OK", "inline": """
close_tab()
goto("https://github.com/", timeout=25)
text = snap(max_chars=2000)
assert "GitHub" in text, text[:200]
print("HEAL_GH_OK")
"""},

    {"name": "lifecycle_close_then_example", "category": "lifecycle", "expect": "HEAL_EX_OK", "inline": """
close_tab()
goto("https://example.com/", timeout=15)
href = eval_js("location.href")
assert "example.com" in href, href
print("HEAL_EX_OK")
"""},

    {"name": "lifecycle_close_then_eval_simple", "category": "lifecycle", "expect": "HEAL_EVAL_OK", "inline": """
close_tab()
v = eval_js("1+1")
assert v == 2, v
print("HEAL_EVAL_OK")
"""},

    {"name": "lifecycle_close_loop_3sites", "category": "lifecycle", "expect": "HEAL_LOOP_OK", "inline": """
close_tab()
goto("https://example.com/", timeout=15)
close_tab()
goto("https://example.org/", timeout=15)
close_tab()
goto("https://example.net/", timeout=15)
href = eval_js("location.href")
assert "example.net" in href, href
print("HEAL_LOOP_OK")
"""},

    # legacy — must still raise
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

    {"name": "legacy_cdp_helper", "category": "legacy", "expect": "CDP_OK", "inline": """
from browser_harness.helpers import cdp
out = cdp("Target.getTargets")
assert "targetInfos" in out
print("CDP_OK", len(out["targetInfos"]))
"""},

    {"name": "legacy_js_helper", "category": "legacy", "expect": "JS_HELPER_OK", "inline": """
from browser_harness.helpers import js
goto("https://example.com/", timeout=15)
v = js("2+2")
assert v == 4 or v == "4", v
print("JS_HELPER_OK", v)
"""},

    # error — eval must raise (commit 1afb010 evaluate_agent fix regression)
    {"name": "error_eval_throw_custom", "category": "error", "expect": "THROW_RAISES", "inline": """
goto("https://example.com/", timeout=15)
try:
    eval_js("throw new Error('hello-soak')")
    print("FAIL: did not raise")
except Exception as e:
    assert "hello-soak" in str(e) or "Error" in str(e), str(e)
    print("THROW_RAISES")
"""},

    {"name": "error_eval_syntax", "category": "error", "expect": "SYNTAX_RAISES", "inline": """
goto("https://example.com/", timeout=15)
try:
    eval_js("syntax @#$%")
    print("FAIL: did not raise")
except Exception as e:
    print("SYNTAX_RAISES", type(e).__name__)
"""},

    {"name": "error_eval_runtime", "category": "error", "expect": "RUNTIME_RAISES", "inline": """
goto("https://example.com/", timeout=15)
try:
    eval_js("undefined.foo.bar")
    print("FAIL: did not raise")
except Exception as e:
    print("RUNTIME_RAISES", type(e).__name__)
"""},

    {"name": "error_invalid_selector", "category": "error", "expect": "SEL_RAISES", "inline": """
goto("https://example.com/", timeout=15)
try:
    fill("###@@@badsel", "x")
    print("FAIL: did not raise")
except Exception as e:
    print("SEL_RAISES", type(e).__name__)
"""},

    # =========================================================================
    # No-browser-call (process startup overhead test)
    # =========================================================================
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
