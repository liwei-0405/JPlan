import json
import re
import time
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4
from zoneinfo import ZoneInfo

from jplan_logging import jjson, jlog, jsection
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
            repaired_validation = self._attempt_route_repair(updated, saved_locations, validation["route_conflicts"])
            if repaired_validation:
                validation = repaired_validation
                updated = {
                    **updated,
                    **validation.get("repaired_envelope", {}),
                }
        updated["location_resolution_requests"] = []
        updated["travel_validation_status"] = validation["travel_validation_status"]
        updated["route_conflicts"] = validation.get("route_conflicts", [])
        updated["route_repair_attempted"] = bool(validation.get("route_repair_attempted", False))
        updated["updated_transition_count"] = int(validation.get("updated_transition_count") or 0)
        updated["schedule_blocks"] = validation.get("schedule_blocks", updated.get("schedule_blocks", []))
        self._sync_resolved_locations_to_activities(updated)
        if validation.get("warnings"):
            updated["warnings"] = list(updated.get("warnings") or []) + validation["warnings"]
            if updated.get("status") == "ok":
                updated["status"] = "warning"
            if updated.get("schedule_status") == "ok":
                updated["schedule_status"] = "warning"

        if validation.get("route_conflicts"):
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
        if status == "validated":
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

    def _attempt_route_repair(
        self,
        envelope: Dict[str, Any],
        saved_locations: List[Dict[str, Any]],
        route_conflicts: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        required_minutes = [
            int(conflict.get("required_route_minutes") or 0)
            for conflict in route_conflicts or []
        ]
        if not required_minutes:
            return None
        preferences = deepcopy(envelope.get("preferences") or {})
        preferences["min_travel_buffer_minutes"] = max(
            int(preferences.get("min_travel_buffer_minutes") or 0),
            max(required_minutes),
        )
        active = [
            item for item in self._load_canonical_activities(envelope)
            if item.get("status") == "active"
        ]
        if not active:
            return None
        self._debug(
            f"[TRAVEL] Attempting route repair with min_travel_buffer_minutes={preferences['min_travel_buffer_minutes']}"
        )
        planned = self._plan_schedule(envelope.get("date") or self._local_today_iso(), active, preferences)
        repaired = deepcopy(envelope)
        repaired["preferences"] = preferences
        repaired["activities"] = [self._format_activity(item) for item in planned.get("activities", [])]
        repaired["schedule_blocks"] = planned.get("schedule_blocks", [])
        repaired["unscheduled_activities"] = [
            self._format_activity(item)
            for item in planned.get("unscheduled_activities", [])
        ]
        validation = self._validate_routes_with_service(repaired, saved_locations)
        validation["route_repair_attempted"] = True
        if validation.get("route_conflicts"):
            return None
        validation["repaired_envelope"] = repaired
        return validation

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
        blocks = list(envelope.get("schedule_blocks") or [])
        for index, block in enumerate(blocks):
            if block.get("block_type") != "activity":
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
            ][:5]
            query = self.travel_service.expand_alias(current_guess or block.get("title") or category or "", category)
            geocode_candidates: List[Dict[str, Any]] = []
            try:
                geocode_candidates = self.travel_service.geocode_candidates(query, limit=5)
            except Exception as exc:
                self._debug(f"[TRAVEL] Geocoding skipped/failed for {block.get('title')}: {exc}")
            self._debug(
                f"[TRAVEL][LOCATION_PENDING] {block.get('title')} requires {category or 'unknown'} location for accurate travel"
            )
            requests.append({
                "activity_id": activity_id,
                "title": block.get("title"),
                "category": category,
                "current_guess": current_guess,
                "expanded_query": query,
                "requires_coordinate": True,
                "affected_transitions": self._affected_transitions_for_activity(blocks, index),
                "reason": "Accurate travel time needs a confirmed coordinate for this activity.",
                "saved_matches": saved_matches,
                "geocode_candidates": geocode_candidates[:5],
            })
        return requests

    def _activity_needs_coordinate_resolution(
        self,
        block: Dict[str, Any],
        saved_locations: List[Dict[str, Any]],
    ) -> bool:
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

    def _affected_transitions_for_activity(
        self,
        blocks: List[Dict[str, Any]],
        index: int,
    ) -> List[Dict[str, Any]]:
        affected: List[Dict[str, Any]] = []
        current = blocks[index]
        previous_activity = next(
            (blocks[pos] for pos in range(index - 1, -1, -1) if blocks[pos].get("block_type") == "activity"),
            None,
        )
        next_activity = next(
            (blocks[pos] for pos in range(index + 1, len(blocks)) if blocks[pos].get("block_type") == "activity"),
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
            raw_lat = payload.get("latitude") if payload.get("latitude") is not None else payload.get("lat")
            raw_lng = payload.get("longitude") if payload.get("longitude") is not None else payload.get("lng")
            try:
                lat = float(raw_lat)
                lng = float(raw_lng)
            except (TypeError, ValueError):
                continue
            if -90 <= lat <= 90 and -180 <= lng <= 180:
                return (lat, lng)
        return None

    def _sync_resolved_locations_to_activities(self, envelope: Dict[str, Any]) -> None:
        activity_blocks = [
            block for block in envelope.get("schedule_blocks") or []
            if block.get("block_type") == "activity" and block.get("resolved_location")
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
        activity_indices = [
            index for index, block in enumerate(blocks)
            if block.get("block_type") == "activity"
        ]
        route_conflicts: List[Dict[str, Any]] = []
        warnings: List[Dict[str, Any]] = []
        replacements: List[Tuple[int, int, List[Dict[str, Any]]]] = []
        fallback_used = False
        updated_transition_count = 0
        transport_mode = ((envelope.get("preferences") or {}).get("transport_mode") or "driving-car")

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
            available_gap = max(0, right_start - left_end)
            time_bucket = f"{envelope.get('date')}T{format_clock(right_start)}"
            try:
                self._debug(
                    f"[TRAVEL][ORS] route request: {left.get('location') or left.get('title')} -> {right.get('location') or right.get('title')}"
                )
                route_minutes = self.travel_service.route_minutes(
                    left_coord,
                    right_coord,
                    transport_mode=transport_mode,
                    time_bucket=time_bucket,
                )
                source = "routing_service"
                self._debug(
                    f"[TRAVEL][ORS] route result: {left.get('location') or left.get('title')} -> {right.get('location') or right.get('title')} = {route_minutes} min"
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
                route_conflicts.append({
                    "type": "route_conflict",
                    "from_activity": left.get("title"),
                    "to_activity": right.get("title"),
                    "from_location": left.get("location"),
                    "to_location": right.get("location"),
                    "available_minutes": available_gap,
                    "required_route_minutes": route_minutes,
                    "reason": (
                        f"Accurate route from {left.get('title')} to {right.get('title')} needs "
                        f"{route_minutes} minutes, but only {available_gap} minutes are available."
                    ),
                })
                continue

            replacement_start = left_index + 1
            if transition_index is not None:
                old_transition_start = parse_clock(transition.get("start") or transition.get("startTime") or "")
                route_start = right_start - route_minutes
                if old_transition_start is not None and route_start >= old_transition_start:
                    replacement_start = transition_index

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

        status = "route_conflict" if route_conflicts else ("fallback_used" if fallback_used else "validated")
        return {
            "travel_validation_status": status,
            "schedule_blocks": blocks,
            "route_conflicts": route_conflicts,
            "warnings": warnings,
            "route_repair_attempted": bool(route_conflicts),
            "updated_transition_count": updated_transition_count,
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
        return replacement

