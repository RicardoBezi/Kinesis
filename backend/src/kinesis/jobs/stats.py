"""Product metrics (docs/METRICS.md §1) computed from stored jobs, decisions and costs."""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence

from kinesis.schemas import DecisionChoice, HumanDecision, ProductStats, RepairJob


def _median(values: Sequence[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def compute_product_stats(
    jobs: Sequence[RepairJob],
    decisions: Sequence[HumanDecision],
    costs: Mapping[str, tuple[float, int]],
) -> ProductStats:
    """Pure: the same inputs always give the same stats.

    - acceptance = decisions for A or B / all decisions;
    - agreement = ``agreed`` decisions / decisions where the model recommended something;
    - slip reduction is the *selected* candidate's;
    - cost per accepted repair = model cost of all jobs / accepted jobs. It is ``None`` when
      any model call had no known price, so the figure is never silently understated.
      Serverless compute cost joins this in Phase 6.
    """
    by_id = {j.job_id: j for j in jobs}
    accepted = [d for d in decisions if d.choice is not DecisionChoice.REJECT_ALL]
    recommended = [d for d in decisions if d.model_recommended is not None]
    slips: list[float] = []
    for d in accepted:
        job = by_id.get(d.job_id)
        chosen = next(
            (c for c in (job.candidates if job else ()) if c.candidate_id == d.candidate_id), None
        )
        if chosen is not None and chosen.metrics is not None:
            slips.append(chosen.metrics.slip_reduction_pct)
    times = [d.time_to_decision_s for d in decisions if d.time_to_decision_s is not None]
    total_cost = sum(usd for usd, _ in costs.values())
    unpriced = sum(n for _, n in costs.values())
    cost_per_accepted = total_cost / len(accepted) if accepted and costs and unpriced == 0 else None
    return ProductStats(
        jobs_total=len(jobs),
        jobs_decided=len(decisions),
        candidate_acceptance_rate=len(accepted) / len(decisions) if decisions else None,
        model_human_agreement_rate=(
            sum(1 for d in recommended if d.agreed) / len(recommended) if recommended else None
        ),
        median_slip_reduction_pct=_median(slips),
        median_time_to_decision_s=_median(times),
        cost_per_accepted_repair_usd=cost_per_accepted,
    )


__all__ = ["compute_product_stats"]
