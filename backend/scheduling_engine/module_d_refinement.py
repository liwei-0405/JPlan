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

from jplan_logging import jlog, jlog_verbose
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
    def _module_d_route_breakdown(
        self,
        timeline: List[Dict[str, Any]],
        unscheduled: List[Dict[str, Any]],
        day_start: int,
        day_end: int,
        min_travel: Optional[int],
        *,
        log_revisits: bool = False,
    ) -> Dict[str, Any]:
        ordered = sorted(timeline or [], key=lambda item: item.get("scheduled_start") or 0)
        physical = [item for item in ordered if self._activity_requires_travel(item)]
        route_violations = self._route_aware_timeline_violations(ordered, day_start, day_end, min_travel)

        total_travel = 0
        total_buffer = 0
        revisit_penalty = 0
        revisit_count = 0
        same_location_split_penalty = 0
        left_locations: set[str] = set()

        def location_key(item: Dict[str, Any]) -> str:
            context = self._active_route_context()
            node = (context.get("nodes") or {}).get(self._route_context_activity_key(item)) if context else None
            coord = (node or {}).get("coordinate") or {}
            if coord.get("latitude") is not None and coord.get("longitude") is not None:
                return f"{float(coord['latitude']):.5f},{float(coord['longitude']):.5f}"
            return clean_title(item.get("location_label") or item.get("location") or item.get("title") or "")

        for left, right in zip(physical, physical[1:]):
            route_entry = self._route_context_entry(left, right)
            route_minutes = int((route_entry or {}).get("duration_minutes") or 0)
            total_travel += route_minutes
            total_buffer += max(
                int(left.get("prep_buffer", DEFAULT_PREP_BUFFER) or 0),
                int(right.get("prep_buffer", DEFAULT_PREP_BUFFER) or 0),
            )

        for index, item in enumerate(physical):
            key = location_key(item)
            if not key:
                continue
            if key in left_locations:
                revisit_count += 1
                revisit_penalty += 350
                if log_revisits:
                    jlog("MODULE_D", f"location={key} pattern=revisit", "REVISIT_PENALTY")
            next_key = location_key(physical[index + 1]) if index + 1 < len(physical) else ""
            if next_key and next_key != key:
                left_locations.add(key)

        context = self._active_route_context()
        same_groups = (context or {}).get("same_location_groups") or []
        for group in same_groups:
            keys = set(group.get("keys") or [])
            positions = [
                index for index, item in enumerate(physical)
                if self._route_context_activity_key(item) in keys
            ]
            if len(positions) < 2:
                continue
            span = max(positions) - min(positions)
            if span > len(positions) - 1:
                same_location_split_penalty += 260 * (span - (len(positions) - 1))

        total_idle = 0
        cursor = day_start
        for item in ordered:
            start = int(item.get("scheduled_start") or cursor)
            total_idle += max(0, start - cursor)
            cursor = max(cursor, int(item.get("scheduled_end") or cursor))
        total_idle += max(0, day_end - cursor)

        preferred_penalty = 0.0
        fixed_penalty = 0
        for item in ordered:
            start = item.get("scheduled_start")
            end = item.get("scheduled_end")
            preference = preference_window_deviation(item, start, end)
            preferred_penalty += float(preference.get("penalty") or 0)
            if item.get("is_user_fixed") and item.get("fixed_start") is not None and start != item.get("fixed_start"):
                fixed_penalty += 100000

        route_conflict_penalty = 100000 * len(route_violations)
        final_total = (
            -float(total_travel)
            - float(total_buffer) * 0.2
            - float(total_idle) * 0.03
            - float(preferred_penalty) * 0.4
            - float(route_conflict_penalty)
            - float(same_location_split_penalty)
            - float(revisit_penalty)
            - float(fixed_penalty)
        )
        return {
            "total_travel_minutes": total_travel,
            "total_buffer_minutes": total_buffer,
            "total_idle_minutes": total_idle,
            "preferred_window_penalty": round(float(preferred_penalty), 2),
            "route_conflict_penalty": route_conflict_penalty,
            "same_location_split_penalty": same_location_split_penalty,
            "revisit_location_penalty": revisit_penalty,
            "fixed_event_violation_penalty": fixed_penalty,
            "location_revisits_count": revisit_count,
            "final_total_score": round(final_total, 4),
        }

    def _module_d_log_score_breakdown(
        self,
        before: Dict[str, Any],
        after: Optional[Dict[str, Any]] = None,
    ) -> None:
        after = after or before
        route_before = int(before.get("total_travel_minutes") or 0)
        route_after = int(after.get("total_travel_minutes") or 0)
        saved = route_before - route_after
        jlog(
            "MODULE_D",
            (
                f"route_before={route_before} route_after={route_after} saved={saved} "
                f"same_location_before={before.get('same_location_split_penalty', 0)} "
                f"same_location_after={after.get('same_location_split_penalty', 0)} "
                f"revisit_before={before.get('revisit_location_penalty', 0)} "
                f"revisit_after={after.get('revisit_location_penalty', 0)} "
                f"idle={after.get('total_idle_minutes', 0)} "
                f"preferred_penalty={after.get('preferred_window_penalty', 0)} "
                f"total={after.get('final_total_score', 0)}"
            ),
            "SCORE_BREAKDOWN",
        )

    def _module_d_log_start_burden(
        self,
        timeline: List[Dict[str, Any]],
        day_start: int,
    ) -> None:
        context = self._active_route_context()
        if not context:
            return
        ordered = sorted(timeline or [], key=lambda item: item.get("scheduled_start") or 0)
        physical = [item for item in ordered if self._activity_requires_travel(item)]
        if not physical:
            return
        first = physical[0]
        start_entry = self._route_context_start_entry(first)
        if not start_entry:
            return
        first_start = int(first.get("scheduled_start") or 0)
        duration = int(start_entry.get("duration_minutes") or 0)
        leave_by = first_start - duration
        blockers = [
            str(item.get("title") or "Activity")
            for item in ordered
            if item is not first
            and int(item.get("scheduled_end") or 0) > leave_by
            and int(item.get("scheduled_start") or 0) < first_start
        ]
        jlog(
            "MODULE_D",
            (
                f"first_event={first.get('title')} leave_by={format_clock(leave_by)} "
                f"duration={duration} blockers={blockers} day_start={format_clock(day_start)}"
            ),
            "START_BURDEN",
        )

    def _module_d_log_same_location_order(self, timeline: List[Dict[str, Any]]) -> None:
        context = self._active_route_context()
        if not context:
            return
        ordered = sorted(timeline or [], key=lambda item: item.get("scheduled_start") or 0)
        positions = {
            self._route_context_activity_key(item): index
            for index, item in enumerate(ordered)
            if self._route_context_activity_key(item)
        }
        for group in context.get("same_location_groups") or []:
            keys = [key for key in group.get("keys", []) if key in positions]
            if len(keys) < 2:
                continue
            ordered_keys = sorted(keys, key=lambda key: positions[key])
            titles_by_key = {
                self._route_context_activity_key(item): str(item.get("title") or "Activity")
                for item in ordered
            }
            ordered_titles = [titles_by_key.get(key, key) for key in ordered_keys]
            span = max(positions[key] for key in keys) - min(positions[key] for key in keys)
            split = max(0, span - (len(keys) - 1))
            jlog(
                "MODULE_D",
                f"activities={ordered_titles} split_gap={split}",
                "SAME_LOCATION_ORDER",
            )

    def _module_d_route_efficiency_metadata(
        self,
        before: Dict[str, Any],
        after: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        after = after or before
        route_before = int(before.get("total_travel_minutes") or 0)
        route_after = int(after.get("total_travel_minutes") or 0)
        return {
            "route_total_before": route_before,
            "route_total_after": route_after,
            "route_minutes_saved": route_before - route_after,
            "same_location_split_penalty_before": before.get("same_location_split_penalty", 0),
            "same_location_split_penalty_after": after.get("same_location_split_penalty", 0),
            "revisit_penalty_before": before.get("revisit_location_penalty", 0),
            "revisit_penalty_after": after.get("revisit_location_penalty", 0),
            "location_revisits_count": after.get("location_revisits_count", 0),
        }

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
        if (
            preferences.get("refinement_reason") == "explicit_route_repair"
            or router_route in {"repair_confirmation", "explicit_route_repair"}
        ):
            preferences["refinement_reason"] = "explicit_route_repair"
            preferences["module_0_route"] = "repair_confirmation"
            jlog(
                "MODULE_D",
                (
                    f"route=repair_confirmation add_ops={len(add_ops)} active_before={active_before} "
                    f"is_apply_operations={str(is_apply_operations).lower()} reason=explicit_route_repair"
                ),
                "POLICY",
            )
            return preferences
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
        before_route_breakdown = self._module_d_route_breakdown(
            timeline,
            unscheduled,
            day_start,
            day_end,
            min_travel,
            log_revisits=True,
        )
        run, reason, skipped_reason = self._module_d_should_run(preferences)
        if not run:
            log_reason = "simple_edit" if skipped_reason == "skipped_simple_edit" else skipped_reason
            jlog("MODULE_D", f"reason={log_reason}", "SKIP")
            if self._active_route_context():
                self._module_d_log_start_burden(timeline, day_start)
                self._module_d_log_same_location_order(timeline)
                self._module_d_log_score_breakdown(before_route_breakdown)
            return (
                timeline,
                unscheduled,
                self._module_d_metadata(
                    applied=False,
                    skipped_reason=log_reason,
                    iterations=0,
                    score_before=before_score,
                    score_after=before_score,
                    route_efficiency=self._module_d_route_efficiency_metadata(before_route_breakdown)
                    if self._active_route_context() else None,
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
        candidate_stats = {"generated": 0, "pruned": 0, "validated": 0}
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
                    route_efficiency=self._module_d_route_efficiency_metadata(before_route_breakdown)
                    if self._active_route_context() else None,
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
                    route_efficiency=self._module_d_route_efficiency_metadata(before_route_breakdown)
                    if self._active_route_context() else None,
                ),
            )

        while iterations < MODULE_D_MAX_ITERATIONS and time.perf_counter() < deadline:
            iterations += 1
            best_candidate: Optional[Tuple[float, List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]] = None
            current_route_total = self._module_d_route_sequence_total(current, day_start) if self._active_route_context() else 0

            for candidate_timeline, candidate_unscheduled, move in self._module_d_generate_candidates(
                current,
                remaining_unscheduled,
                day_start,
                day_end,
                min_travel,
            ):
                candidate_stats["generated"] += 1
                feasible, reject_reason = self._module_d_is_feasible(candidate_timeline, day_start, day_end, min_travel)
                if not feasible:
                    candidate_stats["pruned"] += 1
                    if len(rejected_moves) < 20:
                        rejected_moves.append({**move, "reason": reject_reason})
                    if move.get("type") == "same_location_cluster":
                        jlog("MODULE_D", f"type=same_location_cluster reason={reject_reason} score_delta=0", "REJECT")
                    jlog("MODULE_D", f"reason={reject_reason}", "REJECT")
                    continue

                route_delta = 0
                if self._active_route_context():
                    candidate_route_total = self._module_d_route_sequence_total(candidate_timeline, day_start)
                    route_delta = candidate_route_total - current_route_total
                    move["route_total_before"] = current_route_total
                    move["route_total_after"] = candidate_route_total
                    move["route_delta"] = route_delta
                    guarded, guard_reason = self._module_d_route_total_guard_reject(move, candidate_timeline, route_delta)
                    if guarded:
                        candidate_stats["pruned"] += 1
                        if len(rejected_moves) < 20:
                            rejected_moves.append({**move, "reason": guard_reason})
                        jlog(
                            "MODULE_D",
                            (
                                f"title={move.get('title')} before={current_route_total} "
                                f"after={candidate_route_total} delta={route_delta} reason={guard_reason}"
                            ),
                            "ROUTE_TOTAL_GUARD",
                        )
                        continue

                candidate_score = self._module_d_score(candidate_timeline, candidate_unscheduled, day_start, day_end, min_travel)
                candidate_stats["validated"] += 1
                delta = candidate_score - current_score
                if delta <= MODULE_D_MIN_IMPROVEMENT:
                    candidate_stats["pruned"] += 1
                    if len(rejected_moves) < 20:
                        rejected_moves.append({**move, "reason": "score_delta_below_threshold", "score_delta": round(delta, 4)})
                    if move.get("type") == "same_location_cluster":
                        jlog(
                            "MODULE_D",
                            f"type=same_location_cluster reason=score_delta_below_threshold score_delta={delta:.4f}",
                            "REJECT",
                        )
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
            if self._active_route_context():
                jlog("MODULE_D", "reason=fixes_route_infeasibility", "ACCEPT")
            jlog("MODULE_D", f"moved={move.get('title')} score_delta={delta:.2f}", "ACCEPT")

        after_score = self._module_d_score(current, remaining_unscheduled, day_start, day_end, min_travel)
        after_route_breakdown = self._module_d_route_breakdown(
            current,
            remaining_unscheduled,
            day_start,
            day_end,
            min_travel,
            log_revisits=True,
        )
        if self._active_route_context():
            self._module_d_log_start_burden(timeline, day_start)
            self._module_d_log_same_location_order(timeline)
            self._module_d_log_same_location_order(current)
            self._module_d_log_score_breakdown(before_route_breakdown, after_route_breakdown)
        applied = bool(accepted_moves)
        if not applied and (has_movable or has_insertable):
            no_candidate_reason = self._module_d_no_candidate_reason(rejected_moves)
            jlog("MODULE_D", f"reason={no_candidate_reason}", "NO_CANDIDATE")
        jlog("TIMER", f"module_d_optimize_seconds={time.perf_counter() - started:.2f}", None)
        jlog("MODULE_D", f"before={before_score:.2f} after={after_score:.2f}", "SCORE")
        jlog("MODULE_D", f"applied={str(applied).lower()} iterations={iterations}", "DONE")
        jlog(
            "PERF",
            (
                f"generated={candidate_stats['generated']} pruned={candidate_stats['pruned']} "
                f"validated={candidate_stats['validated']}"
            ),
            "CANDIDATES",
        )
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
                route_efficiency=self._module_d_route_efficiency_metadata(before_route_breakdown, after_route_breakdown),
            ),
        )

    def _module_d_should_run(self, preferences: Dict[str, Any]) -> Tuple[bool, Optional[str], Optional[str]]:
        reason = preferences.get("refinement_reason") or "skipped_simple_edit"
        if not JPLAN_ENABLE_MODULE_D:
            return False, None, "disabled_by_env"
        if self._module_d_disabled_by_preference(preferences):
            preferences["refinement_reason"] = "disabled_by_preference"
            return False, None, "disabled_by_preference"
        if reason in {"initial_generation", "explicit_optimize", "explicit_route_repair"}:
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
        route_efficiency: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return {
            "refinement_applied": bool(applied),
            "refinement_skipped_reason": skipped_reason,
            "refinement_iterations": int(iterations),
            "refinement_score_before": round(float(score_before), 4),
            "refinement_score_after": round(float(score_after), 4),
            "refinement_accepted_moves": accepted_moves or [],
            "refinement_rejected_moves": rejected_moves or [],
            "route_efficiency": route_efficiency or {},
        }

    def _module_d_route_sequence_total(self, timeline: List[Dict[str, Any]], day_start: int) -> int:
        if not self._active_route_context():
            return 0
        ordered = sorted(timeline or [], key=lambda item: item.get("scheduled_start") or 0)
        physical = [item for item in ordered if self._activity_requires_travel(item)]
        if not physical:
            return 0
        total = 0
        first_entry = self._route_context_start_entry(physical[0])
        if first_entry:
            total += int(first_entry.get("duration_minutes") or 0)
        for left, right in zip(physical, physical[1:]):
            total += self._transition_minutes(left, right, 0)
        return int(total)

    def _module_d_route_total_guard_reject(
        self,
        move: Dict[str, Any],
        candidate_timeline: List[Dict[str, Any]],
        route_delta: int,
    ) -> Tuple[bool, str]:
        if route_delta <= 0:
            return False, ""
        target = self._module_d_find_by_activity_id(candidate_timeline, move.get("activity_id")) if move.get("activity_id") else None
        if not target:
            return False, ""
        short_low_weight = is_short_low_weight_flex_item(target)
        if short_low_weight and route_delta > max(10, int(target.get("duration_minutes") or 0)):
            return True, "low_weight_total_route_worse"
        if route_delta > 30 and move.get("type") not in {"same_location_cluster"}:
            return True, "total_route_worse"
        return False, ""

    def _module_d_find_by_activity_id(
        self,
        timeline: List[Dict[str, Any]],
        activity_id: Any,
    ) -> Optional[Dict[str, Any]]:
        if activity_id is None:
            return None
        target_key = str(activity_id)
        for item in timeline or []:
            if str(item.get("stable_activity_id") or item.get("id") or "") == target_key:
                return item
        return None

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
        candidates.extend(
            self._module_d_same_location_cluster_candidates(
                ordered,
                unscheduled,
                day_start,
                day_end,
                min_travel,
            )
        )
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

    def _module_d_same_location_cluster_candidates(
        self,
        ordered: List[Dict[str, Any]],
        unscheduled: List[Dict[str, Any]],
        day_start: int,
        day_end: int,
        min_travel: Optional[int],
    ) -> List[Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]]:
        context = self._active_route_context()
        if not context:
            return []
        groups = context.get("same_location_groups") or []
        if not groups:
            return []

        by_key = {
            self._route_context_activity_key(item): item
            for item in ordered
            if self._route_context_activity_key(item)
        }
        candidates: List[Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]] = []
        seen: set[Tuple[str, str, int, int]] = set()
        for group in groups:
            group_items = [by_key[key] for key in group.get("keys", []) if key in by_key]
            if len(group_items) < 2:
                continue
            for target in group_items:
                if not self._module_d_is_movable(target, ordered):
                    continue
                target_key = self._route_context_activity_key(target)
                duration = int(target.get("duration_minutes") or ((target.get("scheduled_end") or 0) - (target.get("scheduled_start") or 0)) or 60)
                for anchor in group_items:
                    anchor_key = self._route_context_activity_key(anchor)
                    if anchor_key == target_key:
                        continue
                    base = [deepcopy(entry) for entry in ordered if self._route_context_activity_key(entry) != target_key]
                    anchor_in_base = self._module_d_find_by_id(base, anchor)
                    if not anchor_in_base:
                        continue
                    attempts = [
                        (
                            "after",
                            int(anchor_in_base.get("scheduled_end") or 0) + self._transition_minutes(anchor_in_base, target, min_travel),
                        ),
                        (
                            "before",
                            int(anchor_in_base.get("scheduled_start") or 0) - self._transition_minutes(target, anchor_in_base, min_travel) - duration,
                        ),
                    ]
                    for position, start in attempts:
                        end = start + duration
                        if start < day_start or end > day_end:
                            continue
                        if target.get("earliest_start") is not None and start < int(target.get("earliest_start") or 0):
                            continue
                        if target.get("latest_end") is not None and end > int(target.get("latest_end") or 0):
                            continue
                        candidate_item = deepcopy(target)
                        candidate_item["scheduled_start"] = start
                        candidate_item["scheduled_end"] = end
                        candidate_timeline = sorted(base + [candidate_item], key=lambda item: item.get("scheduled_start") or 0)
                        order_rejection = self._module_d_same_location_preference_order_rejection(
                            before=ordered,
                            candidate=candidate_timeline,
                            target=target,
                            anchor=anchor,
                        )
                        if order_rejection:
                            jlog(
                                "SAME_LOCATION",
                                order_rejection,
                                "REJECT_ORDER",
                            )
                            continue
                        key = (target_key, anchor_key, start, end)
                        if key in seen:
                            continue
                        seen.add(key)
                        pref_order_log = self._module_d_same_location_preference_order_log(candidate_timeline, [target, anchor])
                        if pref_order_log:
                            jlog("SAME_LOCATION", pref_order_log, "PREF_ORDER")
                        jlog(
                            "MODULE_D",
                            f"type=same_location_cluster target={target.get('title')} anchor={anchor.get('title')}",
                            "CANDIDATE",
                        )
                        candidates.append((
                            candidate_timeline,
                            deepcopy(unscheduled),
                            {
                                "type": "same_location_cluster",
                                "activity_id": target.get("stable_activity_id") or target.get("id"),
                                "title": target.get("title"),
                                "anchor_activity_id": anchor.get("stable_activity_id") or anchor.get("id"),
                                "anchor_title": anchor.get("title"),
                                "position": position,
                                "from": self._module_d_time_range(target),
                                "to": self._module_d_time_range(candidate_item),
                                "reason": f"Clustered with same-location {anchor.get('title')}",
                            },
                        ))
        return candidates

    def _module_d_same_location_preference_order_rejection(
        self,
        *,
        before: List[Dict[str, Any]],
        candidate: List[Dict[str, Any]],
        target: Dict[str, Any],
        anchor: Dict[str, Any],
    ) -> str:
        before_by_key = {
            self._route_context_activity_key(item): item
            for item in before
            if self._route_context_activity_key(item)
        }
        candidate_by_key = {
            self._route_context_activity_key(item): item
            for item in candidate
            if self._route_context_activity_key(item)
        }
        watched = [target, anchor]
        order_rejection = self._module_d_same_location_order_rejection_by_swap(candidate, watched, target, anchor)
        if order_rejection:
            return order_rejection
        for original in watched:
            key = self._route_context_activity_key(original)
            before_item = before_by_key.get(key)
            after_item = candidate_by_key.get(key)
            if not before_item or not after_item:
                continue
            before_pref = preference_window_deviation(
                before_item,
                before_item.get("scheduled_start"),
                before_item.get("scheduled_end"),
            )
            after_pref = preference_window_deviation(
                after_item,
                after_item.get("scheduled_start"),
                after_item.get("scheduled_end"),
            )
            info = after_pref.get("info") or before_pref.get("info") or {}
            if info.get("weight") not in {"hard", "high"}:
                continue
            if int(after_pref.get("deviation") or 0) > int(before_pref.get("deviation") or 0):
                return (
                    f"order=[{target.get('title')}, {anchor.get('title')}] "
                    f"reason=worsens_preferred_window title={original.get('title')}"
                )
        return ""

    def _module_d_same_location_order_rejection_by_swap(
        self,
        candidate: List[Dict[str, Any]],
        watched: List[Dict[str, Any]],
        target: Dict[str, Any],
        anchor: Dict[str, Any],
    ) -> str:
        keys = {self._route_context_activity_key(item) for item in watched}
        ordered = [
            item for item in sorted(candidate, key=lambda entry: entry.get("scheduled_start") or 0)
            if self._route_context_activity_key(item) in keys
        ]
        if len(ordered) != 2:
            return ""
        first, second = ordered
        first_duration = int((first.get("scheduled_end") or 0) - (first.get("scheduled_start") or 0) or first.get("duration_minutes") or 60)
        second_duration = int((second.get("scheduled_end") or 0) - (second.get("scheduled_start") or 0) or second.get("duration_minutes") or 60)
        cluster_start = int(first.get("scheduled_start") or 0)
        current_penalty = self._module_d_same_location_order_penalty(ordered)
        swapped_first = deepcopy(second)
        swapped_second = deepcopy(first)
        swapped_first["scheduled_start"] = cluster_start
        swapped_first["scheduled_end"] = cluster_start + second_duration
        swapped_second["scheduled_start"] = cluster_start + second_duration
        swapped_second["scheduled_end"] = cluster_start + second_duration + first_duration
        swapped = [swapped_first, swapped_second]
        swapped_penalty = self._module_d_same_location_order_penalty(swapped)
        protected = [
            item for item in ordered
            if (preference_window_deviation(item, item.get("scheduled_start"), item.get("scheduled_end")).get("info") or {}).get("weight")
            in {"hard", "high"}
        ]
        if not protected or swapped_penalty >= current_penalty:
            return ""
        harmed = max(
            protected,
            key=lambda item: float(preference_window_deviation(item, item.get("scheduled_start"), item.get("scheduled_end")).get("penalty") or 0),
        )
        return (
            f"order={[item.get('title') for item in ordered]} "
            f"reason=worsens_preferred_window title={harmed.get('title')}"
        )

    def _module_d_same_location_order_penalty(self, ordered: List[Dict[str, Any]]) -> float:
        total = 0.0
        for item in ordered:
            preference = preference_window_deviation(item, item.get("scheduled_start"), item.get("scheduled_end"))
            total += float(preference.get("penalty") or 0)
        return total

    def _module_d_same_location_preference_order_log(
        self,
        candidate: List[Dict[str, Any]],
        items: List[Dict[str, Any]],
    ) -> str:
        item_keys = {self._route_context_activity_key(item) for item in items}
        ordered = [
            item for item in sorted(candidate, key=lambda entry: entry.get("scheduled_start") or 0)
            if self._route_context_activity_key(item) in item_keys
        ]
        if len(ordered) < 2:
            return ""
        protected = [
            item for item in ordered
            if (preference_window_deviation(item, item.get("scheduled_start"), item.get("scheduled_end")).get("info") or {}).get("weight")
            in {"hard", "high"}
        ]
        if not protected:
            return ""
        return (
            f"activities={[item.get('title') for item in ordered]} "
            f"chosen_order={[item.get('title') for item in ordered]} "
            "reason=protect_high_weight_preference"
        )

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
        infeasible_reasons = {
            "overlap",
            "transition_buffer",
            "dependency_order",
            "day_boundary",
            "missing_schedule_time",
            "no_candidate_slot",
            "route_transition",
            "start_route_blocker",
            "start_route_before_day_start",
            "fixed_to_fixed_infeasible",
            "time_window",
        }
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
        route_violations = self._route_aware_timeline_violations(ordered, day_start, day_end, min_travel)
        if route_violations:
            reason = clean_title(route_violations[0].get("reason") or "route_infeasible")
            jlog("MODULE_D", f"reason={reason}", "ROUTE_PENALTY")
            return False, reason
        for index, item in enumerate(ordered):
            start = item.get("scheduled_start")
            end = item.get("scheduled_end")
            if start is None or end is None:
                return False, "missing_schedule_time"
            if start < day_start or end > day_end or end <= start:
                return False, "day_boundary"
            if item.get("earliest_start") is not None and start < int(item.get("earliest_start") or 0):
                return False, "time_window"
            if item.get("latest_end") is not None and end > int(item.get("latest_end") or 0):
                return False, "time_window"
            preference = preference_window_deviation(item, start, end)
            if preference.get("hard_violation"):
                info = preference.get("info") or {}
                jlog(
                    "PREF_WINDOW",
                    f"title={item.get('title')} reason=hard_window_violation window={format_clock(info.get('acceptable_start'))}-{format_clock(info.get('acceptable_end'))}",
                    "REJECT",
                )
                return False, "hard_window_violation"
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

        route_violations = self._route_aware_timeline_violations(ordered, day_start, day_end, min_travel)
        if route_violations:
            score -= 100000.0 * len(route_violations)
            first_reason = route_violations[0].get("reason") or "route_infeasible"
            jlog("MODULE_D", f"reason={first_reason}", "ROUTE_PENALTY")
        if self._active_route_context():
            route_breakdown = self._module_d_route_breakdown(
                ordered,
                unscheduled,
                day_start,
                day_end,
                min_travel,
            )
            score -= float(route_breakdown.get("total_travel_minutes") or 0) * 0.8
            score -= float(route_breakdown.get("same_location_split_penalty") or 0)
            score -= float(route_breakdown.get("revisit_location_penalty") or 0)
            score -= float(route_breakdown.get("preferred_window_penalty") or 0) * 0.2
            score -= float(route_breakdown.get("fixed_event_violation_penalty") or 0)

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
            preference = preference_window_deviation(item, item.get("scheduled_start"), item.get("scheduled_end"))
            info = preference.get("info") or {}
            penalty = float(preference.get("penalty") or 0)
            if info:
                jlog_verbose(
                    "PREF_WINDOW",
                    (
                        f"title={item.get('title')} start={format_clock(item.get('scheduled_start'))} "
                        f"window={format_clock(info.get('acceptable_start'))}-{format_clock(info.get('acceptable_end'))} "
                        f"deviation={int(preference.get('deviation') or 0)} penalty={round(penalty, 2)}"
                    ),
                    "SCORE",
                )
            if penalty:
                score -= penalty
                jlog_verbose(
                    "PREF_WINDOW",
                    f"title={item.get('title')} affects_total_score=true",
                    "SCORE_APPLIED",
                )
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
