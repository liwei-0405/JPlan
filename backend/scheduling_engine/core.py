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
from .location_normalizer import LocationNormalizerMixin
from .module_0_router import Module0RouterMixin
from .module_8_reply import Module8ReplyMixin
from .module_a_parser import ModuleAParserMixin
from .module_b_validation import ModuleBValidationMixin
from .module_c_constructor import ModuleCConstructorMixin
from .state_model import StateModelMixin
from .state_operations import StateOperationsMixin
from .travel_validation import TravelValidationMixin
from .types_utils import *
from .types_utils import _normalize_location


class SchedulingEngine(
    Module0RouterMixin,
    ModuleAParserMixin,
    LocationNormalizerMixin,
    ModuleBValidationMixin,
    ModuleCConstructorMixin,
    StateModelMixin,
    TravelValidationMixin,
    StateOperationsMixin,
    Module8ReplyMixin,
):
    def __init__(self, client: Any, travel_service: Optional[TravelService] = None):
        self.client = client
        self.travel_service = travel_service or TravelService()

    def _debug(self, message: str) -> None:
        module, stage, clean_message = self._log_context_from_message(message)
        jlog(module, clean_message, stage)

    def _debug_json(self, label: str, payload: Any) -> None:
        module, stage, clean_label = self._log_context_from_message(label)
        if clean_label == label and label.lower().startswith("llm parsed"):
            module, stage = "MODULE_A", "PARSE"
        elif clean_label == label and "fallback parse" in label.lower():
            module, stage = "MODULE_A", "FALLBACK_PARSE"
        jjson(module, clean_label, payload, stage)

    def _log_context_from_message(self, message: str) -> Tuple[str, Optional[str], str]:
        match = re.match(r"^\[([A-Z0-9_]+)\](?:\[([A-Z0-9_]+)\])?\s*(.*)$", str(message))
        if not match:
            return "ENGINE", "FLOW", str(message)

        head, substage, body = match.group(1), match.group(2), match.group(3)
        if head.startswith("MODULE_"):
            stage = substage or ("PARSE" if head == "MODULE_A" else None)
            return head, stage, body
        if head == "STATE":
            return "STATE", substage or "ACTIVITY", body
        if head == "TRAVEL":
            return "TRAVEL_SERVICE", substage or "VALIDATION", body
        if head == "DATE_NORMALIZE":
            return "MODULE_A", "DATE", body
        if head == "CONFLICT":
            return "MODULE_B", "CONFLICT", body
        return "ENGINE", head, body

    def parse_text_request(
        self,
        message: str,
        history: Optional[List[Dict[str, Any]]] = None,
        current_schedule: Optional[Dict[str, Any]] = None,
        saved_locations: List[Dict[str, Any]] = []
    ) -> Dict[str, Any]:
        return self._parse_request(
            latest_request=message,
            history=history or [],
            current_schedule=current_schedule,
            audio_part=None,
            saved_locations=saved_locations
        )

    def parse_audio_request(
        self,
        audio_part: Any,
        history: Optional[List[Dict[str, Any]]] = None,
        current_schedule: Optional[Dict[str, Any]] = None,
        saved_locations: List[Dict[str, Any]] = []
    ) -> Dict[str, Any]:
        return self._parse_request(
            latest_request="Audio request",
            history=history or [],
            current_schedule=current_schedule,
            audio_part=audio_part,
            saved_locations=saved_locations
        )

    def build_schedule_response(
        self,
        parsed: Dict[str, Any],
        current_schedule: Optional[Dict[str, Any]],
        latest_request: str,
        saved_locations: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        parsed = self._normalize_parsed_locations(parsed, latest_request, saved_locations or [])
        reply = self._resolve_user_reply(parsed, latest_request)
        transcription = parsed.get("transcription") or latest_request
        intent = parsed.get("intent", "schedule")

        if intent == "chat" and not parsed.get("operations") and not parsed.get("activities"):
            return {
                "reply": reply,
                "transcription": transcription,
                "schedule_data": None,
            }

        schedule_date = self._resolve_schedule_date(parsed, current_schedule, latest_request)
        base_version = int((current_schedule or {}).get("version") or 0)
        source_turn = base_version + 1
        preferences = deepcopy(parsed.get("preferences") or (current_schedule or {}).get("preferences") or {})
        allow_clash = self._resolve_allow_clash(preferences, current_schedule)
        planning_mode = self._planning_mode(allow_clash)
        preferences["allow_clash"] = allow_clash
        preferences["planning_mode"] = planning_mode
        accurate_travel_time = self._resolve_accurate_travel_time(preferences, current_schedule)
        preferences["accurate_travel_time"] = accurate_travel_time

        canonical_activities = self._load_canonical_activities(current_schedule)
        existing_active = [item for item in canonical_activities if item.get("status") == "active"]
        existing_active_ids = {
            item.get("stable_activity_id")
            for item in existing_active
            if item.get("stable_activity_id")
        }
        requested_operations = list(parsed.get("operations") or [])
        if not requested_operations:
            requested_operations = [{**activity, "op": "add"} for activity in (parsed.get("activities") or [])]
        requested_operations = self._sanitize_operations(requested_operations)

        for raw_op in requested_operations:
            raw_op = {**raw_op, "_user_message": latest_request}
            operation = self._normalize_operation(raw_op)
            if operation["op"] != "add":
                continue
            anchor = operation.get("anchor_relation")
            if anchor:
                resolved_anchor = self._resolve_anchor_relation(anchor, existing_active + canonical_activities)
                if resolved_anchor:
                    operation["anchor_relation"] = resolved_anchor
                    if not operation.get("location") and self._should_inherit_anchor_location(operation):
                        anchor_activity = self._find_activity_by_stable_id(existing_active + canonical_activities, resolved_anchor.get("target_activity_id"))
                        if anchor_activity and not operation.get("location"):
                            operation["location"] = anchor_activity.get("location")
            canonical_activities.append(
                self._canonicalize_activity(
                    operation,
                    source_turn=source_turn,
                    default_source="initial_request",
                )
            )

        active_set = [item for item in canonical_activities if item.get("status") == "active"]
        for item in active_set:
            if (
                item.get("stable_activity_id") in existing_active_ids
                and item.get("scheduled_start") is not None
                and item.get("scheduled_end") is not None
                and item.get("timing_mode") != TimingMode.FIXED
            ):
                item["preserve_scheduled_time"] = True
        planned_result = self._plan_schedule(schedule_date, active_set, preferences)
        conflicts = self._build_conflicts(planned_result["activities"], set())
        warnings = self._collect_schedule_warnings(planned_result["activities"], planned_result["schedule_blocks"])
        formatted_activities = [self._format_activity(item) for item in planned_result["activities"]]
        formatted_unscheduled = [self._format_activity(item) for item in planned_result.get("unscheduled_activities", [])]
        status = "partial" if conflicts else ("warning" if warnings else "ok")

        schedule_data = {
            "schema_version": 4,
            "scheduleId": (current_schedule or {}).get("scheduleId"),
            "date": schedule_date,
            "status": status,
            "planning_mode": planning_mode,
            "allow_clash": allow_clash,
            "accurate_travel_time": accurate_travel_time,
            "version": max(1, source_turn),
            "preferences": preferences,
            "activities": formatted_activities,
            "schedule_blocks": planned_result["schedule_blocks"],
            "unscheduled_activities": formatted_unscheduled,
            "explanations": self._merge_explanations(parsed.get("explanations", []), planned_result.get("explanations", [])),
            "conflicts": conflicts,
            "warnings": warnings,
            "applied_changes": formatted_activities,
            "accepted_with_warnings": warnings,
            "rejected_changes": formatted_unscheduled,
            "unmet_items": [],
            "validation_issues": [],
        }
        schedule_data = self._apply_accurate_travel_if_requested(schedule_data, saved_locations or [])

        return {
            "reply": reply,
            "transcription": transcription,
            "schedule_data": schedule_data,
        }

    def resolve_target_date(
        self,
        parsed: Dict[str, Any],
        current_schedule: Optional[Dict[str, Any]],
        latest_request: str,
    ) -> str:
        return self._resolve_schedule_date(parsed, current_schedule, latest_request)

    def plan_from_text(
        self,
        message: str,
        history: Optional[List[Dict[str, Any]]] = None,
        current_schedule: Optional[Dict[str, Any]] = None,
        saved_locations: List[Dict[str, Any]] = []
    ) -> Dict[str, Any]:
        parsed = self.parse_text_request(
            message=message,
            history=history,
            current_schedule=current_schedule,
            saved_locations=saved_locations
        )
        return self.build_schedule_response(parsed, current_schedule, message)

    def plan_from_audio(
        self,
        audio_part: Any,
        history: Optional[List[Dict[str, Any]]] = None,
        current_schedule: Optional[Dict[str, Any]] = None,
        saved_locations: List[Dict[str, Any]] = []
    ) -> Dict[str, Any]:
        parsed = self.parse_audio_request(
            audio_part=audio_part,
            history=history,
            current_schedule=current_schedule,
            saved_locations=saved_locations
        )
        return self.build_schedule_response(
            parsed,
            current_schedule,
            parsed.get("transcription") or "Audio request",
        )

    def finalize_schedule(self, schedule_data: Dict[str, Any]) -> Dict[str, Any]:
        normalized_date = schedule_data.get("date") or self._local_today_iso()
        incoming_explanations = list(schedule_data.get("explanations") or [])
        incoming_unscheduled = list(schedule_data.get("unscheduled_activities") or [])
        normalized_activities = []
        for raw in schedule_data.get("activities", []):
            if raw.get("type") != "activity":
                continue
            start_min = parse_clock(raw.get("startTime"))
            end_min = parse_clock(raw.get("endTime"))
            if start_min is None:
                continue
            if end_min is None:
                end_min = start_min + parse_duration_minutes(raw.get("duration"))
            if end_min <= start_min:
                end_min += 24 * 60
            normalized_activities.append({
                "id": raw.get("id") or self._make_id(raw.get("title", "activity")),
                "title": raw.get("title", "Untitled Activity"),
                "location": raw.get("location"),
                "priority": raw.get("priority", "medium"),
                "is_mandatory": bool(raw.get("isMandatory", True)),
                "notes": raw.get("notes") or "Imported from existing schedule.",
                "prep_buffer": parse_duration_minutes(raw.get("prep_buffer") or DEFAULT_PREP_BUFFER, minimum=0),
                "duration_minutes": end_min - start_min,
                "fixed_start": start_min,
                "fixed_end": end_min,
                "earliest_start": start_min,
                "latest_end": end_min,
                "scheduled_start": start_min,
                "scheduled_end": end_min,
                "source": raw.get("source", "existing_schedule"),
                "trace": list(raw.get("trace") or ["Kept the manually arranged time block as a fixed commitment."]),
                "is_conflict": bool(raw.get("isConflict")),
                "conflict_with": list(raw.get("conflictWith") or []),
                "conflict_reason": raw.get("conflictReason"),
                "conflict_priority": raw.get("conflictPriority"),
                "conflict_severity": raw.get("conflictSeverity"),
            })

        normalized_activities.sort(key=lambda item: item["scheduled_start"])
        final_schedule = self._materialize_schedule(normalized_date, normalized_activities, [])
        final_schedule["explanations"] = self._merge_explanations(
            incoming_explanations,
            final_schedule.get("explanations", []),
        )
        if incoming_unscheduled and not final_schedule.get("unscheduled_activities"):
            final_schedule["unscheduled_activities"] = incoming_unscheduled
        return final_schedule



class VersionMismatchError(Exception):
    """Raised when the baseVersion does not match the currentVersion in the backend."""
    pass
