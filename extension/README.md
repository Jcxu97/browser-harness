# BH Companion Extension

Optional Chrome extension that gives `browser_harness.second_window` a **zero
focus-steal** path for spawning agent tabs.

Without this extension: spawn uses CDP `window.open` from a seed tab, which
causes a brief 100-200ms focus flicker.
With this extension: spawn uses `chrome.tabs.create({active:false, windowId:X})`,
which is genuinely silent.

## Install (one-time)

1. Open Chrome → navigate to `chrome://extensions/`
2. Toggle **Developer mode** (top-right)
3. Click **Load unpacked**
4. Select this `extension/` directory
5. Confirm the extension shows up: name "Browser-Harness Companion", v0.1.0

## How it works

```
BH Python                                Chrome
  ├─ second_window.ensure_agent_tab()
  │     └─ tries bh_extension_client.is_available()
  │
  ├─ bh_extension_client                ┌─ extension/background.js
  │  ↓ HTTP POST /command               │     ↓ long-polls /poll
  │  bh_extension_server (daemon)       │     ↓ executes chrome.tabs.create
  │  ↑ POST /result                     │     ↑ returns result
  │  (auto-spawned at 127.0.0.1:9223)   │
  │                                     └─ silently creates tab in second window
  └─ caller receives CDP targetId
```

## Verify it works

```python
from browser_harness import bh_extension_client as ext

ext.start_server_if_needed()
print("server up:", ext.server_is_up())
print("extension connected:", ext.is_available())

# If True: ensure_agent_tab() will use the silent path automatically.
```

`is_available()` returns False until the extension polls (within ~35s of
load). If the extension isn't connecting, check:

- Server running? `curl http://127.0.0.1:9223/status`
- Extension errors? `chrome://extensions/` → "Errors" button under the
  extension card
- Service worker active? `chrome://extensions/` → "Service worker" link to
  inspect

## Stop the server

```python
from browser_harness import bh_extension_client as ext
ext.stop_server()
```

Or kill the python process whose cmdline contains `bh_extension_server`.

## Notes

- Server binds 127.0.0.1 only (localhost-only, never network-exposed)
- Extension only requests `tabs` + `alarms` permissions — no host page access
- All commands proxied via the local server; extension doesn't talk to the
  internet
