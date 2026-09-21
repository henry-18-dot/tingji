from __future__ import annotations

from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import require_csrf, require_user, require_verified
from .config import get_settings
from .course_links import enqueue_reinforcement, reinforcement_json
from .database import get_db
from .logic import iso
from .models import Course, User, utcnow
from .preferences import save_settings, settings_json
from .preferences_models import CourseReinforcement, UserFeedback, FeedbackAttachment
from .schemas import StrictModel


router = APIRouter()


class SettingsPatch(StrictModel):
    preferences: dict[str, Literal["low", "medium", "high"]] | None = None
    customPrompt: str | None = Field(default=None, max_length=12000)
    greetingCategories: list[Literal["math", "life", "novel", "drama", "film", "game", "absurd"]] | None = None


class FeedbackFileBody(StrictModel):
    filename: str = Field(min_length=1, max_length=180)
    contentBase64: str = Field(max_length=4194304)


class FeedbackBody(StrictModel):
    content: str = Field(min_length=1, max_length=4000)
    attachments: list[FeedbackFileBody] = Field(default_factory=list, max_length=4)


@router.get("/api/settings")
def get_preferences(user: User = Depends(require_user), db: Session = Depends(get_db)):
    return {"settings": settings_json(db, user)}


@router.patch("/api/settings", dependencies=[Depends(require_csrf)])
def patch_preferences(body: SettingsPatch, user: User = Depends(require_user), db: Session = Depends(get_db)):
    result = save_settings(db, user, body.model_dump(exclude_unset=True))
    db.commit()
    return {"settings": result}


@router.post("/api/feedback", dependencies=[Depends(require_csrf)])
def submit_feedback(body: FeedbackBody, user: User = Depends(require_user), db: Session = Depends(get_db)):
    content = body.content.strip()
    if not content:
        raise ValueError("请写下想反馈的内容。")
    recent = db.scalar(select(func.count()).select_from(UserFeedback).where(
        UserFeedback.user_id == user.id, UserFeedback.created_at >= utcnow() - timedelta(hours=1)
    ))
    if recent >= 20:
        raise HTTPException(429, "反馈提交较多，请稍后再试。")
    from .feedback_files import validate_files
    files = validate_files(body.attachments)
    row = UserFeedback(user_id=user.id, content=content)
    db.add(row)
    db.flush()
    for filename, media, data in files:
        db.add(FeedbackAttachment(feedback_id=row.id, user_id=user.id, filename=filename, media_type=media, data=data, size=len(data)))
    db.commit()
    return {"message": "反馈已收到。", "id": row.id}


@router.get("/api/admin/feedback")
def admin_feedback(limit: int = Query(100, ge=1, le=200), user: User = Depends(require_user), db: Session = Depends(get_db)):
    if not user.is_admin:
        raise HTTPException(403, "只有管理员可以查看反馈。")
    from .feedback_files import attachments_for
    rows = db.execute(select(UserFeedback, User.name).join(User, User.id == UserFeedback.user_id)
                      .order_by(UserFeedback.created_at.desc()).limit(limit))
    return {"feedback": [{"id": feedback.id, "userId": feedback.user_id, "name": name,
                          "content": feedback.content, "createdAt": iso(feedback.created_at), "attachments": attachments_for(db, feedback.id)} for feedback, name in rows]}


def _owned_course(db: Session, user: User, course_id: str) -> Course:
    course = db.scalar(select(Course).where(Course.id == course_id, Course.user_id == user.id).with_for_update())
    if course is None:
        raise HTTPException(404, "找不到这门课程。")
    return course


@router.post("/api/courses/{course_id}/reinforce", dependencies=[Depends(require_csrf)])
def reinforce_course(course_id: str, user: User = Depends(require_verified), db: Session = Depends(get_db)):
    course = _owned_course(db, user, course_id)
    if not get_settings().deepseek_api_key:
        raise HTTPException(503, "整理服务尚未配置，请联系开发者。")
    row = enqueue_reinforcement(db, user, course)
    db.commit()
    return {"reinforcement": reinforcement_json(db, row)}


@router.get("/api/courses/{course_id}/reinforce")
def reinforcement_status(course_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    _owned_course(db, user, course_id)
    row = db.scalar(select(CourseReinforcement).where(
        CourseReinforcement.course_id == course_id, CourseReinforcement.user_id == user.id
    ).order_by(CourseReinforcement.created_at.desc()).limit(1))
    return {"reinforcement": reinforcement_json(db, row) if row else None}


@router.get("/api/feedback/files/{file_id}")
def feedback_file(file_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    from .feedback_files import attachment_response
    return attachment_response(db,user,file_id)
