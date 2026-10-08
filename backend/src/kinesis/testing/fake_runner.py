"""FakeJobRunner: answers worker commands from the numpy mirror, with scriptable faults.

It implements ``JobRunner`` exactly like ``LocalJobRunner`` from the orchestrator's point of
view: it reads ``<node>.spec.json`` and writes ``<node>.result.json`` inside the job directory.
The scene file itself is ignored; every scene is ``foot_slide_v1``.

Faults are scripted per node name (the spec file stem, e.g. ``apply_a``) and consumed one per
attempt, so "crash once, then succeed" is ``{"apply_a": [Fault.CRASH]}``.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from kinesis.errors import WorkerCrashed, WorkerTimeout
from kinesis.jobs.runner import WorkerInvocation, WorkerOutcome, assert_inside
from kinesis.schemas.api import ArmatureInfo, BoneInfo
from kinesis.schemas.worker import (
    ApplyRenderResult,
    ApplyRenderSpec,
    BoneKeys,
    ExportResult,
    ExportSpec,
    ExtractSpec,
    InspectResult,
    WorkerCommand,
)
from kinesis.testing.fake_worker import (
    all_bone_samples,
    apply_keys,
    extract_result,
    scene_action_hash,
)
from kinesis.testing.synthetic import RIG_BONES, SyntheticScene, foot_slide_v1

# Smallest valid JPEG (1x1 grey), so frame files are real images.
TINY_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c"
    "1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0"
    "000b080001000101011100ffc4001f0000010501010101010100000000000000000102030405060708090a0bff"
    "c400b5100002010303020403050504040000017d01020300041105122131410613516107227114328191a10823"
    "42b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a5354555657"
    "58595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aa"
    "b2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8"
    "f9faffda0008010100003f00fbd3ffd9"
)


class Fault(StrEnum):
    CRASH = "CRASH"  # WorkerCrashed (non-zero exit)
    TIMEOUT = "TIMEOUT"  # WorkerTimeout
    MALFORMED = "MALFORMED"  # result.json is not valid JSON
    REPORTED = "REPORTED"  # the worker writes a WorkerFailure (ok: false)
    HASH_MISMATCH = "HASH_MISMATCH"  # apply_render claims the original action changed
    HANG = "HANG"  # never returns (until cancelled)


@dataclass(frozen=True)
class Call:
    node: str
    command: WorkerCommand
    started: float
    ended: float


class FakeJobRunner:
    name = "fake"

    def __init__(
        self,
        faults: Mapping[str, Iterable[Fault]] | None = None,
        *,
        delay_s: float = 0.0,
        scene: SyntheticScene | None = None,
    ) -> None:
        self._faults: dict[str, deque[Fault]] = defaultdict(deque)
        for node, items in (faults or {}).items():
            self._faults[node].extend(items)
        self.delay_s = delay_s
        self.scene = scene or foot_slide_v1()
        self.calls: list[Call] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.cancelled: list[str] = []

    def script(self, node: str, *faults: Fault) -> None:
        self._faults[node].extend(faults)

    def max_concurrent(self, command: WorkerCommand) -> int:
        """Largest number of overlapping calls of one command (from recorded timestamps)."""
        spans = [(c.started, c.ended) for c in self.calls if c.command is command]
        return max((sum(1 for s, e in spans if s <= t < e) for t, _ in spans), default=0)

    async def run(self, invocation: WorkerInvocation) -> WorkerOutcome:
        node = Path(invocation.spec_relpath).name.removesuffix(".spec.json")
        started = time.monotonic()
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            fault = self._faults[node].popleft() if self._faults[node] else None
            if fault is Fault.HANG:
                await asyncio.sleep(3600)
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            if fault is Fault.CRASH:
                raise WorkerCrashed(f"{node}: fake worker exited with code -11")
            if fault is Fault.TIMEOUT:
                raise WorkerTimeout(f"{node}: fake worker timed out")
            result_path = assert_inside(invocation.job_dir, invocation.result_relpath)
            result_path.parent.mkdir(parents=True, exist_ok=True)
            spec = json.loads(
                assert_inside(invocation.job_dir, invocation.spec_relpath).read_text("utf-8")
            )
            if fault is Fault.MALFORMED:
                result_path.write_text("{not json", encoding="utf-8")
                exit_code = 0
            elif fault is Fault.REPORTED:
                failure = {
                    "protocol": 1,
                    "ok": False,
                    "error_type": "RuntimeError",
                    "message": "fake",
                }
                result_path.write_text(json.dumps(failure), encoding="utf-8")
                exit_code = 2
            else:
                payload = self._answer(invocation, spec, fault)
                result_path.write_text(json.dumps(payload), encoding="utf-8")
                exit_code = 0
            return WorkerOutcome(result_path, int((time.monotonic() - started) * 1000), exit_code)
        except asyncio.CancelledError:
            self.cancelled.append(node)
            raise
        finally:
            self.in_flight -= 1
            self.calls.append(Call(node, invocation.command, started, time.monotonic()))

    def _answer(self, inv: WorkerInvocation, spec: dict[str, Any], fault: Fault | None) -> Any:
        scene = self.scene
        if inv.command is WorkerCommand.INSPECT:
            return InspectResult(
                blender_version="4.5.14 (fake)",
                frame_start=scene.frame_start,
                frame_end=scene.frame_end,
                fps=scene.fps,
                armatures=(
                    ArmatureInfo(
                        name=scene.armature,
                        bones=tuple(BoneInfo(name=n, parent=p) for n, p, *_ in RIG_BONES),
                    ),
                ),
                autoexec_disabled=True,
            ).model_dump(mode="json")
        if inv.command is WorkerCommand.EXTRACT:
            s = ExtractSpec.model_validate(spec)
            return extract_result(scene, s.chain_bones, (s.frame_start, s.frame_end)).model_dump(
                mode="json"
            )
        if inv.command is WorkerCommand.APPLY_RENDER:
            return self._apply_render(inv, ApplyRenderSpec.model_validate(spec), fault)
        e = ExportSpec.model_validate(spec)
        out = assert_inside(inv.job_dir, e.output_blend)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"BLENDER-fake-export")
        return ExportResult(
            output_blend=e.output_blend, original_action_hash=scene_action_hash(scene)
        ).model_dump(mode="json")

    def _apply_render(
        self, inv: WorkerInvocation, spec: ApplyRenderSpec, fault: Fault | None
    ) -> dict[str, Any]:
        keys: tuple[BoneKeys, ...] = () if spec.render_original else spec.keys
        repaired = apply_keys(self.scene, keys)
        out = assert_inside(inv.job_dir, spec.output_blend)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"BLENDER-fake-candidate")
        crop: list[str] = []
        context: list[str] = []
        if spec.render is not None:
            frames_dir = assert_inside(inv.job_dir, spec.frames_dir)
            frames_dir.mkdir(parents=True, exist_ok=True)
            first, last = spec.render.crop_frames
            for i in range(last - first + 1):
                (frames_dir / f"crop_{i:04d}.jpg").write_bytes(TINY_JPEG)
                crop.append(f"{spec.frames_dir}/crop_{i:04d}.jpg")
            for j, _ in enumerate(range(0, last - first + 1, spec.render.context_every)):
                (frames_dir / f"ctx_{j:04d}.jpg").write_bytes(TINY_JPEG)
                context.append(f"{spec.frames_dir}/ctx_{j:04d}.jpg")
        original_hash = scene_action_hash(self.scene)
        after = "0" * 64 if fault is Fault.HASH_MISMATCH else original_hash
        return ApplyRenderResult(
            output_blend=spec.output_blend,
            original_action_hash_before=original_hash,
            original_action_hash_after=after,
            all_bones=all_bone_samples(repaired),
            crop_frames=tuple(crop),
            context_frames=tuple(context),
            render_ms=1,
        ).model_dump(mode="json")

    async def health_check(self) -> tuple[bool, str]:
        return True, "fake runner"


__all__ = ["TINY_JPEG", "Call", "FakeJobRunner", "Fault"]
