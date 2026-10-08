"""Headless Blender integration tests. They run with ``uv run task test-blender``, or in the
CI ``blender.yml`` workflow.

They need a Blender 4.5 executable at KINESIS_BLENDER_BIN and the committed fixture
``blender/fixtures/foot_slide_v1.blend`` (rebuild with ``uv run task fixture``).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from kinesis.analysis.extraction import detect_from_extract, foot_and_toe, motion_from_extract
from kinesis.errors import SelectionInvalid, WorkerReportedFailure
from kinesis.evaluation.metrics import Samples, compute_metrics
from kinesis.jobs.local import LocalJobRunner
from kinesis.jobs.specs import candidate_apply_spec, export_spec, original_render_spec
from kinesis.jobs.worker_io import run_worker, spec_paths
from kinesis.repair.candidates import generate_candidate, repair_input
from kinesis.repair.render_plan import crop_render_spec
from kinesis.schemas import (
    DEFAULT_CANDIDATES,
    AnimationSelection,
    CandidateLabel,
    CandidateMetrics,
    ErrorCode,
    Severity,
    SkeletalScope,
)
from kinesis.schemas.worker import (
    ApplyRenderResult,
    ExportResult,
    ExtractResult,
    ExtractSpec,
    InspectResult,
    InspectSpec,
    RenderSpec,
    WorkerCommand,
)
from kinesis.testing.fixture_pipeline import LEFT_LEG, metric_context
from kinesis.testing.synthetic import RIG_BONES, foot_slide_v1

pytestmark = [
    pytest.mark.blender,
    pytest.mark.skipif(not os.environ.get("KINESIS_BLENDER_BIN"), reason="KINESIS_BLENDER_BIN"),
]

REPO = Path(__file__).resolve().parents[2]
FIXTURE_BLEND = REPO / "blender" / "fixtures" / "foot_slide_v1.blend"
BUILDER = REPO / "blender" / "fixtures" / "build_fixture.py"
TIMEOUT_S = 300
MIRROR_TOLERANCE_M = 1e-4  # tests/golden/thresholds.json: blender_vs_numpy_mirror_max_mm
THRESHOLDS: dict[str, Any] = json.loads(
    (REPO / "tests" / "golden" / "thresholds.json").read_text(encoding="utf-8")
)


def _blender() -> Path:
    return Path(os.environ["KINESIS_BLENDER_BIN"])


def _job_dir(root: Path, blend: Path = FIXTURE_BLEND) -> Path:
    (root / "input").mkdir(parents=True)
    shutil.copyfile(blend, root / "input" / "scene.blend")
    return root


def _inspect(job_dir: Path) -> InspectResult:
    spec = InspectSpec(result_path=spec_paths("inspect")[1])
    runner = LocalJobRunner(_blender())
    return asyncio.run(
        run_worker(
            runner, job_dir, "inspect", WorkerCommand.INSPECT, spec, InspectResult, TIMEOUT_S
        )
    )


def _extract(job_dir: Path, scope: SkeletalScope, window: tuple[int, int]) -> ExtractResult:
    spec = ExtractSpec(
        armature=scope.armature,
        chain_bones=scope.chain_bones,
        frame_start=window[0],
        frame_end=window[1],
        result_path=spec_paths("extract_scope")[1],
    )
    runner = LocalJobRunner(_blender())
    return asyncio.run(
        run_worker(
            runner, job_dir, "extract_scope", WorkerCommand.EXTRACT, spec, ExtractResult, TIMEOUT_S
        )
    )


@pytest.fixture(scope="module")
def module_scope() -> SkeletalScope:
    return SkeletalScope(
        armature="Rig",
        target_bones=("foot.L",),
        chain_bones=("thigh.L", "shin.L", "foot.L", "toe.L"),
        keyable_bones=("thigh.L", "shin.L", "foot.L"),
        context_bones=("pelvis", "root"),
    )


@pytest.fixture(scope="module")
def inspected(tmp_path_factory: pytest.TempPathFactory) -> InspectResult:
    return _inspect(_job_dir(tmp_path_factory.mktemp("inspect")))


@pytest.fixture(scope="module")
def extracted(
    tmp_path_factory: pytest.TempPathFactory, module_scope: SkeletalScope
) -> ExtractResult:
    return _extract(_job_dir(tmp_path_factory.mktemp("extract")), module_scope, (35, 100))


# ------------------------------------------------------------------ Phase 1


def test_scene_loads_and_inspect_reports_rig(
    inspected: InspectResult, fixture_truth: dict[str, Any]
) -> None:
    assert inspected.blender_version.startswith("4.5")
    assert (inspected.frame_start, inspected.frame_end) == tuple(fixture_truth["frame_range"])
    assert inspected.fps == fixture_truth["fps"]
    (rig,) = inspected.armatures
    assert rig.name == fixture_truth["armature"]
    assert {(b.name, b.parent) for b in rig.bones} == {(n, p) for n, p, *_ in RIG_BONES}
    assert all(b.deform for b in rig.bones)


def test_autoexec_scripts_disabled(inspected: InspectResult) -> None:
    assert inspected.autoexec_disabled


def test_extract_matches_numpy_mirror_within_0_1mm(extracted: ExtractResult) -> None:
    mirror = foot_slide_v1()
    motion = motion_from_extract(extracted)
    worst = 0.0
    for name in mirror.skeleton.names:
        worst = max(worst, float(np.abs(motion.heads[name] - mirror.head(name)).max()))
        worst = max(worst, float(np.abs(motion.tails[name] - mirror.tail(name)).max()))
        assert np.allclose(motion.skeleton.rest[name], mirror.skeleton.rest[name], atol=1e-6)
    sl = slice(35 - mirror.frame_start, 100 - mirror.frame_start + 1)
    for name, world in motion.chain_world.items():
        worst = max(worst, float(np.abs(world - mirror.world[name][sl]).max()))
    assert worst < MIRROR_TOLERANCE_M, f"max deviation {worst:.3e} m"


def test_phase1_exit_criterion_on_real_fixture(
    extracted: ExtractResult,
    selection: AnimationSelection,
    module_scope: SkeletalScope,
    fixture_truth: dict[str, Any],
) -> None:
    """About 10 cm of planted displacement on frames 40-90, measured on the real .blend."""
    _, detection = detect_from_extract(selection, module_scope, extracted)
    worst = detection.report.worst
    assert worst is not None
    tol = fixture_truth["expected"]["detected_interval_tolerance_frames"]
    start, end = fixture_truth["planted_interval"]
    assert abs(worst.start - start) <= tol
    assert abs(worst.end - end) <= tol
    band = fixture_truth["expected"]["planted_displacement_cm"]
    assert band["min"] <= worst.planted_displacement_cm <= band["max"]
    assert detection.report.severity is Severity(fixture_truth["expected"]["severity"])


def test_extract_reports_unknown_armature(tmp_path: Path, module_scope: SkeletalScope) -> None:
    job_dir = _job_dir(tmp_path)
    with pytest.raises(WorkerReportedFailure, match="armature 'Nope' not found"):
        _extract(job_dir, module_scope.model_copy(update={"armature": "Nope"}), (35, 100))


def test_scaled_armature_is_unsupported(tmp_path: Path, module_scope: SkeletalScope) -> None:
    """Concern C7: rest lengths are in armature units, so scaled rigs are rejected up front."""
    scaled = tmp_path / "scaled.blend"
    expr = (
        "import bpy; bpy.data.objects['Rig'].scale = (2, 2, 2); "
        f"bpy.ops.wm.save_as_mainfile(filepath={str(scaled)!r})"
    )
    proc = subprocess.run(  # noqa: S603 - fixed argv, test-only
        [
            str(_blender()),
            *("--background", "--factory-startup", "-noaudio", str(FIXTURE_BLEND)),
            *("--python-exit-code", "3", "--python-expr", expr),
        ],
        capture_output=True,
        timeout=TIMEOUT_S,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout.decode(errors="replace")[-2000:]
    with pytest.raises(SelectionInvalid, match="world scale") as err:
        _extract(_job_dir(tmp_path / "job", scaled), module_scope, (35, 100))
    assert err.value.code is ErrorCode.UNSUPPORTED_RIG


def test_committed_fixture_matches_builder(
    tmp_path: Path, extracted: ExtractResult, module_scope: SkeletalScope
) -> None:
    """Rebuilding the fixture yields the same motion: the .blend has not drifted from the mirror.

    The comparison is numeric, not by action hash: keys are float32, and libm differences
    between platforms can move a key by one ulp, so a .blend rebuilt on Linux CI is not
    bit-identical to the committed one built on Windows.
    """
    rebuilt = tmp_path / "rebuilt.blend"
    proc = subprocess.run(  # noqa: S603 - fixed argv, test-only
        [
            str(_blender()),
            *("--background", "--factory-startup", "-noaudio", "--python-exit-code", "3"),
            *("--python", str(BUILDER), "--", "--out", str(rebuilt)),
        ],
        capture_output=True,
        timeout=TIMEOUT_S,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout.decode(errors="replace")[-2000:]
    again = motion_from_extract(
        _extract(_job_dir(tmp_path / "job", rebuilt), module_scope, (35, 100))
    )
    committed = motion_from_extract(extracted)
    for name, heads in committed.heads.items():
        assert np.abs(again.heads[name] - heads).max() < 1e-6, name
        assert np.abs(again.tails[name] - committed.tails[name]).max() < 1e-6, name


# ------------------------------------------------------------------ Phase 2

PROBE = """
import bpy, json, sys
rig = bpy.data.objects["Rig"]
ad = rig.animation_data
out = {
    "active_action": ad.action.name if ad.action else None,
    "tracks": [
        {"name": t.name, "mute": t.mute,
         "strips": [{"action": s.action.name, "blend": s.blend_type,
                     "extrapolation": s.extrapolation} for s in t.strips]}
        for t in ad.nla_tracks
    ],
    "candidate_bones": sorted({
        fc.data_path.split(chr(34))[1]
        for a in bpy.data.actions if a.name.startswith("KIN_")
        for layer in a.layers for strip in layer.strips
        for bag in strip.channelbags for fc in bag.fcurves
    }),
}
mute = [a for a in sys.argv if a.startswith("--mute=")]
if mute:
    ad.nla_tracks[mute[0][7:]].mute = True
    sc = bpy.context.scene
    pts = []
    for f in range(sc.frame_start, sc.frame_end + 1):
        sc.frame_set(f)
        pts.append(list(rig.matrix_world @ rig.pose.bones["foot.L"].head))
    out["muted_foot"] = pts
print("PROBE" + json.dumps(out))
"""


def _probe(blend: Path, *extra: str) -> dict[str, Any]:
    """Inspect the NLA layout of a saved .blend (and optionally evaluate with a track muted)."""
    proc = subprocess.run(  # noqa: S603 - fixed argv, test-only
        [
            str(_blender()),
            *("--background", "--factory-startup", "-noaudio", str(blend)),
            *("--python-exit-code", "3", "--python-expr", PROBE, "--", *extra),
        ],
        capture_output=True,
        timeout=TIMEOUT_S,
        check=False,
    )
    out = proc.stdout.decode(errors="replace")
    assert proc.returncode == 0, out[-2000:]
    line = next(x for x in out.splitlines() if x.startswith("PROBE"))
    data: dict[str, Any] = json.loads(line[len("PROBE") :])
    return data


def _jpeg_size(path: Path) -> tuple[int, int]:
    """(width, height) from the first SOF marker of a JPEG."""
    data = path.read_bytes()
    i = 2
    while i < len(data):
        marker, length = data[i + 1], int.from_bytes(data[i + 2 : i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2):
            height = int.from_bytes(data[i + 5 : i + 7], "big")
            width = int.from_bytes(data[i + 7 : i + 9], "big")
            return width, height
        i += 2 + length
    raise AssertionError(f"no SOF marker in {path}")


@dataclass(frozen=True)
class AppliedCandidate:
    job_dir: Path
    candidate_id: str
    result: ApplyRenderResult
    metrics: CandidateMetrics
    render: RenderSpec | None


@pytest.fixture(scope="module")
def phase2(
    tmp_path_factory: pytest.TempPathFactory,
    extracted: ExtractResult,
    module_scope: SkeletalScope,
) -> dict[CandidateLabel, AppliedCandidate]:
    """Extract -> detect -> A/B -> apply_render on the real fixture (only A renders)."""
    selection = AnimationSelection.model_validate(
        {
            "scene_id": "scn_fixture01",
            "armature": "Rig",
            "target_bones": ["foot.L"],
            "temporal": {"frame_start": 45, "frame_end": 90},
        }
    )
    motion, detection = detect_from_extract(selection, module_scope, extracted)
    worst = detection.report.worst
    assert worst is not None
    interval = (worst.start, worst.end)
    inp = repair_input(motion, LEFT_LEG, interval, detection.floor_height)
    original = Samples.from_worker(motion.scene_range[0], extracted.all_bones)
    job_dir = _job_dir(tmp_path_factory.mktemp("phase2"))
    runner = LocalJobRunner(_blender())
    out: dict[CandidateLabel, AppliedCandidate] = {}
    for label, params in DEFAULT_CANDIDATES.items():
        result = generate_candidate(inp, params)
        candidate_id = label.value.lower() * 16
        render = None
        if label is CandidateLabel.A:
            ankle, ball = foot_and_toe(motion, "foot.L", "toe.L")
            k = params.blend_frames
            render = crop_render_spec(
                ankle,
                ball,
                motion.context_range[0],
                (interval[0] - k, interval[1] + k),
                motion.context_range,
            )
        node, spec = candidate_apply_spec(
            "jobtest01", label, candidate_id, "Rig", result.bone_keys(), render
        )
        applied = asyncio.run(
            run_worker(
                runner,
                job_dir,
                node,
                WorkerCommand.APPLY_RENDER,
                spec,
                ApplyRenderResult,
                TIMEOUT_S,
            )
        )
        metrics = compute_metrics(
            original,
            Samples.from_worker(motion.scene_range[0], applied.all_bones),
            metric_context(params, interval, detection.floor_height),
            result.ik_unreachable_frames,
        )
        out[label] = AppliedCandidate(job_dir, candidate_id, applied, metrics, render)
    return out


def test_original_action_fcurves_hash_unchanged(
    phase2: dict[CandidateLabel, AppliedCandidate], extracted: ExtractResult
) -> None:
    for c in phase2.values():
        assert c.result.original_action_hash_before == extracted.original_action_hash
        assert c.result.original_action_hash_after == extracted.original_action_hash


def test_candidate_action_created_on_nla_track(
    phase2: dict[CandidateLabel, AppliedCandidate],
) -> None:
    c = phase2[CandidateLabel.A]
    probe = _probe(c.job_dir / c.result.output_blend)
    assert probe["active_action"] is None
    assert [t["name"] for t in probe["tracks"]] == ["Kinesis:Original", "Kinesis:A"]
    (orig_strip,) = probe["tracks"][0]["strips"]
    assert orig_strip == {
        "action": "Rig_foot_slide_v1",
        "blend": "REPLACE",
        "extrapolation": "HOLD",
    }
    (cand_strip,) = probe["tracks"][1]["strips"]
    assert cand_strip == {
        "action": "KIN_jobtest01_A",
        "blend": "REPLACE",
        "extrapolation": "NOTHING",
    }
    assert probe["candidate_bones"] == ["foot.L", "shin.L", "thigh.L"]


def test_metrics_produced_and_defect_reduced(
    phase2: dict[CandidateLabel, AppliedCandidate],
) -> None:
    """The golden thresholds hold on Blender's own evaluation, not only on the mirror."""
    t = THRESHOLDS["all_candidates"]
    for label, c in phase2.items():
        m = c.metrics
        minimum = THRESHOLDS["candidates"][label.value]["slip_reduction_pct_min"]
        assert m.slip_reduction_pct >= minimum, label
        assert m.collateral_max_cm <= t["collateral_max_cm_max"], label
        assert m.outside_window_max_cm <= t["outside_window_max_cm_max"], label
        assert m.penetration_max_cm <= t["penetration_max_cm_max"], label
        assert m.jerk_rms_ratio <= t["jerk_rms_ratio_max"], label
        assert m.joint_limit_violations == 0, label
        assert not m.gated, m.gate_reasons


def test_render_produces_expected_frame_count(
    phase2: dict[CandidateLabel, AppliedCandidate],
) -> None:
    c = phase2[CandidateLabel.A]
    assert c.render is not None
    first, last = c.render.crop_frames
    frames = last - first + 1
    assert len(c.result.crop_frames) == frames
    assert len(c.result.context_frames) == len(range(0, frames, c.render.context_every))
    assert c.result.render_ms > 0
    for rel in (c.result.crop_frames[0], c.result.crop_frames[-1]):
        assert _jpeg_size(c.job_dir / rel) == (512, 512)
    assert _jpeg_size(c.job_dir / c.result.context_frames[0]) == (512, 384)
    assert not phase2[CandidateLabel.B].result.crop_frames  # B was applied without a render


def test_export_writes_output_blend_with_selected_track(
    phase2: dict[CandidateLabel, AppliedCandidate],
    extracted: ExtractResult,
    module_scope: SkeletalScope,
) -> None:
    c = phase2[CandidateLabel.B]
    node, spec = export_spec("foot_slide_v1", "Rig", CandidateLabel.B, c.candidate_id)
    runner = LocalJobRunner(_blender())
    result = asyncio.run(
        run_worker(runner, c.job_dir, node, WorkerCommand.EXPORT, spec, ExportResult, TIMEOUT_S)
    )
    assert result.original_action_hash == extracted.original_action_hash
    output = c.job_dir / result.output_blend
    probe = _probe(output, "--mute=Kinesis:B")
    assert [t["name"] for t in probe["tracks"]] == ["Kinesis:Original", "Kinesis:B"]
    assert not any(t["mute"] for t in probe["tracks"])
    # Muting the candidate track restores the original motion exactly.
    original_foot = next(b for b in extracted.all_bones if b.name == "foot.L").head
    assert np.abs(np.array(probe["muted_foot"]) - np.array(original_foot)).max() < 1e-6
    # With the track enabled, the exported file evaluates to the candidate.
    exported = _extract(_job_dir(c.job_dir.parent / "exported", output), module_scope, (35, 100))
    expected = Samples.from_worker(1, c.result.all_bones)
    got = motion_from_extract(exported)
    assert np.abs(got.heads["foot.L"] - expected.heads["foot.L"]).max() < 1e-6


def test_render_original_uses_the_same_cameras(
    phase2: dict[CandidateLabel, AppliedCandidate], extracted: ExtractResult
) -> None:
    a = phase2[CandidateLabel.A]
    assert a.render is not None
    node, spec = original_render_spec("Rig", a.render)
    runner = LocalJobRunner(_blender())
    result = asyncio.run(
        run_worker(
            runner, a.job_dir, node, WorkerCommand.APPLY_RENDER, spec, ApplyRenderResult, TIMEOUT_S
        )
    )
    assert len(result.crop_frames) == len(a.result.crop_frames)
    assert len(result.context_frames) == len(a.result.context_frames)
    assert result.original_action_hash_after == extracted.original_action_hash
    original = Samples.from_worker(1, extracted.all_bones)
    rendered = Samples.from_worker(1, result.all_bones)
    assert np.abs(rendered.heads["foot.L"] - original.heads["foot.L"]).max() < 1e-6


# ------------------------------------------------------------------ Phase 3: full pipeline


async def test_full_job_through_api_with_real_blender(tmp_path: Path) -> None:
    """Upload the real fixture, run the whole repair DAG through LocalJobRunner, choose B,
    and download the exported .blend."""
    import httpx

    from kinesis.jobs.service import JobService, ServiceConfig
    from kinesis.main import create_app
    from kinesis.providers.null import NullProvider
    from kinesis.storage.sqlite import Database, SqliteArtifactStore, SqliteJobStore

    db = Database(tmp_path / "k.db")
    service = JobService(
        SqliteJobStore(db),
        SqliteArtifactStore(db, tmp_path / "jobs"),
        LocalJobRunner(_blender()),
        NullProvider(),
        ServiceConfig(data_dir=tmp_path),
    )
    transport = httpx.ASGITransport(app=create_app(service))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        upload = await client.post(
            "/v1/scenes",
            files={
                "file": ("fixture.blend", FIXTURE_BLEND.read_bytes(), "application/octet-stream")
            },
        )
        assert upload.status_code == 201, upload.text
        body = {
            "selection": {
                "scene_id": upload.json()["scene_id"],
                "armature": "Rig",
                "target_bones": ["foot.L"],
                "temporal": {"frame_start": 45, "frame_end": 90},
            }
        }
        created = await client.post("/v1/jobs", json=body, headers={"Idempotency-Key": "real-0001"})
        assert created.status_code == 202, created.text
        await service.wait_idle()
        job = (await client.get(f"/v1/jobs/{created.json()['job_id']}")).json()
        assert job["status"] == "AWAITING_DECISION", job.get("error")
        assert (
            9.5
            <= job["defect"]["intervals"][job["defect"]["worst_interval_index"]][
                "planted_displacement_cm"
            ]
            <= 10.5
        )
        by_label = {c["label"]: c for c in job["candidates"]}
        assert by_label["A"]["metrics"]["slip_reduction_pct"] >= 95
        assert by_label["B"]["metrics"]["slip_reduction_pct"] >= 70
        assert job["evaluation"]["recommended"] == "B"
        frames = next(a for a in job["original_artifacts"] if a["kind"] == "PREVIEW_FRAMES")
        jpeg = await client.get(f"{frames['uri']}?frame=0")
        assert jpeg.content.startswith(b"\xff\xd8")

        decided = await client.post(f"/v1/jobs/{job['job_id']}/decision", json={"choice": "B"})
        assert decided.status_code == 202
        await service.wait_idle()
        done = (await client.get(f"/v1/jobs/{job['job_id']}")).json()
        assert done["status"] == "COMPLETED", done.get("error")
        output = await client.get(done["output"]["uri"])
        assert output.content.startswith(b"BLENDER")
