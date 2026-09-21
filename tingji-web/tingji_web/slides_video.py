"""Replace embedded PowerPoint videos with readable stills in a disposable copy."""
from __future__ import annotations

import io
import json
import math
import posixpath
import shutil
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from defusedxml.ElementTree import fromstring
from PIL import Image, ImageStat

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
P14 = "http://schemas.microsoft.com/office/powerpoint/2010/main"
for _prefix, _uri in (("p", P), ("a", A), ("r", R), ("p14", P14)):
    ET.register_namespace(_prefix, _uri)


def _part(slide: str, target: str) -> str:
    target = unquote(target)
    value = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(posixpath.dirname(slide), target))
    if value.startswith("../") or value.startswith("/"):
        raise ValueError("课件中的视频路径不完整，请重新嵌入视频后上传。")
    return value


def _package_xml(root: ET.Element, namespace: str) -> bytes:
    # LibreOffice's package loader requires the default namespace on these two OPC documents.
    for element in root.iter():
        if element.tag.startswith("{" + namespace + "}"):
            element.tag = element.tag.split("}", 1)[1]
    root.set("xmlns", namespace)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _frame(content: bytes, suffix: str, folder: Path, index: int) -> bytes:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("视频截图工具暂时不可用，上传的 PPT 已保留。")
    source = folder / f"embedded-video-{index}{suffix}"
    source.write_bytes(content)
    try:
        probe = subprocess.run([ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe",
                                "-show_entries", "format=duration", "-of", "json", str(source)],
                               capture_output=True, timeout=20)
        duration = float(json.loads(probe.stdout).get("format", {}).get("duration", 0))
        if probe.returncode or not math.isfinite(duration) or duration <= 0:
            raise ValueError
        candidates = []
        # Sample across the clip so an opening black frame or title card is not the only choice.
        for position in (0.15, 0.5, 0.8):
            output = folder / f"video-{index}-{position}.jpg"
            result = subprocess.run(
                [ffmpeg, "-nostdin", "-v", "error", "-threads", "1", "-protocol_whitelist", "file,pipe",
                 "-ss", str(duration * position), "-i", str(source), "-an", "-sn", "-dn",
                 "-vf", "scale=1280:1280:force_original_aspect_ratio=decrease,thumbnail=12",
                 "-frames:v", "1", "-q:v", "2", "-y", str(output)], capture_output=True, timeout=35)
            if result.returncode or not output.is_file():
                continue
            data = output.read_bytes()
            with Image.open(io.BytesIO(data)) as image:
                gray = image.convert("L")
                stats = ImageStat.Stat(gray)
                # Favor detailed, nonblank frames; this does not invent a description of the clip.
                score = gray.entropy() + min(stats.stddev[0], 50) / 25
                candidates.append((score, data))
        if not candidates:
            raise ValueError
        return max(candidates, key=lambda pair: pair[0])[1]
    except (ValueError, KeyError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        raise RuntimeError("有视频无法截图，上传的 PPT 已保留。请将该视频替换为图片后重传。") from None
    finally:
        source.unlink(missing_ok=True)


def replace_video_frames(content: bytes, folder: Path) -> tuple[bytes, int]:
    """Read a validated PPTX. Never fetch linked videos or modify the user's file."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        parts = {item.filename: archive.read(item) for item in archive.infolist()}
    stills: dict[str, tuple[str, bytes]] = {}
    video_parts: set[str] = set()
    count = 0
    for name in list(parts):
        if not name.startswith("ppt/slides/") or not name.endswith(".xml") or "/_rels/" in name:
            continue
        slide = fromstring(parts[name])
        rel_name = posixpath.dirname(name) + "/_rels/" + posixpath.basename(name) + ".rels"
        rel_root = fromstring(parts.get(rel_name, f'<Relationships xmlns="{REL}"/>'))
        rels = {rel.get("Id"): rel for rel in rel_root}
        changed = False
        removed_ids = set()
        for picture in slide.iter(f"{{{P}}}pic"):
            video = picture.find(f".//{{{A}}}videoFile")
            if video is None:
                continue
            video_rel = rels.get(video.get(f"{{{R}}}link"))
            media = picture.find(f".//{{{P14}}}media")
            embedded = rels.get(media.get(f"{{{R}}}embed")) if media is not None else None
            relation = embedded if embedded is not None and embedded.get("TargetMode") != "External" else video_rel
            if relation is None or relation.get("TargetMode") == "External":
                raise ValueError("课件含链接视频，请先将视频嵌入 PPT，或替换为图片后上传。")
            target = _part(name, relation.get("Target", ""))
            if target not in parts:
                raise ValueError("课件中的视频文件缺失，请重新嵌入后上传。")
            if target not in stills:
                frame = _frame(parts[target], Path(target).suffix, folder, len(stills) + 1)
                frame_name = f"ppt/media/tingji-video-frame-{len(stills) + 1}.jpg"
                while frame_name in parts:
                    frame_name = frame_name.replace(".jpg", "-new.jpg")
                stills[target] = frame_name, frame
                parts[frame_name] = frame
            frame_name, _ = stills[target]
            new_id = "rIdTingjiVideo" + str(count + 1)
            while new_id in rels:
                new_id += "x"
            rel_root.append(ET.Element(f"{{{REL}}}Relationship", {
                "Id": new_id, "Type": R + "/image", "Target": "../media/" + posixpath.basename(frame_name)}))
            blip = picture.find(f".//{{{A}}}blip")
            if blip is None:
                raise ValueError("视频画面位置不完整，请将视频替换为图片后上传。")
            blip.attrib.pop(f"{{{R}}}link", None)
            blip.set(f"{{{R}}}embed", new_id)
            for parent in picture.iter():
                for child in list(parent):
                    if child.tag in {f"{{{A}}}videoFile", f"{{{P14}}}media"} or (
                        child.tag == f"{{{A}}}hlinkClick" and child.get("action", "").startswith("ppaction://media")):
                        parent.remove(child)
            shape = picture.find(f".//{{{P}}}cNvPr")
            if shape is not None:
                removed_ids.add(shape.get("id"))
            for old in (video_rel, embedded):
                if old is not None and old in list(rel_root):
                    if old.get("TargetMode") != "External":
                        video_parts.add(_part(name, old.get("Target", "")))
                    rel_root.remove(old)
            count += 1
            changed = True
        if changed:
            # Static PDF has no playback; remove video timing trees which refer to the old media objects.
            timing = slide.find(f"{{{P}}}timing")
            if timing is not None and any(e.get("spid") in removed_ids for e in timing.iter()):
                slide.remove(timing)
            parts[name] = ET.tostring(slide, encoding="utf-8", xml_declaration=True)
            parts[rel_name] = _package_xml(rel_root, REL)
    if not count:
        return content, 0
    # Only remove a video part after all remaining package relationships release it.
    referenced = set()
    for name, data in parts.items():
        if not name.endswith(".rels"):
            continue
        owner = name.replace("/_rels/", "/").removesuffix(".rels")
        for rel in fromstring(data):
            if rel.get("TargetMode") != "External":
                referenced.add(_part(owner, rel.get("Target", "")))
    removed_parts = video_parts - referenced
    for target in removed_parts:
        parts.pop(target, None)
    content_types = fromstring(parts["[Content_Types].xml"])
    if not any(x.get("Extension") == "jpg" for x in content_types):
        content_types.append(ET.Element(f"{{{CT}}}Default", {"Extension": "jpg", "ContentType": "image/jpeg"}))
    for element in list(content_types):
        if element.get("PartName", "").lstrip("/") in removed_parts:
            content_types.remove(element)
    parts["[Content_Types].xml"] = _package_xml(content_types, CT)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return output.getvalue(), count
