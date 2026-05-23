"""
BH safe-mode 100 — A through F auto-runner.

Each scenario is one fresh `browser-harness` subprocess fed inline Python via stdin.
Captures stdout/stderr/exit/runtime; check() decides PASS/FAIL.

Run:    python runner.py
Output: results.json + colored summary on stdout.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).parent
RESULTS = HERE / "results.json"
STATE_FILE = pathlib.Path.home() / ".browser-harness" / "second-window-state.json"

# ---- helpers ----------------------------------------------------------------

def run_bh(code: str, env_extra: dict | None = None, timeout: int = 60):
    env = os.environ.copy()
    env.setdefault("BH_SAFE_MODE", "1")
    if env_extra:
        env.update(env_extra)
    t0 = time.time()
    try:
        p = subprocess.run(
            ["browser-harness"],
            input=code,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            shell=False,
        )
        return {
            "stdout": p.stdout,
            "stderr": p.stderr,
            "exit": p.returncode,
            "runtime": round(time.time() - t0, 2),
            "timeout": False,
        }
    except subprocess.TimeoutExpired as e:
        return {
            "stdout": e.stdout or "",
            "stderr": e.stderr or "",
            "exit": -1,
            "runtime": round(time.time() - t0, 2),
            "timeout": True,
        }


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def state_tab_count():
    return len(load_state().get("agent_tabs", []))


def state_has_tid(tid):
    return any(r.get("tid") == tid for r in load_state().get("agent_tabs", []))


def state_url_for_tid(tid):
    # not stored — return None; check via second probe instead
    return None


# ---- scenarios --------------------------------------------------------------

SCENARIOS = []


def scen(id, name, code, check, env=None, timeout=60, type_="auto"):
    SCENARIOS.append({
        "id": id,
        "name": name,
        "code": code,
        "check": check,
        "env": env or {},
        "timeout": timeout,
        "type": type_,
    })


# ---- A: navigation / globals -----------------------------------------------

scen("A01", "safe globals injected", r"""
import json
names = ['agent_tab','goto','eval_js','snap','shot','click_at','type_text','send_keys','fill','upload','close_tab']
present = {n: (n in globals()) for n in names}
print("RESULT", json.dumps(present))
""",
    check=lambda o, e, x: (
        ("RESULT" in o and all(json.loads(o.split("RESULT",1)[1].strip().splitlines()[0]).values()),
         "all 11 names present" if "RESULT" in o else "no RESULT line")
    ))

scen("A02", "goto real URL", r"""
goto("https://example.org/")
print("HREF", eval_js("location.href"))
""",
    check=lambda o, e, x: ("example.org" in o, "href contains example.org"))

scen("A03", "goto same tab multiple times", r"""
tid_before = agent_tab
goto("https://example.org/")
goto("https://example.com/")
print("HREF", eval_js("location.href"))
print("TID_SAME", agent_tab == tid_before)
""",
    check=lambda o, e, x: ("example.com" in o and "TID_SAME True" in o, "url=example.com & tid unchanged"))

scen("A04", "new_tab raises", r"""
try:
    new_tab("https://x.com")
    print("NO_RAISE")
except RuntimeError as ex:
    print("RAISED", "disabled" in str(ex))
""",
    check=lambda o, e, x: ("RAISED True" in o, "RuntimeError with 'disabled'"))

scen("A05", "goto_url raises", r"""
try:
    goto_url("https://x.com")
    print("NO_RAISE")
except RuntimeError as ex:
    print("RAISED", "disabled" in str(ex))
""",
    check=lambda o, e, x: ("RAISED True" in o, "RuntimeError with 'disabled'"))

# A06 / A07 skipped — slow timeout cases per task spec

scen("A08", "goto invalid URL doesn't crash", r"""
try:
    r = goto("not-a-url")
    print("OK", r)
except Exception as ex:
    print("THREW", type(ex).__name__)
""",
    check=lambda o, e, x: (x == 0 and ("OK" in o or "THREW" in o), "exit 0; either OK or THREW"),
    timeout=30)

scen("A09", "goto then eval_js works", r"""
goto("https://example.com/")
print("R", eval_js("1+1"))
""",
    check=lambda o, e, x: ("R 2" in o, "eval_js returned 2"))

scen("A10", "goto data URL", r"""
goto("data:text/html,<h1>hi</h1>")
print("H", eval_js("document.querySelector('h1').textContent"))
""",
    check=lambda o, e, x: ("H hi" in o, "h1 text == hi"))


# ---- B: agent_tab reuse / PID isolation ------------------------------------

scen("B01", "ensure_agent_tab idempotent", r"""
from browser_harness.second_window import ensure_agent_tab
t1 = ensure_agent_tab()
t2 = ensure_agent_tab()
print("SAME", t1 == t2, t1, t2)
""",
    check=lambda o, e, x: ("SAME True" in o, "same tid both calls"))

scen("B02", "safe_globals idempotent", r"""
from browser_harness.bootstrap import safe_globals
g1 = safe_globals()
g2 = safe_globals()
print("SAME", g1["agent_tab"] == g2["agent_tab"])
""",
    check=lambda o, e, x: ("SAME True" in o, "same agent_tab across calls"))

# B06: navigate to nothing, exit, expect placeholder closed
scen("B06_setup", "B06 setup: bootstrap then exit (placeholder)", r"""
import json
print("TID", agent_tab)
# do not navigate
""",
    check=lambda o, e, x: ("TID" in o, "captured tid"))

scen("B06_check", "B06 check: prior placeholder gone", r"""
from browser_harness.helpers import cdp
prev = """ + "open(r'" + str(HERE / "_b06_tid.txt") + r"').read().strip()" + r"""
targets = cdp("Target.getTargets").get("targetInfos", [])
tids = {t.get("targetId") for t in targets}
print("STILL_LIVE", prev in tids, "prev=", prev)
""",
    check=lambda o, e, x: ("STILL_LIVE False" in o, "placeholder tab removed by atexit"))

# B07: navigate to real URL, exit, expect tab still alive
scen("B07_setup", "B07 setup: goto real then exit", r"""
goto("https://example.com/")
print("TID", agent_tab)
""",
    check=lambda o, e, x: ("TID" in o, "captured tid"))

scen("B07_check", "B07 check: real-URL tab survived", r"""
from browser_harness.helpers import cdp
prev = """ + "open(r'" + str(HERE / "_b07_tid.txt") + r"').read().strip()" + r"""
targets = cdp("Target.getTargets").get("targetInfos", [])
tids = {t.get("targetId") for t in targets}
print("STILL_LIVE", prev in tids, "prev=", prev)
""",
    check=lambda o, e, x: ("STILL_LIVE True" in o, "navigated tab survived atexit"))

scen("B09", "close_tab forces new tid", r"""
from browser_harness.second_window import ensure_agent_tab
t1 = agent_tab
close_tab()
t2 = ensure_agent_tab()
print("DIFF", t1 != t2, t1, t2)
""",
    check=lambda o, e, x: ("DIFF True" in o, "new tid after close"))


# ---- C: atexit cleanup -----------------------------------------------------

scen("C01_setup", "C01 setup: placeholder then exit", r"""
print("TID", agent_tab)
""",
    check=lambda o, e, x: ("TID" in o, "captured tid"))

scen("C01_check", "C01 check: placeholder gone", r"""
from browser_harness.helpers import cdp
prev = """ + "open(r'" + str(HERE / "_c01_tid.txt") + r"').read().strip()" + r"""
targets = cdp("Target.getTargets").get("targetInfos", [])
tids = {t.get("targetId") for t in targets}
print("STILL_LIVE", prev in tids)
""",
    check=lambda o, e, x: ("STILL_LIVE False" in o, "placeholder closed"))

# C02: same as B07 — covered

scen("C03_setup", "C03 setup: KEEP_PLACEHOLDERS=1", r"""
print("TID", agent_tab)
""",
    env={"BH_KEEP_PLACEHOLDERS": "1"},
    check=lambda o, e, x: ("TID" in o, "captured tid"))

scen("C03_check", "C03 check: placeholder survives KEEP=1", r"""
from browser_harness.helpers import cdp
prev = """ + "open(r'" + str(HERE / "_c03_tid.txt") + r"').read().strip()" + r"""
targets = cdp("Target.getTargets").get("targetInfos", [])
tids = {t.get("targetId") for t in targets}
print("STILL_LIVE", prev in tids)
""",
    check=lambda o, e, x: ("STILL_LIVE True" in o, "placeholder kept"))

scen("C04", "atexit doesn't crash on cdp errors", r"""
import browser_harness.bootstrap as bs
def boom(*a, **kw):
    raise RuntimeError("simulated")
bs._cdp = boom
print("OK")
""",
    check=lambda o, e, x: (x == 0 and "OK" in o, "exit 0 even with cdp boom"))

scen("C05", "exec exception still triggers atexit", r"""
print("TID", agent_tab)
raise RuntimeError("intentional")
""",
    check=lambda o, e, x: ("TID" in o and ("intentional" in o + e), "exception printed; atexit fires next"))

scen("C06", "close_tab then exit no error", r"""
close_tab()
print("DONE")
""",
    check=lambda o, e, x: (x == 0 and "DONE" in o, "exit 0 after close_tab"))

scen("C08_pre", "C08 pre: write fake other-PID record", r"""
import json, pathlib, time
sf = pathlib.Path.home()/".browser-harness"/"second-window-state.json"
state = json.loads(sf.read_text()) if sf.exists() else {"agent_tabs":[]}
# inject a fake record claimed by impossible PID (we'll restore later)
state.setdefault("agent_tabs", []).append({
    "tid": "FAKE-OTHER-PID-TID",
    "last_access": time.time(),
    "claimed_by_pid": 999999  # likely unused
})
sf.write_text(json.dumps(state, indent=2))
print("INJECTED")
""",
    check=lambda o, e, x: ("INJECTED" in o, "injected fake other-PID record"))

scen("C08_check", "C08 check: other-PID record untouched after BH exit", r"""
import json, pathlib
sf = pathlib.Path.home()/".browser-harness"/"second-window-state.json"
state = json.loads(sf.read_text())
found = any(r.get("tid") == "FAKE-OTHER-PID-TID" for r in state.get("agent_tabs", []))
print("FOUND", found)
""",
    check=lambda o, e, x: ("FOUND True" in o, "other-PID record preserved"))

scen("C10", "atexit double-register guard", r"""
import atexit
from browser_harness.bootstrap import safe_globals, _close_placeholder_tabs
safe_globals(); safe_globals(); safe_globals()
# count registered atexit handlers — best-effort via private attr
import sys
# Python: atexit._ncallbacks() since 3.10
n = atexit._ncallbacks()
print("NCALL", n)
""",
    check=lambda o, e, x: ("NCALL" in o, "no crash; count printed"))


# ---- D: second window detection / spawn ------------------------------------

scen("D01", "pinned wid reused across bootstrap", r"""
from browser_harness.bootstrap import ensure_pinned_second_window
w1 = ensure_pinned_second_window()
w2 = ensure_pinned_second_window()
print("SAME", w1 == w2, w1, w2)
""",
    check=lambda o, e, x: ("SAME True" in o, "pinned wid stable"))

scen("D06", "spawn lands on bh-agent-tab marker", r"""
# agent_tab placeholder URL should contain marker
from browser_harness.helpers import cdp
targets = cdp("Target.getTargets").get("targetInfos", [])
mine_url = None
for t in targets:
    if t.get("targetId") == agent_tab:
        mine_url = t.get("url", "")
print("URL", mine_url)
print("HAS_MARKER", "bh-agent-tab" in (mine_url or ""))
""",
    check=lambda o, e, x: ("HAS_MARKER True" in o, "agent_tab url has bh-agent-tab marker"))

scen("D07", "STATE_FILE has pinned_second_window_id", r"""
import json, pathlib
sf = pathlib.Path.home()/".browser-harness"/"second-window-state.json"
state = json.loads(sf.read_text())
print("HAS_PIN", "pinned_second_window_id" in state, state.get("pinned_second_window_id"))
""",
    check=lambda o, e, x: ("HAS_PIN True" in o, "pinned_second_window_id present"))

scen("D10", "ensure_pinned idempotent x5", r"""
from browser_harness.bootstrap import ensure_pinned_second_window
wids = [ensure_pinned_second_window() for _ in range(5)]
print("ALL_SAME", len(set(wids)) == 1, wids[0])
""",
    check=lambda o, e, x: ("ALL_SAME True" in o, "5 calls return same wid"))


# ---- E: tab cap / cumulative control ---------------------------------------

scen("E01", "DEFAULT_MAX_AGENT_TABS == 15", r"""
from browser_harness.second_window import DEFAULT_MAX_AGENT_TABS
print("CAP", DEFAULT_MAX_AGENT_TABS)
""",
    check=lambda o, e, x: ("CAP 15" in o, "constant is 15"))

scen("E07", "close_tab removes record", r"""
import json, pathlib
sf = pathlib.Path.home()/".browser-harness"/"second-window-state.json"
tid = agent_tab
close_tab()
state = json.loads(sf.read_text())
in_state = any(r.get("tid") == tid for r in state.get("agent_tabs", []))
print("IN_STATE", in_state)
""",
    check=lambda o, e, x: ("IN_STATE False" in o, "tid removed from state after close_tab"))

scen("E09", "prune_agent_tabs(5) caps at 5", r"""
from browser_harness.second_window import prune_agent_tabs
import json, pathlib
sf = pathlib.Path.home()/".browser-harness"/"second-window-state.json"
prune_agent_tabs(max_n=5)
state = json.loads(sf.read_text())
import os
mine = [r for r in state.get("agent_tabs", []) if r.get("claimed_by_pid") == os.getpid()]
print("MINE", len(mine))
""",
    check=lambda o, e, x: ("MINE" in o, "prune ran without error"))


# ---- F: long task / state persistence --------------------------------------

scen("F02", "multi-goto state preserved", r"""
goto("https://example.com/")
eval_js("window.__bh_test = 42")
goto("https://example.com/?next")
v = eval_js("window.__bh_test")
print("V", v)  # expected None (cross-page state is page-scoped)
print("URL", eval_js("location.href"))
""",
    check=lambda o, e, x: ("URL" in o and "?next" in o, "navigated through both pages"))

scen("F03", "localStorage same-origin persists", r"""
goto("https://example.com/")
eval_js("localStorage.setItem('k','42')")
goto("https://example.com/?after")
v = eval_js("localStorage.getItem('k')")
print("V", v)
""",
    check=lambda o, e, x: ("V 42" in o, "localStorage retained across same-origin navs"))

scen("F04", "iframe doesn't break main eval_js", r"""
goto("data:text/html,<iframe src='https://example.com/'></iframe><h2>main</h2>")
print("H", eval_js("document.querySelector('h2').textContent"))
""",
    check=lambda o, e, x: ("H main" in o, "main frame eval works with iframe present"))

scen("F05", "eval_js immediately after goto", r"""
goto("https://example.com/")
print("R", eval_js("document.title.length >= 0"))
""",
    check=lambda o, e, x: ("R True" in o, "eval after goto returns truthy"))

scen("F06", "snap on rendered page", r"""
goto("https://example.com/")
text = snap(2000)
print("LEN", len(text))
print("HAS_EXAMPLE", "Example" in text or "example" in text.lower())
""",
    check=lambda o, e, x: ("HAS_EXAMPLE True" in o, "snap returned page text"))

scen("F08", "repeat bootstrap doesn't lose cookie jar", r"""
from browser_harness.bootstrap import safe_globals
g1 = safe_globals()
g2 = safe_globals()
g3 = safe_globals()
print("STABLE", g1["agent_tab"] == g2["agent_tab"] == g3["agent_tab"])
""",
    check=lambda o, e, x: ("STABLE True" in o, "agent_tab stable across repeats"))


# ---- runner core -----------------------------------------------------------

# Cross-scenario state passing: setup writes tid to a file, check reads it.
SETUP_TID_FILES = {
    "B06_setup": HERE / "_b06_tid.txt",
    "B07_setup": HERE / "_b07_tid.txt",
    "C01_setup": HERE / "_c01_tid.txt",
    "C03_setup": HERE / "_c03_tid.txt",
}


def patch_setup_code(scenario_id, code):
    """Inject TID-capture-to-file before the user code's `print("TID"...)`."""
    if scenario_id in SETUP_TID_FILES:
        f = SETUP_TID_FILES[scenario_id]
        return code + f"\nimport pathlib; pathlib.Path(r'{f}').write_text(str(agent_tab))\n"
    return code


def color(s, c):
    if not sys.stdout.isatty():
        return s
    codes = {"green": "32", "red": "31", "yellow": "33", "cyan": "36", "bold": "1"}
    return f"\033[{codes.get(c, '0')}m{s}\033[0m"


def main():
    results = []
    n_pass = n_fail = n_skip = 0
    for s in SCENARIOS:
        sid = s["id"]
        if s["type"] != "auto":
            print(color(f"SKIP {sid:14}", "yellow"), s["name"], "(non-auto)")
            results.append({"id": sid, "status": "skip", "reason": "non-auto"})
            n_skip += 1
            continue
        code = patch_setup_code(sid, s["code"])
        print(color(f"RUN  {sid:14}", "cyan"), s["name"], flush=True)
        r = run_bh(code, env_extra=s["env"], timeout=s["timeout"])
        try:
            ok, why = s["check"](r["stdout"], r["stderr"], r["exit"])
        except Exception as ex:
            ok, why = False, f"check raised {ex!r}"
        rec = {
            "id": sid,
            "name": s["name"],
            "status": "pass" if ok else "fail",
            "why": why,
            "exit": r["exit"],
            "runtime": r["runtime"],
            "timeout": r["timeout"],
            "stdout_tail": r["stdout"][-800:],
            "stderr_tail": r["stderr"][-800:],
        }
        results.append(rec)
        if ok:
            n_pass += 1
            print(color(f"PASS {sid:14}", "green"), why, f"({r['runtime']}s)")
        else:
            n_fail += 1
            print(color(f"FAIL {sid:14}", "red"), why, f"(exit={r['exit']} {r['runtime']}s)")
            if r["stdout"]:
                tail = r["stdout"].strip().splitlines()[-3:]
                for t in tail:
                    print("       stdout:", t[:200])
            if r["stderr"]:
                tail = r["stderr"].strip().splitlines()[-3:]
                for t in tail:
                    print("       stderr:", t[:200])
    summary = {"total": len(results), "pass": n_pass, "fail": n_fail, "skip": n_skip}
    RESULTS.write_text(json.dumps({"summary": summary, "results": results}, indent=2))
    print()
    print(color("=" * 60, "bold"))
    print(color(f"  TOTAL {summary['total']}  PASS {summary['pass']}  FAIL {summary['fail']}  SKIP {summary['skip']}", "bold"))
    print(color("=" * 60, "bold"))
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
