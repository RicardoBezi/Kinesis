"""Service construction from settings, and FastAPI dependency access."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import FastAPI, Request

from kinesis.jobs.local import LocalJobRunner
from kinesis.jobs.runner import JobRunner
from kinesis.jobs.service import JobService, ServiceConfig
from kinesis.orchestration.cache import Cache
from kinesis.orchestration.caches import MemoryCache, RedisCache
from kinesis.providers.base import ModelProvider
from kinesis.providers.mock import MockProvider
from kinesis.providers.null import NullProvider
from kinesis.settings import ProviderKind, Settings, get_settings
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
        log.warning("provider.token_factory_unavailable_until_phase_4")
    return NullProvider()


def build_cache(settings: Settings) -> Cache:
    if settings.kinesis_redis_url:
        return RedisCache.from_url(settings.kinesis_redis_url)
    return MemoryCache()


def build_service(settings: Settings, *, runner: JobRunner | None = None) -> JobService:
    db = Database(settings.db_path)
    return JobService(
        SqliteJobStore(db),
        SqliteArtifactStore(db, settings.jobs_root),
        runner or LocalJobRunner(_blender_bin(settings)),
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


def get_service(request: Request) -> JobService:
    return ensure_service(request.app)


__all__ = ["build_cache", "build_provider", "build_service", "ensure_service", "get_service"]
