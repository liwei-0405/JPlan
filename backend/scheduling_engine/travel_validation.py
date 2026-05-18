import json
import re
import time
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4
from zoneinfo import ZoneInfo

from jplan_logging import jjson, jlog, jlog_verbose, jsection
from travel_service import MissingORSApiKey, TravelService, TravelServiceError, coordinate_from_saved_location
from .types_utils import *
from .types_utils import _normalize_location

class TravelValidationMixin:
    def _apply_accurate_travel_if_requested(
        self,
        envelope: Dict[str, Any],
        saved_locations: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        validation_started = time.perf_counter()
        updated = deepcopy(envelope)
        preferences = updated.setdefault("preferences", {})
        if hasattr(self, "_normalize_day_boundary_preferences"):
            self._normalize_day_boundary_preferences(preferences)
        accurate = self._resolve_accurate_travel_time(preferences, updated)
        updated["accurate_travel_time"] = accurate
        preferences["accurate_travel_time"] = accurate
        updated.setdefault("schedule_status", updated.get("status") or "ok")

        if not accurate:
            updated["travel_validation_status"] = "not_requested"
            updated["location_resolution_requests"] = []
            self._mark_transition_estimate_source(updated, "heuristic")
            jlog("TIMER", f"accurate_travel_validation_seconds={time.perf_counter() - validation_started:.2f}", None)
            return updated

        location_requests = self._location_resolution_requests(updated, saved_locations)
        if location_requests:
            updated["schedule_status"] = "location_pending"
            updated["status"] = "location_pending"
            updated["travel_validation_status"] = "pending_locations"
            updated["location_resolution_requests"] = location_requests
            missing_coordinates = [
                req.get("title") or req.get("current_guess") or "activity"
                for req in location_requests
            ]
            jlog("TRAVEL_VALIDATION", f"missing_coordinates={missing_coordinates}", "LOCATION_PENDING")
            updated.setdefault("validation_issues", [])
            pending_titles = ", ".join(req.get("title", "activity") for req in location_requests[:4])
            updated["validation_issues"] = list(dict.fromkeys(updated["validation_issues"] + [
                f"Accurate travel time is pending location confirmation for {pending_titles}."
            ]))
            updated.setdefault("explanations", [])
            updated["explanations"] = self._merge_explanations(
                updated.get("explanations", []),
                ["Accurate travel time is pending until the requested locations are confirmed."],
            )
            jlog("TIMER", f"accurate_travel_validation_seconds={time.perf_counter() - validation_started:.2f}", None)
            return updated

        if updated.get("schedule_status") == "location_pending":
            updated["schedule_status"] = "ok"
        if updated.get("status") == "location_pending":
            updated["status"] = "ok"
        if updated.get("validation_issues"):
            updated["validation_issues"] = [
                issue for issue in updated.get("validation_issues", [])
                if "pending location confirmation" not in str(issue).lower()
                and "accurate travel time is pending" not in str(issue).lower()
            ]
        updated["explanations"] = self._without_pending_travel_explanations(updated.get("explanations", []))

        validation = self._validate_routes_with_service(updated, saved_locations)
        if validation.get("route_conflicts"):
            repair_meta = self._attempt_route_repair(updated, saved_locations, validation["route_conflicts"])
            if repair_meta:
                validation["route_repair_attempted"] = bool(repair_meta.get("route_repair_attempted", True))
                validation["pending_repair_suggestions"] = repair_meta.get("pending_repair_suggestions", [])
                validation["unfit_activities"] = repair_meta.get("unfit_activities", [])
                validation["route_repair_actions"] = repair_meta.get("route_repair_actions", [])
                validation["route_efficiency"] = repair_meta.get("route_efficiency", {})
                for key in (
                    "route_total_before",
                    "route_total_after",
                    "route_minutes_saved",
                    "location_revisits_count",
                    "same_location_split_penalty_before",
                    "same_location_split_penalty_after",
                    "revisit_penalty_before",
                    "revisit_penalty_after",
                ):
                    if key in repair_meta.get("route_efficiency", {}):
                        validation[key] = repair_meta["route_efficiency"][key]
                validation["travel_validation_status"] = repair_meta.get(
                    "travel_validation_status",
                    validation.get("travel_validation_status"),
                )
                if repair_meta.get("start_route_summary"):
                    validation["start_route_summary"] = repair_meta.get("start_route_summary")
                repaired_validation = repair_meta.get("repaired_validation")
                repaired_envelope = repair_meta.get("repaired_envelope")
                if repaired_validation and repaired_envelope and not validation["pending_repair_suggestions"]:
                    validation["schedule_blocks"] = repaired_validation.get(
                        "schedule_blocks",
                        repaired_envelope.get("schedule_blocks", validation.get("schedule_blocks", [])),
                    )
                    validation["activities"] = repaired_envelope.get("activities", updated.get("activities", []))
                    validation["unscheduled_activities"] = repaired_envelope.get(
                        "unscheduled_activities",
                        updated.get("unscheduled_activities", []),
                    )
                    validation["route_conflicts"] = repaired_validation.get("route_conflicts", [])
                    validation["warnings"] = repaired_validation.get("warnings", validation.get("warnings", []))
                    validation["updated_transition_count"] = repaired_validation.get(
                        "updated_transition_count",
                        validation.get("updated_transition_count", 0),
                    )
                    if repaired_validation.get("start_route_summary"):
                        validation["start_route_summary"] = repaired_validation.get("start_route_summary")
        updated["location_resolution_requests"] = []
        updated["travel_validation_status"] = validation["travel_validation_status"]
        updated["route_conflicts"] = validation.get("route_conflicts", [])
        updated["route_repair_attempted"] = bool(validation.get("route_repair_attempted", False))
        updated["pending_repair_suggestions"] = validation.get("pending_repair_suggestions", [])
        updated["unfit_activities"] = validation.get("unfit_activities", [])
        updated["route_repair_actions"] = validation.get("route_repair_actions", [])
        updated["route_efficiency"] = validation.get("route_efficiency", updated.get("route_efficiency", {}))
        for key in (
            "route_total_before",
            "route_total_after",
            "route_minutes_saved",
            "location_revisits_count",
            "same_location_split_penalty_before",
            "same_location_split_penalty_after",
            "revisit_penalty_before",
            "revisit_penalty_after",
        ):
            if key in validation:
                updated[key] = validation[key]
        updated["start_route_summary"] = validation.get("start_route_summary") or updated.get("start_route_summary")
        if validation.get("activities"):
            updated["activities"] = validation.get("activities", updated.get("activities", []))
        if validation.get("unscheduled_activities") is not None:
            updated["unscheduled_activities"] = validation.get(
                "unscheduled_activities",
                updated.get("unscheduled_activities", []),
            )
        updated["updated_transition_count"] = int(validation.get("updated_transition_count") or 0)
        updated["schedule_blocks"] = validation.get("schedule_blocks", updated.get("schedule_blocks", []))
        self._sync_resolved_locations_to_activities(updated)
        if validation.get("warnings"):
            updated["warnings"] = list(updated.get("warnings") or []) + validation["warnings"]
            if updated.get("status") == "ok":
                updated["status"] = "warning"
            if updated.get("schedule_status") == "ok":
                updated["schedule_status"] = "warning"

        if updated.get("travel_validation_status") == "repair_suggestion_pending":
            updated["schedule_status"] = "warning"
            updated["status"] = "warning"
            updated.setdefault("validation_issues", [])
            updated["validation_issues"] = list(dict.fromkeys(
                updated["validation_issues"] + [
                    conflict.get("reason")
                    for conflict in validation.get("route_conflicts", [])
                    if conflict.get("reason")
                ]
            ))
        elif updated.get("travel_validation_status") == "partial_feasible_with_unfit":
            updated["schedule_status"] = "partial"
            updated["status"] = "partial"
            updated.setdefault("validation_issues", [])
            for item in updated.get("unfit_activities") or []:
                title = item.get("title") or "One flexible activity"
                updated["validation_issues"].append(f"{title} could not fit after accurate travel validation.")
            updated["validation_issues"] = list(dict.fromkeys(updated["validation_issues"]))
        elif updated.get("travel_validation_status") == "repaired_validated":
            updated["schedule_status"] = "ok"
            updated["status"] = "ok"
            updated.setdefault("explanations", [])
            actions = updated.get("route_repair_actions") or []
            action_titles = ", ".join(
                str(action.get("title"))
                for action in actions[:3]
                if action.get("title")
            )
            message = (
                f"Adjusted for accurate travel: {action_titles} shifted to route-safe times."
                if action_titles
                else "Adjusted the plan for accurate travel time."
            )
            updated["explanations"] = self._merge_explanations(updated.get("explanations", []), [message])
        elif validation.get("route_conflicts"):
            updated["schedule_status"] = "route_conflict"
            updated["status"] = "route_conflict"
            updated.setdefault("validation_issues", [])
            updated["validation_issues"] = list(dict.fromkeys(
                updated["validation_issues"] + [
                    conflict.get("reason")
                    for conflict in validation["route_conflicts"]
                    if conflict.get("reason")
                ]
            ))
        else:
            updated["schedule_status"] = updated.get("schedule_status") or updated.get("status") or "ok"
            updated["explanations"] = self._merge_explanations(
                updated.get("explanations", []),
                self._travel_validation_explanations(updated.get("travel_validation_status")),
            )
        self._debug(
            f"[TRAVEL][VALIDATION] travel_validation_status={updated.get('travel_validation_status')}"
        )
        self._debug(
            f"[TRAVEL][COMPLETE] Updated transition blocks: {updated.get('updated_transition_count', 0)}"
        )
        jlog("TIMER", f"accurate_travel_validation_seconds={time.perf_counter() - validation_started:.2f}", None)
        return updated

    def _without_pending_travel_explanations(self, explanations: List[str]) -> List[str]:
        stale_markers = (
            "accurate travel time is pending",
            "pending until the requested locations are confirmed",
            "pending location confirmation",
        )
        return [
            explanation
            for explanation in explanations or []
            if not any(marker in str(explanation).lower() for marker in stale_markers)
        ]

    def _travel_validation_explanations(self, status: Optional[str]) -> List[str]:
        if status in {"validated", "repaired_validated"}:
            return [
                "Accurate travel time has been validated using the routing service.",
                "Travel blocks were updated using route-based durations.",
            ]
        if status == "fallback_used":
            return [
                "Accurate travel time used confirmed coordinates, but route service fallback estimates were needed.",
                "Travel blocks were updated using the available route-duration estimates.",
            ]
        return []

    def _build_route_context(
        self,
        envelope: Dict[str, Any],
        activities: List[Dict[str, Any]],
        saved_locations: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        preferences = envelope.get("preferences") or {}
        transport_mode = preferences.get("transport_mode") or "driving-car"
        nodes: Dict[str, Dict[str, Any]] = {}
        missing: List[Dict[str, Any]] = []
        fallback_used = False

        for item in activities or []:
            if item.get("status") not in {None, "active"}:
                continue
            if not self._block_requires_travel_coordinate(item):
                continue
            key = self._route_context_activity_key(item)
            if not key:
                continue
            coord, resolved = self._activity_coordinate_resolution(item, saved_locations)
            if not coord:
                missing.append({
                    "title": item.get("title"),
                    "location": item.get("location") or item.get("location_label"),
                })
                continue
            node_item = deepcopy(item)
            self._apply_resolved_location_to_block(node_item, resolved)
            nodes[key] = {
                "key": key,
                "activity_id": item.get("stable_activity_id") or item.get("id"),
                "title": item.get("title"),
                "location": node_item.get("location") or node_item.get("location_label"),
                "location_label": node_item.get("location_label") or node_item.get("location"),
                "coordinate": {"latitude": coord[0], "longitude": coord[1]},
                "item": node_item,
            }

        pairs: Dict[str, Dict[str, Any]] = {}
        date_prefix = envelope.get("date") or self._local_today_iso()
        for left_key, left in nodes.items():
            for right_key, right in nodes.items():
                if left_key == right_key:
                    continue
                pair_key = f"{left_key}->{right_key}"
                left_coord = (left["coordinate"]["latitude"], left["coordinate"]["longitude"])
                right_coord = (right["coordinate"]["latitude"], right["coordinate"]["longitude"])
                if left_coord == right_coord:
                    route_minutes = 0
                    source = "same_location"
                else:
                    right_start = parse_clock((right.get("item") or {}).get("start") or (right.get("item") or {}).get("startTime") or "")
                    time_bucket = f"{date_prefix}T{format_clock(right_start)}" if right_start is not None else None
                    try:
                        jlog_verbose(
                            "TRAVEL_SERVICE",
                            f"route request: {left.get('location_label') or left.get('title')} -> {right.get('location_label') or right.get('title')}",
                            "ORS",
                        )
                        route_minutes = self.travel_service.route_minutes(
                            left_coord,
                            right_coord,
                            transport_mode=transport_mode,
                            time_bucket=time_bucket,
                        )
                        source = "routing_service"
                        jlog_verbose(
                            "TRAVEL_SERVICE",
                            f"route result: {left.get('location_label') or left.get('title')} -> {right.get('location_label') or right.get('title')} = {route_minutes} min",
                            "ORS",
                        )
                    except (MissingORSApiKey, TravelServiceError, Exception):
                        route_minutes = estimate_travel_minutes(left.get("location"), right.get("location"))
                        source = "fallback"
                        fallback_used = True
                pairs[pair_key] = {
                    "from_key": left_key,
                    "to_key": right_key,
                    "from_title": left.get("title"),
                    "to_title": right.get("title"),
                    "from_location": left.get("location"),
                    "to_location": right.get("location"),
                    "duration_minutes": int(route_minutes or 0),
                    "source": source,
                    "transport_mode": transport_mode,
                    "from_coordinate": deepcopy(left.get("coordinate")),
                    "to_coordinate": deepcopy(right.get("coordinate")),
                }

        parent = {key: key for key in nodes}

        def find(key: str) -> str:
            while parent[key] != key:
                parent[key] = parent[parent[key]]
                key = parent[key]
            return key

        def union(left_key: str, right_key: str) -> None:
            left_root = find(left_key)
            right_root = find(right_key)
            if left_root != right_root:
                parent[right_root] = left_root

        for pair in pairs.values():
            if int(pair.get("duration_minutes") or 0) <= 2:
                union(str(pair.get("from_key")), str(pair.get("to_key")))

        grouped_keys: Dict[str, List[str]] = {}
        for key in nodes:
            grouped_keys.setdefault(find(key), []).append(key)
        same_location_groups: List[Dict[str, Any]] = []
        for keys in grouped_keys.values():
            if len(keys) < 2:
                continue
            titles = [nodes[key].get("title") for key in keys if nodes.get(key)]
            same_location_groups.append({
                "keys": keys,
                "titles": titles,
                "travel_minutes": 0,
            })
            jlog(
                "MODULE_C",
                f"activities=[{', '.join(str(title) for title in titles if title)}] travel=0",
                "SAME_LOCATION_GROUP",
            )

        start_routes: Dict[str, Dict[str, Any]] = {}
        start_coord, start_resolved = self._start_location_coordinate_resolution(preferences, saved_locations)
        if start_coord and start_resolved:
            start_label = (
                start_resolved.get("display_name")
                or start_resolved.get("label")
                or "Starting point"
            )
            jlog("DEFAULT_LOCATION", f"start={start_label}", None)
            for key, node in nodes.items():
                node_coord = (node["coordinate"]["latitude"], node["coordinate"]["longitude"])
                if start_coord == node_coord:
                    route_minutes = 0
                    source = "same_location"
                else:
                    try:
                        jlog_verbose(
                            "TRAVEL_SERVICE",
                            f"route request: {start_label} -> {node.get('location_label') or node.get('title')}",
                            "ORS",
                        )
                        route_minutes = self.travel_service.route_minutes(
                            start_coord,
                            node_coord,
                            transport_mode=transport_mode,
                            time_bucket=None,
                        )
                        source = "routing_service"
                        jlog_verbose(
                            "TRAVEL_SERVICE",
                            f"route result: {start_label} -> {node.get('location_label') or node.get('title')} = {route_minutes} min",
                            "ORS",
                        )
                    except (MissingORSApiKey, TravelServiceError, Exception):
                        route_minutes = estimate_travel_minutes(start_label, node.get("location"))
                        source = "fallback"
                        fallback_used = True
                start_routes[key] = {
                    "to_key": key,
                    "to_title": node.get("title"),
                    "to_location": node.get("location"),
                    "start_location": start_label,
                    "duration_minutes": int(route_minutes or 0),
                    "source": source,
                    "transport_mode": transport_mode,
                    "from_coordinate": {"latitude": start_coord[0], "longitude": start_coord[1]},
                    "to_coordinate": deepcopy(node.get("coordinate")),
                }

        jlog("ROUTE_CONTEXT", f"pairs={len(pairs)} missing={missing}", None)
        return {
            "enabled": True,
            "transport_mode": transport_mode,
            "nodes": nodes,
            "pairs": pairs,
            "start_routes": start_routes,
            "same_location_groups": same_location_groups,
            "missing": missing,
            "fallback_used": fallback_used,
        }

    def _attempt_route_repair(
        self,
        envelope: Dict[str, Any],
        saved_locations: List[Dict[str, Any]],
        route_conflicts: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        required_minutes = [
            int(conflict.get("required_route_minutes") or conflict.get("required_travel_minutes") or 0)
            for conflict in route_conflicts or []
        ]
        if not required_minutes:
            return None
        repair_source = deepcopy(envelope)
        self._sync_resolved_locations_to_activities(repair_source)
        preferences = deepcopy(repair_source.get("preferences") or {})
        preferences["refinement_reason"] = "explicit_optimize"
        preferences["route_aware_repair"] = True
        active = [
            item for item in self._load_canonical_activities(repair_source)
            if item.get("status") == "active"
        ]
        if not active:
            return None
        route_context = self._build_route_context(repair_source, active, saved_locations)
        preferences["_route_context"] = route_context
        start_route_constraints = self._start_route_repair_constraints(route_conflicts)
        repair_active = [
            self._prepare_activity_for_route_repair(item, start_route_constraints)
            for item in active
        ]
        jlog("TRAVEL_REPAIR", "start", "MODULE_C")
        jlog("TRAVEL_REPAIR", "start", "MODULE_D")
        planned = self._plan_schedule(envelope.get("date") or self._local_today_iso(), repair_active, preferences)
        jlog(
            "TRAVEL_REPAIR",
            f"applied={str(bool(planned.get('refinement_applied'))).lower()}",
            "MODULE_D",
        )
        planned_activities = list(planned.get("activities", []))
        conflict_unfit = [
            item for item in planned_activities
            if item.get("is_conflict") and self._activity_can_auto_move_for_route_repair(item)
        ]
        if conflict_unfit:
            removed_ids = {
                str(item.get("stable_activity_id") or item.get("id") or "")
                for item in conflict_unfit
            }
            planned_activities = [
                item for item in planned_activities
                if str(item.get("stable_activity_id") or item.get("id") or "") not in removed_ids
            ]
            for item in planned_activities:
                conflict_with = [
                    conflict_id for conflict_id in (item.get("conflict_with") or [])
                    if str(conflict_id) not in removed_ids
                ]
                item["conflict_with"] = conflict_with
                if not conflict_with:
                    item.pop("is_conflict", None)
                    item.pop("conflict_reason", None)
                    item.pop("conflict_severity", None)
                    item.pop("conflict_priority", None)
        repaired = deepcopy(envelope)
        repaired["preferences"] = preferences
        repaired["activities"] = [self._format_activity(item) for item in planned_activities]
        if conflict_unfit:
            repaired["schedule_blocks"] = self._materialize_blocks(
                planned_activities,
                parse_clock(planned.get("day_start") or preferences.get("day_start") or preferences.get("day_start_time") or "") or DEFAULT_DAY_START,
                int(preferences.get("min_travel_buffer_minutes") or 0),
            )
        else:
            repaired["schedule_blocks"] = planned.get("schedule_blocks", [])
        jlog("SUPPORT_BLOCK", f"blocks={len(repaired.get('schedule_blocks') or [])}", "REBUILD_FINAL")
        repaired["unscheduled_activities"] = [
            self._format_activity(item)
            for item in list(planned.get("unscheduled_activities", [])) + conflict_unfit
        ]
        validation = self._final_validate_route_aware_repair(
            original=envelope,
            repaired=repaired,
            route_context=route_context,
        )
        repaired_preferences = deepcopy(repaired.get("preferences") or {})
        repaired_preferences.pop("_route_context", None)
        repaired_preferences.pop("route_aware_repair", None)
        repaired["preferences"] = repaired_preferences
        semantic_violations = self._protected_semantic_violations(envelope, repaired)
        if semantic_violations:
            validation.setdefault("route_conflicts", [])
            validation["route_conflicts"].extend(semantic_violations)
            validation["travel_validation_status"] = "route_conflict"
        suggestions = self._protected_repair_suggestions_from_conflicts(envelope, route_conflicts)
        unfit = self._repair_unfit_activities(
            active,
            list(planned.get("unscheduled_activities", [])) + conflict_unfit,
            validation.get("route_conflicts", []),
        )
        route_repair_actions = self._route_repair_actions_from_delta(
            envelope,
            repaired,
            route_context,
        )
        route_efficiency = planned.get("route_efficiency") or {}
        if suggestions:
            status = "repair_suggestion_pending"
        elif not validation.get("route_conflicts") and unfit:
            status = "partial_feasible_with_unfit"
        elif not validation.get("route_conflicts") and route_repair_actions:
            status = "repaired_validated"
        else:
            status = validation.get("travel_validation_status") or "route_conflict"
        validation["travel_validation_status"] = status
        validation["route_repair_actions"] = route_repair_actions
        validation["route_efficiency"] = route_efficiency
        self._log_route_repair_actions(route_repair_actions)
        for key in (
            "route_total_before",
            "route_total_after",
            "route_minutes_saved",
            "location_revisits_count",
            "same_location_split_penalty_before",
            "same_location_split_penalty_after",
            "revisit_penalty_before",
            "revisit_penalty_after",
        ):
            if key in route_efficiency:
                validation[key] = route_efficiency[key]
        self._log_repaired_final_schedule(repaired, status)
        jlog("TRAVEL_REPAIR", f"status={status}", "FINAL_VALIDATE")
        return {
            "route_repair_attempted": True,
            "pending_repair_suggestions": suggestions,
            "unfit_activities": unfit,
            "travel_validation_status": status,
            "route_repair_actions": route_repair_actions,
            "route_efficiency": route_efficiency,
            "repaired_envelope": repaired if not suggestions and not validation.get("route_conflicts") else None,
            "repaired_validation": validation if not suggestions and not validation.get("route_conflicts") else None,
            "start_route_summary": validation.get("start_route_summary"),
        }

    def _log_route_repair_actions(self, actions: List[Dict[str, Any]]) -> None:
        if not actions:
            jlog("ROUTE_REPAIR_ACTIONS", "count=0", None)
            return
        lines = [f"count={len(actions)}"]
        for action in actions:
            title = action.get("title") or "Activity"
            from_time = action.get("from") or "?"
            to_time = action.get("to") or "?"
            reason = action.get("reason") or "route repair"
            lines.append(f"- {title}: {from_time} -> {to_time} | {reason}")
        jlog("ROUTE_REPAIR_ACTIONS", "\n".join(lines), None)

    def _log_repaired_final_schedule(self, repaired: Dict[str, Any], status: str) -> None:
        blocks = repaired.get("schedule_blocks") or []
        jlog("SUMMARY", f"Repaired final schedule status={status}", "REPAIRED_FINAL")
        for block in blocks:
            start = block.get("start") or block.get("startTime") or "?"
            end = block.get("end") or block.get("endTime") or "?"
            title = block.get("title") or block.get("display_label") or "Block"
            block_type = block.get("block_type") or block.get("type")
            suffix = ""
            if block_type == "activity" and block.get("location"):
                suffix = f" ({block.get('location')})"
            elif block_type in {"transition", "travel"}:
                duration = block.get("duration_minutes") or block.get("route_duration_minutes")
                suffix = f" ({duration} min)" if duration is not None else ""
            jlog("SUMMARY", f"- [{start} - {end}] {title}{suffix}", "REPAIRED_FINAL")

    def _prepare_activity_for_route_repair(
        self,
        item: Dict[str, Any],
        start_route_constraints: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        prepared = deepcopy(item)
        if self._activity_can_auto_move_for_route_repair(prepared):
            prepared["preserve_scheduled_time"] = False
            prepared["locked_fixed"] = False
            prepared["can_move_for_repair"] = True
            if prepared.get("timing_mode") == TimingMode.FIXED:
                prepared["timing_mode"] = prepared.get("original_timing_mode") or TimingMode.UNSPECIFIED
            if prepared.get("timing_mode") == TimingMode.FIXED:
                prepared["timing_mode"] = TimingMode.UNSPECIFIED
            prepared["fixed_start"] = None
            prepared["fixed_end"] = None
            prepared["scheduled_start"] = None
            prepared["scheduled_end"] = None
            constraint = self._start_route_constraint_for_activity(prepared, start_route_constraints or {})
            if constraint:
                earliest = int(constraint.get("earliest_start") or 0)
                prepared["earliest_start"] = max(int(prepared.get("earliest_start") or 0), earliest)
                prepared.setdefault("trace", [])
                prepared["trace"].append(
                    f"Route repair moved this before-first-event task after {constraint.get('first_event_title')} travel leave-by."
                )
            return prepared

        prepared["can_move_for_repair"] = False
        prepared["locked_fixed"] = True
        if prepared.get("fixed_start") is None and prepared.get("scheduled_start") is not None:
            prepared["fixed_start"] = prepared.get("scheduled_start")
        if prepared.get("fixed_end") is None and prepared.get("scheduled_end") is not None:
            prepared["fixed_end"] = prepared.get("scheduled_end")
        prepared["timing_mode"] = TimingMode.FIXED
        return prepared

    def _activity_repair_protection(self, item: Dict[str, Any]) -> str:
        protection = clean_title(item.get("repair_protection") or item.get("repairProtection") or "")
        if protection in {"fixed", "protected_social", "flexible", "optional"}:
            return protection
        is_mandatory = bool(item.get("is_mandatory", item.get("isMandatory", True)))
        return self._infer_repair_protection(
            item,
            item.get("title") or "",
            item.get("timing_mode"),
            item.get("fixed_start"),
            bool(item.get("is_user_fixed")),
            is_mandatory,
        )

    def _activity_can_move_for_route_repair(self, item: Dict[str, Any]) -> bool:
        raw_can_move = item.get("can_move_for_repair")
        if raw_can_move is not None and not bool(raw_can_move):
            return False
        if item.get("is_user_fixed") or item.get("user_fixed_start") is not None:
            return False
        if item.get("timing_mode") == TimingMode.FIXED or item.get("fixed_start") is not None:
            return False
        if item.get("anchor_relation"):
            return False
        return True

    def _activity_can_auto_move_for_route_repair(self, item: Dict[str, Any]) -> bool:
        if not self._activity_can_move_for_route_repair(item):
            return False
        protection = self._activity_repair_protection(item)
        if protection in {"flexible", "optional"}:
            return True
        if protection == "protected_social":
            return self._protected_social_can_auto_move(item)
        return False

    def _activity_requires_repair_confirmation(self, item: Dict[str, Any]) -> bool:
        protection = self._activity_repair_protection(item)
        if protection == "fixed":
            return True
        if protection == "protected_social":
            return not self._protected_social_can_auto_move(item)
        return False

    def _protected_social_can_auto_move(self, item: Dict[str, Any]) -> bool:
        return not (
            item.get("is_user_fixed")
            or item.get("user_fixed_start") is not None
            or item.get("timing_mode") == TimingMode.FIXED
            or item.get("fixed_start") is not None
        )

    def _start_route_repair_constraints(
        self,
        route_conflicts: List[Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        constraints: Dict[str, Dict[str, Any]] = {}
        for conflict in route_conflicts or []:
            if conflict.get("reason_code") != "start_route_blocker":
                continue
            blocker_id = str(conflict.get("blocker_activity_id") or "")
            blocker_title = clean_title(conflict.get("blocker_activity_title") or conflict.get("from_activity") or "")
            earliest = parse_clock(conflict.get("first_physical_event_end") or "")
            if earliest is None:
                continue
            constraint = {
                "earliest_start": earliest,
                "first_event_title": conflict.get("to_activity") or conflict.get("first_physical_event"),
            }
            if blocker_id:
                constraints[f"id:{blocker_id}"] = constraint
            if blocker_title:
                constraints[f"title:{blocker_title}"] = constraint
        return constraints

    def _start_route_constraint_for_activity(
        self,
        item: Dict[str, Any],
        constraints: Dict[str, Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        activity_id = str(item.get("stable_activity_id") or item.get("id") or "")
        if activity_id and f"id:{activity_id}" in constraints:
            return constraints[f"id:{activity_id}"]
        title = clean_title(item.get("title") or "")
        if title and f"title:{title}" in constraints:
            return constraints[f"title:{title}"]
        return None

    def _activity_records_for_repair(self, envelope: Dict[str, Any]) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        for collection in (envelope.get("activities") or [], envelope.get("schedule_blocks") or []):
            for source in collection:
                if not isinstance(source, dict):
                    continue
                if (source.get("block_type") or source.get("type") or "activity") not in {"activity", None}:
                    continue
                if source.get("block_type") and source.get("block_type") != "activity":
                    continue
                start = parse_clock(source.get("start") or source.get("startTime") or "")
                end = parse_clock(source.get("end") or source.get("endTime") or "")
                if start is None or end is None:
                    continue
                record = deepcopy(source)
                record["_repair_start"] = start
                record["_repair_end"] = end if end > start else end + 24 * 60
                records.append(record)
        return records

    def _repair_record_key(self, item: Dict[str, Any]) -> Tuple[str, str]:
        return (
            str(item.get("stable_activity_id") or item.get("id") or ""),
            clean_title(item.get("title") or ""),
        )

    def _find_repair_record(self, records: List[Dict[str, Any]], *, activity_id: Optional[str] = None, title: Optional[str] = None) -> Optional[Dict[str, Any]]:
        clean = clean_title(title or "")
        if activity_id:
            for record in records:
                if str(record.get("stable_activity_id") or record.get("id") or "") == str(activity_id):
                    return record
        if clean:
            for record in records:
                if clean_title(record.get("title") or "") == clean:
                    return record
        return None

    def _repair_suggestions_from_candidate(
        self,
        envelope: Dict[str, Any],
        repaired: Dict[str, Any],
        route_conflicts: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        original_records = self._activity_records_for_repair(envelope)
        repaired_records = self._activity_records_for_repair(repaired)
        suggestions: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()

        for index, conflict in enumerate(route_conflicts or [], start=1):
            target_title = conflict.get("to_activity") or conflict.get("to")
            target = self._find_repair_record(original_records, title=target_title)
            if not target or not self._activity_can_auto_move_for_route_repair(target):
                continue
            activity_id = str(target.get("stable_activity_id") or target.get("id") or "")
            if activity_id in seen_ids:
                continue
            candidate = self._find_repair_record(repaired_records, activity_id=activity_id, title=target_title)
            if not candidate:
                continue
            if candidate["_repair_start"] == target["_repair_start"] and candidate["_repair_end"] == target["_repair_end"]:
                continue
            required = int(conflict.get("required_route_minutes") or conflict.get("required_travel_minutes") or 0)
            from_end = parse_clock(conflict.get("from_end") or "")
            suggested_start = candidate["_repair_start"]
            duration = max(1, target["_repair_end"] - target["_repair_start"])
            if from_end is not None and required > 0:
                suggested_start = max(target["_repair_start"], from_end + required)
            suggested_end = suggested_start + duration
            suggestion = {
                "id": f"suggestion_{len(suggestions) + 1}",
                "type": "move_activity",
                "activity_id": activity_id,
                "title": target.get("title") or target_title,
                "from": format_clock(target["_repair_start"]),
                "from_end": format_clock(target["_repair_end"]),
                "to": format_clock(suggested_start),
                "to_end": format_clock(suggested_end),
                "to_start_minutes": suggested_start,
                "to_end_minutes": suggested_end,
                "duration_minutes": duration,
                "reason": f"Travel from {conflict.get('from_activity') or conflict.get('from')} takes about {required} minutes",
                "impact": "minimal_shift",
                "requires_user_confirmation": True,
                "schedule_version": int(envelope.get("version") or 1),
                "route_conflict_index": index - 1,
            }
            suggestions.append(suggestion)
            seen_ids.add(activity_id)
            jlog("PENDING_REPAIR", f"id={suggestion['id']} action=created title={suggestion.get('title')}", None)
        return suggestions

    def _protected_repair_suggestions_from_conflicts(
        self,
        envelope: Dict[str, Any],
        route_conflicts: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        records = self._activity_records_for_repair(envelope)
        suggestions: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        for index, conflict in enumerate(route_conflicts or [], start=1):
            if conflict.get("reason_code") == "start_route_blocker":
                continue
            target_title = conflict.get("to_activity") or conflict.get("to")
            target = self._find_repair_record(
                records,
                activity_id=conflict.get("destination_activity_id"),
                title=target_title,
            )
            if not target or not self._activity_requires_repair_confirmation(target):
                continue
            activity_id = str(target.get("stable_activity_id") or target.get("id") or "")
            if activity_id and activity_id in seen_ids:
                continue
            from_end = parse_clock(conflict.get("from_end") or "")
            required = int(conflict.get("required_route_minutes") or conflict.get("required_travel_minutes") or 0)
            if from_end is None or required <= 0:
                continue
            duration = max(1, target["_repair_end"] - target["_repair_start"])
            suggested_start = max(target["_repair_start"], from_end + required)
            suggested_end = suggested_start + duration
            title = target.get("title") or target_title or "activity"
            protection = self._activity_repair_protection(target)
            if protection == "protected_social":
                reason = f"Travel from {conflict.get('from_activity') or conflict.get('from')} takes about {required} minutes"
            else:
                reason = f"Can I move {title} to make room for about {required} minutes of accurate travel time?"
            suggestion = {
                "id": f"suggestion_{len(suggestions) + 1}",
                "type": "move_activity",
                "activity_id": activity_id,
                "title": title,
                "from": format_clock(target["_repair_start"]),
                "from_end": format_clock(target["_repair_end"]),
                "to": format_clock(suggested_start),
                "to_end": format_clock(suggested_end),
                "to_start_minutes": suggested_start,
                "to_end_minutes": suggested_end,
                "duration_minutes": duration,
                "reason": reason,
                "impact": "minimal_shift",
                "requires_user_confirmation": True,
                "schedule_version": int(envelope.get("version") or 1),
                "route_conflict_index": index - 1,
                "repair_protection": protection,
            }
            suggestions.append(suggestion)
            if activity_id:
                seen_ids.add(activity_id)
            jlog("PENDING_REPAIR", f"id={suggestion['id']} action=created title={title}", None)
        return suggestions

    def _protected_semantic_violations(
        self,
        envelope: Dict[str, Any],
        repaired: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        original_records = self._activity_records_for_repair(envelope)
        repaired_records = self._activity_records_for_repair(repaired)
        violations: List[Dict[str, Any]] = []
        for original in original_records:
            if self._activity_repair_protection(original) != "protected_social":
                continue
            title = original.get("title") or "activity"
            if "dinner" not in clean_title(title or ""):
                continue
            candidate = self._find_repair_record(
                repaired_records,
                activity_id=original.get("stable_activity_id") or original.get("id"),
                title=title,
            )
            if not candidate:
                continue
            if candidate["_repair_start"] < 17 * 60:
                jlog(
                    "TRAVEL_REPAIR",
                    f"title={title} reason=violates_evening_or_social_commitment",
                    "REJECT_CANDIDATE",
                )
                violations.append({
                    "type": "route_conflict",
                    "from": "Route repair",
                    "to": title,
                    "from_activity": "Route repair",
                    "to_activity": title,
                    "from_end": format_clock(candidate["_repair_start"]),
                    "to_start": format_clock(candidate["_repair_start"]),
                    "available_minutes": 0,
                    "available_gap_minutes": 0,
                    "required_route_minutes": 0,
                    "required_travel_minutes": 0,
                    "destination_movable": False,
                    "destination_activity_id": candidate.get("stable_activity_id") or candidate.get("id"),
                    "reason": f"{title} is a protected evening/social commitment and should not be moved before 5:00 PM without confirmation.",
                    "reason_code": "violates_evening_or_social_commitment",
                })
        return violations

    def _repair_unfit_score(self, item: Dict[str, Any]) -> float:
        if not self._activity_can_move_for_route_repair(item):
            return float("inf")
        protection = self._activity_repair_protection(item)
        if protection == "fixed":
            return float("inf")
        priority = clean_title(item.get("priority") or "medium")
        priority_score = {"low": 10, "medium": 30, "high": 60}.get(priority, 30)
        protection_score = {"optional": 0, "flexible": 50, "protected_social": 10000}.get(protection, 50)
        mandatory_score = 100 if item.get("is_mandatory", item.get("isMandatory", True)) else 0
        explicit_score = 20 if item.get("source") in {"initial_request", "user_operation"} else 0
        base_score = float(self._calculate_activity_base_score(item) if hasattr(self, "_calculate_activity_base_score") else 0)
        return protection_score + mandatory_score + priority_score + explicit_score + (base_score / 100.0)

    def _repair_unfit_activities(
        self,
        active: List[Dict[str, Any]],
        unscheduled: List[Dict[str, Any]],
        remaining_conflicts: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        candidates = list(unscheduled or [])
        if not candidates and remaining_conflicts:
            blocker_candidates: List[Dict[str, Any]] = []
            for conflict in remaining_conflicts:
                blocker_id = str(conflict.get("blocker_activity_id") or "")
                blocker_title = clean_title(conflict.get("blocker_activity_title") or "")
                for item in active:
                    item_id = str(item.get("stable_activity_id") or item.get("id") or "")
                    if blocker_id and item_id == blocker_id:
                        blocker_candidates.append(item)
                    elif blocker_title and clean_title(item.get("title") or "") == blocker_title:
                        blocker_candidates.append(item)
            movable = blocker_candidates or [item for item in active if self._activity_can_auto_move_for_route_repair(item)]
            candidates = sorted(movable, key=self._repair_unfit_score)[:1]
        unfit: List[Dict[str, Any]] = []
        for item in candidates:
            score = self._repair_unfit_score(item)
            if score == float("inf"):
                continue
            reason = "Not enough time after accurate travel validation"
            metadata = {
                "title": item.get("title"),
                "reason": reason,
                "priority": item.get("priority", "medium"),
                "timing_mode": item.get("timing_mode") or item.get("original_timing_mode") or TimingMode.UNSPECIFIED,
                "score": round(float(score), 2),
            }
            unfit.append(metadata)
            jlog("TRAVEL_REPAIR", f"title={metadata['title']} score={metadata['score']} reason={reason}", "UNFIT")
        return unfit

    def _final_validate_route_aware_repair(
        self,
        original: Dict[str, Any],
        repaired: Dict[str, Any],
        route_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        blocks = deepcopy(repaired.get("schedule_blocks") or [])
        preferences = repaired.get("preferences") or {}
        day_start = parse_clock(preferences.get("day_start") or preferences.get("day_start_time") or "") or DEFAULT_DAY_START
        day_end = parse_clock(preferences.get("day_end") or preferences.get("day_end_time") or "") or DEFAULT_DAY_END
        route_conflicts: List[Dict[str, Any]] = []
        warnings: List[Dict[str, Any]] = []
        start_route_summary: Optional[Dict[str, Any]] = None

        route_conflicts.extend(self._support_block_overlaps(blocks))

        activity_indices = [
            index for index, block in enumerate(blocks)
            if self._is_activity_schedule_block(block)
        ]
        for index in activity_indices:
            block = blocks[index]
            start = parse_clock(block.get("start") or block.get("startTime") or "")
            end = parse_clock(block.get("end") or block.get("endTime") or "")
            if start is None or end is None:
                route_conflicts.append({
                    "type": "route_conflict",
                    "to_activity": block.get("title"),
                    "reason": f"{block.get('title') or 'Activity'} is missing final timing after route repair.",
                    "reason_code": "missing_schedule_time",
                })
                continue
            if start < day_start or end > day_end or end <= start:
                route_conflicts.append({
                    "type": "route_conflict",
                    "to_activity": block.get("title"),
                    "from_end": format_clock(start),
                    "to_start": format_clock(end),
                    "reason": f"{block.get('title') or 'Activity'} violates the plan day boundary after route repair.",
                    "reason_code": "day_boundary",
                })

        original_records = self._activity_records_for_repair(original)
        repaired_records = self._activity_records_for_repair(repaired)
        for original_record in original_records:
            if not (
                original_record.get("is_user_fixed")
                or original_record.get("user_fixed_start") is not None
                or original_record.get("timing_mode") == TimingMode.FIXED
                or original_record.get("fixed_start") is not None
            ):
                continue
            candidate = self._find_repair_record(
                repaired_records,
                activity_id=original_record.get("stable_activity_id") or original_record.get("id"),
                title=original_record.get("title"),
            )
            if not candidate:
                route_conflicts.append({
                    "type": "route_conflict",
                    "to_activity": original_record.get("title"),
                    "reason": f"Fixed event {original_record.get('title')} was removed during route repair.",
                    "reason_code": "fixed_event_removed",
                })
                continue
            if (
                candidate.get("_repair_start") != original_record.get("_repair_start")
                or candidate.get("_repair_end") != original_record.get("_repair_end")
            ):
                route_conflicts.append({
                    "type": "route_conflict",
                    "to_activity": original_record.get("title"),
                    "from_end": format_clock(original_record.get("_repair_start") or 0),
                    "to_start": format_clock(candidate.get("_repair_start") or 0),
                    "reason": f"Fixed event {original_record.get('title')} moved during route repair.",
                    "reason_code": "fixed_event_moved",
                })

        physical_indices = [
            index for index in activity_indices
            if self._block_requires_travel_coordinate(blocks[index])
        ]
        if physical_indices:
            first_index = physical_indices[0]
            first = blocks[first_index]
            first_start = parse_clock(first.get("start") or first.get("startTime") or "")
            start_entry = (route_context.get("start_routes") or {}).get(self._route_context_activity_key(first))
            if start_entry and first_start is not None:
                route_minutes = int(start_entry.get("duration_minutes") or 0)
                leave_by = first_start - route_minutes
                start_route_summary = {
                    "start_location": start_entry.get("start_location") or "Starting point",
                    "first_physical_event": first.get("title"),
                    "first_physical_event_start": format_clock(first_start),
                    "travel_duration_minutes": route_minutes,
                    "leave_by": format_clock(leave_by),
                }
                jlog("START_ROUTE", f"first_event={first.get('title')}", "RECOMPUTE")
                jlog(
                    "START_ROUTE",
                    f"first_event={first.get('title')} leave_by={format_clock(leave_by)} duration={route_minutes}",
                    "CONSTRAINT",
                )
                if leave_by < day_start:
                    route_conflicts.append({
                        "type": "route_conflict",
                        "from_activity": "Starting point",
                        "to_activity": first.get("title"),
                        "required_route_minutes": route_minutes,
                        "available_gap_minutes": max(0, first_start - day_start),
                        "reason": f"Accurate travel from the starting point to {first.get('title')} starts before the plan day.",
                        "reason_code": "start_route_before_day_start",
                    })
                for block in blocks[:first_index]:
                    if not self._is_activity_schedule_block(block):
                        continue
                    block_end = parse_clock(block.get("end") or block.get("endTime") or "")
                    if block_end is not None and block_end > leave_by:
                        jlog(
                            "START_ROUTE",
                            f"blocker={block.get('title')} ends={format_clock(block_end)} leave_by={format_clock(leave_by)}",
                            "CONFLICT",
                        )
                        route_conflicts.append({
                            "type": "route_conflict",
                            "from_activity": block.get("title"),
                            "to_activity": first.get("title"),
                            "blocker_activity_id": block.get("stable_activity_id") or block.get("id"),
                            "blocker_activity_title": block.get("title"),
                            "from_end": format_clock(block_end),
                            "to_start": format_clock(first_start),
                            "leave_by": format_clock(leave_by),
                            "required_route_minutes": route_minutes,
                            "required_travel_minutes": route_minutes,
                            "available_gap_minutes": max(0, leave_by - day_start),
                            "reason": f"{block.get('title')} ends after the leave-by time for route-based travel to {first.get('title')}.",
                            "reason_code": "start_route_blocker",
                        })

        for left_pos, right_pos in zip(physical_indices, physical_indices[1:]):
            left = blocks[left_pos]
            right = blocks[right_pos]
            entry = (route_context.get("pairs") or {}).get(self._route_context_pair_key(left, right))
            if not entry:
                continue
            left_end = parse_clock(left.get("end") or left.get("endTime") or "")
            right_start = parse_clock(right.get("start") or right.get("startTime") or "")
            if left_end is None or right_start is None:
                continue
            route_minutes = int(entry.get("duration_minutes") or 0)
            prep = max(
                int(left.get("prep_buffer", DEFAULT_PREP_BUFFER) or 0),
                int(right.get("prep_buffer", DEFAULT_PREP_BUFFER) or 0),
            )
            intermediate_activity_ends = [
                parse_clock(block.get("end") or block.get("endTime") or "")
                for block in blocks[left_pos + 1:right_pos]
                if self._is_activity_schedule_block(block)
            ]
            route_gap_start = max([left_end] + [value for value in intermediate_activity_ends if value is not None])
            required_start = route_gap_start + prep + route_minutes
            transition_index = self._transition_index_between(blocks, left_pos, right_pos)
            transition = blocks[transition_index] if transition_index is not None else None
            transition_end = parse_clock((transition or {}).get("end") or (transition or {}).get("endTime") or "")
            if transition_end is not None and transition_end > right_start:
                route_conflicts.append({
                    "type": "route_conflict",
                    "from_activity": left.get("title"),
                    "to_activity": right.get("title"),
                    "from_end": format_clock(left_end),
                    "to_start": format_clock(right_start),
                    "required_route_minutes": route_minutes,
                    "required_travel_minutes": route_minutes,
                    "available_gap_minutes": max(0, right_start - route_gap_start),
                    "reason": f"Travel to {right.get('title')} overlaps the destination activity.",
                    "reason_code": "travel_overlaps_destination",
                })
            if right_start < required_start:
                route_conflicts.append({
                    "type": "route_conflict",
                    "from_activity": left.get("title"),
                    "to_activity": right.get("title"),
                    "from_location": left.get("location"),
                    "to_location": right.get("location"),
                    "from_end": format_clock(route_gap_start),
                    "to_start": format_clock(right_start),
                    "required_route_minutes": route_minutes,
                    "required_travel_minutes": route_minutes,
                    "available_gap_minutes": max(0, right_start - route_gap_start),
                    "destination_movable": self._activity_can_move_for_route_repair(right),
                    "destination_activity_id": right.get("stable_activity_id") or right.get("id"),
                    "reason": (
                        f"Accurate route from {left.get('title')} to {right.get('title')} needs "
                        f"{route_minutes} minutes plus buffer, but the final repair did not leave enough time."
                    ),
                    "reason_code": "not_enough_travel_time",
                })

        for record in repaired_records:
            if self._activity_repair_protection(record) != "protected_social":
                continue
            preferred_start = record.get("preferred_window_start")
            preferred_end = record.get("preferred_window_end")
            if preferred_start is None and "dinner" in clean_title(record.get("title") or ""):
                preferred_start = 17 * 60
            if preferred_end is None and "dinner" in clean_title(record.get("title") or ""):
                preferred_end = 21 * 60
            if preferred_start is None and preferred_end is None:
                continue
            start = record.get("_repair_start")
            end = record.get("_repair_end")
            if start is None or end is None:
                continue
            if (preferred_start is not None and start < int(preferred_start)) or (
                preferred_end is not None and end > int(preferred_end)
            ):
                jlog(
                    "TRAVEL_REPAIR",
                    f"title={record.get('title')} reason=violates_evening_or_social_commitment",
                    "REJECT_CANDIDATE",
                )
                route_conflicts.append({
                    "type": "route_conflict",
                    "to_activity": record.get("title"),
                    "reason": f"{record.get('title')} would move outside its protected social/evening window.",
                    "reason_code": "violates_evening_or_social_commitment",
                })

        status = "route_conflict" if route_conflicts else (
            "fallback_used" if route_context.get("fallback_used") else "validated"
        )
        jlog("TRAVEL_REPAIR", f"status={status}", "FINAL_VALIDATE")
        return {
            "travel_validation_status": status,
            "schedule_blocks": blocks,
            "route_conflicts": route_conflicts,
            "warnings": warnings,
            "route_repair_attempted": True,
            "updated_transition_count": len([
                block for block in blocks
                if block.get("block_type") == "transition" or block.get("type") in {"travel", "transition"}
            ]),
            "start_route_summary": start_route_summary,
        }

    def _route_repair_actions_from_delta(
        self,
        original: Dict[str, Any],
        repaired: Dict[str, Any],
        route_context: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        original_records = self._activity_records_for_repair(original)
        repaired_records = self._activity_records_for_repair(repaired)
        repaired_order = sorted(repaired_records, key=lambda item: item.get("_repair_start") or 0)
        actions: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for original_record in original_records:
            record_key = (
                str(original_record.get("stable_activity_id") or original_record.get("id") or "")
                or clean_title(original_record.get("title") or "")
            )
            if record_key in seen:
                continue
            seen.add(record_key)
            if not self._activity_can_auto_move_for_route_repair(original_record):
                continue
            candidate = self._find_repair_record(
                repaired_records,
                activity_id=original_record.get("stable_activity_id") or original_record.get("id"),
                title=original_record.get("title"),
            )
            if not candidate:
                continue
            if (
                candidate.get("_repair_start") == original_record.get("_repair_start")
                and candidate.get("_repair_end") == original_record.get("_repair_end")
            ):
                continue
            reason = "Adjusted to satisfy accurate travel constraints"
            candidate_index = repaired_order.index(candidate) if candidate in repaired_order else -1
            previous_physical = None
            if candidate_index >= 0:
                for previous in reversed(repaired_order[:candidate_index]):
                    if self._block_requires_travel_coordinate(previous):
                        previous_physical = previous
                        break
            if previous_physical:
                entry = (route_context.get("pairs") or {}).get(
                    self._route_context_pair_key(previous_physical, candidate)
                )
                if entry:
                    reason = (
                        f"Accurate travel from {previous_physical.get('title')} "
                        f"requires {int(entry.get('duration_minutes') or 0)} minutes"
                    )
            actions.append({
                "type": "move_activity",
                "title": original_record.get("title"),
                "activity_id": original_record.get("stable_activity_id") or original_record.get("id"),
                "from": format_clock(original_record.get("_repair_start") or 0),
                "from_end": format_clock(original_record.get("_repair_end") or 0),
                "to": format_clock(candidate.get("_repair_start") or 0),
                "to_end": format_clock(candidate.get("_repair_end") or 0),
                "reason": reason,
            })
        return actions

    def handle_pending_repair_confirmation(
        self,
        message: str,
        envelope: Optional[Dict[str, Any]],
        saved_locations: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if not envelope:
            return None
        action = self._pending_repair_confirmation_action(message)
        pending = list((envelope or {}).get("pending_repair_suggestions") or [])
        if not action or not pending:
            return None

        suggestion = pending[0]
        suggestion_id = suggestion.get("id")
        schedule_version = int(envelope.get("version") or 1)
        suggestion_version = int(suggestion.get("schedule_version") or -1)
        if not suggestion_id or suggestion_version != schedule_version:
            return {
                "handled": True,
                "reply": "That repair suggestion is out of date now, so I kept the schedule unchanged. Please run accurate travel validation again.",
                "reply_status": "warning",
                "reply_reason": "pending_repair_version_mismatch",
                "envelope": envelope,
            }

        if action == "reject":
            updated = deepcopy(envelope)
            updated["pending_repair_suggestions"] = []
            jlog("PENDING_REPAIR", f"id={suggestion_id} action=rejected", None)
            return {
                "handled": True,
                "reply": "No problem. I kept the current schedule unchanged and left the route conflict visible.",
                "reply_status": "warning",
                "reply_reason": "pending_repair_rejected",
                "envelope": updated,
            }

        jlog("PENDING_REPAIR", f"id={suggestion_id} action=accepted", None)
        operation = {
            "op": "update",
            "target_id": suggestion.get("activity_id"),
            "target_title": suggestion.get("title"),
            "fixed_start": suggestion.get("to"),
            "duration_minutes": suggestion.get("duration_minutes"),
            "notes": "Accepted accurate travel repair suggestion.",
        }
        result = self.apply_operations(
            envelope=envelope,
            operations=[operation],
            base_version=schedule_version,
            new_date=envelope.get("date"),
            saved_locations=saved_locations,
        )
        updated_envelope = result.get("envelope") or envelope
        if updated_envelope.get("travel_validation_status") != "route_conflict" and not updated_envelope.get("route_conflicts"):
            updated_envelope["pending_repair_suggestions"] = []
            reply = f"I applied the repair suggestion and moved {suggestion.get('title')} to {suggestion.get('to')}."
            reply_status = "success"
            reply_reason = "pending_repair_accepted"
        else:
            reply = "I applied that repair suggestion, but accurate travel time still found a route conflict."
            reply_status = "warning"
            reply_reason = "pending_repair_accepted_with_remaining_conflict"
        result.update({
            "handled": True,
            "reply": reply,
            "reply_status": reply_status,
            "reply_reason": reply_reason,
            "envelope": updated_envelope,
        })
        return result

    def _pending_repair_confirmation_action(self, message: str) -> Optional[str]:
        clean = clean_title(message or "")
        if re.fullmatch(r"(yes|y|yes apply it|apply it|apply the change|confirm|ok|okay|do it)", clean):
            return "accept"
        if re.fullmatch(r"(no|n|keep it|keep the current schedule|dont change it|don t change it|don't change it|cancel)", clean):
            return "reject"
        return None

    def _mark_transition_estimate_source(self, envelope: Dict[str, Any], source: str) -> None:
        for block in envelope.get("schedule_blocks") or []:
            if block.get("block_type") == "transition" or block.get("type") in {"travel", "transition"}:
                block.setdefault("travel_estimate_source", source)

    def _location_resolution_requests(
        self,
        envelope: Dict[str, Any],
        saved_locations: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        requests: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        grouped_by_location: Dict[str, Dict[str, Any]] = {}
        blocks = list(envelope.get("schedule_blocks") or envelope.get("activities") or [])
        first_physical = self._first_physical_activity_index(blocks)
        if first_physical is not None and not self._start_location_coordinate_resolution(envelope.get("preferences") or {}, saved_locations)[0]:
            requests.append(self._start_location_resolution_request(envelope.get("preferences") or {}))
        for index, block in enumerate(blocks):
            block_type = block.get("block_type") or block.get("type")
            if block_type and block_type != "activity":
                continue
            if not self._activity_needs_coordinate_resolution(block, saved_locations):
                continue
            activity_id = str(block.get("stable_activity_id") or block.get("id") or block.get("title") or index)
            if activity_id in seen_ids:
                continue
            seen_ids.add(activity_id)
            current_guess = block.get("location_label") or block.get("location")
            category = block.get("location_category")
            saved_matches = [
                self.travel_service.format_saved_match(match)
                for match in self.travel_service.saved_location_matches(current_guess, category, saved_locations)
                if coordinate_from_saved_location(match)
            ][:5]
            query = self.travel_service.expand_alias(current_guess or block.get("title") or category or "", category)
            self._debug(
                f"[TRAVEL][LOCATION_PENDING] {block.get('title')} requires {category or 'unknown'} location for accurate travel"
            )
            location_key = clean_title(current_guess or "")
            if location_key and location_key in grouped_by_location:
                grouped = grouped_by_location[location_key]
                grouped.setdefault("related_activity_ids", []).append(activity_id)
                grouped.setdefault("related_titles", []).append(block.get("title"))
                continue
            request = {
                "activity_id": activity_id,
                "title": block.get("title"),
                "category": category,
                "current_guess": current_guess,
                "expanded_query": query,
                "requires_coordinate": True,
                "location_readiness_status": "missing_coordinates",
                "affected_transitions": self._affected_transitions_for_activity(blocks, index),
                "reason": "Needs exact map location for accurate travel time.",
                "display_reason": "needs exact map location",
                "saved_matches": saved_matches,
                "geocode_candidates": [],
                "same_location_as": block.get("same_location_as"),
                "related_activity_ids": [],
                "related_titles": [],
            }
            requests.append(request)
            if location_key:
                grouped_by_location[location_key] = request
        return requests

    def _start_location_resolution_request(self, preferences: Dict[str, Any]) -> Dict[str, Any]:
        configured = self._configured_start_location(preferences) or {}
        label = (
            configured.get("label")
            or configured.get("display_name")
            or configured.get("address")
            or "Starting point"
        )
        return {
            "activity_id": "__start_location__",
            "request_type": "start_location",
            "title": "Where are you starting from for this plan?",
            "category": "start_location",
            "current_guess": label if configured else None,
            "expanded_query": label if configured else None,
            "requires_coordinate": True,
            "location_readiness_status": "missing_coordinates",
            "affected_transitions": [],
            "reason": "Needs exact starting point for accurate travel time.",
            "display_reason": "Where are you starting from for this plan?",
            "saved_matches": [],
            "geocode_candidates": [],
            "same_location_as": None,
            "related_activity_ids": [],
            "related_titles": [],
        }

    def _configured_start_location(self, preferences: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(preferences, dict):
            return None
        for key in ("day_start_location_override", "default_start_location"):
            candidate = preferences.get(key)
            if isinstance(candidate, dict) and candidate:
                return candidate
        return None

    def _start_location_coordinate_resolution(
        self,
        preferences: Dict[str, Any],
        saved_locations: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[Optional[Tuple[float, float]], Optional[Dict[str, Any]]]:
        configured = self._configured_start_location(preferences)
        if not configured:
            return None, None
        coord = self._coordinate_from_payload(configured)
        saved = None
        if not coord:
            saved = self.travel_service.confirmed_saved_location(
                configured.get("label") or configured.get("display_name") or configured.get("address"),
                configured.get("category"),
                saved_locations or [],
            )
            coord = coordinate_from_saved_location(saved) if saved else None
        if not coord:
            return None, None
        resolved = {
            "label": configured.get("label") or (saved or {}).get("label") or configured.get("display_name") or "Starting point",
            "display_name": configured.get("display_name") or (saved or {}).get("display_name") or configured.get("label") or configured.get("address") or "Starting point",
            "address": configured.get("address") or (saved or {}).get("address") or configured.get("display_name") or configured.get("label") or "Starting point",
            "category": configured.get("category") or (saved or {}).get("category") or "start_location",
            "latitude": coord[0],
            "longitude": coord[1],
            "source": configured.get("source") or (saved or {}).get("source") or "default_start_location",
            "confirmed_by_user": bool(configured.get("confirmed_by_user", True)),
            "saved_location_label": configured.get("saved_location_label") or configured.get("label") or (saved or {}).get("label"),
        }
        return coord, resolved

    def _coordinate_from_payload(self, payload: Dict[str, Any]) -> Optional[Tuple[float, float]]:
        raw_lat = payload.get("latitude") if payload.get("latitude") is not None else payload.get("lat")
        raw_lng = payload.get("longitude") if payload.get("longitude") is not None else payload.get("lng")
        try:
            lat = float(raw_lat)
            lng = float(raw_lng)
        except (TypeError, ValueError):
            return None
        if -90 <= lat <= 90 and -180 <= lng <= 180:
            return (lat, lng)
        return None

    def _first_physical_activity_index(self, blocks: List[Dict[str, Any]]) -> Optional[int]:
        for index, block in enumerate(blocks or []):
            if not self._is_activity_schedule_block(block):
                continue
            if self._block_requires_travel_coordinate(block):
                return index
        return None

    def _is_activity_schedule_block(self, block: Dict[str, Any]) -> bool:
        block_type = clean_title(block.get("block_type") or block.get("type") or "")
        if block_type:
            return block_type == "activity"
        return bool(block.get("title") or block.get("stable_activity_id") or block.get("id"))

    def _activity_needs_coordinate_resolution(
        self,
        block: Dict[str, Any],
        saved_locations: List[Dict[str, Any]],
    ) -> bool:
        if not self._block_requires_travel_coordinate(block):
            return False
        if self._embedded_activity_coordinate(block):
            return False
        saved = self.travel_service.confirmed_saved_location(
            block.get("location_label") or block.get("location"),
            block.get("location_category"),
            saved_locations,
        )
        if saved:
            return False
        status = clean_title(block.get("location_status") or "")
        category = clean_title(block.get("location_category") or "")
        if not block.get("location"):
            return status in {"needs_resolution", "fallback_used", "unresolved"} and category in {
                "meal_place",
                "supermarket",
                "fitness_center",
                "workplace",
                "institution",
                "campus_area",
                "office",
                "library",
            }
        if status in {"needs_resolution", "fallback_used", "unresolved"}:
            return True
        return True

    def _block_requires_travel_coordinate(self, block: Dict[str, Any]) -> bool:
        status = clean_title(block.get("location_status") or "")
        category = clean_title(block.get("location_category") or "")
        raw_travel_required = block.get("travel_required")
        if raw_travel_required is False:
            return False
        if isinstance(raw_travel_required, str) and raw_travel_required.strip().lower() in {"0", "false", "no", "off"}:
            return False
        if status in {"not_required", "no_location_required"}:
            return False
        if category in {"home_or_online", "none"}:
            return False
        return True

    def _affected_transitions_for_activity(
        self,
        blocks: List[Dict[str, Any]],
        index: int,
    ) -> List[Dict[str, Any]]:
        affected: List[Dict[str, Any]] = []
        current = blocks[index]
        previous_activity = next(
            (blocks[pos] for pos in range(index - 1, -1, -1) if self._is_activity_schedule_block(blocks[pos])),
            None,
        )
        next_activity = next(
            (blocks[pos] for pos in range(index + 1, len(blocks)) if self._is_activity_schedule_block(blocks[pos])),
            None,
        )
        if previous_activity and previous_activity.get("location") != current.get("location"):
            affected.append({
                "from_activity": previous_activity.get("title"),
                "to_activity": current.get("title"),
                "from_location": previous_activity.get("location"),
                "to_location": current.get("location"),
            })
        if next_activity and next_activity.get("location") != current.get("location"):
            affected.append({
                "from_activity": current.get("title"),
                "to_activity": next_activity.get("title"),
                "from_location": current.get("location"),
                "to_location": next_activity.get("location"),
            })
        return affected

    def _activity_coordinate(
        self,
        block: Dict[str, Any],
        saved_locations: List[Dict[str, Any]],
    ) -> Optional[Tuple[float, float]]:
        embedded, _ = self._activity_coordinate_resolution(block, saved_locations)
        if embedded:
            return embedded
        return None

    def _activity_coordinate_resolution(
        self,
        block: Dict[str, Any],
        saved_locations: List[Dict[str, Any]],
    ) -> Tuple[Optional[Tuple[float, float]], Optional[Dict[str, Any]]]:
        embedded = self._embedded_activity_coordinate(block)
        if embedded:
            resolved = deepcopy(block.get("resolved_location")) if isinstance(block.get("resolved_location"), dict) else {}
            resolved.setdefault("display_name", block.get("location_label") or block.get("location") or block.get("title"))
            resolved.setdefault("address", block.get("location_label") or block.get("location") or block.get("title"))
            resolved.setdefault("source", block.get("location_source") or "event_confirmed")
            resolved.setdefault("confirmed_by_user", True)
            resolved["latitude"] = embedded[0]
            resolved["longitude"] = embedded[1]
            return embedded, resolved
        saved = self.travel_service.confirmed_saved_location(
            block.get("location_label") or block.get("location"),
            block.get("location_category"),
            saved_locations,
        )
        coord = coordinate_from_saved_location(saved) if saved else None
        if not coord:
            return None, None
        resolved = {
            "label": saved.get("label"),
            "display_name": saved.get("display_name") or saved.get("label") or saved.get("address"),
            "address": saved.get("address") or saved.get("display_name") or saved.get("label"),
            "category": saved.get("category") or block.get("location_category"),
            "latitude": coord[0],
            "longitude": coord[1],
            "source": saved.get("source") or "saved_profile",
            "confirmed_by_user": bool(saved.get("confirmed_by_user", True)),
            "saved_location_label": saved.get("label"),
        }
        return coord, resolved

    def _apply_resolved_location_to_block(
        self,
        block: Dict[str, Any],
        resolved_location: Optional[Dict[str, Any]],
    ) -> None:
        if not resolved_location:
            return
        block["resolved_location"] = deepcopy(resolved_location)
        block["location_status"] = "resolved"
        block["location_source"] = resolved_location.get("source") or block.get("location_source") or "saved_profile"
        block["location_label"] = (
            resolved_location.get("display_name")
            or resolved_location.get("address")
            or block.get("location_label")
            or block.get("location")
        )
        if resolved_location.get("saved_location_label"):
            block["saved_location_label"] = resolved_location.get("saved_location_label")

    def _embedded_activity_coordinate(self, block: Dict[str, Any]) -> Optional[Tuple[float, float]]:
        resolved = block.get("resolved_location")
        payloads: List[Dict[str, Any]] = []
        if isinstance(resolved, dict):
            payloads.append(resolved)
        payloads.append(block)

        for payload in payloads:
            coord = self._coordinate_from_payload(payload)
            if coord:
                return coord
        return None

    def _sync_resolved_locations_to_activities(self, envelope: Dict[str, Any]) -> None:
        activity_blocks = [
            block for block in envelope.get("schedule_blocks") or []
            if self._is_activity_schedule_block(block) and block.get("resolved_location")
        ]
        if not activity_blocks:
            return
        by_id = {
            str(block.get("stable_activity_id") or block.get("id")): block
            for block in activity_blocks
            if block.get("stable_activity_id") or block.get("id")
        }
        by_title = {
            clean_title(block.get("title") or ""): block
            for block in activity_blocks
            if block.get("title")
        }
        synced: List[Dict[str, Any]] = []
        for activity in envelope.get("activities") or []:
            key = str(activity.get("stable_activity_id") or activity.get("id") or "")
            block = by_id.get(key) if key else None
            if not block:
                block = by_title.get(clean_title(activity.get("title") or ""))
            if block:
                updated = deepcopy(activity)
                updated["resolved_location"] = deepcopy(block.get("resolved_location"))
                updated["location_status"] = "resolved"
                updated["location_source"] = block.get("location_source")
                updated["location_label"] = block.get("location_label") or updated.get("location_label")
                if block.get("saved_location_label"):
                    updated["saved_location_label"] = block.get("saved_location_label")
                synced.append(updated)
            else:
                synced.append(activity)
        envelope["activities"] = synced

    def _validate_routes_with_service(
        self,
        envelope: Dict[str, Any],
        saved_locations: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        blocks = deepcopy(envelope.get("schedule_blocks") or [])
        preferences = envelope.get("preferences") or {}
        if hasattr(self, "_normalize_day_boundary_preferences"):
            self._normalize_day_boundary_preferences(preferences)
        activity_indices = [
            index for index, block in enumerate(blocks)
            if self._is_activity_schedule_block(block)
            and self._block_requires_travel_coordinate(block)
        ]
        route_conflicts: List[Dict[str, Any]] = []
        warnings: List[Dict[str, Any]] = []
        replacements: List[Tuple[int, int, List[Dict[str, Any]]]] = []
        fallback_used = False
        updated_transition_count = 0
        start_route_summary: Optional[Dict[str, Any]] = None
        transport_mode = (preferences.get("transport_mode") or "driving-car")

        first_physical_index = activity_indices[0] if activity_indices else None
        start_coord, start_resolved = self._start_location_coordinate_resolution(preferences, saved_locations)
        if first_physical_index is not None and start_coord and start_resolved:
            right = blocks[first_physical_index]
            right_coord, right_resolved = self._activity_coordinate_resolution(right, saved_locations)
            if right_coord:
                self._apply_resolved_location_to_block(right, right_resolved)
                day_start = parse_clock(preferences.get("day_start") or preferences.get("day_start_time") or "") or DEFAULT_DAY_START
                right_start = parse_clock(right.get("start") or right.get("startTime") or "")
                if right_start is not None:
                    preceding_activity_ends = [
                        parse_clock(block.get("end") or block.get("endTime") or "")
                        for block in blocks[:first_physical_index]
                        if self._is_activity_schedule_block(block)
                    ]
                    route_gap_start = max([day_start] + [value for value in preceding_activity_ends if value is not None])
                    available_gap = max(0, right_start - route_gap_start)
                    time_bucket = f"{envelope.get('date')}T{format_clock(right_start)}"
                    start_label = (
                        start_resolved.get("display_name")
                        or start_resolved.get("label")
                        or "Starting point"
                    )
                    jlog("DEFAULT_LOCATION", f"start={start_label}", None)
                    try:
                        jlog_verbose(
                            "TRAVEL_SERVICE",
                            f"route request: {start_label} -> {right.get('location') or right.get('title')}",
                            "ORS",
                        )
                        route_minutes = self.travel_service.route_minutes(
                            start_coord,
                            right_coord,
                            transport_mode=transport_mode,
                            time_bucket=time_bucket,
                        )
                        source = "routing_service"
                        jlog_verbose(
                            "TRAVEL_SERVICE",
                            f"route result: {start_label} -> {right.get('location') or right.get('title')} = {route_minutes} min",
                            "ORS",
                        )
                    except (MissingORSApiKey, TravelServiceError, Exception) as exc:
                        route_minutes = estimate_travel_minutes(start_label, right.get("location"))
                        source = "fallback"
                        fallback_used = True
                        warnings.append({
                            "warning_code": "ORS_FALLBACK_USED",
                            "from_activity": "Starting point",
                            "to_activity": right.get("title"),
                            "explanation": f"ORS route validation was unavailable, so JPlan used heuristic travel time: {exc}",
                        })

                    route_minutes = int(route_minutes or 0)
                    leave_by = right_start - route_minutes
                    start_route_summary = {
                        "start_location": start_label,
                        "first_physical_event": right.get("title"),
                        "first_physical_event_start": format_clock(right_start),
                        "travel_duration_minutes": route_minutes,
                        "leave_by": format_clock(leave_by),
                    }
                    jlog(
                        "START_ROUTE",
                        f"first_event={right.get('title')} leave_by={format_clock(leave_by)} duration={route_minutes}",
                        "CONSTRAINT",
                    )
                    blockers: List[Tuple[Dict[str, Any], int]] = []
                    for block in blocks[:first_physical_index]:
                        if not self._is_activity_schedule_block(block):
                            continue
                        block_end = parse_clock(block.get("end") or block.get("endTime") or "")
                        if block_end is not None and block_end > leave_by:
                            blockers.append((block, block_end))

                    if blockers or route_minutes > max(0, right_start - day_start):
                        if blockers:
                            for blocker, blocker_end in blockers:
                                jlog(
                                    "START_ROUTE",
                                    f"blocker={blocker.get('title')} ends={format_clock(blocker_end)} leave_by={format_clock(leave_by)}",
                                    "CONFLICT",
                                )
                                route_conflicts.append({
                                    "type": "route_conflict",
                                    "from": blocker.get("title"),
                                    "to": right.get("title"),
                                    "from_activity": blocker.get("title"),
                                    "to_activity": right.get("title"),
                                    "from_location": start_label,
                                    "to_location": right.get("location"),
                                    "from_end": format_clock(blocker_end),
                                    "to_start": format_clock(right_start),
                                    "first_physical_event": right.get("title"),
                                    "first_physical_event_end": right.get("end") or right.get("endTime"),
                                    "leave_by": format_clock(leave_by),
                                    "available_minutes": max(0, leave_by - day_start),
                                    "available_gap_minutes": max(0, leave_by - day_start),
                                    "required_route_minutes": route_minutes,
                                    "required_travel_minutes": route_minutes,
                                    "destination_movable": self._activity_can_move_for_route_repair(right),
                                    "destination_activity_id": right.get("stable_activity_id") or right.get("id"),
                                    "blocker_activity_id": blocker.get("stable_activity_id") or blocker.get("id"),
                                    "blocker_activity_title": blocker.get("title"),
                                    "blocker_movable": self._activity_can_auto_move_for_route_repair(blocker),
                                    "reason": (
                                        f"{blocker.get('title')} ends after the leave-by time for route-based travel "
                                        f"to {right.get('title')}."
                                    ),
                                    "reason_code": "start_route_blocker",
                                })
                        else:
                            available_gap = max(0, right_start - day_start)
                            route_conflicts.append({
                                "type": "route_conflict",
                                "from": "Starting point",
                                "to": right.get("title"),
                                "from_activity": "Starting point",
                                "to_activity": right.get("title"),
                                "from_location": start_label,
                                "to_location": right.get("location"),
                                "from_end": format_clock(day_start),
                                "to_start": format_clock(right_start),
                                "available_minutes": available_gap,
                                "available_gap_minutes": available_gap,
                                "required_route_minutes": route_minutes,
                                "required_travel_minutes": route_minutes,
                                "destination_movable": self._activity_can_move_for_route_repair(right),
                                "destination_activity_id": right.get("stable_activity_id") or right.get("id"),
                                "reason": (
                                    f"Accurate route from your starting point to {right.get('title')} needs "
                                    f"{route_minutes} minutes, but only {available_gap} minutes are available."
                                ),
                                "reason_code": "not_enough_travel_time",
                            })
                    else:
                        # Start-location travel is UI metadata, not an activity-to-activity
                        # support block. Keeping it separate prevents a synthetic "home ->
                        # first event" route from being mixed into normal transition rebuilds.
                        pass

        for left_index, right_index in zip(activity_indices, activity_indices[1:]):
            left = blocks[left_index]
            right = blocks[right_index]
            if not left.get("location") or not right.get("location") or left.get("location") == right.get("location"):
                continue
            left_coord, left_resolved = self._activity_coordinate_resolution(left, saved_locations)
            right_coord, right_resolved = self._activity_coordinate_resolution(right, saved_locations)
            if not left_coord or not right_coord:
                continue
            self._apply_resolved_location_to_block(left, left_resolved)
            self._apply_resolved_location_to_block(right, right_resolved)
            left_end = parse_clock(left.get("end") or left.get("endTime") or "")
            right_start = parse_clock(right.get("start") or right.get("startTime") or "")
            if left_end is None or right_start is None:
                continue
            intermediate_activity_ends = [
                parse_clock(block.get("end") or block.get("endTime") or "")
                for block in blocks[left_index + 1:right_index]
                if self._is_activity_schedule_block(block)
            ]
            route_gap_start = max(
                [left_end] + [value for value in intermediate_activity_ends if value is not None]
            )
            available_gap = max(0, right_start - route_gap_start)
            time_bucket = f"{envelope.get('date')}T{format_clock(right_start)}"
            try:
                jlog_verbose(
                    "TRAVEL_SERVICE",
                    f"route request: {left.get('location') or left.get('title')} -> {right.get('location') or right.get('title')}",
                    "ORS",
                )
                route_minutes = self.travel_service.route_minutes(
                    left_coord,
                    right_coord,
                    transport_mode=transport_mode,
                    time_bucket=time_bucket,
                )
                source = "routing_service"
                jlog_verbose(
                    "TRAVEL_SERVICE",
                    f"route result: {left.get('location') or left.get('title')} -> {right.get('location') or right.get('title')} = {route_minutes} min",
                    "ORS",
                )
            except (MissingORSApiKey, TravelServiceError, Exception) as exc:
                route_minutes = estimate_travel_minutes(left.get("location"), right.get("location"))
                source = "fallback"
                fallback_used = True
                warnings.append({
                    "warning_code": "ORS_FALLBACK_USED",
                    "from_activity": left.get("title"),
                    "to_activity": right.get("title"),
                    "explanation": f"ORS route validation was unavailable, so JPlan used heuristic travel time: {exc}",
                })

            route_minutes = int(route_minutes or 0)
            transition_index = self._transition_index_between(blocks, left_index, right_index)
            transition = blocks[transition_index] if transition_index is not None else None
            if transition:
                transition["duration_minutes"] = route_minutes
                transition["travel_estimate_source"] = source
                transition["travel_validation_status"] = "fallback_used" if source == "fallback" else "validated"
                transition["transport_mode"] = transport_mode
                transition["route_duration_minutes"] = route_minutes
                transition["from_coordinate"] = {"latitude": left_coord[0], "longitude": left_coord[1]}
                transition["to_coordinate"] = {"latitude": right_coord[0], "longitude": right_coord[1]}

            if route_minutes > available_gap:
                destination_movable = self._activity_can_move_for_route_repair(right)
                route_conflicts.append({
                    "type": "route_conflict",
                    "from": left.get("title"),
                    "to": right.get("title"),
                    "from_activity": left.get("title"),
                    "to_activity": right.get("title"),
                    "from_location": left.get("location"),
                    "to_location": right.get("location"),
                    "from_end": format_clock(left_end),
                    "to_start": format_clock(right_start),
                    "available_minutes": available_gap,
                    "available_gap_minutes": available_gap,
                    "required_route_minutes": route_minutes,
                    "required_travel_minutes": route_minutes,
                    "destination_movable": destination_movable,
                    "destination_activity_id": right.get("stable_activity_id") or right.get("id"),
                    "reason": (
                        f"Accurate route from {left.get('title')} to {right.get('title')} needs "
                        f"{route_minutes} minutes, but only {available_gap} minutes are available."
                    ),
                    "reason_code": "not_enough_travel_time",
                })
                continue

            replacement_start = self._support_segment_start_before(blocks, right_index)
            old_blocks_removed = max(0, right_index - replacement_start)
            jlog(
                "TRAVEL_BLOCK",
                f"from={left.get('title')} to={right.get('title')} old_blocks_removed={old_blocks_removed}",
                "REPLACE",
            )

            replacement = self._route_timing_replacement_blocks(
                blocks=blocks,
                replacement_start=replacement_start,
                right_index=right_index,
                left=left,
                right=right,
                route_minutes=route_minutes,
                source=source,
                transport_mode=transport_mode,
                left_coord=left_coord,
                right_coord=right_coord,
                transition_template=transition,
            )
            replacements.append((replacement_start, right_index, replacement))
            updated_transition_count += 1

        for start, end, replacement in sorted(replacements, key=lambda item: item[0], reverse=True):
            blocks[start:end] = replacement

        support_overlaps = self._support_block_overlaps(blocks)
        jlog("SUPPORT_BLOCK", f"overlaps_found={len(support_overlaps)}", "OVERLAP_CHECK")
        if support_overlaps:
            route_conflicts.extend(support_overlaps)

        status = "route_conflict" if route_conflicts else ("fallback_used" if fallback_used else "validated")
        return {
            "travel_validation_status": status,
            "schedule_blocks": blocks,
            "route_conflicts": route_conflicts,
            "warnings": warnings,
            "route_repair_attempted": bool(route_conflicts),
            "updated_transition_count": updated_transition_count,
            "start_route_summary": start_route_summary,
        }

    def _start_route_replacement_blocks(
        self,
        idle_start: int,
        right: Dict[str, Any],
        route_minutes: int,
        source: str,
        transport_mode: str,
        start_coord: Tuple[float, float],
        right_coord: Tuple[float, float],
        start_resolved: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        right_start = parse_clock(right.get("start") or right.get("startTime") or "") or 0
        route_start = right_start - route_minutes
        start_label = (
            start_resolved.get("display_name")
            or start_resolved.get("label")
            or "Starting point"
        )
        replacement: List[Dict[str, Any]] = []
        if route_start > idle_start:
            replacement.append({
                "block_type": "idle",
                "type": "idle",
                "title": "Free Time",
                "start": format_clock(idle_start),
                "end": format_clock(route_start),
                "startTime": format_clock(idle_start),
                "endTime": format_clock(route_start),
                "duration_minutes": route_start - idle_start,
                "reason": "Slack before route-based travel from starting point",
            })
        transition = {
            "block_type": "transition",
            "type": "travel",
            "title": f"Travel to {right.get('location') or right.get('title')}",
            "start": format_clock(route_start),
            "end": format_clock(right_start),
            "startTime": format_clock(route_start),
            "endTime": format_clock(right_start),
            "duration_minutes": route_minutes,
            "from_location": start_label,
            "to_location": right.get("location"),
            "travel_estimate_source": source,
            "travel_validation_status": "fallback_used" if source == "fallback" else "validated",
            "transport_mode": transport_mode,
            "route_duration_minutes": route_minutes,
            "from_coordinate": {"latitude": start_coord[0], "longitude": start_coord[1]},
            "to_coordinate": {"latitude": right_coord[0], "longitude": right_coord[1]},
            "reason": "Route-based travel duration from starting point",
        }
        replacement.append(transition)
        jlog(
            "TRAVEL_BLOCK",
            (
                f"from=Starting point to={right.get('title')} "
                f"start={transition['start']} end={transition['end']} duration={route_minutes}"
            ),
            "FINAL",
        )
        return replacement

    def _route_timing_replacement_blocks(
        self,
        blocks: List[Dict[str, Any]],
        replacement_start: int,
        right_index: int,
        left: Dict[str, Any],
        right: Dict[str, Any],
        route_minutes: int,
        source: str,
        transport_mode: str,
        left_coord: Tuple[float, float],
        right_coord: Tuple[float, float],
        transition_template: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        right_start = parse_clock(right.get("start") or right.get("startTime") or "") or 0
        route_start = right_start - route_minutes
        first_replaced = blocks[replacement_start] if replacement_start < len(blocks) else left
        idle_start = parse_clock(first_replaced.get("start") or first_replaced.get("startTime") or "") if first_replaced else None
        if idle_start is None:
            idle_start = parse_clock(left.get("end") or left.get("endTime") or "") or route_start

        replacement: List[Dict[str, Any]] = []
        if route_start > idle_start:
            idle = {
                "block_type": "idle",
                "type": "idle",
                "title": "Free Time",
                "start": format_clock(idle_start),
                "end": format_clock(route_start),
                "startTime": format_clock(idle_start),
                "endTime": format_clock(route_start),
                "duration_minutes": route_start - idle_start,
                "reason": "Slack created after route-based travel validation",
            }
            replacement.append(idle)
            self._debug(
                f"[TRAVEL][TIMING] Added idle/free block: {idle['start']}-{idle['end']}"
            )

        transition = deepcopy(transition_template) if transition_template else {}
        transition.update({
            "block_type": "transition",
            "type": "travel",
            "title": transition.get("title") or f"Travel to {right.get('location') or right.get('title')}",
            "start": format_clock(route_start),
            "end": format_clock(right_start),
            "startTime": format_clock(route_start),
            "endTime": format_clock(right_start),
            "duration_minutes": route_minutes,
            "from_location": left.get("location"),
            "to_location": right.get("location"),
            "travel_estimate_source": source,
            "travel_validation_status": "fallback_used" if source == "fallback" else "validated",
            "transport_mode": transport_mode,
            "route_duration_minutes": route_minutes,
            "from_coordinate": {"latitude": left_coord[0], "longitude": left_coord[1]},
            "to_coordinate": {"latitude": right_coord[0], "longitude": right_coord[1]},
            "reason": "Route-based travel duration validated after draft construction",
        })
        transition.pop("is_tight", None)
        transition.pop("warning_code", None)
        replacement.append(transition)
        self._debug(
            f"[TRAVEL][TIMING] Updated transition {transition.get('title')}: {transition['start']}-{transition['end']}"
        )
        jlog(
            "TRAVEL_BLOCK",
            (
                f"from={left.get('title')} to={right.get('title')} "
                f"start={transition['start']} end={transition['end']} duration={route_minutes}"
            ),
            "FINAL",
        )
        return replacement

    def _support_segment_start_before(
        self,
        blocks: List[Dict[str, Any]],
        right_index: int,
    ) -> int:
        index = right_index - 1
        while index >= 0 and self._is_support_block(blocks[index]):
            index -= 1
        return index + 1

    def _is_support_block(self, block: Dict[str, Any]) -> bool:
        block_type = clean_title(block.get("block_type") or block.get("type") or "")
        title = clean_title(block.get("title") or "")
        return (
            block_type in {"idle", "free_time", "buffer", "prep", "transition", "travel"}
            or title == "free time"
            or "prep buffer" in title
            or title.startswith("travel to")
        )

    def _block_time_bounds(self, block: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
        start = parse_clock(block.get("start") or block.get("startTime") or "")
        end = parse_clock(block.get("end") or block.get("endTime") or "")
        if start is not None and end is not None and end <= start:
            end += 24 * 60
        return start, end

    def _support_block_overlaps(self, blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        timed: List[Tuple[int, int, Dict[str, Any], bool]] = []
        for block in blocks or []:
            start, end = self._block_time_bounds(block)
            if start is None or end is None:
                continue
            timed.append((start, end, block, self._is_support_block(block)))
        timed.sort(key=lambda item: (item[0], item[1]))

        overlaps: List[Dict[str, Any]] = []
        for index, (start, end, block, is_support) in enumerate(timed):
            for previous_start, previous_end, previous, previous_is_support in timed[:index]:
                if previous_end <= start:
                    continue
                if not (is_support or previous_is_support):
                    continue
                overlaps.append({
                    "type": "support_block_overlap",
                    "from_activity": previous.get("title"),
                    "to_activity": block.get("title"),
                    "available_minutes": max(0, start - previous_start),
                    "required_route_minutes": max(0, previous_end - start),
                    "reason": (
                        f"Support block timing overlaps between {previous.get('title')} "
                        f"and {block.get('title')}."
                    ),
                })
                break
        return overlaps

