from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterBody(StrictModel):
    email: str
    password: str
    name: str


class TokenBody(StrictModel):
    token: str


class LoginBody(StrictModel):
    email: str
    password: str


class ForgotBody(StrictModel):
    email: str


class ResetBody(StrictModel):
    token: str
    password: str


class AccountPatch(StrictModel):
    name: str | None = None
    defaultPrompt: str | None = None
    language: str | None = None


class PasswordBody(StrictModel):
    currentPassword: str
    password: str


class CourseBody(StrictModel):
    name: str
    term: str = ""
    teacher: str = ""
    hotwords: str = ""
    prompt: str = ""


class CoursePatch(StrictModel):
    name: str | None = None
    term: str | None = None
    teacher: str | None = None
    hotwords: str | None = None
    prompt: str | None = None


class NotePatch(StrictModel):
    title: str | None = None
    courseId: str | None = None
    summary: str | None = None
    recordingDate: str | None = None


class UploadBody(StrictModel):
    filename: str
    size: int
    courseId: str | None = None
    title: str = ""
    prompt: str | None = None


class SummarizeBody(StrictModel):
    prompt: str | None = None
