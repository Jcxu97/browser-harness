# BH Companion Extension

This optional Chrome extension lets `browser_harness.second_window` add tabs
to its minimized agent window without taking focus.

1. `create_tab` opens a tab in the agent window with `active: false`. Chrome
   does not show or restore the window for a background tab.
2. There is no `create_window`. With `focused: false`, `chrome.windows.create`
   shows the window inactive but not minimized, often on top of the user's app.
   BH opens the agent window with CDP (`Target.createTarget` with a background
   new window, minimized).

Without the extension, CDP cannot add a tab to the minimized window without
focus. Each new agent tab then opens in a minimized window of its own.

## Install (one time)

1. Open `chrome://extensions/`.
2. Turn on **Developer mode**.
3. Click **Load unpacked** and select this `extension/` folder.
4. Make sure the card shows "Browser-Harness Companion" 0.5.1.

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
