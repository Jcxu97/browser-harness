# BH Companion Extension

This optional Chrome extension lets `browser_harness.second_window` open its
agent window and agent tabs without taking focus.

1. `create_window` opens the agent window minimized and unfocused. The window
   goes straight to the taskbar.
2. `create_tab` opens a tab in that window with `active: false`.

Without the extension, BH uses CDP (`Target.createTarget` with a background
new window, then minimizes it). That works too, but the extension path is the
quiet one.

## Install (one time)

1. Open `chrome://extensions/`.
2. Turn on **Developer mode**.
3. Click **Load unpacked** and select this `extension/` folder.
4. Make sure the card shows "Browser-Harness Companion" 0.5.0.

## After an update

Chrome does not see new files until the extension reloads. Do one of these:

1. Click the reload arrow on the card in `chrome://extensions/`.
2. Run `bh_extension_client.send_command("reload")`. This works only when the
   running extension already speaks protocol 2.

## How it works

```
BH Python                                Chrome
  second_window
    bh_extension_client                  extension/background.js
      POST /command  ──>  bh_extension_server  <──  POST /poll  (long poll)
                          127.0.0.1:9223       ──>  runs chrome.windows / chrome.tabs
      answer         <──                       <──  POST /result
```

`bh_extension_server` starts on demand as a hidden background process.

## Security

1. The server binds to 127.0.0.1 only and sends no CORS headers. It answers
   `OPTIONS` with 405, so web pages cannot read its replies.
2. BH writes a random token to `bridge-token.txt` in this folder. The
   extension reads it from its own package. The token never goes over the
   wire. Git ignores the file. Do not share it.
3. Each request carries an HMAC-SHA256 of its content, a timestamp, and a
   nonce. The server refuses replays and old timestamps.
4. Each command to the extension carries an HMAC too. The extension ignores
   commands from a server that does not know the token.
5. Each command has a deadline. When the caller stops waiting, neither the
   server nor the extension runs the command later.
6. The extension asks only for `alarms` and access to the bridge port. It
   cannot read pages, and it has no action that lists or closes your tabs.

## Check it

```python
from browser_harness import bh_extension_client as ext

ext.start_server_if_needed()
print("server up:", ext.server_is_up())
print("extension connected:", ext.is_available())
print(ext.send_command("ping"))
```

`is_available()` stays False until the extension polls. That happens within
about 35 seconds after Chrome loads it. If it does not connect, open
`chrome://extensions/` and look at **Errors** and **Service worker** on the
card.

## Stop the server

```python
from browser_harness import bh_extension_client as ext
ext.stop_server()
```
