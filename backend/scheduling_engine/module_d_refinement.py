"""Module D v1 deterministic refinement.

Module D in the Final Combined Method is ANSA Refinement. This file implements
only a safe V1 subset: bounded deterministic local refinement after Module C
placement and before block materialization.

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

Module D v1 is an ANSA-style deterministic refinement subset. It should not be
described as full ANSA until temperature-based probabilistic acceptance,
adaptive move weighting, and the complete neighborhood set are implemented.
"""

import time
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

from jplan_logging import jlog
from .types_utils import *


REFINEMENT_META_KEYS = (
    "refinement_applied",
    "refinement_skipped_reason",
    "refinement_iterations",
    "refinement_score_before",
    "refinement_score_after",
    "refinement_accepted_moves",
    "refinement_rejected_moves",
)


class ModuleDRefinementMixin:
    def _configure_module_d_run_policy(
        self,
        preferences: Dict[str, Any],
        current_schedule: Optional[Dict[str, Any]],
        parsed: Optional[Dict[str, Any]],
        operations: Optional[List[Dict[str, Any]]],
        latest_request: str = "",
        *,
        is_apply_operations: bool = False,
    ) -> Dict[str, Any]:
        """Set the internal run reason used by Module D without changing API shape."""
        if self._module_d_disabled_by_preference(preferences):
            preferences["refinement_reason"] = "disabled_by_preference"
            return preferences

        ops = list(operations or [])
        if any(clean_title(op.get("op") or "") == "optimize_schedule" for op in ops):
            preferences["refinement_reason"] = "explicit_optimize"
            jlog(
                "MODULE_D",
                "route=explicit_optimize add_ops=0 active_before=unknown is_apply_operations="
                f"{str(is_apply_operations).lower()} reason=explicit_optimize",
                "POLICY",
            )
            return preferences

        active_before = len([
            item for item in (current_schedule or {}).get("activities", [])
            if isinstance(item, dict) and item.get("type", "activity") == "activity"
            and clean_title(item.get("status") or "active") == "active"
        ])
        add_ops = [op for op in ops if clean_title(op.get("op") or "add") == "add"]
        parsed_activities = (parsed or {}).get("activities") or []
        request_text = clean_title(latest_request or (parsed or {}).get("transcription") or "")
        router_route = (
            preferences.get("module_0_route")
            or (parsed or {}).get("module_0_route")
            or next((op.get("_router_route") for op in ops if op.get("_router_route")), None)
        )
        router_reason = (
            preferences.get("module_0_reason")
            or (parsed or {}).get("module_0_reason")
            or next((op.get("_router_reason") for op in ops if op.get("_router_reason")), None)
        )
        router_is_complex_generation = (
            router_route == "complex_schedule_command"
            or clean_title(router_reason or "") in {"multi_activity_generation", "multi_activity_redesign"}
        )
        looks_like_complex_generation = (
            active_before == 0
            and (
                (router_is_complex_generation and len(add_ops) >= 2)
                or len(add_ops) >= 2
                or len(parsed_activities) >= 2
                or (
                    len(ops) >= 2
                    and any(marker in request_text for marker in ("generate", "plan my day", "busy workday", "fit in"))
                )
            )
        )
        reason = "initial_generation" if looks_like_complex_generation else "skipped_simple_edit"
        preferences["refinement_reason"] = reason
        jlog(
            "MODULE_D",
            (
                f"route={router_route or 'unknown'} add_ops={len(add_ops)} active_before={active_before} "
                f"is_apply_operations={str(is_apply_operations).lower()} reason={reason}"
            ),
            "POLICY",
        )
        return preferences

    def _apply_module_d_refinement(
        self,
        timeline: List[Dict[str, Any]],
        unscheduled: List[Dict[str, Any]],
        day_start: int,
        day_end: int,
        min_travel: Optional[int],
        preferences: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
        before_score = self._module_d_score(timeline, unscheduled, day_start, day_end, min_travel)
        run, reason, skipped_reason = self._module_d_should_run(preferences)
        if not run:
            log_reason = "simple_edit" if skipped_reason == "skipped_simple_edit" else skipped_reason
            jlog("MODULE_D", f"reason={log_reason}", "SKIP")
            return (
                timeline,
                unscheduled,
                self._module_d_metadata(
                    applied=False,
                    skipped_reason=log_reason,
                    iterations=0,
                    score_before=before_score,
                    score_after=before_score,
                ),
            )

        jlog("MODULE_D", f"reason={reason}", "START")
        started = time.perf_counter()
        deadline = started + (MODULE_D_TIME_BUDGET_MS / 1000.0)
        current = sorted(deepcopy(timeline), key=lambda item: item.get("scheduled_start") or 0)
        remaining_unscheduled = deepcopy(unscheduled)
        current_score = before_score
        accepted_moves: List[Dict[str, Any]] = []
        rejected_moves: List[Dict[str, Any]] = []
        iterations = 0

        if any(item.get("is_conflict") for item in current):
            jlog("MODULE_D", "reason=draft_has_conflict", "NO_CANDIDATE")
            return (
                timeline,
                unscheduled,
                self._module_d_metadata(
                    applied=False,
                    skipped_reason=None,
                    iterations=0,
                    score_before=before_score,
                    score_after=before_score,
                ),
            )

        candidate_scan = []
        for item in current:
            movable, scan_reason = self._module_d_movable_reason(item, current)
            candidate_scan.append((item, movable, scan_reason))
            jlog(
                "MODULE_D",
                f"title={item.get('title')} movable={str(movable).lower()} reason={scan_reason}",
                "CANDIDATE_SCAN",
            )
        has_movable = any(movable for _, movable, _ in candidate_scan)
        has_insertable = any(self._module_d_is_insertable_unscheduled(item) for item in remaining_unscheduled)
        if not has_movable and not has_insertable:
            jlog("MODULE_D", "reason=all_movable_items_protected", "NO_CANDIDATE")
            return (
                timeline,
                unscheduled,
                self._module_d_metadata(
                    applied=False,
                    skipped_reason=None,
                    iterations=0,
                    score_before=before_score,
                    score_after=before_score,
                ),
            )

        while iterations < MODULE_D_MAX_ITERATIONS and time.perf_counter() < deadline:
            iterations += 1
            best_candidate: Optional[Tuple[float, List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]] = None

            for candidate_timeline, candidate_unscheduled, move in self._module_d_generate_candidates(
                current,
                remaining_unscheduled,
                day_start,
                day_end,
                min_travel,
            ):
                feasible, reject_reason = self._module_d_is_feasible(candidate_timeline, day_start, day_end, min_travel)
                if not feasible:
                    if len(rejected_moves) < 20:
                        rejected_moves.append({**move, "reason": reject_reason})
                    jlog("MODULE_D", f"reason={reject_reason}", "REJECT")
                    continue

                candidate_score = self._module_d_score(candidate_timeline, candidate_unscheduled, day_start, day_end, min_travel)
                delta = candidate_score - current_score
                if delta <= MODULE_D_MIN_IMPROVEMENT:
                    if len(rejected_moves) < 20:
                        rejected_moves.append({**move, "reason": "score_delta_below_threshold", "score_delta": round(delta, 4)})
                    continue
                if best_candidate is None or delta > best_candidate[0]:
                    best_candidate = (delta, candidate_timeline, candidate_unscheduled, move)

            if best_candidate is None:
                break

            delta, current, remaining_unscheduled, move = best_candidate
            current_score += delta
            accepted = {
                **move,
                "score_delta": round(delta, 4),
            }
            accepted_moves.append(accepted)
            self._module_d_append_trace(current, move)
            jlog("MODULE_D", f"moved={move.get('title')} score_delta={delta:.2f}", "ACCEPT")

        after_score = self._module_d_score(current, remaining_unscheduled, day_start, day_end, min_travel)
        applied = bool(accepted_moves)
        if not applied and (has_movable or has_insertable):
            no_candidate_reason = self._module_d_no_candidate_reason(rejected_moves)
            jlog("MODULE_D", f"reason={no_candidate_reason}", "NO_CANDIDATE")
        jlog("MODULE_D", f"before={before_score:.2f} after={after_score:.2f}", "SCORE")
        jlog("MODULE_D", f"applied={str(applied).lower()} iterations={iterations}", "DONE")
        return (
            current,
            remaining_unscheduled,
            self._module_d_metadata(
                applied=applied,
                skipped_reason=None,
                iterations=iterations,
                score_before=before_score,
                score_after=after_score,
                accepted_moves=accepted_moves,
                rejected_moves=rejected_moves,
            ),
        )

    def _module_d_should_run(self, preferences: Dict[str, Any]) -> Tuple[bool, Optional[str], Optional[str]]:
        reason = preferences.get("refinement_reason") or "skipped_simple_edit"
        if not JPLAN_ENABLE_MODULE_D:
            return False, None, "disabled_by_env"
        if self._module_d_disabled_by_preference(preferences):
            preferences["refinement_reason"] = "disabled_by_preference"
            return False, None, "disabled_by_preference"
        if reason in {"initial_generation", "explicit_optimize"}:
            return True, reason, None
        preferences["refinement_reason"] = "skipped_simple_edit"
        return False, None, "skipped_simple_edit"

    def _module_d_disabled_by_preference(self, preferences: Dict[str, Any]) -> bool:
        value = preferences.get("enable_refinement")
        if isinstance(value, str):
            return value.strip().lower() in {"0", "false", "no", "off"}
        return value is False

    def _module_d_metadata(
        self,
        *,
        applied: bool,
        skipped_reason: Optional[str],
        iterations: int,
        score_before: float,
        score_after: float,
        accepted_moves: Optional[List[Dict[str, Any]]] = None,
        rejected_moves: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        return {
            "refinement_applied": bool(applied),
            "refinement_skipped_reason": skipped_reason,
            "refinement_iterations": int(iterations),
            "refinement_score_before": round(float(score_before), 4),
            "refinement_score_after": round(float(score_after), 4),
            "refinement_accepted_moves": accepted_moves or [],
            "refinement_rejected_moves": rejected_moves or [],
        }

    def _module_d_generate_candidates(
        self,
        timeline: List[Dict[str, Any]],
        unscheduled: List[Dict[str, Any]],
        day_start: int,
        day_end: int,
        min_travel: Optional[int],
    ) -> List[Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]]:
        candidates: List[Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]] = []
        ordered = sorted(timeline, key=lambda item: item.get("scheduled_start") or 0)
        for index, item in enumerate(ordered):
            if not self._module_d_is_movable(item, ordered):
                continue
            base = [deepcopy(entry) for pos, entry in enumerate(ordered) if pos != index]
            candidate_item = deepcopy(item)
            candidate_item.pop("scheduled_start", None)
            candidate_item.pop("scheduled_end", None)
            inserted, reason = self._insert_best_position(candidate_item, base, day_start, day_end, min_travel)
            if not inserted:
                candidates.append((ordered, unscheduled, {
                    "type": "relocate",
                    "title": item.get("title"),
                    "reason": reason or "no_candidate_slot",
                }))
                continue
            placed = self._module_d_find_by_id(inserted, item)
            if not placed:
                continue
            if placed.get("scheduled_start") == item.get("scheduled_start") and placed.get("scheduled_end") == item.get("scheduled_end"):
                continue
            candidates.append((inserted, deepcopy(unscheduled), {
                "type": "relocate",
                "activity_id": item.get("stable_activity_id") or item.get("id"),
                "title": item.get("title"),
                "from": self._module_d_time_range(item),
                "to": self._module_d_time_range(placed),
            }))

        for index, item in enumerate(unscheduled):
            if not self._module_d_is_insertable_unscheduled(item):
                continue
            inserted, reason = self._insert_best_position(item, ordered, day_start, day_end, min_travel)
            if not inserted:
                candidates.append((ordered, unscheduled, {
                    "type": "insert_optional",
                    "title": item.get("title"),
                    "reason": reason or "no_candidate_slot",
                }))
                continue
            new_unscheduled = [deepcopy(entry) for pos, entry in enumerate(unscheduled) if pos != index]
            placed = self._module_d_find_by_id(inserted, item)
            candidates.append((inserted, new_unscheduled, {
                "type": "insert_optional",
                "activity_id": item.get("stable_activity_id") or item.get("id"),
                "title": item.get("title"),
                "to": self._module_d_time_range(placed or item),
            }))
        return candidates

    def _module_d_is_movable(self, item: Dict[str, Any], timeline: List[Dict[str, Any]]) -> bool:
        movable, _ = self._module_d_movable_reason(item, timeline)
        return movable

    def _module_d_movable_reason(self, item: Dict[str, Any], timeline: List[Dict[str, Any]]) -> Tuple[bool, str]:
        if item.get("is_conflict") or item.get("status") not in {None, "active"}:
            return False, "conflict_or_inactive"
        if item.get("timing_mode") == TimingMode.FIXED or item.get("fixed_start") is not None or item.get("locked_fixed"):
            return False, "fixed_or_locked"
        if item.get("preserve_scheduled_time"):
            return False, "preserved_time"
        if item.get("anchor_relation"):
            return False, "hard_anchor_relation"
        if self._module_d_is_referenced_anchor(item, timeline):
            return False, "referenced_hard_anchor"

        has_soft_preference = bool(
            item.get("preferred_start") is not None
            or item.get("earliest_start") is not None
            or item.get("latest_end") is not None
            or item.get("timing_mode") == TimingMode.PREFERRED
            or item.get("timing_mode") == TimingMode.UNSPECIFIED
            or item.get("preferred_time_window")
            or item.get("preferred_window_start") is not None
            or item.get("preferred_window_end") is not None
            or item.get("preferred_order")
            or item.get("preferred_orders")
            or item.get("soft_dependency")
        )
        if has_soft_preference:
            return True, "preferred_or_flexible"
        return False, "no_soft_preference"

    def _module_d_no_candidate_reason(self, rejected_moves: List[Dict[str, Any]]) -> str:
        reasons = {
            clean_title(move.get("reason") or "")
            for move in rejected_moves
            if move.get("reason")
        }
        if not reasons:
            return "no_score_improving_move"
        infeasible_reasons = {"overlap", "transition_buffer", "dependency_order", "day_boundary", "missing_schedule_time", "no_candidate_slot"}
        if reasons and reasons.issubset(infeasible_reasons):
            return "all_candidates_infeasible"
        return "no_score_improving_move"

    def _module_d_is_insertable_unscheduled(self, item: Dict[str, Any]) -> bool:
        return (
            item.get("status") in {None, "active"}
            and not item.get("is_mandatory", True)
            and item.get("timing_mode") != TimingMode.FIXED
            and not item.get("anchor_relation")
        )

    def _module_d_is_referenced_anchor(self, item: Dict[str, Any], timeline: List[Dict[str, Any]]) -> bool:
        item_id = str(item.get("stable_activity_id") or item.get("id") or "")
        item_title = clean_title(item.get("title") or "")
        for candidate in timeline:
            relation = candidate.get("anchor_relation") or {}
            target_id = str(relation.get("target_activity_id") or relation.get("target_id") or "")
            target_title = clean_title(relation.get("target_title") or "")
            if (item_id and target_id == item_id) or (item_title and target_title == item_title):
                return True
        return False

    def _module_d_is_feasible(
        self,
        timeline: List[Dict[str, Any]],
        day_start: int,
        day_end: int,
        min_travel: Optional[int],
    ) -> Tuple[bool, str]:
        ordered = sorted(timeline, key=lambda item: item.get("scheduled_start") or 0)
        for index, item in enumerate(ordered):
            start = item.get("scheduled_start")
            end = item.get("scheduled_end")
            if start is None or end is None:
                return False, "missing_schedule_time"
            if start < day_start or end > day_end or end <= start:
                return False, "day_boundary"
            if index == 0:
                continue
            previous = ordered[index - 1]
            if start < (previous.get("scheduled_end") or 0):
                return False, "overlap"
            required_transition = self._transition_minutes(previous, item, min_travel)
            if start < (previous.get("scheduled_end") or 0) + required_transition:
                return False, "transition_buffer"
        if not self._module_d_dependencies_satisfied(ordered):
            return False, "dependency_order"
        return True, ""

    def _module_d_dependencies_satisfied(self, timeline: List[Dict[str, Any]]) -> bool:
        for item in timeline:
            relation = item.get("anchor_relation") or {}
            if not relation:
                continue
            anchor = self._find_anchor_in_timeline(relation, timeline)
            if not anchor:
                return False
            kind = clean_title(relation.get("kind") or "after")
            if kind == "before":
                if (item.get("scheduled_end") or 0) > (anchor.get("scheduled_start") or 0):
                    return False
            else:
                if (item.get("scheduled_start") or 0) < (anchor.get("scheduled_end") or 0):
                    return False
        return True

    def _module_d_score(
        self,
        timeline: List[Dict[str, Any]],
        unscheduled: List[Dict[str, Any]],
        day_start: int,
        day_end: int,
        min_travel: Optional[int],
    ) -> float:
        ordered = sorted(timeline, key=lambda item: item.get("scheduled_start") or 0)
        score = 0.0
        priority_value = {"low": 6.0, "medium": 18.0, "high": 45.0}

        for item in ordered:
            priority = clean_title(item.get("priority") or "medium")
            score += priority_value.get(priority, 18.0)
            if item.get("is_mandatory", True):
                score += 80.0
            if item.get("preferred_start") is not None and item.get("scheduled_start") is not None:
                score -= abs(int(item["scheduled_start"]) - int(item["preferred_start"])) * 0.35
            window_start = item.get("preferred_window_start")
            window_end = item.get("preferred_window_end")
            if window_start is not None or window_end is not None:
                start = item.get("scheduled_start")
                end = item.get("scheduled_end")
                preferred_start = int(window_start if window_start is not None else day_start)
                preferred_end = int(window_end if window_end is not None else day_end)
                if start is not None and end is not None and start >= preferred_start and end <= preferred_end:
                    score += 10.0
                elif start is not None and end is not None:
                    if start < preferred_start:
                        score -= (preferred_start - start) * 0.08
                    if end > preferred_end:
                        score -= (end - preferred_end) * 0.08
            if item.get("timing_mode") == TimingMode.PREFERRED:
                score += 2.0

        for item in unscheduled:
            penalty = priority_value.get(clean_title(item.get("priority") or "medium"), 18.0)
            score -= penalty + (80.0 if item.get("is_mandatory", True) else 8.0)

        cursor = day_start
        for index, item in enumerate(ordered):
            start = item.get("scheduled_start") or cursor
            gap = max(0, start - cursor)
            if gap:
                score -= min(gap, 240) * 0.03
            if index > 0:
                previous = ordered[index - 1]
                transition = self._transition_minutes(previous, item, min_travel)
                score -= transition * 0.2
                slack = max(0, start - (previous.get("scheduled_end") or 0) - transition)
                if slack < 10:
                    score -= (10 - slack) * 0.15
            cursor = max(cursor, item.get("scheduled_end") or cursor)
        end_gap = max(0, day_end - cursor)
        score -= min(end_gap, 240) * 0.01
        return score

    def _module_d_append_trace(self, timeline: List[Dict[str, Any]], move: Dict[str, Any]) -> None:
        activity_id = str(move.get("activity_id") or "")
        for item in timeline:
            if activity_id and str(item.get("stable_activity_id") or item.get("id")) != activity_id:
                continue
            item.setdefault("trace", [])
            if MODULE_D_TRACE not in item["trace"]:
                item["trace"].append(MODULE_D_TRACE)
            return

    def _module_d_find_by_id(self, timeline: List[Dict[str, Any]], reference: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        reference_id = str(reference.get("stable_activity_id") or reference.get("id") or "")
        reference_title = clean_title(reference.get("title") or "")
        for item in timeline:
            item_id = str(item.get("stable_activity_id") or item.get("id") or "")
            if reference_id and item_id == reference_id:
                return item
            if reference_title and clean_title(item.get("title") or "") == reference_title:
                return item
        return None

    def _module_d_time_range(self, item: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
        if not item or item.get("scheduled_start") is None or item.get("scheduled_end") is None:
            return None
        return {
            "start": format_clock(item["scheduled_start"]),
            "end": format_clock(item["scheduled_end"]),
        }
