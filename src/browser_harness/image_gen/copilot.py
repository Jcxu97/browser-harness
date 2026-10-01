"""M365 Copilot image generation driver via second-window agent tab.

Hard rules (see memory feedback_m365_copilot_image_gen.md):
1. Model must be switched to the GPT model that thinks deeper (default Auto
   only returns text).
2. Prompt must be prefixed with "生成图片：" (intent trigger).
3. One prompt per call, no concurrent invocations.
4. Each task gets a FRESH conversation (created from /chat). After success,
   the previous task's conversation is deleted from the sidebar (rolling
   1-window cleanup). User direction 2026-05-20: polluted conversations
   stall the Copilot backend — fresh conversation per task is the workaround.

Image lives inside a Microsoft Designer iframe at
`designer.svc.cloud.microsoft/chat-image-creator?...`. We pierce the
cross-origin iframe via `Page.createIsolatedWorld` to read the base64 src.

Pipeline:
1. Find/reuse the single m365 tab in user's true secondary Chrome window.
2. Navigate to /chat (always fresh conversation).
3. Snapshot baseline designer iframes (rare leftover from sidebar preview).
4. Open model selector → click GPT submenu → click the GPT item that thinks deeper.
5. Inject "生成图片：<prompt>" via CDP Input.insertText, click Send.
6. Capture the new conversation_id from URL after SPA pushes /conversation/<id>.
7. Poll frame tree; for each Designer iframe, createIsolatedWorld, look for
   img[src^="data:image/"] with width≥512. Image rendering can take >2 min
   AFTER chat says "已生成" — patient polling, reload only if grace expires.
8. Decode base64 → write PNG.
9. Cleanup: delete PREVIOUS task's conversation from sidebar (PointerEvent
   chain, see memory reference_doubao_chat_delete_technique).
10. Persist the new conversation_id as next call's "previous to delete".
"""
from __future__ import annotations
import base64
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

from ..helpers import cdp
from ..second_window import (
    _attach, _detach, close_agent_tabs_matching, ensure_agent_tab, navigate_agent,
)

# Strategy 2026-05-20 (per user direction):
# Each generate() call creates a FRESH conversation, then deletes the
# previous task's conversation after success. One conversation = one image.
# Polluted conversations (many prompts) cause Copilot backend to stall —
# fresh conversation per task is the workaround.
NEW_CHAT_URL = os.environ.get(
    "BH_M365_COPILOT_URL",
    "https://m365.cloud.microsoft/chat",
)
# Persisted previous conversation_id (the one to delete after THIS success).
STATE_FILE = Path.home() / ".cache" / "browser-harness" / "copilot-prev-conv.txt"
WAIT_GEN_SECS = 600  # Designer iframe render is non-deterministic — chat
                     # text may say "已生成" but the actual base64 PNG can
                     # take another 1-3 minutes to land in the iframe DOM.
                     # User 2026-05-20 observation: "出现没有规律可能在已生成后还需要等好久".
GRACE_AFTER_DONE = 120  # how long to keep polling without reload after the
                        # backend reports completion. Only reload if STILL
                        # stuck after this — premature reload interrupts
                        # in-progress iframe hydration.
POLL_INTERVAL = 4.0
PROMPT_PREFIX = "生成图片："
# The UI is Chinese or English, and the model name changes with each release
# (GPT 5.5 深度思考 in 2026-05, GPT 5.6 Sol Think deeper in 2026-10).
DEEP_GPT = re.compile(r"GPT.*(深度思考|think deeper)", re.I)


# ---------- tab routing ----------

def _force_agent_tab_in_secondary():
    """Lease the agent tab for Copilot. Several m365 tabs break generation, so
    reuse an existing one and close the extra ones."""
    tid = ensure_agent_tab(prefer_url="m365.cloud.microsoft")
    close_agent_tabs_matching("m365.cloud.microsoft", keep=tid)
    return tid


# ---------- top-frame helpers ----------

def _eval(tid, expr):
    sid = _attach(tid)
    try:
        r = cdp("Runtime.evaluate", session_id=sid, expression=expr, returnByValue=True)
        return r.get("result", {}).get("value")
    finally:
        _detach(sid)


def _wait_selector(tid, check_expr, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _eval(tid, check_expr):
            return True
        time.sleep(0.5)
    return False


def _current_model_label(tid):
    return _eval(tid,
        """(([...document.querySelectorAll('button')]"""
        """.find(b=>/模型选择|model selector/i.test(b.getAttribute('aria-label')||'')))||{}).innerText"""
    )


def _switch_to_gpt_deep(tid):
    """Switch to the GPT model that thinks deeper. Verifies the selector label changed."""
    # 1. Wait for the selector button to be visible (SPA hydrate).
    if not _wait_selector(
        tid,
        """(([...document.querySelectorAll('button')]"""
        """.find(b => /模型选择|model selector/i.test(b.getAttribute('aria-label')||'')))||{})"""
        """.offsetParent !== undefined""",
        timeout=30,
    ):
        raise RuntimeError("model selector never rendered")
    # Extra hydrate buffer — selector may be in DOM but not interactive yet.
    time.sleep(1.5)

    # If already on it (idempotent reuse), skip.
    label = _current_model_label(tid) or ""
    if DEEP_GPT.search(label):
        return

    # 2. Click model selector to open menu.
    _eval(tid,
        """(()=>{const s=[...document.querySelectorAll('button')]"""
        """.find(b=>/模型选择|model selector/i.test(b.getAttribute('aria-label')||''));"""
        """if(s)s.click();})()"""
    )
    # Wait for the menu to render (look for the GPT submenu trigger).
    if not _wait_selector(
        tid,
        """!![...document.querySelectorAll('[role="menuitem"]')]"""
        """.filter(it=>it.offsetParent!==null)"""
        """.find(it=>/^GPT$/m.test((it.innerText||'').trim()))""",
        timeout=8,
    ):
        raise RuntimeError("model menu never rendered")

    # 3. Click GPT submenu trigger.
    ok = _eval(tid,
        """JSON.stringify((()=>{const gpt=[...document.querySelectorAll('[role="menuitem"]')]"""
        """.filter(it=>it.offsetParent!==null)"""
        """.find(it=>/^GPT$/m.test((it.innerText||'').trim()));"""
        """if(!gpt)return false;gpt.click();return true;})())"""
    )
    if ok != "true":
        raise RuntimeError("GPT submenu trigger not found")

    # Wait for GPT submenu items to render.
    if not _wait_selector(
        tid,
        r"""!![...document.querySelectorAll('[role="menuitemradio"]')]"""
        r""".filter(x=>x.offsetParent!==null)"""
        r""".find(x=>/GPT.*(深度思考|think deeper)/i.test((x.innerText||'').trim()))""",
        timeout=8,
    ):
        raise RuntimeError("the GPT menu item that thinks deeper never appeared")

    # 4. Click the GPT item that thinks deeper.
    ok = _eval(tid,
        r"""JSON.stringify((()=>{const it=[...document.querySelectorAll('[role="menuitemradio"]')]"""
        r""".filter(x=>x.offsetParent!==null)"""
        r""".find(x=>/GPT.*(深度思考|think deeper)/i.test((x.innerText||'').trim()));"""
        r"""if(!it)return false;it.click();return true;})())"""
    )
    if ok != "true":
        raise RuntimeError("click on the GPT item that thinks deeper failed")

    # 5. Verify label changed AND stabilized (SPA can flicker label briefly to
    # the picked option, then revert if click didn't register on the right item).
    # Require 3 consecutive observations of the model separated by 0.5s.
    deadline = time.time() + 8
    streak = 0
    label = ""
    while time.time() < deadline:
        label = _current_model_label(tid) or ""
        if DEEP_GPT.search(label):
            streak += 1
            if streak >= 3:
                return
        else:
            streak = 0
        time.sleep(0.5)
    raise RuntimeError(
        f"model label did not stabilize on the deep GPT model (last seen {label!r}, "
        f"streak={streak}) — menu click likely selected a different item"
    )


def _inject_and_send(tid, prompt):
    # Re-verify model right before send — selector can silently revert to "自动"
    # between _switch_to_gpt_deep and now (SPA hydrate races, focus shifts).
    # 自动 model returns text-only, never spawns Designer iframe → 240s timeout.
    label = _current_model_label(tid) or ""
    if not DEEP_GPT.search(label):
        # One more switch attempt before failing.
        _switch_to_gpt_deep(tid)
        label = _current_model_label(tid) or ""
        if not DEEP_GPT.search(label):
            raise RuntimeError(
                f"model reverted to {label!r} before send and re-switch failed"
            )

    _eval(tid,
        """(()=>{const ed=document.querySelector('[contenteditable=true][role=textbox]')"""
        """||document.querySelector('[contenteditable=true]');if(ed)ed.focus();})()"""
    )
    time.sleep(0.3)
    sid = _attach(tid)
    try:
        cdp("Input.insertText", session_id=sid, text=prompt)
    finally:
        _detach(sid)
    time.sleep(0.5)
    sent = _eval(tid,
        """JSON.stringify((()=>{const s=[...document.querySelectorAll('button')]"""
        """.find(b=>['发送','Send'].includes(b.getAttribute('aria-label'))&&!b.disabled);"""
        """if(!s)return false;s.click();return true;})())"""
    )
    if sent != "true":
        raise RuntimeError("send button not found or disabled")


# ---------- frame piercing ----------

def _list_designer_frame_ids(tid):
    return [fid for fid, _ in _list_designer_frames(tid)]


def _list_designer_frames(tid):
    """Returns [(frame_id, src_url)] for every Designer iframe in the page.

    Each iframe URL contains a unique `correlationId` query param, so we
    use the full src as identity (more reliable than frame-id, which can
    be reused for placeholder slots).
    """
    sid = _attach(tid)
    try:
        ft = cdp("Page.getFrameTree", session_id=sid)
    finally:
        _detach(sid)
    out = []
    def walk(node):
        f = node.get("frame", {})
        url = f.get("url", "") or ""
        if "designer.svc.cloud.microsoft" in url:
            out.append((f.get("id"), url))
        for c in node.get("childFrames", []):
            walk(c)
    walk(ft["frameTree"])
    return out


def _probe_frame_for_image(tid, frame_id):
    """Returns base64 PNG bytes if frame contains a generated image, else None."""
    sid = _attach(tid)
    try:
        cdp("Page.enable", session_id=sid)
        cdp("Runtime.enable", session_id=sid)
        iso = cdp("Page.createIsolatedWorld", session_id=sid,
                  frameId=frame_id, worldName="bh_copilot_probe")
        ctx = iso.get("executionContextId")
        r = cdp("Runtime.evaluate", session_id=sid, contextId=ctx,
                expression=r"""
JSON.stringify((() => {
  const imgs = [...document.querySelectorAll('img')]
    .filter(i => /^data:image\//.test(i.src||''))
    .map(i => ({src: i.src, w: i.naturalWidth}));
  imgs.sort((a,b) => b.w - a.w);
  return imgs[0] || null;
})())
""", returnByValue=True)
        v = r.get("result", {}).get("value")
        if not v:
            return None
        d = json.loads(v)
        if not d or d.get("w", 0) < 512:
            return None
        src = d["src"]
        _, b64 = src.split(",", 1)
        return base64.b64decode(b64)
    except Exception:
        return None
    finally:
        _detach(sid)


def _scroll_chat_to_bottom(tid):
    """Force the chat scroll container to bottom so the latest message
    (and its lazy-mounted Designer iframe) enters viewport.

    M365 Copilot uses IntersectionObserver-style lazy mounting — iframes
    only attach when their message is on-screen. Without this, the freshly
    generated image's iframe never appears in Page.getFrameTree.
    """
    _eval(tid, """(() => {
      const els = [...document.querySelectorAll('*')].filter(e => {
        const s = getComputedStyle(e);
        return (s.overflowY === 'auto' || s.overflowY === 'scroll')
            && e.scrollHeight > e.clientHeight + 50;
      });
      els.forEach(e => { e.scrollTop = e.scrollHeight; });
      window.scrollTo(0, document.body.scrollHeight);
    })()""")


def _collect_designer_baseline(tid):
    """Snapshot Designer iframe identity at baseline time.

    Returns set of (frame_id, image_hash_or_none) tuples. We track frame_id
    because URL alone is unreliable: new iframes can be mounted as bare
    placeholders (no correlationId), so multiple distinct iframes share the
    SAME bare URL. Image hash inside the iframe lets us distinguish "iframe
    was here at baseline with image X" from "same iframe now has new image Y".
    """
    import hashlib
    _scroll_chat_to_bottom(tid)
    time.sleep(1.5)
    out = set()
    for fid, _ in _list_designer_frames(tid):
        png = _probe_frame_for_image(tid, fid)
        h = hashlib.md5(png).hexdigest() if png else None
        out.add((fid, h))
    return out


def _collect_designer_srcs(tid):
    """Backwards-compat alias — kept so external callers still work."""
    return _collect_designer_baseline(tid)


def _chat_says_image_done(tid):
    """Detect Copilot's completion message in the chat stream.

    The assistant text confirms 'image generated' before the Designer iframe
    finishes its UI update — sometimes the iframe never updates and stays
    stuck on 'generating the image'. See memory feedback_m365_copilot_reload_on_done.md.
    """
    return bool(_eval(tid,
        """(() => {
          const txt = document.body.innerText || '';
          // "已生成。" / "图片已生成" / "image generated" / "image successfully generated"
          // are all the same backend signal. Accept the loosest form — the whole
          // page is the search scope so false positives here are negligible
          // vs missing a real completion.
          return /已生成|image generated|image successfully generated/i.test(txt);
        })()"""
    ))


def _wait_for_new_image(tid, baseline, timeout=WAIT_GEN_SECS):
    """Poll until a Designer iframe contains a base64 PNG that is NOT in
    baseline. baseline = set of (frame_id, image_md5_or_None).

    Timing model (memory feedback_m365_copilot_reload_on_done updated 2026-05-20):
    - Chat text "已生成" appears MUCH earlier than the actual base64 PNG
      lands in the iframe DOM. User observed "可能在已生成后还需要等好久".
    - Strategy: keep polling normally after "已生成"; only reload as a last
      resort if STILL no image GRACE_AFTER_DONE seconds later (real stuck).
    - Premature reload interrupts in-progress iframe hydration → makes it
      worse, not better.
    """
    import hashlib
    start = time.time()
    done_first_seen = None
    reloaded_once = False
    while time.time() - start < timeout:
        _scroll_chat_to_bottom(tid)
        for fid, _ in _list_designer_frames(tid):
            png = _probe_frame_for_image(tid, fid)
            if not png:
                continue
            h = hashlib.md5(png).hexdigest()
            if (fid, h) in baseline:
                continue
            return fid, png
        # Track when "已生成" first appears; reload only if grace period elapsed
        if _chat_says_image_done(tid):
            if done_first_seen is None:
                done_first_seen = time.time()
            elif (not reloaded_once
                  and time.time() - done_first_seen > GRACE_AFTER_DONE):
                sid = _attach(tid)
                try:
                    cdp("Page.reload", session_id=sid)
                finally:
                    _detach(sid)
                time.sleep(8)
                reloaded_once = True
                baseline = set()
                continue
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"no new designer image within {timeout}s")


# ---------- conversation lifecycle (rolling cleanup) ----------

def _conv_id_from_url(url):
    """Extract conversation UUID from m365 URL, or None if URL is /chat."""
    marker = "/chat/conversation/"
    if marker not in (url or ""):
        return None
    tail = url.split(marker, 1)[1]
    return tail.split("?", 1)[0].split("/", 1)[0]


def _read_prev_conv():
    if STATE_FILE.exists():
        v = STATE_FILE.read_text(encoding="utf-8").strip()
        return v or None
    return None


def _write_prev_conv(conv_id):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(conv_id or "", encoding="utf-8")


def _delete_conversation_by_id(tid, conv_id):
    """Delete a conversation from M365 sidebar via PointerEvent chain.

    Mirrors the doubao technique (memory reference_doubao_chat_delete_technique):
    React-managed hover state — JS dispatchEvent on PointerEvents convinces
    React the user really hovered, even when the 3-dots button has 0×0 box.

    Step 1: hover sidebar entry → click "更多操作" (3-dots) button.
    Step 2: in the popped menu, click "删除" item.
    Step 3: in confirm dialog, click "删除" button.

    Returns True on success, False otherwise.
    """
    if not conv_id:
        return False
    # Idempotent: if button is already gone from sidebar, treat as success.
    if not _eval(tid, f'!!document.querySelector(\'button[id="{conv_id}"]\')'):
        return True
    # Step 1: hover sidebar entry → click "More" button (fui-MenuButton).
    # M365 sidebar structure (probed 2026-05-20):
    #   div.fui-SplitNavItem
    #     button[id="<conv_id>"]              ← main entry
    #     button[aria-label="More"][aria-haspopup="menu"]   ← 3-dots
    js1 = """
JSON.stringify((() => {
  const main = document.querySelector('button[id="CONV_ID"]');
  if (!main) return {ok: false, why: 'main button not found'};
  main.scrollIntoView({block: 'center'});
  const wrapper = main.parentElement;
  if (!wrapper) return {ok: false, why: 'no wrapper'};
  ['pointerover','pointerenter','mouseover','mouseenter'].forEach(t =>
    wrapper.dispatchEvent(new PointerEvent(t, {bubbles:true,cancelable:true,pointerType:'mouse'})));
  main.dispatchEvent(new PointerEvent('mouseover', {bubbles:true,cancelable:true,pointerType:'mouse'}));
  const moreBtn = [...wrapper.querySelectorAll('button')].find(b =>
    b !== main && (
      b.classList.contains('fui-MenuButton') ||
      b.getAttribute('aria-haspopup') === 'menu' ||
      /^more$/i.test((b.getAttribute('aria-label')||'').trim()) ||
      /更多/.test(b.getAttribute('aria-label')||'')
    )
  );
  if (!moreBtn) return {ok: false, why: 'more button not found'};
  ['pointerover','pointerenter','mouseover','mouseenter'].forEach(t =>
    moreBtn.dispatchEvent(new PointerEvent(t, {bubbles:true,cancelable:true,pointerType:'mouse'})));
  ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(t =>
    moreBtn.dispatchEvent(new PointerEvent(t, {bubbles:true,cancelable:true,pointerType:'mouse',button:0})));
  return {ok: true};
})())
""".replace("CONV_ID", conv_id)
    r = _eval(tid, js1)
    try:
        r1 = json.loads(r) if r else {"ok": False}
    except Exception:
        r1 = {"ok": False}
    if not r1.get("ok"):
        return False
    time.sleep(0.8)

    # Step 2: click "删除" menu item in dropdown
    js2 = """
JSON.stringify((() => {
  let found = null;
  document.querySelectorAll('[role=menuitem],button,div,span,li').forEach(n => {
    if (found) return;
    const txt = (n.innerText||'').trim();
    if ((txt === '删除' || txt === 'Delete') && n.offsetParent !== null) {
      // child of just-opened menu → first match wins (assume top-level menu)
      found = n;
    }
  });
  if (!found) return {ok: false, why: 'no delete menu item'};
  ['pointerover','pointerenter','mouseover','mouseenter'].forEach(t =>
    found.dispatchEvent(new PointerEvent(t, {bubbles:true,cancelable:true,pointerType:'mouse'})));
  ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(t =>
    found.dispatchEvent(new PointerEvent(t, {bubbles:true,cancelable:true,pointerType:'mouse',button:0})));
  return {ok: true};
})())
"""
    r = _eval(tid, js2)
    try:
        r2 = json.loads(r) if r else {"ok": False}
    except Exception:
        r2 = {"ok": False}
    if not r2.get("ok"):
        return False
    time.sleep(1.0)

    # Step 3: click "删除" / "Delete" confirm in modal dialog
    js3 = """
JSON.stringify((() => {
  let found = null;
  // Dialog confirm button — usually centered, narrow
  document.querySelectorAll('button').forEach(b => {
    if (found) return;
    const txt = (b.innerText||'').trim();
    if ((txt === '删除' || txt === 'Delete') && b.offsetParent !== null) {
      const r = b.getBoundingClientRect();
      // Heuristic: dialog button has reasonable position + size
      if (r.width > 0 && r.width < 250) found = b;
    }
  });
  if (!found) return {ok: false, why: 'no confirm button'};
  ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(t =>
    found.dispatchEvent(new PointerEvent(t, {bubbles:true,cancelable:true,pointerType:'mouse',button:0})));
  return {ok: true};
})())
"""
    r = _eval(tid, js3)
    try:
        r3 = json.loads(r) if r else {"ok": False}
    except Exception:
        r3 = {"ok": False}
    if not r3.get("ok"):
        return False
    # Poll for DOM update — M365 removes the button asynchronously after
    # confirm. Wait up to 5s for the button to be gone.
    deadline = time.time() + 5
    while time.time() < deadline:
        if not _eval(tid, f'!!document.querySelector(\'button[id="{conv_id}"]\')'):
            return True
        time.sleep(0.3)
    return False  # confirm clicked but DOM never updated


def _wait_for_conv_url(tid, timeout=60):
    """After sending a prompt on /chat, the URL eventually flips to
    /chat/conversation/<id>. Poll for that.
    """
    start = time.time()
    while time.time() - start < timeout:
        sid = _attach(tid)
        try:
            r = cdp("Runtime.evaluate", session_id=sid,
                    expression="location.href", returnByValue=True)
            url = r.get("result", {}).get("value", "") or ""
        finally:
            _detach(sid)
        cid = _conv_id_from_url(url)
        if cid:
            return cid
        time.sleep(1.0)
    return None


# ---------- public API (mirrors doubao_generate / doubao_pick) ----------

def generate(prompt, save_dir, max_retry=0):
    """Generate 1 image via M365 Copilot Designer in a FRESH conversation.

    After a successful generation, deletes the previous task's conversation
    from the sidebar (rolling 1-window cleanup) per user direction
    "一个对话一张，生成完成就删掉老的".

    Returns:
        {
            'prompt': str,                # full prompt sent (with prefix)
            'session_dir': str,
            'fulls': [path],              # 1 PNG
            'thumbnails': [path],         # alias of fulls
            'conversation_id': str,       # the conv where this image was generated
        }
    """
    save_dir_p = Path(save_dir)
    save_dir_p.mkdir(parents=True, exist_ok=True)
    full_prompt = prompt if prompt.startswith(PROMPT_PREFIX) else PROMPT_PREFIX + prompt

    tid = _force_agent_tab_in_secondary()
    # Always start in a fresh /chat — each task gets its own conversation.
    navigate_agent(tid, "about:blank")
    time.sleep(0.4)
    navigate_agent(tid, NEW_CHAT_URL)
    time.sleep(8.0)  # SPA hydrate
    baseline = _collect_designer_baseline(tid)
    _switch_to_gpt_deep(tid)
    _inject_and_send(tid, full_prompt)
    _, png_bytes = _wait_for_new_image(tid, baseline)
    out_path = save_dir_p / "image_0.png"
    out_path.write_bytes(png_bytes)
    (save_dir_p / "prompt.txt").write_text(full_prompt, encoding="utf-8")

    # Capture conversation_id AFTER image — by now the SPA has pushed
    # /chat/conversation/<id> for sure (image only renders post-routing).
    # Polling pre-image is racy: SPA may take >60s to update URL.
    new_conv_id = _wait_for_conv_url(tid, timeout=10)

    # Rolling cleanup: delete the PREVIOUS task's conversation (if any).
    prev = _read_prev_conv()
    if prev and prev != new_conv_id:
        try:
            _delete_conversation_by_id(tid, prev)
        except Exception:
            pass  # best-effort; will retry next call
    # Save this conversation as the next "previous" to clean up.
    if new_conv_id:
        _write_prev_conv(new_conv_id)

    return {
        "prompt": full_prompt,
        "session_dir": str(save_dir_p),
        "fulls": [str(out_path)],
        "thumbnails": [str(out_path)],
        "conversation_id": new_conv_id,
    }


def pick(session, idx, dst_path):
    """Copy chosen image to dst_path. Copilot only returns one, so idx must be 0."""
    src = Path(session["fulls"][idx])
    dst = Path(dst_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    for p in session["fulls"]:
        Path(p).unlink(missing_ok=True)
    return str(dst)
