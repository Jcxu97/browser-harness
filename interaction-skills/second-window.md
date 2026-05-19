# Second-window agent tabs

When the user's policy requires that automation **never disturbs** their main
Chrome window, route all agent operations through their second window.

## Hard rule

- agent tabs MUST live in the user's second Chrome window
- main window (the one user is actively working in) is never touched
- focus is never stolen during navigate / click / fill / screenshot / etc.

## Quick start

```python
from browser_harness.second_window import (
    ensure_agent_tab, navigate_agent, snapshot_agent, screenshot_agent,
    save_as_pdf_agent, evaluate_agent, fill_agent, click_at_agent,
    key_type_agent, send_keys_agent, upload_agent,
    list_agent_tabs, find_agent_tab, close_agent_tab,
)

# Single entry point — auto-detect, auto-spawn, persistent reuse
tid = ensure_agent_tab()

navigate_agent(tid, "https://example.com")
text = snapshot_agent(tid)
screenshot_agent(tid, "/tmp/shot.png")
save_as_pdf_agent(tid, "/tmp/page.pdf")
fill_agent(tid, 'input[name="q"]', "search term")
send_keys_agent(tid, ["Enter"])
```

## How it works

`ensure_agent_tab()` does (in order):

1. **Try fast-path via BH companion Chrome extension** (zero focus steal).
   If `bh_extension_client.is_available()` → uses
   `chrome.tabs.create({active:false, windowId:X})` for true silent spawn.
2. **Detect existing second window** by heuristic: the window with the
   FEWEST real tabs (main window has user's daily browsing).
3. **Spawn second window if missing** via `chrome.exe --new-window`.
   Steals focus once (Windows OS behavior, unavoidable).
4. **Reuse existing agent tab** if state file records one that's still alive.
5. **Spawn new agent tab** via `window.open` from a seed tab in the second
   window. Uses `userGesture=True` to bypass popup blocker. Brief focus
   flicker (~100-200ms) without extension; zero with extension.

State persists at `~/.browser-harness/second-window-state.json` — survives
process restart.

## API surface (Kimi WebBridge tool parity)

| Kimi tool | second_window helper |
|-----------|----------------------|
| navigate | `navigate_agent` |
| snapshot | `snapshot_agent` (innerText) or `evaluate_agent` for AX tree |
| evaluate | `evaluate_agent` |
| click | `click_at_agent` (alias `mouse_click_agent`) |
| mouse_click | `click_at_agent` |
| fill | `fill_agent` |
| key_type | `key_type_agent` |
| send_keys | `send_keys_agent` |
| screenshot | `screenshot_agent` |
| save_as_pdf | `save_as_pdf_agent` |
| upload | `upload_agent` |
| list_tabs | `list_agent_tabs` (agent-scoped) |
| find_tab | `find_agent_tab` |
| close_tab | `close_agent_tab` |
| close_session | LRU prune via `prune_agent_tabs` |
| network | use raw `cdp("Network.enable", ...)` + `drain_events()` |

## Focus-steal mitigation (CDP fallback path)

Without the companion extension, Chrome activates the target window when any
new tab is created. We mitigate by:

1. Capturing the user's main-window tab id BEFORE spawn
2. Issuing the `window.open` from second-window seed tab
3. Calling `Target.activateTarget` on the captured main tab to restore focus
4. Repeating the activate after the new tab finishes loading

Net effect: a brief flicker rather than persistent theft. For zero flicker,
install the companion extension at `extension/` (see `extension/README.md`).

## Constraints

- Requires CDP attach (port 9222 chrome instance)
- `window.open` from agent operations is the only path that doesn't abuse
  `Target.createTarget` — which respects neither windowId nor focus
- `seed_tab` must allow `Runtime.evaluate(userGesture=True)`. Banking sites
  with strict CSP may reject; fallback: companion extension path
- chrome.exe path autodetected on Windows; override via `BH_CHROME_EXE`
