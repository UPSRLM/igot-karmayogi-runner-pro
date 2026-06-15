from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import os
from pathlib import Path

from fastapi.testclient import TestClient

os.environ.setdefault("IGOT_SERVICE_TOKEN", "test-token")

from hosted_service.config import Settings
from hosted_service.main import create_app
from hosted_service.schemas import ArtifactInfo, RunStatusResponse, RunSummary


@dataclass
class FakeJobManager:
    artifact_file: Path

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def enqueue(self, request):
        return RunSummary(
            run_id="run-test-123",
            status="queued",
            queued_at=datetime.now(UTC),
        )

    async def list_runs(self):
        return [
            RunSummary(
                run_id="run-test-123",
                status="queued",
                queued_at=datetime.now(UTC),
            )
        ]

    async def get_run(self, run_id: str):
        if run_id != "run-test-123":
            return None
        return RunStatusResponse(
            run_id=run_id,
            status="succeeded",
            queued_at=datetime.now(UTC),
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            exit_code=0,
            report_dir=str(self.artifact_file.parent),
            stdout_log=None,
            stderr_log=None,
            error_message=None,
            artifacts=[ArtifactInfo(relative_path=self.artifact_file.name, size_bytes=self.artifact_file.stat().st_size)],
        )

    async def get_artifact_path(self, run_id: str, artifact_path: str):
        if run_id == "run-test-123" and artifact_path == self.artifact_file.name:
            return self.artifact_file
        return None


def build_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        service_token="test-token",
        data_root=tmp_path / "service-data",
        reports_root=tmp_path / "reports",
        profile_root=tmp_path / "browser-profile",
        runner_path=tmp_path / "run_live_qa.py",
        python_executable="python",
        default_base_url="https://portal.igotkarmayogi.gov.in",
        host="127.0.0.1",
        port=8080,
    )
    app = create_app(settings)
    artifact_file = tmp_path / "reports" / "run-test-123" / "summary.txt"
    artifact_file.parent.mkdir(parents=True, exist_ok=True)
    artifact_file.write_text("ok", encoding="utf-8")
    app.state.job_manager = FakeJobManager(artifact_file=artifact_file)
    return TestClient(app)


def test_authentication_required(tmp_path: Path) -> None:
    client = build_client(tmp_path)
    response = client.get("/api/runs")
    assert response.status_code == 401


def test_create_run_requires_url(tmp_path: Path) -> None:
    client = build_client(tmp_path)
    response = client.post(
        "/api/runs",
        headers={"Authorization": "Bearer test-token"},
        json={"max_modules": 2},
    )
    assert response.status_code == 422


def test_create_run_accepts_authenticated_request(tmp_path: Path) -> None:
    client = build_client(tmp_path)
    response = client.post(
        "/api/runs",
        headers={"Authorization": "Bearer test-token"},
        json={
            "start_url": "https://portal.igotkarmayogi.gov.in/app/seeAll/new?key=continueLearning",
            "groq_api_key": "transient-key",
            "max_modules": 2,
        },
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["run_id"] == "run-test-123"
    assert "groq_api_key" not in payload


def test_artifact_download_is_authenticated(tmp_path: Path) -> None:
    client = build_client(tmp_path)
    response = client.get(
        "/api/runs/run-test-123/artifacts/summary.txt",
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    assert response.text == "ok"
