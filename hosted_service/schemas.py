from __future__ import annotations

from datetime import datetime
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, model_validator


class RunRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    start_url: AnyHttpUrl | None = None
    course_url: AnyHttpUrl | None = None
    max_modules: int = Field(default=50, ge=0, le=500)
    strict_sequence: bool = True
    auto_run_to_end: bool = True
    skip_assessments: bool = False
    pause_for_quiz: bool = False
    continue_on_error: bool = True
    loading_timeout_seconds: int = Field(default=35, ge=5, le=300)
    video_speed: float = Field(default=2.0, ge=0.5, le=16.0)
    video_max_wait_seconds: int = Field(default=2400, ge=30, le=14400)
    headless: bool = True
    groq_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None

    @model_validator(mode="after")
    def validate_urls(self) -> "RunRequest":
        if not self.start_url and not self.course_url:
            raise ValueError("Either start_url or course_url must be provided.")
        return self


class RunCreateResponse(BaseModel):
    run_id: str
    status: str
    queued_at: datetime


class ArtifactInfo(BaseModel):
    relative_path: str
    size_bytes: int


class RunStatusResponse(BaseModel):
    run_id: str
    status: str
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    report_dir: str | None = None
    stdout_log: str | None = None
    stderr_log: str | None = None
    error_message: str | None = None
    artifacts: list[ArtifactInfo] = []


class RunSummary(BaseModel):
    run_id: str
    status: str
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
