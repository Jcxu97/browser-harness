# sub2api gpt-image-2 (paid path)

Reference smoke test for the **OpenAI-compatible gpt-image-2 endpoint** behind
`sub2api`. Lives next to `image_gen/doubao.py` (the free Doubao web path) as the
**second image-generation route** for browser-harness.

## When to pick which

| Path | Cost | Speed | Quality | When |
|------|------|-------|---------|------|
| **Doubao** (`image_gen.doubao_generate`) | free | ~2-3 min | 1773×2364 ×4, 4-pick | default; multi-option needed |
| **gpt-image-2** (this) | paid (sub2api) | ~70 s | 1024×1024 ×1, single | one-shot, time-critical, commercial |

Per `feedback_image_gen_ask_first.md`: **agent must ask the user which path to
use** before generating an image — GPT is meaningfully more expensive.

## Run

```powershell
$env:SUB2API_BASE = "https://sub2api.tcgcard.jp"
$env:SUB2API_KEY  = "sk-..."
python test_gpt_image2.py
# -> out/smoke_0.png  (1024×1024, ~1.4 MB, ~70s)
```

## Endpoint quirks

- Standard OpenAI body: `{model, prompt, n, size}`.
- Returns **`b64_json` only** (no `url` field) — same shape as official `gpt-image-1`.
- Cloudflare in front: **must** send a real browser User-Agent or you get
  HTTP 403 / `error code: 1010`.
- `usage` reports `image_tokens` separately — ~1756 out tokens for 1024×1024.

## Models available (as of 2026-05-20)

`/v1/models` returns 6 entries; the only image-capable one is `gpt-image-2`.

## Promoted into `image_gen` (2026-05-20)

Done — this folder is now the **reference smoke test**, not the entry point.
Production code lives at:

```
src/browser_harness/image_gen/gpt_image.py
```

Public API (mirror of `doubao_generate` / `doubao_pick`):

```python
from browser_harness.image_gen import gpt_image_generate, gpt_image_pick

session = gpt_image_generate(prompt, save_dir, n=1, size="1024x1024")
gpt_image_pick(session, idx=0, dst_path=...)
```

Module also exports `Sub2ApiError`. The smoke test in this folder still runs
standalone (env vars only) for quick CF/UA debugging without importing the
full package.

## Known quirk

sub2api's upstream sometimes terminates the chunked response without the
final 0-length chunk → `http.client.IncompleteRead`. The `gpt_image` module
catches this and parses `partial` bytes (typically the full payload was
already received). Standalone smoke test does not — if you hit it there,
that's the cause.
