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
from .module_d_refinement import ModuleDRefinementMixin, REFINEMENT_META_KEYS
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
    ModuleDRefinementMixin,
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
        saved_locations: List[Dict[str, Any]] = [],
        disable_deterministic_fallback: bool = False,
        fallback_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._parse_request(
            latest_request=message,
            history=history or [],
            current_schedule=current_schedule,
            audio_part=None,
            saved_locations=saved_locations,
            disable_deterministic_fallback=disable_deterministic_fallback,
            fallback_reason=fallback_reason,
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
        schedule_constraints = deepcopy(
            parsed.get("schedule_constraints")
            or preferences.get("schedule_constraints")
            or (current_schedule or {}).get("schedule_constraints")
            or ((current_schedule or {}).get("preferences") or {}).get("schedule_constraints")
            or {}
        )
        if schedule_constraints:
            preferences["schedule_constraints"] = schedule_constraints
        preferences["travel_intent"] = bool(preferences.get("travel_intent") or detect_travel_intent(latest_request or ""))
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
        requested_operations = self._apply_implicit_lunch_handling(
            requested_operations,
            latest_request,
            existing_active,
            preferences,
        )
        self._log_normalized_operations(requested_operations)
        self._configure_module_d_run_policy(
            preferences,
            current_schedule,
            parsed,
            requested_operations,
            latest_request,
            is_apply_operations=False,
        )

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
                            self._copy_anchor_location_to_operation(operation, anchor_activity)
            canonical_activities.append(
                self._canonicalize_activity(
                    operation,
                    source_turn=source_turn,
                    default_source="initial_request",
                )
            )

        active_set = [item for item in canonical_activities if item.get("status") == "active"]
        self._apply_default_prep_buffer(active_set, preferences)
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
        status = "partial" if (conflicts or formatted_unscheduled) else ("warning" if warnings else "ok")

        schedule_data = {
            "schema_version": 4,
            "scheduleId": (current_schedule or {}).get("scheduleId"),
            "date": schedule_date,
            "status": status,
            "planning_mode": planning_mode,
            "allow_clash": allow_clash,
            "accurate_travel_time": accurate_travel_time,
            "schedule_constraints": schedule_constraints,
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
            "unmet_items": formatted_unscheduled,
            "unmet_optional": formatted_unscheduled,
            "validation_issues": [],
        }
        for key in REFINEMENT_META_KEYS:
            schedule_data[key] = planned_result.get(key)
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

    def run_manual_scheduler(
        self,
        envelope: Dict[str, Any],
        saved_locations: Optional[List[Dict[str, Any]]] = None,
        base_version: Optional[int] = None,
        source: str = "manual_button",
    ) -> Dict[str, Any]:
        current_version = int((envelope or {}).get("version") or 1)
        if base_version is not None and int(base_version) != current_version:
            raise VersionMismatchError(f"Version mismatch: schedule_version {base_version} != currentVersion {current_version}")

        working = deepcopy(envelope or {})
        if hasattr(self, "_clear_route_preview_metadata"):
            self._clear_route_preview_metadata(working)
        for key in (
            "route_repair_actions",
            "route_conflicts",
            "pending_repair_suggestions",
            "blocked_activities",
            "start_route_summary",
            "failed_repair_attempt",
        ):
            if key.endswith("_summary") or key.endswith("_attempt"):
                working[key] = None
            else:
                working[key] = []

        preferences = deepcopy(working.get("preferences") or {})
        if hasattr(self, "_normalize_day_boundary_preferences"):
            self._normalize_day_boundary_preferences(preferences)
        allow_clash = self._resolve_allow_clash(preferences, working)
        planning_mode = self._planning_mode(allow_clash)
        accurate_travel_time = self._resolve_accurate_travel_time(preferences, working)
        preferences["allow_clash"] = allow_clash
        preferences["planning_mode"] = planning_mode
        preferences["accurate_travel_time"] = accurate_travel_time
        preferences["refinement_reason"] = "explicit_optimize"
        preferences["module_0_route"] = "manual_scheduler"
        preferences["source"] = source

        jlog("RUN_SCHEDULER", f"source={source} accurate_travel_time={str(accurate_travel_time).lower()}", None)

        input_activities = [
            item for item in (working.get("activities") or [])
            if self._is_replan_input_user_activity(item)
        ]
        input_titles = [str(item.get("title") or "Untitled") for item in input_activities]
        jlog("REPLAN", f"count={len(input_activities)} titles={input_titles}", "INPUT_ACTIVITIES")

        loaded_canonical = self._load_canonical_activities(working)
        loaded_titles = [str(item.get("title") or "Untitled") for item in loaded_canonical if item.get("status") == "active"]
        jlog("REPLAN", f"count={len(loaded_titles)} titles={loaded_titles}", "LOADED_ACTIVITIES")
        integrity_failure = self._replan_activity_integrity_failure(working, input_activities, loaded_canonical)
        if integrity_failure:
            return integrity_failure

        active_set = [
            self._prepare_activity_for_manual_scheduler(item)
            for item in loaded_canonical
            if item.get("status") == "active"
        ]
        self._apply_default_prep_buffer(active_set, preferences)
        for item in active_set:
            if item.get("resolved_location") or item.get("location_status") == "resolved":
                jlog(
                    "RUN_SCHEDULER",
                    (
                        f"title={item.get('title')} travel_required={str(bool(item.get('travel_required'))).lower()} "
                        f"location_status={item.get('location_status') or item.get('location_resolution_status')}"
                    ),
                    "INPUT_LOCATION",
                )
        schedule_date = working.get("date") or self._local_today_iso()
        jlog("MODULE_9", f"replanning date={schedule_date} source=manual_button full_optimizer=true", "REPLAN")
        planned_result = self._plan_schedule(schedule_date, active_set, preferences)
        conflicts = self._build_conflicts(planned_result.get("activities", []), set())
        warnings = self._collect_schedule_warnings(
            planned_result.get("activities", []),
            planned_result.get("schedule_blocks", []),
        )
        formatted_activities = [self._format_activity(item) for item in planned_result.get("activities", [])]
        formatted_unscheduled = [self._format_activity(item) for item in planned_result.get("unscheduled_activities", [])]
        status = "partial" if (conflicts or formatted_unscheduled) else ("warning" if warnings else "ok")

        updated = {
            **working,
            "schema_version": 4,
            "date": schedule_date,
            "status": status,
            "schedule_status": status,
            "planning_mode": planning_mode,
            "allow_clash": allow_clash,
            "accurate_travel_time": accurate_travel_time,
            "preferences": preferences,
            "activities": formatted_activities,
            "schedule_blocks": planned_result.get("schedule_blocks", []),
            "unscheduled_activities": formatted_unscheduled,
            "version": current_version + 1,
            "explanations": planned_result.get("explanations", []),
            "conflicts": conflicts,
            "warnings": warnings,
            "conflict": conflicts[0] if conflicts else None,
            "unmet_items": formatted_unscheduled,
            "validation_issues": [],
            "travel_validation_status": "not_requested",
            "location_resolution_requests": [],
            "needs_reschedule": False,
            "reschedule_reason": None,
            "needs_travel_validation": False,
            "last_rescheduled_at": self._now_iso(),
        }
        for key in REFINEMENT_META_KEYS:
            updated[key] = planned_result.get(key)

        updated = self._apply_accurate_travel_if_requested(updated, saved_locations or [])
        travel_status = updated.get("travel_validation_status")
        if travel_status == "pending_locations" or updated.get("schedule_status") == "location_pending":
            updated["needs_travel_validation"] = True
            updated["needs_reschedule"] = False
        else:
            updated["needs_travel_validation"] = False
            updated["needs_reschedule"] = False
            updated["reschedule_reason"] = None
            updated["last_rescheduled_at"] = updated.get("last_rescheduled_at") or self._now_iso()

        jlog("RUN_SCHEDULER", f"status={updated.get('travel_validation_status') or updated.get('schedule_status')}", "DONE")
        return updated

    def _is_replan_input_user_activity(self, item: Any) -> bool:
        return (
            self._is_activity_entry(item)
            and not self._is_generic_system_activity_payload(item)
            and str((item or {}).get("status") or "active") == "active"
        )

    def _replan_activity_key(self, item: Dict[str, Any]) -> str:
        return str(
            item.get("stable_activity_id")
            or item.get("activity_id")
            or item.get("id")
            or clean_title(item.get("title") or "")
        )

    def _replan_activity_integrity_failure(
        self,
        working: Dict[str, Any],
        input_activities: List[Dict[str, Any]],
        loaded_canonical: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        input_by_key = {
            self._replan_activity_key(item): item
            for item in input_activities
            if self._replan_activity_key(item)
        }
        loaded_keys = {
            self._replan_activity_key(item)
            for item in loaded_canonical
            if item.get("status") == "active" and self._replan_activity_key(item)
        }
        missing_keys = [key for key in input_by_key if key not in loaded_keys]
        if not missing_keys and len(input_by_key) == len(loaded_keys):
            return None

        missing_labels = []
        for key in missing_keys:
            item = input_by_key[key]
            label = f"{item.get('title') or 'Untitled'} ({key})"
            missing_labels.append(label)
            jlog(
                "REPLAN",
                f"title={item.get('title') or 'Untitled'} id={key}",
                "MISSING_ACTIVITY_BEFORE_MODULE_C",
            )
        if not missing_labels and len(input_by_key) != len(loaded_keys):
            missing_labels.append(
                f"input_count={len(input_by_key)} loaded_count={len(loaded_keys)}"
            )

        updated = deepcopy(working)
        updated["schedule_status"] = "replan_input_invalid"
        updated["status"] = "replan_input_invalid"
        updated["travel_validation_status"] = "replan_input_invalid"
        updated["needs_reschedule"] = True
        updated["needs_travel_validation"] = bool(updated.get("needs_travel_validation"))
        updated.setdefault("validation_issues", [])
        updated["validation_issues"] = list(dict.fromkeys(
            list(updated.get("validation_issues") or [])
            + [f"Run scheduler stopped because activities disappeared before Module C: {', '.join(missing_labels)}."]
        ))
        updated.setdefault("warnings", [])
        updated["warnings"] = list(updated.get("warnings") or []) + [{
            "type": "replan_input_invalid",
            "severity": "high",
            "message": updated["validation_issues"][-1],
        }]
        jlog("RUN_SCHEDULER", "status=replan_input_invalid", "DONE")
        return updated

    def _prepare_activity_for_manual_scheduler(self, item: Dict[str, Any]) -> Dict[str, Any]:
        prepared = deepcopy(item)
        resolved_location = prepared.get("resolved_location") if isinstance(prepared.get("resolved_location"), dict) else {}
        has_coordinates = (
            resolved_location.get("latitude") is not None
            and resolved_location.get("longitude") is not None
        )
        if has_coordinates:
            display_name = (
                resolved_location.get("display_name")
                or resolved_location.get("address")
                or prepared.get("location_label")
                or prepared.get("location")
            )
            if display_name:
                prepared["location"] = display_name
                prepared["location_label"] = display_name
            existing_kind = str(prepared.get("location_kind") or "").strip().lower().replace(" ", "_").replace("-", "_")
            existing_category = str(prepared.get("location_category") or "").strip().lower().replace(" ", "_").replace("-", "_")
            prepared["location_status"] = "resolved"
            prepared["location_resolution_status"] = "resolved"
            prepared["location_policy"] = "exact_location_required"
            if existing_kind in {"", "no_location_required", "online", "none", "no_location"}:
                prepared["location_kind"] = "exact_named_place"
            if existing_category in {"", "home_or_online", "no_location", "none"}:
                prepared["location_category"] = resolved_location.get("category") or "manual_place"
            prepared["location_source"] = prepared.get("location_source") or resolved_location.get("source") or "selected_geocode"
            prepared["travel_required"] = True
            prepared["location_flexible"] = False
            prepared["can_be_done_at_current_location"] = False

        protection = str(prepared.get("repair_protection") or "").lower()
        timing_mode = str(prepared.get("timing_mode") or "").lower()
        original_mode = str(prepared.get("original_timing_mode") or "").lower()
        is_fixed_anchor = (
            bool(prepared.get("is_user_fixed"))
            or prepared.get("user_fixed_start") is not None
            or protection in {"fixed", "protected", "protected_social", "critical"}
            or timing_mode == TimingMode.FIXED
            or original_mode == TimingMode.FIXED and prepared.get("fixed_start") is not None
        )

        if is_fixed_anchor:
            fixed_start = prepared.get("fixed_start")
            fixed_end = prepared.get("fixed_end")
            if fixed_start is None:
                fixed_start = prepared.get("scheduled_start")
            if fixed_end is None:
                fixed_end = prepared.get("scheduled_end")
            if fixed_start is not None and fixed_end is None:
                fixed_end = fixed_start + int(prepared.get("duration_minutes") or 60)
            prepared.update({
                "timing_mode": TimingMode.FIXED,
                "is_user_fixed": True,
                "user_fixed_start": fixed_start,
                "fixed_start": fixed_start,
                "fixed_end": fixed_end,
                "scheduled_start": fixed_start,
                "scheduled_end": fixed_end,
                "locked_fixed": True,
                "preserve_scheduled_time": True,
                "can_move_for_repair": False,
                "repair_protection": prepared.get("repair_protection") or "fixed",
            })
            return prepared

        if prepared.get("anchor_relation") or prepared.get("is_derived_time") or protection == "derived":
            prepared.update({
                "timing_mode": TimingMode.RELATIVE,
                "is_user_fixed": False,
                "user_fixed_start": None,
                "fixed_start": None,
                "fixed_end": None,
                "scheduled_start": None,
                "scheduled_end": None,
                "locked_fixed": False,
                "preserve_scheduled_time": False,
                "can_move_for_repair": True,
                "repair_protection": "derived",
                "placement_source": "system_derived",
                "is_derived_time": True,
            })
            return prepared

        preferred_start = prepared.get("preferred_start")
        prepared.update({
            "timing_mode": TimingMode.UNSPECIFIED if timing_mode == TimingMode.FIXED else prepared.get("timing_mode", TimingMode.UNSPECIFIED),
            "is_user_fixed": False,
            "user_fixed_start": None,
            "fixed_start": None,
            "fixed_end": None,
            "scheduled_start": None,
            "scheduled_end": None,
            "preferred_start": preferred_start,
            "locked_fixed": False,
            "preserve_scheduled_time": False,
            "can_move_for_repair": True,
            "repair_protection": "flexible" if protection in {"fixed", "protected", "protected_social", "critical"} else (prepared.get("repair_protection") or "flexible"),
        })
        return prepared

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
