"""
Second-window-respecting agent tab management.

Hard rule: agent tabs MUST live in the user's existing second Chrome window,
spawned via window.open from an existing tab in that window — NEVER abuse
Target.createTarget which lands in active window or steals focus.

Key APIs:
    ensure_agent_tab()             # auto-detect + auto-spawn + persistent reuse
    navigate_agent(tid, url)
    snapshot_agent(tid)            # innerText
    screenshot_agent(tid, path)
    save_as_pdf_agent(tid, path)
    evaluate_agent(tid, expr)
    fill_agent(tid, selector, value)
    click_at_agent(tid, x, y)      # alias: mouse_click_agent
    key_type_agent(tid, text)
    send_keys_agent(tid, [keys])
    upload_agent(tid, selector, [file_paths])
    list_agent_tabs()
    find_agent_tab(url_substring)
    close_agent_tab(tid)
    prune_agent_tabs(max_n=25)

Internal:
    detect_second_window()         # heuristic: window with fewest tabs
    spawn_second_window()          # chrome.exe --new-window (steals focus once)

Optional acceleration: if `bh_extension_client.is_available()` is True (the
companion Chrome extension is installed and connected), spawn goes through
chrome.tabs.create({active:false, windowId:X}) for ZERO focus steal. Fallback
is the CDP `window.open` path with focus-restore mitigation.
"""

import json, time, pathlib, subprocess, base64, os, contextlib
from collections import defaultdict

from .helpers import cdp


# ---------- config ----------

STATE_DIR = pathlib.Path.home() / ".browser-harness"
STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "second-window-state.json"

# Domain fingerprints — last-resort heuristic used only when neither pin nor
# extension `focused=True` give an authoritative answer. A window containing
# ANY user-domain URL is treated as the user's main window — agent tabs MUST
# NOT spawn there, even if it has more "work" tabs by count.
USER_DOMAINS = (
    "bilibili.com", "youtube.com", "douyin.com", "huya.com",
    "twitch.tv", "weibo.com", "qq.com/", "wegame",
    "google.com/search", "baidu.com", "zhihu.com",
    "twitter.com/home", "x.com/home",
    "/maps", "tieba.baidu",
    # User's daily-use admin/management surfaces (user told 2026-05-21):
    "127.0.0.1:8090/admin",  # sub2api admin panel — user opens it on main browser
    "localhost:8090/admin",
)
WORK_DOMAINS = (
    "nexusmods.com", "doubao.com/chat", "m365.cloud.microsoft",
    "chat.openai.com", "claude.ai/", "kimi.com", "kimi.moonshot",
    "example.com/?bh-agent-tab",  # our own marker
    # Note: NOT "127.0.0.1:" — that's user's admin endpoints; see USER_DOMAINS.
)

# Locate chrome.exe (Windows). Override via BH_CHROME_EXE env var if needed.
def _find_chrome_exe():
    env = os.environ.get("BH_CHROME_EXE")
    if env and pathlib.Path(env).exists():
        return env
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if pathlib.Path(c).exists():
            return c
    return None

CHROME_EXE = _find_chrome_exe()

AGENT_TAB_MARKER = "bh-agent-tab"
AGENT_SPAWN_URL = f"https://example.com/?{AGENT_TAB_MARKER}=1"
DEFAULT_MAX_AGENT_TABS = 15  # cap chosen by user 2026-05-24 (was 25) — atexit placeholder cleanup means cap is now a backstop, not the primary GC
SPAWN_FOCUS_WARNING = (
    "[bh.second_window] No second window detected. "
    "Spawning chrome --new-window (OS will steal focus once — unavoidable)."
)


# ---------- state ----------

def _load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"agent_tabs": []}


def _save_state(s):
    """Atomic write: tmp + os.replace so concurrent readers never see a torn file."""
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(s, indent=2))
    os.replace(str(tmp), str(STATE_FILE))


_STATE_LOCK_PATH = STATE_DIR / "second-window-state.lock"


@contextlib.contextmanager
def _state_mutex(timeout=15.0):
    """Cross-process file-lock for STATE read-modify-write.

    Without this, N parallel BH processes each do load→mutate→save concurrently
    and lose-update each other's records. The TABS still exist in CDP but their
    STATE records vanish — orphans that prune_agent_tabs can't see, so the
    DEFAULT_MAX_AGENT_TABS cap silently leaks (3-parallel verify 2026-05-24:
    STATE.agent_tabs=14 but actual second-window tab count=29).

    Stale lock (mtime > 30s) is reaped — holder probably crashed mid-RMW.
    """
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(_STATE_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, f"{os.getpid()}\n{time.time()}".encode())
            finally:
                os.close(fd)
            break
        except FileExistsError:
            try:
                age = time.time() - _STATE_LOCK_PATH.stat().st_mtime
            except FileNotFoundError:
                age = 0  # released — retry immediately
                time.sleep(0.005)
                continue
            if age > 30:
                try: _STATE_LOCK_PATH.unlink()
                except FileNotFoundError: pass
                continue  # retry the create
            if time.time() >= deadline:
                raise RuntimeError(f"STATE mutex held > {timeout}s")
            time.sleep(0.02)
    try:
        yield
    finally:
        try: _STATE_LOCK_PATH.unlink()
        except FileNotFoundError: pass


def _pid_alive(pid):
    """Check if a PID is still running. Returns True on uncertainty (be conservative)."""
    if not pid:
        return False
    try:
        if os.name == "nt":
            # tasklist is slow; use Windows API via ctypes
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
                return bool(ok) and code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False


def _record_access(tid, claim=False, nonce=None):
    """Update last_access for tid. If claim=True, also claim it for self pid.

    `nonce` is the unique tag baked into the spawn URL (see I05 fix —
    bootstrap atexit needs this to distinguish a still-on-placeholder tab
    from a tab the agent navigated to a URL that happens to contain the
    AGENT_TAB_MARKER substring). Only set on first record (initial spawn);
    subsequent _record_access calls preserve the original nonce.
    """
    my_pid = os.getpid()
    with _state_mutex():
        state = _load_state()
        tabs = state.get("agent_tabs", [])
        for r in tabs:
            if r["tid"] == tid:
                r["last_access"] = time.time()
                if claim:
                    r["claimed_by_pid"] = my_pid
                if nonce and not r.get("nonce"):
                    r["nonce"] = nonce
                _save_state(state)
                return
        rec = {"tid": tid, "last_access": time.time()}
        if claim:
            rec["claimed_by_pid"] = my_pid
        if nonce:
            rec["nonce"] = nonce
        tabs.append(rec)
        state["agent_tabs"] = tabs
        _save_state(state)


def _gc_orphan_claims(state):
    """Mutate state in place: drop claimed_by_pid for dead processes (mark orphan)."""
    for r in state.get("agent_tabs", []):
        pid = r.get("claimed_by_pid")
        if pid and not _pid_alive(pid):
            r.pop("claimed_by_pid", None)


# ---------- CDP attach helpers (no focus steal) ----------

def _attach(tid):
    return cdp("Target.attachToTarget", targetId=tid, flatten=True).get("sessionId")


def _detach(sid):
    try: cdp("Target.detachFromTarget", sessionId=sid)
    except Exception: pass


# ---------- focus-steal mitigation ----------

def _detect_main_window():
    """User's main (foreground-focused) window. Prefer extension's `focused`
    field (authoritative); fall back to tab-count heuristic when extension is
    unavailable (the heuristic is wrong when second window has more tabs than
    main, e.g. heavy NexusMods workflow — see 2026-05-20 user incident)."""
    try:
        from . import bh_extension_client as ext
        ext.start_server_if_needed()
        if ext.is_available():
            wins = ext.send_command("list_windows", timeout=3)
            if wins:
                focused = [w for w in wins if w.get("focused")]
                if focused:
                    return focused[0]["id"]
    except Exception:
        pass
    windows = _list_windows()
    if not windows:
        return None
    return max(windows.items(), key=lambda kv: len(kv[1]))[0]


# ---------- window/tab discovery ----------

def _list_windows():
    """{windowId: [(tid, url, title), ...]} for all real page tabs."""
    targets = cdp("Target.getTargets").get("targetInfos", [])
    by_window = defaultdict(list)
    for t in targets:
        if t.get("type") != "page": continue
        url = t.get("url", "")
        if url.startswith(("chrome://", "chrome-extension://", "devtools://")):
            continue
        tid = t.get("targetId")
        try:
            wid = cdp("Browser.getWindowForTarget", targetId=tid).get("windowId")
            by_window[wid].append((tid, url, t.get("title", "")))
        except Exception:
            pass
    return dict(by_window)


def _count_user_hits(urls):
    """How many tabs in this window match a user-domain fingerprint."""
    n = 0
    for u in urls:
        u = (u or "").lower()
        for d in USER_DOMAINS:
            if d in u:
                n += 1
                break
    return n


def _count_work_hits(urls):
    n = 0
    for u in urls:
        u = (u or "").lower()
        for d in WORK_DOMAINS:
            if d in u:
                n += 1
                break
    return n


def pin_second_window(wid):
    """Pin a windowId as the secondary work window. Highest-priority signal —
    `detect_second_window` will return this wid as long as the window stays
    alive in CDP. Use when the heuristic picks wrong."""
    with _state_mutex():
        state = _load_state()
        state["pinned_second_window_id"] = wid
        _save_state(state)
    return wid


def unpin_second_window():
    """Remove the pin so detect_second_window falls back to heuristics."""
    with _state_mutex():
        state = _load_state()
        state.pop("pinned_second_window_id", None)
        _save_state(state)


def detect_second_window():
    """
    Returns (windowId, [(tid,url,title),...]) for user's secondary window,
    or (None, None) if only 1 window with real tabs exists.

    Decision order (HARD RULE: never spawn into user's main window):
      1. If `state.pinned_second_window_id` is set and that window is alive,
         return it. Pin > everything.
      2. Extension reports `focused=True` for exactly one window → that one
         is the user's main; eliminate it from candidates.
      3. Among candidates, score by domain content: WORK_DOMAINS hits boost,
         USER_DOMAINS hits exclude. Any window containing user-domain URLs
         is REFUSED as a candidate (huya/bilibili/etc. = user is watching).
      4. If all candidates contain user-domain URLs → raise RuntimeError
         (refuse to guess; user must `pin_second_window(wid)`).

    Tab-count fallback is dead — the 2026-05-20 incident proved it inverts
    when the work window outgrows the main window.
    """
    state = _load_state()
    cdp_windows = _list_windows()

    # 1. Pin path — wins over all signals
    pinned = state.get("pinned_second_window_id")
    if pinned and pinned in cdp_windows:
        return pinned, cdp_windows[pinned]
    if pinned and pinned not in cdp_windows:
        # Pinned window died — drop the pin and continue to heuristics
        state.pop("pinned_second_window_id", None)
        _save_state(state)

    if len(cdp_windows) < 2:
        return None, None

    # 2. Build candidate set using extension `focused` if available
    main_wid_from_ext = None
    try:
        from . import bh_extension_client as ext
        ext.start_server_if_needed()
        if ext.is_available():
            wins = ext.send_command("list_windows", timeout=3)
            if wins:
                focused = [w for w in wins if w.get("focused")]
                if len(focused) == 1:
                    main_wid_from_ext = focused[0]["id"]
    except Exception:
        pass

    candidates = [(wid, tabs) for wid, tabs in cdp_windows.items()
                  if wid != main_wid_from_ext]
    if not candidates:
        candidates = list(cdp_windows.items())

    # 3. Domain-content scoring + user-domain exclusion (HARD GUARD).
    # Tie-breaker: smaller windowId = older window = more likely user's main
    # (chrome assigns windowIds in creation order; user told 2026-05-21
    # "main browser is on the left of taskbar" — task-order proxy = wid order).
    # So: when work_hits is equal, prefer the LARGER wid (newer = more likely
    # the secondary user opened later for work).
    def score(wid_tabs):
        wid, tabs = wid_tabs
        urls = [t[1] for t in tabs]
        if _count_user_hits(urls) > 0:
            return (-10**6, _count_work_hits(urls), wid)
        return (0, _count_work_hits(urls), wid)

    ranked = sorted(candidates, key=score, reverse=True)
    chosen_wid, chosen_tabs = ranked[0]
    chosen_urls = [t[1] for t in chosen_tabs]

    if _count_user_hits(chosen_urls) > 0:
        # Best candidate still has user content — refuse rather than pollute
        listing = "\n".join(
            f"  win={w}  user_hits={_count_user_hits([t[1] for t in ts])}  "
            f"work_hits={_count_work_hits([t[1] for t in ts])}  tabs={len(ts)}"
            for w, ts in cdp_windows.items()
        )
        raise RuntimeError(
            "second_window: every chrome window contains user-domain tabs "
            "(huya/bilibili/youtube/etc.). Refusing to spawn agent tab — "
            "would pollute user's main window.\n"
            f"Windows seen:\n{listing}\n"
            "Fix: open a clean Chrome window for agent work, then call "
            "browser_harness.second_window.pin_second_window(<wid>)."
        )

    return chosen_wid, chosen_tabs


def spawn_second_window(timeout=10):
    """Launch new chrome window, immediately minimize to taskbar.

    Behavior contract (user 2026-05-20):
      - No screen flash (window doesn't bloom in the middle of the monitor)
      - No focus steal (user's foreground app stays foreground)
      - But: window must remain inspectable — user clicks the taskbar icon
        to peek at automation progress. So we do NOT push it offscreen.

    Strategy: prefer the BH companion extension if reachable
    (chrome.windows.create with focused:false + state:'minimized' = truly
    silent birth). Subprocess path is fallback — it briefly shows a small
    window at a screen corner, then CDP minimize sends it to the taskbar
    within ~150ms. Focus is restored either way.
    """
    if not CHROME_EXE:
        raise RuntimeError("chrome.exe not found — set BH_CHROME_EXE env var")

    # NOTE: We deliberately do NOT call _restore_main_focus here (2026-05-24).
    # The old behavior — capture "main window's first tab" then activateTarget
    # on it after spawn — was a net negative: it MOVED the user's active tab
    # to whatever happened to be tab #0 of their main window. User reported
    # mid-soak: "切了我看的 B 站标签页". Chrome's natural focus-return after
    # minimize already restores main window to foreground; per-window active
    # tab is preserved by Chrome itself across minimize/raise. Anything we do
    # via Target.activateTarget is at best redundant, at worst destructive.

    # Prefer extension path: zero screen flash, zero focus steal
    try:
        from . import bh_extension_client as ext
        ext.start_server_if_needed()
        if ext.is_available():
            res = ext.send_command(
                "create_window",
                url="about:blank",
                state="minimized",
                focused=False,
                timeout=8,
            )
            if res and res.get("ok"):
                # Re-discover via CDP since extension's chrome window id != CDP windowId
                deadline = time.time() + timeout
                while time.time() < deadline:
                    wid = _detect_minimized_blank_window()
                    if wid is not None:
                        return wid
                    time.sleep(0.15)
    except Exception:
        pass

    # Fallback: subprocess path. Spawn at corner with modest size so the brief
    # pre-minimize frame is unobtrusive, then immediately CDP-minimize.
    initial = set(_list_windows().keys())
    subprocess.Popen(
        [
            CHROME_EXE,
            "--new-window",
            "--window-position=20,20",
            "--window-size=400,300",
            "about:blank",
        ],
        creationflags=0x08000000,  # CREATE_NO_WINDOW
    )
    deadline = time.time() + timeout
    new_wid = None
    while time.time() < deadline:
        time.sleep(0.15)
        new_wids = set(_list_windows().keys()) - initial
        if new_wids:
            new_wid = new_wids.pop()
            break
    if new_wid is not None:
        try:
            cdp("Browser.setWindowBounds",
                windowId=new_wid,
                bounds={"windowState": "minimized"})
        except Exception:
            pass
    # No _restore_main_focus — see top-of-function note.
    if new_wid is None:
        raise RuntimeError("spawn_second_window: timeout")
    return new_wid


def _detect_minimized_blank_window():
    """Find a CDP windowId whose only tab is about:blank (just-spawned second window)."""
    targets = cdp("Target.getTargets").get("targetInfos", [])
    by_window = defaultdict(list)
    for t in targets:
        if t.get("type") != "page":
            continue
        try:
            wid = cdp("Browser.getWindowForTarget", targetId=t["targetId"]).get("windowId")
        except Exception:
            continue
        by_window[wid].append(t)
    for wid, ts in by_window.items():
        if len(ts) == 1 and (ts[0].get("url") or "").startswith("about:blank"):
            return wid
    return None


# ---------- LRU pruning ----------

def prune_agent_tabs(max_n=DEFAULT_MAX_AGENT_TABS):
    """Close oldest agent tabs over max_n. Only prunes tabs claimed by THIS process
    (or unclaimed orphans whose owner died). Never poaches another live session's tabs.

    Also reaps CDP-only orphans: tabs that exist in the pinned second window but
    have no STATE record (typically from prior lost-update races before the
    state mutex was added). Without this, the cap silently leaked under
    concurrent fresh-mode load.
    """
    my_pid = os.getpid()
    targets = cdp("Target.getTargets").get("targetInfos", [])
    live_tids = {t.get("targetId") for t in targets if t.get("type") == "page"}

    with _state_mutex():
        state = _load_state()
        _gc_orphan_claims(state)
        records = state.get("agent_tabs", [])
        live_records = [r for r in records if r["tid"] in live_tids]
        not_mine = [r for r in live_records if r.get("claimed_by_pid") not in (my_pid, None)]
        mine_or_orphan = [r for r in live_records if r.get("claimed_by_pid") in (my_pid, None)]
        victims = []
        while len(mine_or_orphan) > max_n:
            mine_or_orphan.sort(key=lambda r: r["last_access"])
            victims.append(mine_or_orphan.pop(0))
        state["agent_tabs"] = not_mine + mine_or_orphan
        _save_state(state)

    # Close outside lock — closeTarget is slow (CDP RTT) and we don't want
    # other processes blocked on STATE while we wait on the network.
    closed = 0
    for v in victims:
        try:
            cdp("Target.closeTarget", targetId=v["tid"])
            closed += 1
        except Exception:
            pass

    # CDP-orphan reap: tabs in second window that no STATE record claims.
    # Only run when we know the second window — otherwise we'd be guessing.
    pinned = state.get("pinned_second_window_id")
    if pinned:
        try:
            windows = _list_windows()
            if pinned in windows:
                known_tids = {r["tid"] for r in state.get("agent_tabs", []) if r.get("tid")}
                # Don't touch known tabs OR our spawn placeholder URL during the same cleanup pass.
                cdp_orphans = [
                    (tid, url) for (tid, url, _title) in windows[pinned]
                    if tid not in known_tids and url not in ("about:blank",)
                ]
                # Be conservative: only close if window is over the cap. Below cap, leave them.
                excess = len(windows[pinned]) - max_n
                if excess > 0:
                    for tid, _url in cdp_orphans[:excess]:
                        try:
                            cdp("Target.closeTarget", targetId=tid)
                            closed += 1
                        except Exception:
                            pass
        except Exception:
            pass  # best-effort

    return closed


# ---------- core: ensure agent tab ----------

def ensure_agent_tab(max_tabs=DEFAULT_MAX_AGENT_TABS, prefer_extension=True):
    """
    Ensure an agent tab exists in the user's second Chrome window.
    Order of operations (HARD RULE: check before create):
      1. Detect second window. If exists, USE IT.
      2. If a previously-spawned agent tab in that second window is still
         alive (per state file), REUSE it. Don't create a new tab.
      3. Only if no existing agent tab: spawn a new one in the existing
         second window. Prefer extension path (silent), fallback to CDP
         window.open with focus restore.
      4. Only if no second window AT ALL: spawn a new chrome window via
         chrome.exe --new-window. This is the only path that may steal focus
         (Windows OS limitation).

    Returns the agent tab targetId.
    """
    # 1. Detect — DO NOT create yet
    second_wid, tabs = detect_second_window()
    if not second_wid:
        # No second window exists — must spawn one. Steals focus once.
        second_wid = spawn_second_window()
        tabs = _list_windows().get(second_wid, [])
    if not tabs:
        raise RuntimeError(f"second window {second_wid} has no usable tabs")

    # 2. Try to reuse a tab CLAIMED BY THIS PROCESS only — never poach another
    # session's tab (would cause concurrent sessions to fight over one tab,
    # leading to navigation hijacking. See 2026-05-20 doubao-vs-Nexus incident.)
    # Use browser-wide live tids (not just second_wid's tabs): detect_second_window
    # is a flaky heuristic that flips when tab counts shift, so a tab spawned in
    # round 1 may not appear "in second window" in round 2 even though it's still
    # live and usable. Filtering by browser-wide live keeps reuse stable.
    all_targets = cdp("Target.getTargets").get("targetInfos", [])
    browser_live_tids = {t.get("targetId") for t in all_targets if t.get("type") == "page"}
    state = _load_state()
    _gc_orphan_claims(state)  # release tabs whose owner died
    my_pid = os.getpid()
    mine = [r for r in state.get("agent_tabs", [])
            if r["tid"] in browser_live_tids and r.get("claimed_by_pid") == my_pid]
    if mine:
        mine.sort(key=lambda r: r["last_access"], reverse=True)
        tid = mine[0]["tid"]
        _record_access(tid, claim=True)
        prune_agent_tabs(max_n=max_tabs)
        return tid

    # 3. No tab claimed by us — must spawn a new one (don't poach others' tabs).
    # 3a. Prefer extension path (silent, zero focus steal). Auto-start the
    # local daemon if it's not running yet — extension polls it within ~30s.
    # If extension is installed, this path always wins; the window.open
    # fallback below should never fire in practice.
    if prefer_extension:
        try:
            from . import bh_extension_client as ext
            ext.start_server_if_needed()
            # Brief grace period for extension to poll & connect on cold start
            for _ in range(20):
                if ext.is_available():
                    break
                time.sleep(0.25)
            if ext.is_available():
                tid, nonce = ext.spawn_agent_tab_in_window(second_wid)
                if tid:
                    _record_access(tid, claim=True, nonce=nonce)
                    prune_agent_tabs(max_n=max_tabs)
                    return tid
        except Exception:
            pass  # fall through

    # 3b. CDP fallback: window.open from seed tab. NO focus restore.
    # Tag window.open URL with a per-call nonce so concurrent callers don't both
    # match the same "first new agent tab" (2026-05-20 test: two threads racing
    # on the same window.open both returned the same tid).
    #
    # NOTE (2026-05-24): we used to call _restore_main_focus(main_window_first_tab)
    # twice after window.open. window.open() doesn't change Chrome's active tab
    # in any window, so there was nothing to restore — but Target.activateTarget
    # DOES change the targeted tab to active AND raise its window. Net effect:
    # if user's main window was on tab #5, the "restore" moved them to tab #0
    # AND raised main window to foreground (interrupting whatever app they had
    # in the foreground). User incident: "切走了 B 站视频标签页". Removed.
    import uuid as _uuid
    nonce = f"{my_pid}-{int(time.time()*1000)}-{_uuid.uuid4().hex[:8]}"
    spawn_url = f"{AGENT_SPAWN_URL}&bh-nonce={nonce}"
    seed_tid = tabs[0][0]
    before_tids = {t[0] for t in tabs}
    sid = _attach(seed_tid)
    try:
        cdp("Runtime.evaluate", session_id=sid,
            expression=f"window.open({json.dumps(spawn_url)}, '_blank', 'noopener')",
            userGesture=True)
    finally:
        _detach(sid)
    time.sleep(1.0)

    # Match by nonce, not by AGENT_TAB_MARKER (which is shared across calls).
    # Look browser-wide because window.open may land in main window not second.
    tid = None
    for _ in range(8):
        all_targets = cdp("Target.getTargets").get("targetInfos", [])
        for t in all_targets:
            if t.get("type") == "page" and nonce in (t.get("url") or ""):
                tid = t.get("targetId")
                break
        if tid:
            break
        time.sleep(0.3)
    if not tid:
        raise RuntimeError("Failed to spawn agent tab — nonce never appeared (popup blocker?)")
    _record_access(tid, claim=True, nonce=nonce)
    prune_agent_tabs(max_n=max_tabs)
    return tid


# ---------- agent operations ----------

def navigate_agent(agent_tid, url, timeout=15):
    sid = _attach(agent_tid)
    try:
        cdp("Page.enable", session_id=sid)
        cdp("Page.navigate", session_id=sid, url=url)
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = cdp("Runtime.evaluate", session_id=sid, expression="document.readyState")
            if r.get("result", {}).get("value") == "complete":
                _record_access(agent_tid)
                return True
            time.sleep(0.3)
        _record_access(agent_tid)
        return False
    finally:
        _detach(sid)


def snapshot_agent(agent_tid, max_chars=10000):
    sid = _attach(agent_tid)
    try:
        r = cdp("Runtime.evaluate", session_id=sid, expression="document.body.innerText")
        _record_access(agent_tid)
        return (r.get("result", {}).get("value") or "")[:max_chars]
    finally:
        _detach(sid)


def screenshot_agent(agent_tid, path):
    sid = _attach(agent_tid)
    try:
        r = cdp("Page.captureScreenshot", session_id=sid, format="png")
        data = base64.b64decode(r.get("data", ""))
        pathlib.Path(path).write_bytes(data)
        _record_access(agent_tid)
        return len(data)
    finally:
        _detach(sid)


def save_as_pdf_agent(agent_tid, path):
    sid = _attach(agent_tid)
    try:
        r = cdp("Page.printToPDF", session_id=sid, printBackground=True, preferCSSPageSize=True)
        data = base64.b64decode(r.get("data", ""))
        pathlib.Path(path).write_bytes(data)
        _record_access(agent_tid)
        return len(data)
    finally:
        _detach(sid)


def evaluate_agent(agent_tid, expression):
    sid = _attach(agent_tid)
    try:
        r = cdp("Runtime.evaluate", session_id=sid, expression=expression)
        _record_access(agent_tid)
        # Match helpers.js() behavior: raise on JS exceptions / parse errors
        # rather than silently returning None. Without this, eval_js("syntax @#")
        # or eval_js("undefined.x") returned None and masked user-code bugs.
        details = r.get("exceptionDetails")
        result = r.get("result", {}) or {}
        if details or result.get("subtype") == "error":
            ex = (details or {}).get("exception", {}) or {}
            desc = ex.get("description") or result.get("description") or (details or {}).get("text") or "JavaScript error"
            raise RuntimeError(f"JavaScript evaluation failed: {desc}")
        return result.get("value")
    finally:
        _detach(sid)


def click_at_agent(agent_tid, x, y, button="left"):
    sid = _attach(agent_tid)
    try:
        cdp("Input.dispatchMouseEvent", session_id=sid,
            type="mousePressed", x=x, y=y, button=button, clickCount=1)
        cdp("Input.dispatchMouseEvent", session_id=sid,
            type="mouseReleased", x=x, y=y, button=button, clickCount=1)
        _record_access(agent_tid)
        return True
    finally:
        _detach(sid)


# Kimi-compat alias
mouse_click_agent = click_at_agent


_VK_MAP = {
    "Enter": 13, "Return": 13, "Tab": 9, "Escape": 27, "Esc": 27,
    "Backspace": 8, "Delete": 46, "Space": 32,
    "ArrowUp": 38, "ArrowDown": 40, "ArrowLeft": 37, "ArrowRight": 39,
    "Home": 36, "End": 35, "PageUp": 33, "PageDown": 34,
    "F1": 112, "F2": 113, "F3": 114, "F4": 115, "F5": 116, "F12": 123,
}


def key_type_agent(agent_tid, text):
    sid = _attach(agent_tid)
    try:
        for ch in text:
            cdp("Input.dispatchKeyEvent", session_id=sid, type="keyDown", text=ch)
            cdp("Input.dispatchKeyEvent", session_id=sid, type="keyUp", text=ch)
        _record_access(agent_tid)
    finally:
        _detach(sid)


def send_keys_agent(agent_tid, keys):
    sid = _attach(agent_tid)
    try:
        for k in keys:
            if len(k) == 1:
                cdp("Input.dispatchKeyEvent", session_id=sid, type="keyDown", text=k)
                cdp("Input.dispatchKeyEvent", session_id=sid, type="keyUp", text=k)
            else:
                vk = _VK_MAP.get(k, 0)
                cdp("Input.dispatchKeyEvent", session_id=sid, type="rawKeyDown",
                    code=k, key=k, windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk)
                cdp("Input.dispatchKeyEvent", session_id=sid, type="keyUp",
                    code=k, key=k, windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk)
        _record_access(agent_tid)
    finally:
        _detach(sid)


def upload_agent(agent_tid, selector, file_paths):
    sid = _attach(agent_tid)
    try:
        doc = cdp("DOM.getDocument", session_id=sid)
        root_id = doc.get("root", {}).get("nodeId")
        node = cdp("DOM.querySelector", session_id=sid, nodeId=root_id, selector=selector)
        node_id = node.get("nodeId")
        if not node_id:
            return "no-element"
        cdp("DOM.setFileInputFiles", session_id=sid, files=list(file_paths), nodeId=node_id)
        _record_access(agent_tid)
        return f"uploaded {len(file_paths)} file(s)"
    finally:
        _detach(sid)


def fill_agent(agent_tid, selector, value):
    sid = _attach(agent_tid)
    try:
        expr = f"""(()=>{{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return 'no-element';
            el.focus();
            el.value = {json.dumps(value)};
            el.dispatchEvent(new Event('input', {{bubbles:true}}));
            el.dispatchEvent(new Event('change', {{bubbles:true}}));
            return 'filled';
        }})()"""
        r = cdp("Runtime.evaluate", session_id=sid, expression=expr)
        _record_access(agent_tid)
        return r.get("result", {}).get("value")
    finally:
        _detach(sid)


def list_agent_tabs():
    state = _load_state()
    targets = cdp("Target.getTargets").get("targetInfos", [])
    live = {t.get("targetId"): t for t in targets if t.get("type") == "page"}
    out = []
    for r in state.get("agent_tabs", []):
        info = live.get(r["tid"])
        if info:
            out.append({
                "tid": r["tid"],
                "url": info.get("url", ""),
                "title": info.get("title", ""),
                "last_access": r["last_access"],
            })
    return out


def find_agent_tab(url_substring):
    for t in list_agent_tabs():
        if url_substring in t["url"]:
            return t["tid"]
    return None


def close_agent_tab(agent_tid):
    try:
        cdp("Target.closeTarget", targetId=agent_tid)
        state = _load_state()
        state["agent_tabs"] = [r for r in state.get("agent_tabs", []) if r["tid"] != agent_tid]
        _save_state(state)
        return True
    except Exception:
        return False
