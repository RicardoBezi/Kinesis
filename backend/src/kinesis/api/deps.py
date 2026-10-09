"""Service construction from settings, and FastAPI dependency access."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import FastAPI, Request

from kinesis.errors import ProviderError
from kinesis.jobs.container import ContainerJobRunner
from kinesis.jobs.local import LocalJobRunner
from kinesis.jobs.runner import JobRunner
from kinesis.jobs.service import JobService, ServiceConfig
from kinesis.orchestration.cache import Cache
from kinesis.orchestration.caches import MemoryCache, RedisCache
from kinesis.providers.base import ModelProvider
from kinesis.providers.mock import MockProvider
from kinesis.providers.null import NullProvider
from kinesis.providers.token_factory import ModelConfigError, TokenFactoryProvider
from kinesis.settings import ProviderKind, RunnerKind, Settings, get_settings
from kinesis.storage.sqlite import Database, SqliteArtifactStore, SqliteJobStore

log = logging.getLogger("kinesis.api")


def _blender_bin(settings: Settings) -> Path:
    if settings.kinesis_blender_bin is not None:
        return settings.kinesis_blender_bin
    found = shutil.which("blender")
    return Path(found) if found else Path("blender")


def build_provider(settings: Settings) -> ModelProvider:
    if settings.kinesis_provider is ProviderKind.MOCK:
        return MockProvider()
    if settings.kinesis_provider is ProviderKind.TOKEN_FACTORY:
        key = settings.nebius_api_key.get_secret_value() if settings.nebius_api_key else ""
        if not key:
            raise ValueError("KINESIS_PROVIDER=token_factory needs NEBIUS_API_KEY")
        return TokenFactoryProvider.from_settings(
            api_key=key,
            base_url=settings.kinesis_tf_base_url,
            planner_model=settings.kinesis_planner_model,
            vision_model=settings.kinesis_vision_model,
            classifier_model=settings.kinesis_classifier_model,
            timeout_s=settings.kinesis_tf_timeout_s,
        )
    return NullProvider()


def build_cache(settings: Settings) -> Cache:
    if settings.kinesis_redis_url:
        return RedisCache.from_url(settings.kinesis_redis_url)
    return MemoryCache()


def build_nebius_runner(settings: Settings) -> JobRunner:
    """Design A or B from settings; every missing value is reported at once (fail fast)."""
    s = settings
    required = {
        "NEBIUS_PROJECT_ID": s.nebius_project_id,
        "NEBIUS_JOB_IMAGE": s.nebius_job_image,
        "NEBIUS_BUCKET": s.nebius_bucket,
        "NEBIUS_S3_ACCESS_KEY_ID": s.nebius_s3_access_key_id,
        "NEBIUS_S3_SECRET_ACCESS_KEY": s.nebius_s3_secret_access_key,
        "NEBIUS_SERVICE_ACCOUNT_ID": s.nebius_service_account_id,
        "NEBIUS_AUTH_KEY_ID": s.nebius_auth_key_id,
        "NEBIUS_AUTH_PEM": s.nebius_auth_pem,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError(f"KINESIS_JOB_RUNNER=nebius needs {', '.join(missing)}")
    from kinesis.jobs.nebius.api import NebiusJobsClient
    from kinesis.jobs.nebius.auth import NebiusTokenProvider, ServiceAccountKey
    from kinesis.jobs.nebius.config import NebiusJobConfig, SpendGuard
    from kinesis.jobs.nebius.runner import NebiusJobRunner, NebiusSessionRunner
    from kinesis.jobs.nebius.store import Boto3ObjectStore

    assert s.nebius_s3_secret_access_key is not None
    assert s.nebius_auth_pem is not None
    secret = s.nebius_s3_secret_access_key.get_secret_value()
    config = NebiusJobConfig(
        project_id=str(s.nebius_project_id),
        image=str(s.nebius_job_image),
        bucket=str(s.nebius_bucket),
        region=s.nebius_region,
        platform=s.nebius_platform,
        preset=s.nebius_preset,
        s3_endpoint=s.nebius_s3_endpoint,
        s3_access_key_id=str(s.nebius_s3_access_key_id),
        s3_secret_access_key=secret,
        preemptible=s.nebius_preemptible,
        watchdog_s=s.nebius_watchdog_s,
        price_per_hour_usd=s.nebius_price_per_hour_usd,
        budget_usd=s.nebius_budget_usd,
    )
    tokens = NebiusTokenProvider(
        ServiceAccountKey(
            str(s.nebius_service_account_id), str(s.nebius_auth_key_id), s.nebius_auth_pem
        )
    )
    store = Boto3ObjectStore(
        config.bucket,
        endpoint_url=config.s3_endpoint,
        region=config.region,
        access_key_id=config.s3_access_key_id,
        secret_access_key=secret,
    )
    guard = SpendGuard(
        s.kinesis_data_dir / "nebius_spend.json", config.budget_usd, config.price_per_hour_usd
    )
    runner_cls = NebiusSessionRunner if s.nebius_runner_mode == "session" else NebiusJobRunner
    return runner_cls(NebiusJobsClient(tokens), store, config, guard)


def build_runner(settings: Settings) -> JobRunner:
    if settings.kinesis_job_runner is RunnerKind.CONTAINER:
        return ContainerJobRunner(settings.kinesis_worker_image)
    if settings.kinesis_job_runner is RunnerKind.NEBIUS:
        return build_nebius_runner(settings)
    return LocalJobRunner(_blender_bin(settings))


def build_service(settings: Settings, *, runner: JobRunner | None = None) -> JobService:
    db = Database(settings.db_path)
    return JobService(
        SqliteJobStore(db),
        SqliteArtifactStore(db, settings.jobs_root),
        runner or build_runner(settings),
        build_provider(settings),
        ServiceConfig(
            data_dir=settings.kinesis_data_dir,
            max_upload_bytes=settings.max_upload_bytes,
            inspect_timeout_s=settings.kinesis_blender_timeout_s,
            extract_timeout_s=settings.kinesis_blender_timeout_s,
            apply_timeout_s=settings.kinesis_blender_timeout_s,
            export_timeout_s=settings.kinesis_blender_timeout_s,
        ),
        cache=build_cache(settings),
    )


def ensure_service(app: FastAPI) -> JobService:
    """The app's service, built from settings on first use (tests inject their own)."""
    if getattr(app.state, "service", None) is None:
        app.state.service = build_service(get_settings())
    service: JobService = app.state.service
    return service


async def verify_provider(service: JobService) -> None:
    """ADR 0009 startup check: fail fast when the configured models are definitely wrong;
    only warn when the catalog cannot be reached (the breaker handles outages later)."""
    verify = getattr(service.provider, "verify_models", None)
    if verify is None:
        return
    try:
        problems: list[str] = await verify()
    except ProviderError as exc:
        log.warning("provider.catalog_unreachable", extra={"error": exc.message})
        return
    if problems:
        raise ModelConfigError("; ".join(problems))
    log.info("provider.models_verified", extra={"provider": service.provider.name})


def get_service(request: Request) -> JobService:
    return ensure_service(request.app)


__all__ = [
    "build_cache",
    "build_provider",
    "build_service",
    "ensure_service",
    "get_service",
    "verify_provider",
]
