import os
import time
from copy import deepcopy
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from google import genai
from typing import List, Dict, Any, Optional
import database
from jplan_logging import jlog
from scheduling_engine import SchedulingEngine, VersionMismatchError
from travel_service import TravelService

# load environment variables from .env file
load_dotenv()

# Gemini API
client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
travel_service = TravelService()
scheduling_engine = SchedulingEngine(client, travel_service=travel_service)

# Calendar Service (Supabase initialed in database module)
import calendar_service
cal_service = calendar_service.CalendarService(database.supabase)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Models ---

class ScheduleItem(BaseModel):
    id: Optional[str] = None
    stable_activity_id: Optional[str] = None
    type: Optional[str] = "activity"
    entity_type: Optional[str] = None
    activity_type: Optional[str] = None
    block_type: Optional[str] = None # V3 support
    title: str
    normalized_title: Optional[str] = None
    startTime: Optional[str] = None  # Frontend compat
    endTime: Optional[str] = None    # Frontend compat
    start: Optional[str] = None      # Backend V3 compat
    end: Optional[str] = None        # Backend V3 compat
    location: Optional[str] = None
    location_label: Optional[str] = None
    location_category: Optional[str] = None
    location_status: Optional[str] = None
    location_source: Optional[str] = None
    location_confidence: Optional[float] = None
    location_normalized: Optional[str] = None
    raw_location_text: Optional[str] = None
    location_kind: Optional[str] = None
    location_resolution_status: Optional[str] = None
    no_location_reason: Optional[str] = None
    semantic_confidence: Optional[float] = None
    raw_llm_location: Optional[str] = None
    explicit_user_location: Optional[bool] = False
    needs_clarification: Optional[bool] = False
    parse_notes: Optional[str] = None
    location_warning: Optional[str] = None
    saved_location_label: Optional[str] = None
    resolved_location: Optional[Dict[str, Any]] = None
    travel_estimate_source: Optional[str] = None
    travel_validation_status: Optional[str] = None
    transport_mode: Optional[str] = None
    route_duration_minutes: Optional[int] = None
    from_coordinate: Optional[Dict[str, float]] = None
    to_coordinate: Optional[Dict[str, float]] = None
    duration: Optional[str] = None
    duration_minutes: Optional[int] = None
    priority: Optional[str] = "medium"
    isMandatory: Optional[bool] = True
    timing_mode: Optional[str] = None
    original_timing_mode: Optional[str] = None
    is_user_fixed: Optional[bool] = False
    is_system_scheduled: Optional[bool] = False
    user_fixed_start: Optional[int] = None
    can_move_for_repair: Optional[bool] = None
    repair_protection: Optional[str] = None
    fixed_start: Optional[int] = None
    fixed_end: Optional[int] = None
    earliest_start: Optional[int] = None
    latest_end: Optional[int] = None
    preferred_start: Optional[int] = None
    anchor_relation: Optional[Dict[str, Any]] = None
    sequence_index: Optional[int] = None
    status: Optional[str] = "active"
    source_turn: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    scheduled_start: Optional[int] = None
    scheduled_end: Optional[int] = None
    prep_buffer: Optional[int] = None
    aliases: Optional[List[str]] = []
    notes: Optional[str] = None
    explanation: Optional[str] = None
    trace: Optional[List[str]] = []
    source: Optional[str] = None
    isConflict: Optional[bool] = False
    is_conflicting: Optional[bool] = False
    conflict_ids: Optional[List[str]] = []
    conflictWith: Optional[List[str]] = []
    conflictReason: Optional[str] = None
    conflictPriority: Optional[str] = None
    conflictSeverity: Optional[str] = None
    reason_codes: Optional[List[str]] = []

class UnscheduledActivity(BaseModel):
    title: str
    reason: str
    priority: Optional[str] = "medium"
    isMandatory: Optional[bool] = True

class ScheduleEnvelope(BaseModel):
    scheduleId: Optional[str] = None
    date: str
    version: Optional[int] = 1
    schema_version: Optional[int] = 4
    status: Optional[str] = "ok"
    schedule_status: Optional[str] = "ok"
    travel_validation_status: Optional[str] = "not_requested"
    planning_mode: Optional[str] = "feasibility_first"
    allow_clash: Optional[bool] = False
    accurate_travel_time: Optional[bool] = False
    preferences: Optional[Dict[str, Any]] = {}
    activities: List[ScheduleItem]
    schedule_blocks: Optional[List[ScheduleItem]] = [] # Explicit timeline
    explanations: List[str] = []
    unscheduled_activities: List[UnscheduledActivity] = []
    conflict: Optional[Dict[str, Any]] = None
    conflicts: Optional[List[Dict[str, Any]]] = []
    warnings: Optional[List[Dict[str, Any]]] = []
    location_resolution_requests: Optional[List[Dict[str, Any]]] = []
    route_conflicts: Optional[List[Dict[str, Any]]] = []
    pending_repair_suggestions: Optional[List[Dict[str, Any]]] = []
    unfit_activities: Optional[List[Dict[str, Any]]] = []
    route_repair_actions: Optional[List[Dict[str, Any]]] = []
    route_efficiency: Optional[Dict[str, Any]] = {}
    route_total_before: Optional[int] = None
    route_total_after: Optional[int] = None
    route_minutes_saved: Optional[int] = None
    location_revisits_count: Optional[int] = None
    same_location_split_penalty_before: Optional[int] = None
    same_location_split_penalty_after: Optional[int] = None
    revisit_penalty_before: Optional[int] = None
    revisit_penalty_after: Optional[int] = None
    start_route_summary: Optional[Dict[str, Any]] = None
    unmet_items: Optional[List[Dict[str, Any]]] = []
    validation_issues: Optional[List[str]] = []

class SchedulePatchOperation(BaseModel):
    op: str # add, update, remove, move, replace, update_priority
    target_id: Optional[str] = None
    title: Optional[str] = None
    startTime: Optional[str] = None
    endTime: Optional[str] = None
    duration_minutes: Optional[int] = None
    location: Optional[str] = None
    priority: Optional[str] = None
    is_mandatory: Optional[bool] = None
    notes: Optional[str] = None

class SchedulePatchRequest(BaseModel):
    baseVersion: int
    operations: List[SchedulePatchOperation]
    user_id: str

class SchedulePatchResponse(BaseModel):
    scheduleId: Optional[str] = None
    version: int
    applied: bool
    updatedActivities: List[ScheduleItem]
    deletedItemIds: List[str]
    affectedRange: Optional[Dict[str, str]] = None 
    explanation: Optional[str] = None

class ChatRequest(BaseModel):
    message: str
    history: list[dict] = [] 
    current_schedule: Optional[ScheduleEnvelope] = None
    user_id: str | None = None
    allow_clash: bool = False
    accurate_travel_time: bool = False

class ChatResponse(BaseModel):
    reply: str
    patch: Optional[SchedulePatchResponse] = None
    full_schedule: Optional[ScheduleEnvelope] = None 
    schedule_data: Optional[ScheduleEnvelope] = None # Compatibility with legacy frontend
    transcription: str | None = None
    reply_status: Optional[str] = None
    recommend_allow_clash: bool = False
    reply_reason: Optional[str] = None
    llm_fallback_reason: Optional[str] = None

class PlanRequest(BaseModel):
    date: str
    activities: List[ScheduleItem]
    schedule_blocks: Optional[List[ScheduleItem]] = []
    explanations: List[str] = []
    unscheduled_activities: List[UnscheduledActivity] = []
    user_id: str
    version: int = 1
    scheduleId: Optional[str] = None
    status: Optional[str] = "ok"
    conflict: Optional[Dict[str, Any]] = None
    planning_mode: Optional[str] = "feasibility_first"
    allow_clash: bool = False
    accurate_travel_time: bool = False
    preferences: Optional[Dict[str, Any]] = {}
    schedule_status: Optional[str] = None
    travel_validation_status: Optional[str] = None
    conflicts: Optional[List[Dict[str, Any]]] = []
    warnings: Optional[List[Dict[str, Any]]] = []
    location_resolution_requests: Optional[List[Dict[str, Any]]] = []
    route_conflicts: Optional[List[Dict[str, Any]]] = []
    pending_repair_suggestions: Optional[List[Dict[str, Any]]] = []
    unfit_activities: Optional[List[Dict[str, Any]]] = []
    route_repair_actions: Optional[List[Dict[str, Any]]] = []
    route_efficiency: Optional[Dict[str, Any]] = {}
    route_total_before: Optional[int] = None
    route_total_after: Optional[int] = None
    route_minutes_saved: Optional[int] = None
    location_revisits_count: Optional[int] = None
    same_location_split_penalty_before: Optional[int] = None
    same_location_split_penalty_after: Optional[int] = None
    revisit_penalty_before: Optional[int] = None
    revisit_penalty_after: Optional[int] = None
    start_route_summary: Optional[Dict[str, Any]] = None
    unmet_items: Optional[List[Dict[str, Any]]] = []
    validation_issues: Optional[List[str]] = []

class ExportRequest(BaseModel):
    user_id: str
    date: str

class LocationResolveRequest(BaseModel):
    user_id: str
    label: str
    address: str
    display_name: Optional[str] = None
    category: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    source: Optional[str] = "ors_geocoded"
    confirmed_by_user: bool = True

class TravelCompleteRequest(BaseModel):
    user_id: str
    schedule: ScheduleEnvelope
    source: Optional[str] = "manual"

class UserPreferencesRequest(BaseModel):
    user_id: str
    day_start_time: Optional[str] = "08:00"
    day_end_time: Optional[str] = "22:00"
    use_day_boundary_preferences: Optional[bool] = True
    default_start_location: Optional[Dict[str, Any]] = None

class RecentLocationRequest(BaseModel):
    user_id: str
    location: Dict[str, Any]


def debug_log(message: str) -> None:
    jlog("API", message)


def _location_has_coordinates(location: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(location, dict):
        return False
    try:
        lat = float(location.get("latitude"))
        lng = float(location.get("longitude"))
        return -90 <= lat <= 90 and -180 <= lng <= 180
    except (TypeError, ValueError):
        return False


def _merge_user_preferences_into_envelope(
    envelope: Optional[Dict[str, Any]],
    user_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Fill missing schedule preferences from persisted user preferences.

    Precedence is preserved: per-day override/schedule values win, persisted
    preferences only fill gaps, and defaults stay in the database helper.
    """
    if envelope is None:
        return None
    updated = deepcopy(envelope)
    prefs = updated.setdefault("preferences", {})
    if not user_id:
        return updated

    persisted = database.get_user_preferences(user_id)
    if not persisted:
        return updated

    if "use_day_boundary_preferences" not in prefs:
        prefs["use_day_boundary_preferences"] = persisted.get("use_day_boundary_preferences", True)

    use_boundaries = bool(prefs.get("use_day_boundary_preferences", True))
    for key in ("day_start_time", "day_end_time"):
        if not prefs.get(key) and persisted.get(key):
            prefs[key] = persisted[key]

    if use_boundaries:
        if not prefs.get("day_start") and prefs.get("day_start_time"):
            prefs["day_start"] = prefs["day_start_time"]
        if not prefs.get("day_end") and prefs.get("day_end_time"):
            prefs["day_end"] = prefs["day_end_time"]

    if not prefs.get("default_start_location") and persisted.get("default_start_location"):
        prefs["default_start_location"] = persisted.get("default_start_location")

    start_location = prefs.get("day_start_location_override") or prefs.get("default_start_location")
    if _location_has_coordinates(start_location):
        label = start_location.get("label") or start_location.get("display_name") or start_location.get("address")
        jlog("DEFAULT_LOCATION", f"start={label or 'configured'}")
    return updated


def extract_shift_target_date(operations: List[Dict[str, Any]]) -> Optional[str]:
    for operation in operations or []:
        if str(operation.get("op") or "").strip().lower() == "shift_plan_date":
            return operation.get("to_date")
    return None

def log_total_token_usage(parsed: Dict[str, Any], reply_meta: Optional[Dict[str, Any]] = None) -> None:
    parser_usage = parsed.get("_token_usage") or {}
    reply_usage = (reply_meta or {}).get("token_usage") or {}
    total_prompt = int(parser_usage.get("prompt", 0) or 0) + int(reply_usage.get("prompt", 0) or 0)
    total_candidates = int(parser_usage.get("candidates", 0) or 0) + int(reply_usage.get("candidates", 0) or 0)
    total = int(parser_usage.get("total", 0) or 0) + int(reply_usage.get("total", 0) or 0)
    if total:
        jlog("API", f"Prompt={total_prompt} | Candidates={total_candidates} | Total={total}", "TOKEN_TOTAL")

def travel_validation_reply_meta(envelope: Dict[str, Any]) -> Dict[str, Any]:
    status = envelope.get("travel_validation_status") or "not_requested"
    requests = envelope.get("location_resolution_requests") or []
    route_conflicts = envelope.get("route_conflicts") or []
    repair_suggestions = envelope.get("pending_repair_suggestions") or []

    if requests:
        return {
            "reply": "Please confirm the exact locations first so I can calculate accurate travel time.",
            "reply_status": "location_pending",
            "reply_reason": "accurate_travel_location_pending",
        }

    if repair_suggestions:
        suggestion = repair_suggestions[0]
        title = suggestion.get("title") or "that activity"
        to_time = suggestion.get("to") or "a later time"
        reason = suggestion.get("reason") or "accurate travel time needs more room"
        return {
            "reply": f"Accurate travel time found a conflict. {title} needs to start around {to_time} because {reason}. Apply this change?",
            "reply_status": "warning",
            "reply_reason": "pending_repair_confirmation",
        }

    if status == "route_conflict" or route_conflicts:
        conflict = route_conflicts[0] if route_conflicts else {}
        reason = conflict.get("reason") or "Accurate travel time creates a timing conflict in the current plan."
        return {
            "reply": f"I checked accurate travel time, but it creates a travel conflict: {reason}",
            "reply_status": "warning",
            "reply_reason": reason,
        }

    if status == "partial_feasible_with_unfit":
        unfit = (envelope.get("unfit_activities") or [{}])[0]
        title = unfit.get("title") or "one flexible activity"
        return {
            "reply": f"Most of the plan is feasible with accurate travel time, but {title} could not fit.",
            "reply_status": "warning",
            "reply_reason": "accurate_travel_partial_feasible_with_unfit",
        }

    if status == "fallback_used":
        return {
            "reply": "I could not get route data right now, so I kept the current schedule or used fallback travel estimates.",
            "reply_status": "warning",
            "reply_reason": "accurate_travel_fallback_used",
        }

    if status == "validated":
        updated = int(envelope.get("updated_transition_count") or 0)
        suffix = f" Updated {updated} travel transition{'s' if updated != 1 else ''}." if updated else ""
        return {
            "reply": f"I updated the plan using accurate travel time.{suffix}",
            "reply_status": "success",
            "reply_reason": "accurate_travel_validated",
        }

    return {
        "reply": "I checked the current plan, but accurate travel validation was not applied.",
        "reply_status": "warning",
        "reply_reason": status,
    }

@app.post("/chat", response_model=ChatResponse)
async def chat_with_llm(request: ChatRequest):
    chat_started = time.perf_counter()

    def elapsed_seconds(started: float) -> str:
        return f"{time.perf_counter() - started:.2f}"

    def log_total_timer() -> None:
        jlog("TIMER", f"total_chat_request_seconds={elapsed_seconds(chat_started)}")

    debug_log(f"Received chat | user={request.user_id} | message={request.message!r}")
    jlog(
        "API",
        f"allow_clash={bool(request.allow_clash)} accurate_travel_time={bool(request.accurate_travel_time)}",
        "FLAGS",
    )
    
    try:
        # Full envelope for internal processing
        current_envelope = request.current_schedule.model_dump() if request.current_schedule else None
        current_envelope = _merge_user_preferences_into_envelope(current_envelope, request.user_id)
        if current_envelope is not None:
            current_envelope["allow_clash"] = bool(request.allow_clash)
            current_envelope["accurate_travel_time"] = bool(request.accurate_travel_time)
            current_envelope.setdefault("preferences", {})
            current_envelope["preferences"]["allow_clash"] = bool(request.allow_clash)
            current_envelope["preferences"]["accurate_travel_time"] = bool(request.accurate_travel_time)

        if current_envelope and current_envelope.get("pending_repair_suggestions"):
            saved_locations_for_repair = database.get_user_locations(request.user_id) if request.user_id else []
            pending_repair = scheduling_engine.handle_pending_repair_confirmation(
                request.message,
                current_envelope,
                saved_locations_for_repair,
            )
            if pending_repair:
                envelope_dict = database._parse_schedule_payload(
                    pending_repair.get("envelope") or current_envelope,
                    request.user_id or "",
                    (pending_repair.get("envelope") or current_envelope).get("date"),
                )
                full_envelope = ScheduleEnvelope(**envelope_dict)
                log_total_timer()
                return ChatResponse(
                    reply=pending_repair.get("reply", "I kept the current schedule unchanged."),
                    full_schedule=full_envelope,
                    schedule_data=full_envelope,
                    transcription=request.message,
                    reply_status=pending_repair.get("reply_status"),
                    reply_reason=pending_repair.get("reply_reason"),
                )

        # Optimization: Create a lightweight context for the LLM
        lightweight_schedule = None
        if request.current_schedule:
            clean_activities = []
            for act in request.current_schedule.activities:
                if act.type != "activity": continue
                clean_activities.append({
                    "id": act.id,
                    "title": act.title,
                    "startTime": act.startTime,
                    "endTime": act.endTime
                })
            lightweight_schedule = {
                "date": request.current_schedule.date,
                "activities": clean_activities,
                "version": request.current_schedule.version,
                "allow_clash": bool(request.allow_clash),
                "accurate_travel_time": bool(request.accurate_travel_time),
            }

        router_started = time.perf_counter()
        route = scheduling_engine.route_chat_request(request.message, current_envelope)
        jlog("TIMER", f"router_seconds={elapsed_seconds(router_started)}")

        if route.get("route") == "general_chat":
            reply_meta = scheduling_engine.compose_general_chat_reply(request.message)
            log_total_timer()
            return ChatResponse(
                reply=reply_meta["reply"],
                transcription=request.message,
                reply_status=reply_meta.get("reply_status"),
            )

        if route.get("route") == "planning_advice":
            advice_started = time.perf_counter()
            reply_meta = scheduling_engine.compose_advisory_reply(
                latest_request=request.message,
                current_schedule=current_envelope,
                allow_clash=bool(request.allow_clash),
                accurate_travel_time=bool(request.accurate_travel_time),
            )
            jlog("TIMER", f"advisory_llm_seconds={elapsed_seconds(advice_started)}")
            log_total_timer()
            return ChatResponse(
                reply=reply_meta["reply"],
                schedule_data=request.current_schedule,
                transcription=request.message,
                reply_status="advice",
            )

        if route.get("route") == "accurate_travel_validation":
            jlog("TRAVEL_REQUEST", "source=chat route=validate_existing_schedule accurate_travel_time=true")
            if not current_envelope:
                log_total_timer()
                return ChatResponse(
                    reply="I need a current schedule before I can calculate accurate travel time.",
                    transcription=request.message,
                    reply_status="clarification_needed",
                    reply_reason="missing_current_schedule",
                )

            saved_locations = database.get_user_locations(request.user_id)
            travel_envelope = _merge_user_preferences_into_envelope(deepcopy(current_envelope), request.user_id) or deepcopy(current_envelope)
            travel_envelope["accurate_travel_time"] = True
            travel_envelope.setdefault("preferences", {})
            travel_envelope["preferences"]["accurate_travel_time"] = True
            schedule_id = travel_envelope.get("scheduleId") or travel_envelope.get("schedule_id") or "(draft)"
            jlog(
                "TRAVEL_VALIDATION",
                f"schedule_id={schedule_id} date={travel_envelope.get('date')}",
                "START",
            )
            validated = scheduling_engine._apply_accurate_travel_if_requested(travel_envelope, saved_locations)
            pending_requests = validated.get("location_resolution_requests") or []
            if pending_requests:
                missing = [
                    request.get("title") or request.get("current_guess") or "activity"
                    for request in pending_requests
                ]
                jlog("TRAVEL_VALIDATION", f"missing={missing}", "LOCATION_PENDING")
            updated_count = int(validated.get("updated_transition_count") or 0)
            if updated_count:
                jlog("TRAVEL_VALIDATION", f"transitions={updated_count}", "UPDATED")
            jlog(
                "TRAVEL_VALIDATION",
                f"status={validated.get('travel_validation_status')}",
                "DONE",
            )

            envelope_dict = database._parse_schedule_payload(validated, request.user_id, validated.get("date"))
            full_envelope = ScheduleEnvelope(**envelope_dict)
            reply_meta = travel_validation_reply_meta(envelope_dict)
            log_total_timer()
            return ChatResponse(
                reply=reply_meta["reply"],
                full_schedule=full_envelope,
                schedule_data=full_envelope,
                transcription=request.message,
                reply_status=reply_meta.get("reply_status"),
                reply_reason=reply_meta.get("reply_reason"),
            )

        # Fetch saved locations for the user to provide context to parsing/location normalization.
        saved_locations = database.get_user_locations(request.user_id)

        module_a_started = time.perf_counter()
        parsed = None
        fast_path_fallback_reason = None
        if route.get("use_deterministic_parser") and not route.get("use_module_a_llm"):
            parsed = scheduling_engine.parse_deterministic_fast_path(
                latest_request=request.message,
                current_schedule=lightweight_schedule,
                history=request.history,
                saved_locations=saved_locations,
            )
            if parsed is None:
                fast_path_fallback_reason = getattr(scheduling_engine, "_last_fast_path_fallback_reason", None)
                jlog("ROUTER", "fast_path_failed falling_back_to_module_a_llm", "FALLBACK")
        if parsed is None:
            parse_kwargs = {
                "message": request.message,
                "history": request.history,
                "current_schedule": lightweight_schedule,
                "saved_locations": saved_locations,
            }
            if fast_path_fallback_reason:
                parse_kwargs["disable_deterministic_fallback"] = True
                parse_kwargs["fallback_reason"] = fast_path_fallback_reason
            elif (
                route.get("route") == "complex_schedule_command"
                or route.get("travel_intent")
                or route.get("reason") in {"multi_activity_generation", "multi_activity_redesign", "natural_edit_wording"}
            ):
                parse_kwargs["disable_deterministic_fallback"] = True
                parse_kwargs["fallback_reason"] = "complex_or_travel_intent"
            parsed = scheduling_engine.parse_text_request(**parse_kwargs)
        jlog("TIMER", f"module_a_total_seconds={elapsed_seconds(module_a_started)}")
        parsed.setdefault("preferences", {})
        persisted_preferences = database.get_user_preferences(request.user_id) if request.user_id else {}
        for key in ("day_start_time", "day_end_time", "use_day_boundary_preferences", "default_start_location"):
            if persisted_preferences.get(key) is not None and key not in parsed["preferences"]:
                parsed["preferences"][key] = persisted_preferences.get(key)
        if parsed["preferences"].get("use_day_boundary_preferences", True):
            if not parsed["preferences"].get("day_start") and parsed["preferences"].get("day_start_time"):
                parsed["preferences"]["day_start"] = parsed["preferences"]["day_start_time"]
            if not parsed["preferences"].get("day_end") and parsed["preferences"].get("day_end_time"):
                parsed["preferences"]["day_end"] = parsed["preferences"]["day_end_time"]
        parsed["preferences"]["allow_clash"] = bool(request.allow_clash)
        travel_intent = bool(
            parsed["preferences"].get("travel_intent")
            or route.get("travel_intent")
        )
        parsed["preferences"]["travel_intent"] = travel_intent
        parsed["preferences"]["accurate_travel_time"] = bool(request.accurate_travel_time or travel_intent)
        parsed["preferences"]["module_0_route"] = route.get("route")
        parsed["preferences"]["module_0_reason"] = route.get("reason")
        parsed["preferences"]["latest_request"] = request.message
        if current_envelope is not None:
            current_envelope.setdefault("preferences", {})
            current_envelope["preferences"]["travel_intent"] = bool(
                current_envelope["preferences"].get("travel_intent")
                or parsed["preferences"].get("travel_intent")
            )
        
        intent = parsed.get("intent", "chat")
        reply = parsed.get("reply", "I've processed your request.")
        if intent == "no_operation":
            log_total_token_usage(parsed)
            log_total_timer()
            return ChatResponse(
                reply=reply,
                transcription=parsed.get("transcription"),
                reply_status="clarification_needed",
                recommend_allow_clash=False,
                reply_reason=parsed.get("_failure_type") or "no_operation",
            )
        
        if intent == "chat" and not parsed.get("operations") and not parsed.get("activities"):
            failure_type = parsed.get("_failure_type")
            parser_failure_types = {
                "module_a_timeout",
                "module_a_unavailable",
                "module_a_executor_saturated",
                "llm_call_error",
                "llm_parse_error",
            }
            reply_status = "error" if failure_type in parser_failure_types else "chat"
            log_total_token_usage(parsed)
            log_total_timer()
            return ChatResponse(
                reply=reply,
                transcription=parsed.get("transcription"),
                reply_status=reply_status,
                reply_reason=failure_type if reply_status == "error" else None,
            )

        if request.current_schedule and (parsed.get("operations") or parsed.get("activities")):
            try:
                ops = parsed.get("operations") or []
                if not ops and parsed.get("activities"):
                    for act in parsed.get("activities"):
                        ops.append({**act, "op": "add"})
                ops = [
                    {
                        **op,
                        "_user_message": request.message,
                        "_latest_request": request.message,
                        "_transcription": parsed.get("transcription"),
                        "_router_route": route.get("route"),
                        "_router_reason": route.get("reason"),
                    }
                    for op in ops
                ]

                target_date_envelope = None
                shift_target_date = extract_shift_target_date(ops)
                if request.user_id and shift_target_date and shift_target_date != request.current_schedule.date:
                    target_date_envelope = database.get_plan_by_date(shift_target_date, request.user_id)
                
                apply_started = time.perf_counter()
                patch_result = scheduling_engine.apply_operations(
                    envelope=current_envelope,
                    operations=ops,
                    base_version=request.current_schedule.version,
                    new_date=parsed.get("date"),
                    target_date_envelope=target_date_envelope,
                    saved_locations=saved_locations,
                )
                jlog("TIMER", f"apply_operations_seconds={elapsed_seconds(apply_started)}")
                
                module_8_started = time.perf_counter()
                reply_meta = scheduling_engine.compose_result_reply(
                    latest_request=request.message,
                    parsed={**parsed, "operations": ops},
                    result=patch_result,
                    allow_clash=bool(request.allow_clash),
                )
                jlog("TIMER", f"module_8_total_seconds={elapsed_seconds(module_8_started)}")
                final_reply = reply_meta["reply"]
                log_total_token_usage(parsed, reply_meta)
                log_total_timer()

                return ChatResponse(
                    reply=final_reply,
                    patch=SchedulePatchResponse(
                        scheduleId=patch_result["envelope"].get("scheduleId") or "temp-id",
                        version=patch_result["version"],
                        applied=bool(patch_result.get("applied", True)),
                        updatedActivities=patch_result["updatedActivities"],
                        deletedItemIds=patch_result["deletedItemIds"],
                        explanation=final_reply
                    ),
                    schedule_data=patch_result["envelope"],
                    transcription=parsed.get("transcription"),
                    reply_status=reply_meta.get("reply_status"),
                    recommend_allow_clash=bool(reply_meta.get("recommend_allow_clash")),
                    reply_reason=reply_meta.get("reply_reason"),
                    llm_fallback_reason=reply_meta.get("llm_fallback_reason"),
                )
            except VersionMismatchError:
                debug_log("Version mismatch during patch, fallback to full schedule.")

        schedule_build_started = time.perf_counter()
        result = scheduling_engine.build_schedule_response(
            parsed=parsed,
            current_schedule=current_envelope,
            latest_request=request.message,
            saved_locations=saved_locations,
        )
        jlog("TIMER", f"schedule_build_seconds={elapsed_seconds(schedule_build_started)}")
        
        schedule_dict = result.get("schedule_data")
        if schedule_dict:
            module_8_started = time.perf_counter()
            reply_meta = scheduling_engine.compose_result_reply(
                latest_request=request.message,
                parsed=parsed,
                result=result,
                allow_clash=bool(request.allow_clash),
            )
            jlog("TIMER", f"module_8_total_seconds={elapsed_seconds(module_8_started)}")
            log_total_token_usage(parsed, reply_meta)
            full_envelope_dict = database._parse_schedule_payload(schedule_dict, request.user_id, schedule_dict.get("date"))
            full_envelope = ScheduleEnvelope(**full_envelope_dict)
            log_total_timer()
            return ChatResponse(
                reply=reply_meta["reply"],
                full_schedule=full_envelope,
                schedule_data=full_envelope,
                transcription=result.get("transcription"),
                reply_status=reply_meta.get("reply_status"),
                recommend_allow_clash=bool(reply_meta.get("recommend_allow_clash")),
                reply_reason=reply_meta.get("reply_reason"),
                llm_fallback_reason=reply_meta.get("llm_fallback_reason"),
            )
        
        log_total_token_usage(parsed)
        log_total_timer()
        return ChatResponse(
            reply=result.get("reply", reply), 
            schedule_data=None,
            transcription=result.get("transcription")
        )
        
    except Exception as e:
        import traceback
        traceback.print_exc() 
        log_total_timer()
        return ChatResponse(reply=f"Sorry, I encountered an error: {str(e)}")

@app.post("/api/schedules/{scheduleId}/operations", response_model=SchedulePatchResponse)
async def apply_operations_endpoint(scheduleId: str, request: SchedulePatchRequest):
    """Apply a batch of operations to a schedule."""
    try:
        plans = database.get_all_plans(request.user_id)
        current_plan = next((p for p in plans if p.get("scheduleId") == scheduleId), None)
        
        if not current_plan:
            raise HTTPException(status_code=404, detail="Schedule not found")
        
        ops = [op.model_dump() for op in request.operations]
        target_date_envelope = None
        shift_target_date = extract_shift_target_date(ops)
        if shift_target_date and shift_target_date != current_plan.get("date"):
            target_date_envelope = database.get_plan_by_date(shift_target_date, request.user_id)
        saved_locations = database.get_user_locations(request.user_id)
        result = scheduling_engine.apply_operations(
            envelope=current_plan,
            operations=ops,
            base_version=request.baseVersion,
            target_date_envelope=target_date_envelope,
            saved_locations=saved_locations,
        )
        
        updated_envelope = result["envelope"]
        database.save_plan(
            date=updated_envelope["date"],
            activities=updated_envelope["activities"],
            schedule_blocks=updated_envelope.get("schedule_blocks"),
            user_id=request.user_id,
            explanations=updated_envelope["explanations"],
            unscheduled_activities=updated_envelope["unscheduled_activities"],
            version=updated_envelope["version"],
            schedule_id=updated_envelope["scheduleId"],
            preferences=updated_envelope.get("preferences"),
            status=updated_envelope.get("status", "ok"),
            conflict=updated_envelope.get("conflict"),
            planning_mode=updated_envelope.get("planning_mode", "feasibility_first"),
            allow_clash=bool(updated_envelope.get("allow_clash", False)),
            conflicts=updated_envelope.get("conflicts"),
            warnings=updated_envelope.get("warnings"),
            schedule_status=updated_envelope.get("schedule_status"),
            accurate_travel_time=bool(updated_envelope.get("accurate_travel_time", False)),
            travel_validation_status=updated_envelope.get("travel_validation_status"),
            location_resolution_requests=updated_envelope.get("location_resolution_requests"),
            route_conflicts=updated_envelope.get("route_conflicts"),
            pending_repair_suggestions=updated_envelope.get("pending_repair_suggestions"),
            unfit_activities=updated_envelope.get("unfit_activities"),
            route_repair_actions=updated_envelope.get("route_repair_actions"),
            route_efficiency=updated_envelope.get("route_efficiency"),
            route_total_before=updated_envelope.get("route_total_before"),
            route_total_after=updated_envelope.get("route_total_after"),
            route_minutes_saved=updated_envelope.get("route_minutes_saved"),
            location_revisits_count=updated_envelope.get("location_revisits_count"),
            same_location_split_penalty_before=updated_envelope.get("same_location_split_penalty_before"),
            same_location_split_penalty_after=updated_envelope.get("same_location_split_penalty_after"),
            revisit_penalty_before=updated_envelope.get("revisit_penalty_before"),
            revisit_penalty_after=updated_envelope.get("revisit_penalty_after"),
            start_route_summary=updated_envelope.get("start_route_summary"),
            unmet_items=updated_envelope.get("unmet_items"),
            validation_issues=updated_envelope.get("validation_issues"),
        )
        
        return SchedulePatchResponse(
            scheduleId=scheduleId,
            version=updated_envelope["version"],
            applied=bool(result.get("applied", True)),
            updatedActivities=result["updatedActivities"],
            deletedItemIds=result["deletedItemIds"]
        )
        
    except VersionMismatchError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/plans", response_model=List[ScheduleEnvelope])
async def get_all_plans(user_id: str):
    """Get all saved plans for a user as envelopes"""
    try:
        plans = database.get_all_plans(user_id)
        results = []
        for p in plans:
            # Normalize database data to Envelope
            p["activities"] = p.get("activities") or p.get("items") or []
            p["unscheduled_activities"] = p.get("unscheduled_activities") or []
            results.append(ScheduleEnvelope(**p))
        return results
    except Exception as e:
        jlog("API", f"Error in get_all_plans: {e}", "ERROR")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/plans/{date}", response_model=ScheduleEnvelope)
async def get_plan_by_date(date: str, user_id: str):
    """Get plan for a specific date and user as envelope"""
    try:
        plan = database.get_plan_by_date(date, user_id)
        if plan is None:
            raise HTTPException(status_code=404, detail=f"No plan found for {date}")
        
        # Normalize database data to Envelope
        plan["activities"] = plan.get("activities") or plan.get("items") or []
        plan["unscheduled_activities"] = plan.get("unscheduled_activities") or []
        plan["schedule_blocks"] = plan.get("schedule_blocks") or []
        
        return ScheduleEnvelope(**plan)
    except HTTPException:
        raise
    except Exception as e:
        jlog("API", f"Error in get_plan_by_date: {e}", "ERROR")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/plans", response_model=ScheduleEnvelope)
async def save_plan_endpoint(plan: PlanRequest):
    """Save or update a plan (Full-Save compatibility)"""
    try:
        saved_plan = database.save_plan(
            date=plan.date,
            activities=[act.model_dump() for act in plan.activities],
            schedule_blocks=[act.model_dump() for act in (plan.schedule_blocks or [])],
            user_id=plan.user_id,
            explanations=plan.explanations,
            unscheduled_activities=[act.model_dump() for act in plan.unscheduled_activities],
            version=plan.version,
            schedule_id=plan.scheduleId,
            preferences={
                **(plan.preferences or {}),
                "allow_clash": bool(plan.allow_clash),
                "planning_mode": plan.planning_mode,
                "accurate_travel_time": bool(plan.accurate_travel_time),
            },
            status=plan.status or "ok",
            schedule_status=plan.schedule_status or plan.status or "ok",
            travel_validation_status=plan.travel_validation_status or "not_requested",
            conflict=plan.conflict,
            planning_mode=plan.planning_mode,
            allow_clash=bool(plan.allow_clash),
            accurate_travel_time=bool(plan.accurate_travel_time),
            conflicts=plan.conflicts,
            warnings=plan.warnings,
            location_resolution_requests=plan.location_resolution_requests,
            route_conflicts=plan.route_conflicts,
            pending_repair_suggestions=plan.pending_repair_suggestions,
            unfit_activities=plan.unfit_activities,
            route_repair_actions=plan.route_repair_actions,
            route_efficiency=plan.route_efficiency,
            route_total_before=plan.route_total_before,
            route_total_after=plan.route_total_after,
            route_minutes_saved=plan.route_minutes_saved,
            location_revisits_count=plan.location_revisits_count,
            same_location_split_penalty_before=plan.same_location_split_penalty_before,
            same_location_split_penalty_after=plan.same_location_split_penalty_after,
            revisit_penalty_before=plan.revisit_penalty_before,
            revisit_penalty_after=plan.revisit_penalty_after,
            start_route_summary=plan.start_route_summary,
            unmet_items=plan.unmet_items,
            validation_issues=plan.validation_issues,
        )
        return ScheduleEnvelope(**saved_plan)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/sync-calendar")
async def sync_calendar(request: Dict[str, Any]):
    user_id = request.get("user_id")
    date = request.get("date") # Optional target date
    
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id is required")
    
    try:
        # 1. Fetch upcoming from Google (next 60 days)
        grouped_events = cal_service.sync_upcoming_events(user_id)
        
        if not grouped_events:
            return {"events": [], "message": "No upcoming events found or sync failed"}

        all_synced_days = []
        target_date_events = []

        # 2. Process each date
        for event_date, google_events in grouped_events.items():
            # Get existing plan (envelope)
            existing_plan = database.get_plan_by_date(event_date, user_id)
            
            # Map to JPlan format
            new_activities = []
            for ge in google_events:
                new_activities.append({
                    "id": ge.get("id"),
                    "type": "activity",
                    "title": ge.get("activity"),
                    "startTime": ge.get("startTime"),
                    "endTime": ge.get("endTime"),
                    "category": "External",
                    "source": "google_calendar"
                })

            final_activities = []
            if existing_plan:
                # Envelope uses 'activities'
                existing_activities = existing_plan.get("activities", [])
                
                # Build fingerprints for existing activities: Title|Start|End
                def get_fingerprint(a):
                    title = str(a.get("title", "")).strip().lower()
                    start = str(a.get("startTime", "")).strip().lower()
                    end = str(a.get("endTime", "")).strip().lower()
                    
                    if start.startswith("0"): start = start[1:]
                    if end.startswith("0"): end = end[1:]
                    
                    return f"{title}|{start}|{end}"
                
                existing_ids = {a.get("id") for a in existing_activities if a.get("id")}
                existing_fingerprints = {get_fingerprint(a) for a in existing_activities}
                
                merged = list(existing_activities)
                for na in new_activities:
                    if na.get("id") not in existing_ids and get_fingerprint(na) not in existing_fingerprints:
                        merged.append(na)
                final_activities = merged
            else:
                final_activities = new_activities

            # Save to DB
            database.save_plan(event_date, final_activities, user_id)
            all_synced_days.append(event_date)
            
            if event_date == date:
                target_date_events = final_activities

        return {
            "synced_days": all_synced_days,
            "message": f"Successfully synced events for {len(all_synced_days)} days",
            "events": target_date_events if date else []
        }
        
    except Exception as e:
        error_msg = str(e)
        jlog("API", f"Sync error: {error_msg}", "CALENDAR")
        if "TOKEN_EXPIRED" in error_msg:
            try:
                database.supabase.table('profiles').update({
                    'google_refresh_token': None, 
                    'calendar_sync_enabled': False
                }).eq('id', user_id).execute()
            except Exception as db_err:
                jlog("API", f"Failed to clear token: {db_err}", "CALENDAR")
            raise HTTPException(status_code=401, detail="TOKEN_EXPIRED")
        raise HTTPException(status_code=500, detail=error_msg)


@app.post("/api/export-calendar")
async def export_calendar(request: ExportRequest):
    if not request.user_id or not request.date:
        raise HTTPException(status_code=400, detail="user_id and date are required")
    
    try:
        # Get existing plan (envelope)
        plan = database.get_plan_by_date(request.date, request.user_id)
        if not plan or not plan.get("activities"):
            raise HTTPException(status_code=404, detail="No activities found for this date")
            
        count = cal_service.export_schedule_to_google(request.user_id, request.date, plan.get("activities"))
            
        return {"message": "Success", "exported_count": count}
    except Exception as e:
        error_msg = str(e)
        jlog("API", f"Export error: {error_msg}", "CALENDAR")
        if "TOKEN_EXPIRED" in error_msg:
            try:
                database.supabase.table('profiles').update({
                    'google_refresh_token': None, 
                    'calendar_sync_enabled': False
                }).eq('id', request.user_id).execute()
            except Exception as db_err:
                jlog("API", f"Failed to clear token: {db_err}", "CALENDAR")
            raise HTTPException(status_code=401, detail="TOKEN_EXPIRED")
        
        if "403" in error_msg or "insufficientPermissions" in error_msg:
            raise HTTPException(status_code=401, detail="TOKEN_EXPIRED")
            
        raise HTTPException(status_code=500, detail=error_msg)

# --- Location Management ---

@app.get("/api/preferences")
async def get_preferences(user_id: str):
    return database.get_user_preferences(user_id)

@app.post("/api/preferences")
async def save_preferences(request: UserPreferencesRequest):
    if request.default_start_location is not None and not _location_has_coordinates(request.default_start_location):
        raise HTTPException(status_code=400, detail="Default start location must include latitude and longitude")
    try:
        return database.save_user_preferences(
            user_id=request.user_id,
            day_start_time=request.day_start_time,
            day_end_time=request.day_end_time,
            use_day_boundary_preferences=bool(request.use_day_boundary_preferences),
            default_start_location=request.default_start_location,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.get("/api/recent-locations")
async def get_recent_locations(user_id: str):
    return database.get_user_recent_locations(user_id)

@app.post("/api/recent-locations")
async def add_recent_location(request: RecentLocationRequest):
    if not _location_has_coordinates(request.location):
        raise HTTPException(status_code=400, detail="Recent location must include latitude and longitude")
    try:
        return database.upsert_user_recent_location(request.user_id, request.location)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.get("/api/locations")
async def get_locations(user_id: str):
    return database.get_user_locations(user_id)

@app.post("/api/locations")
async def add_location(user_id: str, label: str, address: str, lat: float = None, lng: float = None):
    return database.add_user_location(user_id, label, address, lat, lng)

@app.get("/api/locations/geocode")
async def geocode_location(query: str, category: Optional[str] = None):
    expanded = travel_service.expand_alias(query, category)
    try:
        jlog("LOCATION_SEARCH", f"query={expanded}", "ORS")
        geocode_result = travel_service.geocode_candidates_with_metadata(expanded, category=category, limit=5)
        if "nominatim" in (geocode_result.get("providers_used") or []):
            jlog("LOCATION_SEARCH", f"query={expanded} reason=ors_no_result", "NOMINATIM")
        return {
            "query": query,
            "expanded_query": expanded,
            **geocode_result,
        }
    except Exception as exc:
        warning = f"Geocoding unavailable: {exc}"
        return {
            "query": query,
            "expanded_query": expanded,
            "candidates": [],
            "geocode_status": "fallback_unavailable",
            "providers_used": [],
            "warnings": [warning],
            "warning": warning,
        }

@app.post("/api/locations/resolve")
async def resolve_location(request: LocationResolveRequest):
    lat = request.latitude
    lng = request.longitude
    display_name = request.display_name or request.label
    address = request.address
    source = request.source or "ors_geocoded"

    if lat is None or lng is None:
        expanded = travel_service.expand_alias(address or request.label, request.category)
        candidates = travel_service.geocode_candidates(expanded, limit=5)
        if not candidates:
            raise HTTPException(status_code=400, detail="No geocoding candidates found for this location")
        return {
            "requires_confirmation": True,
            "expanded_query": expanded,
            "candidates": candidates,
        }

    return database.add_user_location(
        request.user_id,
        request.label,
        address,
        lat,
        lng,
        display_name=display_name,
        source=source,
        confirmed_by_user=request.confirmed_by_user,
    )

@app.post("/api/travel/complete", response_model=ScheduleEnvelope)
async def complete_travel_validation(request: TravelCompleteRequest):
    envelope = _merge_user_preferences_into_envelope(request.schedule.model_dump(), request.user_id) or request.schedule.model_dump()
    envelope["accurate_travel_time"] = True
    envelope.setdefault("preferences", {})
    envelope["preferences"]["accurate_travel_time"] = True
    schedule_id = envelope.get("scheduleId") or envelope.get("schedule_id") or "(draft)"
    jlog(
        "TRAVEL_REQUEST",
        f"source={request.source or 'manual'} route=validate_existing_schedule accurate_travel_time=true",
    )
    jlog(
        "TRAVEL_SERVICE",
        f"Starting accurate travel validation schedule_id={schedule_id} date={envelope.get('date')}",
        "COMPLETE",
    )
    saved_locations = database.get_user_locations(request.user_id)
    validated = scheduling_engine._apply_accurate_travel_if_requested(envelope, saved_locations)
    jlog(
        "TRAVEL_SERVICE",
        f"travel_validation_status={validated.get('travel_validation_status')}",
        "VALIDATION",
    )
    jlog(
        "TRAVEL_SERVICE",
        f"Updated transition blocks={validated.get('updated_transition_count', 0)}",
        "COMPLETE",
    )
    return ScheduleEnvelope(**validated)

@app.delete("/api/locations")
async def delete_location(user_id: str, label: str):
    return {"success": database.delete_user_location(user_id, label)}
