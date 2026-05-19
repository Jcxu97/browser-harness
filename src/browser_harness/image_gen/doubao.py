"""Doubao image generation driver via second-window agent tab.

Lossless watermark removal via the dual-URL trick from
github.com/Qalxry/doubao-no-watermark — Doubao's React `realImageInfo` exposes
two CDN URLs per image:
  - previewImage.url  (image_pre_watermark)  → watermark TOP-LEFT
  - downloadImage.url (image_dld_watermark)  → watermark BOTTOM-RIGHT
Compose: top-left half from B + bottom-right half from A → no watermarked
pixel ever appears in the output.

Pipeline:
1. Open / reuse Doubao chat in second window (logged-in profile).
2. Click "图像生成" pill so the Slate prompt editor is visible.
3. Inject prompt via CDP `Input.insertText` (Slate-friendly IME path).
4. Click the send button (36×36 SVG-only button, x>1500 y>700).
5. Poll for 4 unique `rc_gen_image/{id}` thumbnails.
6. For each thumbnail: open viewer → walk React fiber for `realImageInfo`
   → force-load both URLs in-page (`new Image().src = ...`) → intercept
   responses via Network domain → save raw bytes.
7. Merge each pair → clean PNG via `merge_doubao_pair`.

Public API:
- generate(prompt, save_dir) → {'session_dir', 'fulls', 'thumbnails', ...}
- pick(session, idx, dst_path) → str
"""
from __future__ import annotations
import base64
import json
import shutil
import time
from pathlib import Path

from ..helpers import cdp, drain_events
from ..second_window import (
    ensure_agent_tab,
    _attach,
    _detach,
)
from .watermark import merge_doubao_pair

DOUBAO_URL = "https://www.doubao.com/chat/"
WAIT_GEN_SECS = 240
POLL_INTERVAL = 2.5


def _eval(tid: str, expr: str, await_promise: bool = False):
    sid = _attach(tid)
    try:
        r = cdp(
            "Runtime.evaluate",
            session_id=sid,
            expression=expr,
            awaitPromise=await_promise,
            returnByValue=True,
        )
        return r.get("result", {}).get("value")
    finally:
        _detach(sid)


def _switch_to_image_mode(tid: str) -> None:
    """Click the bottom 图像生成 pill so the Slate prompt editor surfaces."""
    # already in image mode?
    state = _eval(tid, """
JSON.stringify((() => {
  const el = document.querySelector('div[contenteditable="true"]');
  if (!el) return {ready:false};
  const ph = el.querySelector('[data-slate-placeholder="true"]') ||
             [...el.querySelectorAll('*')].find(n => /描述你想要的图片/.test(n.textContent||''));
  return {ready:true, isImage: !!(ph && /描述/.test(ph.textContent||''))};
})())
""")
    info = json.loads(state) if state else {"ready": False}
    if info.get("isImage"):
        return

    clicked = _eval(tid, """
JSON.stringify((() => {
  const btns = [...document.querySelectorAll('button')]
    .filter(b => b.textContent.trim() === '图像生成');
  if (!btns.length) return false;
  btns[0].click();
  return true;
})())
""")
    if not clicked or clicked == "false":
        raise RuntimeError("could not find 图像生成 pill")

    # wait for Slate editor to appear
    deadline = time.time() + 8
    while time.time() < deadline:
        time.sleep(0.4)
        s = _eval(tid, """
JSON.stringify((() => {
  const el = document.querySelector('div[contenteditable="true"]');
  return !!el;
})())
""")
        if s == "true":
            return
    raise RuntimeError("Slate editor never appeared after pill click")


def _inject_prompt(tid: str, prompt: str) -> None:
    """Clear contenteditable and insert prompt via CDP Input.insertText."""
    sid = _attach(tid)
    try:
        cdp("Runtime.evaluate", session_id=sid, expression="""
(() => {
  const el = document.querySelector('div[contenteditable="true"]');
  if (!el) return;
  el.focus();
  const range = document.createRange();
  range.selectNodeContents(el);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
})()
""")
        cdp("Input.dispatchKeyEvent", session_id=sid, type="rawKeyDown",
            code="Delete", key="Delete", windowsVirtualKeyCode=46, nativeVirtualKeyCode=46)
        cdp("Input.dispatchKeyEvent", session_id=sid, type="keyUp",
            code="Delete", key="Delete", windowsVirtualKeyCode=46, nativeVirtualKeyCode=46)
        time.sleep(0.25)
        cdp("Input.insertText", session_id=sid, text=prompt)
    finally:
        _detach(sid)


def _click_send(tid: str) -> None:
    """Click the 36×36 svg-only button at the right edge below the editor."""
    r = _eval(tid, """
JSON.stringify((() => {
  const btns = [...document.querySelectorAll('button, [role="button"]')]
    .filter(b => {
      if (b.offsetParent === null) return false;
      const r = b.getBoundingClientRect();
      return r.width >= 30 && r.width <= 50
          && r.height >= 30 && r.height <= 50
          && r.x > 1400 && r.y > 700
          && !!b.querySelector('svg')
          && !b.textContent.trim();
    });
  if (!btns.length) return {ok:false, n:0};
  btns[btns.length - 1].click();
  return {ok:true, n:btns.length};
})())
""")
    info = json.loads(r) if r else {"ok": False}
    if not info.get("ok"):
        raise RuntimeError("send button not found — prompt may not have registered")


def _wait_thumbnails(tid: str, timeout: int = WAIT_GEN_SECS) -> list[dict]:
    """Poll until ≥4 unique rc_gen_image thumbnails are present."""
    start = time.time()
    last = {"count": 0}
    while time.time() - start < timeout:
        raw = _eval(tid, """
JSON.stringify((() => {
  const seen = new Set();
  const out = [];
  for (const i of document.querySelectorAll('img')) {
    const u = i.src || '';
    if (!u.includes('rc_gen_image')) continue;
    const m = u.match(/rc_gen_image\\/([a-f0-9]+)/);
    if (!m || seen.has(m[1])) continue;
    seen.add(m[1]);
    out.push({id: m[1], src: u});
  }
  const generating = (document.body.innerText||'').includes('生成中') ||
                     (document.body.innerText||'').includes('排队中');
  return {generating, count: out.length, items: out};
})())
""")
        last = json.loads(raw) if raw else {"count": 0}
        if last.get("count", 0) >= 4 and not last.get("generating"):
            return last["items"][:4]
        # also accept ≥4 even if "生成中" still present (4 may be older session)
        if last.get("count", 0) >= 4:
            return last["items"][:4]
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"only {last.get('count',0)} thumbnails after {timeout}s")


def _open_viewer_and_extract(tid: str, thumb_id: str, max_wait: int = 10) -> dict:
    """Click thumbnail, wait for viewer, extract realImageInfo from React fiber.

    Returns {'previewUrl': str, 'downloadUrl': str}. Reraises on failure.
    """
    sid = _attach(tid)
    try:
        # click thumbnail + parent walk (Doubao thumbs are nested)
        cdp("Runtime.evaluate", session_id=sid, expression=f"""
(() => {{
  const imgs = [...document.querySelectorAll('img')].filter(i => (i.src||'').includes('{thumb_id}'));
  if (!imgs.length) return false;
  imgs[0].click();
  let p = imgs[0].parentElement;
  for (let k=0; k<3 && p; k++) {{ p.click(); p = p.parentElement; }}
  return true;
}})()
""", returnByValue=True)

        deadline = time.time() + max_wait
        last_reason = None
        while time.time() < deadline:
            time.sleep(0.4)
            r = cdp("Runtime.evaluate", session_id=sid, expression=f"""
JSON.stringify((() => {{
  const imgs = [...document.querySelectorAll('img')]
    .filter(i => (i.src||'').includes('{thumb_id}'));
  if (!imgs.length) return {{found:false, reason:'no img'}};
  // pick the largest rendered img containing this id
  let big = imgs[0];
  let bigArea = 0;
  for (const im of imgs) {{
    const r = im.getBoundingClientRect();
    const a = r.width * r.height;
    if (a > bigArea) {{ big = im; bigArea = a; }}
  }}
  if (Math.sqrt(bigArea) < 250) return {{found:false, reason:'no large img', area:bigArea}};

  const fiberKey = Object.keys(big).find(k => k.startsWith('__reactFiber'));
  if (!fiberKey) return {{found:false, reason:'no fiber key'}};
  let fiber = big[fiberKey];
  let depth = 0;
  while (fiber && depth < 20) {{
    const props = fiber.memoizedProps;
    if (props && props.realImageInfo
        && props.realImageInfo.previewImage && props.realImageInfo.previewImage.url
        && props.realImageInfo.downloadImage && props.realImageInfo.downloadImage.url) {{
      return {{
        found: true,
        previewUrl: props.realImageInfo.previewImage.url,
        downloadUrl: props.realImageInfo.downloadImage.url,
      }};
    }}
    fiber = fiber.return;
    depth++;
  }}
  return {{found:false, reason:'fiber walk exhausted'}};
}})())
""", returnByValue=True)
            v = r.get("result", {}).get("value")
            d = json.loads(v) if v else {}
            if d.get("found"):
                return {"previewUrl": d["previewUrl"], "downloadUrl": d["downloadUrl"]}
            last_reason = d.get("reason")
        raise RuntimeError(f"fiber realImageInfo not found for {thumb_id} ({last_reason})")
    finally:
        _detach(sid)


def _intercept_pair(tid: str, preview_url: str, download_url: str,
                    dst_dir: Path, idx: int, max_wait: int = 30) -> tuple[Path, Path]:
    """Force-load both URLs in-page, intercept responses via CDP Network."""
    sid = _attach(tid)
    try:
        cdp("Network.enable", session_id=sid)
        cdp("Network.setCacheDisabled", session_id=sid, cacheDisabled=True)
        drain_events()

        # in-page Image() loader — bypasses CORS for byte capture
        cdp("Runtime.evaluate", session_id=sid, expression=f"""
(() => {{
  const a = new Image(); a.src = {json.dumps(preview_url)};
  const b = new Image(); b.src = {json.dumps(download_url)};
  window.__bh_imgs = [a, b];  // hold refs to prevent GC
}})()
""")

        deadline = time.time() + max_wait
        preview_rid = None
        download_rid = None
        while time.time() < deadline and not (preview_rid and download_rid):
            time.sleep(0.4)
            for ev in drain_events():
                if ev.get("method") != "Network.responseReceived":
                    continue
                url = ev.get("params", {}).get("response", {}).get("url", "")
                rid = ev["params"]["requestId"]
                if not preview_rid and url == preview_url:
                    preview_rid = rid
                if not download_rid and url == download_url:
                    download_rid = rid

        if not preview_rid or not download_rid:
            raise RuntimeError(
                f"intercept timeout: preview={'yes' if preview_rid else 'NO'} "
                f"download={'yes' if download_rid else 'NO'}"
            )

        # let body finish
        time.sleep(1.5)
        preview_p = dst_dir / f"_pre_{idx}.png"
        download_p = dst_dir / f"_dld_{idx}.png"
        for path, rid in [(preview_p, preview_rid), (download_p, download_rid)]:
            body = cdp("Network.getResponseBody", session_id=sid, requestId=rid)
            data = base64.b64decode(body["body"]) if body.get("base64Encoded") else body["body"].encode()
            path.write_bytes(data)
        return preview_p, download_p
    finally:
        try:
            cdp("Network.setCacheDisabled", session_id=sid, cacheDisabled=False)
        except Exception:
            pass
        _detach(sid)


def _close_viewer(tid: str) -> None:
    """Press Escape to dismiss the image viewer if open."""
    sid = _attach(tid)
    try:
        cdp("Input.dispatchKeyEvent", session_id=sid, type="rawKeyDown",
            code="Escape", key="Escape", windowsVirtualKeyCode=27, nativeVirtualKeyCode=27)
        cdp("Input.dispatchKeyEvent", session_id=sid, type="keyUp",
            code="Escape", key="Escape", windowsVirtualKeyCode=27, nativeVirtualKeyCode=27)
    finally:
        _detach(sid)
    time.sleep(0.6)


def generate(prompt: str, save_dir: str, max_retry: int = 1) -> dict:
    """Generate 4 image candidates and download all 4 with watermark removed.

    Returns:
        {
            'prompt': str,
            'session_dir': str,
            'fulls': [path, ...],         # 4 clean PNGs (lossless dual-merge)
            'thumbnails': [path, ...],    # alias of fulls — Claude reads these
            'thumb_ids': [str, ...],
        }
    """
    save_dir_p = Path(save_dir)
    save_dir_p.mkdir(parents=True, exist_ok=True)

    tid = ensure_agent_tab()

    last_err: Exception | None = None
    for attempt in range(max_retry + 1):
        try:
            sid = _attach(tid)
            try:
                cdp("Page.navigate", session_id=sid, url=DOUBAO_URL)
            finally:
                _detach(sid)
            time.sleep(3.5)

            _switch_to_image_mode(tid)
            time.sleep(1.0)
            _inject_prompt(tid, prompt)
            time.sleep(0.6)
            _click_send(tid)

            thumbs = _wait_thumbnails(tid)

            full_paths: list[str] = []
            for i, t in enumerate(thumbs):
                urls = _open_viewer_and_extract(tid, t["id"])
                pre_p, dld_p = _intercept_pair(
                    tid, urls["previewUrl"], urls["downloadUrl"],
                    save_dir_p, i,
                )
                clean = save_dir_p / f"image_{i}.png"
                merge_doubao_pair(str(pre_p), str(dld_p), str(clean))
                pre_p.unlink(missing_ok=True)
                dld_p.unlink(missing_ok=True)
                full_paths.append(str(clean))
                _close_viewer(tid)

            (save_dir_p / "prompt.txt").write_text(prompt, encoding="utf-8")

            return {
                "prompt": prompt,
                "session_dir": str(save_dir_p),
                "fulls": full_paths,
                "thumbnails": full_paths,  # high-res IS the preview
                "thumb_ids": [t["id"] for t in thumbs],
            }
        except Exception as e:
            last_err = e
            if attempt < max_retry:
                time.sleep(2)
                continue
            raise

    raise RuntimeError(f"generate failed: {last_err}")


def pick(session: dict, idx: int, dst_path: str) -> str:
    """Move chosen image to dst_path; delete the rest."""
    src = Path(session["fulls"][idx])
    dst = Path(dst_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    for p in session["fulls"]:
        Path(p).unlink(missing_ok=True)
    return str(dst)
