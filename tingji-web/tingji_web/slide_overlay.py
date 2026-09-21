"""Offline OCR layout and bounded, lossless PNG translation overlays.

Coordinates refer to the EXIF-oriented source pixels. Original files are never
modified. A block that cannot be rendered completely is left untouched and is
reported in the returned metadata; callers must check ``complete``.
"""
from __future__ import annotations

import hashlib
import io
import math
import os
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import image_text

VERSION = 1
MIN_FONT_SIZE = 5
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_UNITS = {"m", "mm", "cm", "km", "nm", "kg", "g", "mg", "s", "ms", "min", "h",
          "N", "kN", "Pa", "kPa", "MPa", "GPa", "K", "C", "F", "V", "A", "W", "kW",
          "J", "kJ", "Hz", "kHz", "rpm", "mol", "rad", "deg"}
_NUMBER = re.compile(r"(?<![A-Za-z\d])[-+]?\d+(?:[.,]\d+)*(?:[eE][-+]?\d+)?")


def _open_image(content: bytes) -> Image.Image:
    with Image.open(io.BytesIO(content)) as source:
        if source.width * source.height > image_text.MAX_PIXELS:
            raise ValueError("课件页像素过大，请降低渲染分辨率。")
        image = ImageOps.exif_transpose(source)
        return image.convert("RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB")


def _candidate(text: str) -> bool:
    words = re.findall(r"[A-Za-z]+", text)
    prose = [word for word in words if len(word) >= 3 and word not in _UNITS]
    if not prose:
        return False
    if len(words) == 1 and len(words[0]) <= 4:
        word = words[0]
        if (not word.islower() and not word.istitle()) or re.search(r"[₀-₉⁰¹²³⁴⁵⁶⁷⁸⁹]", text):
            return False
    # Standalone equations and variable lists stay in their original typography.
    if re.search(r"[=∫∑√^±≈≤≥]", text):
        return False
    return True


def _protected_tokens(text: str) -> list[str]:
    tokens = _CJK.findall(text) + _NUMBER.findall(text)
    tokens += [word for word in re.findall(r"(?<![A-Za-z])[A-Za-z]+(?![A-Za-z])", text)
               if word in _UNITS and (len(word) > 1 or re.search(r"\d\s*" + re.escape(word) + r"\b", text))]
    tokens += re.findall(r"[°℃℉%‰]+", text)
    tokens += re.findall(r"[Α-Ωα-ωϑϕ]+[₀-₉⁰¹²³⁴⁵⁶⁷⁸⁹]*", text)
    tokens += re.findall(r"\b[A-Z]{2,}\d+\b", text)
    tokens += [match.group(0) for match in re.finditer(r"(?<![\w'’])[A-Za-z](?![\w'’])", text)
               if match.group(0) not in {"a", "A", "I"} and match.group(0) not in tokens
               and not (match.group(0) == "p" and re.match(r"\.\s*\d", text[match.end():]))]
    # Keep OCR's spelling of exponents; the renderer never guesses a corrected
    # physical unit or formula from the model's translation.
    tokens += [match.group(0) for match in re.finditer(r"\b[A-Za-z]+[23²³](?!\d)", text)
               if re.sub(r"[23²³]$", "", match.group(0)) in _UNITS]
    return tokens


def _rect(word: dict, width: int, height: int) -> tuple[int, int, int, int] | None:
    try:
        x, y, w, h = (float(word[key]) for key in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (x, y, w, h)) or w <= 0 or h <= 0:
        return None
    left, top = max(0, math.floor(x)), max(0, math.floor(y))
    right, bottom = min(width, math.ceil(x + w)), min(height, math.ceil(y + h))
    return (left, top, right, bottom) if right > left and bottom > top else None


def _palette(pixels: np.ndarray) -> tuple[list[int], list[int], float]:
    rgb = pixels[..., :3]
    bins = rgb.astype(np.int32) // 16
    keys = bins[..., 0] * 256 + bins[..., 1] * 16 + bins[..., 2]
    keys_flat = keys.ravel()
    dominant = np.bincount(keys_flat, minlength=4096).argmax()
    background = np.median(rgb[keys == dominant], axis=0).astype(np.uint8)
    distance = np.max(np.abs(rgb.astype(np.int16) - background), axis=2)
    foreground_pixels = rgb[distance > 55]
    if len(foreground_pixels):
        core_distance = distance[distance > 55]
        foreground = np.median(foreground_pixels[core_distance >= np.quantile(core_distance, .8)], axis=0).astype(np.uint8)
    else:
        foreground = np.array([0, 0, 0] if background.mean() > 128 else [255, 255, 255], dtype=np.uint8)
    return background.tolist(), foreground.tolist(), float(np.mean(keys == dominant))


def _refine_box(pixels: np.ndarray, box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Include clipped connected glyph tips, never neighbouring components."""
    import cv2
    x, y, right, bottom = box
    small_label = bottom - y <= 20
    margin = min(14, max(6 if small_label else 3, round((bottom - y) * .2)))
    left, top = max(0, x - margin), max(0, y - margin)
    end_x, end_y = min(pixels.shape[1], right + margin), min(pixels.shape[0], bottom + margin)
    sample = pixels[top:end_y, left:end_x, :3]
    background, _, uniformity = _palette(pixels[y:bottom, x:right])
    if uniformity < .35:
        return box
    # Tiny raster labels can have compression noise joining a whole word into
    # one component. A firmer edge threshold recovers its letters separately.
    mask = (np.max(np.abs(sample.astype(np.int16) - np.array(background)), axis=2) > (15 if small_label else 5)).astype(np.uint8)
    _, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    inside = labels[y - top:bottom - top, x - left:right - left]
    selected, counts = np.unique(inside[inside > 0], return_counts=True)
    bounds = [x, y, right, bottom]
    for label, count in zip(selected, counts):
        cx, cy, w, h, area = stats[label]
        if count < area * .35 or w > (bottom - y) * (3.5 if small_label else 3) or h > (bottom - y) * 1.3:
            continue
        bounds = [min(bounds[0], left + int(cx)), min(bounds[1], top + int(cy)),
                  max(bounds[2], left + int(cx + w)), max(bounds[3], top + int(cy + h))]
    return tuple(bounds)


def _merge_word_fragments(words: list[dict], pixels: np.ndarray) -> list[dict]:
    """Recover prose rows before short words and Chinese fragments are filtered."""
    height, width = pixels.shape[:2]
    rows = []
    for word in sorted(words, key=lambda item: (item["y"] + item["h"] / 2, item["x"], str(item.get("text", "")))):
        box = _rect(word, width, height)
        if not box:
            continue
        matches = []
        for index, row in enumerate(rows):
            reference = max(row, key=lambda item: item["h"])
            center_gap = abs(word["y"] + word["h"] / 2 - reference["y"] - reference["h"] / 2)
            if center_gap <= max(word["h"], reference["h"]) * .3 and min(word["h"], reference["h"]) >= max(word["h"], reference["h"]) * .5:
                matches.append((center_gap, index))
        if matches:
            rows[min(matches)[1]].append(word)
        else:
            rows.append([word])
    result = []
    for row in rows:
        groups = [[]]
        for word in sorted(row, key=lambda item: item["x"]):
            if groups[-1]:
                previous = groups[-1][-1]
                gap = word["x"] - previous["x"] - previous["w"]
                separated = gap > min(word["h"], previous["h"]) * 1.75
                if gap > 2 and not separated:
                    left, right = math.ceil(previous["x"] + previous["w"]), math.floor(word["x"])
                    top = math.ceil(max(previous["y"], word["y"]))
                    bottom = math.floor(min(previous["y"] + previous["h"], word["y"] + word["h"]))
                    strip = pixels[top:bottom, left:right, :3]
                    if strip.size:
                        background, _, _ = _palette(strip)
                        ink = np.max(np.abs(strip.astype(np.int16) - background), axis=2) > 55
                        separated = bool(np.any(ink.mean(axis=0) > .8))
                if separated:
                    groups.append([])
            groups[-1].append(word)
        for group in groups:
            text = " ".join(str(item.get("text", "")).strip() for item in group)
            rects = [_rect(item, width, height) for item in group]
            left, top = min(rect[0] for rect in rects), min(rect[1] for rect in rects)
            right, bottom = max(rect[2] for rect in rects), max(rect[3] for rect in rects)
            # Narrow diagram labels and table cells remain independent. Body
            # rows contain several words even when OCR returns tiny fragments.
            if len(group) < 2 or right - left < width * .22 or len(re.findall(r"[A-Za-z]+", text)) < 4:
                result.extend(group)
                continue
            result.append({"text": text, "x": left, "y": top, "w": right - left, "h": bottom - top,
                           "confidence": float(np.mean([item.get("confidence", 0) for item in group])),
                           "sourceRects": [list(_refine_box(pixels, rect)) for rect in rects]})
    return result


def _merge_lines(blocks: list[dict], words: list[dict], width: int, height: int, pixels: np.ndarray | None = None) -> list[dict]:
    """Join aligned continuation lines without crossing other OCR content."""
    merged, used = [], set()
    for index, first in enumerate(blocks):
        if index in used:
            continue
        group = [first]
        used.add(index)
        while True:
            previous = group[-1]
            if previous["text"].rstrip().endswith((".", "?", "!", ":", "。", "？", "！", "：")):
                break
            candidates = []
            for other_index, other in enumerate(blocks):
                if other_index in used:
                    continue
                continuation = other["text"].lstrip(" \t\"'“‘（(")
                wide_body = first["w"] >= width * .5
                group_text = re.sub(r"^\s*\d+\s*[)）.、]\s*", "", "".join(item["text"] for item in group))
                open_parenthesis = group_text.count("(") + group_text.count("（") > group_text.count(")") + group_text.count("）")
                bracket_continuation = wide_body and open_parenthesis and bool(re.match(r"^[\u3400-\u9fff）)]", continuation))
                if not continuation or not (continuation[0].islower() or bracket_continuation):
                    continue
                # Glyph-tip refinement can overlap neighbours' loose margins;
                # use OCR's original line spacing to identify continuations.
                gap = other["ocrRect"][1] - previous["ocrRect"][3]
                if not (-previous["h"] * .3 <= gap <= previous["h"] * .65):
                    continue
                left_aligned = abs(other["x"] - first["x"]) <= max(12, first["h"] * .4)
                center_aligned = abs(other["x"] + other["w"] / 2 - first["x"] - first["w"] / 2) <= max(12, first["h"] * .5)
                hanging_aligned = wide_body and 0 <= other["x"] - first["x"] <= first["h"] * 1.4
                if other["y"] <= previous["y"] or not (left_aligned or center_aligned or hanging_aligned):
                    continue
                if not .65 <= other["h"] / previous["h"] <= 1.5:
                    continue
                if np.max(np.abs(np.array(first["background"]) - np.array(other["background"]))) > 35:
                    continue
                left = min(block["x"] for block in group + [other])
                top = first["y"]
                right = max(block["x"] + block["w"] for block in group + [other])
                bottom = other["y"] + other["h"]
                if pixels is not None:
                    # Table row rules separate entries even if OCR mistakes a
                    # capital I for lowercase l and suggests a continuation.
                    strip_top = (previous["ocrRect"][1] + previous["ocrRect"][3]) // 2
                    strip_bottom = (other["ocrRect"][1] + other["ocrRect"][3]) // 2
                    strip = pixels[strip_top:strip_bottom, left:right, :3]
                    if strip.size:
                        ink = np.max(np.abs(strip.astype(np.int16) - first["background"]), axis=2) > 55
                        if np.any(ink.mean(axis=1) > .85):
                            continue
                members = {tuple(block["ocrRect"]) for block in group + [other]}
                foreign = False
                for word in words:
                    rect = _rect(word, width, height)
                    if not rect or rect in members:
                        continue
                    cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
                    if left < cx < right and top < cy < bottom:
                        foreign = True
                        break
                if not foreign:
                    candidates.append((other["y"], other_index, other))
            if not candidates:
                break
            _, next_index, next_block = min(candidates, key=lambda item: (item[0], item[1]))
            group.append(next_block)
            used.add(next_index)
        block = dict(first)
        block["sourceRects"] = [rect for item in group for rect in item.get("sourceRects", [[item["x"], item["y"], item["x"] + item["w"], item["y"] + item["h"]]])]
        block["sourceTexts"] = [item["text"] for item in group]
        if len(group) > 1:
            block["id"] = "p_" + hashlib.sha256("|".join(item["id"] for item in group).encode()).hexdigest()[:16]
            block["text"] = " ".join(item["text"] for item in group)
            block["x"] = min(item["x"] for item in group)
            block["y"] = min(item["y"] for item in group)
            block["w"] = max(item["x"] + item["w"] for item in group) - block["x"]
            block["h"] = max(item["y"] + item["h"] for item in group) - block["y"]
            block["protectedTokens"] = _protected_tokens(block["text"])
        merged.append(block)
    return merged


def _partition_boxes(boxes: list[tuple | None], pixels: np.ndarray | None = None) -> list[tuple | None]:
    """Share loose OCR margins between adjacent rows without widening them."""
    result = [list(box) if box else None for box in boxes]
    for i, one in enumerate(boxes):
        if not one:
            continue
        for j in range(i + 1, len(boxes)):
            two = boxes[j]
            if not two:
                continue
            ox = min(one[2], two[2]) - max(one[0], two[0])
            oy = min(one[3], two[3]) - max(one[1], two[1])
            if ox <= 0 or oy <= 0:
                continue
            if oy <= min(one[3] - one[1], two[3] - two[1]) * .5:
                split = (min(one[3], two[3]) + max(one[1], two[1])) // 2
                upper, lower = (i, j) if one[1] < two[1] else (j, i)
                result[upper][3] = min(result[upper][3], split)
                result[lower][1] = max(result[lower][1], split)
            elif pixels is not None and ox >= min(one[2] - one[0], two[2] - two[0]) * .8 and (
                one[1] < two[1] < one[3] < two[3] or two[1] < one[1] < two[3] < one[3]
            ):
                # Loose OCR boxes may include most of the neighbouring line.
                # Split only on a real blank row, never through source glyphs.
                left, right = min(one[0], two[0]), max(one[2], two[2])
                top, bottom = max(one[1], two[1]), min(one[3], two[3])
                sample = pixels[top:bottom, left:right, :3]
                background, _, uniformity = _palette(sample)
                ink = np.max(np.abs(sample.astype(np.int16) - background), axis=2) > 55
                blank = np.flatnonzero(~ink.any(axis=1)) if uniformity >= .35 else []
                if len(blank):
                    split = top + int(min(blank, key=lambda row: abs(row - (bottom - top) / 2)))
                    upper, lower = (i, j) if one[1] < two[1] else (j, i)
                    result[upper][3] = min(result[upper][3], split)
                    result[lower][1] = max(result[lower][1], split)
            elif ox < min(one[2] - one[0], two[2] - two[0]) * .25:
                split = (min(one[2], two[2]) + max(one[0], two[0])) // 2
                left, right = (i, j) if one[0] < two[0] else (j, i)
                result[left][2] = min(result[left][2], split)
                result[right][0] = max(result[right][0], split)
    return [tuple(box) if box else None for box in result]


def _read_layout_words(image: Image.Image) -> list[dict]:
    """Preserve native OCR; recover missing prose at one bounded second scale."""
    words = list(image_text.read_words(image))
    if image.width <= 1440:
        return words
    resized = image.resize((1440, math.ceil(image.height * 1440 / image.width)), Image.Resampling.LANCZOS)
    scale_x, scale_y = image.width / resized.width, image.height / resized.height
    occupied = [box for word in words if (box := _rect(word, image.width, image.height))]
    for word in image_text.read_words(resized):
        if not _candidate(str(word.get("text", ""))) or float(word.get("confidence", 0)) < .75:
            continue
        mapped = dict(word, x=word["x"] * scale_x, y=word["y"] * scale_y,
                      w=word["w"] * scale_x, h=word["h"] * scale_y)
        box = _rect(mapped, image.width, image.height)
        if not box or any(min(box[2], other[2]) > max(box[0], other[0])
                          and min(box[3], other[3]) > max(box[1], other[1]) for other in occupied):
            continue
        words.append(mapped)
        occupied.append(box)
    return words


def extract_layout(image_bytes: bytes) -> dict:
    """Run real RapidOCR and return stable source-coordinate translation blocks."""
    image = _open_image(image_bytes)
    pixels = np.asarray(image)
    blocks = []
    seen = set()
    words = _merge_word_fragments(_read_layout_words(image), pixels)
    for word in words:
        text = str(word.get("text", "")).strip()
        box = _rect(word, image.width, image.height)
        if not box or not _candidate(text):
            continue
        identity = f"{box[0]}:{box[1]}:{box[2]}:{box[3]}:{text}"
        if identity in seen:
            continue
        seen.add(identity)
        x, y, right, bottom = _refine_box(pixels, box)
        background, foreground, uniformity = _palette(pixels[y:bottom, x:right])
        blocks.append({"id": "b_" + hashlib.sha256(identity.encode()).hexdigest()[:16],
                       "text": text, "x": x, "y": y, "w": right - x, "h": bottom - y,
                       "ocrRect": list(box),
                       "confidence": round(float(word.get("confidence", 0)), 5),
                       "background": background, "foreground": foreground,
                       "backgroundUniformity": round(uniformity, 4),
                       "protectedTokens": _protected_tokens(text)})
        if "sourceRects" in word:
            blocks[-1]["sourceRects"] = word["sourceRects"]
    blocks.sort(key=lambda block: (block["y"], block["x"], block["text"]))
    blocks = _merge_lines(blocks, words, image.width, image.height, pixels)
    return {"version": VERSION, "width": image.width, "height": image.height,
            "sourceSha256": hashlib.sha256(image_bytes).hexdigest(), "blocks": blocks}


@lru_cache(maxsize=1)
def _font_path() -> str:
    candidates = [os.environ.get("TINGJI_CJK_FONT", ""),
                  "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
                  "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                  "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
                  "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
                  "/System/Library/Fonts/PingFang.ttc"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise ValueError("缺少中文字体，请安装 Noto Sans CJK 或设置 TINGJI_CJK_FONT。")


@lru_cache(maxsize=128)
def _font(size: int):
    return ImageFont.truetype(_font_path(), size=size)


def _wrap(text: str, font, width: int) -> list[str] | None:
    lines = []
    for paragraph in text.split("\n"):
        line_tokens = []
        # Measurements are atomic, including signed decimals and scientific
        # notation; Latin words stay intact and CJK characters remain breakable.
        tokens = re.findall(
            r"[+\-−±<>≤≥]?(?:\d+(?:[.,]\d+)*|[.,]\d+)(?:[eE][+\-−]?\d+)?"
            r"(?:[ \t]*(?:[°℃℉%‰]|[A-Za-z]+[0-9⁰¹²³⁴⁵⁶⁷⁸⁹]*))?"
            r"|[A-Za-z]+[0-9⁰¹²³⁴⁵⁶⁷⁸⁹]*|.", paragraph)
        for token in tokens:
            trial = "".join(line_tokens) + token
            box = font.getbbox(trial)
            if box[2] - box[0] <= width:
                line_tokens.append(token)
                continue
            if not line_tokens:
                return None
            if token in "，。；：！？、）】》〉,.!?;:%" or line_tokens[-1] in "（【《〈(":
                if len(line_tokens) == 1:
                    return None
                lines.append("".join(line_tokens[:-1]))
                line_tokens = [line_tokens[-1], token]
            else:
                lines.append("".join(line_tokens))
                line_tokens = [token]
            box = font.getbbox("".join(line_tokens))
            if box[2] - box[0] > width:
                return None
        lines.append("".join(line_tokens))
    return lines


def _fit(text: str, width: int, height: int, maximum: int | None = None):
    maximum = min(512, maximum or 512, max(MIN_FONT_SIZE, round(height * 1.4)))
    for size in range(maximum, MIN_FONT_SIZE - 1, -1):
        font = _font(size)
        lines = _wrap(text, font, width)
        if lines is None:
            continue
        boxes = [font.getbbox(line or "中") for line in lines]
        spacing = max(1, round(size * .12))
        ink_height = sum(box[3] - box[1] for box in boxes) + spacing * (len(lines) - 1)
        if ink_height <= height:
            return font, size, lines, boxes, spacing, ink_height
    return None


def _text_mask(pixels: np.ndarray, background: list[int]) -> tuple[np.ndarray, np.ndarray]:
    """Erase glyph pixels only; leave long rule/leader components untouched."""
    import cv2
    distance = np.max(np.abs(pixels[..., :3].astype(np.int16) - np.array(background)), axis=2)
    mask = (distance > 3).astype(np.uint8)
    height, width = mask.shape
    # Opening finds a rule even when a letter descender touches it and both
    # belong to one connected component.
    rule_width = max(3, min(width, math.ceil(max(height * 2, width * .5))))
    solid = (distance > 35).astype(np.uint8)
    graphics = cv2.morphologyEx(solid, cv2.MORPH_OPEN, np.ones((1, rule_width), dtype=np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(solid, connectivity=8)
    for index in range(1, count):
        x, y, w, h, _ = stats[index]
        if (w > max(height * 2, width * .5) and h <= max(2, height * .12)) or (
            h >= height * .9 and w <= 2 and (x <= 1 or x + w >= width - 1)
        ):
            graphics[labels == index] = 1
    if height <= 20:
        # At small raster sizes, adjacent letters can share a dark middle row.
        # Unlike a rule, these bands have letter ink above/below almost every
        # column. Keep edge rules and sparse arrow/leader segments protected.
        count, labels, stats, _ = cv2.connectedComponentsWithStats(graphics, connectivity=8)
        for index in range(1, count):
            x, y, w, h, _ = stats[index]
            if h > max(2, round(height * .25)) or w < 6 or not height * .2 <= y + h / 2 <= height * .8:
                continue
            surroundings = solid[:, x:x + w].copy()
            surroundings[y:y + h] = 0
            if np.mean(surroundings.sum(axis=0) >= 2) >= .8:
                graphics[labels == index] = 0
    mask[graphics > 0] = 0
    return mask.astype(bool), graphics.astype(bool)


def _background_surface(pixels: np.ndarray, background: list[int], uniformity: float):
    if uniformity >= .35:
        return np.broadcast_to(np.array(background, dtype=np.uint8), pixels[..., :3].shape)
    # Smooth slide gradients are estimated column by column from the background
    # side of the pixel distribution. Photos fail the residual check and remain
    # untouched rather than being filled with one guessed colour.
    candidates = []
    for quantile in (.1, .9):
        sample = np.quantile(pixels[..., :3], quantile, axis=0)
        positions = np.arange(sample.shape[0])
        fit = np.stack([np.polyval(np.polyfit(positions, sample[:, channel], 1), positions) for channel in range(3)], axis=1)
        candidates.append((float(np.quantile(np.abs(fit - sample), .9)), fit))
    error, fit = min(candidates, key=lambda item: item[0])
    if error > 12:
        return None
    return np.broadcast_to(np.clip(np.round(fit), 0, 255).astype(np.uint8)[None, ...], pixels[..., :3].shape)


def _missing_tokens(text: str, tokens: list[str]) -> list[str]:
    # Compare numeric tokens separately: 10 must not be accepted as part of 100.
    numbers = Counter(_NUMBER.findall(text))
    missing = []
    for token, count in Counter(tokens).items():
        available = numbers[token] if _NUMBER.fullmatch(token) else text.count(token)
        if available < count:
            missing.append(token)
    return missing


def render_translation(image_bytes: bytes, layout: dict, translations: dict[str, str]) -> tuple[bytes, dict]:
    """Return PNG plus per-block fit/pixel evidence, without cropping any text.

Only non-empty translations are applied. Missing, unsafe or unfit blocks retain
their original pixels. ``complete`` requires every candidate to be rendered (or
already equal to its source), so partial pages cannot be mistaken for complete.
"""
    image = _open_image(image_bytes)
    if layout.get("version") != VERSION or (layout.get("width"), layout.get("height")) != image.size:
        raise ValueError("课件文字布局与原图尺寸不一致，请重新识别。")
    if layout.get("sourceSha256") and layout["sourceSha256"] != hashlib.sha256(image_bytes).hexdigest():
        raise ValueError("课件文字布局来自另一张原图，请重新识别。")
    original = np.asarray(image).copy()
    result = original.copy()
    allowed = np.zeros((image.height, image.width), dtype=bool)
    occupied = np.zeros_like(allowed)
    reports = []
    blocks = layout.get("blocks", [])
    boxes = [_rect(block, image.width, image.height) for block in blocks]
    source_regions = []
    for index, (block, box) in enumerate(zip(blocks, boxes)):
        regions = block.get("sourceRects", [list(box)] if box else [])
        if box and ("sourceRects" not in block or (len(regions) == 1 and box[3] - box[1] <= 20)):
            boxes[index] = _refine_box(original, box)
            regions = [list(boxes[index])]
        source_regions.append(regions)
    boxes = _partition_boxes(boxes, original)
    conflicting = set()
    for i, one in enumerate(boxes):
        for j in range(i + 1, len(boxes)):
            two = boxes[j]
            if one and two and min(one[2], two[2]) > max(one[0], two[0]) and min(one[3], two[3]) > max(one[1], two[1]):
                conflicting.update((i, j))
    for index, (block, box, regions) in enumerate(zip(blocks, boxes, source_regions)):
        report = {"id": block.get("id"), "rect": list(box) if box else None,
                  "fontSize": None, "overflow": False, "rendered": False,
                  "changedPixels": 0, "erasedPixels": 0}
        reports.append(report)
        if not box or box[2] <= box[0] or box[3] <= box[1] or not _candidate(str(block.get("text", ""))):
            report["reason"] = "not_translatable"
            continue
        x, y, right, bottom = box
        raw = translations.get(block["id"])
        if not isinstance(raw, str) or not raw.strip():
            report["reason"] = "empty_translation"
            continue
        text = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
        # Noto CJK lacks decorative dingbat/PUA bullets found in PDF text layers.
        # Keep a standard bullet; private-use glyphs contain no portable text.
        text = re.sub(r"[\ue000-\uf8ff]", "", text.replace("➢", "•")).strip()
        if not text:
            report["reason"] = "empty_translation"
            continue
        if text == block["text"]:
            report.update(reason="unchanged", rendered=True, renderedText=text)
            continue
        missing = _missing_tokens(text, _protected_tokens(block["text"]))
        if missing:
            report.update(reason="protected_text_missing", missingTokens=missing)
            continue
        if index in conflicting or occupied[y:bottom, x:right].any():
            report["reason"] = "overlapping_block"
            continue
        source = original[y:bottom, x:right]
        background, foreground, uniformity = _palette(source)
        background_surface = _background_surface(source, background, uniformity)
        if background_surface is None:
            report["reason"] = "complex_background"
            continue
        distance = np.max(np.abs(source[..., :3].astype(np.int16) - background_surface), axis=2)
        ink = source[..., :3][distance > 55]
        if len(ink):
            foreground = np.median(ink[distance[distance > 55] >= np.quantile(distance[distance > 55], .8)], axis=0).astype(np.uint8).tolist()
        width, height = right - x, bottom - y
        padding = 1 if min(width, height) >= 8 else 0
        erase, graphics = _text_mask(source, background_surface)
        source_mask = np.zeros((height, width), dtype=bool)
        for sx, sy, sr, sb in regions:
            source_mask[max(0, sy - y):max(0, min(height, sb - y)),
                        max(0, sx - x):max(0, min(width, sr - x))] = True
        erase &= source_mask
        # Table borders occasionally fall inside an OCR rectangle. Reserve their
        # rows before fitting rather than painting across them or widening a box.
        fit_top, fit_bottom = 0, height
        for row in np.flatnonzero(graphics.sum(axis=1) > width * .5):
            if row < height * .25:
                fit_top = max(fit_top, int(row) + 1)
            elif row > height * .75:
                fit_bottom = min(fit_bottom, int(row))
        fit_height = fit_bottom - fit_top
        # A bounded input also prevents a malformed provider reply from creating
        # an unbounded font-fitting loop. The full reply is never shortened.
        source_heights = [rect[3] - rect[1] for rect in block.get("sourceRects", [list(box)])]
        font_limit = round(float(np.median(source_heights)) * (1.05 if len(source_heights) > 1 else 1.4))
        fitted = _fit(text, width - padding * 2, fit_height - padding * 2, font_limit) if len(text) <= 8000 else None
        if fitted is None:
            report.update(reason="text_does_not_fit", overflow=True)
            continue
        font, size, lines, boxes, spacing, ink_height = fitted
        glyphs = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(glyphs)
        fragmented_rows = len(source_heights) > len(block.get("sourceTexts", source_heights))
        top = fit_top + padding + (0 if fragmented_rows else (fit_height - padding * 2 - ink_height) // 2)
        for line, bounds in zip(lines, boxes):
            draw.text((padding - bounds[0], top - bounds[1]), line, font=font, fill=255)
            top += bounds[3] - bounds[1] + spacing
        glyph_array = np.asarray(glyphs)
        glyph_box = glyphs.getbbox()
        # All drawing is local to this exact OCR rectangle. No surrounding
        # diagram, formula, margin, or neighbouring text can be erased.
        if np.any(graphics & (glyph_array > 0)):
            report["reason"] = "graphic_overlap"
            continue
        patch = source.copy()
        patch[..., :3][erase] = background_surface[erase]
        alpha = glyph_array.astype(np.float32)[..., None] / 255
        patch[..., :3] = np.round(patch[..., :3] * (1 - alpha) + np.array(foreground) * alpha).astype(np.uint8)
        if patch.shape[2] == 4:
            patch[..., 3][erase | (glyph_array > 0)] = 255
        result[y:bottom, x:right] = patch
        allowed[y:bottom, x:right] = True
        occupied[y:bottom, x:right] = True
        report.update(rendered=True, renderedText=text, lines=lines, fontSize=size,
                      drawRect=[x + glyph_box[0], y + glyph_box[1], x + glyph_box[2], y + glyph_box[3]] if glyph_box else None,
                      erasedPixels=int(erase.sum()), changedPixels=int(np.any(patch != source, axis=2).sum()),
                      reason="rendered")
    changed = np.any(result != original, axis=2)
    metadata = {"version": VERSION, "width": image.width, "height": image.height,
                "blocks": reports, "complete": all(report["rendered"] for report in reports),
                "renderedBlocks": sum(report["rendered"] for report in reports),
                "changedPixels": int(changed.sum()), "changedOutsideBoxes": int((changed & ~allowed).sum())}
    buffer = io.BytesIO()
    Image.fromarray(result).save(buffer, format="PNG")
    return buffer.getvalue(), metadata
