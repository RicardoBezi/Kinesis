"""Object storage for job directories. Each Nebius job mounts ``s3://<bucket>/<prefix>`` as
``/work``, so the job directory layout (io_contract.md) is unchanged: the runner uploads the
inputs under the prefix and downloads the outputs after the job finishes.

``Boto3ObjectStore`` talks to Nebius Object Storage (S3-compatible). ``MemoryObjectStore`` is
the test double used by the fake Nebius API.
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ObjectStore(Protocol):
    bucket: str

    async def put_bytes(self, key: str, data: bytes) -> None: ...

    async def get_bytes(self, key: str) -> bytes | None: ...

    async def list_keys(self, prefix: str) -> list[str]: ...

    async def delete_prefix(self, prefix: str) -> int: ...


async def upload_files(store: ObjectStore, root: Path, relpaths: list[str], prefix: str) -> None:
    for rel in relpaths:
        data = await asyncio.to_thread((root / rel).read_bytes)
        await store.put_bytes(f"{prefix}{rel}", data)


async def download_prefix(
    store: ObjectStore, prefix: str, root: Path, *, skip: set[str] | None = None
) -> list[str]:
    """Copy every object under ``prefix`` into ``root`` (keys are confined to ``root``)."""
    written: list[str] = []
    for key in await store.list_keys(prefix):
        rel = key[len(prefix) :]
        if not rel or (skip and rel in skip):
            continue
        data = await store.get_bytes(key)
        if data is not None and await asyncio.to_thread(_write_confined, root, rel, data):
            written.append(rel)
    return written


def _write_confined(root: Path, rel: str, data: bytes) -> bool:
    """Write ``root/rel`` unless the key would escape ``root``."""
    base = root.resolve()
    target = (base / rel).resolve()
    if not target.is_relative_to(base) or target == base:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return True


class MemoryObjectStore:
    def __init__(self, bucket: str = "kinesis-test") -> None:
        self.bucket = bucket
        self.objects: dict[str, bytes] = {}

    async def put_bytes(self, key: str, data: bytes) -> None:
        self.objects[key] = bytes(data)

    async def get_bytes(self, key: str) -> bytes | None:
        return self.objects.get(key)

    async def list_keys(self, prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))

    async def delete_prefix(self, prefix: str) -> int:
        keys = [k for k in self.objects if k.startswith(prefix)]
        for k in keys:
            del self.objects[k]
        return len(keys)


class Boto3ObjectStore:
    """Nebius Object Storage via boto3 (the ``nebius`` extra). Calls run in a worker thread."""

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str,
        region: str,
        access_key_id: str,
        secret_access_key: str,
    ) -> None:
        boto3 = importlib.import_module("boto3")  # optional `nebius` extra; no type stubs

        self.bucket = bucket
        self._s3: Any = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
        )

    async def put_bytes(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(self._s3.put_object, Bucket=self.bucket, Key=key, Body=data)

    async def get_bytes(self, key: str) -> bytes | None:
        def get() -> bytes | None:
            try:
                return bytes(self._s3.get_object(Bucket=self.bucket, Key=key)["Body"].read())
            except self._s3.exceptions.NoSuchKey:
                return None

        return await asyncio.to_thread(get)

    async def list_keys(self, prefix: str) -> list[str]:
        def list_all() -> list[str]:
            keys: list[str] = []
            for page in self._s3.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=prefix
            ):
                keys.extend(str(o["Key"]) for o in page.get("Contents", []))
            return keys

        return await asyncio.to_thread(list_all)

    async def delete_prefix(self, prefix: str) -> int:
        keys = await self.list_keys(prefix)
        for i in range(0, len(keys), 1000):
            batch = [{"Key": k} for k in keys[i : i + 1000]]
            await asyncio.to_thread(
                self._s3.delete_objects, Bucket=self.bucket, Delete={"Objects": batch}
            )
        return len(keys)


__all__ = [
    "Boto3ObjectStore",
    "MemoryObjectStore",
    "ObjectStore",
    "download_prefix",
    "upload_files",
]
