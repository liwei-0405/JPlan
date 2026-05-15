# Scheduling Engine Package

This package is an internal split of the original `backend/scheduling_engine.py`. The first refactor pass is intentionally mechanical: behavior, prompts, API payloads, travel validation, and clash handling should remain unchanged.

## Module Responsibilities

- `core.py`: composes mixins and owns the public `SchedulingEngine` orchestration methods.
- `types_utils.py`: shared constants, enum-like classes, parser prompt text, time formatting/parsing, and pure helpers.
- `module_0_router.py`: lightweight chat route classification, deterministic simple-command routing, general chat templates, and non-mutating advisory replies.
- `module_a_parser.py`: Module A request parsing, parser retry/fallback, operation sanitization, and date normalization helpers.
- `location_normalizer.py`: backend location normalization and scoped explicit-location detection.
- `module_b_validation.py`: conflict, overlap, and validation helpers used by construction and operation application.
- `module_c_constructor.py`: feasibility-first schedule construction and placement helpers.
- `module_d_refinement.py`: Module D v1 deterministic refinement after Module C placement and before block materialization.
- `state_model.py`: canonical activity/state coercion, timing classification, and planner mode helpers.
- `travel_validation.py`: accurate-travel location checks, route validation, route timing retiming, and travel metadata.
- `state_operations.py`: operation normalization/application, postcondition validation, and legacy materialization helpers.
- `module_8_reply.py`: result-aware reply summary, LLM reply guardrails, and deterministic fallback replies.

## Refactor Rule

Mixin modules should not import `core.py`; `core.py` composes the mixins. Package-internal imports should stay relative. Dead-code deletion belongs in a later pass after import smoke tests and regression tests pass.

## Module D Implementation Status

Implemented in V1:
- deterministic bounded local refinement
- safe run policy
- feasible candidate relocation
- optional unscheduled insertion
- heuristic/cached travel scoring
- fixed-event preservation
- dependency-order preservation
- refinement metadata and logs

Not implemented yet / Future full ANSA:
- stochastic simulated annealing acceptance of worse solutions
- temperature schedule and cooling loop
- adaptive neighborhood move probabilities
- full swap / insert / relocate / replace move set
- replace move using candidate activity pool
- perturbation / ILS escape mechanism
- SPM-IR preference mining integration
- route-service calls inside refinement loop
- global optimality search
- long-run optimization mode

Module D v1 is an ANSA-style deterministic refinement subset. It should not be described as full ANSA until temperature-based probabilistic acceptance, adaptive move weighting, and the complete neighborhood set are implemented.
