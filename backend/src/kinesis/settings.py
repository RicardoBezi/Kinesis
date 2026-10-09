"""Typed configuration. Values come only from environment variables or ``.env``.

See .env.example for documentation of each variable.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderKind(StrEnum):
    NULL = "null"
    MOCK = "mock"
    TOKEN_FACTORY = "token_factory"  # noqa: S105 - provider name, not a secret


class RunnerKind(StrEnum):
    LOCAL = "local"  # host Blender (KINESIS_BLENDER_BIN)
    CONTAINER = "container"  # the worker image via docker (KINESIS_WORKER_IMAGE)
    NEBIUS = "nebius"  # Nebius Serverless Jobs (docs/PHASE6.md; live runs pending)


class Settings(BaseSettings):
    # Empty values (e.g. `NEBIUS_PRICE_PER_HOUR_USD=` copied from .env.example) mean "unset".
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", frozen=True, env_ignore_empty=True
    )

    # runtime
    kinesis_env: str = "dev"
    kinesis_log_level: str = "INFO"
    kinesis_data_dir: Path = Path("var")
    kinesis_max_upload_mb: int = Field(default=200, ge=1, le=2048)
    kinesis_redis_url: str | None = None

    # blender worker
    kinesis_blender_bin: Path | None = None
    kinesis_blender_timeout_s: float = Field(default=300, gt=0)
    kinesis_job_runner: RunnerKind = RunnerKind.LOCAL
    kinesis_worker_image: str = "kinesis-worker:4.5.14"

    # model provider
    kinesis_provider: ProviderKind = ProviderKind.NULL
    nebius_api_key: SecretStr | None = None
    kinesis_tf_base_url: str | None = None
    kinesis_planner_model: str | None = None
    kinesis_vision_model: str | None = None
    kinesis_classifier_model: str | None = None
    kinesis_tf_timeout_s: float = Field(default=60, gt=0)

    # nebius serverless (phase 6): see docs/PHASE6.md
    nebius_project_id: str | None = None
    nebius_job_image: str | None = None
    nebius_runner_mode: str = "job"  # "job" (design A) | "session" (design B)
    nebius_region: str = "us-central1"
    nebius_platform: str = "cpu-d3"
    nebius_preset: str = "4vcpu-16gb"
    nebius_preemptible: bool = False
    nebius_bucket: str | None = None
    nebius_s3_endpoint: str = "https://storage.us-central1.nebius.cloud"
    nebius_s3_access_key_id: str | None = None
    nebius_s3_secret_access_key: SecretStr | None = None
    nebius_service_account_id: str | None = None
    nebius_auth_key_id: str | None = None
    nebius_auth_pem: Path | None = None  # e.g. C:/Users/<you>/.nebius/kinesis-runner.pem
    nebius_watchdog_s: float = Field(default=900, gt=0)
    nebius_budget_usd: float = Field(default=5.0, ge=0)
    nebius_price_per_hour_usd: float | None = None

    @property
    def jobs_root(self) -> Path:
        return self.kinesis_data_dir / "jobs"

    @property
    def db_path(self) -> Path:
        return self.kinesis_data_dir / "kinesis.db"

    @property
    def max_upload_bytes(self) -> int:
        return self.kinesis_max_upload_mb * 1024 * 1024


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
