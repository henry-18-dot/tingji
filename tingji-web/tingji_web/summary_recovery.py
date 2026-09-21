"""Explicit, idempotent replacement of confirmed legacy extraction failures."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from sqlalchemy import select

from .config import get_settings
from .logic import resolve_prompt
from .models import Job, Note, ProviderRequest, User


PREFIX = "length-recovery-v2:"
LENGTH_ERROR = "DeepSeek 输出达到长度上限，未保存不完整结果。"
PER_JOB_BOUND = Decimal("2")


def check_recovery_request_budget(key: str, messages: list[dict], max_tokens: int) -> Decimal:
    if key != "final" or max_tokens != 32000:
        raise RuntimeError("本次恢复仅允许完整原文的一次整理。")
    rates = get_settings()
    input_bound = len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) + 512 + 64 * len(messages)
    # Above published peak Flash prices even at 10 RMB/USD; also honor higher
    # configured prices. Input UTF-8 bytes conservatively bound token count.
    amount = (Decimal(input_bound) * max(Decimal("3"), rates.deepseek_cache_miss_yuan_per_million)
              + Decimal(max_tokens) * max(Decimal("12"), rates.deepseek_output_yuan_per_million)) / Decimal(1_000_000)
    if amount > PER_JOB_BOUND:
        raise RuntimeError("本次恢复的预计费用超过预设范围，任务已停止。")
    return amount


def inspect_recovery(db, job_id: str) -> dict:
    job = db.get(Job, job_id)
    if not job:
        raise ValueError("恢复任务不存在。")
    replacement = db.scalar(select(Job).where(Job.dedupe_key == PREFIX + job.id))
    if replacement:
        return {"jobId": job.id, "replacementJobId": replacement.id, "alreadyQueued": True}
    note = db.get(Note, job.note_id)
    requests = list(db.scalars(select(ProviderRequest).where(ProviderRequest.job_id == job.id).order_by(ProviderRequest.id)))
    if (job.kind != "summarize" or job.status != "error" or job.error != LENGTH_ERROR
            or not note or note.deleted_at or not note.transcript.strip() or len(note.transcript) > 60000):
        raise ValueError("仅恢复已有完整原文、长度不超过六万字的明确分段长度失败。")
    if not requests or any(r.provider != "deepseek" or not r.operation_key.startswith(f"deepseek:{job.id}:extract-")
                           or r.state not in {"completed", "failed"}
                           or (r.state == "failed" and r.error != LENGTH_ERROR) for r in requests):
        raise ValueError("存在未知、其他失败或已准备的成稿请求，不能进入本次恢复。")
    other = list(db.scalars(select(Job).where(Job.note_id == note.id, Job.id != job.id)))
    if any(j.status in {"queued", "running", "waiting", "uncertain"} or j.created_at > job.created_at for j in other):
        raise ValueError("这条笔记已有后续或未知任务，不能恢复旧任务。")
    snapshot = {"job": job.id, "status": job.status, "error": job.error,
                "transcript": note.transcript, "summary": note.summary,
                "requests": [{"id": r.id, "state": r.state, "error": r.error, "metadata": r.metadata_json,
                              "usage": r.usage_json} for r in requests]}
    fingerprint = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {"jobId": job.id, "noteId": note.id, "fingerprint": fingerprint, "alreadyQueued": False,
            "transcriptChars": len(note.transcript), "priorRequestCount": len(requests),
            "unknownRequests": 0, "maxAdditionalYuan": str(PER_JOB_BOUND)}


def enqueue_recovery(db, approved: dict) -> dict:
    job = db.scalar(select(Job).where(Job.id == approved["jobId"]).with_for_update())
    if job:
        db.scalar(select(Note).where(Note.id == job.note_id).with_for_update())
    current = inspect_recovery(db, approved["jobId"])
    if current["alreadyQueued"]:
        return current
    if current["fingerprint"] != approved.get("fingerprint"):
        raise ValueError("任务在检查后发生变化，本次没有重新排队。")
    note = db.get(Note, job.note_id)
    user = db.get(User, note.user_id)
    # Re-resolve current built-in guidance while preserving course/account
    # custom requirements. The old job's frozen prompt remains untouched.
    prompt, version = resolve_prompt(db, user, note.course_id)
    replacement = Job(user_id=note.user_id, note_id=note.id, kind="summarize", dedupe_key=PREFIX + job.id,
                      prompt_snapshot=prompt, prompt_version_id=version, model=get_settings().deepseek_model)
    db.add(replacement)
    db.flush()
    note.status, note.stage, note.error = "queued", "已排队，继续整理", ""
    return {**current, "replacementJobId": replacement.id}
