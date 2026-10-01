# Agent window and agent tabs

In this fork, browser automation never disturbs the user's own Chrome windows.
All automation runs in agent tabs inside one agent window that BH owns. The
window is in the user's daily Chrome (same profile, port 9222), so the user's
logins work.

## Rules

1. The agent window opens minimized and unfocused, and it stays minimized.
2. BH never activates a tab, raises a window, or brings Chrome to the front.
   The one exception is `show_window()`, for a login that the user must finish.
3. BH never closes, navigates, or reads the user's tabs.
4. Each process works in its own agent tab.

## How BH finds its window

BH opens the window with an anchor tab at
`https://example.com/?bh-agent-window=<nonce>` and stores the nonce in the
state file. The window that holds the anchor is the agent window. BH never
guesses a window from tab counts or URLs. When the user closes the agent
window, BH opens a new one on the next call.

BH opens the window with `Target.createTarget(newWindow=True, background=True,
windowState="minimized")`, then `Browser.setWindowBounds` with
`windowState: "minimized"`. Chrome shows it inactive and minimizes it in the
same step. BH does not use `chrome.windows.create({focused: false})`. With it,
Chrome shows the window inactive but not minimized, often on top of the user's
app.

Agent tabs open with the extension (`chrome.tabs.create({windowId, active: false})`).
Each new tab has a nonce in its start URL (`example.com/?bh-agent-tab=1&bh-nonce=...`),
so BH knows which tab it opened. When a new tab lands outside the agent window,
BH closes it.

Without the extension, CDP cannot add a tab to the minimized agent window
without focus. `window.open` from the anchor tab makes Chrome restore the window
and bring it to the front. So each new tab opens in a minimized window of its
own (`Target.createTarget(newWindow=True, background=True, windowState="minimized")`).
The record of the tab keeps that window id. BH treats the tab as an agent tab
while it stays in that window, and closes the tab and its window when the
process exits. Before it falls back, BH waits up to 35 seconds for an extension
that polled in the last 2 minutes.

Pages in agent tabs open links and `window.open()` in the same tab
(`SAME_TAB_JS`). When a page opens a new tab, Chrome shows and activates its
window, which would bring the minimized agent window over the user's app.

## Leases

The state file `~/.browser-harness/second-window-state.json` has one record
per agent tab: `tid`, `claimed_by` (`session:<CLAUDE_CODE_SESSION_ID>` or
`pid:<pid>`), `lease_pid`, `last_access` and `nonce`.

`ensure_agent_tab()` picks the first match:

1. a tab that this process already leases,
2. a free tab that this Claude session claimed before,
3. a free tab that nobody claimed,
4. a new tab.

A tab is busy while its `lease_pid` is alive and the tab was used in the last
30 minutes. BH never gives a busy tab to another process. Each
`browser-harness` call is a new Python process, so two subagents that run at
the same time get different tabs. A file lock serializes all state changes.

At exit, a process releases its leases, closes its tabs that still show the
start page, and detaches its CDP sessions. Above 15 agent tabs,
`prune_agent_tabs()` closes the free tabs that were used least recently. It
also closes lost start-page tabs that are older than 60 seconds.

## Request policy

`helpers._send` passes every request to `second_window.request_policy`.

| Request | Policy |
|---|---|
| Page commands without a session (`Page.*`, `Runtime.*`, `Input.*`, ...) | Run on the agent tab of this process. |
| Requests with an explicit session | Forward. A script gets sessions only from the attach rule below. |
| `Target.createTarget` | Open an agent tab instead, and return its targetId. |
| `Target.closeTarget`, `Target.attachToTarget` | Agent tabs only. |
| `Target.getTargets` | Show agent tabs only, without the anchor. |
| `Browser.setWindowBounds` | The agent window only. |
| `Target.activateTarget`, `Browser.close`, `Browser.crash*` | Refused. |
| `Page.bringToFront` | Ignored. |
| Daemon meta `current_tab`, `session`, `set_session` | Answered for the agent tab of this process. |

At start, the daemon attaches its default session to the first page, which is
a user tab. Each process moves that session to the anchor tab once, so no
request without a session can reach a user tab.

When the agent tab closes during a call, BH binds a new tab. Only
`Page.navigate` repeats on the new tab. Other calls raise, because they would
act on a different page.

## API

The functions take the agent tab id first.

| Function | Use |
|---|---|
| `ensure_agent_tab(prefer_url=None)` | Lease a tab (see Leases). `prefer_url` picks a free tab whose URL contains it. |
| `new_agent_tab(url=None)` | Open a new agent tab. |
| `bound_tab()`, `bind(tid)` | The tab that gets requests without a session in this process. |
| `navigate_agent(tid, url, timeout=15)` | Navigate and wait for the load. False on a load error or timeout. |
| `snapshot_tree_agent(tid, interactive_only=True, roles=None)` | `@e` ref tree. Refs persist in `~/.browser-harness/refs/<tid>.json`. |
| `ref_for_agent`, `click_ref_agent`, `fill_ref_agent` | Find a ref and act on it. |
| `snapshot_agent(tid)` | Visible text of the page. |
| `evaluate_agent(tid, expr)` | Run JavaScript. The result of a promise is awaited. |
| `screenshot_agent(tid, path, full=False)`, `save_as_pdf_agent(tid, path)` | Capture the page. |
| `click_at_agent`, `key_type_agent`, `send_keys_agent`, `hotkey_agent` | Input. |
| `fill_agent(tid, selector, value)`, `upload_agent(tid, selector, paths)` | Forms and file inputs. |
| `list_agent_tabs()`, `find_agent_tab(url_part)` | Agent tabs, with `mine` and `busy` flags. |
| `close_agent_tab(tid)`, `close_agent_tabs_matching(url_part, keep=None)` | Close agent tabs. |
| `show_window(tid=None)`, `hide_window()` | Show the agent window for a login, then minimize it. |

```python
from browser_harness.second_window import (
    ensure_agent_tab, navigate_agent, snapshot_tree_agent, click_ref_agent,
    fill_ref_agent, show_window, hide_window,
)

tid = ensure_agent_tab()
navigate_agent(tid, "https://example.com/login")
print(snapshot_tree_agent(tid))      # @e1 textbox "Email" ...
fill_ref_agent(tid, "@e1", "me@example.com")
show_window(tid)                     # the user types the password
# ... wait for the user to say they are done ...
hide_window()
```

### send_keys vs hotkey

`send_keys_agent(tid, ["Tab", "Enter"])` presses each key on its own, with no
held modifier. A string is typed per character, except when the whole string
names one key (`"Enter"`). `send_keys_agent(tid, "Control+End")` types the
characters `Control+End`.

For a shortcut, use `hotkey_agent(tid, chord)`. It holds the modifiers down
while it presses the last key:

```python
hotkey_agent(tid, "Control+End")       # caret to the end of the document
hotkey_agent(tid, "Control+A")         # select all
hotkey_agent(tid, "Shift+ArrowRight")  # extend the selection
hotkey_agent(tid, "Control+Shift+End") # more than one modifier
```

Modifiers are `Ctrl`/`Control`, `Shift`, `Alt` and `Meta`/`Cmd`/`Win`. The last
key is one character (`a`, `1`) or a named key (`End`, `Home`, `ArrowRight`,
`Enter`, ...). Use hotkeys for editors that draw on a canvas (Google Docs,
Sheets), where DOM edits do not work.

## Companion extension

The extension in `extension/` adds background tabs to the agent window. See
`extension/README.md` for the install steps and the bridge security. Without
the extension, BH uses the CDP way above.

## Environment

| Variable | Effect |
|---|---|
| `BH_KEEP_PLACEHOLDERS=1` | Keep start-page tabs at exit. |
| `BH_TAB_MARKER` | Defaults to 0 in this fork, so BH does not put the horse emoji in tab titles. |
| `BH_EXTENSION_DIR` | Folder of the companion extension. The bridge token file goes there. |
| `BH_SAFE_MODE` | Never set it to 0. The user's hook blocks that. |
