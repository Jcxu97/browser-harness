// BH companion extension service worker.
// Long-polls the local bridge and runs chrome.windows / chrome.tabs actions.
// Every message is signed with HMAC-SHA256. The shared token comes from
// bridge-token.txt in this folder, which BH writes. It never goes over the wire,
// so a fake server on the port cannot learn it or send us commands.

const SERVER = "http://127.0.0.1:9223";
const RETRY_MS = 2000;

let pollRunning = false;
let keyPromise = null;

const enc = (s) => new TextEncoder().encode(s);
const toHex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");

function fromHex(s) {
  const out = new Uint8Array(s.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(s.substr(i * 2, 2), 16);
  return out;
}

async function loadKey() {
  const resp = await fetch(chrome.runtime.getURL("bridge-token.txt"), { cache: "no-store" });
  if (!resp.ok) throw new Error("bridge token not written yet");
  const token = (await resp.text()).trim();
  if (!token) throw new Error("bridge token is empty");
  return crypto.subtle.importKey("raw", enc(token), { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"]);
}

function key() {
  if (!keyPromise) keyPromise = loadKey().catch((e) => { keyPromise = null; throw e; });
  return keyPromise;
}

async function sign(kind, payload) {
  return toHex(await crypto.subtle.sign("HMAC", await key(), enc(`${kind}\n${payload}`)));
}

async function verify(kind, payload, sig) {
  if (typeof sig !== "string" || !/^[0-9a-f]{64}$/.test(sig)) return false;
  return crypto.subtle.verify("HMAC", await key(), fromHex(sig), enc(`${kind}\n${payload}`));
}

async function pollOnce() {
  const ts = String(Date.now());
  const nonce = toHex(crypto.getRandomValues(new Uint8Array(12)));
  const resp = await fetch(`${SERVER}/poll`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ts, nonce, mac: await sign("poll", `${ts}\n${nonce}`) }),
  });
  if (resp.status === 401) {
    keyPromise = null; // BH may have written a new token
    throw new Error("bridge refused our auth");
  }
  if (!resp.ok) throw new Error(`poll HTTP ${resp.status}`);
  const data = await resp.json();
  for (const item of data.commands || []) {
    if (typeof item.payload !== "string" || !(await verify("cmd", item.payload, item.sig))) continue;
    const cmd = JSON.parse(item.payload);
    // A command whose caller gave up must never run late.
    if (!cmd.deadline || cmd.deadline < Date.now()) continue;
    let result;
    try {
      result = await execute(cmd);
    } catch (e) {
      result = { error: String((e && e.message) || e) };
    }
    const payload = JSON.stringify({ id: cmd.id, result });
    await fetch(`${SERVER}/result`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ payload, mac: await sign("result", payload) }),
    });
  }
}

async function pollLoop() {
  if (pollRunning) return;
  pollRunning = true;
  try {
    while (true) {
      try {
        // An extension API call resets Chrome's 30 s idle timer, so the
        // worker stays alive through the 25 s long poll.
        await chrome.runtime.getPlatformInfo();
        await pollOnce();
      } catch (e) {
        await new Promise((r) => setTimeout(r, RETRY_MS));
      }
    }
  } finally {
    pollRunning = false;
  }
}

async function execute(cmd) {
  switch (cmd.action) {
    case "create_tab": {
      const tab = await chrome.tabs.create({ url: cmd.url || "about:blank", windowId: cmd.windowId, active: false });
      return { tabId: tab.id, windowId: tab.windowId };
    }
    // No create_window: with focused:false, chrome.windows.create shows the
    // window inactive but not minimized, often on top of the user's app.
    // BH opens its window with CDP instead.
    case "update_window": {
      // Used only to show the agent window for a login, and to hide it again.
      const info = {};
      if (cmd.state) info.state = cmd.state;
      if (cmd.focused !== undefined) info.focused = cmd.focused;
      const w = await chrome.windows.update(cmd.windowId, info);
      return { ok: true, state: w.state };
    }
    case "ping":
      return { pong: true, version: chrome.runtime.getManifest().version };
    case "reload":
      setTimeout(() => chrome.runtime.reload(), 100);
      return { ok: true };
    default:
      return { error: `unknown action: ${cmd.action}` };
  }
}

// Chrome stops an idle service worker. The alarm wakes it and restarts the loop.
chrome.alarms.create("bh-keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "bh-keepalive") pollLoop();
});
chrome.runtime.onInstalled.addListener(() => pollLoop());
chrome.runtime.onStartup.addListener(() => pollLoop());
pollLoop();
