"""sub2api gpt-image-2 smoke test.

Env vars (required):
    SUB2API_BASE  — e.g. https://sub2api.tcgcard.jp
    SUB2API_KEY   — sk-...

Run (PowerShell):
    $env:SUB2API_BASE="https://sub2api.tcgcard.jp"
    $env:SUB2API_KEY="sk-..."
    python test_gpt_image2.py
"""
import base64
import json
import os
import sys
import time
from pathlib import Path
from urllib import request, error

BASE = os.environ.get("SUB2API_BASE", "").rstrip("/")
KEY = os.environ.get("SUB2API_KEY", "")
if not BASE or not KEY:
    sys.exit("set SUB2API_BASE and SUB2API_KEY env vars first")
OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)


def call(prompt: str, size: str = "1024x1024", n: int = 1, tag: str = "t1"):
    body = json.dumps({
        "model": "gpt-image-2",
        "prompt": prompt,
        "n": n,
        "size": size,
    }).encode()
    req = request.Request(
        f"{BASE}/v1/images/generations",
        data=body,
        headers={
            "Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/130.0.0.0 Safari/537.36",
            "Accept": "application/json",
        },
        method="POST",
    )
    t0 = time.time()
    try:
        with request.urlopen(req, timeout=180) as r:
            payload = json.loads(r.read())
    except error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"[HTTP {e.code}] {body[:600]}")
        return
    except Exception as e:
        print(f"[ERR] {type(e).__name__}: {e}")
        return
    dt = time.time() - t0
    print(f"[OK {dt:.1f}s] keys={list(payload.keys())}")
    items = payload.get("data", [])
    print(f"  data len = {len(items)}")
    for i, item in enumerate(items):
        if "b64_json" in item:
            raw = base64.b64decode(item["b64_json"])
            p = OUT / f"{tag}_{i}.png"
            p.write_bytes(raw)
            print(f"  saved b64 -> {p}  ({len(raw)//1024} KB)")
        elif "url" in item:
            print(f"  url[{i}] = {item['url'][:120]}")
            try:
                raw = request.urlopen(item["url"], timeout=60).read()
                p = OUT / f"{tag}_{i}.png"
                p.write_bytes(raw)
                print(f"  downloaded -> {p}  ({len(raw)//1024} KB)")
            except Exception as e:
                print(f"  download fail: {e}")
        else:
            print(f"  item[{i}] keys={list(item.keys())}")
    if "usage" in payload:
        print(f"  usage = {payload['usage']}")


if __name__ == "__main__":
    prompt = (
        "A photorealistic shot of a red origami crane sitting on a polished "
        "wooden desk next to a cup of black coffee, soft morning light from "
        "the left, shallow depth of field, 35mm photography style."
    )
    call(prompt, size="1024x1024", n=1, tag="smoke")
