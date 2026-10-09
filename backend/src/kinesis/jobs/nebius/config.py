"""Nebius runner configuration and the spend guard.

Target values come from the owner's decision record (2026-10-09): region us-central1, CPU
shape ``cpu-d3`` / ``4vcpu-16gb`` by default, an optional GPU showcase shape, a hard budget
cap, and a client-side watchdog (Nebius' minimum ``timeout`` is 1 h, far longer than any
Kinesis step should take).
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

from kinesis.errors import KinesisError
from kinesis.schemas.common import ErrorCode

GIB = 1024**3


@dataclass(frozen=True)
class NebiusJobConfig:
    project_id: str
    image: str
    bucket: str
    region: str = "us-central1"
    platform: str = "cpu-d3"
    preset: str = "4vcpu-16gb"
    s3_endpoint: str = "https://storage.us-central1.nebius.cloud"
    s3_access_key_id: str = field(default="", repr=False)
    s3_secret_access_key: str = field(default="", repr=False)
    registry_username: str | None = None
    registry_password: str | None = field(default=None, repr=False)
    preemptible: bool = False
    disk_gib: int = 32  # the default is 250 GiB, and disk is billed
    job_timeout_s: int = 3600  # Nebius minimum
    watchdog_s: float = 15 * 60  # cancel anything running longer than this
    price_per_hour_usd: float | None = None  # for the cost estimate; None = unknown
    budget_usd: float = 5.0  # hard cap across all runs (SpendGuard)
    key_prefix: str = "kinesis/jobs/"

    def prefix_for(self, job_dir: Path) -> str:
        return f"{self.key_prefix}{job_dir.name}/"


class BudgetExceeded(KinesisError):
    """Submitting would exceed the configured Nebius budget. Never retried."""

    code = ErrorCode.WORKER_CRASHED


class SpendGuard:
    """Tracks estimated Nebius spend in a small JSON file and refuses runs over the cap.

    Before a submission it reserves the worst case (watchdog duration at the configured hourly
    price); after the run it settles the estimate from the job's actual start/finish times.
    With no price configured it cannot estimate, so it counts runs and refuses nothing.
    """

    def __init__(self, path: Path, budget_usd: float, price_per_hour_usd: float | None) -> None:
        self.path = path
        self.budget_usd = budget_usd
        self.price = price_per_hour_usd
        self._lock = threading.Lock()

    def _read(self) -> dict[str, float]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return {
                "spent_usd": float(data.get("spent_usd", 0)),
                "runs": float(data.get("runs", 0)),
            }
        except (OSError, ValueError):
            return {"spent_usd": 0.0, "runs": 0.0}

    def _write(self, data: dict[str, float]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data), encoding="utf-8")

    @property
    def spent_usd(self) -> float:
        return self._read()["spent_usd"]

    def estimate(self, seconds: float) -> float | None:
        return None if self.price is None else self.price * seconds / 3600

    def reserve(self, worst_case_s: float) -> float:
        with self._lock:
            data = self._read()
            worst = self.estimate(worst_case_s) or 0.0
            if data["spent_usd"] + worst > self.budget_usd:
                raise BudgetExceeded(
                    f"Nebius budget ${self.budget_usd:.2f} would be exceeded "
                    f"(spent ${data['spent_usd']:.2f}, worst case for this run ${worst:.2f})"
                )
            data["spent_usd"] += worst
            data["runs"] += 1
            self._write(data)
            return worst

    def settle(self, reserved: float, actual_s: float | None) -> float | None:
        """Replace the reservation with the estimate from the real duration."""
        with self._lock:
            data = self._read()
            actual = self.estimate(actual_s) if actual_s is not None else None
            data["spent_usd"] = max(
                0.0, data["spent_usd"] - reserved + (actual if actual is not None else reserved)
            )
            self._write(data)
            return actual


__all__ = ["GIB", "BudgetExceeded", "NebiusJobConfig", "SpendGuard"]
