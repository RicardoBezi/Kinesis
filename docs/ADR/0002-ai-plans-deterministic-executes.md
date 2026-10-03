# ADR 0002: AI plans and evaluates; deterministic code executes

**Status:** Accepted

## Context
LLM output is non-deterministic, and it can be malformed or hallucinated. Executing model-generated code inside Blender would be a security hole, and the results could not be reproduced.

## Decision
- **Models only fill in a closed schema.** A model's only output is a `RepairPlan`. This is a Pydantic model with `extra="forbid"`, hard bounds on every numeric field, and closed enums.
- **Plans are checked against the selection.** After schema validation, `validate_plan_against_scope()` checks four things:
  - the bones are a subset of the skeletal scope;
  - the frames fall inside the temporal context;
  - the repair type is supported;
  - every parameter is in range.
- **A failed check never executes.** The orchestrator substitutes `default_plan()` (`plan_source=FALLBACK`) and records why. An invalid plan is never executed. Model output is never passed to `eval` or turned into code.
- **Evaluation is advisory.** Evaluation responses are scores on a 1–5 scale plus short notes. A model's recommendation is only a suggestion; the apply step runs only after a `HumanDecision`.
- **AI is optional.** The pipeline works end to end with no model at all (`KINESIS_PROVIDER=null`).

## Consequences
- **AI's role is deliberately limited.** It adds value by choosing between parameter regimes and by explaining and evaluating candidates. It cannot invent new operations.
- **Contract tests must cover bad responses.** That means malformed, empty, out-of-range and hallucinated-bone responses.
