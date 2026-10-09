"""Scripted, reproducible end-to-end demo (definition of done, METRICS §4).

    uv run task demo [--mock]

Runs the canonical fixture through the real pipeline on host Blender: upload and inspect ->
measure the slide -> plan A/B (Nemotron via Token Factory, or the mock provider with
``--mock`` / no key) -> apply and render A and B concurrently -> measure -> visual review ->
recommendation -> apply the recommendation as an NLA layer and export the .blend.

It prints a narrated log and writes ``docs/evaluation/demo_run.json``: total and per-node
latency, model ids and token usage, slip before/after for A and B, the collateral score, and
the original-action hash check.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchlib import ROOT, environment, make_service, run_scene, write_json

from kinesis.jobs.local import LocalJobRunner
from kinesis.providers.base import ModelProvider
from kinesis.providers.mock import MockProvider
from kinesis.providers.token_factory import TokenFactoryProvider
from kinesis.schemas import JobEventType

FIXTURE = ROOT / "blender" / "fixtures" / "foot_slide_v1.blend"
OUT = ROOT / "docs" / "evaluation" / "demo_run.json"
SELECTION = {
    "armature": "Rig",
    "target_bones": ["foot.L"],
    "temporal": {"frame_start": 45, "frame_end": 90},
    "instruction": "keep the heel planted",
}


# Model output can contain characters a Windows console (cp1252) cannot print.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")


def say(step: str, text: str) -> None:
    print(f"[{step:>9}] {text}", flush=True)


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mock", action="store_true", help="use the mock provider (no Token Factory)")
    args = ap.parse_args(argv)
    blender = os.environ.get("KINESIS_BLENDER_BIN") or next(
        (str(p) for p in (ROOT / ".tools").glob("blender-4.5.14*/blender*") if p.stem == "blender"),
        "",
    )
    if not blender:
        print("Blender 4.5 not found: run `uv run task setup-blender`")
        return 1
    key = os.environ.get("NEBIUS_API_KEY")
    provider: ModelProvider
    if key and not args.mock:
        tf = TokenFactoryProvider.from_settings(
            api_key=key, base_url=os.environ.get("KINESIS_TF_BASE_URL"),
            planner_model=os.environ.get("KINESIS_PLANNER_MODEL"),
            vision_model=os.environ.get("KINESIS_VISION_MODEL"),
            classifier_model=None, timeout_s=120,
        )  # fmt: skip
        await tf.verify_models()
        provider = tf
        say("models", f"Token Factory: planner {tf.planner_model}, vision {tf.vision_model}")
    else:
        provider = MockProvider()
        say("models", "mock provider (no NEBIUS_API_KEY or --mock): plans and reviews are canned")

    data_dir = ROOT / "var" / "demo" / time.strftime("%Y%m%d-%H%M%S")
    service = make_service(data_dir, LocalJobRunner(Path(blender)), provider)
    say("scene", f"{FIXTURE.relative_to(ROOT)}: left foot, frames 45-90")
    try:
        run = await run_scene(
            service,
            "demo",
            FIXTURE,
            SELECTION,
            decide="recommended",
            key=f"demo-{int(time.time())}",
        )
    finally:
        service.store.db.close()
        if isinstance(provider, TokenFactoryProvider):
            await provider.aclose()

    job = run.job
    worst = job.defect.worst if job.defect else None
    assert worst is not None, job.error
    say("measure", f"{job.defect.summary if job.defect else ''}")
    assert job.plan is not None
    say(
        "plan",
        f"{job.plan.plan_source.value} plan ({job.plan.model_id or 'deterministic presets'}): {job.plan.explanation}",
    )
    cands = {c.label.value: c for c in job.candidates}
    for label, c in cands.items():
        m = c.metrics
        if m is None:
            say(f"cand {label}", f"FAILED: {c.error.message if c.error else '?'}")
            continue
        say(
            f"cand {label}",
            f"slip {m.planted_displacement_cm_before:.2f} -> {m.planted_displacement_cm_after:.2f} cm "
            f"({m.slip_reduction_pct:.1f}% less), jerk ratio {m.jerk_rms_ratio:.2f}, "
            f"collateral {m.collateral_max_cm:.4f} cm",
        )
    ev = job.evaluation
    if ev is not None:
        for v in ev.visual:
            label = next(
                (c.label.value for c in job.candidates if c.candidate_id == v.candidate_id), "?"
            )
            say(
                "review",
                f"{label}: contact {v.contact_stability}/5, natural {v.naturalness}/5, prefers over original: {v.prefers_over_original} ({v.model_id})",
            )
        say(
            "recommend",
            f"{ev.recommended.value if ev.recommended else 'none'}: {ev.recommendation_reason}",
        )
    say("export", f"{job.status.value}; output {job.output.uri if job.output else 'none'}")

    output_path = None
    if job.output is not None:
        output_path = data_dir / "jobs" / job.job_id / "output"
        copies = sorted(output_path.glob("*.blend"))
        if copies:
            shutil.copyfile(copies[0], data_dir / "repaired_kinesis.blend")
            say(
                "export",
                f"repaired file: {(data_dir / 'repaired_kinesis.blend').relative_to(ROOT)}",
            )

    events = run.events
    hash_ok = not any(
        e.type is JobEventType.NODE_FAILED and "hash" in (e.message or "") for e in events
    )
    model_calls: list[dict[str, Any]] = []
    if ev is not None:
        model_calls += [
            {"task": "evaluate", "model": v.model_id, "prompt_tokens": v.usage.prompt_tokens,
             "completion_tokens": v.usage.completion_tokens, "latency_ms": v.latency_ms}
            for v in ev.visual
        ]  # fmt: skip
    record = {
        "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
        "environment": environment(),
        "provider": provider.name,
        "job_id": job.job_id,
        "status": job.status.value,
        "latency_s": {
            "upload_to_review": round(run.wall_s, 3),
            "inspect": round(run.upload_s, 3),
            "export": round(run.export_s, 3) if run.export_s is not None else None,
            "per_node": run.node_seconds(),
        },
        "defect": {
            "interval": [worst.start, worst.end],
            "planted_displacement_cm": round(worst.planted_displacement_cm, 3),
            "severity": job.defect.severity.value if job.defect else None,
        },
        "plan": {"source": job.plan.plan_source.value, "model_id": job.plan.model_id},
        "candidates": {
            label: None
            if c.metrics is None
            else {
                "slip_before_cm": round(c.metrics.planted_displacement_cm_before, 3),
                "slip_after_cm": round(c.metrics.planted_displacement_cm_after, 3),
                "slip_reduction_pct": round(c.metrics.slip_reduction_pct, 2),
                "collateral_max_cm": c.metrics.collateral_max_cm,
                "outside_window_max_cm": c.metrics.outside_window_max_cm,
                "jerk_rms_ratio": round(c.metrics.jerk_rms_ratio, 3),
            }
            for label, c in cands.items()
        },
        "model_calls": model_calls,
        "recommended": ev.recommended.value if ev and ev.recommended else None,
        "decision": job.decision.choice.value if job.decision else None,
        "original_action_untouched": hash_ok,
        "output": job.output.model_dump(mode="json") if job.output else None,
    }
    write_json(OUT, record)
    say("done", f"wrote {OUT.relative_to(ROOT)}")
    return 0 if job.status.value == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
