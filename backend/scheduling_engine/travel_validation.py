import json
import math
import re
import time
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4
from zoneinfo import ZoneInfo

from jplan_logging import jjson, jlog, jlog_verbose, jsection
from travel_service import MissingORSApiKey, TravelService, TravelServiceError, coordinate_from_saved_location
from .module_d_refinement import REFINEMENT_META_KEYS
from .types_utils import *
from .types_utils import _normalize_location

NEAR_LOCATION_THRESHOLD_METERS = 300

class TravelValidationMixin:
    def _reset_travel_perf(self) -> None:
        self._travel_perf_timers = {
            "geocoding_seconds": 0.0,
            "route_matrix_seconds": 0.0,
            "route_fetch_seconds": 0.0,
            "route_aware_refit_seconds": 0.0,
            "cluster_reorder_seconds": 0.0,
            "final_validation_seconds": 0.0,
        }
        if hasattr(self.travel_service, "reset_stats"):
            self.travel_service.reset_stats()

    def _add_travel_perf_time(self, key: str, seconds: float) -> None:
        timers = getattr(self, "_travel_perf_timers", None)
        if not isinstance(timers, dict):
            timers = {}
            self._travel_perf_timers = timers
        timers[key] = float(timers.get(key) or 0.0) + max(0.0, float(seconds or 0.0))

    def _log_travel_perf_summary(self) -> None:
        timers = dict(getattr(self, "_travel_perf_timers", {}) or {})
        stats = self.travel_service.stats_snapshot() if hasattr(self.travel_service, "stats_snapshot") else {}
        if stats.get("geocode_seconds") is not None:
            timers["geocoding_seconds"] = max(
                float(timers.get("geocoding_seconds") or 0.0),
                float(stats.get("geocode_seconds") or 0.0),
            )
        if stats.get("route_fetch_seconds") is not None:
            timers["route_fetch_seconds"] = max(
                float(timers.get("route_fetch_seconds") or 0.0),
                float(stats.get("route_fetch_seconds") or 0.0),
            )
        for key in (
            "geocoding_seconds",
            "route_matrix_seconds",
            "route_fetch_seconds",
            "route_aware_refit_seconds",
            "cluster_reorder_seconds",
            "final_validation_seconds",
        ):
            jlog("TIMER", f"{key}={float(timers.get(key) or 0.0):.2f}", None)
        jlog("PERF", f"count={int(stats.get('route_api_calls') or 0)}", "ROUTE_API_CALLS")
        jlog(
            "PERF",
            f"hits={int(stats.get('route_cache_hits') or 0)} misses={int(stats.get('route_cache_misses') or 0)}",
            "ROUTE_CACHE",
        )

    def _apply_accurate_travel_if_requested(
        self,
        envelope: Dict[str, Any],
        saved_locations: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        validation_started = time.perf_counter()
        self._reset_travel_perf()
        updated = deepcopy(envelope)
        preferences = updated.setdefault("preferences", {})
        if hasattr(self, "_normalize_day_boundary_preferences"):
            self._normalize_day_boundary_preferences(preferences)
        travel_intent = bool(preferences.get("travel_intent") or updated.get("travel_intent"))
        accurate = bool(self._resolve_accurate_travel_time(preferences, updated) or travel_intent)
        updated["accurate_travel_time"] = accurate
        preferences["accurate_travel_time"] = accurate
        updated["travel_intent"] = travel_intent
        preferences["travel_intent"] = travel_intent
        updated.setdefault("schedule_status", updated.get("status") or "ok")

        if not accurate:
            if travel_intent:
                location_requests = self._location_resolution_requests(
                    updated,
                    saved_locations,
                    include_start_location=False,
                )
                if location_requests:
                    updated["schedule_status"] = "location_pending"
                    updated["status"] = "location_pending"
                    updated["travel_validation_status"] = "pending_locations"
                    updated["location_resolution_requests"] = location_requests
                    updated.setdefault("validation_issues", [])
                    pending_titles = ", ".join(req.get("title", "activity") for req in location_requests[:5])
                    updated["validation_issues"] = list(dict.fromkeys(updated["validation_issues"] + [
                        f"Travel-aware planning needs exact location confirmation for {pending_titles}."
                    ]))
                    updated.setdefault("explanations", [])
                    updated["explanations"] = self._merge_explanations(
                        updated.get("explanations", []),
                        ["I drafted the plan, but exact travel validation is pending location confirmation."],
                    )
                    self._clear_route_preview_metadata(updated)
                    self._mark_transition_estimate_source(updated, "heuristic_pending_locations")
                    self._log_travel_perf_summary()
                    jlog("TIMER", f"accurate_travel_validation_seconds={time.perf_counter() - validation_started:.2f}", None)
                    return updated
            updated["travel_validation_status"] = "not_requested"
            updated["location_resolution_requests"] = []
            self._clear_route_preview_metadata(updated)
            self._mark_transition_estimate_source(updated, "heuristic")
            self._log_travel_perf_summary()
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
            self._clear_route_preview_metadata(updated)
            self._log_travel_perf_summary()
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
        committed_schedule_blocks = deepcopy(updated.get("schedule_blocks") or [])
        committed_activities = deepcopy(updated.get("activities") or [])
        committed_unscheduled = deepcopy(updated.get("unscheduled_activities") or [])

        validation = self._validate_routes_with_service(updated, saved_locations)
        if validation.get("route_conflicts"):
            repair_meta = self._attempt_route_repair(updated, saved_locations, validation["route_conflicts"])
            if repair_meta:
                validation["route_repair_attempted"] = bool(repair_meta.get("route_repair_attempted", True))
                pending_suggestions = repair_meta.get("pending_repair_suggestions", [])
                repaired_validation = repair_meta.get("repaired_validation")
                repaired_envelope = repair_meta.get("repaired_envelope")
                repaired_conflicts = list((repaired_validation or {}).get("route_conflicts") or [])
                residual_fixed_route_conflicts = self._fixed_route_conflicts(repaired_conflicts)
                blocking_repaired_conflicts = self._blocking_route_conflicts(repaired_conflicts)
                has_usable_repair_candidate = bool(
                    repaired_envelope
                    and repaired_validation
                    and self._has_renderable_repaired_chronology(repaired_validation)
                    and not blocking_repaired_conflicts
                    and (not pending_suggestions or residual_fixed_route_conflicts)
                    and (not residual_fixed_route_conflicts or repair_meta.get("travel_validation_status") == "partial_feasible_with_fixed_route_conflicts")
                )
                validation["pending_repair_suggestions"] = pending_suggestions
                validation["unfit_activities"] = repair_meta.get("unfit_activities", [])
                validation["optional_skipped"] = repair_meta.get("optional_skipped", [])
                validation["blocked_activities"] = repair_meta.get("blocked_activities", [])
                validation["route_repair_actions"] = []
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
                if has_usable_repair_candidate:
                    validation["schedule_blocks"] = repaired_validation.get(
                        "schedule_blocks",
                        repaired_envelope.get("schedule_blocks", validation.get("schedule_blocks", [])),
                    )
                    validation["activities"] = repaired_envelope.get("activities", updated.get("activities", []))
                    validation["unscheduled_activities"] = repaired_envelope.get(
                        "unscheduled_activities",
                        updated.get("unscheduled_activities", []),
                    )
                    validation["route_conflicts"] = repaired_conflicts
                    validation["warnings"] = repaired_validation.get("warnings", validation.get("warnings", []))
                    validation["updated_transition_count"] = repaired_validation.get(
                        "updated_transition_count",
                        validation.get("updated_transition_count", 0),
                    )
                    if repaired_validation.get("start_route_summary"):
                        validation["start_route_summary"] = repaired_validation.get("start_route_summary")
                    validation["route_repair_actions"] = repair_meta.get("route_repair_actions", [])
                    if residual_fixed_route_conflicts:
                        validation["travel_validation_status"] = "partial_feasible_with_fixed_route_conflicts"
                    else:
                        validation["travel_validation_status"] = repair_meta.get(
                            "travel_validation_status",
                            validation.get("travel_validation_status"),
                        )
                    preview_status = validation.get("travel_validation_status")
                    if preview_status in {
                        "repaired_validated",
                        "partial_feasible_with_unfit",
                        "partial_feasible_with_fixed_route_conflicts",
                    }:
                        preview_id = f"preview_{uuid4().hex[:12]}"
                        schedule_version = int(updated.get("version") or 1)
                        bound_suggestions = self._bind_preview_to_repair_suggestions(
                            validation.get("pending_repair_suggestions", []),
                            preview_id,
                            schedule_version,
                        )
                        validation["pending_repair_suggestions"] = bound_suggestions
                        repair_meta["pending_repair_suggestions"] = bound_suggestions
                        validation["preview_id"] = preview_id
                        validation["preview_base_version"] = schedule_version
                        validation["preview_status"] = preview_status
                        validation["preview_reason"] = self._route_preview_reason(
                            preview_status,
                            validation,
                            repair_meta,
                        )
                        validation["preview_schedule"] = self._build_route_preview_schedule(
                            current=updated,
                            repaired_envelope=repaired_envelope,
                            repaired_validation=repaired_validation,
                            repair_meta=repair_meta,
                            preview_status=preview_status,
                        )
        updated["location_resolution_requests"] = []
        updated["travel_validation_status"] = validation["travel_validation_status"]
        updated["route_conflicts"] = validation.get("route_conflicts", [])
        updated["route_repair_attempted"] = bool(validation.get("route_repair_attempted", False))
        updated["pending_repair_suggestions"] = validation.get("pending_repair_suggestions", [])
        updated["unfit_activities"] = self._dedupe_route_repair_items(
            validation.get("unfit_activities", []),
            item_type="unfit",
        )[0]
        updated["optional_skipped"] = self._dedupe_route_repair_items(
            validation.get("optional_skipped", updated.get("optional_skipped", [])),
            item_type="optional_skipped",
        )[0]
        updated["blocked_activities"] = validation.get("blocked_activities", [])
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
            updated["unscheduled_activities"] = self._dedupe_route_repair_items(
                updated.get("unscheduled_activities", []),
                item_type="unscheduled",
            )[0]
        updated["updated_transition_count"] = int(validation.get("updated_transition_count") or 0)
        updated["schedule_blocks"] = validation.get("schedule_blocks", updated.get("schedule_blocks", []))
        if validation.get("preview_schedule"):
            updated["committed_schedule_blocks"] = committed_schedule_blocks
            updated["preview_id"] = validation.get("preview_id")
            updated["preview_base_version"] = validation.get("preview_base_version")
            updated["preview_status"] = validation.get("preview_status")
            updated["preview_reason"] = validation.get("preview_reason")
            updated["preview_schedule"] = validation.get("preview_schedule")
            updated["schedule_blocks"] = committed_schedule_blocks
            updated["activities"] = committed_activities
            if validation.get("preview_status") != "partial_feasible_with_unfit":
                updated["unscheduled_activities"] = committed_unscheduled
        else:
            self._clear_route_preview_metadata(updated)
        self._sync_resolved_locations_to_activities(updated)
        return_home_conflict = self._return_home_deadline_conflict(updated)
        if return_home_conflict:
            updated["travel_validation_status"] = "return_home_deadline_conflict"
            updated["schedule_status"] = "route_conflict"
            updated["status"] = "route_conflict"
            updated.setdefault("route_conflicts", [])
            updated["route_conflicts"] = list(updated.get("route_conflicts") or []) + [return_home_conflict]
            updated.setdefault("validation_issues", [])
            updated["validation_issues"] = list(dict.fromkeys(
                list(updated.get("validation_issues") or []) + [return_home_conflict["reason"]]
            ))
            self._set_return_home_deadline_status(updated, "violated", return_home_conflict.get("required_arrival"))
        else:
            self._set_return_home_deadline_status(updated, "satisfied", None)
        if validation.get("warnings"):
            updated["warnings"] = list(updated.get("warnings") or []) + validation["warnings"]
            if updated.get("status") == "ok":
                updated["status"] = "warning"
            if updated.get("schedule_status") == "ok":
                updated["schedule_status"] = "warning"

        if updated.get("travel_validation_status") == "return_home_deadline_conflict":
            pass
        elif updated.get("travel_validation_status") == "repair_suggestion_pending":
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
        elif updated.get("travel_validation_status") == "partial_feasible_with_fixed_route_conflicts":
            updated["schedule_status"] = "partial"
            updated["status"] = "partial"
            updated.setdefault("validation_issues", [])
            for conflict in updated.get("route_conflicts") or []:
                if conflict.get("reason"):
                    updated["validation_issues"].append(conflict["reason"])
            for item in updated.get("unfit_activities") or []:
                title = item.get("title") or "One flexible activity"
                updated["validation_issues"].append(f"{title} could not fit after accurate travel validation.")
            updated["validation_issues"] = list(dict.fromkeys(updated["validation_issues"]))
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
        self._log_travel_perf_summary()
        jlog("TIMER", f"accurate_travel_validation_seconds={time.perf_counter() - validation_started:.2f}", None)
        return updated

    def _set_return_home_deadline_status(
        self,
        envelope: Dict[str, Any],
        status: str,
        arrival_time: Optional[Any],
    ) -> None:
        constraints = envelope.get("schedule_constraints") or (envelope.get("preferences") or {}).get("schedule_constraints") or {}
        if parse_clock(constraints.get("return_home_deadline")) is None:
            return
        constraints["return_home_deadline_status"] = status
        if arrival_time:
            constraints["return_home_arrival_time"] = arrival_time
        envelope["schedule_constraints"] = constraints
        envelope.setdefault("preferences", {})["schedule_constraints"] = constraints
        jlog(
            "RETURN_HOME_DEADLINE",
            f"status={status} arrival_time={arrival_time or 'within_deadline'}",
            None,
        )

    def _clear_route_preview_metadata(self, envelope: Dict[str, Any]) -> None:
        for key in (
            "committed_schedule_blocks",
            "preview_id",
            "preview_base_version",
            "preview_status",
            "preview_reason",
            "preview_schedule",
        ):
            envelope.pop(key, None)

    def _return_home_deadline_conflict(self, envelope: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        constraints = (
            envelope.get("schedule_constraints")
            or (envelope.get("preferences") or {}).get("schedule_constraints")
            or {}
        )
        deadline = parse_clock(constraints.get("return_home_deadline"))
        if deadline is None:
            return None
        activity_blocks = [
            block for block in envelope.get("schedule_blocks") or []
            if (block.get("block_type") or block.get("type")) == "activity"
            and block.get("end")
        ]
        if not activity_blocks:
            return None
        last = max(activity_blocks, key=lambda block: parse_clock(block.get("end")) or 0)
        last_end = parse_clock(last.get("end"))
        if last_end is None:
            return None
        location_text = clean_title(
            " ".join(
                str(value or "")
                for value in (
                    last.get("location_label"),
                    last.get("location"),
                    last.get("location_category"),
                    last.get("title"),
                )
            )
        )
        if "home" in location_text:
            required_arrival = last_end
            travel_minutes = 0
        else:
            preferences = envelope.get("preferences") or {}
            home = (
                preferences.get("home_location")
                or preferences.get("default_home_location")
                or preferences.get("default_start_location")
                or {}
            )
            home_label = (
                home.get("label")
                or home.get("display_name")
                or home.get("address")
                if isinstance(home, dict)
                else None
            ) or "home"
            travel_minutes = estimate_travel_minutes(last.get("location") or last.get("location_label") or last.get("title"), home_label)
            required_arrival = last_end + travel_minutes
        if required_arrival <= deadline:
            return None
        return {
            "reason_code": "return_home_deadline_conflict",
            "type": "return_home_deadline_conflict",
            "from_activity": last.get("title"),
            "deadline": format_clock(deadline),
            "required_arrival": format_clock(required_arrival),
            "required_travel": travel_minutes,
            "reason": (
                f"Return-home deadline missed: after {last.get('title')}, arriving home would be "
                f"about {format_clock(required_arrival)}, later than {format_clock(deadline)}."
            ),
        }

    def _bind_preview_to_repair_suggestions(
        self,
        suggestions: List[Dict[str, Any]],
        preview_id: str,
        schedule_version: int,
    ) -> List[Dict[str, Any]]:
        bound: List[Dict[str, Any]] = []
        for suggestion in suggestions or []:
            updated = deepcopy(suggestion)
            updated["preview_id"] = preview_id
            updated["schedule_version"] = schedule_version
            cascades: List[Dict[str, Any]] = []
            for cascade in updated.get("cascade_suggestions") or []:
                cascade_updated = deepcopy(cascade)
                if cascade_updated.get("from_start") is not None:
                    cascade_updated.setdefault("from", format_clock(int(cascade_updated.get("from_start") or 0)))
                if cascade_updated.get("from_end") is not None:
                    cascade_updated.setdefault("from_end_label", format_clock(int(cascade_updated.get("from_end") or 0)))
                if cascade_updated.get("to_start") is not None:
                    cascade_updated.setdefault("to", format_clock(int(cascade_updated.get("to_start") or 0)))
                if cascade_updated.get("to_end") is not None:
                    cascade_updated.setdefault("to_end_label", format_clock(int(cascade_updated.get("to_end") or 0)))
                cascades.append(cascade_updated)
            updated["cascade_suggestions"] = cascades
            bound.append(updated)
        return bound

    def _route_preview_reason(
        self,
        preview_status: str,
        validation: Dict[str, Any],
        repair_meta: Dict[str, Any],
    ) -> str:
        if preview_status == "partial_feasible_with_unfit":
            unfit = repair_meta.get("unfit_activities") or validation.get("unfit_activities") or []
            title = (unfit[0] or {}).get("title") if unfit else None
            return (
                f"Suggested route-aware plan excludes {title} because it could not fit."
                if title
                else "Suggested route-aware plan excludes activities that could not fit."
            )
        if preview_status == "repair_suggestion_pending":
            suggestions = repair_meta.get("pending_repair_suggestions") or validation.get("pending_repair_suggestions") or []
            title = (suggestions[0] or {}).get("title") if suggestions else None
            if any(self._repair_suggestion_requires_fixed_move_approval(suggestion) for suggestion in suggestions):
                return (
                    "This preview is not fully feasible because fixed events cannot be reached on time. "
                    "Fixed-event repair suggestions are advisory until you explicitly allow moving the fixed event."
                )
            return (
                f"Suggested route-aware plan needs confirmation before moving {title}."
                if title
                else "Suggested route-aware plan needs confirmation before moving protected events."
            )
        if preview_status == "partial_feasible_with_fixed_route_conflicts":
            return "Suggested route-aware plan keeps fixed events locked and marks unresolved fixed-route conflicts."
        return "Suggested route-aware plan is ready to apply."

    def _build_route_preview_schedule(
        self,
        *,
        current: Dict[str, Any],
        repaired_envelope: Optional[Dict[str, Any]],
        repaired_validation: Optional[Dict[str, Any]],
        repair_meta: Dict[str, Any],
        preview_status: str,
    ) -> Dict[str, Any]:
        source = deepcopy(repaired_envelope or current)
        validation = repaired_validation or {}
        preview = {
            "activities": source.get("activities", current.get("activities", [])),
            "schedule_blocks": validation.get("schedule_blocks") or source.get("schedule_blocks") or current.get("schedule_blocks", []),
            "start_route_summary": validation.get("start_route_summary") or repair_meta.get("start_route_summary") or current.get("start_route_summary"),
            "route_repair_actions": repair_meta.get("route_repair_actions", []),
            "pending_repair_suggestions": repair_meta.get("pending_repair_suggestions", []),
            "unfit_activities": repair_meta.get("unfit_activities", []),
            "optional_skipped": repair_meta.get("optional_skipped", []),
            "blocked_activities": repair_meta.get("blocked_activities", []),
            "unscheduled_activities": source.get("unscheduled_activities", current.get("unscheduled_activities", [])),
            "travel_validation_status": preview_status,
            "route_conflicts": validation.get("route_conflicts", []),
            "warnings": validation.get("warnings", []),
        }
        if preview_status == "repair_suggestion_pending" and preview.get("pending_repair_suggestions"):
            preview["schedule_blocks"] = self._preview_blocks_with_repair_suggestions(
                preview.get("schedule_blocks") or current.get("schedule_blocks", []),
                preview["pending_repair_suggestions"],
            )
        return preview

    def _preview_blocks_with_repair_suggestions(
        self,
        blocks: List[Dict[str, Any]],
        suggestions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        preview_blocks = deepcopy(blocks or [])
        moves: List[Dict[str, Any]] = []
        for suggestion in suggestions or []:
            if self._repair_suggestion_requires_fixed_move_approval(suggestion):
                continue
            moves.append({
                "activity_id": suggestion.get("activity_id"),
                "title": suggestion.get("title"),
                "to": suggestion.get("to"),
                "to_end": suggestion.get("to_end"),
            })
            for cascade in suggestion.get("cascade_suggestions") or []:
                if clean_title(cascade.get("impact_type") or "") in {"fixed_target_move", "protected_cascade_move"}:
                    continue
                moves.append({
                    "activity_id": cascade.get("activity_id"),
                    "title": cascade.get("title"),
                    "to": cascade.get("to") or format_clock(int(cascade.get("to_start") or 0)),
                    "to_end": cascade.get("to_end_label") or format_clock(int(cascade.get("to_end") or 0)),
                })
        for move in moves:
            new_start = move.get("to")
            new_end = move.get("to_end")
            if not new_start:
                continue
            for block in preview_blocks:
                if not self._is_activity_schedule_block(block):
                    continue
                block_id = str(block.get("stable_activity_id") or block.get("id") or "")
                title = clean_title(block.get("title") or "")
                if (
                    (move.get("activity_id") and block_id == str(move.get("activity_id")))
                    or (move.get("title") and title == clean_title(move.get("title")))
                ):
                    old_start = block.get("start") or block.get("startTime")
                    old_end = block.get("end") or block.get("endTime")
                    block["preview_original_start"] = old_start
                    block["preview_original_end"] = old_end
                    block["start"] = new_start
                    block["startTime"] = new_start
                    if new_end:
                        block["end"] = new_end
                        block["endTime"] = new_end
                    block["preview_pending_confirmation"] = True
                    break
        return sorted(
            preview_blocks,
            key=lambda block: parse_clock(block.get("start") or block.get("startTime") or "") or 0,
        )

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

    def _coordinate_distance_meters(
        self,
        left: Tuple[float, float],
        right: Tuple[float, float],
    ) -> float:
        lat1, lon1 = math.radians(float(left[0])), math.radians(float(left[1]))
        lat2, lon2 = math.radians(float(right[0])), math.radians(float(right[1]))
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return 6371000.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    def _near_location_estimate_minutes(self, distance_meters: float) -> int:
        if distance_meters <= 25:
            return 0
        return 3 if distance_meters <= 150 else 5

    def _route_matrix_cache_key(
        self,
        left_coord: Tuple[float, float],
        right_coord: Tuple[float, float],
        transport_mode: str,
    ) -> Tuple[float, float, float, float, str]:
        return (
            round(float(left_coord[0]), 5),
            round(float(left_coord[1]), 5),
            round(float(right_coord[0]), 5),
            round(float(right_coord[1]), 5),
            transport_mode,
        )

    def _route_minutes_for_matrix_pair(
        self,
        *,
        left: Dict[str, Any],
        right: Dict[str, Any],
        left_coord: Tuple[float, float],
        right_coord: Tuple[float, float],
        transport_mode: str,
        matrix_cache: Dict[Tuple[float, float, float, float, str], Dict[str, Any]],
    ) -> Tuple[int, str, bool, Optional[float]]:
        distance_m = self._coordinate_distance_meters(left_coord, right_coord)
        if distance_m <= 1:
            return 0, "same_location", False, distance_m
        if distance_m <= NEAR_LOCATION_THRESHOLD_METERS:
            return self._near_location_estimate_minutes(distance_m), "near_location", False, distance_m

        cache_key = self._route_matrix_cache_key(left_coord, right_coord, transport_mode)
        if cache_key in matrix_cache:
            cached = matrix_cache[cache_key]
            return int(cached.get("duration_minutes") or 0), str(cached.get("source") or "route_cache"), False, distance_m

        before_stats = self.travel_service.stats_snapshot() if hasattr(self.travel_service, "stats_snapshot") else {}
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
                time_bucket=None,
            )
            after_stats = self.travel_service.stats_snapshot() if hasattr(self.travel_service, "stats_snapshot") else {}
            source = (
                "route_cache"
                if int(after_stats.get("route_cache_hits") or 0) > int(before_stats.get("route_cache_hits") or 0)
                else "routing_service"
            )
            jlog_verbose(
                "TRAVEL_SERVICE",
                f"route result: {left.get('location_label') or left.get('title')} -> {right.get('location_label') or right.get('title')} = {route_minutes} min",
                "ORS",
            )
        except (MissingORSApiKey, TravelServiceError, Exception):
            route_minutes = estimate_travel_minutes(left.get("location"), right.get("location"))
            source = "fallback"
        matrix_cache[cache_key] = {"duration_minutes": int(route_minutes or 0), "source": source}
        return int(route_minutes or 0), source, source == "routing_service", distance_m

    def _activity_pair_from_physical_pair(
        self,
        *,
        left_key: str,
        left: Dict[str, Any],
        right_key: str,
        right: Dict[str, Any],
        physical_pair: Dict[str, Any],
        transport_mode: str,
    ) -> Dict[str, Any]:
        return {
            "from_key": left_key,
            "to_key": right_key,
            "from_title": left.get("title"),
            "to_title": right.get("title"),
            "from_location": left.get("location"),
            "to_location": right.get("location"),
            "duration_minutes": int(physical_pair.get("duration_minutes") or 0),
            "source": physical_pair.get("source"),
            "distance_meters": physical_pair.get("distance_meters"),
            "transport_mode": transport_mode,
            "from_coordinate": deepcopy(left.get("coordinate")),
            "to_coordinate": deepcopy(right.get("coordinate")),
        }

    def _build_route_context(
        self,
        envelope: Dict[str, Any],
        activities: List[Dict[str, Any]],
        saved_locations: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        route_matrix_started = time.perf_counter()
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

        physical_nodes: Dict[str, Dict[str, Any]] = {}
        activity_to_physical: Dict[str, str] = {}
        for activity_key, node in nodes.items():
            coord = (node["coordinate"]["latitude"], node["coordinate"]["longitude"])
            matched_key = ""
            matched_distance: Optional[float] = None
            for physical_key, physical in physical_nodes.items():
                physical_coord = (physical["coordinate"]["latitude"], physical["coordinate"]["longitude"])
                distance_m = self._coordinate_distance_meters(coord, physical_coord)
                if distance_m <= NEAR_LOCATION_THRESHOLD_METERS:
                    matched_key = physical_key
                    matched_distance = distance_m
                    break
            if not matched_key:
                matched_key = f"phys:{len(physical_nodes) + 1}"
                physical_nodes[matched_key] = {
                    "key": matched_key,
                    "coordinate": deepcopy(node.get("coordinate")),
                    "location": node.get("location"),
                    "location_label": node.get("location_label"),
                    "title": node.get("title"),
                    "activity_keys": [],
                    "activities": [],
                }
            physical_nodes[matched_key]["activity_keys"].append(activity_key)
            physical_nodes[matched_key]["activities"].append(node)
            activity_to_physical[activity_key] = matched_key
            if matched_distance is not None:
                stage = "SAME_LOCATION_HIT" if matched_distance <= 1 else "NEAR_LOCATION_HIT"
                jlog(
                    "ROUTE_CACHE",
                    (
                        f"activity={node.get('title')} physical_node={matched_key} "
                        f"distance_m={round(float(matched_distance), 1)}"
                    ),
                    stage,
                )
                if matched_distance > 1:
                    existing_titles = [activity.get("title") for activity in physical_nodes[matched_key].get("activities", [])]
                    jlog(
                        "NEAR_LOCATION_GROUP",
                        (
                            f"activities={existing_titles + [node.get('title')]} "
                            f"distance_m={round(float(matched_distance), 1)} "
                            f"estimate={self._near_location_estimate_minutes(matched_distance)}"
                        ),
                        None,
                    )

        jlog(
            "ROUTE_MATRIX",
            f"raw_activities={len(nodes)} unique_nodes={len(physical_nodes)}",
            "PHYSICAL_NODES",
        )

        physical_pairs: Dict[str, Dict[str, Any]] = {}
        matrix_cache: Dict[Tuple[float, float, float, float, str], Dict[str, Any]] = {}
        for left_physical_key, left_physical in physical_nodes.items():
            for right_physical_key, right_physical in physical_nodes.items():
                if left_physical_key == right_physical_key:
                    continue
                pair_key = f"{left_physical_key}->{right_physical_key}"
                left_coord = (left_physical["coordinate"]["latitude"], left_physical["coordinate"]["longitude"])
                right_coord = (right_physical["coordinate"]["latitude"], right_physical["coordinate"]["longitude"])
                route_minutes, source, fetched, distance_m = self._route_minutes_for_matrix_pair(
                    left=left_physical,
                    right=right_physical,
                    left_coord=left_coord,
                    right_coord=right_coord,
                    transport_mode=transport_mode,
                    matrix_cache=matrix_cache,
                )
                if source == "fallback":
                    fallback_used = True
                if source == "near_location":
                    jlog(
                        "NEAR_LOCATION_GROUP",
                        (
                            f"activities={[activity.get('title') for activity in left_physical.get('activities', []) + right_physical.get('activities', [])]} "
                            f"distance_m={round(float(distance_m or 0), 1)} estimate={route_minutes}"
                        ),
                        None,
                    )
                physical_pairs[pair_key] = {
                    "from_key": left_physical_key,
                    "to_key": right_physical_key,
                    "from_title": left_physical.get("title"),
                    "to_title": right_physical.get("title"),
                    "from_location": left_physical.get("location"),
                    "to_location": right_physical.get("location"),
                    "duration_minutes": int(route_minutes or 0),
                    "source": source,
                    "distance_meters": round(float(distance_m), 2) if distance_m is not None else None,
                    "transport_mode": transport_mode,
                    "from_coordinate": deepcopy(left_physical.get("coordinate")),
                    "to_coordinate": deepcopy(right_physical.get("coordinate")),
                    "fetched": fetched,
                }

        pairs: Dict[str, Dict[str, Any]] = {}
        for left_key, left in nodes.items():
            for right_key, right in nodes.items():
                if left_key == right_key:
                    continue
                pair_key = f"{left_key}->{right_key}"
                left_physical_key = activity_to_physical.get(left_key)
                right_physical_key = activity_to_physical.get(right_key)
                if not left_physical_key or not right_physical_key:
                    continue
                if left_physical_key == right_physical_key:
                    left_coord = (left["coordinate"]["latitude"], left["coordinate"]["longitude"])
                    right_coord = (right["coordinate"]["latitude"], right["coordinate"]["longitude"])
                    distance_m = self._coordinate_distance_meters(left_coord, right_coord)
                    source = "same_location" if distance_m <= 1 else "near_location"
                    route_minutes = 0 if source == "same_location" else self._near_location_estimate_minutes(distance_m)
                    physical_pair = {
                        "duration_minutes": route_minutes,
                        "source": source,
                        "distance_meters": round(float(distance_m), 2),
                        "fetched": False,
                    }
                else:
                    physical_pair = physical_pairs.get(f"{left_physical_key}->{right_physical_key}") or {}
                pairs[pair_key] = self._activity_pair_from_physical_pair(
                    left_key=left_key,
                    left=left,
                    right_key=right_key,
                    right=right,
                    physical_pair=physical_pair,
                    transport_mode=transport_mode,
                )
                debug_message = (
                    f"from={left.get('title')} to={right.get('title')} "
                    f"fetched={str(bool(physical_pair.get('fetched'))).lower()} "
                    f"duration={pairs[pair_key].get('duration_minutes')} "
                    f"source={pairs[pair_key].get('source')} "
                    f"distance_m={pairs[pair_key].get('distance_meters')}"
                )
                jlog_verbose("ROUTE_PAIR_DEBUG", debug_message, None)

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
            if pair.get("source") in {"same_location", "near_location"} or int(pair.get("duration_minutes") or 0) <= 2:
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
                "near_location": any(
                    pair.get("source") == "near_location"
                    for pair in pairs.values()
                    if pair.get("from_key") in keys and pair.get("to_key") in keys
                ),
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
                distance_m = self._coordinate_distance_meters(start_coord, node_coord)
                if distance_m <= 1:
                    route_minutes = 0
                    source = "same_location"
                elif distance_m <= NEAR_LOCATION_THRESHOLD_METERS:
                    route_minutes = self._near_location_estimate_minutes(distance_m)
                    source = "near_location"
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

        elapsed = time.perf_counter() - route_matrix_started
        self._add_travel_perf_time("route_matrix_seconds", elapsed)
        jlog("TIMER", f"route_matrix_seconds={elapsed:.2f}", None)
        jlog("ROUTE_CONTEXT", f"pairs={len(pairs)} missing={missing}", None)
        return {
            "enabled": True,
            "transport_mode": transport_mode,
            "nodes": nodes,
            "physical_nodes": physical_nodes,
            "activity_to_physical_node": activity_to_physical,
            "pairs": pairs,
            "physical_pairs": physical_pairs,
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
        route_refit_started = time.perf_counter()
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
        raw_unscheduled = list(planned.get("unscheduled_activities", [])) + conflict_unfit
        repaired["unscheduled_activities"] = [
            self._format_activity(item)
            for item in raw_unscheduled
        ]
        optional_skipped = self._optional_skipped_from_unscheduled(raw_unscheduled)
        validation = self._final_validate_route_aware_repair(
            original=envelope,
            repaired=repaired,
            route_context=route_context,
        )
        backfill_result = self._post_repair_flexible_backfill(
            original=envelope,
            repaired=repaired,
            validation=validation,
            route_context=route_context,
        )
        if backfill_result:
            repaired = backfill_result["repaired"]
            validation = backfill_result["validation"]
        preference_rescue_result = self._post_repair_preference_rescue(
            original=envelope,
            repaired=repaired,
            validation=validation,
            route_context=route_context,
        )
        if preference_rescue_result:
            repaired = preference_rescue_result["repaired"]
            validation = preference_rescue_result["validation"]
        repaired_preferences = deepcopy(repaired.get("preferences") or {})
        repaired_preferences.pop("_route_context", None)
        repaired_preferences.pop("route_aware_repair", None)
        repaired["preferences"] = repaired_preferences
        semantic_violations = self._protected_semantic_violations(envelope, repaired)
        if semantic_violations:
            validation.setdefault("route_conflicts", [])
            validation["route_conflicts"].extend(semantic_violations)
            validation["travel_validation_status"] = "route_conflict"
        suggestions = self._protected_repair_suggestions_from_conflicts(
            envelope,
            validation.get("route_conflicts", []),
        )
        remaining_conflicts = validation.get("route_conflicts", [])
        blocking_conflicts = self._blocking_route_conflicts(remaining_conflicts)
        unfit = self._repair_unfit_activities(
            active,
            [
                item for item in raw_unscheduled
                if not self._route_repair_item_is_optional(item)
            ],
            blocking_conflicts,
        )
        blocked = self._blocked_activities_for_fixed_route_conflict(
            active,
            raw_unscheduled,
            remaining_conflicts,
        )
        route_repair_actions = self._route_repair_actions_from_delta(
            envelope,
            repaired,
            route_context,
        )
        self._log_preference_window_violations(repaired)
        unfit = self._filter_items_not_scheduled_in_repair(unfit, repaired)
        blocked = self._filter_items_not_scheduled_in_repair(blocked, repaired)
        optional_skipped = self._filter_items_not_scheduled_in_repair(optional_skipped, repaired)
        unfit = self._preserve_existing_unfit_activities(envelope, repaired, unfit)
        accounting_source = self._original_repair_accounting_records(envelope, active)
        accounting = self._account_for_route_repair_activities(
            accounting_source,
            repaired,
            unfit,
            optional_skipped,
        )
        unfit = accounting["unfit_activities"]
        optional_skipped = accounting["optional_skipped"]
        missing_after_accounting = accounting["missing_after_accounting"]
        repaired["unfit_activities"] = unfit
        repaired["optional_skipped"] = optional_skipped
        route_efficiency = planned.get("route_efficiency") or {}
        residual_fixed_conflicts = self._fixed_route_conflicts(remaining_conflicts)
        has_non_fixed_repair_scope = self._has_non_fixed_repair_scope(active, unfit, route_repair_actions)
        if blocking_conflicts and suggestions:
            status = "repair_suggestion_pending"
        elif residual_fixed_conflicts and has_non_fixed_repair_scope:
            status = "partial_feasible_with_fixed_route_conflicts"
        elif missing_after_accounting:
            status = "route_conflict"
        elif not blocking_conflicts and unfit:
            status = "partial_feasible_with_unfit"
        elif not blocking_conflicts and route_repair_actions:
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
        result = {
            "route_repair_attempted": True,
            "pending_repair_suggestions": suggestions,
            "unfit_activities": unfit,
            "optional_skipped": optional_skipped,
            "blocked_activities": blocked,
            "travel_validation_status": status,
            "route_repair_actions": route_repair_actions,
            "route_efficiency": route_efficiency,
            "repaired_envelope": repaired,
            "repaired_validation": validation,
            "start_route_summary": validation.get("start_route_summary"),
        }
        elapsed = time.perf_counter() - route_refit_started
        self._add_travel_perf_time("route_aware_refit_seconds", elapsed)
        jlog("TIMER", f"route_aware_refit_seconds={elapsed:.2f}", None)
        return result

    def _preserve_existing_unfit_activities(
        self,
        envelope: Dict[str, Any],
        repaired: Dict[str, Any],
        unfit: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        preserved, duplicate_keys = self._dedupe_route_repair_items(unfit or [], item_type="unfit")
        existing_keys = {self._route_repair_activity_key(item) for item in preserved}
        scheduled_keys = set()
        for item in (repaired.get("activities") or []) + (repaired.get("schedule_blocks") or []):
            if isinstance(item, dict):
                key = self._route_repair_activity_key(item)
                if key:
                    scheduled_keys.add(key)
        for item in (envelope.get("unfit_activities") or []) + (envelope.get("unscheduled_activities") or []):
            if not isinstance(item, dict):
                continue
            key = self._route_repair_activity_key(item)
            if not key or key in scheduled_keys:
                continue
            if key in existing_keys:
                duplicate_keys.append(key)
                jlog(
                    "UNFIT",
                    f"title={item.get('title')} reason=duplicate_existing_unfit",
                    "MERGE",
                )
                jlog(
                    "UNFIT",
                    f"title={item.get('title')} source=envelope deduped=true",
                    "PRESERVE",
                )
                preserved = self._merge_route_repair_item(preserved, item, key)
                continue
            preserved.append(deepcopy(item))
            existing_keys.add(key)
            jlog("UNFIT", f"title={item.get('title')} source=envelope", "PRESERVE")
        return self._dedupe_route_repair_items(preserved, item_type="unfit")[0]

    def _original_repair_accounting_records(
        self,
        envelope: Dict[str, Any],
        active: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for collection in (
            envelope.get("activities") or [],
            active or [],
            envelope.get("unfit_activities") or [],
            envelope.get("unscheduled_activities") or [],
        ):
            for item in collection or []:
                if not isinstance(item, dict):
                    continue
                if self._route_repair_item_is_schedule_constraint(item):
                    continue
                if not self._is_activity_entry(item):
                    continue
                if self._is_generic_system_activity_payload(item):
                    continue
                if str(item.get("status") or "active") != "active":
                    continue
                key = self._route_repair_activity_key(item)
                if not key or key in seen:
                    continue
                seen.add(key)
                records.append(item)
        return records

    def _post_repair_flexible_backfill(
        self,
        *,
        original: Dict[str, Any],
        repaired: Dict[str, Any],
        validation: Dict[str, Any],
        route_context: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if not repaired.get("activities") or not validation.get("schedule_blocks"):
            jlog("MODULE_D", "reason=missing_repaired_chronology", "BACKFILL][SKIP")
            return None
        if self._blocking_route_conflicts(validation.get("route_conflicts", [])):
            jlog("MODULE_D", "reason=blocking_route_conflicts", "BACKFILL][SKIP")
            return None

        current = deepcopy(repaired)
        current_validation = deepcopy(validation)
        current_travel_total = self._route_sequence_total_minutes(current, route_context)
        applied: List[Dict[str, Any]] = []
        moved_activity_ids: set[str] = set()

        for _ in range(5):
            move = self._best_post_repair_backfill_move(
                current=current,
                original=original,
                validation=current_validation,
                route_context=route_context,
                current_travel_total=current_travel_total,
                moved_activity_ids=moved_activity_ids,
            )
            if not move:
                break

            candidate = self._apply_backfill_move(current, move, route_context)
            candidate_validation = self._final_validate_route_aware_repair(
                original=original,
                repaired=candidate,
                route_context=route_context,
            )
            blocking = self._blocking_route_conflicts(candidate_validation.get("route_conflicts", []))
            candidate_travel_total = self._route_sequence_total_minutes(candidate, route_context)
            if blocking:
                jlog(
                    "MODULE_D",
                    f"candidate={move['title']} reason=validation_failed conflicts={len(blocking)}",
                    "BACKFILL][SKIP",
                )
                self._mark_backfill_rejected(current, move)
                continue
            if candidate_travel_total > current_travel_total:
                jlog(
                    "MODULE_D",
                    (
                        f"candidate={move['title']} reason=travel_worse "
                        f"before={current_travel_total} after={candidate_travel_total}"
                    ),
                    "BACKFILL][SKIP",
                )
                self._mark_backfill_rejected(current, move)
                continue

            jlog(
                "MODULE_D",
                (
                    f"moved={move['title']} from={format_clock(move['from_start'])}-"
                    f"{format_clock(move['from_end'])} to={format_clock(move['to_start'])}-"
                    f"{format_clock(move['to_end'])} score_delta={move['score_delta']:.2f}"
                ),
                "BACKFILL][APPLY",
            )
            applied.append(move)
            moved_activity_ids.add(str(move.get("activity_id") or ""))
            current = candidate
            current_validation = candidate_validation
            current_travel_total = candidate_travel_total

        if not applied:
            jlog("MODULE_D", "reason=no_valid_improvement", "BACKFILL][SKIP")
            return None
        jlog("MODULE_D", f"moved={len(applied)}", "BACKFILL][STOP")
        current.setdefault("route_repair_actions", [])
        for move in applied:
            current["route_repair_actions"].append({
                "type": "move_activity",
                "title": move.get("title"),
                "activity_id": move.get("activity_id"),
                "from": format_clock(move["from_start"]),
                "from_end": format_clock(move["from_end"]),
                "to": format_clock(move["to_start"]),
                "to_end": format_clock(move["to_end"]),
                "reason": "Backfilled into an earlier route-safe free slot after accurate travel repair.",
            })
        return {"repaired": current, "validation": current_validation}

    def _post_repair_preference_rescue(
        self,
        *,
        original: Dict[str, Any],
        repaired: Dict[str, Any],
        validation: Dict[str, Any],
        route_context: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if not repaired.get("activities") or not validation.get("schedule_blocks"):
            return None
        if self._blocking_route_conflicts(validation.get("route_conflicts", [])):
            jlog("PREF_RESCUE", "reason=blocking_route_conflicts", "REJECT")
            return None

        current = deepcopy(repaired)
        current_validation = deepcopy(validation)
        current_travel_total = self._route_sequence_total_minutes(current, route_context)
        applied: List[Dict[str, Any]] = []

        violations: List[Dict[str, Any]] = []
        for item in current.get("activities") or []:
            if not self._activity_can_auto_move_for_route_repair(item):
                continue
            start, end = self._activity_time_bounds(item)
            if not should_run_preference_rescue(item, start, end):
                continue
            pref = preference_window_deviation(item, start, end)
            info = pref.get("info") or {}
            jlog(
                "PREF_WINDOW",
                (
                    f"title={item.get('title')} source={info.get('source')} "
                    f"window={format_clock(info.get('acceptable_start'))}-{format_clock(info.get('acceptable_end'))} "
                    f"weight={info.get('weight')}"
                ),
                "DETECT",
            )
            violations.append({
                "item": item,
                "start": start,
                "end": end,
                "penalty": float(pref.get("penalty") or 0),
                "deviation": int(pref.get("deviation") or 0),
            })
        if not violations:
            return None

        attempts_by_key = deepcopy(current.get("preference_rescue_attempts") or {})
        candidate_stats = {"generated": 0, "pruned": 0, "validated": 0}
        for violation in sorted(violations, key=lambda entry: entry["penalty"], reverse=True)[:2]:
            activity = violation["item"]
            title = activity.get("title") or "Activity"
            activity_key = self._route_repair_activity_key(activity)
            attempts: List[Dict[str, Any]] = []
            jlog(
                "PREF_RESCUE",
                (
                    f"title={title} current_start={format_clock(violation['start'])} "
                    f"deviation={violation['deviation']}"
                ),
                "START",
            )
            cluster_started = time.perf_counter()
            blocker_moves = self._preference_rescue_blocker_reorder_moves(current, activity, route_context)
            blocker_titles = [move.get("blocker_title") for move in blocker_moves]
            jlog(
                "PREF_RESCUE",
                f"title={title} blockers={blocker_titles}",
                "BLOCKER_SCAN",
            )
            accepted = False
            for move in blocker_moves[:3]:
                candidate_stats["generated"] += 1
                candidate_order = [move.get("title"), move.get("blocker_title")]
                jlog(
                    "PREF_RESCUE",
                    f"title={title} order={candidate_order}",
                    "REORDER_CANDIDATE",
                )
                candidate = self._apply_preference_reorder_move(current, move, route_context)
                candidate_validation = self._final_validate_route_aware_repair(
                    original=original,
                    repaired=candidate,
                    route_context=route_context,
                )
                blocking = self._blocking_route_conflicts(candidate_validation.get("route_conflicts", []))
                if blocking:
                    candidate_stats["pruned"] += 1
                    attempts.append({
                        "candidate_order": candidate_order,
                        "rejection_reason": "route_validation_failed",
                    })
                    jlog("PREF_RESCUE", f"title={title} order={candidate_order} reason=route_validation_failed", "REJECT")
                    continue
                candidate_stats["validated"] += 1
                candidate_travel_total = self._route_sequence_total_minutes(candidate, route_context)
                route_delta = candidate_travel_total - current_travel_total
                accepted_tradeoff = move["score_delta"] > 0
                jlog(
                    "PREF_RESCUE",
                    (
                        f"title={title} pref_improvement_minutes={move['pref_improvement_minutes']} "
                        f"route_delta={route_delta} accepted={str(accepted_tradeoff).lower()}"
                    ),
                    "TRADEOFF",
                )
                if not accepted_tradeoff:
                    candidate_stats["pruned"] += 1
                    attempts.append({
                        "candidate_order": candidate_order,
                        "rejection_reason": "weighted_score_not_improved",
                    })
                    jlog("PREF_RESCUE", f"title={title} order={candidate_order} reason=weighted_score_not_improved", "REJECT")
                    continue
                jlog(
                    "PREF_RESCUE",
                    (
                        f"title={title} from={format_clock(move['from_start'])} "
                        f"to={format_clock(move['to_start'])} reason=reorder_same_location_blocker"
                    ),
                    "APPLY",
                )
                applied.append(move)
                current = candidate
                current_validation = candidate_validation
                current_travel_total = candidate_travel_total
                accepted = True
                break
            cluster_elapsed = time.perf_counter() - cluster_started
            self._add_travel_perf_time("cluster_reorder_seconds", cluster_elapsed)
            jlog("TIMER", f"cluster_reorder_seconds={cluster_elapsed:.2f}", None)
            if accepted:
                attempts_by_key[activity_key] = attempts
                continue
            free_slots = self._preference_rescue_slots_for_activity(
                current_validation.get("schedule_blocks", []),
                activity,
            )[:5]
            slot_log = [
                f"{format_clock(slot['start'])}-{format_clock(slot['end'])}"
                for slot in free_slots
            ]
            jlog(
                "PREF_RESCUE",
                f"title={title} free_slots={slot_log}",
                "FREE_SLOT_SCAN",
            )
            for slot in free_slots:
                move = self._preference_rescue_move_for_slot(
                    current,
                    activity,
                    slot,
                    route_context,
                )
                if not move:
                    candidate_stats["pruned"] += 1
                    attempts.append({
                        "target_free_slot": f"{format_clock(slot['start'])}-{format_clock(slot['end'])}",
                        "rejection_reason": "no_feasible_candidate_in_slot",
                    })
                    continue
                candidate_stats["generated"] += 1
                jlog(
                    "PREF_RESCUE",
                    (
                        f"title={title} candidate_start={format_clock(move['to_start'])} "
                        f"target_free_slot={format_clock(slot['start'])}-{format_clock(slot['end'])}"
                    ),
                    "CANDIDATE",
                )
                candidate = self._apply_backfill_move(current, move, route_context)
                candidate_validation = self._final_validate_route_aware_repair(
                    original=original,
                    repaired=candidate,
                    route_context=route_context,
                )
                blocking = self._blocking_route_conflicts(candidate_validation.get("route_conflicts", []))
                if blocking:
                    candidate_stats["pruned"] += 1
                    attempts.append({
                        "candidate_start": format_clock(move["to_start"]),
                        "target_free_slot": f"{format_clock(slot['start'])}-{format_clock(slot['end'])}",
                        "rejection_reason": "route_validation_failed",
                    })
                    jlog(
                        "PREF_RESCUE",
                        f"title={title} candidate_start={format_clock(move['to_start'])} reason=route_validation_failed",
                        "REJECT",
                    )
                    continue
                candidate_stats["validated"] += 1
                candidate_travel_total = self._route_sequence_total_minutes(candidate, route_context)
                route_delta = candidate_travel_total - current_travel_total
                accepted_tradeoff = move["score_delta"] > 0 and route_delta <= 10
                jlog(
                    "PREF_RESCUE",
                    (
                        f"title={title} pref_improvement_minutes={move['pref_improvement_minutes']} "
                        f"route_delta={route_delta} accepted={str(accepted_tradeoff).lower()}"
                    ),
                    "TRADEOFF",
                )
                if not accepted_tradeoff:
                    candidate_stats["pruned"] += 1
                    attempts.append({
                        "candidate_start": format_clock(move["to_start"]),
                        "target_free_slot": f"{format_clock(slot['start'])}-{format_clock(slot['end'])}",
                        "rejection_reason": "route_cost_too_high" if route_delta > 10 else "weighted_score_not_improved",
                    })
                    jlog(
                        "PREF_RESCUE",
                        (
                            f"title={title} candidate_start={format_clock(move['to_start'])} "
                            f"reason={'route_cost_too_high' if route_delta > 10 else 'weighted_score_not_improved'}"
                        ),
                        "REJECT",
                    )
                    continue

                jlog(
                    "PREF_RESCUE",
                    f"title={title} from={format_clock(move['from_start'])} to={format_clock(move['to_start'])}",
                    "APPLY",
                )
                applied.append(move)
                current = candidate
                current_validation = candidate_validation
                current_travel_total = candidate_travel_total
                accepted = True
                break
            attempts_by_key[activity_key] = attempts
            if not accepted and attempts:
                current.setdefault("preference_rescue_attempts", {})[activity_key] = attempts

        current["preference_rescue_attempts"] = attempts_by_key
        current_validation["preference_rescue_attempts"] = attempts_by_key
        jlog(
            "PERF",
            (
                f"generated={candidate_stats['generated']} pruned={candidate_stats['pruned']} "
                f"validated={candidate_stats['validated']}"
            ),
            "CANDIDATES",
        )
        if not applied:
            if attempts_by_key:
                return {"repaired": current, "validation": current_validation}
            return None
        current.setdefault("route_repair_actions", [])
        for move in applied:
            current["route_repair_actions"].append({
                "type": "move_activity",
                "title": move.get("title"),
                "activity_id": move.get("activity_id"),
                "from": format_clock(move["from_start"]),
                "from_end": format_clock(move["from_end"]),
                "to": format_clock(move["to_start"]),
                "to_end": format_clock(move["to_end"]),
                "reason": "Moved closer to its preferred time window after accurate travel repair.",
            })
        return {"repaired": current, "validation": current_validation}

    def _preference_rescue_blocker_reorder_moves(
        self,
        current: Dict[str, Any],
        activity: Dict[str, Any],
        route_context: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        start, end = self._activity_time_bounds(activity)
        if start is None or end is None:
            return []
        activity_key = self._route_repair_activity_key(activity)
        ordered = sorted(current.get("activities") or [], key=lambda item: self._activity_time_bounds(item)[0] or 0)
        index = next((idx for idx, item in enumerate(ordered) if self._route_repair_activity_key(item) == activity_key), -1)
        if index <= 0:
            return []
        activity_duration = int(activity.get("duration_minutes") or (end - start) or DEFAULT_DURATION)
        old_pref = preference_window_deviation(activity, start, end)
        moves: List[Dict[str, Any]] = []
        for blocker in reversed(ordered[:index]):
            blocker_start, blocker_end = self._activity_time_bounds(blocker)
            if blocker_start is None or blocker_end is None:
                continue
            if blocker_end > start:
                continue
            if not self._activity_can_auto_move_for_route_repair(blocker):
                continue
            route_minutes = self._route_minutes_between(blocker, activity, route_context)
            same_location = route_minutes <= 2 or clean_title(blocker.get("location") or blocker.get("location_label") or "") == clean_title(activity.get("location") or activity.get("location_label") or "")
            if not same_location:
                break
            blocker_duration = int(blocker.get("duration_minutes") or (blocker_end - blocker_start) or DEFAULT_DURATION)
            new_activity_start = blocker_start
            new_activity_end = new_activity_start + activity_duration
            new_blocker_start = new_activity_end
            new_blocker_end = new_blocker_start + blocker_duration
            if not self._preference_rescue_respects_constraints(activity, new_activity_start, new_activity_end):
                continue
            if not self._preference_rescue_respects_constraints(blocker, new_blocker_start, new_blocker_end):
                continue
            new_activity_pref = preference_window_deviation(activity, new_activity_start, new_activity_end)
            old_blocker_pref = preference_window_deviation(blocker, blocker_start, blocker_end)
            new_blocker_pref = preference_window_deviation(blocker, new_blocker_start, new_blocker_end)
            pref_improvement = (
                float(old_pref.get("penalty") or 0)
                + float(old_blocker_pref.get("penalty") or 0)
                - float(new_activity_pref.get("penalty") or 0)
                - float(new_blocker_pref.get("penalty") or 0)
            )
            if pref_improvement <= 0:
                continue
            moves.append({
                "type": "preference_reorder",
                "activity_id": activity.get("stable_activity_id") or activity.get("id"),
                "title": activity.get("title") or "Activity",
                "blocker_activity_id": blocker.get("stable_activity_id") or blocker.get("id"),
                "blocker_title": blocker.get("title") or "Activity",
                "from_start": start,
                "from_end": end,
                "to_start": new_activity_start,
                "to_end": new_activity_end,
                "blocker_from_start": blocker_start,
                "blocker_from_end": blocker_end,
                "blocker_to_start": new_blocker_start,
                "blocker_to_end": new_blocker_end,
                "score_delta": pref_improvement - max(0, abs(new_activity_start - start) / 60.0),
                "pref_improvement_minutes": int((old_pref.get("deviation") or 0) - (new_activity_pref.get("deviation") or 0)),
                "reason": "reorder_same_location_blocker",
            })
            break
        return moves

    def _apply_preference_reorder_move(
        self,
        current: Dict[str, Any],
        move: Dict[str, Any],
        route_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        candidate = deepcopy(current)
        target_id = str(move.get("activity_id") or "")
        blocker_id = str(move.get("blocker_activity_id") or "")
        updated_activities: List[Dict[str, Any]] = []
        for item in candidate.get("activities") or []:
            key = str(item.get("stable_activity_id") or item.get("id") or "")
            updated = deepcopy(item)
            if key == target_id:
                updated["scheduled_start"] = move["to_start"]
                updated["scheduled_end"] = move["to_end"]
                updated["startTime"] = format_clock(move["to_start"])
                updated["endTime"] = format_clock(move["to_end"])
                updated.setdefault("trace", []).append("Moved before a lower-priority same-location blocker to protect preferred time.")
            elif key == blocker_id:
                updated["scheduled_start"] = move["blocker_to_start"]
                updated["scheduled_end"] = move["blocker_to_end"]
                updated["startTime"] = format_clock(move["blocker_to_start"])
                updated["endTime"] = format_clock(move["blocker_to_end"])
                updated.setdefault("trace", []).append("Moved after a higher-priority preferred-window activity at the same location.")
            updated_activities.append(updated)
        candidate["activities"] = sorted(updated_activities, key=lambda item: self._activity_time_bounds(item)[0] or 0)
        preferences = candidate.get("preferences") or {}
        materialize_preferences = deepcopy(preferences)
        materialize_preferences["_route_context"] = route_context
        candidate["preferences"] = materialize_preferences
        day_start = parse_clock(materialize_preferences.get("day_start") or materialize_preferences.get("day_start_time") or "") or DEFAULT_DAY_START
        candidate["schedule_blocks"] = self._materialize_blocks(
            candidate["activities"],
            day_start,
            int(materialize_preferences.get("min_travel_buffer_minutes") or 0),
        )
        return candidate

    def _preference_rescue_slots_for_activity(
        self,
        blocks: List[Dict[str, Any]],
        activity: Dict[str, Any],
    ) -> List[Dict[str, int]]:
        duration = int(activity.get("duration_minutes") or DEFAULT_DURATION)
        info = (preference_window_deviation(activity, *self._activity_time_bounds(activity)).get("info") or {})
        ideal_start = int(info.get("ideal_start") or info.get("acceptable_start") or DEFAULT_DAY_START)
        slots = [
            slot for slot in self._post_repair_large_free_slots(blocks)
            if int(slot.get("duration") or 0) >= duration
        ]
        current_start, current_end = self._activity_time_bounds(activity)
        target_key = self._route_repair_activity_key(activity)
        ordered_blocks = sorted(blocks or [], key=lambda block: self._block_time_bounds(block)[0] or 0)
        for index, block in enumerate(ordered_blocks):
            block_type = clean_title(block.get("block_type") or block.get("type") or "")
            if block_type not in {"idle", "free", "free_time"} and clean_title(block.get("title") or "") != "free time":
                continue
            free_start, free_end = self._block_time_bounds(block)
            if free_start is None or free_end is None or current_start is None or free_end > current_start:
                continue
            extended_end = free_end
            blocked_by_activity = False
            for next_block in ordered_blocks[index + 1:]:
                next_start, next_end = self._block_time_bounds(next_block)
                if next_start is None or next_end is None:
                    continue
                if next_start < free_end:
                    continue
                if next_start >= current_start:
                    break
                next_type = clean_title(next_block.get("block_type") or next_block.get("type") or "")
                next_key = self._route_repair_activity_key(next_block)
                if next_type == "activity" and next_key != target_key:
                    blocked_by_activity = True
                    break
                extended_end = max(extended_end, next_end)
            if blocked_by_activity:
                continue
            if current_end is not None:
                extended_end = max(extended_end, current_end)
            elif extended_end < current_start:
                extended_end = current_start
            if extended_end - free_start >= duration:
                slots.append({
                    "start": free_start,
                    "end": extended_end,
                    "duration": extended_end - free_start,
                    "priority": 20,
                })
                jlog(
                    "PREF_RESCUE",
                    (
                        f"title={activity.get('title')} expanded_pre_activity_slot="
                        f"{format_clock(free_start)}-{format_clock(extended_end)}"
                    ),
                    "FREE_SLOT_SCAN",
                )
        deduped: Dict[Tuple[int, int], Dict[str, int]] = {}
        for slot in slots:
            key = (int(slot["start"]), int(slot["end"]))
            existing = deduped.get(key)
            if not existing or int(slot.get("priority") or 0) > int(existing.get("priority") or 0):
                deduped[key] = slot
        slots = list(deduped.values())
        return sorted(slots, key=lambda slot: (abs(int(slot["start"]) - ideal_start), slot["start"]))

    def _preference_rescue_move_for_slot(
        self,
        current: Dict[str, Any],
        activity: Dict[str, Any],
        slot: Dict[str, int],
        route_context: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        start, end = self._activity_time_bounds(activity)
        if start is None or end is None:
            return None
        duration = int(activity.get("duration_minutes") or (end - start) or DEFAULT_DURATION)
        if duration <= 0 or duration > int(slot.get("duration") or 0):
            return None
        info = (preference_window_deviation(activity, start, end).get("info") or {})
        ideal_start = int(info.get("ideal_start") or info.get("acceptable_start") or slot["start"])
        earliest = int(slot["start"])
        latest = int(slot["end"]) - duration

        other_activities = [
            item for item in current.get("activities") or []
            if self._route_repair_activity_key(item) != self._route_repair_activity_key(activity)
        ]
        physical_order = sorted(
            [item for item in other_activities if self._block_requires_travel_coordinate(item)],
            key=lambda item: self._activity_time_bounds(item)[0] or 0,
        )
        previous_physical = None
        next_physical = None
        for item in physical_order:
            item_start, item_end = self._activity_time_bounds(item)
            if item_end is not None and item_end <= slot["start"]:
                previous_physical = item
            elif item_start is not None and item_start >= slot["end"] and next_physical is None:
                next_physical = item
        if self._block_requires_travel_coordinate(activity):
            if previous_physical:
                previous_end = self._activity_time_bounds(previous_physical)[1]
                travel = self._route_minutes_between(previous_physical, activity, route_context)
                if previous_end is not None:
                    earliest = max(earliest, previous_end + DEFAULT_PREP_BUFFER + travel)
            else:
                start_route = (route_context.get("start_routes") or {}).get(self._route_context_activity_key(activity))
                if start_route:
                    day_start = parse_clock((current.get("preferences") or {}).get("day_start") or (current.get("preferences") or {}).get("day_start_time") or "") or DEFAULT_DAY_START
                    earliest = max(earliest, day_start + int(start_route.get("duration_minutes") or 0))
            if next_physical:
                next_start = self._activity_time_bounds(next_physical)[0]
                travel = self._route_minutes_between(activity, next_physical, route_context)
                if next_start is not None:
                    latest = min(latest, next_start - DEFAULT_PREP_BUFFER - travel - duration)

        if latest < earliest:
            return None
        target_start = min(max(ideal_start, earliest), latest)
        target_end = target_start + duration
        if target_start == start:
            return None
        if not self._preference_rescue_respects_constraints(activity, target_start, target_end):
            return None
        if self._backfill_overlaps_other_activity(current.get("activities") or [], activity, target_start, target_end):
            return None

        old_pref = preference_window_deviation(activity, start, end)
        new_pref = preference_window_deviation(activity, target_start, target_end)
        if new_pref.get("hard_violation"):
            return None
        old_penalty = float(old_pref.get("penalty") or 0)
        new_penalty = float(new_pref.get("penalty") or 0)
        pref_improvement = old_penalty - new_penalty
        if pref_improvement <= 0:
            return None
        score_delta = pref_improvement - max(0, abs(target_start - start) / 60.0)
        return {
            "activity_id": activity.get("stable_activity_id") or activity.get("id"),
            "title": activity.get("title") or "Activity",
            "from_start": start,
            "from_end": end,
            "to_start": target_start,
            "to_end": target_end,
            "slot_key": f"{slot['start']}-{slot['end']}",
            "score_delta": score_delta,
            "pref_improvement_minutes": int((old_pref.get("deviation") or 0) - (new_pref.get("deviation") or 0)),
        }

    def _preference_rescue_respects_constraints(self, item: Dict[str, Any], start: int, end: int) -> bool:
        earliest = item.get("earliest_start")
        latest = item.get("latest_end")
        if earliest is not None and start < int(earliest):
            return False
        if latest is not None and end > int(latest):
            return False
        return not bool(preference_window_deviation(item, start, end).get("hard_violation"))

    def _best_post_repair_backfill_move(
        self,
        *,
        current: Dict[str, Any],
        original: Dict[str, Any],
        validation: Dict[str, Any],
        route_context: Dict[str, Any],
        current_travel_total: int,
        moved_activity_ids: Optional[set[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        free_slots = self._post_repair_large_free_slots(validation.get("schedule_blocks", []))
        if not free_slots:
            jlog("MODULE_D", "reason=no_large_free_slots", "BACKFILL][SKIP")
            return None
        activities = [
            item for item in current.get("activities") or []
            if self._activity_can_auto_move_for_route_repair(item)
            and self._backfill_activity_is_late_or_misplaced(item)
        ]
        if not activities:
            jlog("MODULE_D", "reason=no_late_flexible_candidates", "BACKFILL][SKIP")
            return None

        best: Optional[Dict[str, Any]] = None
        for activity in activities:
            activity_id = str(activity.get("stable_activity_id") or activity.get("id") or "")
            if activity_id and activity_id in (moved_activity_ids or set()):
                jlog(
                    "MODULE_D",
                    f"candidate={activity.get('title')} reason=already_moved_this_pass",
                    "BACKFILL][SKIP",
                )
                continue
            for slot in free_slots:
                move = self._backfill_move_for_slot(current, activity, slot, route_context)
                if not move:
                    continue
                if move["to_start"] >= move["from_start"]:
                    jlog(
                        "MODULE_D",
                        (
                            f"candidate={move['title']} from={format_clock(move['from_start'])} "
                            f"to={format_clock(move['to_start'])} reason=not_earlier_backfill"
                        ),
                        "BACKFILL][REJECT",
                    )
                    continue
                candidate = self._apply_backfill_move(current, move, route_context)
                candidate_travel_total = self._route_sequence_total_minutes(candidate, route_context)
                if candidate_travel_total > current_travel_total:
                    jlog(
                        "MODULE_D",
                        (
                            f"candidate={move['title']} from={format_clock(move['from_start'])} "
                            f"to={format_clock(move['to_start'])} score_delta={move['score_delta']:.2f} "
                            f"reason=travel_worse"
                        ),
                        "BACKFILL][SKIP",
                    )
                    continue
                jlog(
                    "MODULE_D",
                    (
                        f"candidate={move['title']} from={format_clock(move['from_start'])} "
                        f"to={format_clock(move['to_start'])} score_delta={move['score_delta']:.2f}"
                    ),
                    "BACKFILL",
                )
                if move["score_delta"] <= 0:
                    continue
                if best is None or move["score_delta"] > best["score_delta"]:
                    best = move
        return best

    def _post_repair_large_free_slots(self, blocks: List[Dict[str, Any]]) -> List[Dict[str, int]]:
        slots: List[Dict[str, int]] = []
        for block in blocks or []:
            if block.get("display_only") or block.get("is_route_conflict"):
                continue
            block_type = clean_title(block.get("block_type") or block.get("type") or "")
            if block_type not in {"idle", "free", "free_time"} and clean_title(block.get("title") or "") != "free time":
                continue
            start, end = self._block_time_bounds(block)
            if start is None or end is None:
                continue
            duration = end - start
            if duration < 45:
                continue
            priority = 30 if start < 10 * 60 else (15 if start < 14 * 60 else 0)
            slots.append({"start": start, "end": end, "duration": duration, "priority": priority})
        return sorted(slots, key=lambda slot: (0 if slot["start"] < 10 * 60 else 1, slot["start"]))

    def _backfill_activity_is_late_or_misplaced(self, item: Dict[str, Any]) -> bool:
        start, end = self._activity_time_bounds(item)
        if start is None or end is None:
            return False
        title = clean_title(item.get("title") or "")
        category = clean_title(item.get("location_category") or "")
        errand_hint = any(token in title for token in ("laundry", "grocery", "supermarket", "errand", "shopping", "gift"))
        errand_hint = errand_hint or category in {"supermarket", "laundry", "retail", "shop", "store"}
        preferred_start = item.get("preferred_window_start")
        preferred_end = item.get("preferred_window_end")
        outside_preferred = (
            preferred_start is not None and start < int(preferred_start)
        ) or (
            preferred_end is not None and end > int(preferred_end)
        )
        return bool(errand_hint or start >= 19 * 60 or outside_preferred)

    def _backfill_move_for_slot(
        self,
        current: Dict[str, Any],
        activity: Dict[str, Any],
        slot: Dict[str, int],
        route_context: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        start, end = self._activity_time_bounds(activity)
        if start is None or end is None:
            return None
        duration = int(activity.get("duration_minutes") or (end - start) or DEFAULT_DURATION)
        if duration <= 0 or duration > slot["duration"]:
            return None
        rejected = set(activity.get("_backfill_rejected_slots") or [])
        rejected_key = f"{slot['start']}-{slot['end']}"
        if rejected_key in rejected:
            return None

        other_activities = [
            item for item in current.get("activities") or []
            if self._route_repair_activity_key(item) != self._route_repair_activity_key(activity)
        ]
        physical_order = sorted(
            [
                item for item in other_activities
                if self._block_requires_travel_coordinate(item)
            ],
            key=lambda item: self._activity_time_bounds(item)[0] or 0,
        )
        previous_physical = None
        next_physical = None
        for item in physical_order:
            item_start, item_end = self._activity_time_bounds(item)
            if item_end is not None and item_end <= slot["start"]:
                previous_physical = item
            elif item_start is not None and item_start >= slot["end"] and next_physical is None:
                next_physical = item

        earliest = slot["start"]
        latest = slot["end"] - duration
        if self._block_requires_travel_coordinate(activity):
            if previous_physical:
                previous_end = self._activity_time_bounds(previous_physical)[1]
                travel = self._route_minutes_between(previous_physical, activity, route_context)
                if previous_end is not None:
                    earliest = max(earliest, previous_end + DEFAULT_PREP_BUFFER + travel)
            else:
                start_route = (route_context.get("start_routes") or {}).get(self._route_context_activity_key(activity))
                if start_route:
                    day_start = parse_clock((current.get("preferences") or {}).get("day_start") or (current.get("preferences") or {}).get("day_start_time") or "") or DEFAULT_DAY_START
                    earliest = max(earliest, day_start + int(start_route.get("duration_minutes") or 0))
            if next_physical:
                next_start = self._activity_time_bounds(next_physical)[0]
                travel = self._route_minutes_between(activity, next_physical, route_context)
                if next_start is not None:
                    latest = min(latest, next_start - DEFAULT_PREP_BUFFER - travel - duration)

        target_start = earliest
        target_end = target_start + duration
        if target_start >= start:
            jlog(
                "MODULE_D",
                (
                    f"candidate={activity.get('title')} from={format_clock(start)} "
                    f"to={format_clock(target_start)} reason=not_earlier_backfill"
                ),
                "BACKFILL][REJECT",
            )
            return None
        if target_start > latest or target_end > slot["end"]:
            return None
        if not self._backfill_respects_time_constraints(activity, target_start, target_end):
            return None
        if self._backfill_overlaps_other_activity(current.get("activities") or [], activity, target_start, target_end):
            return None

        score_delta = self._backfill_score_delta(activity, slot, start, end, target_start, target_end)
        return {
            "activity_id": activity.get("stable_activity_id") or activity.get("id"),
            "title": activity.get("title") or "Activity",
            "from_start": start,
            "from_end": end,
            "to_start": target_start,
            "to_end": target_end,
            "slot_key": rejected_key,
            "score_delta": score_delta,
        }

    def _backfill_respects_time_constraints(self, item: Dict[str, Any], start: int, end: int) -> bool:
        earliest = item.get("earliest_start")
        latest = item.get("latest_end")
        if earliest is not None and start < int(earliest):
            return False
        if latest is not None and end > int(latest):
            return False
        preference = preference_window_deviation(item, start, end)
        if preference.get("hard_violation"):
            info = preference.get("info") or {}
            jlog(
                "PREF_WINDOW",
                (
                    f"title={item.get('title')} reason=hard_window_violation "
                    f"window={format_clock(info.get('acceptable_start'))}-{format_clock(info.get('acceptable_end'))}"
                ),
                "REJECT",
            )
            return False
        return True

    def _backfill_overlaps_other_activity(
        self,
        activities: List[Dict[str, Any]],
        target: Dict[str, Any],
        start: int,
        end: int,
    ) -> bool:
        target_key = self._route_repair_activity_key(target)
        for item in activities or []:
            if self._route_repair_activity_key(item) == target_key:
                continue
            other_start, other_end = self._activity_time_bounds(item)
            if other_start is None or other_end is None:
                continue
            if start < other_end and end > other_start:
                return True
        return False

    def _backfill_score_delta(
        self,
        item: Dict[str, Any],
        slot: Dict[str, int],
        old_start: int,
        old_end: int,
        new_start: int,
        new_end: int,
    ) -> float:
        title = clean_title(item.get("title") or "")
        category = clean_title(item.get("location_category") or "")
        score = float(max(0, old_start - new_start) / 15.0)
        if old_start >= 19 * 60 and new_start < 18 * 60:
            score += 35
        if any(token in title for token in ("laundry", "grocery", "supermarket", "errand", "shopping", "gift")):
            score += 25
        if category in {"supermarket", "laundry", "retail", "shop", "store"}:
            score += 15
        score += float(slot.get("priority") or 0)
        preferred_start = item.get("preferred_window_start")
        preferred_end = item.get("preferred_window_end")
        if preferred_start is not None and preferred_end is not None:
            old_outside = old_start < int(preferred_start) or old_end > int(preferred_end)
            new_inside = new_start >= int(preferred_start) and new_end <= int(preferred_end)
            if old_outside and new_inside:
                score += 30
        old_pref = preference_window_deviation(item, old_start, old_end)
        new_pref = preference_window_deviation(item, new_start, new_end)
        score += max(0.0, float(old_pref.get("penalty") or 0) - float(new_pref.get("penalty") or 0)) / 8.0
        return score

    def _apply_backfill_move(
        self,
        current: Dict[str, Any],
        move: Dict[str, Any],
        route_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        candidate = deepcopy(current)
        moved_key = str(move.get("activity_id") or "")
        updated_activities: List[Dict[str, Any]] = []
        for item in candidate.get("activities") or []:
            key = str(item.get("stable_activity_id") or item.get("id") or "")
            if key == moved_key:
                updated = deepcopy(item)
                updated["scheduled_start"] = move["to_start"]
                updated["scheduled_end"] = move["to_end"]
                updated["startTime"] = format_clock(move["to_start"])
                updated["endTime"] = format_clock(move["to_end"])
                updated.setdefault("trace", [])
                updated["trace"].append("Backfilled into an earlier route-safe free slot after accurate travel repair.")
                updated_activities.append(updated)
            else:
                updated_activities.append(item)
        candidate["activities"] = sorted(updated_activities, key=lambda item: self._activity_time_bounds(item)[0] or 0)
        preferences = candidate.get("preferences") or {}
        materialize_preferences = deepcopy(preferences)
        materialize_preferences["_route_context"] = route_context
        candidate["preferences"] = materialize_preferences
        day_start = parse_clock(materialize_preferences.get("day_start") or materialize_preferences.get("day_start_time") or "") or DEFAULT_DAY_START
        candidate["schedule_blocks"] = self._materialize_blocks(
            candidate["activities"],
            day_start,
            int(materialize_preferences.get("min_travel_buffer_minutes") or 0),
        )
        return candidate

    def _mark_backfill_rejected(self, current: Dict[str, Any], move: Dict[str, Any]) -> None:
        activity_id = str(move.get("activity_id") or "")
        for item in current.get("activities") or []:
            if str(item.get("stable_activity_id") or item.get("id") or "") != activity_id:
                continue
            rejected = set(item.get("_backfill_rejected_slots") or [])
            rejected.add(str(move.get("slot_key") or ""))
            item["_backfill_rejected_slots"] = sorted(value for value in rejected if value)

    def _activity_time_bounds(self, item: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
        start = item.get("scheduled_start")
        end = item.get("scheduled_end")
        if start is None:
            start = parse_clock(item.get("start") or item.get("startTime") or "")
        if end is None:
            end = parse_clock(item.get("end") or item.get("endTime") or "")
        if start is None:
            return None, None
        if end is None:
            end = int(start) + int(item.get("duration_minutes") or DEFAULT_DURATION)
        return int(start), int(end)

    def _route_minutes_between(
        self,
        left: Dict[str, Any],
        right: Dict[str, Any],
        route_context: Dict[str, Any],
    ) -> int:
        if not left or not right:
            return 0
        entry = (route_context.get("pairs") or {}).get(self._route_context_pair_key(left, right))
        if entry:
            return int(entry.get("duration_minutes") or 0)
        if clean_title(left.get("location") or "") == clean_title(right.get("location") or ""):
            return 0
        return int(estimate_travel_minutes(left.get("location"), right.get("location")) or 0)

    def _route_sequence_total_minutes(self, envelope: Dict[str, Any], route_context: Dict[str, Any]) -> int:
        activities = sorted(
            [
                item for item in envelope.get("activities") or []
                if self._block_requires_travel_coordinate(item)
            ],
            key=lambda item: self._activity_time_bounds(item)[0] or 0,
        )
        total = 0
        if activities:
            first_entry = (route_context.get("start_routes") or {}).get(self._route_context_activity_key(activities[0]))
            if first_entry:
                total += int(first_entry.get("duration_minutes") or 0)
        for left, right in zip(activities, activities[1:]):
            total += self._route_minutes_between(left, right, route_context)
        return total

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
        if protection in {"fixed", "protected_social", "flexible", "optional", "derived"}:
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

        title = clean_title(item.get("title") or "")
        if "critical" in title or "fixed" in title:
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

    def _repair_suggestion_requires_fixed_move_approval(self, suggestion: Dict[str, Any]) -> bool:
        protection = clean_title(suggestion.get("repair_protection") or "")
        if protection in {"fixed", "protected_social"}:
            return True
        if clean_title(suggestion.get("impact_type") or "") == "fixed_target_move":
            return True
        return any(
            clean_title(cascade.get("impact_type") or "") in {"fixed_target_move", "protected_cascade_move"}
            for cascade in suggestion.get("cascade_suggestions") or []
            if isinstance(cascade, dict)
        )

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
        seen_ids: set[str] = set()
        activity_sources: Dict[str, Dict[str, Any]] = {}
        title_sources: Dict[str, Dict[str, Any]] = {}
        for source in envelope.get("activities") or []:
            if not isinstance(source, dict) or not self._is_activity_entry(source):
                continue
            canonical = self._canonicalize_activity(source)
            key = str(canonical.get("stable_activity_id") or canonical.get("id") or "")
            if key:
                activity_sources[key] = canonical
            title = clean_title(canonical.get("title") or "")
            if title:
                title_sources.setdefault(title, canonical)

        block_sources: List[Dict[str, Any]] = []
        blocks_by_key: Dict[str, Dict[str, Any]] = {}
        blocks_by_title: Dict[str, Dict[str, Any]] = {}
        for source in envelope.get("schedule_blocks") or []:
            if not isinstance(source, dict):
                continue
            if (source.get("block_type") or source.get("type") or "activity") not in {"activity", None}:
                continue
            if source.get("block_type") and source.get("block_type") != "activity":
                continue
            block_sources.append(source)
            key = str(source.get("stable_activity_id") or source.get("id") or "")
            if key:
                blocks_by_key[key] = source
            title = clean_title(source.get("title") or "")
            if title:
                blocks_by_title.setdefault(title, source)

        merged_sources: List[Dict[str, Any]] = []
        used_block_ids: set[str] = set()
        for canonical_source in activity_sources.values():
            activity_id = str(canonical_source.get("stable_activity_id") or canonical_source.get("id") or "")
            title_key = clean_title(canonical_source.get("title") or "")
            source = blocks_by_key.get(activity_id) or blocks_by_title.get(title_key) or canonical_source
            if source is not canonical_source:
                used_block_ids.add(str(source.get("stable_activity_id") or source.get("id") or ""))
            record = deepcopy(canonical_source)
            if source is not canonical_source:
                for field in (
                    "start",
                    "end",
                    "startTime",
                    "endTime",
                    "scheduled_start",
                    "scheduled_end",
                    "location",
                    "location_label",
                    "location_category",
                    "location_status",
                    "location_source",
                    "resolved_location",
                    "travel_required",
                ):
                    value = source.get(field)
                    if field in source and value is not None and value != "":
                        record[field] = deepcopy(value) if isinstance(value, (dict, list)) else value
            copy_preference_metadata(canonical_source, record, overwrite=True)
            merged_sources.append(record)
        for source in block_sources:
            activity_id = str(source.get("stable_activity_id") or source.get("id") or "")
            title_key = clean_title(source.get("title") or "")
            if (activity_id and activity_id in activity_sources) or (activity_id and activity_id in used_block_ids) or title_key in title_sources:
                continue
            merged_sources.append(deepcopy(source))

        for source in merged_sources:
            activity_id = str(source.get("stable_activity_id") or source.get("id") or "")
            if activity_id and activity_id in seen_ids:
                continue
            record = deepcopy(source)
            start = parse_clock(record.get("start") or record.get("startTime") or "")
            if start is None:
                start = record.get("scheduled_start")
            end = parse_clock(record.get("end") or record.get("endTime") or "")
            if end is None:
                end = record.get("scheduled_end")
            if start is None or end is None:
                continue
            record["_repair_start"] = int(start)
            record["_repair_end"] = int(end) if int(end) > int(start) else int(end) + 24 * 60
            records.append(record)
            if activity_id:
                seen_ids.add(activity_id)

        records.sort(key=lambda r: r["_repair_start"])
        return records

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

    def _protected_repair_suggestions_from_conflicts(
        self,
        envelope: Dict[str, Any],
        route_conflicts: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        records = self._activity_records_for_repair(envelope)
        suggestions: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        for index, conflict in enumerate(route_conflicts or [], start=1):
            if conflict.get("reason_code") in {"start_route_blocker", "fixed_to_fixed_infeasible"}:
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
            source = self._find_repair_record(
                records,
                activity_id=conflict.get("source_activity_id") or conflict.get("from_activity_id"),
                title=conflict.get("from_activity") or conflict.get("from"),
            )
            prep = (
                DEFAULT_PREP_BUFFER
                if source
                and self._block_requires_travel_coordinate(source)
                and self._block_requires_travel_coordinate(target)
                else 0
            )
            duration = max(1, target["_repair_end"] - target["_repair_start"])
            suggested_start = max(target["_repair_start"], from_end + required + prep)
            suggested_end = suggested_start + duration
            title = target.get("title") or target_title or "activity"
            if not self._repair_suggestion_would_change(target, suggested_start, suggested_end):
                jlog(
                    "PENDING_REPAIR",
                    f"title={title} from={format_clock(target['_repair_start'])} to={format_clock(suggested_start)}",
                    "SKIP_NOOP",
                )
                continue
            protection = self._activity_repair_protection(target)
            if protection in {"fixed", "protected_social"}:
                jlog("PENDING_REPAIR", f"title={title} protection={protection}", "SKIP_FIXED_PROTECTED")
                continue

            # Cascade Simulation
            cascade_suggestions = []
            cascade_reason_parts = []
            current_shifted_end = suggested_end
            current_location = target.get("location")
            target_index = next((i for i, r in enumerate(records) if r == target), -1)

            if target_index >= 0:
                for i in range(target_index + 1, len(records)):
                    next_record = records[i]
                    if not self._activity_requires_repair_confirmation(next_record) or not next_record.get("location"):
                        continue

                    travel_time = estimate_travel_minutes(current_location, next_record.get("location"))
                    prep = int(next_record.get("prep_buffer", DEFAULT_PREP_BUFFER) or 0)
                    required_next_start = current_shifted_end + prep + travel_time

                    if next_record["_repair_start"] < required_next_start:
                        next_duration = max(1, next_record["_repair_end"] - next_record["_repair_start"])
                        next_new_start = required_next_start
                        next_new_end = next_new_start + next_duration

                        cascade_suggestions.append({
                            "activity_id": str(next_record.get("stable_activity_id") or next_record.get("id") or ""),
                            "title": next_record.get("title"),
                            "from_start": next_record["_repair_start"],
                            "from_end": next_record["_repair_end"],
                            "to_start": next_new_start,
                            "to_end": next_new_end,
                            "impact_type": "protected_cascade_move",
                        })
                        cascade_reason_parts.append(f"{next_record.get('title')} to {format_clock(next_new_start)}–{format_clock(next_new_end)}")

                        current_shifted_end = next_new_end
                        current_location = next_record.get("location")
                    else:
                        break # Cascade chain broken

            relative_cascades = self._relative_dependent_cascade_suggestions(
                envelope,
                target,
                suggested_start,
                suggested_end,
            )
            if relative_cascades:
                cascade_suggestions.extend(relative_cascades)
                for relative in relative_cascades:
                    cascade_reason_parts.append(
                        f"{relative.get('title')} after {title} at "
                        f"{relative.get('to') or format_clock(int(relative.get('to_start') or 0))}"
                    )

            if cascade_suggestions:
                cascade_text = ", ".join(cascade_reason_parts)
                reason = f"Move {title} from {format_clock(target['_repair_start'])}–{format_clock(target['_repair_end'])} to {format_clock(suggested_start)}–{format_clock(suggested_end)} because travel needs {required} minutes. This will also shift: {cascade_text}."
            else:
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
                "cascade_suggestions": cascade_suggestions,
                "impact": "minimal_shift",
                "impact_type": "fixed_target_move",
                "requires_user_confirmation": True,
                "requires_explicit_fixed_move_approval": protection in {"fixed", "protected_social"},
                "advisory_only": protection in {"fixed", "protected_social"},
                "would_change": True,
                "schedule_version": int(envelope.get("version") or 1),
                "route_conflict_index": index - 1,
                "repair_protection": protection,
            }
            suggestions.append(suggestion)
            if activity_id:
                seen_ids.add(activity_id)
            jlog("PENDING_REPAIR", f"id={suggestion['id']} action=created title={title}", None)
        return suggestions

    def _relative_dependent_cascade_suggestions(
        self,
        envelope: Dict[str, Any],
        target: Dict[str, Any],
        target_new_start: int,
        target_new_end: int,
    ) -> List[Dict[str, Any]]:
        active = [
            item for item in self._load_canonical_activities(envelope)
            if item.get("status") == "active"
        ]
        target_id = str(target.get("stable_activity_id") or target.get("id") or "")
        target_title = clean_title(target.get("title") or "")
        dependents: List[Dict[str, Any]] = []
        known_ends: Dict[str, int] = {}
        if target_id:
            known_ends[f"id:{target_id}"] = target_new_end
        if target_title:
            known_ends[f"title:{target_title}"] = target_new_end

        progress = True
        while progress:
            progress = False
            for item in active:
                item_id = str(item.get("stable_activity_id") or item.get("id") or "")
                item_title = clean_title(item.get("title") or "")
                if any(
                    (item_id and existing.get("activity_id") == item_id)
                    or (item_title and clean_title(existing.get("title") or "") == item_title)
                    for existing in dependents
                ):
                    continue
                relation = item.get("anchor_relation") or {}
                if not relation:
                    continue
                relation_kind = clean_title(relation.get("kind") or "after")
                if relation_kind != "after":
                    continue
                anchor_id = str(relation.get("target_activity_id") or relation.get("target_id") or "")
                anchor_title = clean_title(relation.get("target_title") or "")
                anchor_key = f"id:{anchor_id}" if anchor_id else (f"title:{anchor_title}" if anchor_title else "")
                if not anchor_key or anchor_key not in known_ends:
                    continue
                old_start = item.get("scheduled_start")
                if old_start is None:
                    old_start = parse_clock(item.get("startTime") or "")
                old_end = item.get("scheduled_end")
                if old_end is None:
                    old_end = parse_clock(item.get("endTime") or "")
                duration = int(item.get("duration_minutes") or ((old_end or 0) - (old_start or 0)) or DEFAULT_DURATION)
                transition = 0
                if self._block_requires_travel_coordinate(target) and self._block_requires_travel_coordinate(item):
                    transition += DEFAULT_PREP_BUFFER
                    if clean_title(target.get("location") or "") != clean_title(item.get("location") or ""):
                        transition += estimate_travel_minutes(target.get("location"), item.get("location"))
                new_start = int(known_ends[anchor_key]) + transition
                new_end = new_start + duration
                cascade = {
                    "activity_id": item.get("stable_activity_id") or item.get("id"),
                    "title": item.get("title"),
                    "from_start": old_start,
                    "from_end": old_end,
                    "from": format_clock(old_start) if old_start is not None else None,
                    "from_end_label": format_clock(old_end) if old_end is not None else None,
                    "to_start": new_start,
                    "to_end": new_end,
                    "to": format_clock(new_start),
                    "to_end_label": format_clock(new_end),
                    "impact_type": "anchor_dependent_recalculated",
                    "depends_on_activity_id": target_id or None,
                    "depends_on_title": target.get("title"),
                    "reason": f"Recalculate {item.get('title')} after moved anchor {target.get('title')}.",
                }
                dependents.append(cascade)
                if item_id:
                    known_ends[f"id:{item_id}"] = new_end
                if item_title:
                    known_ends[f"title:{item_title}"] = new_end
                progress = True
        return dependents

    def _repair_suggestion_would_change(
        self,
        target: Dict[str, Any],
        suggested_start: Optional[int],
        suggested_end: Optional[int],
    ) -> bool:
        if suggested_start is None or suggested_end is None:
            return False
        return not (
            int(target.get("_repair_start") or 0) == int(suggested_start)
            and int(target.get("_repair_end") or 0) == int(suggested_end)
        )

    def _pending_suggestion_would_change(self, suggestion: Dict[str, Any]) -> bool:
        if suggestion.get("would_change") is False:
            return False
        old_start = parse_clock(suggestion.get("from") or "")
        old_end = parse_clock(suggestion.get("from_end") or "")
        new_start = parse_clock(suggestion.get("to") or "")
        new_end = parse_clock(suggestion.get("to_end") or "")
        if old_start is None or new_start is None:
            return bool(suggestion.get("would_change"))
        if old_end is None:
            old_end = int(suggestion.get("from_end_minutes") or -1)
        if new_end is None:
            new_end = int(suggestion.get("to_end_minutes") or -2)
        return not (old_start == new_start and old_end == new_end)

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
        is_relative_unfit = bool(item.get("anchor_relation")) and (
            item.get("status") == "unscheduled" or item.get("is_conflict")
        )
        if not is_relative_unfit and not self._activity_can_move_for_route_repair(item):
            return float("inf")
        protection = self._activity_repair_protection(item)
        if protection == "fixed":
            return float("inf")
        priority = clean_title(item.get("priority") or "medium")
        priority_score = {"low": 10, "medium": 30, "high": 60}.get(priority, 30)
        protection_score = {"optional": 0, "derived": 40, "flexible": 50, "protected_social": 10000}.get(protection, 50)
        mandatory_score = 100 if item.get("is_mandatory", item.get("isMandatory", True)) else 0
        explicit_score = 20 if item.get("source") in {"initial_request", "user_operation"} else 0
        base_score = float(self._calculate_activity_base_score(item) if hasattr(self, "_calculate_activity_base_score") else 0)
        return protection_score + mandatory_score + priority_score + explicit_score + (base_score / 100.0)

    def _is_fixed_route_conflict(self, conflict: Dict[str, Any]) -> bool:
        return str(conflict.get("reason_code") or "") == "fixed_to_fixed_infeasible"

    def _fixed_route_conflicts(self, conflicts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [conflict for conflict in conflicts or [] if self._is_fixed_route_conflict(conflict)]

    def _blocking_route_conflicts(self, conflicts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [conflict for conflict in conflicts or [] if not self._is_fixed_route_conflict(conflict)]

    def _has_non_fixed_repair_scope(
        self,
        active: List[Dict[str, Any]],
        unfit: List[Dict[str, Any]],
        route_repair_actions: List[Dict[str, Any]],
    ) -> bool:
        if unfit or route_repair_actions:
            return True
        for item in active or []:
            if item.get("anchor_relation") and not item.get("is_user_fixed"):
                return True
            if self._activity_can_move_for_route_repair(item):
                return True
        return False

    def _has_renderable_repaired_chronology(self, validation: Optional[Dict[str, Any]]) -> bool:
        if not validation:
            return False
        blocks = validation.get("schedule_blocks") or []
        if not blocks:
            return False
        activity_count = 0
        for block in blocks:
            if not isinstance(block, dict) or block.get("display_only"):
                continue
            start, end = self._block_time_bounds(block)
            if start is None or end is None or end <= start:
                return False
            if self._is_activity_schedule_block(block):
                activity_count += 1
        return activity_count > 0

    def _with_fixed_route_conflict_warning_blocks(
        self,
        blocks: List[Dict[str, Any]],
        conflicts: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        fixed_conflicts = self._fixed_route_conflicts(conflicts)
        if not fixed_conflicts:
            return blocks
        next_blocks = deepcopy(blocks or [])
        existing_ids = {str(block.get("id") or "") for block in next_blocks if block.get("id")}
        for index, conflict in enumerate(fixed_conflicts, start=1):
            required = int(conflict.get("required_travel_minutes") or conflict.get("required_route_minutes") or 0)
            available = int(conflict.get("available_gap_minutes") or conflict.get("available_minutes") or 0)
            start_min = parse_clock(conflict.get("from_end") or "") or parse_clock(conflict.get("to_start") or "")
            to_start = parse_clock(conflict.get("to_start") or "")
            if start_min is None:
                continue
            visual_end = to_start if to_start is not None and to_start > start_min else start_min + max(5, min(required or 5, 15))
            from_title = conflict.get("from_activity") or conflict.get("from") or "previous fixed event"
            to_title = conflict.get("to_activity") or conflict.get("to") or "next fixed event"
            block_id = f"fixed_route_conflict_{index}_{clean_title(from_title)}_{clean_title(to_title)}"
            if block_id in existing_ids:
                continue
            existing_ids.add(block_id)
            next_blocks.append({
                "id": block_id,
                "stable_activity_id": block_id,
                "block_type": "route_conflict",
                "type": "route_conflict",
                "category": "route_conflict",
                "title": f"Route conflict: {from_title} -> {to_title}",
                "display_label": (
                    f"Route conflict: {from_title} -> {to_title} needs {required} min, "
                    f"but only {available} min available."
                ),
                "start": format_clock(start_min),
                "startTime": format_clock(start_min),
                "end": format_clock(visual_end),
                "endTime": format_clock(visual_end),
                "duration_minutes": max(0, visual_end - start_min),
                "route_duration_minutes": required,
                "available_gap_minutes": available,
                "is_route_conflict": True,
                "isConflict": False,
                "display_only": True,
                "travel_validation_status": "fixed_route_conflict",
                "reason_code": "fixed_to_fixed_infeasible",
                "from_activity": from_title,
                "to_activity": to_title,
                "from_location": conflict.get("from_location"),
                "to_location": conflict.get("to_location"),
            })
        return sorted(
            next_blocks,
            key=lambda block: parse_clock(block.get("start") or block.get("startTime") or "") or 0,
        )

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
            item_id = str(item.get("stable_activity_id") or item.get("id") or "")
            item_title = clean_title(item.get("title") or "")
            related_conflict = next(
                (
                    conflict for conflict in remaining_conflicts or []
                    if (
                        (item_id and str(conflict.get("blocker_activity_id") or "") == item_id)
                        or (item_id and str(conflict.get("destination_activity_id") or "") == item_id)
                        or (item_title and clean_title(conflict.get("blocker_activity_title") or "") == item_title)
                        or (item_title and clean_title(conflict.get("to_activity") or conflict.get("to") or "") == item_title)
                    )
                ),
                None,
            )
            reason_code = (
                item.get("unscheduled_reason")
                or item.get("reason_code")
                or (related_conflict or {}).get("reason_code")
                or "not_enough_time_after_travel"
            )
            reason = (
                item.get("unscheduled_reason_detail")
                or item.get("reason")
                or (related_conflict or {}).get("reason")
                or "Not enough time after accurate travel validation"
            )
            blocker_title = (
                (related_conflict or {}).get("blocker_activity_title")
                or (related_conflict or {}).get("from_activity")
                or (related_conflict or {}).get("from")
            )
            blocking_constraint = {
                "reason_code": reason_code,
                "from_activity": (related_conflict or {}).get("from_activity") or (related_conflict or {}).get("from"),
                "to_activity": (related_conflict or {}).get("to_activity") or (related_conflict or {}).get("to"),
                "leave_by": (related_conflict or {}).get("leave_by"),
                "required_travel_minutes": (related_conflict or {}).get("required_travel_minutes") or (related_conflict or {}).get("required_route_minutes"),
                "available_gap_minutes": (related_conflict or {}).get("available_gap_minutes") or (related_conflict or {}).get("available_minutes"),
            }
            suggested_resolution = []
            if blocker_title:
                suggested_resolution.append(f"Move {blocker_title} earlier or later to create a route-safe gap.")
            if item.get("duration_minutes"):
                suggested_resolution.append(f"Shorten {item.get('title') or 'this activity'} below {item.get('duration_minutes')} minutes.")
            suggested_resolution.append(f"Change {item.get('title') or 'this activity'} to flexible/optional or schedule it another day.")
            metadata = {
                "activity_id": item.get("stable_activity_id") or item.get("id"),
                "title": item.get("title"),
                "duration_minutes": item.get("duration_minutes"),
                "reason": reason,
                "reason_code": reason_code,
                "blocking_constraint": blocking_constraint,
                "suggested_resolution": list(dict.fromkeys(suggested_resolution)),
                "priority": item.get("priority", "medium"),
                "timing_mode": item.get("timing_mode") or item.get("original_timing_mode") or TimingMode.UNSPECIFIED,
                "score": round(float(score), 2),
            }
            unfit.append(metadata)
            jlog("TRAVEL_REPAIR", f"title={metadata['title']} score={metadata['score']} reason={reason}", "UNFIT")
        return unfit

    def _route_repair_activity_key(self, item: Dict[str, Any]) -> str:
        key = (
            item.get("stable_activity_id")
            or item.get("activity_id")
            or item.get("original_operation_id")
            or item.get("id")
        )
        if key:
            return str(key).strip()
        title = clean_title(item.get("title") or "")
        duration = item.get("duration_minutes") or item.get("duration")
        index = item.get("original_request_index") or item.get("operation_index") or item.get("sequence_index")
        if title:
            return f"title:{title}|duration:{duration or ''}|index:{index if index is not None else ''}"
        return ""

    def _dedupe_route_repair_items(
        self,
        items: List[Dict[str, Any]],
        *,
        item_type: str,
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        deduped: List[Dict[str, Any]] = []
        index_by_key: Dict[str, int] = {}
        duplicate_keys: List[str] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            key = self._route_repair_activity_key(item)
            if not key:
                deduped.append(deepcopy(item))
                continue
            if key in index_by_key:
                duplicate_keys.append(key)
                existing = deduped[index_by_key[key]]
                deduped[index_by_key[key]] = self._merged_route_repair_item(existing, item)
                jlog(
                    "UNFIT" if item_type == "unfit" else "ACCOUNTING",
                    f"title={item.get('title') or existing.get('title')} reason=duplicate_existing_{item_type}",
                    "MERGE",
                )
                continue
            index_by_key[key] = len(deduped)
            deduped.append(deepcopy(item))
        return deduped, duplicate_keys

    def _merge_route_repair_item(
        self,
        items: List[Dict[str, Any]],
        incoming: Dict[str, Any],
        key: str,
    ) -> List[Dict[str, Any]]:
        merged: List[Dict[str, Any]] = []
        replaced = False
        for item in items or []:
            if self._route_repair_activity_key(item) == key:
                merged.append(self._merged_route_repair_item(item, incoming))
                replaced = True
            else:
                merged.append(item)
        if not replaced:
            merged.append(deepcopy(incoming))
        return merged

    def _merged_route_repair_item(self, existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
        merged = deepcopy(existing)
        for key, value in (incoming or {}).items():
            if value is None or value == "" or value == []:
                continue
            if key in {"reason", "reason_code", "blocking_constraint", "suggested_resolution", "duration_minutes", "title"}:
                merged[key] = deepcopy(value)
            else:
                merged.setdefault(key, deepcopy(value))
        return merged

    def _route_repair_item_is_optional(self, item: Dict[str, Any]) -> bool:
        if item.get("optional_reason"):
            return True
        if item.get("is_mandatory") is False or item.get("isMandatory") is False:
            return True
        if clean_title(item.get("activity_type") or "") == "optional":
            return True
        return self._activity_repair_protection(item) == "optional"

    def _optional_skipped_from_unscheduled(
        self,
        unscheduled: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        skipped: List[Dict[str, Any]] = []
        for item in unscheduled or []:
            if not self._route_repair_item_is_optional(item):
                continue
            reason_code = item.get("unscheduled_reason") or item.get("reason_code") or "optional_skipped"
            reason = (
                item.get("unscheduled_reason_detail")
                or item.get("reason")
                or "Optional activity skipped because no good route-safe slot was available."
            )
            skipped.append({
                "activity_id": item.get("stable_activity_id") or item.get("id"),
                "title": item.get("title"),
                "duration_minutes": item.get("duration_minutes"),
                "reason": reason,
                "reason_code": reason_code,
                "priority": item.get("priority", "low"),
                "timing_mode": item.get("timing_mode") or item.get("original_timing_mode") or TimingMode.UNSPECIFIED,
                "optional_reason": item.get("optional_reason") or "optional",
            })
        return skipped

    def _route_repair_unfit_metadata(
        self,
        item: Dict[str, Any],
        *,
        reason_code: str,
        reason: str,
    ) -> Dict[str, Any]:
        title = item.get("title") or "Activity"
        duration = item.get("duration_minutes")
        return {
            "activity_id": item.get("stable_activity_id") or item.get("id"),
            "title": title,
            "duration_minutes": duration,
            "reason": reason,
            "reason_code": reason_code,
            "blocking_constraint": {"reason_code": reason_code},
            "suggested_resolution": list(dict.fromkeys([
                f"Shorten {title} below {duration} minutes." if duration else f"Shorten {title}.",
                f"Schedule {title} another day or free up a larger route-safe slot.",
            ])),
            "priority": item.get("priority", "medium"),
            "timing_mode": item.get("timing_mode") or item.get("original_timing_mode") or TimingMode.UNSPECIFIED,
        }

    def _account_for_route_repair_activities(
        self,
        original_active: List[Dict[str, Any]],
        repaired: Dict[str, Any],
        unfit_activities: List[Dict[str, Any]],
        optional_skipped: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        original_records, original_duplicate_keys = self._unique_route_repair_original_records(original_active)
        scheduled_keys: set[str] = set()
        for item in repaired.get("activities") or []:
            if not isinstance(item, dict):
                continue
            key = self._route_repair_activity_key(item)
            if key:
                scheduled_keys.add(key)
        for block in repaired.get("schedule_blocks") or []:
            if not isinstance(block, dict) or not self._is_activity_schedule_block(block):
                continue
            key = self._route_repair_activity_key(block)
            if key:
                scheduled_keys.add(key)

        updated_unfit, unfit_duplicate_keys = self._dedupe_route_repair_items(unfit_activities or [], item_type="unfit")
        updated_optional, optional_duplicate_keys = self._dedupe_route_repair_items(optional_skipped or [], item_type="optional_skipped")
        unfit_keys = {self._route_repair_activity_key(item) for item in updated_unfit or []}
        optional_keys = {self._route_repair_activity_key(item) for item in updated_optional or []}
        missing_after: List[str] = []

        for item in original_records:
            key = self._route_repair_activity_key(item)
            if not key:
                continue
            if key in scheduled_keys or key in unfit_keys or key in optional_keys:
                continue
            if self._route_repair_item_is_optional(item):
                metadata = self._optional_skipped_from_unscheduled([
                    {
                        **deepcopy(item),
                        "unscheduled_reason": "optional_skipped_after_route_repair",
                        "reason": "Optional activity skipped after route-aware repair.",
                    }
                ])[0]
                updated_optional.append(metadata)
                optional_keys.add(key)
            else:
                updated_unfit.append(self._route_repair_unfit_metadata(
                    item,
                    reason_code="missing_after_route_repair",
                    reason="Could not fit after route-aware repair.",
                ))
                unfit_keys.add(key)

        accounted_keys = scheduled_keys | unfit_keys | optional_keys
        original_keys = {self._route_repair_activity_key(item) for item in original_records if self._route_repair_activity_key(item)}
        for item in original_records:
            key = self._route_repair_activity_key(item)
            if key and key not in accounted_keys:
                missing_after.append(key)
        duplicate_keys = list(dict.fromkeys(original_duplicate_keys + unfit_duplicate_keys + optional_duplicate_keys))
        duplicate_titles = self._route_repair_duplicate_titles(
            duplicate_keys,
            list(original_records) + list(updated_unfit or []) + list(updated_optional or []),
        )
        missing_titles = self._route_repair_duplicate_titles(missing_after, original_records)

        jlog(
            "TRAVEL_REPAIR",
            (
                f"scheduled={len(scheduled_keys)} unfit={len(updated_unfit)} "
                f"optional_skipped={len(updated_optional)} missing={missing_after}"
            ),
            "ACCOUNTING",
        )
        invariant_ok = len((scheduled_keys | unfit_keys | optional_keys) & original_keys) == len(original_keys)
        accounting_section = "WARNING" if missing_after or duplicate_keys or not invariant_ok else "OK"
        jlog(
            "ACCOUNTING",
            (
                f"original={len(original_keys)} "
                f"scheduled={len(scheduled_keys & original_keys)} unfit={len(unfit_keys & original_keys)} "
                f"optional_skipped={len(optional_keys & original_keys)} explicitly_removed=0 "
                f"missing={missing_after} duplicates={duplicate_keys}"
            ),
            accounting_section,
        )
        if accounting_section == "WARNING":
            jlog(
                "ACCOUNTING",
                f"missing_titles={missing_titles} duplicate_titles={duplicate_titles}",
                "DETAIL",
            )
        return {
            "unfit_activities": updated_unfit,
            "optional_skipped": updated_optional,
            "missing_after_accounting": missing_after,
            "duplicate_after_accounting": duplicate_keys,
        }

    def _unique_route_repair_original_records(self, original_active: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
        records: List[Dict[str, Any]] = []
        seen: set[str] = set()
        duplicates: List[str] = []
        for item in original_active or []:
            if not isinstance(item, dict):
                continue
            if self._route_repair_item_is_schedule_constraint(item):
                continue
            if item.get("status") not in {None, "active"}:
                continue
            key = self._route_repair_activity_key(item)
            if not key:
                continue
            if key in seen:
                duplicates.append(key)
                continue
            seen.add(key)
            records.append(item)
        return records, duplicates

    def _route_repair_duplicate_titles(self, keys: List[str], records: List[Dict[str, Any]]) -> List[str]:
        if not keys:
            return []
        wanted = set(keys)
        titles: List[str] = []
        for record in records or []:
            if self._route_repair_activity_key(record) in wanted:
                title = record.get("title")
                if title:
                    titles.append(str(title))
        return list(dict.fromkeys(titles))

    def _route_repair_item_is_schedule_constraint(self, item: Dict[str, Any]) -> bool:
        constraint_type = clean_title(
            item.get("semantic_constraint_type")
            or item.get("constraint_type")
            or item.get("schedule_constraint_type")
            or ""
        )
        if constraint_type in {"return_home_deadline", "must_arrive_home_by", "home_deadline"}:
            return True
        return bool(item.get("is_schedule_constraint"))

    def _blocked_activities_for_fixed_route_conflict(
        self,
        active: List[Dict[str, Any]],
        unscheduled: List[Dict[str, Any]],
        remaining_conflicts: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        explicit_blocked = [
            item for item in unscheduled or []
            if item.get("reason_code") == "blocked_by_fixed_route_conflict"
            or item.get("unscheduled_reason") == "blocked_by_fixed_route_conflict"
        ]
        if not explicit_blocked:
            return []
        fixed_conflict = next(
            (
                conflict for conflict in remaining_conflicts or []
                if conflict.get("reason_code") == "fixed_to_fixed_infeasible"
            ),
            None,
        )
        if not fixed_conflict:
            return []
        candidates = list(explicit_blocked)
        blocked: List[Dict[str, Any]] = []
        seen: set[str] = set()
        from_title = fixed_conflict.get("from_activity") or fixed_conflict.get("from") or "a fixed event"
        to_title = fixed_conflict.get("to_activity") or fixed_conflict.get("to") or "another fixed event"
        reason = (
            f"{from_title} and {to_title} cannot both be reached on time with accurate travel. "
            "Resolve that fixed route conflict before this activity can be safely placed."
        )
        for item in candidates:
            item_id = str(item.get("stable_activity_id") or item.get("id") or item.get("title") or "")
            if item_id in seen:
                continue
            seen.add(item_id)
            title = item.get("title") or "Activity"
            duration = item.get("duration_minutes")
            blocked.append({
                "activity_id": item.get("stable_activity_id") or item.get("id"),
                "title": title,
                "duration_minutes": duration,
                "reason": reason,
                "reason_code": "blocked_by_fixed_route_conflict",
                "blocking_constraint": {
                    "reason_code": "fixed_to_fixed_infeasible",
                    "from_activity": fixed_conflict.get("from_activity") or fixed_conflict.get("from"),
                    "to_activity": fixed_conflict.get("to_activity") or fixed_conflict.get("to"),
                    "required_travel_minutes": fixed_conflict.get("required_travel_minutes") or fixed_conflict.get("required_route_minutes"),
                    "available_gap_minutes": fixed_conflict.get("available_gap_minutes") or fixed_conflict.get("available_minutes"),
                },
                "suggested_resolution": [
                    f"Allow moving {to_title} or convert it to flexible.",
                    f"Move {from_title} earlier if that fixed event is actually movable.",
                    f"Schedule {title} another day after resolving the fixed route conflict.",
                ],
            })
        return blocked

    def _filter_items_not_scheduled_in_repair(
        self,
        items: List[Dict[str, Any]],
        repaired: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        if not items:
            return []
        scheduled_keys: set[str] = set()
        for block in repaired.get("schedule_blocks") or []:
            if not isinstance(block, dict) or not self._is_activity_schedule_block(block):
                continue
            for value in (
                block.get("stable_activity_id"),
                block.get("id"),
                clean_title(block.get("title") or ""),
            ):
                if value:
                    scheduled_keys.add(str(value))
        filtered: List[Dict[str, Any]] = []
        for item in items:
            item_keys = {
                str(value)
                for value in (
                    item.get("activity_id"),
                    item.get("stable_activity_id"),
                    item.get("id"),
                    clean_title(item.get("title") or ""),
                )
                if value
            }
            if item_keys and item_keys.intersection(scheduled_keys):
                continue
            filtered.append(item)
        return filtered

    def _final_validate_route_aware_repair(
        self,
        original: Dict[str, Any],
        repaired: Dict[str, Any],
        route_context: Dict[str, Any],
        accepted_fixed_move_ids: Optional[set[str]] = None,
    ) -> Dict[str, Any]:
        final_validation_started = time.perf_counter()
        blocks = [
            deepcopy(block)
            for block in (repaired.get("schedule_blocks") or [])
            if not (isinstance(block, dict) and (block.get("display_only") or block.get("is_route_conflict")))
        ]
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
        accepted_fixed_move_ids = {str(value) for value in (accepted_fixed_move_ids or set()) if value}
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
                record_id = str(original_record.get("stable_activity_id") or original_record.get("id") or "")
                if record_id and record_id in accepted_fixed_move_ids:
                    continue
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
                    "first_physical_event_location": (
                        start_entry.get("to_location")
                        or first.get("location_label")
                        or first.get("location")
                        or first.get("title")
                    ),
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
                            "destination_movable": self._activity_can_move_for_route_repair(first),
                            "destination_activity_id": first.get("stable_activity_id") or first.get("id"),
                            "blocker_movable": self._activity_can_auto_move_for_route_repair(block),
                            "reason": (
                                f"{block.get('title')} and {first.get('title')} are fixed/protected around the required "
                                f"leave-by time {format_clock(leave_by)}, so this start route is not physically feasible."
                                if (
                                    not self._activity_can_auto_move_for_route_repair(block)
                                    and not self._activity_can_move_for_route_repair(first)
                                )
                                else f"{block.get('title')} ends after the leave-by time for route-based travel to {first.get('title')}."
                            ),
                            "reason_code": (
                                "fixed_to_fixed_infeasible"
                                if (
                                    not self._activity_can_auto_move_for_route_repair(block)
                                    and not self._activity_can_move_for_route_repair(first)
                                )
                                else "start_route_blocker"
                            ),
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
            if route_minutes <= 0 or clean_title(entry.get("source") or "") == "same_location":
                continue
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
                if not self._activity_can_move_for_route_repair(left) and not self._activity_can_move_for_route_repair(right):
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
                        "destination_movable": False,
                        "destination_activity_id": right.get("stable_activity_id") or right.get("id"),
                        "reason": f"Your {left.get('title')} ends at {left.get('location')} at {format_clock(left_end)}, but {right.get('title')} starts in {right.get('location')} at {format_clock(right_start)}. Accurate travel requires about {route_minutes} minutes, so this fixed sequence is not physically feasible.",
                        "reason_code": "fixed_to_fixed_infeasible",
                    })
                else:
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

        blocks = self._with_fixed_route_conflict_warning_blocks(blocks, route_conflicts)
        status = "route_conflict" if route_conflicts else (
            "fallback_used" if route_context.get("fallback_used") else "validated"
        )
        elapsed = time.perf_counter() - final_validation_started
        self._add_travel_perf_time("final_validation_seconds", elapsed)
        jlog("TIMER", f"final_validation_seconds={elapsed:.2f}", None)
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
            jlog(
                "TRAVEL_REPAIR",
                (
                    f"title={original_record.get('title')} from={format_clock(original_record.get('_repair_start') or 0)} "
                    f"to={format_clock(candidate.get('_repair_start') or 0)} reason={reason}"
                ),
                "MOVE",
            )
        return actions

    def _log_preference_window_violations(self, repaired: Dict[str, Any]) -> None:
        attempts_by_key = repaired.get("preference_rescue_attempts") or {}
        for item in repaired.get("activities") or []:
            start, end = self._activity_time_bounds(item)
            preference = preference_window_deviation(item, start, end)
            info = preference.get("info") or {}
            if not info:
                continue
            jlog_verbose(
                "PREF_WINDOW",
                (
                    f"title={item.get('title')} source={info.get('source')} "
                    f"window={format_clock(info.get('acceptable_start'))}-{format_clock(info.get('acceptable_end'))} "
                    f"weight={info.get('weight')}"
                ),
                "DETECT",
            )
            if not preference.get("deviation"):
                continue
            if info.get("weight") not in {"hard", "high"}:
                continue
            if start is not None and int(info.get("acceptable_start") or 0) <= int(start) <= int(info.get("acceptable_end") or 0):
                continue
            key = self._route_repair_activity_key(item)
            attempts = attempts_by_key.get(key) or []
            jlog(
                "PREF_WINDOW",
                (
                    f"title={item.get('title')} start={format_clock(start)} "
                    f"reason=no_feasible_alternative attempts={attempts}"
                ),
                "VIOLATION_ALLOWED",
            )

    def _find_active_activity_for_repair_confirmation(
        self,
        active: List[Dict[str, Any]],
        *,
        activity_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        clean = clean_title(title or "")
        if activity_id:
            for item in active:
                if str(item.get("stable_activity_id") or item.get("id") or "") == str(activity_id):
                    return item
        if clean:
            for item in active:
                if clean_title(item.get("title") or "") == clean:
                    return item
        return None

    def _repair_confirmation_move_specs(
        self,
        suggestion: Dict[str, Any],
        active: List[Dict[str, Any]],
    ) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
        specs: Dict[str, Dict[str, Any]] = {}
        ignored_cascades: List[Dict[str, Any]] = []

        def add_spec(source: Dict[str, Any], *, default_impact: str) -> None:
            target = self._find_active_activity_for_repair_confirmation(
                active,
                activity_id=source.get("activity_id"),
                title=source.get("title"),
            )
            if not target:
                ignored_cascades.append(source)
                return
            start = parse_clock(source.get("to") or "")
            if start is None:
                start = source.get("to_start") or source.get("to_start_minutes")
            end = parse_clock(source.get("to_end") or source.get("to_end_label") or "")
            if end is None:
                end = source.get("to_end") or source.get("to_end_minutes")
            duration = int(
                source.get("duration_minutes")
                or target.get("duration_minutes")
                or ((int(end) - int(start)) if start is not None and end is not None else DEFAULT_DURATION)
            )
            if start is None:
                ignored_cascades.append(source)
                return
            start = int(start)
            end = int(end) if end is not None else start + duration
            if end <= start:
                end = start + duration
            key = str(target.get("stable_activity_id") or target.get("id") or source.get("activity_id") or "")
            if not key:
                ignored_cascades.append(source)
                return
            specs[key] = {
                "activity_id": key,
                "title": target.get("title") or source.get("title"),
                "start": start,
                "end": end,
                "impact_type": source.get("impact_type") or default_impact,
                "source": deepcopy(source),
            }

        add_spec(suggestion, default_impact="fixed_target_move")
        for cascade in suggestion.get("cascade_suggestions") or []:
            impact = clean_title(cascade.get("impact_type") or "")
            if impact in {"anchor_dependent_recalculated", "flex_replanned", "became_unfit"}:
                continue
            target = self._find_active_activity_for_repair_confirmation(
                active,
                activity_id=cascade.get("activity_id"),
                title=cascade.get("title"),
            )
            if target and self._activity_requires_repair_confirmation(target):
                add_spec(cascade, default_impact="protected_cascade_move")
        return specs, ignored_cascades

    def _activity_is_relative_to_any(
        self,
        item: Dict[str, Any],
        changed_ids: set[str],
        changed_titles: set[str],
    ) -> bool:
        relation = item.get("anchor_relation") or {}
        if not relation:
            return False
        target_id = str(relation.get("target_activity_id") or relation.get("target_id") or "")
        target_title = clean_title(relation.get("target_title") or "")
        return bool((target_id and target_id in changed_ids) or (target_title and target_title in changed_titles))

    def _release_activity_for_repair_confirmation(
        self,
        item: Dict[str, Any],
        *,
        keep_relative: bool = False,
    ) -> None:
        item["preserve_scheduled_time"] = False
        item["locked_fixed"] = False
        item.pop("scheduled_start", None)
        item.pop("scheduled_end", None)
        item.pop("startTime", None)
        item.pop("endTime", None)
        if keep_relative or item.get("anchor_relation"):
            item["timing_mode"] = TimingMode.RELATIVE
        else:
            if item.get("timing_mode") == TimingMode.FIXED:
                item["timing_mode"] = item.get("original_timing_mode") or TimingMode.UNSPECIFIED
            if item.get("timing_mode") == TimingMode.FIXED:
                item["timing_mode"] = TimingMode.UNSPECIFIED
            item["fixed_start"] = None
            item["fixed_end"] = None
            item["user_fixed_start"] = None
            item["is_user_fixed"] = False
            item["can_move_for_repair"] = True

    def _lock_activity_for_repair_confirmation(self, item: Dict[str, Any]) -> None:
        duration = int(item.get("duration_minutes") or DEFAULT_DURATION)
        start = item.get("fixed_start")
        if start is None:
            start = item.get("scheduled_start")
        if start is None:
            start = parse_clock(item.get("startTime") or "")
        end = item.get("fixed_end")
        if end is None:
            end = item.get("scheduled_end")
        if end is None:
            end = parse_clock(item.get("endTime") or "")
        if start is None:
            return
        start = int(start)
        end = int(end) if end is not None else start + duration
        if end <= start:
            end = start + duration
        item["scheduled_start"] = start
        item["scheduled_end"] = end
        item["fixed_start"] = start
        item["fixed_end"] = end
        item["earliest_start"] = start
        item["latest_end"] = end
        item["timing_mode"] = TimingMode.FIXED
        item["locked_fixed"] = True
        item["can_move_for_repair"] = False

    def _prepare_active_for_repair_confirmation(
        self,
        active: List[Dict[str, Any]],
        move_specs: Dict[str, Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], set[str]]:
        prepared = deepcopy(active)
        accepted_ids = set(move_specs.keys())
        changed_ids = set(move_specs.keys())
        changed_titles = {
            clean_title(spec.get("title") or "")
            for spec in move_specs.values()
            if spec.get("title")
        }

        progress = True
        affected_relative_ids: set[str] = set()
        while progress:
            progress = False
            for item in prepared:
                item_id = str(item.get("stable_activity_id") or item.get("id") or "")
                if item_id in affected_relative_ids:
                    continue
                if self._activity_is_relative_to_any(item, changed_ids, changed_titles):
                    affected_relative_ids.add(item_id)
                    if item_id:
                        changed_ids.add(item_id)
                    title = clean_title(item.get("title") or "")
                    if title:
                        changed_titles.add(title)
                    progress = True

        for item in prepared:
            item_id = str(item.get("stable_activity_id") or item.get("id") or "")
            spec = move_specs.get(item_id)
            if spec:
                duration = int(item.get("duration_minutes") or (spec["end"] - spec["start"]) or DEFAULT_DURATION)
                item["scheduled_start"] = spec["start"]
                item["scheduled_end"] = spec["end"]
                item["fixed_start"] = spec["start"]
                item["fixed_end"] = spec["end"]
                item["earliest_start"] = spec["start"]
                item["latest_end"] = spec["end"]
                item["duration_minutes"] = duration
                item["timing_mode"] = TimingMode.FIXED
                item["is_user_fixed"] = True
                item["user_fixed_start"] = spec["start"]
                item["locked_fixed"] = False
                item["preserve_scheduled_time"] = False
                item["can_move_for_repair"] = False
                item.setdefault("trace", []).append("Accepted accurate travel repair suggestion.")
                continue

            if item_id in affected_relative_ids or item.get("anchor_relation"):
                self._release_activity_for_repair_confirmation(item, keep_relative=True)
                continue

            if self._activity_requires_repair_confirmation(item):
                self._lock_activity_for_repair_confirmation(item)
                continue

            timing = clean_title(item.get("timing_mode") or item.get("original_timing_mode") or "")
            if self._activity_can_auto_move_for_route_repair(item) or timing in {
                "flexible",
                TimingMode.PREFERRED,
                TimingMode.UNSPECIFIED,
                "",
            }:
                self._release_activity_for_repair_confirmation(item)
                continue

            item["preserve_scheduled_time"] = False
        return prepared, accepted_ids

    def _activity_can_be_unfit_after_repair_confirmation(
        self,
        item: Dict[str, Any],
        accepted_ids: set[str],
    ) -> bool:
        item_id = str(item.get("stable_activity_id") or item.get("id") or "")
        if item_id and item_id in accepted_ids:
            return False
        if self._activity_requires_repair_confirmation(item):
            return False
        if item.get("anchor_relation"):
            return True
        return self._activity_can_auto_move_for_route_repair(item)

    def _remove_repair_confirmation_unfit_conflicts(
        self,
        planned_activities: List[Dict[str, Any]],
        accepted_ids: set[str],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        conflict_unfit = [
            item for item in planned_activities
            if item.get("is_conflict") and self._activity_can_be_unfit_after_repair_confirmation(item, accepted_ids)
        ]
        if not conflict_unfit:
            return planned_activities, []
        removed_ids = {
            str(item.get("stable_activity_id") or item.get("id") or "")
            for item in conflict_unfit
        }
        kept = [
            item for item in planned_activities
            if str(item.get("stable_activity_id") or item.get("id") or "") not in removed_ids
        ]
        for item in kept:
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
        for item in conflict_unfit:
            item["status"] = "unscheduled"
            item["unscheduled_reason"] = item.get("conflict_reason") or "no_route_safe_slot_after_repair_confirmation"
            item["unscheduled_reason_detail"] = item.get("conflict_reason") or (
                f"{item.get('title') or 'This activity'} could not fit after the accepted route repair."
            )
        return kept, conflict_unfit

    def _repair_confirmation_failure_reason(
        self,
        suggestion: Dict[str, Any],
        route_conflicts: List[Dict[str, Any]],
    ) -> str:
        if not route_conflicts:
            return "The repair could not be applied safely."
        first = route_conflicts[0]
        reason = first.get("reason")
        if reason:
            return str(reason)
        left = first.get("from_activity") or first.get("from")
        right = first.get("to_activity") or first.get("to")
        if left and right:
            return f"The repair could not be applied because {left} and {right} still conflict."
        return f"The repair for {suggestion.get('title') or 'the selected activity'} could not be applied safely."

    def _mark_repair_confirmation_failed(
        self,
        envelope: Dict[str, Any],
        suggestion: Dict[str, Any],
        reason: str,
        route_conflicts: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        updated = deepcopy(envelope)
        suggestion_id = suggestion.get("id")
        updated["pending_repair_suggestions"] = [
            item for item in (updated.get("pending_repair_suggestions") or [])
            if item.get("id") != suggestion_id
        ]
        updated.pop("preview_id", None)
        updated.pop("preview_base_version", None)
        updated.pop("preview_schedule", None)
        updated.pop("committed_schedule_blocks", None)
        updated["preview_status"] = "apply_failed"
        updated["preview_reason"] = reason
        updated["failed_repair_attempt"] = {
            "suggestion_id": suggestion_id,
            "title": suggestion.get("title"),
            "reason": reason,
            "route_conflicts": deepcopy(route_conflicts or []),
            "attempted_at": self._now_iso(),
        }
        return updated

    def _commit_repair_confirmation_candidate(
        self,
        envelope: Dict[str, Any],
        suggestion: Dict[str, Any],
        saved_locations: List[Dict[str, Any]],
        *,
        allow_fixed_event_move: bool = False,
    ) -> Dict[str, Any]:
        if self._repair_suggestion_requires_fixed_move_approval(suggestion) and not allow_fixed_event_move:
            return {
                "applied": False,
                "needs_fixed_move_confirmation": True,
                "envelope": envelope,
                "reply_reason": "pending_fixed_repair_requires_confirmation",
            }
        current_version = int(envelope.get("version") or 1)
        original = deepcopy(envelope)
        self._sync_resolved_locations_to_activities(original)
        active = [
            item for item in self._load_canonical_activities(original)
            if item.get("status") == "active"
        ]
        if not active:
            return {
                "applied": False,
                "envelope": self._mark_repair_confirmation_failed(
                    envelope,
                    suggestion,
                    "There are no active activities to repair.",
                ),
                "reply_reason": "pending_repair_apply_failed",
            }

        move_specs, ignored_cascades = self._repair_confirmation_move_specs(suggestion, active)
        if not move_specs:
            return {
                "applied": False,
                "envelope": self._mark_repair_confirmation_failed(
                    envelope,
                    suggestion,
                    "That repair suggestion no longer targets an active activity.",
                ),
                "reply_reason": "pending_repair_apply_failed",
            }

        prepared_active, accepted_ids = self._prepare_active_for_repair_confirmation(active, move_specs)
        preferences = deepcopy(original.get("preferences") or {})
        preferences["accurate_travel_time"] = True
        preferences["travel_intent"] = True
        preferences["route_aware_repair"] = True
        preferences["refinement_reason"] = "explicit_route_repair"
        preferences["module_0_route"] = "repair_confirmation"
        preferences["module_0_reason"] = "pending_repair_confirmation"

        route_basis = deepcopy(original)
        route_basis["preferences"] = preferences
        route_basis["activities"] = prepared_active
        route_context = self._build_route_context(route_basis, prepared_active, saved_locations)
        preferences["_route_context"] = route_context
        planned = self._plan_schedule(
            original.get("date") or self._local_today_iso(),
            prepared_active,
            preferences,
        )
        planned_activities, conflict_unfit = self._remove_repair_confirmation_unfit_conflicts(
            list(planned.get("activities", [])),
            accepted_ids,
        )
        day_start = parse_clock(planned.get("day_start") or preferences.get("day_start") or preferences.get("day_start_time") or "") or DEFAULT_DAY_START
        min_travel = int(preferences.get("min_travel_buffer_minutes") or 0)
        schedule_blocks = (
            self._materialize_blocks(planned_activities, day_start, min_travel)
            if conflict_unfit
            else planned.get("schedule_blocks", [])
        )

        candidate = deepcopy(original)
        candidate_preferences = deepcopy(preferences)
        candidate_preferences.pop("_route_context", None)
        candidate_preferences.pop("route_aware_repair", None)
        candidate["schema_version"] = 4
        candidate["version"] = current_version + 1
        candidate["status"] = "ok"
        candidate["schedule_status"] = "ok"
        candidate["accurate_travel_time"] = True
        candidate["travel_intent"] = True
        candidate["preferences"] = candidate_preferences
        candidate["activities"] = [self._format_activity(item) for item in planned_activities]
        candidate["schedule_blocks"] = schedule_blocks
        unscheduled_raw = list(planned.get("unscheduled_activities", [])) + conflict_unfit
        candidate["unscheduled_activities"] = [self._format_activity(item) for item in unscheduled_raw]
        candidate["conflicts"] = self._build_conflicts(planned_activities, set())
        candidate["warnings"] = self._collect_schedule_warnings(planned_activities, schedule_blocks)
        for key in REFINEMENT_META_KEYS:
            candidate[key] = planned.get(key)

        validation = self._final_validate_route_aware_repair(
            original=original,
            repaired=candidate,
            route_context=route_context,
            accepted_fixed_move_ids=accepted_ids,
        )
        route_conflicts = validation.get("route_conflicts", [])
        if route_conflicts:
            reason = self._repair_confirmation_failure_reason(suggestion, route_conflicts)
            return {
                "applied": False,
                "envelope": self._mark_repair_confirmation_failed(envelope, suggestion, reason, route_conflicts),
                "reply_reason": "pending_repair_apply_failed",
            }

        unfit = self._repair_unfit_activities(
            prepared_active,
            unscheduled_raw,
            validation.get("route_conflicts", []),
        )
        actions = []
        for spec in move_specs.values():
            source = spec.get("source") or {}
            actions.append({
                "type": "move_activity",
                "impact_type": spec.get("impact_type"),
                "activity_id": spec.get("activity_id"),
                "title": spec.get("title"),
                "from": source.get("from"),
                "from_end": source.get("from_end") or source.get("from_end_label"),
                "to": format_clock(spec.get("start")),
                "to_end": format_clock(spec.get("end")),
                "reason": source.get("reason") or suggestion.get("reason") or "Accepted route repair suggestion.",
            })
        actions.extend(self._route_repair_actions_from_delta(original, candidate, route_context))

        candidate["location_resolution_requests"] = []
        candidate["travel_validation_status"] = "partial_feasible_with_unfit" if unfit else "repaired_validated"
        candidate["route_conflicts"] = []
        candidate["route_repair_attempted"] = True
        candidate["route_repair_actions"] = actions
        candidate["route_efficiency"] = planned.get("route_efficiency") or {}
        candidate["pending_repair_suggestions"] = []
        candidate["unfit_activities"] = unfit
        candidate["unscheduled_activities"] = candidate["unscheduled_activities"] if unfit else []
        candidate["unmet_items"] = candidate["unscheduled_activities"]
        candidate["unmet_optional"] = candidate["unscheduled_activities"]
        candidate["validation_issues"] = [
            f"{item.get('title') or 'One activity'} could not fit after accurate travel validation."
            for item in unfit
        ]
        candidate["updated_transition_count"] = int(validation.get("updated_transition_count") or 0)
        candidate["start_route_summary"] = validation.get("start_route_summary")
        candidate["schedule_blocks"] = validation.get("schedule_blocks") or candidate["schedule_blocks"]
        if candidate["travel_validation_status"] == "partial_feasible_with_unfit":
            candidate["status"] = "partial"
            candidate["schedule_status"] = "partial"
        else:
            candidate["status"] = "ok"
            candidate["schedule_status"] = "ok"
        if ignored_cascades:
            candidate.setdefault("warnings", []).append({
                "warning_code": "IGNORED_REPAIR_CASCADE",
                "explanation": "Some stale cascade entries were ignored while applying the route repair.",
            })
        self._clear_route_preview_metadata(candidate)
        self._sync_resolved_locations_to_activities(candidate)
        return {
            "applied": True,
            "envelope": candidate,
            "reply_reason": "pending_repair_accepted",
        }

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
        envelope_preview_id = envelope.get("preview_id")
        suggestion_preview_id = suggestion.get("preview_id")
        preview_base_version = int(envelope.get("preview_base_version") or schedule_version)
        if (
            not suggestion_id
            or suggestion_version != schedule_version
            or preview_base_version != schedule_version
            or (envelope_preview_id and suggestion_preview_id != envelope_preview_id)
        ):
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
            self._clear_route_preview_metadata(updated)
            jlog("PENDING_REPAIR", f"id={suggestion_id} action=rejected", None)
            return {
                "handled": True,
                "reply": "No problem. I kept the current schedule unchanged and left the route conflict visible.",
                "reply_status": "warning",
                "reply_reason": "pending_repair_rejected",
                "envelope": updated,
            }

        if not self._pending_suggestion_would_change(suggestion):
            updated = deepcopy(envelope)
            updated["pending_repair_suggestions"] = []
            self._clear_route_preview_metadata(updated)
            jlog(
                "PENDING_REPAIR",
                (
                    f"title={suggestion.get('title') or 'activity'} "
                    f"from={suggestion.get('from')} to={suggestion.get('to')}"
                ),
                "SKIP_NOOP",
            )
            jlog("PENDING_REPAIR", f"id={suggestion_id} action=cleared", None)
            return {
                "handled": True,
                "reply": "That repair suggestion no longer changes the schedule, so I cleared it and kept the current plan unchanged.",
                "reply_status": "warning",
                "reply_reason": "pending_repair_noop_cleared",
                "envelope": updated,
            }

        if action == "accept" and self._repair_suggestion_requires_fixed_move_approval(suggestion):
            title = suggestion.get("title") or "this fixed event"
            reply = (
                f"This will move your fixed/protected {title} from {suggestion.get('from')}–{suggestion.get('from_end')} "
                f"to {suggestion.get('to')}–{suggestion.get('to_end')}. Are you sure you want to allow this fixed event to move?"
            )
            jlog("PENDING_REPAIR", f"id={suggestion_id} action=needs_fixed_confirmation", None)
            return {
                "handled": True,
                "reply": reply,
                "reply_status": "warning",
                "reply_reason": "pending_fixed_repair_requires_confirmation",
                "envelope": envelope,
            }

        jlog("PENDING_REPAIR", f"id={suggestion_id} action={action}", None)
        result = self._commit_repair_confirmation_candidate(
            envelope,
            suggestion,
            saved_locations,
            allow_fixed_event_move=(action == "allow_fixed_move"),
        )
        updated_envelope = result.get("envelope") or envelope
        if result.get("applied"):
            jlog("PENDING_REPAIR", f"id={suggestion_id} action=cleared", None)
            if updated_envelope.get("travel_validation_status") == "partial_feasible_with_unfit":
                reply = (
                    "I applied the repair suggestion and rescheduled the route-safe activities. "
                    "Some activities still could not fit and remain listed as unfit."
                )
                reply_status = "warning"
                reply_reason = "pending_repair_accepted_with_unfit"
            else:
                reply = f"I applied the repair suggestion and moved {suggestion.get('title')} to {suggestion.get('to')}."
                reply_status = "success"
                reply_reason = "pending_repair_accepted"
        else:
            reason = updated_envelope.get("preview_reason") or "The previous repair could not be applied safely."
            reply = (
                f"The previous repair could not be applied because {reason} "
                "Please choose a different repair or allow the system to reschedule flexible activities."
            )
            reply_status = "warning"
            reply_reason = result.get("reply_reason") or "pending_repair_apply_failed"
            jlog("PENDING_REPAIR", f"id={suggestion_id} action=failed reason={reason}", None)

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
        if re.search(r"\b(allow|approve|permit)\b.*\b(fixed|protected|critical)\b.*\b(move|moving|change)\b", clean):
            return "allow_fixed_move"
        if re.search(r"\b(convert|make)\b.*\b(flexible|movable)\b", clean):
            return "allow_fixed_move"
        if re.search(r"\ballow\b.*\b(move|moving)\b.*\b(fixed|protected|critical)\b", clean):
            return "allow_fixed_move"
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
        include_start_location: bool = True,
    ) -> List[Dict[str, Any]]:
        requests: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        grouped_by_location: Dict[str, Dict[str, Any]] = {}
        skipped_not_required: List[str] = []
        blocks = list(envelope.get("schedule_blocks") or envelope.get("activities") or [])
        first_physical = self._first_physical_activity_index(blocks)
        if include_start_location and first_physical is not None and not self._start_location_coordinate_resolution(envelope.get("preferences") or {}, saved_locations)[0]:
            requests.append(self._start_location_resolution_request(envelope.get("preferences") or {}))
        for index, block in enumerate(blocks):
            block_type = block.get("block_type") or block.get("type")
            if block_type and block_type != "activity":
                continue
            needs_flexible_route_context = self._location_flexible_block_needs_route_context(blocks, index, envelope)
            if needs_flexible_route_context:
                jlog(
                    "LOCATION_REQUEST",
                    f"title={block.get('title')} reason=location_flexible_affects_route",
                    "ASK",
                )
                block = deepcopy(block)
                block["travel_required"] = True
                block["location_kind"] = "unknown_physical"
                block["location_category"] = "unknown"
                block["location_status"] = "needs_resolution"
                block["location_resolution_status"] = "needs_coordinates"
                block["location_policy"] = "location_flexible"
            elif self._block_has_no_location_policy(block):
                title = block.get("title")
                if title:
                    skipped_not_required.append(str(title))
                    jlog(
                        "LOCATION_REQUEST",
                        f"title={title} reason=no_location_required_no_route_impact",
                        "SKIP",
                    )
                continue
            if not self._block_requires_travel_coordinate(block):
                title = block.get("title")
                if title:
                    skipped_not_required.append(str(title))
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
        requested_titles = [
            str(request.get("title") or request.get("current_guess") or "activity")
            for request in requests
            if request.get("request_type") != "start_location"
        ]
        jlog(
            "LOCATION_REQUESTS",
            f"requested={requested_titles} skipped_not_required={skipped_not_required}",
            "SUMMARY",
        )
        return requests

    def _block_has_no_location_policy(self, block: Dict[str, Any]) -> bool:
        policy = clean_title(block.get("location_policy") or "").replace(" ", "_").replace("-", "_")
        if policy == "no_location_required":
            return True
        kind = clean_title(block.get("location_kind") or "").replace(" ", "_").replace("-", "_")
        category = clean_title(block.get("location_category") or "")
        status = clean_title(block.get("location_resolution_status") or block.get("location_status") or "")
        if self._block_has_explicit_non_home_location(block):
            return False
        return bool(
            kind in {"no_location_required", "online"}
            or category in {"home_or_online", "no_location", "none"}
            or status in {"not_required", "no_location_required"}
        )

    def _location_flexible_block_needs_route_context(
        self,
        blocks: List[Dict[str, Any]],
        index: int,
        envelope: Dict[str, Any],
    ) -> bool:
        if index < 0 or index >= len(blocks):
            return False
        block = blocks[index]
        if not bool(block.get("location_flexible")):
            return False
        if not bool(block.get("travel_context_required")):
            return False
        if block.get("timing_mode") == TimingMode.FIXED or block.get("fixed_start") is not None:
            return False
        if self._operation_has_coordinates(block):
            return False
        preferences = envelope.get("preferences") or {}
        if not bool(
            envelope.get("accurate_travel_time")
            or preferences.get("accurate_travel_time")
            or envelope.get("travel_intent")
            or preferences.get("travel_intent")
        ):
            return False
        return bool(
            self._nearest_route_context_activity(blocks, index, -1)
            or self._nearest_route_context_activity(blocks, index, 1)
        )

    def _nearest_route_context_activity(
        self,
        blocks: List[Dict[str, Any]],
        index: int,
        direction: int,
    ) -> Optional[Dict[str, Any]]:
        pos = index + direction
        while 0 <= pos < len(blocks):
            candidate = blocks[pos]
            block_type = candidate.get("block_type") or candidate.get("type")
            if not block_type or block_type == "activity":
                if self._block_is_physical_route_context(candidate):
                    return candidate
            pos += direction
        return None

    def _block_is_physical_route_context(self, block: Dict[str, Any]) -> bool:
        if self._location_flexible_selected_endpoint_requires_route(block):
            return True
        if self._embedded_activity_coordinate(block):
            return True
        raw_travel_required = block.get("travel_required")
        if raw_travel_required is False:
            return False
        if isinstance(raw_travel_required, str) and raw_travel_required.strip().lower() in {"0", "false", "no", "off"}:
            return False
        category = clean_title(block.get("location_category") or "")
        status = clean_title(block.get("location_status") or "")
        kind = clean_title(block.get("location_kind") or "").replace(" ", "_").replace("-", "_")
        semantic_status = clean_title(block.get("location_resolution_status") or "").replace(" ", "_").replace("-", "_")
        if category in {"home_or_online", "none", "no_location"}:
            return False
        if status in {"not_required", "no_location_required"}:
            return False
        if kind in {"no_location_required", "online"}:
            return False
        if semantic_status in {"not_required", "no_location_required"}:
            return False
        return bool(raw_travel_required is True or block.get("location") or block.get("location_label") or category)

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
        semantic_status = clean_title(block.get("location_resolution_status") or "").replace(" ", "_").replace("-", "_")
        category = clean_title(block.get("location_category") or "")
        if semantic_status in {"not_required", "no_location_required"}:
            return False
        if semantic_status in {"needs_coordinates", "ambiguous", "missing_coordinates"}:
            return True
        if not block.get("location"):
            return status in {"needs_resolution", "fallback_used", "unresolved", "resolved_default"} or bool(block.get("travel_required") is True)
        if status in {"needs_resolution", "fallback_used", "unresolved"}:
            return True
        return True

    def _block_requires_travel_coordinate(self, block: Dict[str, Any]) -> bool:
        if self._location_flexible_selected_endpoint_requires_route(block):
            return True
        status = clean_title(block.get("location_status") or "")
        semantic_status = clean_title(block.get("location_resolution_status") or "").replace(" ", "_").replace("-", "_")
        location_kind = clean_title(block.get("location_kind") or "").replace(" ", "_").replace("-", "_")
        category = clean_title(block.get("location_category") or "")
        raw_travel_required = block.get("travel_required")
        if self._block_has_explicit_non_home_location(block):
            return True
        if raw_travel_required is False:
            return False
        if isinstance(raw_travel_required, str) and raw_travel_required.strip().lower() in {"0", "false", "no", "off"}:
            return False
        if status in {"not_required", "no_location_required"} or semantic_status in {"not_required", "no_location_required"}:
            return False
        if location_kind in {"no_location_required", "online"}:
            return False
        if category in {"home_or_online", "none", "no_location"}:
            return False
        return True

    def _block_has_explicit_non_home_location(self, block: Dict[str, Any]) -> bool:
        location_kind = clean_title(block.get("location_kind") or "").replace(" ", "_").replace("-", "_")
        if location_kind not in {"exact_named_place", "area_only", "unknown_physical"}:
            return False
        text = clean_title(
            block.get("raw_location_text")
            or block.get("raw_llm_location")
            or block.get("location_label")
            or block.get("location")
            or ""
        )
        if not text or text in NULL_TEXT_VALUES:
            return False
        if re.search(r"\b(home|online|virtual|remote|zoom|teams|google meet|current location|current place)\b", text):
            return False
        return not bool(self._embedded_activity_coordinate(block))

    def _location_flexible_selected_endpoint_requires_route(self, block: Dict[str, Any]) -> bool:
        if not bool(block.get("location_flexible") and block.get("travel_context_required")):
            return False
        resolved = block.get("resolved_location")
        if not isinstance(resolved, dict):
            return False
        if not self._coordinate_from_payload(resolved):
            return False
        label_text = clean_title(
            " ".join(
                str(value or "")
                for value in (
                    block.get("location_label"),
                    block.get("location"),
                    resolved.get("label"),
                    resolved.get("display_name"),
                    resolved.get("category"),
                    block.get("location_category"),
                )
            )
        )
        if re.search(r"\b(home|current location|current place|starting point|start location|default start)\b", label_text):
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
        block["location"] = block["location_label"]
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
                updated["location"] = updated["location_label"] or updated.get("location")
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
                        "first_physical_event_location": (
                            right.get("location_label")
                            or right.get("location")
                            or right.get("title")
                        ),
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
                                blocker_movable = self._activity_can_auto_move_for_route_repair(blocker)
                                destination_movable = self._activity_can_move_for_route_repair(right)
                                hard_start_route_conflict = not blocker_movable and not destination_movable
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
                                    "destination_movable": destination_movable,
                                    "destination_activity_id": right.get("stable_activity_id") or right.get("id"),
                                    "blocker_activity_id": blocker.get("stable_activity_id") or blocker.get("id"),
                                    "blocker_activity_title": blocker.get("title"),
                                    "blocker_movable": blocker_movable,
                                    "reason": (
                                        f"{blocker.get('title')} and {right.get('title')} are fixed/protected around the required "
                                        f"leave-by time {format_clock(leave_by)}, so this start route is not physically feasible."
                                        if hard_start_route_conflict
                                        else (
                                            f"{blocker.get('title')} ends after the leave-by time for route-based travel "
                                            f"to {right.get('title')}."
                                        )
                                    ),
                                    "reason_code": (
                                        "fixed_to_fixed_infeasible"
                                        if hard_start_route_conflict
                                        else "start_route_blocker"
                                    ),
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
            if left_coord == right_coord:
                continue
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
            if route_minutes <= 0:
                continue
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
                source_movable = self._activity_can_move_for_route_repair(left)
                hard_fixed_conflict = not source_movable and not destination_movable
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
                        f"{left.get('title')} and {right.get('title')} are fixed/protected, but accurate travel needs "
                        f"{route_minutes} minutes and only {available_gap} minutes are available."
                        if hard_fixed_conflict
                        else (
                            f"Accurate route from {left.get('title')} to {right.get('title')} needs "
                            f"{route_minutes} minutes, but only {available_gap} minutes are available."
                        )
                    ),
                    "reason_code": "fixed_to_fixed_infeasible" if hard_fixed_conflict else "not_enough_travel_time",
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

        blocks = self._with_fixed_route_conflict_warning_blocks(blocks, route_conflicts)
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
        source_activity_id = left.get("stable_activity_id") or left.get("id")
        destination_activity_id = right.get("stable_activity_id") or right.get("id")
        transition.update({
            "block_type": "transition",
            "type": "travel",
            "id": transition.get("id") or (f"travel-{source_activity_id}-{destination_activity_id}" if source_activity_id and destination_activity_id else None),
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
            "source_activity_id": source_activity_id,
            "destination_activity_id": destination_activity_id,
            "related_activity_ids": [
                activity_id
                for activity_id in (source_activity_id, destination_activity_id)
                if activity_id
            ],
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
            if block.get("display_only") or block.get("is_route_conflict"):
                continue
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

