"""Benchmark the scene suite end to end and write docs/BENCHMARKS.md (+ docs/benchmarks/bench.json).

    uv run task bench [--runners local,container4,container2] [--no-models] [--scenes a,b]

Every number comes from a real run of the real pipeline on this machine:
- scenes: the canonical fixture and its variants (``kinesis.testing.synthetic.VARIANTS``), each
  built into a .blend by Blender 4.5 (the canonical one is the committed fixture), plus
  ``ual_walk_slide`` (a Quaternius UAL walk with a hand-made slide) when its glTF source is
  present (``uv run task ual-scene``);
- runners: host Blender (``local``) and the worker image (``container4`` / ``container2`` CPUs);
- models: the ``local`` pass uses Token Factory (planner + vision; about $0.003 per job) unless
  ``--no-models``; container passes use the null provider (timings only, no spend);
- each job runs to the export (decision = the recommendation).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchlib import ROOT, SceneRun, environment, make_service, run_scene, write_json
from prometheus_client import REGISTRY

from kinesis.jobs.container import ContainerJobRunner
from kinesis.jobs.local import LocalJobRunner
from kinesis.jobs.runner import JobRunner
from kinesis.providers.base import ModelProvider
from kinesis.providers.null import NullProvider
from kinesis.providers.token_factory import TokenFactoryProvider
from kinesis.testing.synthetic import VARIANTS

OUT_JSON = ROOT / "docs" / "benchmarks" / "bench.json"
OUT_MD = ROOT / "docs" / "BENCHMARKS.md"
SCENES_DIR = ROOT / "var" / "bench" / "scenes"
FIXTURE = ROOT / "blender" / "fixtures" / "foot_slide_v1.blend"
STAGES = (
    "extract_scope",
    "plan_repair",
    "apply_render_A",
    "apply_render_B",
    "render_original",
    "evaluate",
)
ACCEPT_CM = 1.0
UAL_GLB = Path(
    os.environ.get("KINESIS_UAL_GLB")
    or ROOT
    / "var"
    / "ual"
    / "Universal Animation Library[Standard]"
    / "Unreal-Godot"
    / "UAL1_Standard_RM.glb"
)


@dataclass(frozen=True)
class SceneSpec:
    armature: str
    bone: str
    selection: tuple[int, int]
    builder: str  # script under blender/fixtures/


SCENES: dict[str, SceneSpec] = {
    **{
        name: SceneSpec("Rig", v.bone, v.selection, "build_fixture.py")
        for name, v in VARIANTS.items()
    },
    # Rig, slide and frames: blender/fixtures/build_ual_walk.py (the slide is on frames 66-88).
    "ual_walk_slide": SceneSpec("Armature", "foot_l", (62, 80), "build_ual_walk.py"),
}


def default_scenes() -> list[str]:
    return [n for n in SCENES if n != "ual_walk_slide" or UAL_GLB.is_file()]


def blender_bin() -> str:
    found = os.environ.get("KINESIS_BLENDER_BIN") or next(
        (
            str(p)
            for p in (ROOT / ".tools").glob("blender-4.5.14*/blender*")
            if p.name in ("blender", "blender.exe") and p.is_file()
        ),
        "",
    )
    if not found:
        raise SystemExit("Blender 4.5 not found: run `uv run task setup-blender`")
    return found


def build_scenes(names: list[str]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    SCENES_DIR.mkdir(parents=True, exist_ok=True)
    for name in names:
        if name == "foot_slide_v1":
            out[name] = FIXTURE
            continue
        path = SCENES_DIR / f"{name}.blend"
        spec = SCENES[name]
        extra = ["--glb", str(UAL_GLB)] if name == "ual_walk_slide" else ["--variant", name]
        proc = subprocess.run(  # noqa: S603 - fixed argv
            [
                blender_bin(),
                "--background",
                "--factory-startup",
                "-noaudio",
                "--python-exit-code",
                "3",
                "--python",
                str(ROOT / "blender" / "fixtures" / spec.builder),
                "--",
                "--out",
                str(path),
                *extra,
            ],
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise SystemExit(
                f"building {name} failed:\n{proc.stdout.decode(errors='replace')[-2000:]}"
            )
        out[name] = path
    return out


def _tokens() -> dict[tuple[str, str], float]:
    return {
        (s.labels["model"], s.labels["kind"]): s.value
        for metric in REGISTRY.collect()
        if metric.name == "kinesis_provider_tokens"
        for s in metric.samples
        if s.name.endswith("_total")
    }


def _cost() -> float:
    return sum(
        s.value
        for metric in REGISTRY.collect()
        if metric.name == "kinesis_provider_cost_usd"
        for s in metric.samples
        if s.name.endswith("_total")
    )


def scene_record(
    run: SceneRun, runner: str, tokens: dict[str, int], cost: float | None
) -> dict[str, Any]:
    job = run.job
    worst = job.defect.worst if job.defect else None
    cands = {c.label.value: c for c in job.candidates}

    def after(label: str) -> float | None:
        m = cands[label].metrics if label in cands else None
        return round(m.planted_displacement_cm_after, 3) if m else None

    def metric(label: str, field: str) -> float | None:
        m = cands[label].metrics if label in cands else None
        return round(float(getattr(m, field)), 3) if m else None

    nodes = run.node_seconds()
    return {
        "scene": run.scene,
        "runner": runner,
        "status": job.status.value,
        "detected_interval": [worst.start, worst.end] if worst else None,
        "slip_cm": {
            "original": round(worst.planted_displacement_cm, 3) if worst else None,
            "A": after("A"),
            "B": after("B"),
        },
        "accepted": {
            label: (after(label) is not None and after(label) < ACCEPT_CM)  # type: ignore[operator]
            for label in ("A", "B")
        },
        "jerk_rms_ratio": {label: metric(label, "jerk_rms_ratio") for label in ("A", "B")},
        "foot_lock_error_cm": {label: metric(label, "contact_error_cm") for label in ("A", "B")},
        "collateral_max_cm": {label: metric(label, "collateral_max_cm") for label in ("A", "B")},
        "recommended": job.evaluation.recommended.value
        if job.evaluation and job.evaluation.recommended
        else None,
        "evaluator_status": job.evaluation.evaluator_status.value if job.evaluation else None,
        "plan_source": job.plan.plan_source.value if job.plan else None,
        "stage_s": {
            "inspect": round(run.upload_s, 3),
            **{s: nodes.get(s) for s in STAGES},
            "export": round(run.export_s, 3) if run.export_s is not None else None,
        },
        "upload_to_review_s": round(run.wall_s, 3),
        "model_tokens": tokens,
        "model_cost_usd": cost,
    }


async def run_pass(
    label: str, runner: JobRunner, provider: ModelProvider, scenes: dict[str, Path]
) -> list[dict[str, Any]]:
    data_dir = ROOT / "var" / "bench" / f"data-{label}-{int(time.time())}"
    service = make_service(data_dir, runner, provider)
    if hasattr(provider, "verify_models"):
        await provider.verify_models()
    records = []
    try:
        for name, blend in scenes.items():
            v = SCENES[name]
            selection = {
                "armature": v.armature,
                "target_bones": [v.bone],
                "temporal": {"frame_start": v.selection[0], "frame_end": v.selection[1]},
            }
            tokens0, cost0 = _tokens(), _cost()
            print(f"[{label}] {name} ...", flush=True)
            run = await run_scene(
                service, name, blend, selection, decide="recommended", key=f"bench-{label}-{name}"
            )
            tokens1 = _tokens()
            delta = {
                f"{m}:{k}": int(v - tokens0.get((m, k), 0))
                for (m, k), v in tokens1.items()
                if v - tokens0.get((m, k), 0)
            }
            cost = round(_cost() - cost0, 6) if delta else None
            rec = scene_record(run, label, delta, cost)
            print(
                f"    {rec['status']} slip {rec['slip_cm']} in {rec['upload_to_review_s']} s",
                flush=True,
            )
            records.append(rec)
    finally:
        service.store.db.close()
    return records


def startup_overhead(image: str) -> dict[str, Any]:
    def timed(argv: list[str]) -> float:
        t0 = time.perf_counter()
        subprocess.run(argv, capture_output=True, check=False)  # noqa: S603
        return time.perf_counter() - t0

    host = [blender_bin(), "--background", "--factory-startup", "--version"]
    container = [
        "docker", "run", "--rm", "--network", "none", "--read-only",
        "--entrypoint", "/opt/blender/blender", image, "--background", "--factory-startup", "--version",
    ]  # fmt: skip
    h = [timed(host) for _ in range(3)]
    c = [timed(container) for _ in range(3)]
    return {
        "host_blender_version_s": [round(x, 3) for x in h],
        "container_blender_version_s": [round(x, 3) for x in c],
        "overhead_per_step_s": round(statistics.median(c) - statistics.median(h), 3),
    }


def test_inventory() -> dict[str, int]:
    """Collected test counts per suite (pytest --collect-only), plus Kotlin @Test methods."""
    suites = {
        "unit": ["tests/unit"],
        "contract": ["tests/contract", "-m", "not live_nebius"],
        "fault": ["tests/fault"],
        "golden": ["tests/golden"],
        "integration (offline)": ["tests/integration"],
        "blender": ["-m", "blender"],
        "container": ["-m", "container"],
        "live (gated)": ["-m", "live_nebius or live_nebius_jobs"],
    }
    counts: dict[str, int] = {}
    for name, args in suites.items():
        out = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-p",
                "no:cacheprovider",
                *args,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        counts[name] = sum(1 for line in out.splitlines() if "::" in line)
    kotlin = ROOT / "client" / "kotlin-desktop" / "src" / "test"
    counts["kotlin client"] = sum(
        p.read_text(encoding="utf-8").count("@Test") for p in kotlin.rglob("*.kt")
    )
    return counts


def fmt(x: Any, spec: str = "{:.2f}") -> str:
    return "n/a" if x is None else spec.format(x)


def markdown(data: dict[str, Any]) -> str:
    env = data["environment"]
    rows = data["scenes"]
    lines = [
        "# Kinesis benchmarks",
        "",
        "Generated by `uv run task bench` (`scripts/bench.py`). Every number on this page comes from a real run of the "
        "real pipeline. The raw data is in [`benchmarks/bench.json`](benchmarks/bench.json).",
        "",
        f"- **Run:** {data['finished']}, commit `{env['commit']}`{' (with local changes)' if env['dirty'] else ''}",
        f"- **Machine:** {env['os']}; CPU {env['cpu']} ({env['cpu_count']} logical); GPU {env['gpu'] or 'none'}; Python {env['python']}",
        "- **Runners:**",
        "  - `local`: host Blender 4.5.14, rendering on the GPU when there is one;",
        "  - `container4` / `container2`: the worker image, rendering on CPU (Mesa llvmpipe) with 4 or 2 CPUs.",
        f"- **Models:** the `local` pass {'uses' if data['models'] else 'does not use'} Token Factory, with planner `{data.get('planner_model')}` and vision `{data.get('vision_model')}`. The container passes use the null provider.",
        "",
        "## Scenes",
        "",
        "- `foot_slide_v1`: the canonical synthetic fixture (10 cm sideways slide on `foot.L`).",
        "- `small_backward`, `right_foot`, `long_diagonal`: variants of the fixture motion with other injected slides (`kinesis.testing.synthetic.VARIANTS`).",
        "- `ual_walk_slide` (when present): a real game rig, the Quaternius *Universal Animation Library* mannequin "
        "(CC0, 65 bones, UE-style names), walking 4 cycles of `Walk_Loop` with root motion; "
        "an 8 cm sideways slide is added to `foot_l` on frames 66-88 by `blender/fixtures/build_ual_walk.py`.",
        "",
        "## Repair quality per scene",
        "",
        f"Acceptance means planted slip below {ACCEPT_CM:g} cm after the repair. *Foot-lock error* is the RMS wander of the planted foot (`contact_error_cm`). "
        "There are two plan sources: the `local` pass uses the parameters the model planned, and the container passes use the deterministic presets.",
        "",
    ]
    for runner in dict.fromkeys(r["runner"] for r in rows):
        q = [r for r in rows if r["runner"] == runner]
        source = q[0]["plan_source"] if q else None
        if (
            runner != data["quality_runner"]
            and source == "FALLBACK"
            and runner != next((r["runner"] for r in rows if r["plan_source"] == "FALLBACK"), None)
        ):
            continue  # one preset table is enough: every preset pass gives the same numbers
        lines += [
            f"### {'Model-planned' if source == 'MODEL' else 'Deterministic presets'} (`{runner}` pass)",
            "",
            "| Scene | Original slip | A slip | B slip | Accepted (A / B) | Jerk ratio (A / B) | Foot-lock error, cm (A / B) | Collateral max, cm | Recommended |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for r in q:
            s = r["slip_cm"]
            lines.append(
                f"| {r['scene']} | {fmt(s['original'])} cm | {fmt(s['A'])} cm | {fmt(s['B'])} cm | "
                f"{'yes' if r['accepted']['A'] else 'no'} / {'yes' if r['accepted']['B'] else 'no'} | "
                f"{fmt(r['jerk_rms_ratio']['A'])} / {fmt(r['jerk_rms_ratio']['B'])} | "
                f"{fmt(r['foot_lock_error_cm']['A'], '{:.3f}')} / {fmt(r['foot_lock_error_cm']['B'], '{:.3f}')} | "
                f"{fmt(max(v for v in r['collateral_max_cm'].values() if v is not None), '{:.4f}')} | {r['recommended']} |"
            )
        med_before = statistics.median(r["slip_cm"]["original"] for r in q)
        med_a = statistics.median(r["slip_cm"]["A"] for r in q)
        med_b = statistics.median(r["slip_cm"]["B"] for r in q)
        lines += [
            "",
            f"**Median across {len(q)} scenes:** slip goes from {med_before:.2f} cm to {med_a:.2f} cm (A) and {med_b:.2f} cm (B).",
            "",
        ]
    lines += [
        "## Time per stage",
        "",
        "Wall time in seconds. `inspect` covers the upload plus the headless inspect. The model stages "
        "(`plan_repair`, `evaluate`) include Token Factory latency only in the `local` pass.",
        "",
        "| Runner | Scene | inspect | extract | plan | apply+render A | apply+render B | render original | evaluate | export | upload to review |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        st = r["stage_s"]
        lines.append(
            f"| {r['runner']} | {r['scene']} | {fmt(st['inspect'])} | {fmt(st['extract_scope'])} | {fmt(st['plan_repair'])} | "
            f"{fmt(st['apply_render_A'])} | {fmt(st['apply_render_B'])} | {fmt(st['render_original'])} | "
            f"{fmt(st['evaluate'])} | {fmt(st['export'])} | **{fmt(r['upload_to_review_s'])}** |"
        )
    lines += ["", "**Median upload-to-review time per runner:**", ""]
    for runner in dict.fromkeys(r["runner"] for r in rows):
        times = [r["upload_to_review_s"] for r in rows if r["runner"] == runner]
        lines.append(f"- `{runner}`: {statistics.median(times):.1f} s over {len(times)} scenes")
    so = data.get("startup")
    if so:
        lines += [
            "",
            "## Container start-up overhead",
            "",
            f"`blender --version`: {so['host_blender_version_s']} s on the host against {so['container_blender_version_s']} s in the container.",
            f"The median overhead per worker step is **{so['overhead_per_step_s']:.2f} s**.",
        ]
    priced = [r for r in rows if r["model_cost_usd"] is not None]
    if priced:
        lines += [
            "",
            "## Model usage per job (Token Factory)",
            "",
            "| Scene | Tokens | Cost |",
            "|---|---|---|",
        ]
        for r in priced:
            toks = ", ".join(f"{k} {v}" for k, v in r["model_tokens"].items())
            lines.append(f"| {r['scene']} | {toks} | ${r['model_cost_usd']:.5f} |")
        lines.append("")
        lines.append(
            f"**Median model cost per job:** ${statistics.median(r['model_cost_usd'] for r in priced):.5f}."
        )
    inv = data["tests"]
    lines += [
        "",
        "## Test inventory",
        "",
        "Collected with `pytest --collect-only`. Kotlin counts the `@Test` methods.",
        "",
        "| Suite | Tests |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in inv.items()],
    ]
    vlm = ROOT / "docs" / "benchmarks" / "vlm_bakeoff.md"
    if vlm.exists():
        lines += [
            "",
            vlm.read_text(encoding="utf-8").replace(
                "## Vision-model bake-off",
                "## Vision-model bake-off\n\nFrom `uv run task vlm-bakeoff` ([raw data](benchmarks/vlm_bakeoff.json)).",
                1,
            ),
        ]
    return "\n".join(lines).rstrip() + "\n"


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runners", default="local,container4,container2")
    ap.add_argument("--scenes", default=",".join(default_scenes()))
    ap.add_argument("--no-models", action="store_true")
    ap.add_argument("--image", default="kinesis-worker:4.5.14")
    args = ap.parse_args(argv)
    scenes = build_scenes([s for s in args.scenes.split(",") if s])
    key = os.environ.get("NEBIUS_API_KEY")
    use_models = bool(key) and not args.no_models
    planner = os.environ.get("KINESIS_PLANNER_MODEL")
    vision = os.environ.get("KINESIS_VISION_MODEL")
    rows: list[dict[str, Any]] = []
    for label in [r for r in args.runners.split(",") if r]:
        provider: ModelProvider = NullProvider()
        if label == "local":
            runner: JobRunner = LocalJobRunner(Path(blender_bin()))
            if use_models:
                provider = TokenFactoryProvider.from_settings(
                    api_key=str(key), base_url=os.environ.get("KINESIS_TF_BASE_URL"),
                    planner_model=planner, vision_model=vision,
                    classifier_model=None, timeout_s=120,
                )  # fmt: skip
        elif label.startswith("container"):
            runner = ContainerJobRunner(
                args.image, cpus=float(label.removeprefix("container") or 4)
            )
        else:
            raise SystemExit(f"unknown runner {label!r}")
        rows += await run_pass(label, runner, provider, scenes)
        if isinstance(provider, TokenFactoryProvider):
            vision = provider.vision_model
            await provider.aclose()
    data = {
        "finished": time.strftime("%Y-%m-%d %H:%M"),
        "environment": environment(),
        "models": use_models,
        "planner_model": planner if use_models else None,
        "vision_model": vision if use_models else None,
        "quality_runner": rows[0]["runner"] if rows else None,
        "scenes": rows,
        "startup": startup_overhead(args.image)
        if any(r.startswith("container") for r in args.runners.split(","))
        else None,
        "tests": test_inventory(),
    }
    write_json(OUT_JSON, data)
    OUT_MD.write_text(markdown(data), encoding="utf-8", newline="\n")
    print(f"wrote {OUT_MD.relative_to(ROOT)} and {OUT_JSON.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
