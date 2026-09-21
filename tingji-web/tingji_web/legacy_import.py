"""Copy legacy local notes without changing their database or calling providers.

``scan_legacy`` is read-only. ``import_legacy`` adds rows to a caller-owned
transaction and never commits. Cloud import carries text only; local import
preserves the complete legacy metadata in a separate archive row.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def text_hash(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").replace("\r", "\n").strip().encode("utf-8")).hexdigest()


def _date(value: Any, fallback=None):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except (TypeError, ValueError):
        return fallback


def _stable(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "tingji-legacy:" + ":".join(parts)))


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


@dataclass
class LegacySource:
    root: Path
    namespace: str
    notes: list[dict]
    schedule: dict
    originals: dict[str, str] = field(default_factory=dict)
    original_files: dict[str, dict] = field(default_factory=dict)
    versions: dict[str, list[dict]] = field(default_factory=dict)
    prompt_artifacts: dict[str, str] = field(default_factory=dict)
    prompt: str = ""

    def transcript(self, note: dict) -> str:
        return self.originals.get(note["id"], str(note.get("transcript") or ""))

    def report(self, mode="transcripts_only") -> dict:
        candidates, duplicates, omitted = self.candidates(mode)
        return {
            "mode": mode, "legacyNoteCount": len(self.notes), "selectedNoteCount": len(candidates),
            "omittedNoteCount": omitted, "duplicateTranscriptCount": duplicates,
            "coveredSourceCount": len(self.covered_sources()) if mode == "transcripts_only" else 0,
            "courseCount": len(self.schedule.get("courses", [])),
            "originalFileCount": len(self.original_files),
            "versionCount": sum(len(items) for items in self.versions.values()),
            "transcriptCharacters": sum(len(self.transcript(n)) for n in candidates),
            "notes": [{"sourceId": n["id"], "transcriptCharacters": len(self.transcript(n)),
                       "transcriptSha256": text_hash(self.transcript(n)) if self.transcript(n).strip() else None,
                       "hasSummary": bool(n.get("summary")), "trashed": bool(n.get("trashedAt")),
                       "partialTranscript": self.partial(n),
                       "hasLocalAudio": bool(self.audio_key(n))} for n in candidates],
        }

    def candidates(self, mode):
        if mode not in {"transcripts_only", "full"}:
            raise ValueError("mode must be transcripts_only or full")
        if mode == "full":
            return list(self.notes), 0, 0
        selected, seen, duplicates, omitted = [], set(), 0, 0
        covered = self.covered_sources()
        for note in sorted(self.notes, key=lambda n: (n["id"] not in self.original_files, n["id"])):
            text = self.transcript(note)
            if note.get("trashedAt") or note.get("isDemo") or not text.strip():
                omitted += 1
                continue
            if note["id"] in covered:
                continue
            digest = text_hash(text)
            if digest in seen:
                duplicates += 1
                continue
            seen.add(digest)
            selected.append(note)
        return selected, duplicates, omitted

    def covered_sources(self):
        by_id = {n["id"]: n for n in self.notes}
        covered = set()
        for note in self.notes:
            if note.get("trashedAt") or note.get("isDemo"):
                continue
            text = self.transcript(note).replace("\r\n", "\n")
            for identifier in note.get("sourceNoteIds", []):
                child = by_id.get(identifier)
                if child:
                    raw = self.transcript(child).replace("\r\n", "\n").strip()
                    if raw and raw in text:
                        covered.add(identifier)
        return covered

    def partial(self, note):
        return bool(self.transcript(note).strip()) and (
            self.original_files.get(note["id"], {}).get("partial", note.get("asrComplete") is False)
            or note.get("partialTranscript") is True)

    def audio_key(self, note):
        name = str(note.get("audioFile") or "")
        if not name or note.get("audioDeletedAt") or Path(name).name != name:
            return None
        base = (self.root / "data" / "audio").resolve()
        path = (base / name).resolve()
        if path.parent != base or not path.is_file():
            return None
        return "legacy/audio/" + name


def scan_legacy(root: str | Path) -> LegacySource:
    """Read the old SQLite database and verified immutable source snapshots."""
    root = Path(root).resolve()
    database = root / "data" / "notes.sqlite3"
    if not database.is_file():
        raise ValueError("找不到旧版笔记数据库。")
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        notes = [json.loads(row[0]) for row in db.execute("SELECT body FROM notes ORDER BY id")]
        vault = db.execute("SELECT value FROM settings WHERE key='_vaultId'").fetchone()
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        schedule_row = db.execute("SELECT body FROM schedule WHERE id='active'").fetchone() if "schedule" in tables else None
    seed = root / "config" / "course-schedule.json"
    schedule = json.loads(schedule_row[0]) if schedule_row else (_json(seed) if seed.is_file() else {})
    source = LegacySource(root, str(vault[0]) if vault else hashlib.sha256(str(database).encode()).hexdigest(), notes, schedule)
    ids = {n["id"] for n in notes}
    for manifest in sorted((root / "data" / "prompt-lab").glob("*/source.json")):
        data = _json(manifest)
        note_id = data.get("noteId")
        if note_id not in ids:
            continue
        path = manifest.parent / "original.txt"
        if not path.is_file() or path.resolve().parent != manifest.parent.resolve():
            raise ValueError("原文快照路径无效。")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != data.get("fileSha256", data.get("sha256")):
            raise ValueError("原文快照的 SHA256 校验失败，未导入任何数据。")
        text = raw.decode("utf-8")
        if data.get("textNewlines") == "lf":
            text = text.replace("\r\n", "\n")
        if note_id in source.originals and text_hash(source.originals[note_id]) != text_hash(text):
            raise ValueError("同一条笔记对应多份不同原文，请先核对来源。")
        source.originals[note_id] = text
        source.original_files[note_id] = {"path": str(path.relative_to(root)), "fileSha256": digest,
                                          "transcriptSha256": text_hash(text), "characters": len(text)}
        versions = []
        for result_path in sorted(manifest.parent.glob("*.result.json")):
            result = _json(result_path)
            if result.get("applied") is not True or not str(result.get("summary") or "").strip():
                continue
            if result.get("sourceSha256") and result["sourceSha256"] not in {
                hashlib.sha256(text.encode()).hexdigest(), text_hash(text), data.get("sha256")
            }:
                raise ValueError("整理版本引用的原文与快照不一致。")
            request = result_path.with_name(result_path.name.replace(".result.json", ".request.json"))
            prompt = ""
            if request.is_file():
                prompt = "\n\n".join(str(m.get("content") or "") for m in _json(request).get("messages", []) if m.get("role") == "system")
            versions.append({**result, "prompt_snapshot": prompt})
        source.versions[note_id] = versions
    # Saved provider responses belong to the original recordings, not to the
    # seven prompt experiments. A verified combined original can cover them.
    for response in sorted((root / "data" / "prompt-lab").glob("*/*.asr.json")):
        note_id = response.name.removesuffix(".asr.json")
        if note_id not in ids or note_id in source.originals:
            continue
        data = _json(response)
        result = data.get("result") or {}
        if data.get("state") != "completed" or not isinstance(result, dict):
            continue
        utterances = [u for u in result.get("utterances", []) if isinstance(u, dict) and str(u.get("text") or "").strip()]
        text_file = response.with_name(note_id + ".txt")
        text = text_file.read_text(encoding="utf-8") if text_file.is_file() else ""
        if not text or (utterances and not all(str(u["text"]).strip() in text for u in utterances)):
            text = str(result.get("text") or "").strip()
        if text:
            source.originals[note_id] = text
            source.original_files[note_id] = {"path": str(response.relative_to(root)),
                "fileSha256": hashlib.sha256(response.read_bytes()).hexdigest(), "transcriptSha256": text_hash(text),
                "characters": len(text), "partial": False}
    # A legacy purge cleared the active row; backups can still hold an earlier
    # partial recording. Never restore a deleted/demo note or override a snapshot.
    backup_candidates = {}
    backups = sorted((root / "data" / "backups").glob("*.sqlite3"))
    backups.extend(sorted(p for p in (root / "data").glob("*.sqlite3") if p != database))
    for backup in backups:
        with sqlite3.connect(backup.resolve().as_uri() + "?mode=ro", uri=True) as db:
            if not db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='notes'").fetchone():
                continue
            for row in db.execute("SELECT body FROM notes"):
                old = json.loads(row[0])
                text = str(old.get("transcript") or "")
                if old.get("id") not in ids or not text.strip() or old.get("trashedAt") or old.get("isDemo"):
                    continue
                rank = (old.get("asrComplete") is True, str(old.get("updatedAt") or ""), len(text))
                previous = backup_candidates.get(old["id"])
                if previous is None or rank > previous[0]:
                    backup_candidates[old["id"]] = (rank, old, backup)
    for note in notes:
        if source.transcript(note).strip() or note.get("trashedAt") or note.get("isDemo"):
            continue
        saved = backup_candidates.get(note["id"])
        if saved:
            _, old, path = saved
            text = str(old["transcript"])
            source.originals[note["id"]] = text
            source.original_files[note["id"]] = {"path": str(path.relative_to(root)),
                "transcriptSha256": text_hash(text), "characters": len(text), "partial": old.get("asrComplete") is False}
    prompt = root / "docs" / "note-standard-v5" / "deepseek-system.md"
    if prompt.is_file():
        source.prompt = prompt.read_text(encoding="utf-8-sig").strip()
    source.prompt_artifacts = {str(p.relative_to(root)): p.read_text(encoding="utf-8-sig")
                               for p in sorted((root / "data" / "prompt-lab").glob("*/*.md"))}
    return source


def _archive_model():
    # Kept local-only: cloud transcript imports do not require a schema change.
    from sqlalchemy import JSON, Column, ForeignKey, String, Table
    from .database import Base
    existing = Base.metadata.tables.get("legacy_import_archives")
    if existing is not None:
        return existing
    return Table("legacy_import_archives", Base.metadata,
                 Column("id", String(36), primary_key=True),
                 Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), index=True),
                 Column("payload", JSON))


def _metadata(note):
    # Temporary signed URLs and provider authorization never enter the archive.
    excluded = {"audioUrl", "asrTask", "asrTaskHistory", "fastTask", "liveTask", "groupTask", "groupReceipt"}
    result = {k: v for k, v in note.items() if k not in excluded}
    task = note.get("asrTask") or {}
    result["asrTask"] = {k: task[k] for k in ("requestId", "queryId", "state", "submittedAt", "completedAt") if k in task}
    safe_keys = {"requestId", "queryId", "taskId", "attemptId", "state", "submittedAt", "startedAt",
                 "completedAt", "retryMayCharge", "operationId"}
    for key in ("fastTask", "liveTask"):
        raw = note.get(key) or {}
        if isinstance(raw, dict):
            saved = {k: v for k, v in raw.items() if k in safe_keys}
            if isinstance(raw.get("chunks"), list):
                saved["chunks"] = [{k: v for k, v in c.items() if k in safe_keys} for c in raw["chunks"] if isinstance(c, dict)]
            if saved:
                result[key] = saved
    return result


def _courses(db, user, source, mode, selected):
    from sqlalchemy import select
    from .models import Course
    from .timetable_models import CourseAppearance
    from .timetable import short_name, COLORS
    term = str(source.schedule.get("term") or "")[:80]
    entries = list(source.schedule.get("courses", []))
    if mode == "transcripts_only":
        wanted_ids = {n.get("courseId") for n in selected}
        wanted_names = {n.get("courseName") for n in selected if n.get("courseName")}
        entries = [c for c in entries if c.get("id") in wanted_ids or c.get("name") in wanted_names]
    known = {c.get("name") for c in entries}
    for note in selected:
        name = str(note.get("courseName") or "").strip()
        if name and name not in known:
            entries.append({"name": name, "id": note.get("courseId") or "name:" + name})
            known.add(name)
    mapping = {}
    for entry in entries:
        name = str(entry.get("name") or "").strip()[:120]
        if not name:
            continue
        course = db.scalar(select(Course).where(Course.user_id == user.id, Course.name == name, Course.term == term))
        if course is None:
            teachers = list(dict.fromkeys(str(l.get("teacher") or "") for l in entry.get("lessons", []) if l.get("teacher")))
            course = Course(id=_stable(user.id, source.namespace, "course", str(entry.get("id") or name)),
                            user_id=user.id, name=name, term=term, teacher="、".join(teachers)[:80],
                            hotwords="、".join(entry.get("keywords", [])) if mode == "full" else "", prompt="")
            db.add(course)
            db.flush()
        mapping[str(entry.get("id") or name)] = course
        mapping[name] = course
        if mode == "full" and db.get(CourseAppearance, course.id) is None:
            occupied = list(db.scalars(select(CourseAppearance.short_name).where(CourseAppearance.user_id == user.id)))
            preferred = str(entry.get("shortName") or "")[:12]
            abbreviation = preferred if preferred and preferred not in occupied else short_name(name, occupied)
            color = str(entry.get("color") or "")
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                color = COLORS[len(occupied) % len(COLORS)]
            db.add(CourseAppearance(course_id=course.id, user_id=user.id, short_name=abbreviation, color=color))
            db.flush()
    return mapping


def _timetable(db, user, source, mapping):
    from sqlalchemy import select
    from .timetable_models import TimetableSlot, TimetableState
    # Old periods are individual lessons (1..10); the common UI uses five
    # two-lesson blocks. Full original timing/exception metadata stays archived.
    if db.get(TimetableState, user.id) is None:
        db.add(TimetableState(user_id=user.id, semester_start=source.schedule.get("week1Start"), weeks=20, revision=0))
    cells = {}
    for entry in source.schedule.get("courses", []):
        course = mapping.get(str(entry.get("id") or entry.get("name")))
        if course is None:
            continue
        for lesson in entry.get("lessons", []):
            weekday = lesson.get("weekday")
            if not isinstance(weekday, int) or not 1 <= weekday <= 7:
                continue
            span = lesson.get("weeks") or [1, 20]
            weeks = list(range(max(1, span[0]), min(20, span[1]) + 1))
            if lesson.get("parity"):
                parity = 1 if lesson["parity"] == "odd" else 0
                weeks = [w for w in weeks if w % 2 == parity]
            for period in sorted({(p + 1) // 2 for p in lesson.get("periods", []) if isinstance(p, int) and 1 <= p <= 10}):
                key = (course.id, weekday, period)
                value = cells.setdefault(key, {"weeks": set(), "location": str(lesson.get("location") or "")[:120]})
                value["weeks"].update(weeks)
    for (course_id, weekday, period), value in cells.items():
        existing = db.scalar(select(TimetableSlot).where(TimetableSlot.user_id == user.id,
                             TimetableSlot.course_id == course_id, TimetableSlot.weekday == weekday, TimetableSlot.period == period))
        if existing is None and value["weeks"]:
            db.add(TimetableSlot(id=_stable(user.id, source.namespace, "slot", course_id, str(weekday), str(period)),
                                user_id=user.id, course_id=course_id, weekday=weekday, period=period,
                                weeks=sorted(value["weeks"]), location=value["location"]))


def import_legacy(db, user, source: LegacySource, *, mode="transcripts_only", dry_run=True) -> dict:
    """Add imports to ``db``; caller must commit or roll back explicitly."""
    from sqlalchemy import select
    from .models import Note, NoteVersion
    candidates, _, _ = source.candidates(mode)
    existing = list(db.scalars(select(Note).where(Note.user_id == user.id)))
    ids = {n.id for n in existing}
    hashes = {text_hash(n.transcript) for n in existing if n.transcript.strip()}
    selected, skipped = [], 0
    for note in candidates:
        identifier = _stable(user.id, source.namespace, "note", note["id"])
        digest = text_hash(source.transcript(note))
        if identifier in ids or (mode == "transcripts_only" and digest in hashes):
            skipped += 1
            continue
        selected.append(note)
        if mode == "transcripts_only":
            hashes.add(digest)
    report = {**source.report(mode), "dryRun": dry_run, "importCount": len(selected), "existingSkipped": skipped,
              "targetNoteIds": [_stable(user.id, source.namespace, "note", n["id"]) for n in selected]}
    if dry_run:
        return report
    mapping = _courses(db, user, source, mode, selected)
    for old in selected:
        identifier = _stable(user.id, source.namespace, "note", old["id"])
        text = source.transcript(old)
        course = mapping.get(str(old.get("courseId"))) or mapping.get(str(old.get("courseName")))
        created = _date(old.get("recordedAt")) or _date(old.get("createdAt")) or datetime.now(timezone.utc)
        audio_key = source.audio_key(old) if mode == "full" else None
        if audio_key:
            audio_key = "legacy/" + identifier + "/" + audio_key.removeprefix("legacy/")
        summary = str(old.get("summary") or "") if mode == "full" else ""
        status = "ready" if summary else "transcribed" if text.strip() else "uploaded" if audio_key else "error"
        stage = "原文已导入" if status == "transcribed" else "已整理" if summary else "录音已导入" if audio_key else "原记录没有保留原文或录音"
        if status == "transcribed" and source.partial(old):
            stage = "已导入部分原文，原录音的转写未完成"
        task = old.get("asrTask") or {}
        fast = old.get("fastTask") or {}
        uncertain = ((task.get("requestId") and task.get("state") not in {"completed", "failed", "rejected"})
                     or fast.get("state") in {"submitting", "submit_unknown"}
                     or any(c.get("state") in {"submitting", "submit_unknown"} for c in fast.get("chunks", [])))
        if mode == "full" and uncertain and not text.strip() and not summary:
            status, stage = "uncertain", "原转写请求状态待确认，任务编号已保留"
        note = Note(id=identifier, user_id=user.id, course_id=course.id if course else None,
                    title=str(old.get("title") or "导入笔记")[:160], source_name=str(old.get("sourceName") or "")[:240],
                    transcript=text, summary=summary, status=status, stage=stage, error="",
                    expected_size=int(old.get("fileSize") or 0) if audio_key else 0,
                    object_key=audio_key or "transcript-only/" + user.id + "/" + identifier,
                    duration=old.get("duration"), uploaded_at=_date(old.get("uploadedAt"), created),
                    deleted_at=_date(old.get("trashedAt")) if mode == "full" else None,
                    created_at=created, updated_at=_date(old.get("updatedAt"), created),
                    prompt_snapshot="", model="")
        db.add(note)
        db.flush()
        if mode == "full":
            for index, version in enumerate(source.versions.get(old["id"], [])):
                version_id = _stable(user.id, source.namespace, "version", old["id"], str(version.get("operationId") or index))
                if db.get(NoteVersion, version_id) is None:
                    db.add(NoteVersion(id=version_id, user_id=user.id, note_id=note.id, kind="summary",
                                       summary=version["summary"], prompt_snapshot=version.get("prompt_snapshot", ""),
                                       model=str(version.get("model") or ""), created_at=_date(version.get("createdAt"), created)))
    if mode == "full":
        _timetable(db, user, source, mapping)
        archive = _archive_model()
        archive.create(db.connection(), checkfirst=True)
        archive_id = _stable(user.id, source.namespace, "archive")
        if db.scalar(select(archive.c.id).where(archive.c.id == archive_id)) is None:
            db.execute(archive.insert().values(id=archive_id, user_id=user.id, payload={
                "namespace": source.namespace, "schedule": source.schedule, "prompt": source.prompt,
                "notes": [_metadata(n) for n in source.notes], "originals": source.original_files,
                "promptArtifacts": source.prompt_artifacts,
                "noteIdMap": {n["id"]: _stable(user.id, source.namespace, "note", n["id"]) for n in source.notes},
                "courseIdMap": {k: v.id for k, v in mapping.items()},
            }))
    db.flush()
    return report


def export_transcripts(source: LegacySource, path: str | Path) -> dict:
    """Write a transport copy containing only the explicitly selected text."""
    selected, _, _ = source.candidates("transcripts_only")
    fields = ("id", "title", "createdAt", "updatedAt", "recordedAt", "sourceName", "courseId", "courseName")
    notes = [{**{k: n[k] for k in fields if k in n}, "transcript": source.transcript(n),
              "transcriptSha256": text_hash(source.transcript(n)), "partialTranscript": source.partial(n)} for n in selected]
    ids = {n.get("courseId") for n in selected}
    names = {n.get("courseName") for n in selected if n.get("courseName")}
    courses = [{k: c[k] for k in ("id", "name") if k in c} for c in source.schedule.get("courses", [])
               if c.get("id") in ids or c.get("name") in names]
    bundle = {"format": "tingji-transcripts-v1", "namespace": source.namespace, "notes": notes,
              "schedule": {"term": source.schedule.get("term", ""), "courses": courses}}
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(bundle, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    # Never replace an existing export whose content differs.
    if output.exists() and output.read_bytes() != data:
        raise ValueError("导出文件已存在且内容不同，请指定新文件名。")
    if not output.exists():
        output.write_bytes(data)
    return {"path": str(output), "bundleSha256": hashlib.sha256(data).hexdigest(),
            "noteCount": len(notes), "transcriptCharacters": sum(len(n["transcript"]) for n in notes),
            "transcriptSha256": [n["transcriptSha256"] for n in notes]}


def load_transcript_bundle(path: str | Path) -> LegacySource:
    path = Path(path).resolve()
    data = _json(path)
    if data.get("format") != "tingji-transcripts-v1" or not isinstance(data.get("notes"), list):
        raise ValueError("原文导入包格式无效。")
    allowed = {"id", "title", "createdAt", "updatedAt", "recordedAt", "sourceName", "courseId", "courseName",
               "transcript", "transcriptSha256", "partialTranscript"}
    ids = set()
    for note in data["notes"]:
        if not isinstance(note, dict) or set(note) - allowed or not isinstance(note.get("transcript"), str):
            raise ValueError("原文导入包包含未授权字段。")
        if not isinstance(note.get("id"), str) or note["id"] in ids or not note["transcript"].strip():
            raise ValueError("原文导入包的笔记编号或内容无效。")
        if text_hash(note["transcript"]) != note.get("transcriptSha256"):
            raise ValueError("原文导入包的 SHA256 校验失败。")
        ids.add(note["id"])
    return LegacySource(path.parent, str(data["namespace"]), data["notes"], data.get("schedule") or {})


def main():
    parser = argparse.ArgumentParser(description="只读核对旧版听记的数据和原文快照。")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--mode", choices=("transcripts_only", "full"), default="transcripts_only")
    args = parser.parse_args()
    print(json.dumps(scan_legacy(args.root).report(args.mode), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
