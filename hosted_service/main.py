from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse

from hosted_service.auth import require_bearer_token
from hosted_service.config import Settings, load_settings
from hosted_service.jobs import JobManager
from hosted_service.schemas import ArtifactInfo, RunCreateResponse, RunRequest, RunStatusResponse, RunSummary


@asynccontextmanager
async def lifespan(app: FastAPI):
    manager: JobManager = app.state.job_manager
    await manager.start()
    try:
        yield
    finally:
        await manager.stop()


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or load_settings()
    app = FastAPI(title="iGot Hosted QA Runner", version="0.1.0", lifespan=lifespan)
    app.state.settings = resolved_settings
    app.state.job_manager = JobManager(resolved_settings)

    auth_dependency = Annotated[None, Depends(require_bearer_token)]

    @app.get("/healthz")
    async def healthcheck() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/runs", dependencies=[Depends(require_bearer_token)], response_model=list[RunSummary])
    async def list_runs() -> list[RunSummary]:
        manager: JobManager = app.state.job_manager
        return await manager.list_runs()

    @app.post(
        "/api/runs",
        dependencies=[Depends(require_bearer_token)],
        response_model=RunCreateResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_run(request: RunRequest) -> RunCreateResponse:
        manager: JobManager = app.state.job_manager
        created = await manager.enqueue(request)
        return RunCreateResponse(run_id=created.run_id, status=created.status, queued_at=created.queued_at)

    @app.get("/api/runs/{run_id}", dependencies=[Depends(require_bearer_token)], response_model=RunStatusResponse)
    async def get_run(run_id: str) -> RunStatusResponse:
        manager: JobManager = app.state.job_manager
        status_payload = await manager.get_run(run_id)
        if status_payload is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
        return status_payload

    @app.get(
        "/api/runs/{run_id}/artifacts",
        dependencies=[Depends(require_bearer_token)],
        response_model=list[ArtifactInfo],
    )
    async def list_artifacts(run_id: str) -> list[ArtifactInfo]:
        manager: JobManager = app.state.job_manager
        status_payload = await manager.get_run(run_id)
        if status_payload is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
        return status_payload.artifacts

    @app.get("/api/runs/{run_id}/artifacts/{artifact_path:path}", dependencies=[Depends(require_bearer_token)])
    async def download_artifact(run_id: str, artifact_path: str):
        manager: JobManager = app.state.job_manager
        artifact = await manager.get_artifact_path(run_id, artifact_path)
        if artifact is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
        return FileResponse(path=artifact, filename=artifact.name)

    return app


app = create_app()
