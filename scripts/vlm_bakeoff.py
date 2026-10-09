"""Vision-model bake-off for the visual evaluator (owner decision 2026-10-09).

    uv run task vlm-bakeoff [--cap 0.30] [--repeats 3] [--models a,b,...] [--frames-dir DIR]

1. Renders real previews: one local job on the canonical fixture (host Blender), so
   Original, A and B come from the same crop camera.
2. Builds trials with a known answer (the measured truth):
   - ``orig_vs_A``: the candidate A removes the slide -> should be preferred;
   - ``orig_vs_B``: likewise for B;
   - ``swapped``: A shown as "original", the sliding original as "candidate" -> must NOT be
     preferred (catches a model that always says yes).
   Each trial runs in two frame styles: ``plain`` and ``annotated`` (a red contact anchor and a
   short ankle trail, projected with the worker's crop-camera maths).
3. Probes every model once, prints the projected cost, and aborts above ``--cap`` (default
   $0.30). It also stops mid-run if the measured spend reaches the cap.
4. Scores per model and style: accuracy against the truth, agreement with the measured slip
   ranking (contact A >= contact B), consistency (mean stdev of scores over repeats),
   valid-JSON rate, latency and cost per review.

Results: ``docs/benchmarks/vlm_bakeoff.json`` and ``docs/benchmarks/vlm_bakeoff.md``. Every
number comes from these calls; nothing is estimated except the pre-run cost projection.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchlib import ROOT, environment, make_service, run_scene, write_json

from kinesis.jobs.local import LocalJobRunner
from kinesis.providers.base import EvaluationRequest, ResultStatus
from kinesis.providers.null import NullProvider
from kinesis.providers.token_factory import TokenFactoryProvider
from kinesis.schemas import AnimationSelection, CandidateLabel, RepairCandidate
from kinesis.schemas.worker import ApplyRenderResult, ExtractResult, RenderSpec

MODELS = [
    "openbmb/MiniCPM-V-4_5",
    "Qwen/Qwen3.8-27B",
    "deepseek-ai/DeepSeek-V4.1-Flash",
    "zai-org/GLM-5.3-Flash",
]
SLOW_MODELS = {"zai-org/GLM-5.3-Flash": 180.0}  # timed out at 60 s in spike S4
FRAMES_PER_SIDE = 5
CROP_DIRECTION = np.array([0.9, -1.6, 0.6])  # blender/worker/main.py
LENS_MM, SENSOR_MM = 35.0, 36.0
OUT_DIR = ROOT / "docs" / "benchmarks"
SELECTION = {
    "armature": "Rig",
    "target_bones": ["foot.L"],
    "temporal": {"frame_start": 45, "frame_end": 90},
}


# ------------------------------------------------------------------ frames


@dataclass
class Previews:
    original: list[Path]
    a: list[Path]
    b: list[Path]
    first_frame: int
    interval: tuple[int, int]
    render: RenderSpec
    ankle: dict[str, np.ndarray]  # pane -> (T, 3) world ankle positions over the crop frames
    candidates: dict[CandidateLabel, RepairCandidate]
    selection: AnimationSelection


def _frames(job_dir: Path, rel: str) -> list[Path]:
    return sorted((job_dir / rel).glob("crop_*.jpg"))


async def render_previews(data_dir: Path) -> Previews:
    blender = os.environ.get("KINESIS_BLENDER_BIN") or str(
        next((ROOT / ".tools").glob("blender-4.5.14*/blender*"))
    )
    service = make_service(data_dir, LocalJobRunner(Path(blender)), NullProvider())
    try:
        run = await run_scene(
            service, "fixture", ROOT / "blender/fixtures/foot_slide_v1.blend", SELECTION
        )
    finally:
        service.store.db.close()  # Windows cannot delete an open SQLite file
    job, d = run.job, run.job_dir
    assert job.defect is not None, job.error
    assert job.defect.worst is not None, job.defect.summary
    by_label = {c.label: c for c in job.candidates}
    extract = ExtractResult.model_validate_json(
        (d / "work/extract_scope.result.json").read_text("utf-8")
    )
    render = RenderSpec.model_validate(
        json.loads((d / "work/apply_a.spec.json").read_text("utf-8"))["render"]
    )
    first = render.crop_frames[0]
    lo, hi = first - extract.scene_frame_start, render.crop_frames[1] - extract.scene_frame_start

    def ankle_of(bones: Any) -> np.ndarray:
        foot = next(b for b in bones if b.name == "foot.L")
        return np.array(foot.head, dtype=np.float64)[lo : hi + 1]

    ankle = {"original": ankle_of(extract.all_bones)}
    for label, node in ((CandidateLabel.A, "apply_a"), (CandidateLabel.B, "apply_b")):
        res = ApplyRenderResult.model_validate_json(
            (d / f"work/{node}.result.json").read_text("utf-8")
        )
        ankle[label.value] = ankle_of(res.all_bones)
    cid = {lab: c.candidate_id for lab, c in by_label.items()}
    return Previews(
        original=_frames(d, "original/frames"),
        a=_frames(d, f"candidates/{cid[CandidateLabel.A]}/frames"),
        b=_frames(d, f"candidates/{cid[CandidateLabel.B]}/frames"),
        first_frame=first,
        interval=(job.defect.worst.start, job.defect.worst.end),
        render=render,
        ankle=ankle,
        candidates=by_label,
        selection=job.selection,
    )


def project(points: np.ndarray, render: RenderSpec, size: int) -> np.ndarray:
    """World -> pixel coordinates for the worker's crop camera (perspective case)."""
    center = np.array(render.crop_center)
    location = center + CROP_DIRECTION * render.crop_radius_m
    forward = (center - location) / np.linalg.norm(center - location)
    right = np.cross(forward, [0.0, 0.0, 1.0])
    right /= np.linalg.norm(right)
    up = np.cross(right, forward)
    focal = LENS_MM / SENSOR_MM * size
    d = points - location
    depth = d @ forward
    u = size / 2 + focal * (d @ right) / depth
    v = size / 2 - focal * (d @ up) / depth
    return np.stack([u, v], axis=1)


def annotate(
    src: Path, dst: Path, anchor: np.ndarray, trail: np.ndarray, render: RenderSpec
) -> Path:
    from PIL import Image, ImageDraw

    image = Image.open(src).convert("RGB")
    size = image.width
    draw = ImageDraw.Draw(image)
    pts = project(np.vstack([anchor[None], trail]), render, size)
    (ax, ay), path = pts[0], pts[1:]
    if len(path) > 1:
        draw.line([tuple(p) for p in path], fill=(255, 210, 0), width=3)
    draw.ellipse([ax - 7, ay - 7, ax + 7, ay + 7], outline=(230, 30, 30), width=3)
    dst.parent.mkdir(parents=True, exist_ok=True)
    image.save(dst, quality=90)
    return dst


# ------------------------------------------------------------------ trials


@dataclass(frozen=True)
class Trial:
    condition: str
    style: str
    repeat: int
    shown_original: str  # pane key
    shown_candidate: str
    candidate_label: CandidateLabel
    truth_prefers: bool


@dataclass
class Outcome:
    model: str
    trial: Trial
    ok: bool
    prefers: bool | None
    scores: dict[str, int]
    latency_ms: int
    cost_usd: float | None
    error: str = ""


def trials(repeats: int) -> list[Trial]:
    conditions = [
        ("orig_vs_A", "original", "A", CandidateLabel.A, True),
        ("orig_vs_B", "original", "B", CandidateLabel.B, True),
        ("swapped", "A", "original", CandidateLabel.A, False),
    ]
    return [
        Trial(c, style, r, o, cand, label, truth)
        for r in range(repeats)
        for style in ("plain", "annotated")
        for c, o, cand, label, truth in conditions
    ]


def frame_sets(p: Previews, work: Path) -> dict[tuple[str, str], list[Path]]:
    """(pane, style) -> 5 frames sampled across the planted interval."""
    a, b = p.interval
    idx = sorted(
        {
            round(float(i))
            for i in np.linspace(a - p.first_frame, b - p.first_frame, FRAMES_PER_SIDE)
        }
    )
    panes = {"original": p.original, "A": p.a, "B": p.b}
    anchor = p.ankle["original"][a - p.first_frame]
    out: dict[tuple[str, str], list[Path]] = {}
    for pane, frames in panes.items():
        out[(pane, "plain")] = [frames[i] for i in idx]
        out[(pane, "annotated")] = [
            annotate(
                frames[i],
                work / pane / f"ann_{i:04d}.jpg",
                anchor,
                p.ankle[pane][max(0, i - 6) : i + 1],
                p.render,
            )
            for i in idx
        ]
    return out


async def run_trial(
    provider: TokenFactoryProvider, p: Previews, sets: dict[tuple[str, str], list[Path]], t: Trial
) -> Outcome:
    candidate = p.candidates[t.candidate_label]
    assert candidate.metrics is not None
    req = EvaluationRequest(
        selection=p.selection,
        candidate=candidate,
        metrics=candidate.metrics,
        original_frames=tuple(sets[(t.shown_original, t.style)]),
        candidate_frames=tuple(sets[(t.shown_candidate, t.style)]),
        frame_numbers=tuple(range(FRAMES_PER_SIDE)),
    )
    if t.condition == "swapped":  # do not leak the truth through the measured numbers
        req = EvaluationRequest(
            req.selection, req.candidate,
            candidate.metrics.model_copy(update={"slip_reduction_pct": 0.0, "planted_displacement_cm_after": candidate.metrics.planted_displacement_cm_before}),
            req.original_frames, req.candidate_frames, req.frame_numbers,
        )  # fmt: skip
    started = time.perf_counter()
    try:
        result = await provider.evaluate_candidate(req)
    except Exception as exc:  # transport errors count against the model's reliability
        return Outcome(
            provider.vision_model,
            t,
            False,
            None,
            {},
            int((time.perf_counter() - started) * 1000),
            None,
            type(exc).__name__,
        )
    v = result.value
    scores = {} if v is None else {
        "contact_stability": v.contact_stability, "naturalness": v.naturalness,
        "artifacts_visible": v.artifacts_visible, "performance_preservation": v.performance_preservation,
    }  # fmt: skip
    prefers = (
        v.prefers_over_original if result.status is ResultStatus.OK and v is not None else None
    )
    return Outcome(
        provider.vision_model, t, result.status is ResultStatus.OK, prefers, scores,
        result.latency_ms, result.cost_usd, "; ".join(result.reasons)[:200],
    )  # fmt: skip


# ------------------------------------------------------------------ summary


def summarize(outcomes: list[Outcome]) -> list[dict[str, Any]]:
    rows = []
    for model in dict.fromkeys(o.model for o in outcomes):
        for style in ("plain", "annotated"):
            subset = [o for o in outcomes if o.model == model and o.trial.style == style]
            if not subset:
                continue
            valid = [o for o in subset if o.ok and o.prefers is not None]
            correct = [o for o in valid if o.prefers == o.trial.truth_prefers]
            ranking = []
            for r in {o.trial.repeat for o in valid}:
                a = next(
                    (o for o in valid if o.trial.repeat == r and o.trial.condition == "orig_vs_A"),
                    None,
                )
                b = next(
                    (o for o in valid if o.trial.repeat == r and o.trial.condition == "orig_vs_B"),
                    None,
                )
                if a and b:
                    ranking.append(a.scores["contact_stability"] >= b.scores["contact_stability"])
            spreads = []
            for cond in ("orig_vs_A", "orig_vs_B", "swapped"):
                group = [o for o in valid if o.trial.condition == cond]
                for key in (
                    "contact_stability",
                    "naturalness",
                    "artifacts_visible",
                    "performance_preservation",
                ):
                    values = [o.scores[key] for o in group]
                    if len(values) > 1:
                        spreads.append(statistics.pstdev(values))
            costs = [o.cost_usd for o in subset if o.cost_usd is not None]
            rows.append({
                "model": model,
                "style": style,
                "trials": len(subset),
                "valid_json_rate": round(len(valid) / len(subset), 3),
                "accuracy": round(len(correct) / len(valid), 3) if valid else None,
                "ranking_agreement": round(sum(ranking) / len(ranking), 3) if ranking else None,
                "score_stdev_mean": round(statistics.fmean(spreads), 3) if spreads else None,
                "latency_ms_median": int(statistics.median(o.latency_ms for o in subset)),
                "cost_per_review_usd": round(statistics.fmean(costs), 6) if costs else None,
            })  # fmt: skip
    return rows


def pick_winner(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    eligible = [r for r in rows if (r["valid_json_rate"] or 0) >= 0.9 and r["accuracy"] is not None]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda r: (
            r["accuracy"],
            r["ranking_agreement"] or 0,
            -(r["score_stdev_mean"] or 9),
            -(r["cost_per_review_usd"] or 1),
        ),
    )


def markdown(
    rows: list[dict[str, Any]], winner: dict[str, Any] | None, meta: dict[str, Any]
) -> str:
    lines = [
        "## Vision-model bake-off",
        "",
        f"Run {meta['finished']} on commit `{meta['env']['commit']}`: {meta['calls']} calls, "
        f"${meta['spent_usd']:.4f} total (cap ${meta['cap_usd']:.2f}"
        + (
            f"; an earlier run lost its results after spending ${meta['prior_lost_run_usd']:.2f}"
            if meta.get("prior_lost_run_usd")
            else ""
        )
        + "), "
        f"{meta['repeats']} repeats per condition. Frames: real previews of the canonical fixture.",
        "",
        "Conditions with a known answer: Original vs A and Original vs B (the candidate should be preferred), "
        "and *swapped* (the repaired A shown as the original, the sliding original as the candidate: it "
        "should **not** be preferred).",
        "",
        "| Model | Frames | Trials | Accuracy | Ranking agreement (A >= B) | Score stdev | Valid JSON | Median latency | $/review |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:

        def f(x: Any, fmt: str = "{:.2f}") -> str:
            return "n/a" if x is None else fmt.format(x)

        lines.append(
            f"| `{r['model']}` | {r['style']} | {r['trials']} | {f(r['accuracy'])} | {f(r['ranking_agreement'])} | "
            f"{f(r['score_stdev_mean'])} | {f(r['valid_json_rate'])} | {r['latency_ms_median']} ms | "
            f"{f(r['cost_per_review_usd'], '{:.5f}')} |"
        )
    lines += [
        "",
        f"**Winner:** `{winner['model']}` ({winner['style']} frames)."
        if winner
        else "**Winner:** none met the valid-JSON bar.",
    ]
    misses = meta.get("misses") or {}
    if misses:
        lines += ["", "Wrong answers by condition (valid responses only):", ""]
        lines += [
            f"- `{m}`: " + ", ".join(f"{c} x{n}" for c, n in sorted(v.items()))
            for m, v in misses.items()
        ]
    lines += [
        "",
        "Accuracy is the share of valid answers whose `prefers_over_original` matches the known "
        "truth. The samples are small (6-9 trials per row; the run stopped at its cost cap), so "
        'read 1.00 as "no miss observed", not as a guarantee.',
    ]
    return "\n".join(lines) + "\n"


def misses_by_condition(trials: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for t in trials:
        if t["ok"] and t["prefers"] is not None and t["prefers"] != t["truth_prefers"]:
            per_model = out.setdefault(t["model"], {})
            per_model[t["condition"]] = per_model.get(t["condition"], 0) + 1
    return out


def rerender(path: Path = OUT_DIR / "vlm_bakeoff.json") -> str:
    """Rebuild the markdown report from saved results (no API calls)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    meta = {**data["meta"], "misses": misses_by_condition(data["trials"])}
    text = markdown(data["summary"], data["winner"], meta)
    (OUT_DIR / "vlm_bakeoff.md").write_text(text, encoding="utf-8", newline="\n")
    return text


# ------------------------------------------------------------------ main


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cap", type=float, default=0.30)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--models", default=",".join(MODELS))
    ap.add_argument("--yes", action="store_true", help="skip the confirmation after the estimate")
    ap.add_argument("--report-only", action="store_true", help="re-render the report, no API calls")
    ap.add_argument(
        "--prior-spend", type=float, default=0.0,
        help="USD already spent by an earlier, lost run; reported, not counted against --cap",
    )  # fmt: skip
    args = ap.parse_args(argv)
    if args.report_only:
        print(rerender())
        return 0
    key = os.environ.get("NEBIUS_API_KEY")
    if not key:
        print("NEBIUS_API_KEY is not set")
        return 1
    models = [m for m in args.models.split(",") if m]

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        work = Path(tmp)
        print("rendering real previews of the fixture (host Blender)...", flush=True)
        previews = await render_previews(work / "data")
        sets = frame_sets(previews, work / "annotated")
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        for pane in ("original", "A"):  # keep one annotated sample for the report
            src = sets[(pane, "annotated")][2]
            (OUT_DIR / f"vlm_annotated_{pane}.jpg").write_bytes(src.read_bytes())

        providers: dict[str, TokenFactoryProvider] = {}
        for model in models:
            prov = TokenFactoryProvider.from_settings(
                api_key=key, base_url=os.environ.get("KINESIS_TF_BASE_URL"),
                planner_model="unused", vision_model=model, classifier_model=None,
                timeout_s=SLOW_MODELS.get(model, 90.0),
            )  # fmt: skip
            prov.prefer_nvidia_vision = False
            await prov.verify_models()  # loads prices
            # The same output budget for every model: some reason without being tagged as
            # reasoning models (DeepSeek), and a 600-token cap made them look invalid.
            prov.evaluator_max_tokens = 4096
            providers[model] = prov

        plan = trials(args.repeats)
        print(f"probing {len(models)} models once each to project cost...", flush=True)
        outcomes: list[Outcome] = []
        spent = 0.0
        per_call: dict[str, float] = {}
        for model, prov in providers.items():
            o = await run_trial(prov, previews, sets, plan[0])
            outcomes.append(o)
            spent += o.cost_usd or 0.0
            per_call[model] = o.cost_usd if o.cost_usd is not None else 0.01
            print(f"  {model}: {o.latency_ms} ms, ${o.cost_usd or 0:.5f}, ok={o.ok}", flush=True)
        projected = spent + sum(per_call[m] * (len(plan) - 1) for m in models)
        print(
            f"projected total: ${projected:.4f} for {len(plan) * len(models)} calls (cap ${args.cap:.2f})"
        )
        if projected > args.cap:
            print("projected cost exceeds the cap; rerun with fewer --repeats or --models")
            return 2
        answer = "y" if args.yes else await asyncio.to_thread(input, "continue? [y/N] ")
        if answer.strip().lower() != "y":
            return 3

        for t in plan[1:]:
            for prov in providers.values():
                if spent >= args.cap:
                    print(f"cap reached at ${spent:.4f}; stopping early")
                    break
                o = await run_trial(prov, previews, sets, t)
                outcomes.append(o)
                spent += o.cost_usd or 0.0
                save(outcomes, spent, args, final=False)  # never lose paid-for results
        for prov in providers.values():
            await prov.aclose()

    save(outcomes, spent, args, final=True)
    return 0


def save(outcomes: list[Outcome], spent: float, args: argparse.Namespace, *, final: bool) -> None:
    rows = summarize(outcomes)
    winner = pick_winner(rows) if final else None
    meta = {
        "finished": time.strftime("%Y-%m-%d %H:%M"),
        "complete": final,
        "env": environment(),
        "calls": len(outcomes),
        "spent_usd": round(spent, 6),
        "cap_usd": args.cap,
        "prior_lost_run_usd": args.prior_spend,
        "repeats": args.repeats,
    }
    write_json(OUT_DIR / "vlm_bakeoff.json", {
        "meta": meta, "summary": rows, "winner": winner,
        "trials": [
            {"model": o.model, **o.trial.__dict__, "candidate_label": o.trial.candidate_label.value,
             "ok": o.ok, "prefers": o.prefers, "scores": o.scores, "latency_ms": o.latency_ms,
             "cost_usd": o.cost_usd, "error": o.error}
            for o in outcomes
        ],
    })  # fmt: skip
    if final:
        print(rerender())


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
