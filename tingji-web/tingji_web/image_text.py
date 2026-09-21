"""Shared offline Chinese OCR. Models ship in the wheel; no API or downloads at runtime."""
from __future__ import annotations

import threading

_lock = threading.Lock()
_engine = None
MAX_PIXELS = 18_000_000


def read_words(image) -> list[dict]:
    """Return text lines, confidence and boxes in the original image coordinates."""
    global _engine
    if image.width * image.height > MAX_PIXELS:
        raise ValueError("图片像素过大，请压缩后导入。")
    import numpy as np
    from PIL import Image, ImageOps
    from rapidocr_onnxruntime import RapidOCR

    image = ImageOps.exif_transpose(image).convert("RGBA")
    canvas = Image.new("RGBA", image.size, "white")
    canvas.alpha_composite(image)
    # Bounded CPU use and one shared model instance per server process.
    with _lock:
        if _engine is None:
            _engine = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)
        result, _ = _engine(np.asarray(canvas.convert("RGB")))
    words = []
    for box, text, confidence in result or []:
        if not text.strip():
            continue
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        words.append({"text": text.strip(), "x": min(xs), "y": min(ys),
                      "w": max(xs) - min(xs), "h": max(ys) - min(ys), "confidence": float(confidence)})
    return words


def colored_blocks(image) -> list[tuple[int, int, int, int]]:
    """Locate solid coloured course cards, without interpreting their text."""
    import cv2
    import numpy as np
    pixels = np.asarray(image.convert("RGB"))
    saturation = pixels.max(axis=2).astype(np.int16) - pixels.min(axis=2)
    mask = (saturation > 35).astype(np.uint8)
    _, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    return [(int(x), int(y), int(x + w), int(y + h)) for x, y, w, h, area in stats[1:]
            if w >= 40 and h >= 25 and area >= w * h * .55]
