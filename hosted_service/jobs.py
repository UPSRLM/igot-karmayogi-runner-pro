from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import json
import os
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from hosted_service.config import Settings
from hosted_service.schemas import ArtifactInfo, RunRequest, RunStatusResponse, RunSummary

_REPORT_DIR_PATTERN = re.compile(r"^Report directory:\s*(?P<path>.+)$", re.MULTILINE)


class RunStatus(StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


@dataclass(slots=True)
class QueuedRun:
    run_id: str
    request: RunRequest
    groq_api_key: str | None
    gemini_api_key: str | None


class JobManager:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._queue: asyncio.Queue[QueuedRun] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._worker_task: asyncio.Task[None] | None = None
        self._jobs_file = self._settings.data_root / "jobs.json"
        self._logs_root = self._settings.data_root / "logs"
        self._jobs: dict[str, dict[str, Any]] = {}

    async def start(self) -> None:
        self._settings.data_root.mkdir(parents=True, exist_ok=True)
        self._settings.reports_root.mkdir(parents=True, exist_ok=True)
        self._settings.profile_root.mkdir(parents=True, exist_ok=True)
        self._logs_root.mkdir(parents=True, exist_ok=True)
        await self._load_jobs()
        self._worker_task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        if self._worker_task is None:
            return
        self._worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._worker_task

    async def enqueue(self, request: RunRequest) -> RunSummary:
        run_id = f"run-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"
        queued_at = datetime.now(UTC)
        record = {
            "run_id": run_id,
            "status": RunStatus.queued,
            "queued_at": queued_at.isoformat(),
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "report_dir": None,
            "stdout_log": str((self._logs_root / f"{run_id}.stdout.log").resolve()),
            "stderr_log": str((self._logs_root / f"{run_id}.stderr.log").resolve()),
            "error_message": None,
            "request": self._sanitize_request(request),
        }
        queued = QueuedRun(
            run_id=run_id,
            request=request,
            groq_api_key=request.groq_api_key.get_secret_value() if request.groq_api_key else None,
            gemini_api_key=request.gemini_api_key.get_secret_value() if request.gemini_api_key else None,
        )
        async with self._lock:
            self._jobs[run_id] = record
            await self._save_jobs()
        await self._queue.put(queued)
        return RunSummary(
            run_id=run_id,
            status=RunStatus.queued,
            queued_at=queued_at,
        )

    async def list_runs(self) -> list[RunSummary]:
        async with self._lock:
            runs = [self._to_summary(record) for record in self._jobs.values()]
        return sorted(runs, key=lambda item: item.queued_at, reverse=True)

    async def get_run(self, run_id: str) -> RunStatusResponse | None:
        async with self._lock:
            record = self._jobs.get(run_id)
        if record is None:
            return None
        return self._to_status(record)

    async def get_artifact_path(self, run_id: str, artifact_path: str) -> Path | None:
        status = await self.get_run(run_id)
        if status is None or status.report_dir is None:
            return None
        report_root = Path(status.report_dir).resolve()
        candidate = (report_root / artifact_path).resolve()
        if not self._is_relative_to(candidate, report_root) or not candidate.is_file():
            return None
        return candidate

    async def _worker_loop(self) -> None:
        while True:
            queued = await self._queue.get()
            try:
                await self._run_job(queued)
            finally:
                self._queue.task_done()

    async def _run_job(self, queued: QueuedRun) -> None:
        started_at = datetime.now(UTC)
        stdout_log = self._logs_root / f"{queued.run_id}.stdout.log"
        stderr_log = self._logs_root / f"{queued.run_id}.stderr.log"

        await self._update_record(
            queued.run_id,
            status=RunStatus.running,
            started_at=started_at.isoformat(),
            error_message=None,
        )

        command = self._build_command(queued.request)
        env = self._build_env(queued.groq_api_key, queued.gemini_api_key)

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._settings.runner_path.parent),
            env=env,
        )
        stdout_bytes, stderr_bytes = await process.communicate()
        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")
        stdout_log.write_text(stdout_text, encoding="utf-8")
        stderr_log.write_text(stderr_text, encoding="utf-8")

        report_dir = self._extract_report_dir(stdout_text)
        finished_at = datetime.now(UTC)
        status = RunStatus.succeeded if process.returncode == 0 else RunStatus.failed
        error_message = None if process.returncode == 0 else self._summarize_error(stderr_text, stdout_text)

        await self._update_record(
            queued.run_id,
            status=status,
            finished_at=finished_at.isoformat(),
            exit_code=process.returncode,
            report_dir=str(report_dir) if report_dir else None,
            error_message=error_message,
        )

    async def _update_record(self, run_id: str, **updates: Any) -> None:
        async with self._lock:
            record = self._jobs[run_id]
            record.update(updates)
            await self._save_jobs()

    async def _load_jobs(self) -> None:
        if not self._jobs_file.exists():
            return
        payload = json.loads(self._jobs_file.read_text(encoding="utf-8"))
        for record in payload.get("jobs", []):
            if record.get("status") == RunStatus.running:
                record["status"] = RunStatus.failed
                record["finished_at"] = datetime.now(UTC).isoformat()
                record["error_message"] = "Service restarted while the run was in progress."
            self._jobs[record["run_id"]] = record
        await self._save_jobs()

    async def _save_jobs(self) -> None:
        payload = {"jobs": list(self._jobs.values())}
        self._jobs_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _build_command(self, request: RunRequest) -> list[str]:
        command = [
            self._settings.python_executable,
            str(self._settings.runner_path),
            "--base-url",
            self._settings.default_base_url,
            "--output-dir",
            str(self._settings.reports_root),
            "--profile-dir",
            str(self._settings.profile_root),
            "--max-modules",
            str(request.max_modules),
            "--loading-timeout-seconds",
            str(request.loading_timeout_seconds),
            "--video-speed",
            str(request.video_speed),
            "--video-max-wait-seconds",
            str(request.video_max_wait_seconds),
        ]
        if request.start_url:
            command.extend(["--start-url", str(request.start_url)])
        if request.course_url:
            command.extend(["--course-url", str(request.course_url)])
        if request.strict_sequence:
            command.append("--strict-sequence")
        if request.auto_run_to_end:
            command.append("--auto-run-to-end")
        if request.skip_assessments:
            command.append("--skip-assessments")
        if request.pause_for_quiz:
            command.append("--pause-for-quiz")
        else:
            command.append("--no-pause-for-quiz")
        if request.continue_on_error:
            command.append("--continue-on-error")
        if request.headless:
            command.append("--headless")
        return command

    def _build_env(self, groq_api_key: str | None, gemini_api_key: str | None) -> dict[str, str]:
        env = dict(os.environ)
        if groq_api_key:
            env["IGOT_GROQ_API_KEY"] = groq_api_key
        else:
            env.pop("IGOT_GROQ_API_KEY", None)
        if gemini_api_key:
            env["IGOT_GEMINI_API_KEY"] = gemini_api_key
        else:
            env.pop("IGOT_GEMINI_API_KEY", None)
        return env

    def _extract_report_dir(self, stdout_text: str) -> Path | None:
        match = _REPORT_DIR_PATTERN.search(stdout_text)
        if match is None:
            return None
        return Path(match.group("path").strip()).resolve()

    def _summarize_error(self, stderr_text: str, stdout_text: str) -> str:
        for source in (stderr_text, stdout_text):
            for line in reversed(source.splitlines()):
                stripped = line.strip()
                if stripped:
                    return stripped[:500]
        return "Run failed without an error message."

    def _sanitize_request(self, request: RunRequest) -> dict[str, Any]:
        payload = request.model_dump(mode="json")
        payload.pop("groq_api_key", None)
        payload.pop("gemini_api_key", None)
        return payload

    def _to_summary(self, record: dict[str, Any]) -> RunSummary:
        return RunSummary(
            run_id=record["run_id"],
            status=str(record["status"]),
            queued_at=datetime.fromisoformat(record["queued_at"]),
            started_at=self._parse_datetime(record.get("started_at")),
            finished_at=self._parse_datetime(record.get("finished_at")),
        )

    def _to_status(self, record: dict[str, Any]) -> RunStatusResponse:
        report_dir = record.get("report_dir")
        artifacts = self._list_artifacts(Path(report_dir)) if report_dir else []
        return RunStatusResponse(
            run_id=record["run_id"],
            status=str(record["status"]),
            queued_at=datetime.fromisoformat(record["queued_at"]),
            started_at=self._parse_datetime(record.get("started_at")),
            finished_at=self._parse_datetime(record.get("finished_at")),
            exit_code=record.get("exit_code"),
            report_dir=report_dir,
            stdout_log=record.get("stdout_log"),
            stderr_log=record.get("stderr_log"),
            error_message=record.get("error_message"),
            artifacts=artifacts,
        )

    def _list_artifacts(self, report_dir: Path) -> list[ArtifactInfo]:
        if not report_dir.exists():
            return []
        artifacts: list[ArtifactInfo] = []
        for path in sorted(report_dir.rglob("*")):
            if path.is_file():
                artifacts.append(
                    ArtifactInfo(
                        relative_path=path.relative_to(report_dir).as_posix(),
                        size_bytes=path.stat().st_size,
                    )
                )
        return artifacts

    def _parse_datetime(self, value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value else None

    def _is_relative_to(self, candidate: Path, root: Path) -> bool:
        try:
            candidate.relative_to(root)
            return True
        except ValueError:
            return False
