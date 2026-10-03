"""Frozen Kinesis data contracts.

Every API request and response, every model output that affects execution, and the Blender
worker protocol are defined here. ``docs/api/openapi.json`` is generated from these models
and is the source for the Kotlin client's generated models (ADR 0008).
"""

from kinesis.schemas.analysis import (
    AnimationFeatures,
    DefectReport,
    DetectionConfig,
    PlantedInterval,
    Severity,
)
from kinesis.schemas.api import (
    ArmatureInfo,
    BoneInfo,
    ComponentHealth,
    CreateRepairJobRequest,
    EventPage,
    HealthReport,
    Problem,
    ProductStats,
    SceneRef,
)
from kinesis.schemas.candidate import (
    ArtifactKind,
    ArtifactReference,
    CandidateMetrics,
    RepairCandidate,
    compute_candidate_id,
)
from kinesis.schemas.common import (
    ALGORITHM_VERSION,
    JOB_TRANSITIONS,
    CandidateLabel,
    CandidateStatus,
    ErrorCode,
    ErrorInfo,
    JobStatus,
    KinesisModel,
    RepairType,
    TokenUsage,
)
from kinesis.schemas.evaluation import (
    DecisionChoice,
    DecisionRequest,
    EvaluationReport,
    EvaluatorStatus,
    HumanDecision,
    ModelVisualJudgement,
    ObjectiveRank,
    VisualEvaluation,
)
from kinesis.schemas.job import JobEvent, JobEventType, PlanMode, RepairJob
from kinesis.schemas.plan import (
    DEFAULT_CANDIDATES,
    AnchorMode,
    CandidateParameters,
    ModelPlanProposal,
    PlanSource,
    RepairPlan,
)
from kinesis.schemas.scope import (
    AnimationSelection,
    ContactKind,
    ContactTarget,
    SkeletalScope,
    TemporalScope,
)

__all__ = [
    "ALGORITHM_VERSION",
    "DEFAULT_CANDIDATES",
    "JOB_TRANSITIONS",
    "AnchorMode",
    "AnimationFeatures",
    "AnimationSelection",
    "ArmatureInfo",
    "ArtifactKind",
    "ArtifactReference",
    "BoneInfo",
    "CandidateLabel",
    "CandidateMetrics",
    "CandidateParameters",
    "CandidateStatus",
    "ComponentHealth",
    "ContactKind",
    "ContactTarget",
    "CreateRepairJobRequest",
    "DecisionChoice",
    "DecisionRequest",
    "DefectReport",
    "DetectionConfig",
    "ErrorCode",
    "ErrorInfo",
    "EvaluationReport",
    "EvaluatorStatus",
    "EventPage",
    "HealthReport",
    "HumanDecision",
    "JobEvent",
    "JobEventType",
    "JobStatus",
    "KinesisModel",
    "ModelPlanProposal",
    "ModelVisualJudgement",
    "ObjectiveRank",
    "PlanMode",
    "PlanSource",
    "PlantedInterval",
    "Problem",
    "ProductStats",
    "RepairCandidate",
    "RepairJob",
    "RepairPlan",
    "RepairType",
    "SceneRef",
    "Severity",
    "SkeletalScope",
    "TemporalScope",
    "TokenUsage",
    "VisualEvaluation",
    "compute_candidate_id",
]
