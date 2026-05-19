"""GPT image-2 via sub2api — paid image-generation path.

Mirror of `doubao.generate`/`doubao.pick` signatures so SKILL code can switch
between the two backends by swapping function names.

Per `feedback_image_gen_ask_first`: the agent must ask the user before picking
this path — it costs real money. Doubao is the free default.

Env vars:
    SUB2API_BASE  — e.g. https://sub2api.tcgcard.jp
    SUB2API_KEY   — sk-...

These are read at call time, not import time, so unit tests can monkeypatch.
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import shutil
import time
from pathlib import Path
from urllib import error, request

MODEL = "gpt-image-2"
PATH = "/v1/images/generations"

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


class Sub2ApiError(RuntimeError):
    pass


def _post(prompt: str, n: int, size: str, timeout: int) -> dict:
    base = os.environ.get("SUB2API_BASE", "").rstrip("/")
    key = os.environ.get("SUB2API_KEY", "")
    if not base or not key:
        raise Sub2ApiError("set SUB2API_BASE and SUB2API_KEY env vars")

    body = json.dumps({
        "model": MODEL,
        "prompt": prompt,
        "n": n,
        "size": size,
    }).encode()
    req = request.Request(
        f"{base}{PATH}",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": _BROWSER_UA,
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as r:
            try:
                raw = r.read()
            except http.client.IncompleteRead as ie:
                raw = ie.partial
        return json.loads(raw)
    except error.HTTPError as e:
        msg = e.read().decode(errors="replace")[:600]
        raise Sub2ApiError(f"HTTP {e.code}: {msg}") from e
    except json.JSONDecodeError as e:
        raise Sub2ApiError(f"non-JSON response (got {len(raw)} bytes): {e}") from e


def _save_items(payload: dict, save_dir: Path, tag: str) -> list[Path]:
    items = payload.get("data", []) or []
    saved: list[Path] = []
    for i, item in enumerate(items):
        out = save_dir / f"{tag}_{i}.png"
        if "b64_json" in item:
            out.write_bytes(base64.b64decode(item["b64_json"]))
        elif "url" in item:
            with request.urlopen(item["url"], timeout=60) as r:
                out.write_bytes(r.read())
        else:
            raise Sub2ApiError(f"item {i} has neither b64_json nor url: {list(item.keys())}")
        saved.append(out)
    if not saved:
        raise Sub2ApiError(f"empty data array; payload keys = {list(payload.keys())}")
    return saved


def generate(
    prompt: str,
    save_dir: str,
    n: int = 1,
    size: str = "1024x1024",
    max_retry: int = 1,
    timeout: int = 180,
) -> dict:
    """Generate `n` images via sub2api gpt-image-2 and save to `save_dir`.

    Mirrors `doubao.generate` return shape so callers can pick the path
    dynamically.

    Returns:
        {
            'prompt': str,
            'session_dir': str,
            'fulls': [path, ...],       # n PNGs at requested size
            'thumbnails': [path, ...],  # alias of fulls
            'usage': {...},             # input/output tokens from sub2api
            'model': 'gpt-image-2',
        }

    Raises:
        Sub2ApiError on HTTP failure or empty response after all retries.
    """
    save_dir_p = Path(save_dir)
    save_dir_p.mkdir(parents=True, exist_ok=True)
    (save_dir_p / "prompt.txt").write_text(prompt, encoding="utf-8")

    tag = f"gpt_{int(time.time())}"
    last_err: Exception | None = None
    for attempt in range(max_retry + 1):
        try:
            t0 = time.time()
            payload = _post(prompt, n=n, size=size, timeout=timeout)
            saved = _save_items(payload, save_dir_p, tag)
            return {
                "prompt": prompt,
                "session_dir": str(save_dir_p),
                "fulls": [str(p) for p in saved],
                "thumbnails": [str(p) for p in saved],
                "usage": payload.get("usage", {}),
                "model": MODEL,
                "elapsed_sec": round(time.time() - t0, 1),
            }
        except Sub2ApiError as e:
            last_err = e
            if attempt < max_retry:
                time.sleep(2.0)
                continue
            raise
    raise Sub2ApiError(f"unreachable; last_err={last_err}")


def pick(session: dict, idx: int, dst_path: str) -> str:
    """Move chosen image to dst_path; delete the rest. Same shape as doubao.pick."""
    src = Path(session["fulls"][idx])
    dst = Path(dst_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    for p in session["fulls"]:
        Path(p).unlink(missing_ok=True)
    return str(dst)
