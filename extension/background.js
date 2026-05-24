// BH companion extension service worker.
// Long-polls localhost server, executes chrome.tabs / chrome.windows actions
// on behalf of the BH Python client.

const SERVER = "http://127.0.0.1:9223";
const POLL_INTERVAL_ON_ERROR_MS = 2000;

let pollRunning = false;

async function pollOnce() {
  const resp = await fetch(`${SERVER}/poll`, { method: "POST" });
  if (!resp.ok) throw new Error(`poll HTTP ${resp.status}`);
  const data = await resp.json();
  const cmds = data.commands || [];
  for (const cmd of cmds) {
    let result;
    try {
      result = await execute(cmd);
    } catch (e) {
      result = { error: String(e && e.message || e) };
    }
    await fetch(`${SERVER}/result`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: cmd.id, result })
    });
  }
}

async function pollLoop() {
  if (pollRunning) return;
  pollRunning = true;
  try {
    while (true) {
      try {
        await pollOnce();
      } catch (e) {
        // Server down / network error — back off briefly
        await new Promise(r => setTimeout(r, POLL_INTERVAL_ON_ERROR_MS));
      }
    }
  } finally {
    pollRunning = false;
  }
}

async function execute(cmd) {
  const a = cmd.action;
  if (a === "create_tab") {
    const tab = await chrome.tabs.create({
      url: cmd.url || "about:blank",
      windowId: cmd.windowId,
      active: cmd.active === undefined ? false : cmd.active
    });
    return { tabId: tab.id, windowId: tab.windowId, url: tab.url };
  }
  if (a === "create_window") {
    // Zero-focus-steal window birth: focused:false + state:minimized means
    // the window appears directly in the taskbar, never on screen.
    const w = await chrome.windows.create({
      url: cmd.url || "about:blank",
      focused: cmd.focused === undefined ? false : cmd.focused,
      state: cmd.state || "minimized"
    });
    return { ok: true, windowId: w.id, state: w.state };
  }
  if (a === "list_windows") {
    const wins = await chrome.windows.getAll({ populate: true });
    return wins.map(w => ({
      id: w.id,
      focused: w.focused,
      state: w.state,
      tabs: (w.tabs || []).map(t => ({
        id: t.id,
        url: t.url || "",
        title: t.title || "",
        active: t.active
      }))
    }));
  }
  if (a === "focus_window") {
    // Raise window to OS foreground WITHOUT changing its active tab.
    // chrome.windows.update({focused:true}) is the cleanest "raise without
    // tab side-effects" API — Target.activateTarget over CDP also raises but
    // requires picking a target first (and can switch tabs if you pick wrong).
    await chrome.windows.update(cmd.windowId, { focused: true });
    return { ok: true };
  }
  if (a === "close_tab") {
    await chrome.tabs.remove(cmd.tabId);
    return { ok: true };
  }
  if (a === "navigate_tab") {
    await chrome.tabs.update(cmd.tabId, { url: cmd.url, active: false });
    return { ok: true };
  }
  if (a === "get_tab") {
    const tab = await chrome.tabs.get(cmd.tabId);
    return { id: tab.id, url: tab.url, title: tab.title, windowId: tab.windowId };
  }
  if (a === "ping") {
    return { pong: true, version: chrome.runtime.getManifest().version };
  }
  return { error: `unknown action: ${a}` };
}

// Service worker may be terminated by chrome after idle. Use alarms to wake
// up periodically and resume the poll loop.
chrome.alarms.create("bh-keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "bh-keepalive") pollLoop();
});

// Kick off on install / startup / SW load
chrome.runtime.onInstalled.addListener(() => pollLoop());
chrome.runtime.onStartup.addListener(() => pollLoop());

// Top-level: also start when SW first loads
pollLoop();
