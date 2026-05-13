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
- `state_model.py`: canonical activity/state coercion, timing classification, and planner mode helpers.
- `travel_validation.py`: accurate-travel location checks, route validation, route timing retiming, and travel metadata.
- `state_operations.py`: operation normalization/application, postcondition validation, and legacy materialization helpers.
- `module_8_reply.py`: result-aware reply summary, LLM reply guardrails, and deterministic fallback replies.

## Refactor Rule

Mixin modules should not import `core.py`; `core.py` composes the mixins. Package-internal imports should stay relative. Dead-code deletion belongs in a later pass after import smoke tests and regression tests pass.
