"""Account-scoped syllabuses, kept with their original source and read as context."""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from urllib.parse import quote
from defusedxml import ElementTree

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_csrf, require_user
from .database import get_db
from .logic import course_json
from .models import Course, CourseMaterial, User
from .syllabus import ensure_material_memory, syllabus_status
from .syllabus_models import MaterialMemory

MAX_BYTES = 3 * 1024 * 1024
MAX_CHARS = 60000
router = APIRouter(prefix="/api", tags=["course-materials"])


class MaterialBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(default="课程大纲.txt", min_length=1, max_length=240)
    contentBase64: str = Field(default="", max_length=4 * ((MAX_BYTES + 2) // 3))
    text: str = Field(default="", max_length=MAX_CHARS)
    courseId: str | None = Field(default=None, max_length=36)


def _ocr(image) -> str:
    from .image_text import read_words
    return "\n".join(word["text"] for word in read_words(image))


def extract_material(filename: str, raw: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    try:
        if suffix in {".txt", ".md"}:
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("gb18030")
        elif suffix == ".docx":
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                entry = archive.getinfo("word/document.xml")
                if entry.file_size > 4 * 1024 * 1024:
                    raise ValueError("大纲文字过多，请拆分后添加。")
                root = ElementTree.fromstring(archive.read(entry))
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            text = "\n".join("".join(p.itertext()) for p in root.findall(".//w:p", ns))
        elif suffix == ".pdf":
            import pypdfium2 as pdfium
            parts = []
            with pdfium.PdfDocument(raw) as pdf:
                if len(pdf) > 30:
                    raise ValueError("大纲 PDF 请控制在 30 页内。")
                scanned = 0
                for index in range(len(pdf)):
                    page = pdf[index]
                    textpage = page.get_textpage()
                    part = textpage.get_text_range()
                    textpage.close()
                    if len(part.strip()) < 20:
                        scanned += 1
                        if scanned > 10:
                            raise ValueError("扫描大纲请控制在 10 页内。")
                        bitmap = page.render(scale=min(2, 2400 / max(page.get_size())))
                        part = _ocr(bitmap.to_pil())
                        bitmap.close()
                    page.close()
                    parts.append(part)
            text = "\n\n".join(parts)
        elif suffix in {".png", ".jpg", ".jpeg", ".webp"}:
            from PIL import Image
            with Image.open(io.BytesIO(raw)) as image:
                text = _ocr(image)
        else:
            raise ValueError("请选择 PDF、DOCX、TXT、Markdown 或图片大纲。")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("大纲文件无法读取，请检查文件或粘贴大纲文字。") from exc
    text = text.replace("\x00", "").strip()
    if not text:
        raise ValueError("没有读到大纲文字，请粘贴大纲内容后保存。")
    if len(text) > MAX_CHARS:
        raise ValueError("大纲文字超过 6 万字，请拆分后添加。")
    return text


def _resolve_course(db, user, body, content):
    if body.courseId:
        course = db.scalar(select(Course).where(Course.id == body.courseId, Course.user_id == user.id))
        if not course:
            raise HTTPException(404, "找不到这门课程。")
        return course
    courses = list(db.scalars(select(Course).where(Course.user_id == user.id)))
    # Identity comes from the title area, never from a prerequisite list deep in the syllabus.
    title_area = content[:1800]
    labelled = re.search(r"(?:课程(?:中文)?名称|Course\s*(?:Title|Name))\s*[:：]?\s*\n?\s*([^\n\r\t]{2,120})", title_area, re.I)
    name = labelled.group(1).strip(" :：\t") if labelled else ""
    if name:
        name = re.split(r"\s{2,}|\s+(?:课程代码|Course Code|学分|Credits)\b", name, maxsplit=1, flags=re.I)[0].strip()
    matches = [c for c in courses if c.name.casefold() == name.casefold()] if name else []
    if not name:
        compact = re.sub(r"\s+", "", title_area + "\n" + body.filename).casefold()
        matches = [c for c in courses if re.sub(r"\s+", "", c.name).casefold() in compact]
        # A longer exact course name wins over its prefix (e.g. the lab course).
        matches = [c for c in matches if not any(c.name != other.name and c.name in other.name for other in matches)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError("大纲对应多门课程，请在对应课程中添加。")
    if not name:
        stem = Path(body.filename).stem
        candidate = re.sub(r"(?:课程|教学)?大纲|syllabus|course outline", "", stem, flags=re.I).strip(" _-—（）()")
        if candidate != stem and 2 <= len(candidate) <= 120:
            name = candidate
    if not name or name in {"课程大纲", "教学大纲", "中文", "英文"}:
        raise ValueError("大纲中没有找到课程名称，请在对应课程中添加。")
    from .timetable import course_hotwords
    from .timetable_api import ensure_appearance
    course = Course(user_id=user.id, name=name, hotwords=course_hotwords(name))
    db.add(course)
    db.flush()
    ensure_appearance(db, course)
    return course


def material_json(material, memory=None):
    return {"id": material.id, "filename": material.filename, "content": material.content,
            "createdAt": material.created_at.isoformat(),
            "sourceUrl": memory.source_url if memory else "",
            "sourceKind": memory.source_kind if memory else "upload",
            "sourceUpdatedAt": memory.source_updated_at if memory else "",
            "memorySummary": memory.summary if memory else "",
            "downloadUrl": f"/api/course-materials/{material.id}/original",
            "viewUrl": f"/api/course-materials/{material.id}/view" if material.original_content.startswith(b"%PDF-") else ""}


def course_material_context(db: Session, course_id: str | None, user_id: str) -> str:
    if not course_id:
        return ""
    rows = db.execute(select(CourseMaterial.filename, MaterialMemory.summary, MaterialMemory.source_url)
                      .join(MaterialMemory, MaterialMemory.material_id == CourseMaterial.id)
                      .where(CourseMaterial.user_id == user_id, CourseMaterial.course_id == course_id)
                      .order_by(CourseMaterial.created_at.desc()).limit(6))
    payload, remaining = [], 12000
    for filename, summary, source_url in rows:
        excerpt = summary[:remaining]
        if not excerpt:
            break
        payload.append({"filename": filename, "content": excerpt, "sourceUrl": source_url})
        remaining -= len(excerpt)
    return json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e") if payload else ""


@router.post("/course-materials", dependencies=[Depends(require_csrf)])
def add_material(body: MaterialBody, user: User = Depends(require_user), db: Session = Depends(get_db)):
    if bool(body.contentBase64) == bool(body.text.strip()):
        raise ValueError("请选择一份大纲文件，或粘贴大纲文字。")
    filename = Path(body.filename.replace("\\", "/")).name
    if body.contentBase64:
        try:
            raw = base64.b64decode(body.contentBase64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("大纲文件内容无效。") from exc
        if not raw or len(raw) > MAX_BYTES:
            raise ValueError("大纲文件请控制在 3 MB 内。")
        content = extract_material(filename, raw)
    else:
        content = body.text.strip()
        raw = content.encode("utf-8")
        filename = "课程大纲.txt"
    # Serialize imports for this account, including course creation and retry deduplication.
    db.scalar(select(User.id).where(User.id == user.id).with_for_update())
    course = _resolve_course(db, user, body, content)
    digest = hashlib.sha256(raw).hexdigest()
    material = db.scalar(select(CourseMaterial).where(CourseMaterial.user_id == user.id,
                         CourseMaterial.course_id == course.id, CourseMaterial.sha256 == digest))
    if material is None:
        material = CourseMaterial(user_id=user.id, course_id=course.id, filename=filename,
                                  content=content, original_content=raw, sha256=digest)
        db.add(material)
        db.flush()
    memory = ensure_material_memory(db, material)
    from .syllabus import enqueue_syllabus
    from .slide_matching import enqueue_user_matching
    enqueue_syllabus(db, course)
    enqueue_user_matching(db, user.id)
    db.commit()
    return {"course": course_json(course), "material": material_json(material, memory)}


@router.get("/courses/{course_id}/materials")
def list_materials(course_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    if not db.scalar(select(Course.id).where(Course.id == course_id, Course.user_id == user.id)):
        raise HTTPException(404, "找不到这门课程。")
    rows = list(db.scalars(select(CourseMaterial).where(CourseMaterial.user_id == user.id,
                          CourseMaterial.course_id == course_id).order_by(CourseMaterial.created_at.desc())))
    values = [material_json(row, ensure_material_memory(db, row)) for row in rows]
    db.commit()
    return {"materials": values, "discovery": syllabus_status(db, course_id)}


@router.get("/course-materials/{material_id}/original")
def original_material(material_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    row = db.scalar(select(CourseMaterial).where(CourseMaterial.id == material_id, CourseMaterial.user_id == user.id))
    if row is None:
        raise HTTPException(404, "找不到这份大纲。")
    return Response(row.original_content, media_type="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(row.filename, safe='')}"})


@router.get("/course-materials/{material_id}/view")
def view_material(material_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    row = db.scalar(select(CourseMaterial).where(CourseMaterial.id == material_id, CourseMaterial.user_id == user.id))
    if row is None:
        raise HTTPException(404, "找不到这份大纲。")
    if not row.original_content.startswith(b"%PDF-"):
        raise HTTPException(415, "这份大纲不是 PDF，请下载原文件。")
    return Response(row.original_content, media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(row.filename, safe='')}",
                             "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
