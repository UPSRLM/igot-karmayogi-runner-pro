from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True, slots=True)
class Settings:
    service_token: str
    data_root: Path
    reports_root: Path
    profile_root: Path
    runner_path: Path
    python_executable: str
    default_base_url: str
    host: str
    port: int


def load_settings() -> Settings:
    project_root = Path(__file__).resolve().parent.parent
    service_token = os.getenv("IGOT_SERVICE_TOKEN", "").strip()
    if not service_token:
        raise RuntimeError("IGOT_SERVICE_TOKEN must be set before starting the hosted service.")

    data_root = Path(os.getenv("IGOT_SERVICE_DATA_ROOT", project_root / "service-data")).resolve()
    reports_root = Path(os.getenv("IGOT_SERVICE_REPORTS_ROOT", project_root / "reports")).resolve()
    profile_root = Path(os.getenv("IGOT_SERVICE_PROFILE_ROOT", project_root / "browser-profile")).resolve()
    runner_path = Path(os.getenv("IGOT_RUNNER_PATH", project_root / "run_live_qa.py")).resolve()

    return Settings(
        service_token=service_token,
        data_root=data_root,
        reports_root=reports_root,
        profile_root=profile_root,
        runner_path=runner_path,
        python_executable=os.getenv("IGOT_PYTHON_EXECUTABLE", "python"),
        default_base_url=os.getenv("IGOT_BASE_URL", "https://portal.igotkarmayogi.gov.in"),
        host=os.getenv("IGOT_SERVICE_HOST", "0.0.0.0"),
        port=int(os.getenv("IGOT_SERVICE_PORT", "8080")),
    )
