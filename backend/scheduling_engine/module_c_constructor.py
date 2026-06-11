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

class ModuleCConstructorMixin:
    def _active_route_context(self) -> Optional[Dict[str, Any]]:
        context = getattr(self, "_current_route_context", None)
        if isinstance(context, dict) and context.get("enabled"):
            return context
        return None

    def _route_context_activity_key(self, item: Optional[Dict[str, Any]]) -> str:
        if not item:
            return ""
        activity_id = str(item.get("stable_activity_id") or item.get("id") or "").strip()
        if activity_id:
            return f"id:{activity_id}"
        title = clean_title(item.get("title") or "")
        return f"title:{title}" if title else ""

    def _route_context_pair_key(self, left: Optional[Dict[str, Any]], right: Optional[Dict[str, Any]]) -> str:
        left_key = self._route_context_activity_key(left)
        right_key = self._route_context_activity_key(right)
        if not left_key or not right_key:
            return ""
        return f"{left_key}->{right_key}"

    def _route_context_entry(
        self,
        left: Optional[Dict[str, Any]],
        right: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        context = self._active_route_context()
        if not context:
            return None
        pair_key = self._route_context_pair_key(left, right)
        if not pair_key:
            return None
        return (context.get("pairs") or {}).get(pair_key)

    def _route_context_start_entry(self, item: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        context = self._active_route_context()
        if not context or not item:
            return None
        key = self._route_context_activity_key(item)
        if not key:
            return None
        return (context.get("start_routes") or {}).get(key)

    def _route_aware_timeline_violations(
        self,
        timeline: List[Dict[str, Any]],
        day_start: int,
        day_end: int,
        min_travel: Optional[int],
    ) -> List[Dict[str, Any]]:
        if not self._active_route_context():
            return []
        ordered = sorted(timeline or [], key=lambda item: item.get("scheduled_start") or 0)
        violations: List[Dict[str, Any]] = []

        for index, item in enumerate(ordered):
            start = item.get("scheduled_start")
            end = item.get("scheduled_end")
            if start is None or end is None:
                violations.append({"reason": "missing_schedule_time", "title": item.get("title")})
                continue
            if start < day_start or end > day_end or end <= start:
                violations.append({"reason": "day_boundary", "title": item.get("title")})
            if index > 0:
                previous = ordered[index - 1]
                previous_end = previous.get("scheduled_end") or 0
                if start < previous_end:
                    violations.append({
                        "reason": "overlap",
                        "from": previous.get("title"),
                        "to": item.get("title"),
                    })

        physical_indices = [
            index for index, item in enumerate(ordered)
            if self._activity_requires_travel(item)
        ]
        if physical_indices:
            first_index = physical_indices[0]
            first = ordered[first_index]
            start_entry = self._route_context_start_entry(first)
            first_start = first.get("scheduled_start")
            if start_entry and first_start is not None:
                route_minutes = int(start_entry.get("duration_minutes") or 0)
                leave_by = first_start - route_minutes
                if leave_by < day_start:
                    violations.append({
                        "reason": "start_route_before_day_start",
                        "to": first.get("title"),
                        "leave_by": leave_by,
                    })
                for blocker in ordered[:first_index]:
                    blocker_end = blocker.get("scheduled_end")
                    if blocker_end is not None and blocker_end > leave_by:
                        blocker_movable = (
                            self._activity_can_auto_move_for_route_repair(blocker)
                            if hasattr(self, "_activity_can_auto_move_for_route_repair")
                            else False
                        )
                        destination_movable = (
                            self._activity_can_move_for_route_repair(first)
                            if hasattr(self, "_activity_can_move_for_route_repair")
                            else False
                        )
                        if not blocker_movable and not destination_movable:
                            continue
                        violations.append({
                            "reason": "start_route_blocker",
                            "from": blocker.get("title"),
                            "to": first.get("title"),
                            "blocker_end": blocker_end,
                            "leave_by": leave_by,
                        })

        for left_pos, right_pos in zip(physical_indices, physical_indices[1:]):
            left = ordered[left_pos]
            right = ordered[right_pos]
            entry = self._route_context_entry(left, right)
            if not entry:
                continue
            route_minutes = int(entry.get("duration_minutes") or 0)
            prep = max(
                int(left.get("prep_buffer", DEFAULT_PREP_BUFFER) or 0),
                int(right.get("prep_buffer", DEFAULT_PREP_BUFFER) or 0),
            )
            intermediate_ends = [
                item.get("scheduled_end") or 0
                for item in ordered[left_pos + 1:right_pos]
            ]
            route_gap_start = max([left.get("scheduled_end") or 0] + intermediate_ends)
            right_start = right.get("scheduled_start")
            required_start = route_gap_start + prep + route_minutes
            if right_start is not None and right_start < required_start:
                left_movable = (
                    self._activity_can_move_for_route_repair(left)
                    if hasattr(self, "_activity_can_move_for_route_repair")
                    else False
                )
                right_movable = (
                    self._activity_can_move_for_route_repair(right)
                    if hasattr(self, "_activity_can_move_for_route_repair")
                    else False
                )
                if not left_movable and not right_movable:
                    continue
                violations.append({
                    "reason": "route_transition",
                    "from": left.get("title"),
                    "to": right.get("title"),
                    "required_travel": route_minutes,
                    "required_start": required_start,
                    "actual_start": right_start,
                })
        return violations

    def _activity_requires_travel(self, activity: Optional[Dict[str, Any]]) -> bool:
        if not activity:
            return False
        if self._location_flexible_selected_endpoint_requires_route(activity):
            return True
        if self._activity_has_user_selected_physical_endpoint(activity):
            return True
        status = clean_title(activity.get("location_status") or "")
        category = clean_title(activity.get("location_category") or "")
        raw_travel_required = activity.get("travel_required")
        if raw_travel_required is False:
            return False
        if isinstance(raw_travel_required, str) and raw_travel_required.strip().lower() in {"0", "false", "no", "off"}:
            return False
        if status in {"not_required", "no_location_required"}:
            return False
        if category in {"home_or_online", "none"}:
            return False
        return True

    def _activity_has_user_selected_physical_endpoint(self, activity: Dict[str, Any]) -> bool:
        resolved = activity.get("resolved_location")
        if not isinstance(resolved, dict):
            return False
        if not self._coordinate_from_activity_payload(resolved):
            return False
        label_text = clean_title(
            " ".join(
                str(value or "")
                for value in (
                    activity.get("location_label"),
                    activity.get("location"),
                    resolved.get("label"),
                    resolved.get("display_name"),
                )
            )
        )
        if re.search(r"\b(current location|current place|starting point|start location|default start)\b", label_text):
            return False
        policy = clean_title(activity.get("location_policy") or "").replace(" ", "_").replace("-", "_")
        source = clean_title(activity.get("location_source") or "").replace(" ", "_").replace("-", "_")
        kind = clean_title(activity.get("location_kind") or "").replace(" ", "_").replace("-", "_")
        return bool(
            activity.get("travel_required") is True
            or policy == "exact_location_required"
            or source in {"event_manual_location", "selected_geocode", "event_confirmed", "manual_edit"}
            or kind in {"exact_named_place", "area_only", "unknown_physical"}
            or activity.get("explicit_user_location")
        )

    def _location_flexible_selected_endpoint_requires_route(self, activity: Dict[str, Any]) -> bool:
        if not bool(activity.get("location_flexible") and activity.get("travel_context_required")):
            return False
        if not isinstance(activity.get("resolved_location"), dict):
            return False
        if not self._coordinate_from_activity_payload(activity.get("resolved_location") or {}):
            return False
        label_text = clean_title(
            " ".join(
                str(value or "")
                for value in (
                    activity.get("location_label"),
                    activity.get("location"),
                    (activity.get("resolved_location") or {}).get("label"),
                    (activity.get("resolved_location") or {}).get("display_name"),
                    (activity.get("resolved_location") or {}).get("category"),
                    activity.get("location_category"),
                )
            )
        )
        if re.search(r"\b(home|current location|current place|starting point|start location|default start)\b", label_text):
            return False
        return True

    def _coordinate_from_activity_payload(self, payload: Dict[str, Any]) -> Optional[Tuple[float, float]]:
        try:
            lat = float(payload.get("latitude") if payload.get("latitude") is not None else payload.get("lat"))
            lng = float(payload.get("longitude") if payload.get("longitude") is not None else payload.get("lng"))
        except (TypeError, ValueError):
            return None
        if -90 <= lat <= 90 and -180 <= lng <= 180:
            return (lat, lng)
        return None

    def _materialize_blocks(
        self, 
        activities: List[Dict[str, Any]], 
        day_start: int, 
        min_travel: Optional[int]
    ) -> List[Dict[str, Any]]:
        """[Module B/C] Converts sorted activities into a complete timeline with explicit Transition, Buffer, and Idle blocks."""
        blocks = []
        ordered = sorted(activities, key=lambda item: item.get("scheduled_start") or 0)
        
        last_end = day_start
        last_loc = None
        last_activity = None

        for act in ordered:
            start_min = act.get("scheduled_start") or 0
            end_min = act.get("scheduled_end") or (start_min + act.get("duration_minutes", 60))
            display_loc = act.get("location_label") or act.get("location")
            travel_required = self._activity_requires_travel(act)
            current_loc = (act.get("location") or act.get("location_label")) if travel_required else None
            if not current_loc:
                current_loc = None # Normalize

            # A. Gap Handling (between last_end and start_min)
            if start_min > last_end:
                gap_dur = start_min - last_end
                source_activity_id = (last_activity or {}).get("stable_activity_id") or (last_activity or {}).get("id")
                destination_activity_id = act.get("stable_activity_id") or act.get("id")
                support_links = {
                    "source_activity_id": source_activity_id,
                    "destination_activity_id": destination_activity_id,
                    "related_activity_ids": [
                        activity_id
                        for activity_id in (source_activity_id, destination_activity_id)
                        if activity_id
                    ],
                }
                
                # Calculate required overhead
                route_entry = self._route_context_entry(last_activity, act) if last_activity and current_loc else None
                buffer_dur = max(
                    int((last_activity or {}).get("prep_buffer", DEFAULT_PREP_BUFFER) or 0),
                    int(act.get("prep_buffer", DEFAULT_PREP_BUFFER) or 0),
                ) if last_loc is not None and current_loc is not None else 0
                travel_time = 0
                travel_source = "heuristic"
                route_status = "not_requested"
                route_duration = None
                from_coordinate = None
                to_coordinate = None
                if last_loc and current_loc and last_loc != current_loc:
                    if route_entry:
                        travel_time = int(route_entry.get("duration_minutes") or 0)
                        route_duration = travel_time
                        travel_source = route_entry.get("source") or "routing_service"
                        route_status = "fallback_used" if travel_source == "fallback" else "validated"
                        from_coordinate = route_entry.get("from_coordinate")
                        to_coordinate = route_entry.get("to_coordinate")
                    else:
                        travel_time = estimate_travel_minutes(last_loc, current_loc)
                
                required_total = buffer_dur + travel_time
                
                if gap_dur >= required_total:
                    # Just-in-Time Move Policy:
                    # 1. Idle time at current location
                    idle_at_current = gap_dur - required_total
                    if idle_at_current > 0:
                        blocks.append({
                            "block_type": "idle",
                            "title": "Free Time",
                            "start": format_clock(last_end),
                            "end": format_clock(last_end + idle_at_current),
                            "duration_minutes": idle_at_current,
                            "reason": f"Relax at {last_loc or 'current location'} before departing"
                        })
                    
                    # 2. Buffer block (Prep for departure)
                    if buffer_dur > 0:
                        b_start = last_end + idle_at_current
                        blocks.append({
                            "id": f"buffer-{source_activity_id}-{destination_activity_id}" if source_activity_id and destination_activity_id else None,
                            "block_type": "buffer",
                            "title": "Prep / Buffer",
                            "start": format_clock(b_start),
                            "end": format_clock(b_start + buffer_dur),
                            "duration_minutes": buffer_dur,
                            "reason": "Preparation before travel",
                            **support_links,
                        })
                    
                    # 3. Transition block (Travel)
                    if travel_time > 0:
                        t_start = last_end + idle_at_current + buffer_dur
                        blocks.append({
                            "id": f"travel-{source_activity_id}-{destination_activity_id}" if source_activity_id and destination_activity_id else None,
                            "block_type": "transition",
                            "type": "travel",
                            "title": f"Travel to {current_loc}",
                            "start": format_clock(t_start),
                            "end": format_clock(start_min),
                            "duration_minutes": travel_time,
                            "from_location": last_loc,
                            "to_location": current_loc,
                            "travel_estimate_source": travel_source,
                            "travel_validation_status": route_status,
                            "route_duration_minutes": route_duration,
                            "from_coordinate": deepcopy(from_coordinate) if isinstance(from_coordinate, dict) else from_coordinate,
                            "to_coordinate": deepcopy(to_coordinate) if isinstance(to_coordinate, dict) else to_coordinate,
                            "reason": f"Travel timed to arrive exactly for {act['title']}",
                            **support_links,
                        })
                else:
                    # Not enough time for full buffer + travel? 
                    # Force move immediately and mark as potentially tight
                    blocks.append({
                        "id": f"travel-{source_activity_id}-{destination_activity_id}" if source_activity_id and destination_activity_id else None,
                        "block_type": "transition",
                        "type": "travel",
                        "title": f"Travel to {current_loc} (Tight)",
                        "start": format_clock(last_end),
                        "end": format_clock(start_min),
                        "duration_minutes": gap_dur,
                        "from_location": last_loc,
                        "to_location": current_loc,
                        "travel_estimate_source": travel_source,
                        "travel_validation_status": route_status,
                        "route_duration_minutes": route_duration,
                        "from_coordinate": deepcopy(from_coordinate) if isinstance(from_coordinate, dict) else from_coordinate,
                        "to_coordinate": deepcopy(to_coordinate) if isinstance(to_coordinate, dict) else to_coordinate,
                        "is_tight": True,
                        "warning_code": WARNING_TIGHT_TRANSITION,
                        "reason": "Immediate departure required due to tight schedule",
                        **support_links,
                    })

            # B. Activity Block
            activity_block = {
                "block_type": "activity",
                "id": act.get("id"),
                "stable_activity_id": act.get("stable_activity_id") or act.get("id"),
                "title": act["title"],
                "start": format_clock(start_min),
                "end": format_clock(end_min),
                "startTime": format_clock(start_min),
                "endTime": format_clock(end_min),
                "duration_minutes": end_min - start_min,
                "location": display_loc,
                "location_label": act.get("location_label") or display_loc,
                "location_category": act.get("location_category"),
                "location_status": act.get("location_status"),
                "location_source": act.get("location_source"),
                "location_confidence": act.get("location_confidence"),
                "location_warning": act.get("location_warning"),
                "location_flexible": bool(act.get("location_flexible", False)),
                "can_be_done_at_current_location": bool(act.get("can_be_done_at_current_location", False)),
                "quiet_place_required": bool(act.get("quiet_place_required", False)),
                "activity_role": act.get("activity_role"),
                "travel_context_required": bool(act.get("travel_context_required", False)),
                "semantic_constraint_type": act.get("semantic_constraint_type"),
                "service_kind": act.get("service_kind"),
                "arrive_by": act.get("arrive_by"),
                "area_preference": act.get("area_preference"),
                "travel_required": travel_required,
                "timing_mode": act.get("timing_mode"),
                "original_timing_mode": act.get("original_timing_mode"),
                "fixed_start": act.get("fixed_start"),
                "fixed_end": act.get("fixed_end"),
                "is_user_fixed": bool(act.get("is_user_fixed", False)),
                "is_system_scheduled": bool(act.get("is_system_scheduled", False)),
                "user_fixed_start": act.get("user_fixed_start"),
                "can_move_for_repair": bool(act.get("can_move_for_repair", False)),
                "repair_protection": act.get("repair_protection"),
                "priority": act.get("priority"),
                "is_mandatory": bool(act.get("is_mandatory", True)),
                "saved_location_label": act.get("saved_location_label"),
                "resolved_location": deepcopy(act.get("resolved_location")) if isinstance(act.get("resolved_location"), dict) else None,
                "notes": act.get("notes"),
                "preferred_time_window": act.get("preferred_time_window"),
                "preferred_window_start": act.get("preferred_window_start"),
                "preferred_window_end": act.get("preferred_window_end"),
                "preferred_order": deepcopy(act.get("preferred_order")) if isinstance(act.get("preferred_order"), dict) else None,
                "preferred_orders": deepcopy(act.get("preferred_orders")) if isinstance(act.get("preferred_orders"), list) else [],
                "anchor_relation": deepcopy(act.get("anchor_relation")) if isinstance(act.get("anchor_relation"), dict) else None,
                "relation_type": (act.get("anchor_relation") or {}).get("kind") if isinstance(act.get("anchor_relation"), dict) else None,
                "anchor_activity_id": (act.get("anchor_relation") or {}).get("target_activity_id") if isinstance(act.get("anchor_relation"), dict) else None,
                "anchor_title": (act.get("anchor_relation") or {}).get("target_title") if isinstance(act.get("anchor_relation"), dict) else None,
                "relative_offset_minutes": act.get("relative_offset_minutes") or act.get("offset_minutes") or 0,
                "placement_source": "system_derived" if act.get("anchor_relation") and not act.get("is_user_fixed") else act.get("placement_source"),
                "is_derived_time": bool(act.get("anchor_relation") and not act.get("is_user_fixed")),
                "implicit_activity": bool(act.get("implicit_activity", False)),
                "implicit_reason": act.get("implicit_reason"),
                "is_conflict": act.get("is_conflict", False),
                "is_conflicting": act.get("is_conflict", False),
                "conflict_ids": list(act.get("conflict_ids") or []),
                "reason": "Scheduled activity"
            }
            copy_preference_metadata(act, activity_block, overwrite=False)
            blocks.append(activity_block)
            
            last_end = end_min
            if travel_required:
                last_loc = current_loc
                last_activity = act

        return blocks

    def _find_final_activity_block(
        self,
        schedule_blocks: List[Dict[str, Any]],
        activity: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        activity_id = activity.get("stable_activity_id") or activity.get("id")
        activity_title = clean_title(activity.get("title") or "")
        for block in schedule_blocks:
            if block.get("block_type") != "activity":
                continue
            if activity_id and str(block.get("stable_activity_id") or block.get("id")) == str(activity_id):
                return block
            if activity_title and clean_title(block.get("title") or "") == activity_title:
                return block
        return None

    def _collect_schedule_warnings(
        self,
        activities: List[Dict[str, Any]],
        schedule_blocks: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        warnings: List[Dict[str, Any]] = []
        for activity in activities:
            if not activity.get("accepted_with_warning") and not activity.get("warning_code"):
                continue
            block = self._find_final_activity_block(schedule_blocks, activity)
            block_index = schedule_blocks.index(block) if block in schedule_blocks else -1
            tight_transition = None
            if block_index >= 0 and block_index + 1 < len(schedule_blocks):
                next_block = schedule_blocks[block_index + 1]
                if next_block.get("block_type") == "transition" and next_block.get("is_tight"):
                    tight_transition = next_block

            base_warning = (activity.get("warnings") or [{}])[0]
            warning = {
                "warning_code": activity.get("warning_code") or base_warning.get("warning_code") or WARNING_TIGHT_TRANSITION,
                "activity_id": activity.get("stable_activity_id") or activity.get("id"),
                "activity_title": activity.get("title"),
                "anchor_title": base_warning.get("anchor_title"),
                "start": (block or {}).get("start") or base_warning.get("start"),
                "end": (block or {}).get("end") or base_warning.get("end"),
                "explanation": base_warning.get("explanation") or f"{activity.get('title')} was accepted with a scheduling warning.",
            }
            if tight_transition:
                warning["transition"] = {
                    "title": tight_transition.get("title"),
                    "start": tight_transition.get("start"),
                    "end": tight_transition.get("end"),
                    "from_location": tight_transition.get("from_location"),
                    "to_location": tight_transition.get("to_location"),
                }
            warnings.append(warning)
        return warnings

    def _resolve_user_reply(self, parsed: Dict[str, Any], latest_request: str) -> str:
        llm_reply = str(parsed.get("_llm_reply") or "").strip()
        parsed_reply = str(parsed.get("reply") or "").strip()
        reply_source = parsed.get("_reply_source")
        request_text = clean_title(latest_request)

        if reply_source == "llm" and parsed_reply:
            return parsed_reply

        if llm_reply and clean_title(llm_reply) != request_text:
            return llm_reply

        if parsed_reply and clean_title(parsed_reply) != request_text:
            return parsed_reply

        if parsed.get("intent") == "chat":
            return "I'm here. Tell me what you want to plan or change."

        return "I updated the draft based on your request."

    def _resolve_schedule_date(
        self,
        parsed: Dict[str, Any],
        current_schedule: Optional[Dict[str, Any]],
        latest_request: str,
    ) -> str:
        selected_date = (current_schedule or {}).get("date")
        parsed_date = parsed.get("date")

        if selected_date and not self._user_explicitly_mentions_date(latest_request):
            self._debug(
                f"Date resolution | using selected/current date {selected_date} because request did not explicitly mention a date"
            )
            return selected_date

        resolved = parsed_date or selected_date or self._local_today_iso()
        self._debug(
            f"Date resolution | selected_date={selected_date} | parsed_date={parsed_date} | resolved={resolved}"
        )
        return resolved

    def _user_explicitly_mentions_date(self, request_text: str) -> bool:
        text = (request_text or "").strip().lower()
        if not text:
            return False

        explicit_patterns = [
            r"\b(today|tomorrow|yesterday|tonight)\b",
            r"\bnext\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday|week|month)\b",
            r"\bthis\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekend|week)\b",
            r"\bon\s+\d{4}-\d{2}-\d{2}\b",
            r"\b\d{4}-\d{2}-\d{2}\b",
            r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b",
            r"\b\d{1,2}(st|nd|rd|th)?\s+(of\s+)?"
            r"(january|february|march|april|may|june|july|august|september|october|november|december)\b",
            r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}(st|nd|rd|th)?\b",
        ]
        return any(re.search(pattern, text) for pattern in explicit_patterns)

    def _plan_schedule(
        self,
        schedule_date: str,
        activities: List[Dict[str, Any]],
        preferences: Dict[str, Any],
    ) -> Dict[str, Any]:
        module_c_started = time.perf_counter()
        self._normalize_day_boundary_preferences(preferences)
        day_start = parse_clock(preferences.get("day_start")) or DEFAULT_DAY_START
        day_end = parse_clock(preferences.get("day_end")) or DEFAULT_DAY_END
        min_travel = preferences.get("min_travel_buffer_minutes") or 0
        route_context = preferences.get("_route_context") if preferences.get("route_aware_repair") else None
        self._current_route_context = route_context if isinstance(route_context, dict) else None
        self._current_low_fatigue_preference = bool(preferences.get("low_fatigue_preference"))
        if self._active_route_context():
            jlog("MODULE_C", "enabled=true", "ROUTE_AWARE")
            for pair in (self._active_route_context().get("pairs") or {}).values():
                jlog_verbose(
                    "MODULE_C",
                    (
                        f"from={pair.get('from_title')} to={pair.get('to_title')} "
                        f"required_travel={pair.get('duration_minutes')}"
                    ),
                    "ROUTE_CONSTRAINT",
                )

        # Pass 1: Categorize
        fixed: List[Dict[str, Any]] = []
        deadline: List[Dict[str, Any]] = []
        preserved: List[Dict[str, Any]] = []
        relative: List[Dict[str, Any]] = []
        flexible: List[Dict[str, Any]] = []
        unscheduled: List[Dict[str, Any]] = []

        jlog("MODULE_C", f"Categorizing {len(activities)} items", "PASS_1")
        for item in deepcopy(activities):
            # Support both snake_case and camelCase
            mode = item.get("timing_mode") or item.get("timingMode") or TimingMode.UNSPECIFIED
            semantic_type = clean_title(item.get("semantic_constraint_type") or "")
            service_kind = clean_title(item.get("service_kind") or "")
            item.setdefault("trace", [])
            if semantic_type in {"pickup", "exact_anchor"} or service_kind == "pickup":
                fixed_start = item.get("fixed_start")
                if fixed_start is None:
                    fixed_start = item.get("fixedStart")
                parsed_fixed_start = parse_clock(fixed_start)
                if parsed_fixed_start is not None:
                    duration = int(item.get("duration_minutes") or item.get("durationMinutes") or 60)
                    item["timing_mode"] = TimingMode.FIXED
                    item["fixed_start"] = parsed_fixed_start
                    item["fixed_end"] = parsed_fixed_start + duration
                    item["is_user_fixed"] = True
                    item["can_move_for_repair"] = False
                    item["repair_protection"] = item.get("repair_protection") or "fixed"
                    mode = TimingMode.FIXED
            if semantic_type in {"arrive_by", "dropoff", "deadline"} or service_kind == "dropoff":
                latest_end = parse_clock(item.get("latest_end") if item.get("latest_end") is not None else item.get("arrive_by"))
                if latest_end is not None:
                    item["timing_mode"] = TimingMode.WINDOW
                    item["latest_end"] = latest_end
                    item["preferred_end"] = parse_clock(item.get("preferred_end")) or latest_end
                    item["is_user_fixed"] = False
                    item["can_move_for_repair"] = True
                    item["repair_protection"] = item.get("repair_protection") or "flexible"
                    deadline.append(item)
                    jlog(
                        "MODULE_C",
                        f"'{item['title']}' -> DEADLINE ({format_clock(latest_end)})",
                        "CLASSIFY",
                    )
                    continue
            
            has_preserved_time = (
                item.get("preserve_scheduled_time")
                and item.get("scheduled_start") is not None
                and item.get("scheduled_end") is not None
            )

            if mode == TimingMode.FIXED:
                fs = item.get("fixed_start")
                if fs is None:
                    fs = item.get("fixedStart")
                fs = parse_clock(fs)
                if fs is not None:
                    dur = int(item.get("duration_minutes") or item.get("durationMinutes") or 60)
                    fixed_end = parse_clock(item.get("fixed_end") if item.get("fixed_end") is not None else item.get("fixedEnd"))
                    if fixed_end is not None and fixed_end > fs:
                        dur = fixed_end - fs
                        item["fixed_end"] = fixed_end
                        item["duration_minutes"] = dur
                    item["scheduled_start"] = fs
                    item["scheduled_end"] = fs + dur
                    fixed.append(item)
                    jlog("MODULE_C", f"'{item['title']}' -> FIXED ({format_clock(fs)})", "CLASSIFY")
                else:
                    flexible.append(item)
                    jlog("MODULE_C", f"'{item['title']}' -> FLEX (missing fixed_start)", "CLASSIFY")
            elif has_preserved_time:
                preserved.append(item)
                jlog(
                    "MODULE_C",
                    f"'{item['title']}' -> PRESERVE ({format_clock(item['scheduled_start'])}-{format_clock(item['scheduled_end'])})",
                    "CLASSIFY",
                )
            elif mode == TimingMode.RELATIVE:
                relative.append(item)
                jlog("MODULE_C", f"'{item['title']}' -> RELATIVE", "CLASSIFY")
            else:
                flexible.append(item)
                jlog("MODULE_C", f"'{item['title']}' -> FLEX (mode={mode})", "CLASSIFY")

        jsection("MODULE_C", "feasibility-first construction", "CONSTRUCT")
        
        timeline: List[Dict[str, Any]] = []

        # 1. Place FIXED items first
        fixed.sort(key=lambda x: x["scheduled_start"])
        for item in fixed:
            feasible, reason = self._validate_locked_item(item, timeline, day_start, day_end, min_travel)
            if feasible:
                jlog("MODULE_C", f"'{item['title']}' -> {format_clock(item['scheduled_start'])}", "LOCKING")
                item["trace"].append("Locked as a fixed commitment.")
                timeline.append(item)
                timeline.sort(key=lambda x: x["scheduled_start"])
            else:
                jlog("MODULE_C", f"'{item['title']}' reason={reason}", "CLASH")
                timeline.append(self._create_conflict_item(item, timeline, reason))
                timeline.sort(key=lambda x: x["scheduled_start"])

        # 1b. Place deadline-style anchors close to their requested completion
        # time before ordinary flexible work competes for the remaining gaps.
        deadline.sort(key=lambda x: x.get("latest_end") or x.get("arrive_by") or day_end)
        for item in deadline:
            inserted, reason = self._insert_best_position(
                item,
                timeline,
                day_start,
                day_end,
                min_travel,
                prefer_latest=True,
            )
            if inserted:
                timeline = inserted
                jlog(
                    "MODULE_C",
                    f"'{item['title']}' -> before {format_clock(int(item.get('latest_end') or day_end))}",
                    "PLACE_DEADLINE",
                )
            else:
                if item.get("is_mandatory", item.get("isMandatory", True)):
                    timeline = self._insert_as_conflict(item, timeline, day_start, reason) or timeline
                else:
                    unscheduled.append(item)

        # 2. Preserve existing scheduled flexible/preferred activities before
        # inserting new relative requests. This prevents a small edit from
        # re-optimizing the whole day and invalidating anchor order.
        preserved.sort(key=lambda x: x["scheduled_start"])
        for item in preserved:
            feasible, reason = self._validate_locked_item(item, timeline, day_start, day_end, min_travel)
            if feasible:
                item.setdefault("trace", []).append("Preserved existing scheduled time during replanning.")
                jlog(
                    "MODULE_C",
                    f"Preserving {item.get('title')} at {format_clock(item.get('scheduled_start'))}-{format_clock(item.get('scheduled_end'))}",
                    "ANCHOR",
                )
                timeline.append(item)
                timeline.sort(key=lambda x: x["scheduled_start"])
            else:
                jlog("MODULE_C", f"'{item['title']}' preserve failed reason={reason}", "CLASH")
                timeline.append(self._create_conflict_item(item, timeline, reason))
                timeline.sort(key=lambda x: x["scheduled_start"])

        # 3. Place RELATIVE items. A dependent waits until its anchor has been
        # placed; if the anchor is still flexible, place the anchor first.
        pending_relative = sorted(relative, key=lambda x: x.get("sequence_index") or 999)

        while pending_relative:
            progress = False
            for item in list(pending_relative):
                anchor = item.get("anchor_relation")
                if anchor:
                    target_title = anchor.get("target_title")
                    jlog("MODULE_C", f"{item.get('title')} depends on {target_title or anchor.get('target_activity_id')}", "DEPENDENCY")

                anchor_item = self._find_anchor_in_timeline(item.get("anchor_relation"), timeline)
                if not anchor_item:
                    placed_anchor = self._place_flexible_anchor_for_relative(
                        item,
                        flexible,
                        timeline,
                        day_start,
                        day_end,
                        min_travel,
                    )
                    if placed_anchor:
                        timeline, flexible = placed_anchor
                        progress = True
                    continue

                new_timeline, handled = self._place_relative_item(
                    item,
                    anchor_item,
                    timeline,
                    day_start,
                    day_end,
                    min_travel,
                )
                timeline = new_timeline
                pending_relative.remove(item)
                progress = True

            if not progress:
                for item in pending_relative:
                    flexible.append(item)
                pending_relative = []

        # 4. Place FLEXIBLE items
        self._mark_soft_anchor_boosts(flexible)
        flexible.sort(key=self._calculate_activity_base_score, reverse=True)
        for item in flexible:
            jlog("MODULE_C", f"'{item['title']}' score={self._calculate_activity_base_score(item)}", "PLACE_FLEX")
            inserted, reason = self._insert_best_position(item, timeline, day_start, day_end, min_travel)
            if inserted:
                timeline = inserted
            else:
                if self._should_unschedule_initial_generation_item(item, preferences):
                    unscheduled.append(self._mark_unscheduled_optional(item, reason or "no_relaxed_slot"))
                elif item.get("is_mandatory"):
                    conflict_timeline = self._insert_as_conflict(item, timeline, day_start, reason)
                    timeline = conflict_timeline or timeline
                else:
                    unscheduled.append(item)

        timeline, unscheduled, refinement_metadata = self._apply_module_d_refinement(
            timeline,
            unscheduled,
            day_start,
            day_end,
            min_travel,
            preferences,
        )

        # FINAL PASS: Materialize
        final_blocks = self._materialize_blocks(timeline, day_start, min_travel)

        result = {
            "activities": timeline,
            "schedule_blocks": final_blocks,
            "unscheduled_activities": unscheduled,
            "day_start": format_clock(day_start),
            "day_end": format_clock(day_end),
            **refinement_metadata,
        }
        jlog("TIMER", f"module_c_greedy_seconds={time.perf_counter() - module_c_started:.2f}", None)
        return result

    def _normalize_day_boundary_preferences(self, preferences: Dict[str, Any]) -> None:
        """Map UI preference names into the fields Module C already consumes."""
        if not isinstance(preferences, dict):
            return
        if not preferences.get("day_start") and preferences.get("day_start_time"):
            preferences["day_start"] = preferences.get("day_start_time")
        if not preferences.get("day_end") and preferences.get("day_end_time"):
            preferences["day_end"] = preferences.get("day_end_time")

    def _should_unschedule_initial_generation_item(
        self,
        item: Dict[str, Any],
        preferences: Dict[str, Any],
    ) -> bool:
        if preferences.get("refinement_reason") != "initial_generation":
            return False
        if item.get("timing_mode") == TimingMode.FIXED or item.get("fixed_start") is not None:
            return False
        priority = clean_title(item.get("priority") or "medium")
        return (
            priority == "low"
            or not item.get("is_mandatory", True)
            or bool(item.get("soft_dependency"))
            or bool(item.get("preferred_time_window"))
        )

    def _mark_unscheduled_optional(
        self,
        item: Dict[str, Any],
        reason: str,
    ) -> Dict[str, Any]:
        unscheduled_item = deepcopy(item)
        unscheduled_item["status"] = "unscheduled"
        unscheduled_item["unscheduled_reason"] = reason or "no_relaxed_slot"
        unscheduled_item.setdefault("trace", []).append(
            reason or "No relaxed slot was available during initial generation."
        )
        jlog(
            "MODULE_C",
            f"{unscheduled_item.get('title')} reason={unscheduled_item['unscheduled_reason']}",
            "UNSCHEDULED_OPTIONAL",
        )
        return unscheduled_item

    def _find_anchor_in_timeline(
        self,
        anchor: Optional[Dict[str, Any]],
        timeline: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if not anchor:
            return None
        target_id = anchor.get("target_activity_id") or anchor.get("target_id")
        target_title = anchor.get("target_title")
        return next(
            (
                activity for activity in timeline
                if (
                    target_id and activity.get("stable_activity_id") == target_id
                ) or (
                    target_title and clean_title(activity.get("title")) == clean_title(target_title)
                )
            ),
            None,
        )

    def _pop_matching_flexible_anchor(
        self,
        anchor: Optional[Dict[str, Any]],
        flexible: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if not anchor:
            return None
        target_id = anchor.get("target_activity_id") or anchor.get("target_id")
        target_title = anchor.get("target_title")
        for index, candidate in enumerate(flexible):
            if (
                target_id and candidate.get("stable_activity_id") == target_id
            ) or (
                target_title and clean_title(candidate.get("title")) == clean_title(target_title)
            ):
                return flexible.pop(index)
        return None

    def _place_flexible_anchor_for_relative(
        self,
        dependent: Dict[str, Any],
        flexible: List[Dict[str, Any]],
        timeline: List[Dict[str, Any]],
        day_start: int,
        day_end: int,
        min_travel: Optional[int],
    ) -> Optional[Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]]:
        anchor = dependent.get("anchor_relation")
        anchor_item = self._pop_matching_flexible_anchor(anchor, flexible)
        if not anchor_item:
            return None

        inserted, reason = self._insert_best_position(anchor_item, timeline, day_start, day_end, min_travel)
        if not inserted:
            flexible.append(anchor_item)
            jlog(
                "MODULE_C",
                f"Could not place anchor {anchor_item.get('title')} before {dependent.get('title')}: {reason}",
                "ANCHOR",
            )
            return None

        placed_anchor = self._find_anchor_in_timeline(
            {"target_activity_id": anchor_item.get("stable_activity_id"), "target_title": anchor_item.get("title")},
            inserted,
        )
        if placed_anchor:
            jlog(
                "MODULE_C",
                f"Placed anchor {placed_anchor.get('title')} at {format_clock(placed_anchor.get('scheduled_start'))}-{format_clock(placed_anchor.get('scheduled_end'))}",
                "ANCHOR",
            )
        return inserted, flexible

    def _place_relative_item(
        self,
        item: Dict[str, Any],
        anchor_item: Dict[str, Any],
        timeline: List[Dict[str, Any]],
        day_start: int,
        day_end: int,
        min_travel: Optional[int],
    ) -> Tuple[List[Dict[str, Any]], bool]:
        anchor = item.get("anchor_relation")
        if not anchor:
            return timeline, False

        relation_kind = clean_title(anchor.get("kind") or "after")
        target_title = anchor.get("target_title") or anchor_item.get("title")
        if relation_kind == "before":
            search_end = anchor_item["scheduled_start"]
            predecessors = [a for a in timeline if a["scheduled_end"] <= search_end and a.get("id") != anchor_item.get("id")]
            search_start = max([day_start] + [a["scheduled_end"] for a in predecessors])
            jlog("MODULE_C", f"'{item['title']}' before '{target_title}' search={format_clock(search_start)}->{format_clock(search_end)}", "PLACE_RELATIVE")
            inserted, reason = self._insert_best_position(
                item, timeline, search_start, search_end, min_travel,
                prefer_latest=True,
                validation_day_start=day_start,
                validation_day_end=day_end,
            )
        else:
            search_start = anchor_item["scheduled_end"]
            search_end = day_end

            successors = sorted(
                [a for a in timeline if a["scheduled_start"] >= search_start],
                key=lambda a: a["scheduled_start"],
            )
            if successors:
                search_end = min(a["scheduled_start"] for a in successors)

            jlog("MODULE_C", f"'{item['title']}' after '{target_title}' search={format_clock(search_start)}->{format_clock(search_end)}", "PLACE_RELATIVE")
            inserted, reason = self._insert_best_position(
                item, timeline, search_start, search_end, min_travel,
                prefer_earliest=True,
                validation_day_start=day_start,
                validation_day_end=day_end,
            )

        if inserted:
            timeline = inserted
            placed_item = self._find_anchor_in_timeline(
                {"target_activity_id": item.get("stable_activity_id"), "target_title": item.get("title")},
                timeline,
            )
            if placed_item:
                jlog(
                    "MODULE_C",
                    f"{item.get('title')} after {target_title} -> {format_clock(placed_item.get('scheduled_start'))}-{format_clock(placed_item.get('scheduled_end'))}",
                    "PLACE_RELATIVE",
                )
                placed_item.setdefault("trace", []).append(f"Placed near anchor '{target_title}' to respect sequence.")
        else:
            tight_inserted = None
            if not self._active_route_context():
                tight_inserted = self._insert_tight_relative_position(
                    item,
                    timeline,
                    search_start,
                    search_end,
                    relation_kind,
                    target_title,
                )
            if tight_inserted:
                jlog("MODULE_C", f"'{item['title']}' fits activity window but transition is tight", "ACCEPTED_TIGHT_TRANSITION")
                timeline = tight_inserted
            else:
                extended_inserted = None
                if relation_kind == "after":
                    extended_inserted = self._try_extended_after_window(
                        item,
                        timeline,
                        anchor_item,
                        successors,
                        day_start,
                        day_end,
                        min_travel,
                        target_title,
                    )
                if extended_inserted:
                    timeline = extended_inserted
                else:
                    jlog("MODULE_C", f"'{item['title']}' no feasible slot in narrative window", "REJECT_REL")
                    conflict_timeline = self._insert_as_conflict(
                        item,
                        timeline,
                        search_start if relation_kind == "before" else search_start,
                        reason or "No space in narrative window.",
                    )
                    timeline = conflict_timeline or timeline

        return timeline, True

    def _try_extended_after_window(
        self,
        item: Dict[str, Any],
        timeline: List[Dict[str, Any]],
        anchor_item: Dict[str, Any],
        successors: List[Dict[str, Any]],
        day_start: int,
        day_end: int,
        min_travel: Optional[int],
        target_title: Optional[str],
    ) -> Optional[List[Dict[str, Any]]]:
        if not successors:
            return None
        next_item = successors[0]
        if self._has_explicit_dependency(next_item, anchor_item):
            return None

        search_start = max(anchor_item["scheduled_end"], next_item.get("scheduled_end") or anchor_item["scheduled_end"])
        later_successors = [
            candidate for candidate in timeline
            if candidate.get("scheduled_start") is not None and candidate.get("scheduled_start") >= search_start
        ]
        search_end = min([day_end] + [candidate["scheduled_start"] for candidate in later_successors])
        jlog(
            "MODULE_C",
            (
                f"{item.get('title')} after {target_title or anchor_item.get('title')} "
                "immediate_window_failed=true trying_after_next_activity=true"
            ),
            "RELATIVE_WINDOW",
        )
        inserted, _ = self._insert_best_position(
            item,
            timeline,
            search_start,
            search_end,
            min_travel,
            prefer_earliest=True,
            validation_day_start=day_start,
            validation_day_end=day_end,
        )
        if not inserted:
            return None
        placed_item = self._find_anchor_in_timeline(
            {"target_activity_id": item.get("stable_activity_id"), "target_title": item.get("title")},
            inserted,
        )
        if placed_item:
            placed_item.setdefault("trace", []).append(
                f"Immediate window after '{target_title}' was full, so it was placed later while still after the anchor."
            )
            placed_item["relative_window_extended"] = True
            placed_item["relative_window_alternative_after"] = next_item.get("title")
        return inserted

    def _has_explicit_dependency(self, dependent: Dict[str, Any], anchor: Dict[str, Any]) -> bool:
        relation = dependent.get("anchor_relation") or {}
        if clean_title(relation.get("kind") or "") != "after":
            return False
        target_id = relation.get("target_activity_id") or relation.get("target_id")
        target_title = relation.get("target_title")
        anchor_id = anchor.get("stable_activity_id") or anchor.get("id")
        return bool(
            (target_id and str(target_id) == str(anchor_id))
            or (target_title and clean_title(target_title) == clean_title(anchor.get("title")))
        )

    def _insert_tight_relative_position(
        self,
        item: Dict[str, Any],
        timeline: List[Dict[str, Any]],
        search_start: int,
        search_end: int,
        relation_kind: str,
        target_title: Optional[str],
    ) -> Optional[List[Dict[str, Any]]]:
        duration = int(item.get("duration_minutes") or 60)
        if search_end - search_start < duration:
            return None

        if relation_kind == "before":
            candidate_start = search_end - duration
        else:
            candidate_start = search_start
        candidate_end = candidate_start + duration

        if candidate_start < search_start or candidate_end > search_end:
            return None

        candidate = deepcopy(item)
        candidate["scheduled_start"] = candidate_start
        candidate["scheduled_end"] = candidate_end
        if self._find_overlaps(candidate, timeline):
            return None

        next_item = next(
            (entry for entry in sorted(timeline, key=lambda value: value.get("scheduled_start") or 0)
             if (entry.get("scheduled_start") or 0) >= candidate_end),
            None,
        )
        warning = {
            "warning_code": WARNING_TIGHT_TRANSITION,
            "activity_id": candidate.get("stable_activity_id") or candidate.get("id"),
            "activity_title": candidate.get("title"),
            "anchor_title": target_title,
            "start": format_clock(candidate_start),
            "end": format_clock(candidate_end),
            "explanation": (
                f"{candidate.get('title')} was added, but the transition"
                f"{f' to {next_item.get('title')}' if next_item else ''} is tight."
            ),
        }
        candidate["accepted_with_warning"] = True
        candidate["warning_code"] = WARNING_TIGHT_TRANSITION
        candidate.setdefault("warnings", [])
        candidate["warnings"].append(warning)
        candidate.setdefault("trace", [])
        candidate["trace"].append(warning["explanation"])
        return sorted(timeline + [candidate], key=lambda entry: entry["scheduled_start"])

    def _insert_as_conflict(
        self,
        item: Dict[str, Any],
        timeline: List[Dict[str, Any]],
        day_start: int,
        reason: Optional[str],
    ) -> Optional[List[Dict[str, Any]]]:
        base_start = (
            item.get("fixed_start")
            or item.get("earliest_start")
            or item.get("scheduled_start")
            or day_start
        )
        candidate = deepcopy(item)
        candidate["scheduled_start"] = base_start
        candidate["scheduled_end"] = base_start + candidate["duration_minutes"]
        conflict_item = self._create_conflict_item(
            candidate,
            timeline,
            reason or "Added activity but kept clash because an existing block already occupies that time.",
        )
        return sorted(timeline + [conflict_item], key=lambda entry: entry["scheduled_start"])

    def _create_conflict_item(
        self,
        item: Dict[str, Any],
        timeline: List[Dict[str, Any]],
        reason: str,
    ) -> Dict[str, Any]:
        conflict_item = deepcopy(item)
        overlapping = self._find_overlaps(conflict_item, timeline)
        conflict_ids = [entry["id"] for entry in overlapping]
        highest_priority = self._highest_priority(overlapping + [conflict_item])

        conflict_item["is_conflict"] = True
        conflict_item["conflict_with"] = list(dict.fromkeys(conflict_ids))
        conflict_item["conflict_reason"] = reason
        conflict_item["reason_codes"] = [self._reason_code_from_text(reason)]
        conflict_item["conflict_priority"] = conflict_item.get("priority", "medium")
        conflict_item["conflict_severity"] = self._conflict_severity(conflict_item, overlapping)
        conflict_item.setdefault("trace", [])
        if reason not in conflict_item["trace"]:
            conflict_item["trace"].append(reason)
        manual_resolution_trace = (
            f"Both blocks were retained for manual resolution; highest priority in the clash is {highest_priority}."
        )
        if manual_resolution_trace not in conflict_item["trace"]:
            conflict_item["trace"].append(manual_resolution_trace)

        for existing in overlapping:
            existing["is_conflict"] = True
            existing.setdefault("conflict_with", [])
            if conflict_item["id"] not in existing["conflict_with"]:
                existing["conflict_with"].append(conflict_item["id"])
            existing["conflict_with"] = [
                clash_id for clash_id in dict.fromkeys(existing["conflict_with"])
                if clash_id != existing.get("id")
            ]
            existing["conflict_reason"] = existing.get("conflict_reason") or "This activity overlaps with another retained block."
            existing["reason_codes"] = list(dict.fromkeys((existing.get("reason_codes") or []) + ["fixed_overlap"]))
            existing["conflict_priority"] = existing.get("priority", "medium")
            existing["conflict_severity"] = self._conflict_severity(existing, [conflict_item])
            existing.setdefault("trace", [])
            conflict_trace = f"Clash preserved with {conflict_item['title']} for manual resolution."
            if conflict_trace not in existing["trace"]:
                existing["trace"].append(conflict_trace)

        self._debug(
            f"Conflict created | title={conflict_item.get('title')} | overlaps={conflict_item.get('conflict_with')} | priority={highest_priority} | severity={conflict_item.get('conflict_severity')}"
        )

        return conflict_item

    def _calculate_activity_base_score(self, activity: Dict[str, Any]) -> int:
        """Heuristic score calculation for placement priority."""
        prio_map = {"high": 50, "medium": 20, "low": 5}
        score = prio_map.get(activity.get("priority", "medium").lower(), 20)
        
        if activity.get("is_mandatory") or activity.get("isMandatory"):
            score += 100
            
        if activity.get("latest_end") is not None:
            score += max(0, (DEFAULT_DAY_END - activity["latest_end"]) // 30)

        if activity.get("preferred_time_window") or activity.get("preferred_window_start") is not None:
            score += 35

        if activity.get("implicit_activity"):
            score += 90

        if activity.get("_soft_anchor_boost"):
            score += 90

        if getattr(self, "_current_low_fatigue_preference", False):
            if self._activity_role(activity) == "recovery":
                score += 45
            if activity.get("travel_required"):
                score += 10
            
        return score

    def _activity_role(self, activity: Optional[Dict[str, Any]]) -> str:
        if not activity:
            return ""
        return clean_title(activity.get("activity_role") or "")

    def _activity_is_demanding_for_recovery(self, activity: Optional[Dict[str, Any]]) -> bool:
        if not activity:
            return False
        if activity.get("travel_required"):
            return True
        if activity.get("timing_mode") == TimingMode.FIXED or activity.get("fixed_start") is not None:
            return True
        return int(activity.get("duration_minutes") or 0) >= 45

    def _semantic_candidate_score_adjustment(
        self,
        item: Dict[str, Any],
        timeline: List[Dict[str, Any]],
        insertion_index: int,
        candidate_start: int,
        candidate_end: int,
    ) -> float:
        if self._activity_role(item) != "recovery":
            return 0.0
        before = timeline[:insertion_index]
        after = timeline[insertion_index:]
        demanding_before = [entry for entry in before if self._activity_is_demanding_for_recovery(entry)]
        demanding_after = [entry for entry in after if self._activity_is_demanding_for_recovery(entry)]
        adjustment = 0.0
        if not before:
            adjustment -= 500.0
        if demanding_before:
            adjustment += min(220.0, len(demanding_before) * 55.0)
            previous = before[-1]
            if self._activity_is_demanding_for_recovery(previous):
                adjustment += 70.0
        if demanding_after:
            next_demanding = demanding_after[0]
            gap_to_next = (next_demanding.get("scheduled_start") or candidate_end) - candidate_end
            if 0 <= gap_to_next <= 120:
                adjustment += 35.0
        if getattr(self, "_current_low_fatigue_preference", False) and demanding_before:
            adjustment += 60.0
        return adjustment

    def _route_candidate_practicality_adjustment(
        self,
        item: Dict[str, Any],
        previous_real_item: Optional[Dict[str, Any]],
        next_real_item: Optional[Dict[str, Any]],
    ) -> float:
        """Penalize obvious route zigzags using the already-built route context."""
        if not self._active_route_context() or not self._activity_requires_travel(item):
            return 0.0
        if not previous_real_item or not next_real_item:
            return 0.0
        if not self._activity_requires_travel(previous_real_item) or not self._activity_requires_travel(next_real_item):
            return 0.0
        if self._route_context_activity_key(item) in {
            self._route_context_activity_key(previous_real_item),
            self._route_context_activity_key(next_real_item),
        }:
            return 0.0
        direct = self._transition_minutes(previous_real_item, next_real_item, 0)
        via = self._transition_minutes(previous_real_item, item, 0) + self._transition_minutes(item, next_real_item, 0)
        route_delta = max(0, via - direct)
        if route_delta <= 10:
            return 0.0
        previous_fixed = previous_real_item.get("timing_mode") == TimingMode.FIXED or previous_real_item.get("fixed_start") is not None
        next_fixed = next_real_item.get("timing_mode") == TimingMode.FIXED or next_real_item.get("fixed_start") is not None
        if previous_fixed or next_fixed:
            penalty = route_delta * 18.0
            if route_delta >= 30:
                jlog(
                    "ROUTE_GUARD",
                    (
                        f"title={item.get('title')} reason=route_zigzag_between_fixed_anchors "
                        f"route_delta={route_delta}"
                    ),
                    "REJECT_GLOBAL_RESCUE",
                )
            else:
                jlog_verbose(
                    "ROUTE_GUARD",
                    f"title={item.get('title')} route_delta={route_delta} penalty={round(penalty, 2)}",
                    "SCORE",
                )
            return -penalty
        return -(route_delta * 4.0)

    def _short_low_weight_flex(self, item: Dict[str, Any]) -> bool:
        return is_short_low_weight_flex_item(item)

    def _optional_route_cost_adjustment(
        self,
        item: Dict[str, Any],
        previous_real_item: Optional[Dict[str, Any]],
        next_real_item: Optional[Dict[str, Any]],
    ) -> float:
        if not self._active_route_context() or not self._short_low_weight_flex(item):
            return 0.0
        duration = max(1, int(item.get("duration_minutes") or DEFAULT_DURATION))
        inbound = self._transition_minutes(previous_real_item, item, 0) if previous_real_item else 0
        outbound = self._transition_minutes(item, next_real_item, 0) if next_real_item else 0
        direct = self._transition_minutes(previous_real_item, next_real_item, 0) if previous_real_item and next_real_item else 0
        detour = max(0, inbound + outbound - direct)
        jlog(
            "OPTIONAL_ROUTE_GUARD",
            (
                f"title={item.get('title')} prev={(previous_real_item or {}).get('title')} "
                f"next={(next_real_item or {}).get('title')} prev_next={direct} "
                f"prev_item={inbound} item_next={outbound} detour={detour}"
            ),
            "EVALUATE",
        )
        near_anchor = False
        for neighbor in (previous_real_item, next_real_item):
            entry = self._route_context_entry(neighbor, item) or self._route_context_entry(item, neighbor)
            if not entry:
                continue
            route_minutes = int(entry.get("duration_minutes") or 0)
            if entry.get("source") in {"same_location", "near_location"} or route_minutes <= 8:
                near_anchor = True
                break
        if near_anchor or detour <= max(8, duration + 5):
            jlog(
                "OPTIONAL_ROUTE_GUARD",
                f"title={item.get('title')} reason=small_incremental_detour",
                "ACCEPT",
            )
            return 120.0 if near_anchor else 80.0
        one_sided_route = not previous_real_item or not next_real_item
        if one_sided_route and detour > max(20, duration + 5):
            jlog(
                "OPTIONAL_ROUTE_GUARD",
                (
                    f"title={item.get('title')} detour_minutes={detour} duration={duration} "
                    "reason=low_weight_detour_too_high"
                ),
                "REJECT",
            )
            return -detour * 8.0
        if detour > max(20, duration * 2):
            jlog(
                "OPTIONAL_ROUTE_GUARD",
                (
                    f"title={item.get('title')} detour_minutes={detour} duration={duration} "
                    "reason=low_weight_detour_too_high"
                ),
                "REJECT",
            )
            return -detour * 8.0
        return 0.0

    def _mark_soft_anchor_boosts(self, flexible: List[Dict[str, Any]]) -> None:
        referenced_titles = set()
        for item in flexible:
            for order in self._preferred_order_list(item):
                target_title = clean_title(order.get("target_title") or "")
                if target_title:
                    referenced_titles.add(target_title)
        if not referenced_titles:
            return
        for item in flexible:
            if clean_title(item.get("title") or "") in referenced_titles:
                item["_soft_anchor_boost"] = True

    def _preferred_order_list(self, item: Dict[str, Any]) -> List[Dict[str, Any]]:
        orders: List[Dict[str, Any]] = []
        if isinstance(item.get("preferred_order"), dict):
            orders.append(item["preferred_order"])
        if isinstance(item.get("preferred_orders"), list):
            orders.extend(order for order in item["preferred_orders"] if isinstance(order, dict))

        deduped: List[Dict[str, Any]] = []
        seen = set()
        for order in orders:
            key = (clean_title(order.get("kind") or "after"), clean_title(order.get("target_title") or ""))
            if not key[1] or key in seen:
                continue
            seen.add(key)
            deduped.append(order)
        return deduped

    def _adjust_candidate_for_preferred_orders(
        self,
        item: Dict[str, Any],
        timeline: List[Dict[str, Any]],
        candidate_start: int,
        duration: int,
        gap_start: int,
        gap_end: int,
        before_transition: int,
        after_transition: int,
    ) -> int:
        adjusted_start = candidate_start
        earliest_start = max(gap_start + before_transition, item.get("earliest_start") or gap_start)
        for order in self._preferred_order_list(item):
            anchor = self._find_anchor_in_timeline(order, timeline)
            if not anchor:
                continue
            kind = clean_title(order.get("kind") or "after")
            if kind == "before":
                latest_start = (anchor.get("scheduled_start") or gap_end) - duration
                if adjusted_start + duration > (anchor.get("scheduled_start") or gap_end) and latest_start >= earliest_start:
                    adjusted_start = latest_start
            else:
                anchor_end = anchor.get("scheduled_end")
                if anchor_end is not None and adjusted_start < anchor_end:
                    shifted_start = max(adjusted_start, anchor_end)
                    if shifted_start + duration + after_transition <= gap_end:
                        adjusted_start = shifted_start
        return adjusted_start

    def _preferred_order_score_adjustment(
        self,
        item: Dict[str, Any],
        timeline: List[Dict[str, Any]],
        candidate_start: int,
        candidate_end: int,
    ) -> Tuple[float, List[Dict[str, str]]]:
        delta = 0.0
        violations: List[Dict[str, str]] = []
        for order in self._preferred_order_list(item):
            anchor = self._find_anchor_in_timeline(order, timeline)
            if not anchor:
                continue
            kind = clean_title(order.get("kind") or "after")
            anchor_title = anchor.get("title") or order.get("target_title")
            if kind == "before":
                satisfied = candidate_end <= (anchor.get("scheduled_start") or 0)
            else:
                satisfied = candidate_start >= (anchor.get("scheduled_end") or 0)
            if satisfied:
                delta += 140.0
            else:
                delta -= 6000.0
                violations.append({
                    "kind": kind,
                    "anchor_title": anchor_title or "the requested anchor",
                })
        return delta, violations

    def _insert_best_position(
        self,
        item: Dict[str, Any],
        timeline: List[Dict[str, Any]],
        day_start: int,
        day_end: int,
        min_travel: Optional[int],
        prefer_earliest: bool = False,
        prefer_latest: bool = False,
        validation_day_start: Optional[int] = None,
        validation_day_end: Optional[int] = None,
    ) -> Tuple[Optional[List[Dict[str, Any]]], str]:
        """Finds the optimal time slot for an activity within a range, applying narrative-aware scoring."""
        best_timeline = None
        best_score = -999999
        best_preferred_window_penalty = 0.0
        failure_reason = "No feasible slot was available."
        
        duration = int(item.get("duration_minutes") or 60)

        for index in range(len(timeline) + 1):
            previous_item = timeline[index - 1] if index > 0 else None
            next_item = timeline[index] if index < len(timeline) else None
            previous_real_item = self._nearest_travel_required_before(timeline, index)
            next_real_item = self._nearest_travel_required_after(timeline, index)
            item_requires_travel = self._activity_requires_travel(item)

            gap_start = day_start if previous_item is None else previous_item["scheduled_end"]
            gap_end = day_end if next_item is None else next_item["scheduled_start"]

            before_transition = self._transition_minutes(previous_real_item, item, min_travel) if item_requires_travel else 0
            if item_requires_travel:
                after_transition = self._transition_minutes(item, next_item, min_travel) if self._activity_requires_travel(next_item) else 0
            else:
                after_transition = self._transition_minutes(previous_real_item, next_real_item, min_travel) if next_real_item else 0

            candidate_start = max(
                gap_start + before_transition,
                item.get("earliest_start") or day_start,
                day_start
            )
            preferred_start = item.get("preferred_start")
            if (
                preferred_start is not None
                and candidate_start < preferred_start
                and preferred_start + duration + after_transition <= gap_end
                and preferred_start + duration <= (item.get("latest_end") or day_end)
            ):
                candidate_start = preferred_start
            preferred_end = item.get("preferred_end")
            if preferred_end is None and clean_title(item.get("semantic_constraint_type") or "") in {"arrive_by", "dropoff", "deadline"}:
                preferred_end = item.get("latest_end") or item.get("arrive_by")
            preferred_end = parse_clock(preferred_end)
            latest_end_limit = parse_clock(item.get("latest_end")) or day_end
            if preferred_end is not None:
                latest_start_for_deadline = min(
                    preferred_end - duration,
                    latest_end_limit - duration,
                    gap_end - after_transition - duration,
                )
                earliest_candidate = max(
                    gap_start + before_transition,
                    item.get("earliest_start") or day_start,
                    day_start,
                )
                if latest_start_for_deadline >= earliest_candidate and candidate_start < latest_start_for_deadline:
                    candidate_start = latest_start_for_deadline
            preferred_window_start = item.get("preferred_window_start")
            preferred_window_end = item.get("preferred_window_end")
            if (
                preferred_window_start is not None
                and candidate_start < preferred_window_start
                and preferred_window_start + duration + after_transition <= gap_end
                and preferred_window_start + duration <= (item.get("latest_end") or preferred_window_end or day_end)
            ):
                candidate_start = preferred_window_start
            candidate_start = self._adjust_candidate_for_preferred_orders(
                item,
                timeline,
                candidate_start,
                duration,
                gap_start,
                gap_end,
                before_transition,
                after_transition,
            )
            candidate_end = candidate_start + duration
            if self._short_low_weight_flex(item) and previous_item:
                jlog(
                    "CANDIDATE_DEBUG",
                    (
                        f"title={item.get('title')} candidate_slot=after {previous_item.get('title')} "
                        "generated=true"
                    ),
                    None,
                )

            # Feasibility Checks
            if candidate_end + after_transition > gap_end:
                if self._short_low_weight_flex(item) and previous_item:
                    jlog(
                        "CANDIDATE_REJECT",
                        f"title={item.get('title')} slot=after {previous_item.get('title')} reason=insufficient_gap_after_travel",
                        None,
                    )
                continue 
            
            if candidate_end > latest_end_limit:
                if self._short_low_weight_flex(item) and previous_item:
                    jlog(
                        "CANDIDATE_REJECT",
                        f"title={item.get('title')} slot=after {previous_item.get('title')} reason=latest_end_limit",
                        None,
                    )
                continue 

            candidate = deepcopy(item)
            candidate["scheduled_start"] = candidate_start
            candidate["scheduled_end"] = candidate_end
            candidate_timeline = sorted(timeline + [candidate], key=lambda x: x["scheduled_start"])
            route_violations = self._route_aware_timeline_violations(
                candidate_timeline,
                validation_day_start if validation_day_start is not None else day_start,
                validation_day_end if validation_day_end is not None else day_end,
                min_travel,
            )
            if route_violations:
                first_violation = route_violations[0]
                reason = first_violation.get("reason") or "route_infeasible"
                if self._short_low_weight_flex(item) and previous_item:
                    jlog(
                        "CANDIDATE_REJECT",
                        f"title={item.get('title')} slot=after {previous_item.get('title')} reason={reason}",
                        None,
                    )
                if reason == "start_route_blocker":
                    jlog(
                        "MODULE_C",
                        f"first_event={first_violation.get('to')} leave_by={format_clock(int(first_violation.get('leave_by') or 0))}",
                        "START_ROUTE_CONSTRAINT",
                    )
                elif reason == "route_transition":
                    jlog(
                        "MODULE_C",
                        (
                            f"from={first_violation.get('from')} to={first_violation.get('to')} "
                            f"required_travel={first_violation.get('required_travel')}"
                        ),
                        "ROUTE_CONSTRAINT",
                    )
                continue

            # Scoring
            base_score = self._calculate_activity_base_score(item)
            delay_penalty = 0
            if prefer_earliest:
                delay_minutes = max(0, candidate_start - day_start)
                delay_penalty = delay_minutes * 2.0 # Strong penalty for delay
            
            total_score = base_score - delay_penalty
            if prefer_latest:
                total_score += candidate_start
            if preferred_start is not None:
                total_score -= abs(candidate_start - preferred_start) * 1.5
            preferred_window_penalty = 0.0
            generic_preference = preference_window_deviation(item, candidate_start, candidate_end)
            generic_info = generic_preference.get("info") or {}
            if generic_preference.get("hard_violation"):
                jlog(
                    "PREF_WINDOW",
                    (
                        f"title={item.get('title')} reason=hard_window_violation "
                        f"window={format_clock(generic_info.get('acceptable_start'))}-{format_clock(generic_info.get('acceptable_end'))}"
                    ),
                    "REJECT",
                )
                continue
            if generic_info:
                jlog_verbose(
                    "PREF_WINDOW",
                    (
                        f"title={item.get('title')} source={generic_info.get('source')} "
                        f"window={format_clock(generic_info.get('acceptable_start'))}-{format_clock(generic_info.get('acceptable_end'))} "
                        f"weight={generic_info.get('weight')}"
                    ),
                    "DETECT",
                )
            if preferred_window_start is not None or preferred_window_end is not None:
                window_start = preferred_window_start if preferred_window_start is not None else day_start
                window_end = preferred_window_end if preferred_window_end is not None else day_end
                window_label = clean_title(item.get("preferred_time_window") or "")
                if candidate_start >= window_start and candidate_end <= window_end:
                    total_score += 160
                    jlog_verbose(
                        "PREFERRED_WINDOW",
                        f"title={item.get('title')} window={window_label or 'preferred'} start={format_clock(candidate_start)} status=within_window penalty=0",
                        None,
                    )
                else:
                    penalty_multiplier = 4.0
                    if candidate_start < window_start:
                        preferred_window_penalty += (window_start - candidate_start) * penalty_multiplier
                    if candidate_end > window_end:
                        preferred_window_penalty += (candidate_end - window_end) * penalty_multiplier
                    total_score -= preferred_window_penalty
            generic_penalty = float(generic_preference.get("penalty") or 0)
            if generic_info:
                jlog_verbose(
                    "PREF_WINDOW",
                    (
                        f"title={item.get('title')} start={format_clock(candidate_start)} "
                        f"window={format_clock(generic_info.get('acceptable_start'))}-{format_clock(generic_info.get('acceptable_end'))} "
                        f"deviation={int(generic_preference.get('deviation') or 0)} penalty={round(generic_penalty, 2)}"
                    ),
                    "SCORE",
                )
                jlog_verbose(
                    "PREF_WINDOW",
                    f"title={item.get('title')} affects_total_score=true",
                    "SCORE_APPLIED",
                )
                if not generic_penalty:
                    weight_bonus = {"hard": 220, "high": 180, "medium": 80, "low": 25}.get(str(generic_info.get("weight")), 60)
                    total_score += weight_bonus
            if generic_penalty > preferred_window_penalty:
                total_score -= generic_penalty - preferred_window_penalty
            order_delta, order_violations = self._preferred_order_score_adjustment(
                item,
                timeline,
                candidate_start,
                candidate_end,
            )
            total_score += order_delta
            total_score += self._semantic_candidate_score_adjustment(
                item,
                timeline,
                index,
                candidate_start,
                candidate_end,
            )
            total_score += self._route_candidate_practicality_adjustment(
                item,
                previous_real_item,
                next_real_item,
            )
            total_score += self._optional_route_cost_adjustment(
                item,
                previous_real_item,
                next_real_item,
            )
            
            if total_score > best_score:
                best_score = total_score
                best_preferred_window_penalty = preferred_window_penalty
                
                if prefer_earliest and delay_minutes > 0:
                    candidate.setdefault("trace", []).append(f"Placed with {delay_minutes}m delay from earliest possible start.")
                if preferred_window_start is not None or preferred_window_end is not None:
                    window_label = item.get("preferred_time_window") or "preferred window"
                    candidate.setdefault("trace", []).append(f"Placed with awareness of the {window_label} preference.")
                    if preferred_window_penalty > 0:
                        candidate["preferred_window_penalty"] = round(float(preferred_window_penalty), 2)
                        candidate.setdefault("trace", []).append(
                            f"Preferred window fallback penalty={round(float(preferred_window_penalty), 2)}."
                        )
                        if clean_title(window_label) != "evening":
                            jlog(
                                "MODULE_C",
                                (
                                    f"title={item.get('title')} window={window_label} "
                                    f"penalty={round(float(preferred_window_penalty), 2)}"
                                ),
                                "PREFERRED_WINDOW_PENALTY",
                            )
                if order_violations:
                    candidate["accepted_with_warning"] = True
                    candidate["warning_code"] = "SOFT_PREFERENCE_UNMET"
                    candidate.setdefault("warnings", [])
                    for violation in order_violations:
                        explanation = (
                            f"{candidate.get('title')} could not fully satisfy the soft "
                            f"{violation['kind']} {violation['anchor_title']} preference."
                        )
                        candidate["warnings"].append({
                            "warning_code": "SOFT_PREFERENCE_UNMET",
                            "activity_id": candidate.get("stable_activity_id") or candidate.get("id"),
                            "activity_title": candidate.get("title"),
                            "anchor_title": violation["anchor_title"],
                            "start": format_clock(candidate_start),
                            "end": format_clock(candidate_end),
                            "explanation": explanation,
                        })
                        candidate.setdefault("trace", []).append(explanation)
                
                best_timeline = candidate_timeline

        if best_timeline:
            if self._optional_preferred_item_should_skip(item, best_preferred_window_penalty):
                return None, "preferred_window_unavailable"
            return best_timeline, ""
        return None, failure_reason

    def _optional_preferred_item_should_skip(
        self,
        item: Dict[str, Any],
        preferred_window_penalty: float,
    ) -> bool:
        return should_skip_optional_preferred_item(item, preferred_window_penalty)

    def _nearest_travel_required_before(
        self,
        timeline: List[Dict[str, Any]],
        insertion_index: int,
    ) -> Optional[Dict[str, Any]]:
        for candidate in reversed(timeline[:insertion_index]):
            if self._activity_requires_travel(candidate):
                return candidate
        return None

    def _nearest_travel_required_after(
        self,
        timeline: List[Dict[str, Any]],
        insertion_index: int,
    ) -> Optional[Dict[str, Any]]:
        for candidate in timeline[insertion_index:]:
            if self._activity_requires_travel(candidate):
                return candidate
        return None

    def _transition_minutes(
        self,
        left: Optional[Dict[str, Any]],
        right: Optional[Dict[str, Any]],
        min_travel: Optional[int],
    ) -> int:
        if left is None or right is None:
            return 0
        if not self._activity_requires_travel(left) or not self._activity_requires_travel(right):
            return 0
        if not left.get("location") or not right.get("location"):
            return 0
        route_entry = self._route_context_entry(left, right)
        if route_entry:
            travel = int(route_entry.get("duration_minutes") or 0)
        else:
            travel = estimate_travel_minutes(left.get("location"), right.get("location"))
            travel = max(travel, min_travel or 0)
        prep = max(
            int(left.get("prep_buffer", DEFAULT_PREP_BUFFER) or 0),
            int(right.get("prep_buffer", DEFAULT_PREP_BUFFER) or 0),
        )
        return travel + prep

