"""Doubao "AI 生成" watermark removal.

Two strategies:

1. **Lossless dual-image merge** (preferred) — adapted from
   github.com/Qalxry/doubao-no-watermark. Doubao serves two URLs per image:
   - `image_pre_watermark` URL  → watermark in TOP-LEFT
   - `image_dld_watermark` URL  → watermark in BOTTOM-RIGHT
   Take the right-bottom half from #1 + the top-left half from #2 → no pixel
   is ever from a watermarked region. Truly lossless.

2. **cv2.inpaint fallback** — if only the preview URL is available, mask the
   top-left "AI 生成" region (ratios validated on 1773×2364 output) and use
   TELEA inpainting. Lossy (smudge artifact possible) but no second URL needed.
"""
from pathlib import Path

WM_X1, WM_X2 = 0.018, 0.165
WM_Y1, WM_Y2 = 0.014, 0.058


def merge_doubao_pair(preview_path: str, download_path: str, dst_path: str) -> str:
    """Lossless watermark removal by merging the two Doubao image URLs.

    `preview_path` is the file saved from `image_pre_watermark` (TL watermark);
    `download_path` is the file saved from `image_dld_watermark` (BR watermark).
    Output: clean PNG at `dst_path`.
    """
    from PIL import Image

    a = Image.open(preview_path).convert("RGB")    # bottom-right clean
    b = Image.open(download_path).convert("RGB")   # top-left clean

    if a.size != b.size:
        b = b.resize(a.size, Image.LANCZOS)

    W, H = a.size
    half_w = (W + 1) // 2
    half_h = (H + 1) // 2

    out = a.copy()
    tl = b.crop((0, 0, half_w, half_h))
    out.paste(tl, (0, 0))

    Path(dst_path).parent.mkdir(parents=True, exist_ok=True)
    if dst_path.lower().endswith((".jpg", ".jpeg")):
        out.save(dst_path, "JPEG", quality=95)
    else:
        out.save(dst_path, "PNG")
    return dst_path


def remove_doubao_watermark(src_path: str, dst_path: str | None = None) -> str:
    """Inpaint the top-left "AI 生成" watermark. Returns dst_path.

    Lossy fallback when the dld URL is not available — prefer
    `merge_doubao_pair` if you have both URLs.
    """
    import cv2
    import numpy as np

    src = Path(src_path)
    dst = Path(dst_path) if dst_path else src

    img = cv2.imread(str(src))
    if img is None:
        raise ValueError(f"cv2 failed to read {src}")
    H, W = img.shape[:2]

    x1 = int(W * WM_X1)
    y1 = int(H * WM_Y1)
    x2 = int(W * WM_X2)
    y2 = int(H * WM_Y2)

    mask = np.zeros((H, W), dtype=np.uint8)
    mask[y1:y2, x1:x2] = 255

    out = cv2.inpaint(img, mask, 5, cv2.INPAINT_TELEA)

    if str(dst).lower().endswith((".jpg", ".jpeg")):
        cv2.imwrite(str(dst), out, [cv2.IMWRITE_JPEG_QUALITY, 95])
    else:
        cv2.imwrite(str(dst), out)
    return str(dst)
