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

# Constants for the Rich Scheduling Model
class TimingMode:
    FIXED = "fixed"          # Specific HH:MM requested
    PREFERRED = "preferred"  # Around HH:MM
    RELATIVE = "relative"    # After/Before another activity
    WINDOW = "window"        # Between T1 and T2
    UNSPECIFIED = "unspecified"

class BlockType:
    ACTIVITY = "activity"
    TRANSITION = "transition" # Travel or Prep
    BUFFER = "buffer"         # Idle time

DEFAULT_DAY_START = 8 * 60  # 8:00 AM
DEFAULT_DAY_END = 22 * 60   # 10:00 PM
DEFAULT_DURATION = 60
DEFAULT_PREP_BUFFER = 5
DEFAULT_LOCAL_TIMEZONE = "Asia/Kuala_Lumpur"
MAX_HISTORY_TURNS = 4
MAX_HISTORY_MESSAGE_CHARS = 160

PRIORITY_WEIGHT = {
    "low": 1,
    "medium": 2,
    "high": 3,
}

PLANNING_MODE_FEASIBILITY = "feasibility_first"
PLANNING_MODE_CLASH = "clash_allowed"
WARNING_TIGHT_TRANSITION = "TIGHT_TRANSITION"
PARSER_RETRY_DELAYS_SECONDS = (0.4, 0.8)

MONTH_NAME_TO_NUMBER = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

CONFLICT_SEVERITY_RULES = {
    "fixed-vs-fixed": "critical",
    "fixed-vs-mandatory": "high",
    "fixed-vs-optional": "medium",
    "optional-vs-optional": "low",
    "travel-risk": "medium",
    "user-forced": "high",
}

FIXED_EVENT_KEYWORDS = {
    "appointment",
    "class",
    "exam",
    "flight",
    "interview",
    "lecture",
    "meeting",
    "presentation",
    "seminar",
    "train",
    "workshop",
}

PREFERRED_EXACT_KEYWORDS = {
    "assignment",
    "coding",
    "fyp",
    "groceries",
    "grocery",
    "gym",
    "implementation",
    "shopping",
    "study",
    "workout",
}

SOCIAL_OR_BOOKED_KEYWORDS = {
    "appointment",
    "booked",
    "booking",
    "client",
    "date",
    "doctor",
    "friend",
    "girl",
    "girlfriend",
    "reservation",
    "reserved",
    "team",
    "trainer",
    "with",
}

NULL_TEXT_VALUES = {"", "null", "none", "n/a", "na", "nil", "undefined"}

GENERIC_SYSTEM_ACTIVITY_TITLES = {
    "buffer",
    "commute",
    "commuting",
    "free time",
    "idle",
    "prep",
    "prep buffer",
    "prep / buffer",
    "transit",
    "transition",
    "travel",
    "travel time",
}

GENERIC_SYSTEM_ACTIVITY_TYPES = {
    "buffer",
    "free time",
    "idle",
    "transit",
    "transition",
    "travel",
}

PARSER_PROMPT = """
You are the parsing layer for JPlan. Convert user requests into structured operations.
Return ONLY ONE JSON object with: intent, reply, transcription, date, operations, conflict_analysis.

OPERATIONS SCHEMA:
{ 
  "op": "add|remove|move|update|replace|shift_plan_date", 
  "title": "str", 
  "timing_mode": "fixed|relative|flexible",
  "fixed_start": "HH:MM (only if user said exact time)",
  "anchor_relation": {
    "kind": "after",
    "target_title": "str"
  },
  "sequence_index": int,
  "duration_minutes": int, 
  "priority": "low|medium|high",
  "location": "str",
  "notes": "str"
}

EXAMPLE RESPONSE:
{
  "intent": "edit",
  "reply": "I've scheduled your meeting at MMU, followed by lunch at home, then your gym session.",
  "operations": [
    {
      "op": "add",
      "title": "Project Meeting",
      "timing_mode": "fixed",
      "fixed_start": "10:00",
      "duration_minutes": 120,
      "location": "school",
      "sequence_index": 1
    },
    {
      "op": "add",
      "title": "Lunch",
      "timing_mode": "relative",
      "anchor_relation": { "kind": "after", "target_title": "Project Meeting" },
      "location": "home",
      "sequence_index": 2
    },
    {
      "op": "add",
      "title": "Gym Session",
      "timing_mode": "fixed",
      "fixed_start": "16:00",
      "location": "gym",
      "sequence_index": 3
    }
  ]
}

TIMING RULES:
1. FIXED: Only if user says "at 4pm" or "from 10am to 12pm".
2. RELATIVE: If user says "then", "after that", or "followed by". Set kind="after" and target_title.
3. PREFERRED: If user says "around 12pm" or "hopefully by 5".
4. LOCATION: Cross-reference with SAVED_LOCATIONS. Return the 'label'.

STRICT RULES:
1. Max 3 words for titles.
2. LOCATION: Always specify a location if possible. Use 'home' for lunch/dinner, 'school' or 'office' for meetings, etc. Set to null only if location is irrelevant.
3. Use 24H format for all times.
4. DURATIONS: Use reasonable defaults if not mentioned (e.g. Lunch = 60).
5. If intent is "chat", leave operations empty.
6. If the user wants to move the whole plan to another date, use op="shift_plan_date" with from_date, to_date, and scope="all_active_activities".
7. Do NOT create operations for generic travel, transit, prep, buffer, free-time, or idle blocks. If the user says to account for travel time, keep only the real activities and their locations; the scheduler will add travel blocks automatically.
8. Keep real travel activities only when they are concrete user commitments, such as "Flight to KL", "Train Ride", "Road Trip", or "Airport Transfer".
"""


def parse_clock(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(".", ":").upper()

    for fmt in (
        r"^(?P<h>\d{1,2}):(?P<m>\d{2})\s*(?P<ampm>AM|PM)$",
        r"^(?P<h>\d{1,2})\s*(?P<ampm>AM|PM)$",
        r"^(?P<h>\d{1,2}):(?P<m>\d{2})$",
        r"^(?P<h>\d{1,2})$",
    ):
        match = re.match(fmt, text)
        if not match:
            continue
        hour = int(match.group("h"))
        minute = int(match.groupdict().get("m") or 0)
        ampm = match.groupdict().get("ampm")
        if ampm:
            hour = hour % 12
            if ampm == "PM":
                hour += 12
        if 0 <= hour < 24 and 0 <= minute < 60:
            return hour * 60 + minute
    return None


def format_clock(minutes: int) -> str:
    normalized = minutes % (24 * 60)
    hour = normalized // 60
    minute = normalized % 60
    suffix = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12:02d}:{minute:02d} {suffix}"


def parse_duration_minutes(value: Any, minimum: int = 15) -> int:
    if value is None:
        return DEFAULT_DURATION
    if isinstance(value, (int, float)):
        return max(minimum, int(value))

    text = str(value).strip().lower()
    if not text:
        return DEFAULT_DURATION
    if text.isdigit():
        return max(minimum, int(text))

    hours = 0
    minutes = 0
    hour_match = re.search(r"(\d+)\s*h", text)
    minute_match = re.search(r"(\d+)\s*m", text)
    if hour_match:
        hours = int(hour_match.group(1))
    if minute_match:
        minutes = int(minute_match.group(1))

    if hours or minutes:
        return max(minimum, hours * 60 + minutes)
    return DEFAULT_DURATION

def clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip()).lower()

def clean_optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if clean_title(text) in NULL_TEXT_VALUES:
        return None
    return text


# [Module B/C Knowledge] Mock Database for Locations and Travel Times
LOCATION_ALIASES = {
    "home": "Home",
    "house": "Home",
    "office": "Main Office",
    "main office": "Main Office",
    "campus": "MMU",
    "school": "MMU",
    "near campus": "MMU",
    "campus area": "MMU",
    "mmu": "MMU",
    "university": "MMU",
    "gym": "Fitness Center",
    "fitness center": "Fitness Center",
    "store": "Store",
    "supermarket": "Store",
    "market": "Store",
}

# Distance matrix (in minutes) between normalized location names
MOCK_DISTANCE_MATRIX = {
    "Home": {
        "Main Office": 30,
        "MMU": 20,
        "Fitness Center": 15
    },
    "Main Office": {
        "Home": 35, # Asymmetric (maybe traffic?)
        "MMU": 45,
        "Fitness Center": 20
    },
    "MMU": {
        "Home": 25,
        "Main Office": 40,
        "Fitness Center": 30
    },
    "Fitness Center": {
        "Home": 15,
        "Main Office": 20,
        "MMU": 30,
        "Store": 15
    },
    "Store": {
        "Home": 20,
        "Main Office": 20,
        "MMU": 20,
        "Fitness Center": 15
    }
}

def _normalize_location(raw_name: Optional[str]) -> Optional[str]:
    raw_name = clean_optional_text(raw_name)
    if not raw_name: return None
    clean = clean_title(raw_name)
    return LOCATION_ALIASES.get(clean, raw_name.strip())

def estimate_travel_minutes(previous_location: Optional[str], next_location: Optional[str]) -> int:
    """
    [Module B] Estimate travel time between two locations.
    Uses the mock matrix if both locations are known, otherwise defaults to 20m.
    """
    if not previous_location or not next_location:
        return 0
    
    loc1 = _normalize_location(previous_location)
    loc2 = _normalize_location(next_location)

    if loc1 == loc2:
        return 0
    
    # Try to look up in the matrix
    if loc1 in MOCK_DISTANCE_MATRIX and loc2 in MOCK_DISTANCE_MATRIX[loc1]:
        travel_time = MOCK_DISTANCE_MATRIX[loc1][loc2]
        jlog("MODULE_B", f"Found heuristic travel time: '{loc1}' -> '{loc2}' = {travel_time}m", "TRAVEL")
        return travel_time
    
    # Fallback to default travel time
    return 20


class SchedulingEngine:
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
                    if not operation.get("location"):
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

    def _parse_request(
        self,
        latest_request: str,
        history: List[Dict[str, Any]],
        current_schedule: Optional[Dict[str, Any]],
        audio_part: Any,
        saved_locations: List[Dict[str, Any]] = []
    ) -> Dict[str, Any]:
        jsection("MODULE_A", "LLM parsing", "PARSE")
        jlog("MODULE_A", f"Request={latest_request!r}", "PARSE")
        
        # Inject Saved Locations as context for the LLM
        loc_context = ""
        if saved_locations:
            loc_context = "\nSAVED_LOCATIONS:\n" + json.dumps(saved_locations, indent=2)

        prompt = self._build_parser_prompt(latest_request, history, current_schedule)
        if loc_context:
            prompt += loc_context

        contents: Any = prompt if audio_part is None else [prompt, audio_part]
        raw_llm_reply: Optional[str] = None
        raw_response_text: Optional[str] = None

        try:
            response = self._generate_parser_content_with_retry(contents)
            raw_response_text = response.text or ""
            
            # Print Token Usage
            usage = getattr(response, "usage_metadata", None)
            token_usage = None
            if usage:
                token_usage = {
                    "prompt": int(getattr(usage, "prompt_token_count", 0) or 0),
                    "candidates": int(getattr(usage, "candidates_token_count", 0) or 0),
                    "total": int(getattr(usage, "total_token_count", 0) or 0),
                }
                jlog(
                    "MODULE_A",
                    f"Prompt={usage.prompt_token_count} | Candidates={usage.candidates_token_count} | Total={usage.total_token_count}",
                    "TOKEN",
                )
            
            parsed = self._safe_json_loads(raw_response_text)
            if isinstance(parsed, dict):
                raw_llm_reply = str(parsed.get("reply") or "").strip() or None
        except json.JSONDecodeError as exc:
            self._debug(f"LLM parse exception | type={type(exc).__name__} | message={str(exc)}")
            fallback = self._deterministic_fallback_parse(latest_request, current_schedule)
            if fallback:
                self._debug_json("Deterministic fallback parse result", fallback)
                return fallback
            invalid = self._invalid_llm_parse(
                latest_request=latest_request,
                current_schedule=current_schedule,
                raw_llm_reply=raw_llm_reply,
                failure_type="llm_parse_error",
                failure_message=str(exc),
                raw_response_text=raw_response_text,
            )
            self._debug_json("Invalid LLM parse result", invalid)
            return invalid
        except Exception as exc:
            self._debug(f"LLM call exception | type={type(exc).__name__} | message={str(exc)}")
            fallback = self._deterministic_fallback_parse(latest_request, current_schedule)
            if fallback:
                self._debug_json("Deterministic fallback parse result", fallback)
                return fallback
            invalid = self._invalid_llm_parse(
                latest_request=latest_request,
                current_schedule=current_schedule,
                raw_llm_reply=raw_llm_reply,
                failure_type="llm_call_error",
                failure_message=str(exc),
                raw_response_text=raw_response_text,
            )
            self._debug_json("Invalid LLM parse result", invalid)
            return invalid

        if not isinstance(parsed, dict):
            fallback = self._deterministic_fallback_parse(latest_request, current_schedule)
            if fallback:
                self._debug_json("Deterministic fallback parse result", fallback)
                return fallback
            invalid = self._invalid_llm_parse(
                latest_request=latest_request,
                current_schedule=current_schedule,
                raw_llm_reply=raw_llm_reply,
                failure_type="llm_parse_error",
                failure_message="LLM did not return a JSON object.",
                raw_response_text=raw_response_text,
            )
            self._debug_json("Invalid LLM parse result", invalid)
            return invalid

        parsed.setdefault("intent", "schedule")
        parsed.setdefault("reply", "I translated your request into a plan draft.")
        parsed.setdefault("transcription", latest_request)
        parsed.setdefault("activities", [])
        parsed.setdefault("operations", [])
        parsed.setdefault("preferences", {})
        parsed["_reply_source"] = "llm"
        parsed["_llm_reply"] = raw_llm_reply
        if token_usage:
            parsed["_token_usage"] = token_usage
        parsed = self._normalize_plan_level_operations(parsed, latest_request, current_schedule)
        parsed = self._normalize_parsed_locations(parsed, latest_request, saved_locations)
        self._debug_json("LLM parsed request", parsed)
        self._debug(
            f"Parsed request | intent={parsed.get('intent')} | parsed_date={parsed.get('date')} | activities={len(parsed.get('activities', []))} | operations={len(parsed.get('operations', []))}"
        )
        return parsed

    def _generate_parser_content_with_retry(self, contents: Any) -> Any:
        for retry_index in range(len(PARSER_RETRY_DELAYS_SECONDS) + 1):
            try:
                response = self.client.models.generate_content(
                    model="gemini-3.1-flash-lite-preview",
                    contents=contents,
                    config={"response_mime_type": "application/json"},
                )
                if retry_index > 0:
                    jlog("MODULE_A", "success on retry", "LLM_RETRY")
                return response
            except Exception as exc:
                if self._is_transient_llm_error(exc) and retry_index < len(PARSER_RETRY_DELAYS_SECONDS):
                    reason = self._transient_error_label(exc)
                    jlog(
                        "MODULE_A",
                        f"attempt {retry_index + 1}/{len(PARSER_RETRY_DELAYS_SECONDS)} after {reason}",
                        "LLM_RETRY",
                    )
                    time.sleep(PARSER_RETRY_DELAYS_SECONDS[retry_index])
                    continue
                raise

    def _is_transient_llm_error(self, exc: Exception) -> bool:
        message = str(exc).lower()
        transient_markers = (
            "503",
            "unavailable",
            "deadline exceeded",
            "timeout",
            "timed out",
            "temporarily overloaded",
            "temporary",
            "high demand",
        )
        return any(marker in message for marker in transient_markers)

    def _transient_error_label(self, exc: Exception) -> str:
        message = str(exc)
        if "503" in message:
            return "503"
        if "UNAVAILABLE" in message.upper():
            return "UNAVAILABLE"
        if "deadline" in message.lower():
            return "deadline exceeded"
        if "timeout" in message.lower() or "timed out" in message.lower():
            return "timeout"
        return "transient error"

    def _build_parser_prompt(
        self,
        latest_request: str,
        history: List[Dict[str, Any]],
        current_schedule: Optional[Dict[str, Any]],
    ) -> str:
        history_lines = self._summarize_history(history)
        local_context = self._local_datetime_context()
        current_activity_index = self._build_current_activity_index(current_schedule)
        schedule_date = (current_schedule or {}).get("date") or "(none)"
        return (
            f"{PARSER_PROMPT}\n\n"
            f"{local_context}\n"
            f"CURRENT_SCHEDULE_DATE: {schedule_date}\n"
            f"CURRENT_ACTIVITY_INDEX:\n{current_activity_index}\n"
            f"HISTORY:\n" + ("\n".join(history_lines) if history_lines else "(none)") + "\n\n"
            f"LATEST_REQUEST:\n{latest_request}\n"
        )

    def _build_current_activity_index(self, current_schedule: Optional[Dict[str, Any]]) -> str:
        lines: List[str] = []
        for item in (current_schedule or {}).get("activities", []):
            if item.get("type") not in [None, "activity"]:
                continue
            title = item.get("title", "Untitled")
            start = item.get("startTime", "??:??")
            end = item.get("endTime", "??:??")
            lines.append(f"- {title} | {start} - {end}")
        return "\n".join(lines) if lines else "(none)"

    def _summarize_history(self, history: List[Dict[str, Any]]) -> List[str]:
        trimmed = history[-(MAX_HISTORY_TURNS * 2):]
        lines: List[str] = []
        for item in trimmed:
            role = "User" if item.get("role") == "user" else "Assistant"
            message = re.sub(r"\s+", " ", str(item.get("message") or "").strip())
            if not message:
                continue
            if role == "Assistant" and (
                "i created a structured draft from your request" in message.lower()
                or "i couldn't parse that request into a schedule change" in message.lower()
            ):
                continue
            if len(message) > MAX_HISTORY_MESSAGE_CHARS:
                message = message[: MAX_HISTORY_MESSAGE_CHARS - 3].rstrip() + "..."
            lines.append(f"{role}: {message}")
        return lines

    def _invalid_llm_parse(
        self,
        latest_request: str,
        current_schedule: Optional[Dict[str, Any]],
        raw_llm_reply: Optional[str],
        failure_type: str,
        failure_message: str,
        raw_response_text: Optional[str],
    ) -> Dict[str, Any]:
        reply = raw_llm_reply or "I couldn't parse that request into a schedule change. Please try rephrasing it."
        return {
            "intent": "chat",
            "reply": reply,
            "transcription": latest_request,
            "date": (current_schedule or {}).get("date") or self._local_today_iso(),
            "preferences": {},
            "activities": [],
            "operations": [],
            "_reply_source": failure_type,
            "_llm_reply": raw_llm_reply,
            "_failure_type": failure_type,
            "_failure_message": failure_message,
            "_raw_response_text": raw_response_text,
        }

    def _base_year_for_date_parse(self, current_schedule: Optional[Dict[str, Any]]) -> int:
        schedule_date = (current_schedule or {}).get("date")
        if schedule_date:
            try:
                return int(str(schedule_date).split("-", 1)[0])
            except Exception:
                pass
        return self._local_now().year

    def _extract_explicit_absolute_date(
        self,
        request_text: str,
        current_schedule: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        matches = self._extract_explicit_absolute_dates(request_text, current_schedule)
        return matches[-1] if matches else None

    def _extract_explicit_absolute_dates(
        self,
        request_text: str,
        current_schedule: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        text = request_text or ""
        month_pattern = "|".join(sorted(MONTH_NAME_TO_NUMBER.keys(), key=len, reverse=True))
        patterns = [
            re.compile(
                rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<month>{month_pattern})(?:\s*,?\s*(?P<year>\d{{4}}))?\b",
                re.IGNORECASE,
            ),
            re.compile(
                rf"\b(?P<month>{month_pattern})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:\s*,?\s*(?P<year>\d{{4}}))?\b",
                re.IGNORECASE,
            ),
        ]
        matches: List[Tuple[int, str]] = []
        base_year = self._base_year_for_date_parse(current_schedule)
        for pattern in patterns:
            for match in pattern.finditer(text):
                month = MONTH_NAME_TO_NUMBER.get(match.group("month").lower())
                day = int(match.group("day"))
                year = int(match.group("year") or base_year)
                try:
                    parsed_date = date(year, month, day).isoformat()
                except Exception:
                    continue
                matches.append((match.start(), parsed_date))
        if not matches:
            return []
        matches.sort(key=lambda item: item[0])
        return [item[1] for item in matches]

    def _apply_deterministic_shift_date_override(
        self,
        parsed: Dict[str, Any],
        latest_request: str,
        current_schedule: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        target_date = self._extract_explicit_absolute_date(latest_request, current_schedule)
        if not target_date:
            return parsed

        candidate = deepcopy(parsed)
        candidate["date"] = target_date
        if not self._request_implies_whole_plan_shift(latest_request, current_schedule, candidate):
            return parsed

        previous_date = parsed.get("date")
        if previous_date and previous_date != target_date:
            self._debug(f"[DATE_NORMALIZE] Deterministic shift date override: {previous_date} -> {target_date}")
        candidate["date"] = target_date
        for operation in candidate.get("operations") or []:
            if clean_title(operation.get("op") or "") == "shift_plan_date":
                previous_to_date = operation.get("to_date")
                if previous_to_date and previous_to_date != target_date:
                    self._debug(f"[DATE_NORMALIZE] Deterministic shift operation override: {previous_to_date} -> {target_date}")
                operation["to_date"] = target_date
        return candidate

    def _deterministic_fallback_parse(
        self,
        latest_request: str,
        current_schedule: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        request = re.sub(r"\s+", " ", latest_request or "").strip()
        clean_request = clean_title(request)
        schedule_date = (current_schedule or {}).get("date") or self._local_today_iso()
        parsed: Optional[Dict[str, Any]] = None

        target_date = self._extract_explicit_absolute_date(request, current_schedule)
        if target_date and self._request_text_implies_whole_plan_shift(clean_request):
            parsed = {
                "intent": "edit",
                "reply": f"I found the target date and will move the whole plan to {target_date}.",
                "transcription": request,
                "date": target_date,
                "operations": [],
                "activities": [],
                "preferences": {},
            }

        if parsed is None:
            parsed = self._fallback_parse_relative_add(request, schedule_date)

        if parsed is None:
            parsed = self._fallback_parse_fixed_time_update(request, schedule_date)

        if parsed is None:
            return None

        parsed["_reply_source"] = "deterministic_fallback"
        parsed["_llm_reply"] = None
        parsed["_failure_type"] = "llm_fallback_parse"
        jlog("MODULE_A", "Used deterministic fallback parser for simple request", "LLM_FALLBACK_PARSE")
        parsed = self._normalize_plan_level_operations(parsed, latest_request, current_schedule)
        parsed = self._normalize_parsed_locations(parsed, latest_request, [])
        return parsed

    def _fallback_parse_relative_add(self, request: str, schedule_date: str) -> Optional[Dict[str, Any]]:
        text = clean_title(request)
        duration_minutes: Optional[int] = None
        duration_match = re.search(r"\b(?P<duration>\d{1,3})[-\s]*minute\b", text)
        if duration_match:
            duration_minutes = int(duration_match.group("duration"))

        patterns = [
            re.compile(
                r"\badd\s+(?:a\s+|an\s+)?(?:quick\s+)?(?:(?P<duration>\d{1,3})[-\s]*minute\s+)?(?P<title>.+?)\s+(?P<kind>right\s+after|right\s+before|after|before)\s+(?:the\s+|my\s+)?(?P<anchor>.+?)(?:\.|$)",
                re.IGNORECASE,
            ),
            re.compile(
                r"\b(?P<kind>after|before)\s+(?:the\s+|my\s+)?(?P<anchor>.+?)\s+add\s+(?:a\s+|an\s+)?(?P<title>.+?)(?:\.|$)",
                re.IGNORECASE,
            ),
        ]
        match = next((pattern.search(request) for pattern in patterns if pattern.search(request)), None)
        if not match:
            return None

        title = self._clean_fallback_activity_title(match.group("title"))
        anchor = self._clean_fallback_activity_title(match.group("anchor"))
        if not title or not anchor:
            return None
        if match.groupdict().get("duration"):
            duration_minutes = int(match.group("duration"))

        kind = clean_title(match.group("kind")).replace("right ", "")
        operation = {
            "op": "add",
            "title": title,
            "timing_mode": TimingMode.RELATIVE,
            "anchor_relation": {"kind": kind, "target_title": anchor},
        }
        if duration_minutes:
            operation["duration_minutes"] = duration_minutes

        return {
            "intent": "edit",
            "reply": f"I understood this as adding {title} {kind} {anchor}.",
            "transcription": request,
            "date": schedule_date,
            "operations": [operation],
            "activities": [],
            "preferences": {},
        }

    def _fallback_parse_fixed_time_update(self, request: str, schedule_date: str) -> Optional[Dict[str, Any]]:
        match = re.search(
            r"\b(?:move|update|change|shift)\s+(?:my\s+|the\s+)?(?P<title>.+?)\s+to\s+(?P<time>\d{1,2}(?::\d{2})?\s*(?:am|pm))\b",
            request,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        fixed_start = parse_clock(match.group("time"))
        if fixed_start is None:
            return None
        title = self._clean_fallback_activity_title(match.group("title"))
        if not title:
            return None
        return {
            "intent": "edit",
            "reply": f"I understood this as moving {title} to {format_clock(fixed_start)}.",
            "transcription": request,
            "date": schedule_date,
            "operations": [{
                "op": "update",
                "title": title,
                "timing_mode": TimingMode.FIXED,
                "fixed_start": format_clock(fixed_start),
            }],
            "activities": [],
            "preferences": {},
        }

    def _clean_fallback_activity_title(self, value: str) -> str:
        text = re.sub(r"\b(right|quick|my|the)\b", " ", value or "", flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip(" .")
        return text.title() if text else ""

    def _request_text_implies_whole_plan_shift(self, request_text: str) -> bool:
        whole_plan_patterns = [
            r"\bmove (this|the|these|my)? ?(whole|entire)? ?(plan|schedule|day)\b",
            r"\bshift (this|the|whole|entire|my)? ?(plan|schedule|day)\b",
            r"\bmove everything\b",
            r"\bmove all\b",
            r"\bwhole plan\b",
            r"\bentire plan\b",
            r"\bwrong date\b",
            r"\bi said wrong about the date\b",
            r"\bnot\s+\d{1,2}(?:st|nd|rd|th)?\s+(?:it'?s|its)\s+\d{1,2}(?:st|nd|rd|th)?",
            r"\bnot .* make it\b",
        ]
        return any(re.search(pattern, request_text) for pattern in whole_plan_patterns)

    def _request_implies_whole_plan_shift(
        self,
        latest_request: str,
        current_schedule: Optional[Dict[str, Any]],
        parsed: Dict[str, Any],
    ) -> bool:
        if not current_schedule:
            return False

        request_text = clean_title(latest_request)
        current_date = (current_schedule or {}).get("date")
        parsed_date = parsed.get("date") or self._extract_explicit_absolute_date(latest_request, current_schedule)
        if not current_date or not parsed_date or parsed_date == current_date:
            return False

        if self._request_text_implies_whole_plan_shift(request_text):
            return True

        for operation in parsed.get("operations") or []:
            op = clean_title(operation.get("op") or "")
            title = clean_title(operation.get("title") or operation.get("target_title") or "")
            if op == "shift_plan_date":
                return True
            if op == "move" and title in {"all activities", "whole plan", "entire plan", "whole schedule"}:
                return True

        return False

    def _normalize_plan_level_operations(
        self,
        parsed: Dict[str, Any],
        latest_request: str,
        current_schedule: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        normalized = self._apply_deterministic_shift_date_override(
            deepcopy(parsed),
            latest_request,
            current_schedule,
        )
        if not self._request_implies_whole_plan_shift(latest_request, current_schedule, normalized):
            return normalized

        from_date = (current_schedule or {}).get("date")
        to_date = self._extract_explicit_absolute_date(latest_request, current_schedule) or normalized.get("date")
        if not from_date or not to_date:
            return normalized

        normalized["intent"] = "edit"
        normalized["activities"] = []
        normalized["operations"] = [{
            "op": "shift_plan_date",
            "from_date": from_date,
            "to_date": to_date,
            "scope": "all_active_activities",
            "notes": f"Shift the whole active plan from {from_date} to {to_date}.",
        }]
        self._debug(f"[STATE] Normalized whole-plan shift request from {from_date} to {to_date}")
        return normalized

    def _is_generic_system_activity_payload(self, item: Dict[str, Any]) -> bool:
        title = clean_title(str(item.get("title") or item.get("target_title") or ""))
        item_type = clean_title(str(item.get("type") or item.get("block_type") or item.get("entity_type") or ""))

        if title in GENERIC_SYSTEM_ACTIVITY_TITLES:
            return True
        if item_type in GENERIC_SYSTEM_ACTIVITY_TYPES and title in GENERIC_SYSTEM_ACTIVITY_TITLES:
            return True
        return False

    def _sanitize_operation_payload(self, raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        operation = deepcopy(raw)
        if "location" in operation:
            operation["location"] = clean_optional_text(operation.get("location"))
        if "location_normalized" in operation:
            operation["location_normalized"] = clean_optional_text(operation.get("location_normalized"))

        if self._is_generic_system_activity_payload(operation):
            self._debug(
                f"[STATE] Ignored generic system block from parser/current plan: {operation.get('title') or operation.get('type')}"
            )
            return None
        return operation

    def _sanitize_operations(self, operations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        sanitized: List[Dict[str, Any]] = []
        for raw in operations or []:
            if not isinstance(raw, dict):
                continue
            operation = self._sanitize_operation_payload(raw)
            if operation:
                sanitized.append(operation)
        return sanitized

    def _normalize_parsed_locations(
        self,
        parsed: Dict[str, Any],
        latest_request: str,
        saved_locations: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        normalized = deepcopy(parsed)
        transcription = normalized.get("transcription") or latest_request
        request_text = latest_request or transcription or ""
        normalized["operations"] = self._normalize_operation_locations(
            normalized.get("operations") or [],
            request_text,
            transcription,
            saved_locations or [],
        )
        normalized["activities"] = self._normalize_operation_locations(
            normalized.get("activities") or [],
            request_text,
            transcription,
            saved_locations or [],
        )
        return normalized

    def _normalize_operation_locations(
        self,
        operations: List[Dict[str, Any]],
        request_text: str,
        transcription: str,
        saved_locations: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        source_text = transcription or request_text or ""
        location_scopes = self._build_activity_location_scopes(operations, source_text)
        for index, raw in enumerate(operations):
            if not isinstance(raw, dict):
                continue
            operation = deepcopy(raw)
            if clean_title(operation.get("op") or "") in {"remove", "shift_plan_date"}:
                normalized.append(operation)
                continue
            if operation.get("location_status") and "raw_llm_location" in operation:
                normalized.append(operation)
                continue
            location_payload = self._resolve_operation_location(
                operation,
                request_text=request_text,
                transcription=transcription,
                saved_locations=saved_locations,
                explicit_evidence=location_scopes.get(index),
                all_operations=operations,
            )
            operation.update(location_payload)
            normalized.append(operation)
        return normalized

    def _resolve_operation_location(
        self,
        operation: Dict[str, Any],
        request_text: str,
        transcription: str,
        saved_locations: List[Dict[str, Any]],
        explicit_evidence: Optional[str] = None,
        all_operations: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        title = str(operation.get("title") or operation.get("target_title") or "").strip()
        raw_location = clean_optional_text(operation.get("location"))
        evidence = " ".join(value for value in [request_text, transcription, operation.get("notes") or ""] if value)
        scoped_evidence = explicit_evidence if explicit_evidence is not None else ""
        explicit_location = self._detect_explicit_location(scoped_evidence, title, raw_location)
        if not explicit_location:
            explicit_location = self._detect_shared_explicit_location(
                transcription or request_text or "",
                title,
                raw_location,
                all_operations or [],
            )
        category = self._infer_location_category(title, evidence)

        if explicit_location:
            label = explicit_location["label"]
            category = explicit_location.get("category") or category
            source = "explicit_user"
            status = "resolved"
            confidence = 0.95
        else:
            saved = self._match_saved_location_for_category(category, saved_locations)
            if saved:
                label = saved["label"]
                source = "saved_profile"
                status = "resolved"
                confidence = 0.9
            else:
                label, status, source, confidence = self._deterministic_location_default(
                    title,
                    raw_location,
                    category,
                    evidence,
                )

        label = clean_optional_text(label)
        normalized_location = _normalize_location(label)
        payload = {
            "location": label,
            "location_label": label,
            "location_category": category or "unknown",
            "location_status": status,
            "location_source": source,
            "location_confidence": confidence,
            "location_normalized": normalized_location,
            "raw_llm_location": raw_location,
            "explicit_user_location": bool(explicit_location),
        }
        if status in {"needs_resolution", "fallback_used", "resolved_default"}:
            payload["location_warning"] = (
                f"{title or 'Activity'} location was estimated as {label or category} because no exact place was provided."
            )
        self._log_location_resolution(title, raw_location, payload)
        return payload

    def _infer_location_category(self, title: str, evidence: str) -> str:
        title_text = clean_title(title or "")
        if self._contains_any_keyword(title_text, {"grocery", "groceries", "shopping", "supermarket", "buy food", "buy groceries"}):
            return "supermarket"
        if self._contains_any_keyword(title_text, {"gym", "workout", "exercise"}):
            return "fitness_center"
        if self._contains_any_keyword(title_text, {"lunch", "dinner", "meal", "breakfast", "restaurant", "cafe", "coffee"}):
            return "meal_place"
        if self._contains_any_keyword(title_text, {"meeting", "seminar", "class", "lecture", "office", "library", "campus"}):
            return "institution"
        if self._contains_any_keyword(title_text, {"study", "fyp", "implementation", "coding"}):
            return "workplace"

        text = clean_title(evidence or "")
        if self._contains_any_keyword(text, {"grocery", "groceries", "shopping", "supermarket", "buy food", "buy groceries"}):
            return "supermarket"
        if self._contains_any_keyword(text, {"gym", "workout", "exercise"}):
            return "fitness_center"
        if self._contains_any_keyword(text, {"lunch", "dinner", "meal", "breakfast", "restaurant", "cafe", "coffee"}):
            return "meal_place"
        if self._contains_any_keyword(text, {"meeting", "seminar", "class", "lecture", "office", "library", "campus"}):
            return "institution"
        if self._contains_any_keyword(text, {"study", "fyp", "implementation", "coding"}):
            return "workplace"
        return "unknown"

    def _build_activity_location_scopes(
        self,
        operations: List[Dict[str, Any]],
        source_text: str,
    ) -> Dict[int, str]:
        text = re.sub(r"\s+", " ", source_text or "").strip()
        if not text:
            return {}

        spans: Dict[int, Tuple[int, int]] = {}
        cursor = 0
        for index, operation in enumerate(operations or []):
            if not isinstance(operation, dict):
                continue
            if clean_title(operation.get("op") or "") in {"remove", "shift_plan_date"}:
                continue
            title = str(operation.get("title") or operation.get("target_title") or "").strip()
            span = self._find_activity_mention_span(text, title, start_at=cursor)
            if span is None:
                span = self._find_activity_mention_span(text, title, start_at=0)
            if span is None:
                continue
            spans[index] = span
            cursor = max(cursor, span[1])

        if not spans:
            if len([op for op in operations or [] if isinstance(op, dict)]) == 1:
                return {0: text}
            return {}

        ordered = sorted(spans.items(), key=lambda item: item[1][0])
        scopes: Dict[int, str] = {}
        for ordered_index, (operation_index, span) in enumerate(ordered):
            previous_span = ordered[ordered_index - 1][1] if ordered_index > 0 else None
            next_span = ordered[ordered_index + 1][1] if ordered_index + 1 < len(ordered) else None

            if previous_span:
                boundary = self._last_location_scope_boundary(text, previous_span[1], span[0])
                segment_start = boundary if boundary is not None else span[0]
            else:
                boundary = self._last_location_scope_boundary(text, 0, span[0])
                segment_start = boundary if boundary is not None else 0

            if next_span:
                boundary = self._first_location_scope_boundary(text, span[1], next_span[0])
                segment_end = boundary if boundary is not None else next_span[0]
            else:
                boundary = self._first_location_scope_boundary(text, span[1], len(text))
                segment_end = boundary if boundary is not None else len(text)

            segment_start = max(0, min(segment_start, span[0]))
            segment_end = max(span[1], min(segment_end, len(text)))
            scopes[operation_index] = text[segment_start:segment_end].strip()

        for index, operation in enumerate(operations or []):
            if index not in scopes and isinstance(operation, dict):
                scopes[index] = str(operation.get("notes") or "").strip()
        return scopes

    def _find_activity_mention_span(
        self,
        text: str,
        title: str,
        start_at: int = 0,
    ) -> Optional[Tuple[int, int]]:
        search_text = clean_title(text or "")
        aliases = self._activity_mention_aliases(title)
        for alias in aliases:
            pattern = r"\b" + r"\s+".join(re.escape(token) for token in alias.split()) + r"\b"
            match = re.search(pattern, search_text[start_at:], flags=re.IGNORECASE)
            if match:
                return start_at + match.start(), start_at + match.end()

        tokens = self._activity_title_tokens(title)
        if len(tokens) >= 2:
            pattern = r"\b" + r"\b.{0,80}?\b".join(re.escape(token) for token in tokens) + r"\b"
            match = re.search(pattern, search_text[start_at:], flags=re.IGNORECASE)
            if match:
                return start_at + match.start(), start_at + match.end()
        return None

    def _activity_mention_aliases(self, title: str) -> List[str]:
        aliases = set(self._generate_aliases(title))
        normalized = clean_title(title or "")
        if normalized:
            aliases.add(normalized)
        return sorted(
            (alias for alias in aliases if alias),
            key=lambda value: (len(value.split()), len(value)),
            reverse=True,
        )

    def _activity_title_tokens(self, title: str) -> List[str]:
        stop_words = {"quick", "the", "my", "a", "an"}
        return [
            token
            for token in re.split(r"[^a-z0-9]+", clean_title(title or ""))
            if token and token not in stop_words
        ]

    def _last_location_scope_boundary(self, text: str, start: int, end: int) -> Optional[int]:
        boundary: Optional[int] = None
        for match in re.finditer(
            r"[.;]|\b(?:followed by|and then|after that|then|also)\b",
            text[start:end],
            flags=re.IGNORECASE,
        ):
            boundary = start + match.end()
        return boundary

    def _first_location_scope_boundary(self, text: str, start: int, end: int) -> Optional[int]:
        match = re.search(
            r"[.;]|\b(?:followed by|and then|after that|then|also)\b",
            text[start:end],
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        return start + match.start()

    def _activity_location_context(self, evidence: str, title: str) -> str:
        text = re.sub(r"\s+", " ", evidence or "").strip()
        clean_text = clean_title(text)
        tokens = [token for token in re.split(r"[^a-z0-9]+", clean_title(title or "")) if token]
        stop_words = {"quick", "the", "my", "a", "an"}
        tokens = [token for token in tokens if token not in stop_words]
        if not tokens:
            return text

        positions = [clean_text.find(token) for token in tokens if clean_text.find(token) >= 0]
        if not positions:
            return text
        start = max(0, min(positions) - 60)
        end = min(len(text), max(positions) + 140)
        return text[start:end]

    def _detect_explicit_location(
        self,
        evidence: str,
        title: str,
        raw_location: Optional[str],
    ) -> Optional[Dict[str, str]]:
        matches = self._explicit_location_matches(evidence, title, raw_location)
        if not matches:
            return None

        title_span = self._find_activity_mention_span(evidence, title, start_at=0)
        if title_span:
            after_title = [match for match in matches if match["start"] >= title_span[1]]
            if after_title:
                chosen = min(after_title, key=lambda match: match["start"] - title_span[1])
                return {"label": chosen["label"], "category": chosen["category"]}
            chosen = min(matches, key=lambda match: title_span[0] - match["end"])
            return {"label": chosen["label"], "category": chosen["category"]}

        chosen = min(matches, key=lambda match: match["start"])
        return {"label": chosen["label"], "category": chosen["category"]}

    def _explicit_location_matches(
        self,
        evidence: str,
        title: str,
        raw_location: Optional[str],
    ) -> List[Dict[str, Any]]:
        text = clean_title(evidence or "")
        matches: List[Dict[str, Any]] = []
        explicit_patterns = [
            (r"\b(?:at|in|from)\s+(?:my\s+|the\s+)?home\b", "home", "home"),
            (r"\bgo home\b", "home", "home"),
            (r"\bat\s+(?:the\s+)?library\b", "library", "library"),
            (r"\bat\s+(?:the\s+)?gym\b", "gym", "fitness_center"),
            (r"\bnear\s+(?:the\s+)?campus\b", "school", "campus_area"),
            (r"\bat\s+(?:the\s+)?campus\b", "school", "campus_area"),
            (r"\bat\s+(?:the\s+)?school\b", "school", "campus_area"),
            (r"\bat\s+(?:the\s+)?main office\b", "office", "office"),
            (r"\bat\s+(?:a\s+|the\s+)?(?:cafe|restaurant)\b", "restaurant", "meal_place"),
            (r"\bonline\b|\bdelivery\b|\bhome delivery\b", "home", "home"),
        ]
        for pattern, label, category in explicit_patterns:
            for match in re.finditer(pattern, text):
                matches.append({
                    "start": match.start(),
                    "end": match.end(),
                    "label": label,
                    "category": category,
                })

        raw_clean = clean_title(raw_location or "")
        if raw_clean and raw_clean not in {"home", "school", "campus", "gym", "office", "library"}:
            location_pattern = rf"\b(?:at|in|near)\s+(?:the\s+)?{re.escape(raw_clean)}\b"
            for match in re.finditer(location_pattern, text):
                matches.append({
                    "start": match.start(),
                    "end": match.end(),
                    "label": raw_location or raw_clean,
                    "category": self._infer_location_category(title, evidence),
                })
        return sorted(matches, key=lambda match: match["start"])

    def _detect_shared_explicit_location(
        self,
        source_text: str,
        title: str,
        raw_location: Optional[str],
        operations: List[Dict[str, Any]],
    ) -> Optional[Dict[str, str]]:
        text = re.sub(r"\s+", " ", source_text or "").strip()
        if not text:
            return None

        clauses = [clause.strip() for clause in re.split(r"[.;]", text) if clause.strip()]
        for clause in clauses:
            title_span = self._find_activity_mention_span(clause, title, start_at=0)
            if not title_span:
                continue
            matches = self._explicit_location_matches(clause, title, raw_location)
            if not matches:
                continue
            for match in matches:
                if title_span[0] > match["start"]:
                    continue
                other_before_location = False
                for operation in operations or []:
                    if not isinstance(operation, dict):
                        continue
                    other_title = str(operation.get("title") or operation.get("target_title") or "").strip()
                    if clean_title(other_title) == clean_title(title):
                        continue
                    other_span = self._find_activity_mention_span(clause, other_title, start_at=0)
                    if other_span and other_span[0] < match["start"]:
                        other_before_location = True
                        break
                if not other_before_location:
                    continue

                prefix = clean_title(clause[:match["start"]])
                if (
                    re.search(r"\b(?:both|all)\b", prefix)
                    or "same location" in clean_title(clause)
                    or "for all" in clean_title(clause)
                    or re.search(r"\band\b", prefix)
                ):
                    return {"label": match["label"], "category": match["category"]}
        return None

    def _match_saved_location_for_category(
        self,
        category: str,
        saved_locations: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        category_keywords = {
            "supermarket": {"supermarket", "grocery", "groceries", "market", "store"},
            "fitness_center": {"gym", "fitness", "workout"},
            "meal_place": {"restaurant", "cafe", "food", "meal", "lunch", "dinner"},
            "campus_area": {"campus", "school", "university", "mmu"},
            "office": {"office", "work"},
            "library": {"library"},
            "home": {"home", "house"},
        }
        keywords = category_keywords.get(category, set())
        for saved in saved_locations or []:
            haystack = clean_title(" ".join(str(saved.get(field) or "") for field in ("label", "address", "category", "type")))
            if any(keyword in haystack for keyword in keywords):
                return {"label": saved.get("label") or saved.get("address") or next(iter(keywords))}
        return None

    def _deterministic_location_default(
        self,
        title: str,
        raw_location: Optional[str],
        category: str,
        evidence: str,
    ) -> Tuple[Optional[str], str, str, float]:
        text = clean_title(f"{title} {evidence}")
        raw_clean = clean_title(raw_location or "")
        if category == "supermarket":
            return "store", "needs_resolution", "deterministic_default", 0.65
        if category == "fitness_center":
            label = raw_location if raw_clean in {"gym", "fitness center"} else "gym"
            return label, "resolved_default", "deterministic_default", 0.75
        if category == "meal_place":
            return None, "needs_resolution", "unresolved", 0.35
        if category == "institution" and raw_location:
            return raw_location, "fallback_used", "llm_inferred", 0.55
        if category == "workplace":
            if raw_location:
                return raw_location, "resolved_default", "deterministic_default", 0.6
            return None, "needs_resolution", "unresolved", 0.3
        if raw_location:
            return raw_location, "fallback_used", "llm_inferred", 0.45
        return None, "needs_resolution", "unresolved", 0.2

    def _log_location_resolution(
        self,
        title: str,
        raw_location: Optional[str],
        payload: Dict[str, Any],
    ) -> None:
        jlog(
            "LOCATION",
            f"{title or 'Untitled'} | raw_llm_location={raw_location} | "
            f"explicit_user_location={str(payload.get('explicit_user_location')).lower()} | "
            f"normalized={payload.get('location_label')} | "
            f"category={payload.get('location_category')} | "
            f"source={payload.get('location_source')} | "
            f"status={payload.get('location_status')} | module=MODULE_A",
        )

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

        for act in ordered:
            start_min = act.get("scheduled_start") or 0
            end_min = act.get("scheduled_end") or (start_min + act.get("duration_minutes", 60))
            current_loc = act.get("location")
            if not current_loc: current_loc = None # Normalize

            # A. Gap Handling (between last_end and start_min)
            if start_min > last_end:
                gap_dur = start_min - last_end
                
                # Calculate required overhead
                buffer_dur = DEFAULT_PREP_BUFFER if last_loc is not None else 0
                travel_time = 0
                if last_loc and current_loc and last_loc != current_loc:
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
                            "block_type": "buffer",
                            "title": "Prep / Buffer",
                            "start": format_clock(b_start),
                            "end": format_clock(b_start + buffer_dur),
                            "duration_minutes": buffer_dur,
                            "reason": "Preparation before travel"
                        })
                    
                    # 3. Transition block (Travel)
                    if travel_time > 0:
                        t_start = last_end + idle_at_current + buffer_dur
                        blocks.append({
                            "block_type": "transition",
                            "title": f"Travel to {current_loc}",
                            "start": format_clock(t_start),
                            "end": format_clock(start_min),
                            "duration_minutes": travel_time,
                            "from_location": last_loc,
                            "to_location": current_loc,
                            "travel_estimate_source": "heuristic",
                            "travel_validation_status": "not_requested",
                            "reason": f"Travel timed to arrive exactly for {act['title']}"
                        })
                else:
                    # Not enough time for full buffer + travel? 
                    # Force move immediately and mark as potentially tight
                    blocks.append({
                        "block_type": "transition",
                        "title": f"Travel to {current_loc} (Tight)",
                        "start": format_clock(last_end),
                        "end": format_clock(start_min),
                        "duration_minutes": gap_dur,
                        "from_location": last_loc,
                        "to_location": current_loc,
                        "travel_estimate_source": "heuristic",
                        "travel_validation_status": "not_requested",
                        "is_tight": True,
                        "warning_code": WARNING_TIGHT_TRANSITION,
                        "reason": "Immediate departure required due to tight schedule"
                    })

            # B. Activity Block
            blocks.append({
                "block_type": "activity",
                "id": act.get("id"),
                "stable_activity_id": act.get("stable_activity_id") or act.get("id"),
                "title": act["title"],
                "start": format_clock(start_min),
                "end": format_clock(end_min),
                "startTime": format_clock(start_min),
                "endTime": format_clock(end_min),
                "duration_minutes": end_min - start_min,
                "location": current_loc,
                "location_label": act.get("location_label") or current_loc,
                "location_category": act.get("location_category"),
                "location_status": act.get("location_status"),
                "location_source": act.get("location_source"),
                "location_confidence": act.get("location_confidence"),
                "saved_location_label": act.get("saved_location_label"),
                "resolved_location": deepcopy(act.get("resolved_location")) if isinstance(act.get("resolved_location"), dict) else None,
                "notes": act.get("notes"),
                "is_conflict": act.get("is_conflict", False),
                "is_conflicting": act.get("is_conflict", False),
                "conflict_ids": list(act.get("conflict_ids") or []),
                "reason": "Scheduled activity"
            })
            
            last_end = end_min
            last_loc = current_loc

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
        day_start = parse_clock(preferences.get("day_start")) or DEFAULT_DAY_START
        day_end = parse_clock(preferences.get("day_end")) or DEFAULT_DAY_END
        min_travel = preferences.get("min_travel_buffer_minutes") or 0

        # Pass 1: Categorize
        fixed: List[Dict[str, Any]] = []
        relative: List[Dict[str, Any]] = []
        flexible: List[Dict[str, Any]] = []
        unscheduled: List[Dict[str, Any]] = []

        jlog("MODULE_C", f"Categorizing {len(activities)} items", "PASS_1")
        for item in deepcopy(activities):
            # Support both snake_case and camelCase
            mode = item.get("timing_mode") or item.get("timingMode") or TimingMode.UNSPECIFIED
            
            if mode == TimingMode.FIXED:
                fs = item.get("fixed_start")
                if fs is None:
                    fs = item.get("fixedStart")
                if fs is not None:
                    dur = int(item.get("duration_minutes") or item.get("durationMinutes") or 60)
                    item["scheduled_start"] = fs
                    item["scheduled_end"] = fs + dur
                    fixed.append(item)
                    jlog("MODULE_C", f"'{item['title']}' -> FIXED ({format_clock(fs)})", "CLASSIFY")
                else:
                    flexible.append(item)
                    jlog("MODULE_C", f"'{item['title']}' -> FLEX (missing fixed_start)", "CLASSIFY")
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

        # 2. Place RELATIVE items (Narrative Order Matters)
        relative.sort(key=lambda x: x.get("sequence_index") or 999)
        
        for item in relative:
            anchor = item.get("anchor_relation")
            if not anchor:
                flexible.append(item)
                continue

            target_id = anchor.get("target_activity_id")
            target_title = anchor.get("target_title")
            anchor_item = next(
                (
                    activity for activity in timeline
                    if (
                        target_id and activity.get("stable_activity_id") == target_id
                    ) or (
                        target_title and clean_title(activity["title"]) == clean_title(target_title)
                    )
                ),
                None,
            )

            if not anchor_item:
                flexible.append(item)
                continue

            relation_kind = clean_title(anchor.get("kind") or "after")
            if relation_kind == "before":
                search_end = anchor_item["scheduled_start"]
                predecessors = [a for a in timeline if a["scheduled_end"] <= search_end and a.get("id") != anchor_item.get("id")]
                search_start = max([day_start] + [a["scheduled_end"] for a in predecessors])
                jlog("MODULE_C", f"'{item['title']}' before '{target_title}' search={format_clock(search_start)}->{format_clock(search_end)}", "PLACE_RELATIVE")
                inserted, reason = self._insert_best_position(
                    item, timeline, search_start, search_end, min_travel,
                    prefer_latest=True
                )
            else:
                search_start = anchor_item["scheduled_end"]
                search_end = day_end

                successors = [a for a in timeline if a["scheduled_start"] >= search_start]
                if successors:
                    search_end = min(a["scheduled_start"] for a in successors)

                jlog("MODULE_C", f"'{item['title']}' after '{target_title}' search={format_clock(search_start)}->{format_clock(search_end)}", "PLACE_RELATIVE")
                inserted, reason = self._insert_best_position(
                    item, timeline, search_start, search_end, min_travel,
                    prefer_earliest=True
                )
            
            if inserted:
                timeline = inserted
                item["trace"].append(f"Placed near anchor '{target_title}' to respect sequence.")
            else:
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
                    jlog("MODULE_C", f"'{item['title']}' no feasible slot in narrative window", "REJECT_REL")
                    conflict_timeline = self._insert_as_conflict(
                        item,
                        timeline,
                        search_start if relation_kind == "before" else search_start,
                        reason or "No space in narrative window.",
                    )
                    timeline = conflict_timeline or timeline

        # 3. Place FLEXIBLE items
        flexible.sort(key=self._calculate_activity_base_score, reverse=True)
        for item in flexible:
            jlog("MODULE_C", f"'{item['title']}' score={self._calculate_activity_base_score(item)}", "PLACE_FLEX")
            inserted, reason = self._insert_best_position(item, timeline, day_start, day_end, min_travel)
            if inserted:
                timeline = inserted
            else:
                if item.get("is_mandatory"):
                    conflict_timeline = self._insert_as_conflict(item, timeline, day_start, reason)
                    timeline = conflict_timeline or timeline
                else:
                    unscheduled.append(item)
        
        # FINAL PASS: Materialize
        final_blocks = self._materialize_blocks(timeline, day_start, min_travel)
        
        return {
            "activities": timeline,
            "schedule_blocks": final_blocks,
            "unscheduled_activities": unscheduled,
            "day_start": format_clock(day_start),
            "day_end": format_clock(day_end)
        }

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

    def _find_overlaps(self, item: Dict[str, Any], timeline: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        overlaps = []
        start = item.get("scheduled_start")
        end = item.get("scheduled_end")
        
        # If the item itself isn't scheduled, it can't overlap in time
        if start is None or end is None:
            return []

        for existing in timeline:
            if existing.get("id") == item.get("id"):
                continue
            
            other_start = existing.get("scheduled_start")
            other_end = existing.get("scheduled_end")
            
            if other_start is None or other_end is None:
                continue

            if start < other_end and end > other_start:
                overlaps.append(existing)
        return overlaps

    def _highest_priority(self, activities: List[Dict[str, Any]]) -> str:
        best = "low"
        best_weight = -1
        for activity in activities:
            priority = activity.get("priority", "medium")
            weight = PRIORITY_WEIGHT.get(priority, 2)
            if weight > best_weight:
                best = priority
                best_weight = weight
        return best

    def _conflict_severity(self, item: Dict[str, Any], overlapping: List[Dict[str, Any]]) -> str:
        overlap_count = len(overlapping)
        if item.get("priority") == "high" and overlap_count:
            return "high"
        if overlap_count > 1:
            return "high"
        if overlap_count == 1:
            return "medium"
        return "low"

    def _reason_code_from_text(self, reason: Optional[str]) -> str:
        clean = clean_title(reason or "")
        if "outside day" in clean or "boundary" in clean:
            return "outside_day"
        if "no feasible slot" in clean or "no space" in clean or "narrative window" in clean:
            return "no_relative_slot"
        if "travel" in clean or "tight" in clean:
            return "travel_tight"
        if "clash" in clean or "overlap" in clean:
            return "fixed_overlap"
        return "infeasible_request"

    def _validate_locked_item(
        self,
        item: Dict[str, Any],
        timeline: List[Dict[str, Any]],
        day_start: int,
        day_end: int,
        min_travel: Optional[int],
    ) -> Tuple[bool, str]:
        """[Module B] Rule-Based Feasibility Validator."""
        start = item["scheduled_start"]
        end = item["scheduled_end"]
        
        # Check boundary
        if start < day_start or end > day_end:
            return False, f"Outside day boundary ({format_clock(day_start)}-{format_clock(day_end)})"

        for existing in timeline:
            if start < existing["scheduled_end"] and end > existing["scheduled_start"]:
                return False, f"Clashes with '{existing['title']}'"
        
        return True, ""

    def _calculate_activity_base_score(self, activity: Dict[str, Any]) -> int:
        """Heuristic score calculation for placement priority."""
        prio_map = {"high": 50, "medium": 20, "low": 5}
        score = prio_map.get(activity.get("priority", "medium").lower(), 20)
        
        if activity.get("is_mandatory") or activity.get("isMandatory"):
            score += 100
            
        if activity.get("latest_end") is not None:
            score += max(0, (DEFAULT_DAY_END - activity["latest_end"]) // 30)
            
        return score

    def _insert_best_position(
        self,
        item: Dict[str, Any],
        timeline: List[Dict[str, Any]],
        day_start: int,
        day_end: int,
        min_travel: Optional[int],
        prefer_earliest: bool = False,
        prefer_latest: bool = False,
    ) -> Tuple[Optional[List[Dict[str, Any]]], str]:
        """Finds the optimal time slot for an activity within a range, applying narrative-aware scoring."""
        best_timeline = None
        best_score = -999999
        failure_reason = "No feasible slot was available."
        
        duration = int(item.get("duration_minutes") or 60)

        for index in range(len(timeline) + 1):
            previous_item = timeline[index - 1] if index > 0 else None
            next_item = timeline[index] if index < len(timeline) else None

            gap_start = day_start if previous_item is None else previous_item["scheduled_end"]
            gap_end = day_end if next_item is None else next_item["scheduled_start"]

            before_transition = self._transition_minutes(previous_item, item, min_travel)
            after_transition = self._transition_minutes(item, next_item, min_travel)

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
            candidate_end = candidate_start + duration

            # Feasibility Checks
            if candidate_end + after_transition > gap_end:
                continue 
            
            if candidate_end > (item.get("latest_end") or day_end):
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
            
            if total_score > best_score:
                best_score = total_score
                candidate = deepcopy(item)
                candidate["scheduled_start"] = candidate_start
                candidate["scheduled_end"] = candidate_end
                
                if prefer_earliest and delay_minutes > 0:
                    candidate["trace"].append(f"Placed with {delay_minutes}m delay from earliest possible start.")
                
                best_timeline = sorted(timeline + [candidate], key=lambda x: x["scheduled_start"])

        if best_timeline:
            return best_timeline, ""
        return None, failure_reason

    def _transition_minutes(
        self,
        left: Optional[Dict[str, Any]],
        right: Optional[Dict[str, Any]],
        min_travel: Optional[int],
    ) -> int:
        if left is None or right is None:
            return 0
        travel = estimate_travel_minutes(left.get("location"), right.get("location"))
        travel = max(travel, min_travel or 0)
        prep = max(
            left.get("prep_buffer", DEFAULT_PREP_BUFFER),
            right.get("prep_buffer", DEFAULT_PREP_BUFFER),
        )
        return travel + prep

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _new_stable_activity_id(self) -> str:
        return f"act-{uuid4().hex[:12]}"

    def _is_activity_entry(self, item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        if item.get("block_type"):
            return False
        item_type = str(item.get("type") or "").strip().lower()
        return item_type not in {"buffer", "travel", "transition", "idle"}

    def _coerce_minutes(self, *values: Any) -> Optional[int]:
        for value in values:
            if value is None or value == "":
                continue
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                return int(value)
            parsed = parse_clock(value)
            if parsed is not None:
                return parsed
            text = str(value).strip()
            if text.isdigit():
                return int(text)
        return None

    def _infer_timing_mode(
        self,
        raw: Dict[str, Any],
        fixed_start: Optional[int],
        fixed_end: Optional[int],
        earliest_start: Optional[int],
        latest_end: Optional[int],
        preferred_start: Optional[int],
        anchor_relation: Optional[Dict[str, Any]],
    ) -> str:
        mode = clean_title(raw.get("timing_mode") or raw.get("timingMode") or "")
        if mode == "flexible":
            mode = TimingMode.UNSPECIFIED
        if mode in {
            TimingMode.FIXED,
            TimingMode.RELATIVE,
            TimingMode.PREFERRED,
            TimingMode.WINDOW,
            TimingMode.UNSPECIFIED,
        }:
            return mode
        if fixed_start is not None or fixed_end is not None:
            return TimingMode.FIXED
        if anchor_relation:
            return TimingMode.RELATIVE
        if preferred_start is not None:
            return TimingMode.PREFERRED
        if earliest_start is not None or latest_end is not None:
            return TimingMode.WINDOW
        return TimingMode.UNSPECIFIED

    def _contains_any_keyword(self, text: str, keywords: set[str]) -> bool:
        clean = clean_title(text)
        return any(keyword in clean for keyword in keywords)

    def _classify_timing_with_domain_rules(
        self,
        raw: Dict[str, Any],
        title: str,
        timing_mode: str,
        fixed_start: Optional[int],
        fixed_end: Optional[int],
        earliest_start: Optional[int],
        latest_end: Optional[int],
        preferred_start: Optional[int],
        anchor_relation: Optional[Dict[str, Any]],
    ) -> Tuple[str, Optional[int], Optional[int], Optional[int], Optional[str]]:
        evidence_text = " ".join(
            str(value or "")
            for value in (
                title,
                raw.get("notes"),
                raw.get("transcription"),
                raw.get("_user_message"),
                raw.get("_latest_request"),
            )
        )
        exact_time = fixed_start is not None or fixed_end is not None

        if anchor_relation:
            return (
                TimingMode.RELATIVE,
                None,
                None,
                preferred_start,
                "Smart timing: relative because the request is anchored before/after another activity.",
            )

        if exact_time:
            if self._contains_any_keyword(evidence_text, FIXED_EVENT_KEYWORDS):
                return (
                    TimingMode.FIXED,
                    fixed_start,
                    fixed_end,
                    preferred_start,
                    "Smart timing: fixed because the activity type is usually a locked commitment.",
                )

            if self._contains_any_keyword(evidence_text, SOCIAL_OR_BOOKED_KEYWORDS):
                return (
                    TimingMode.FIXED,
                    fixed_start,
                    fixed_end,
                    preferred_start,
                    "Smart timing: fixed because the request sounds like a booked or social commitment.",
                )

            if self._contains_any_keyword(evidence_text, PREFERRED_EXACT_KEYWORDS):
                preferred = preferred_start or fixed_start
                return (
                    TimingMode.PREFERRED,
                    None,
                    None,
                    preferred,
                    "Smart timing: preferred because the activity is usually movable even though a time was mentioned.",
                )

            if timing_mode == TimingMode.FIXED:
                return (
                    TimingMode.FIXED,
                    fixed_start,
                    fixed_end,
                    preferred_start,
                    "Smart timing: fixed because an exact time was requested.",
                )

        if preferred_start is not None:
            return (
                TimingMode.PREFERRED,
                fixed_start,
                fixed_end,
                preferred_start,
                "Smart timing: preferred because the request has a soft target time.",
            )

        if earliest_start is not None or latest_end is not None:
            return (
                TimingMode.WINDOW,
                fixed_start,
                fixed_end,
                preferred_start,
                "Smart timing: window because the request has an earliest/latest time constraint.",
            )

        return timing_mode, fixed_start, fixed_end, preferred_start, None

    def _generate_aliases(self, title: str) -> List[str]:
        normalized = clean_title(title)
        if not normalized:
            return []
        aliases = {normalized}
        tokens = [token for token in re.split(r"[^a-z0-9]+", normalized) if token]
        stop_words = {"the", "my", "a", "an", "quick"}
        filtered = [token for token in tokens if token not in stop_words]
        if filtered:
            aliases.add(" ".join(filtered))
            for token in filtered:
                aliases.add(token)
            if len(filtered) >= 2:
                aliases.add(" ".join(filtered[-2:]))
        return sorted(alias for alias in aliases if alias)

    def _canonicalize_activity(
        self,
        raw: Dict[str, Any],
        source_turn: Optional[int] = None,
        default_source: str = "planner",
    ) -> Dict[str, Any]:
        now_iso = self._now_iso()
        title = str(raw.get("title") or raw.get("target_title") or "Untitled Activity").strip() or "Untitled Activity"
        stable_activity_id = (
            raw.get("stable_activity_id")
            or raw.get("stableActivityId")
            or raw.get("canonical_id")
            or raw.get("id")
            or self._new_stable_activity_id()
        )
        normalized_title = raw.get("normalized_title") or clean_title(title)
        duration_minutes = parse_duration_minutes(
            raw.get("duration_minutes")
            or raw.get("durationMinutes")
            or raw.get("duration")
        )
        fixed_start = self._coerce_minutes(
            raw.get("fixed_start"),
            raw.get("fixedStart"),
            raw.get("scheduled_start"),
            raw.get("startTime"),
        )
        fixed_end = self._coerce_minutes(
            raw.get("fixed_end"),
            raw.get("fixedEnd"),
            raw.get("scheduled_end"),
            raw.get("endTime"),
        )
        if fixed_start is not None and fixed_end is None:
            fixed_end = fixed_start + duration_minutes
        if fixed_end is not None and fixed_start is None:
            fixed_start = fixed_end - duration_minutes
        if fixed_start is not None and fixed_end is not None and fixed_end <= fixed_start:
            fixed_end += 24 * 60

        earliest_start = self._coerce_minutes(raw.get("earliest_start"), raw.get("earliestStart"))
        latest_end = self._coerce_minutes(raw.get("latest_end"), raw.get("latestEnd"))
        preferred_start = self._coerce_minutes(raw.get("preferred_start"), raw.get("preferredStart"))
        anchor_relation = deepcopy(raw.get("anchor_relation"))

        timing_mode = self._infer_timing_mode(
            raw,
            fixed_start,
            fixed_end,
            earliest_start,
            latest_end,
            preferred_start,
            anchor_relation,
        )

        timing_mode, fixed_start, fixed_end, preferred_start, timing_trace = self._classify_timing_with_domain_rules(
            raw,
            title,
            timing_mode,
            fixed_start,
            fixed_end,
            earliest_start,
            latest_end,
            preferred_start,
            anchor_relation,
        )

        if timing_mode == TimingMode.FIXED and fixed_start is not None:
            earliest_start = fixed_start
            latest_end = fixed_end

        scheduled_start = self._coerce_minutes(raw.get("scheduled_start"), raw.get("startTime"))
        scheduled_end = self._coerce_minutes(raw.get("scheduled_end"), raw.get("endTime"))
        if scheduled_start is None and timing_mode == TimingMode.FIXED:
            scheduled_start = fixed_start
        if scheduled_end is None and timing_mode == TimingMode.FIXED and fixed_end is not None:
            scheduled_end = fixed_end
        if scheduled_start is not None and scheduled_end is None:
            scheduled_end = scheduled_start + duration_minutes

        location = clean_optional_text(raw.get("location"))
        location_normalized = raw.get("location_normalized") or _normalize_location(location)
        location_label = clean_optional_text(raw.get("location_label")) or location
        location_category = clean_optional_text(raw.get("location_category"))
        location_status = clean_optional_text(raw.get("location_status"))
        location_source = clean_optional_text(raw.get("location_source"))
        location_confidence = raw.get("location_confidence")
        resolved_location = deepcopy(raw.get("resolved_location")) if isinstance(raw.get("resolved_location"), dict) else None

        is_mandatory = bool(
            raw.get("is_mandatory")
            if raw.get("is_mandatory") is not None
            else raw.get("isMandatory", True)
        )

        trace = list(raw.get("trace") or [])
        if raw.get("notes") and raw.get("notes") not in trace:
            trace.append(raw.get("notes"))
        if timing_trace and timing_trace not in trace:
            trace.append(timing_trace)

        return {
            "id": stable_activity_id,
            "stable_activity_id": stable_activity_id,
            "entity_type": "activity",
            "type": raw.get("type") or "activity",
            "activity_type": raw.get("activity_type") or ("mandatory" if is_mandatory else "optional"),
            "title": title,
            "normalized_title": normalized_title,
            "aliases": list(raw.get("aliases") or self._generate_aliases(title)),
            "timing_mode": timing_mode,
            "fixed_start": fixed_start,
            "fixed_end": fixed_end,
            "earliest_start": earliest_start,
            "latest_end": latest_end,
            "preferred_start": preferred_start,
            "anchor_relation": anchor_relation,
            "sequence_index": raw.get("sequence_index"),
            "duration_minutes": duration_minutes,
            "priority": str(raw.get("priority") or "medium").lower(),
            "location": location,
            "location_label": location_label,
            "location_category": location_category,
            "location_status": location_status,
            "location_source": location_source,
            "location_confidence": location_confidence,
            "location_normalized": location_normalized,
            "saved_location_label": raw.get("saved_location_label"),
            "resolved_location": resolved_location,
            "raw_llm_location": raw.get("raw_llm_location"),
            "explicit_user_location": bool(raw.get("explicit_user_location", False)),
            "location_warning": raw.get("location_warning"),
            "status": raw.get("status") or "active",
            "source_turn": raw.get("source_turn") if raw.get("source_turn") is not None else (source_turn or 0),
            "created_at": raw.get("created_at") or now_iso,
            "updated_at": raw.get("updated_at") or now_iso,
            "notes": raw.get("notes"),
            "source": raw.get("source") or default_source,
            "trace": trace,
            "is_mandatory": is_mandatory,
            "scheduled_start": scheduled_start,
            "scheduled_end": scheduled_end,
            "prep_buffer": raw.get("prep_buffer", DEFAULT_PREP_BUFFER),
            "is_conflict": bool(raw.get("is_conflict") or raw.get("isConflict", False)),
            "is_conflicting": bool(raw.get("is_conflict") or raw.get("isConflict", False)),
            "conflict_ids": list(raw.get("conflict_ids") or []),
            "conflict_with": list(raw.get("conflict_with") or raw.get("conflictWith") or []),
            "conflict_reason": raw.get("conflict_reason") or raw.get("conflictReason"),
            "conflict_priority": raw.get("conflict_priority") or raw.get("conflictPriority"),
            "conflict_severity": raw.get("conflict_severity") or raw.get("conflictSeverity"),
            "accepted_with_warning": bool(raw.get("accepted_with_warning")),
            "warning_code": raw.get("warning_code"),
            "warnings": list(raw.get("warnings") or []),
        }

    def _load_canonical_activities(self, envelope: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        canonical: List[Dict[str, Any]] = []
        excluded = 0
        for raw in list((envelope or {}).get("activities") or (envelope or {}).get("items") or []):
            if not self._is_activity_entry(raw) or self._is_generic_system_activity_payload(raw):
                excluded += 1
                continue
            canonical.append(self._canonicalize_activity(raw))

        active_count = sum(1 for item in canonical if item.get("status") == "active")
        inactive_count = len(canonical) - active_count
        self._debug(f"[STATE] Loaded active activities: {active_count}")
        if inactive_count:
            self._debug(f"[STATE] Excluded superseded/deleted activities from construction: {inactive_count}")
        if excluded:
            self._debug(f"[STATE] Ignored derived schedule blocks during reload: {excluded}")
        return canonical

    def _resolve_allow_clash(self, preferences: Optional[Dict[str, Any]], envelope: Optional[Dict[str, Any]] = None) -> bool:
        if preferences and "allow_clash" in preferences:
            return bool(preferences.get("allow_clash"))
        if envelope and "allow_clash" in envelope:
            return bool(envelope.get("allow_clash"))
        return bool(((envelope or {}).get("preferences") or {}).get("allow_clash", False))

    def _resolve_accurate_travel_time(
        self,
        preferences: Optional[Dict[str, Any]],
        envelope: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if preferences and "accurate_travel_time" in preferences:
            return bool(preferences.get("accurate_travel_time"))
        if envelope and "accurate_travel_time" in envelope:
            return bool(envelope.get("accurate_travel_time"))
        return bool(((envelope or {}).get("preferences") or {}).get("accurate_travel_time", False))

    def _planning_mode(self, allow_clash: bool) -> str:
        return PLANNING_MODE_CLASH if allow_clash else PLANNING_MODE_FEASIBILITY

    def _apply_accurate_travel_if_requested(
        self,
        envelope: Dict[str, Any],
        saved_locations: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
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
        if not block.get("location"):
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

    def _transition_block_between(
        self,
        blocks: List[Dict[str, Any]],
        left_index: int,
        right_index: int,
    ) -> Optional[Dict[str, Any]]:
        for index in range(left_index + 1, right_index):
            block = blocks[index]
            if block.get("block_type") == "transition" or block.get("type") in {"travel", "transition"}:
                return block
        return None

    def _transition_index_between(
        self,
        blocks: List[Dict[str, Any]],
        left_index: int,
        right_index: int,
    ) -> Optional[int]:
        for index in range(left_index + 1, right_index):
            block = blocks[index]
            if block.get("block_type") == "transition" or block.get("type") in {"travel", "transition"}:
                return index
        return None

    def _conflict_priority_label(
        self,
        left: Dict[str, Any],
        right: Dict[str, Any],
        user_forced: bool,
    ) -> str:
        left_fixed = left.get("timing_mode") == TimingMode.FIXED
        right_fixed = right.get("timing_mode") == TimingMode.FIXED
        left_mandatory = bool(left.get("is_mandatory", True))
        right_mandatory = bool(right.get("is_mandatory", True))
        if user_forced:
            return "user-forced"
        if left_fixed and right_fixed:
            return "fixed-vs-fixed"
        if left_fixed or right_fixed:
            if left_mandatory and right_mandatory:
                return "fixed-vs-mandatory"
            return "fixed-vs-optional"
        if not left_mandatory and not right_mandatory:
            return "optional-vs-optional"
        return "fixed-vs-mandatory"

    def _conflict_severity_for_pair(
        self,
        left: Dict[str, Any],
        right: Dict[str, Any],
        user_forced: bool,
    ) -> str:
        label = self._conflict_priority_label(left, right, user_forced)
        return CONFLICT_SEVERITY_RULES.get(label, "medium")

    def _conflict_suggestions(
        self,
        left: Dict[str, Any],
        right: Dict[str, Any],
    ) -> List[str]:
        suggestions: List[str] = []
        fixed_candidate = right if right.get("timing_mode") == TimingMode.FIXED else left
        moving_candidate = left if fixed_candidate is right else right
        next_start = fixed_candidate.get("scheduled_end")
        if next_start is not None:
            suggestions.append(f"Move {moving_candidate.get('title')} to {format_clock(next_start + 5)}")
        existing_fixed = self._coerce_minutes(moving_candidate.get("fixed_start"))
        if existing_fixed is not None:
            suggestions.append(f"Keep {moving_candidate.get('title')} at {format_clock(existing_fixed)}")
        suggestions.append(f"Shift {fixed_candidate.get('title')} if allowed")
        return suggestions

    def _build_conflicts(
        self,
        timeline: List[Dict[str, Any]],
        user_forced_ids: Optional[set[str]] = None,
    ) -> List[Dict[str, Any]]:
        conflicts: List[Dict[str, Any]] = []
        seen_pairs: set[Tuple[str, str]] = set()
        forced_ids = user_forced_ids or set()

        for index, left in enumerate(timeline):
            if not self._is_activity_entry(left):
                continue
            left_start = left.get("scheduled_start")
            left_end = left.get("scheduled_end")
            if left_start is None or left_end is None:
                continue
            for right in timeline[index + 1:]:
                if not self._is_activity_entry(right):
                    continue
                right_start = right.get("scheduled_start")
                right_end = right.get("scheduled_end")
                if right_start is None or right_end is None:
                    continue
                overlap_start = max(left_start, right_start)
                overlap_end = min(left_end, right_end)
                if overlap_start >= overlap_end:
                    continue

                left_id = left.get("stable_activity_id") or left.get("id")
                right_id = right.get("stable_activity_id") or right.get("id")
                pair_key = tuple(sorted([str(left_id), str(right_id)]))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                conflict_identity = "|".join(pair_key)

                user_forced = left_id in forced_ids or right_id in forced_ids
                priority_label = self._conflict_priority_label(left, right, user_forced)
                severity = self._conflict_severity_for_pair(left, right, user_forced)
                conflict_id = f"conf-{uuid4().hex[:10]}"
                explanation = (
                    f"{left.get('title')} overlaps with {right.get('title')} from "
                    f"{format_clock(overlap_start)} to {format_clock(overlap_end)}."
                )
                conflict = {
                    "conflict_id": conflict_id,
                    "type": "time_overlap",
                    "conflict_identity": conflict_identity,
                    "reason_codes": ["fixed_overlap" if priority_label.startswith("fixed") else "time_overlap"],
                    "activities": [left.get("title"), right.get("title")],
                    "activity_ids": [left_id, right_id],
                    "start": format_clock(overlap_start),
                    "end": format_clock(overlap_end),
                    "severity": severity,
                    "priority_label": priority_label,
                    "user_forced": user_forced,
                    "explanation": explanation,
                    "suggested_resolution": self._conflict_suggestions(left, right),
                }
                conflicts.append(conflict)
                for activity in (left, right):
                    activity["is_conflict"] = True
                    activity["is_conflicting"] = True
                    activity.setdefault("conflict_ids", [])
                    if conflict_id not in activity["conflict_ids"]:
                        activity["conflict_ids"].append(conflict_id)
        return conflicts

    def _merge_target_date_context(
        self,
        shifted_activities: List[Dict[str, Any]],
        target_date_envelope: Optional[Dict[str, Any]],
        source_date: Optional[str],
        target_date: Optional[str],
        is_explicit_shift: bool = False,
    ) -> List[Dict[str, Any]]:
        target_context = self._load_canonical_activities(target_date_envelope)
        existing_active = [item for item in target_context if item.get("status") == "active"]
        self._debug(f"[STATE] Loaded active activities for {target_date_envelope.get('date') if target_date_envelope else '(none)'}: {len(existing_active)}")

        incoming_ids = {item.get("stable_activity_id") for item in shifted_activities}
        merged = list(shifted_activities)
        for activity in existing_active:
            if activity.get("stable_activity_id") in incoming_ids:
                continue
            merged.append(activity)
        if source_date:
            target_label = target_date or (target_date_envelope.get("date") if target_date_envelope else source_date)
            if is_explicit_shift:
                self._debug(f"[STATE] Applying bulk date shift from {source_date} to {target_label}")
                self._debug(f"[STATE] Shifted {len(shifted_activities)} active activities to target date")
            else:
                self._debug(f"[STATE] Assigning plan date to {target_label}")
        return merged

    def _normalize_operation(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        op = clean_title(raw.get("op") or "add") or "add"
        normalized = deepcopy(raw)
        normalized["op"] = op
        if "location" in normalized:
            normalized["location"] = clean_optional_text(normalized.get("location"))
        normalized["fixed_start"] = raw.get("fixed_start") or raw.get("startTime")
        normalized["fixed_end"] = raw.get("fixed_end") or raw.get("endTime")
        normalized["target_id"] = raw.get("target_id") or raw.get("stable_activity_id") or raw.get("id")
        normalized["target_title"] = raw.get("target_title") or raw.get("target") or raw.get("title")
        if raw.get("duration_minutes") is None and raw.get("duration") is not None:
            normalized["duration_minutes"] = parse_duration_minutes(raw.get("duration"))
        if normalized.get("anchor_relation") and not normalized.get("timing_mode"):
            normalized["timing_mode"] = TimingMode.RELATIVE
        if normalized.get("fixed_start") is not None and not normalized.get("timing_mode"):
            normalized["timing_mode"] = TimingMode.FIXED
        return normalized

    def _conflict_identity(self, conflict: Dict[str, Any]) -> Optional[str]:
        activity_ids = conflict.get("activity_ids") or []
        if len(activity_ids) < 2:
            return conflict.get("conflict_identity")
        return "|".join(sorted(str(activity_id) for activity_id in activity_ids))

    def _existing_conflict_identities(self, envelope: Dict[str, Any]) -> set[str]:
        identities: set[str] = set()
        for conflict in envelope.get("conflicts") or []:
            identity = self._conflict_identity(conflict)
            if identity:
                identities.add(identity)
        return identities

    def _message_explicitly_targets_activity(self, message: str, title: str) -> bool:
        clean_message = clean_title(message)
        clean_activity = clean_title(title)
        action_words = ("move", "shift", "reschedule", "change", "edit", "update")
        return any(f"{word} {clean_activity}" in clean_message for word in action_words)

    def _infer_anchor_from_user_message(
        self,
        operation: Dict[str, Any],
        active_pool: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        message = clean_title(operation.get("_user_message") or "")
        target_title = clean_title(operation.get("target_title") or operation.get("title") or "")
        if not message or not target_title:
            return None

        for anchor in active_pool:
            anchor_title = clean_title(anchor.get("title") or "")
            if not anchor_title or anchor_title == target_title:
                continue
            before_patterns = (
                f"{target_title} before {anchor_title}",
                f"{target_title} before the {anchor_title}",
            )
            after_patterns = (
                f"{target_title} after {anchor_title}",
                f"{target_title} after the {anchor_title}",
            )
            if any(pattern in message for pattern in before_patterns):
                return {"kind": "before", "target_title": anchor.get("title")}
            if any(pattern in message for pattern in after_patterns):
                return {"kind": "after", "target_title": anchor.get("title")}
        return None

    def _prepare_operations_for_apply(
        self,
        operations: List[Dict[str, Any]],
        active_pool: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        prepared: List[Dict[str, Any]] = []

        for raw_operation in operations:
            operation = self._normalize_operation(raw_operation)
            inferred_anchor = self._infer_anchor_from_user_message(operation, active_pool)
            if inferred_anchor:
                operation["anchor_relation"] = inferred_anchor
                operation["timing_mode"] = TimingMode.RELATIVE
                operation["fixed_start"] = None
                operation["fixed_end"] = None

            target_reference = operation.get("target_id") or operation.get("target_title")
            resolution = self._resolve_activity_reference(target_reference, active_pool) if target_reference else {"status": "missing"}
            target = resolution.get("activity") if resolution.get("status") == "resolved" else None
            has_anchor = bool(operation.get("anchor_relation"))
            explicit_fixed = operation.get("fixed_start") is not None or operation.get("fixed_end") is not None
            message = operation.get("_user_message") or ""

            if (
                target
                and has_anchor
                and not explicit_fixed
                and target.get("timing_mode") == TimingMode.FIXED
                and not self._message_explicitly_targets_activity(message, target.get("title", ""))
            ):
                self._debug(f"[STATE] Ignored parser operation that tried to move fixed anchor '{target.get('title')}'")
                continue

            prepared.append(operation)

        return prepared

    def _operation_postcondition(
        self,
        operation: Dict[str, Any],
        target: Dict[str, Any],
        active_pool: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        anchor = operation.get("anchor_relation")
        if not anchor:
            return None
        resolved_anchor = self._resolve_anchor_relation(anchor, active_pool)
        anchor_id = (resolved_anchor or {}).get("target_activity_id")
        if not anchor_id:
            return None
        return {
            "kind": clean_title((resolved_anchor or {}).get("kind") or "after"),
            "target_activity_id": target.get("stable_activity_id"),
            "target_title": target.get("title"),
            "anchor_activity_id": anchor_id,
            "anchor_title": (resolved_anchor or {}).get("target_title"),
        }

    def _validate_postconditions(
        self,
        planned_by_id: Dict[str, Dict[str, Any]],
        postconditions: List[Dict[str, Any]],
        planned_activities: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        failures: List[Dict[str, Any]] = []
        planned_items = planned_activities or list(planned_by_id.values())
        for condition in postconditions:
            target = planned_by_id.get(condition.get("target_activity_id"))
            anchor = planned_by_id.get(condition.get("anchor_activity_id"))
            if not target or not anchor:
                failures.append({**condition, "reason": "Target or anchor activity was not found after replanning."})
                continue

            kind = condition.get("kind") or "after"
            target_start = target.get("scheduled_start") or 0
            target_end = target.get("scheduled_end") or 0
            anchor_start = anchor.get("scheduled_start") or 0
            anchor_end = anchor.get("scheduled_end") or 0
            required_duration = parse_duration_minutes(target.get("duration_minutes"), minimum=0)

            if kind == "before":
                ok = target_end <= anchor_start
                prior_blocks = [
                    item for item in planned_items
                    if item.get("stable_activity_id") not in {target.get("stable_activity_id"), anchor.get("stable_activity_id")}
                    and item.get("scheduled_end") is not None
                    and item.get("scheduled_end") <= anchor_start
                ]
                available_start = max([item.get("scheduled_end") or DEFAULT_DAY_START for item in prior_blocks] + [DEFAULT_DAY_START])
                available_end = anchor_start
                reason = f"{target.get('title')} could not be placed before {anchor.get('title')}."
            else:
                ok = target_start >= anchor_end
                next_blocks = [
                    item for item in planned_items
                    if item.get("stable_activity_id") not in {target.get("stable_activity_id"), anchor.get("stable_activity_id")}
                    and item.get("scheduled_start") is not None
                    and item.get("scheduled_start") >= anchor_end
                ]
                available_start = anchor_end
                available_end = min([item.get("scheduled_start") or DEFAULT_DAY_END for item in next_blocks] + [DEFAULT_DAY_END])
                reason = f"{target.get('title')} could not be placed after {anchor.get('title')}."

            if not ok:
                available_minutes = max(0, available_end - available_start)
                if available_minutes < required_duration:
                    detail = (
                        f"The available window is {format_clock(available_start)}-{format_clock(available_end)} "
                        f"({available_minutes} min), but {target.get('title')} needs {required_duration} min."
                    )
                else:
                    detail = "The final schedule did not satisfy the requested ordering after replanning."
                failures.append({
                    **condition,
                    "reason": reason,
                    "detail": detail,
                    "target_start": format_clock(target_start),
                    "target_end": format_clock(target_end),
                    "anchor_start": format_clock(anchor_start),
                    "anchor_end": format_clock(anchor_end),
                    "required_duration_minutes": required_duration,
                    "available_window_start": format_clock(available_start),
                    "available_window_end": format_clock(available_end),
                    "available_window_minutes": available_minutes,
                })
        return failures

    def _build_postcondition_response(
        self,
        original_envelope: Dict[str, Any],
        current_version: int,
        failures: List[Dict[str, Any]],
        allow_clash: bool,
    ) -> Dict[str, Any]:
        failure = failures[0]
        reason = failure.get("detail") or failure.get("reason") or "The requested ordering could not be satisfied."
        conflict_payload = {
            "status": "conflict",
            "type": "postcondition_failed",
            "conflict_target": failure.get("target_title"),
            "conflict_reason": reason,
            "reason_codes": ["postcondition_failed", "no_relative_slot"],
            "suggestions": [
                f"Choose a different time for {failure.get('target_title')}",
                f"Move {failure.get('anchor_title')} if that commitment can change",
            ],
            "postcondition": failure,
        }
        envelope = deepcopy(original_envelope)
        envelope["status"] = "conflict"
        envelope["planning_mode"] = self._planning_mode(allow_clash)
        envelope["allow_clash"] = allow_clash
        envelope["conflict"] = conflict_payload
        envelope["conflicts"] = [conflict_payload]
        envelope["version"] = current_version
        envelope["validation_issues"] = list(envelope.get("validation_issues") or []) + [reason]
        envelope["warnings"] = []
        envelope["applied_changes"] = []
        envelope["accepted_with_warnings"] = []
        envelope["rejected_changes"] = [conflict_payload]
        return {
            "status": "conflict",
            "applied": False,
            "envelope": envelope,
            "version": current_version,
            "activities": envelope.get("activities", []),
            "updatedActivities": [],
            "deletedItemIds": [],
            "conflict": conflict_payload,
            "postcondition_results": failures,
            "applied_changes": [],
            "accepted_with_warnings": [],
            "rejected_changes": [conflict_payload],
            "warnings": [],
        }

    def _find_activity_by_stable_id(
        self,
        activities: List[Dict[str, Any]],
        stable_activity_id: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        if not stable_activity_id:
            return None
        for activity in activities:
            if activity.get("stable_activity_id") == stable_activity_id:
                return activity
        return None

    def _token_overlap_score(self, left: str, right: str) -> int:
        left_tokens = {token for token in left.split() if token}
        right_tokens = {token for token in right.split() if token}
        if not left_tokens or not right_tokens:
            return 0
        return len(left_tokens & right_tokens)

    def _resolve_activity_reference(
        self,
        reference: Any,
        activities: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        normalized_reference = clean_title(str(reference or ""))
        if not normalized_reference:
            self._debug("[STATE] Target resolution failed because the reference was empty")
            return {"status": "missing", "reason": "empty_reference"}

        active_activities = [item for item in activities if item.get("status") == "active"]
        scored: List[Tuple[int, int, int, Dict[str, Any]]] = []
        for index, activity in enumerate(active_activities):
            score = 0
            if str(reference) == activity.get("stable_activity_id") or str(reference) == activity.get("id"):
                score = 200
            elif normalized_reference == activity.get("normalized_title"):
                score = 150
            elif normalized_reference == clean_title(activity.get("title", "")):
                score = 145
            elif normalized_reference in set(activity.get("aliases") or []):
                score = 130
            else:
                overlap = self._token_overlap_score(normalized_reference, activity.get("normalized_title") or "")
                if overlap:
                    score = max(score, 90 + overlap * 10)
                if normalized_reference in (activity.get("normalized_title") or "") or (activity.get("normalized_title") or "") in normalized_reference:
                    score = max(score, 80)

            if score <= 0:
                continue

            recency_rank = len(active_activities) - index
            scheduled_rank = int(activity.get("scheduled_start") or activity.get("fixed_start") or -1)
            scored.append((score, recency_rank, scheduled_rank, activity))

        if not scored:
            self._debug(f"[STATE] Target resolution failed for '{reference}'")
            return {"status": "not_found", "reason": "no_match", "reference": str(reference)}

        scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        best_score, _, _, best_activity = scored[0]
        tied = [entry for entry in scored if entry[0] == best_score]
        if len(tied) > 1:
            titles = sorted({entry[3].get("title") for entry in tied})
            self._debug(f"[STATE] Target resolution ambiguous for '{reference}': {titles}")
            return {
                "status": "ambiguous",
                "reference": str(reference),
                "candidates": titles,
            }

        self._debug(f"[STATE] Resolved target '{reference}' -> activity_id={best_activity.get('stable_activity_id')}")
        return {"status": "resolved", "activity": best_activity}

    def _resolve_anchor_relation(
        self,
        anchor_relation: Optional[Dict[str, Any]],
        activities: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if not anchor_relation:
            return None
        target_reference = (
            anchor_relation.get("target_activity_id")
            or anchor_relation.get("target_id")
            or anchor_relation.get("target_title")
        )
        if not target_reference:
            return deepcopy(anchor_relation)
        resolved = self._resolve_activity_reference(target_reference, activities)
        if resolved.get("status") != "resolved":
            return deepcopy(anchor_relation)
        target = resolved["activity"]
        return {
            "kind": anchor_relation.get("kind"),
            "target_activity_id": target.get("stable_activity_id"),
            "target_title": target.get("title"),
        }

    def _mutate_existing_activity(
        self,
        existing: Dict[str, Any],
        operation: Dict[str, Any],
        source_turn: int,
        anchor_pool: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        updated = deepcopy(existing)
        updated["updated_at"] = self._now_iso()
        updated["source_turn"] = source_turn
        updated["source"] = "user_operation"
        updated.setdefault("trace", [])

        for field in ("title", "priority", "location", "notes", "sequence_index"):
            if operation.get(field) is not None:
                updated[field] = operation.get(field)

        if operation.get("duration_minutes") is not None:
            updated["duration_minutes"] = parse_duration_minutes(operation.get("duration_minutes"))

        if operation.get("is_mandatory") is not None:
            updated["is_mandatory"] = bool(operation.get("is_mandatory"))
            updated["activity_type"] = "mandatory" if updated["is_mandatory"] else "optional"

        if updated.get("title"):
            updated["normalized_title"] = clean_title(updated["title"])
            updated["aliases"] = self._generate_aliases(updated["title"])

        explicit_fixed_start = self._coerce_minutes(operation.get("fixed_start"))
        explicit_fixed_end = self._coerce_minutes(operation.get("fixed_end"))
        explicit_earliest = self._coerce_minutes(operation.get("earliest_start"))
        explicit_latest = self._coerce_minutes(operation.get("latest_end"))
        explicit_preferred = self._coerce_minutes(operation.get("preferred_start"))
        explicit_anchor = operation.get("anchor_relation")

        if explicit_anchor:
            resolved_anchor = self._resolve_anchor_relation(explicit_anchor, anchor_pool)
            if resolved_anchor:
                updated["anchor_relation"] = resolved_anchor
            updated["timing_mode"] = TimingMode.RELATIVE
            updated["fixed_start"] = None
            updated["fixed_end"] = None
            updated["scheduled_start"] = None
            updated["scheduled_end"] = None
            if updated.get("location") is None and resolved_anchor:
                anchor_activity = self._find_activity_by_stable_id(anchor_pool, resolved_anchor.get("target_activity_id"))
                if anchor_activity:
                    updated["location"] = anchor_activity.get("location")
                    updated["location_normalized"] = anchor_activity.get("location_normalized")
        elif explicit_fixed_start is not None or explicit_fixed_end is not None:
            updated["timing_mode"] = TimingMode.FIXED
            updated["anchor_relation"] = None
            if explicit_fixed_start is not None:
                updated["fixed_start"] = explicit_fixed_start
            if explicit_fixed_end is not None:
                updated["fixed_end"] = explicit_fixed_end
            elif explicit_fixed_start is not None:
                updated["fixed_end"] = explicit_fixed_start + int(updated.get("duration_minutes") or DEFAULT_DURATION)
            updated["earliest_start"] = updated.get("fixed_start")
            updated["latest_end"] = updated.get("fixed_end")
            updated["scheduled_start"] = updated.get("fixed_start")
            updated["scheduled_end"] = updated.get("fixed_end")

        if explicit_earliest is not None:
            updated["earliest_start"] = explicit_earliest
        if explicit_latest is not None:
            updated["latest_end"] = explicit_latest
        if explicit_preferred is not None:
            updated["preferred_start"] = explicit_preferred
            if updated.get("timing_mode") == TimingMode.UNSPECIFIED:
                updated["timing_mode"] = TimingMode.PREFERRED

        if updated.get("location"):
            updated["location_normalized"] = _normalize_location(updated.get("location"))

        note = operation.get("notes") or f"{operation.get('op', 'update')} request applied to existing activity."
        if note not in updated["trace"]:
            updated["trace"].append(note)

        updated["status"] = "active"
        updated["is_conflict"] = False
        updated["is_conflicting"] = False
        updated["conflict_ids"] = []
        updated["conflict_with"] = []
        updated["conflict_reason"] = None
        updated["conflict_priority"] = None
        updated["conflict_severity"] = None
        return self._canonicalize_activity(updated, source_turn=source_turn, default_source="user_operation")

    def _build_conflict_response(
        self,
        original_envelope: Dict[str, Any],
        current_version: int,
        activity: Dict[str, Any],
        blockers: List[Dict[str, Any]],
        allow_clash: bool = False,
    ) -> Dict[str, Any]:
        blocker = blockers[0] if blockers else None
        reason = activity.get("conflict_reason") or "Requested change conflicts with the current locked plan."
        suggestions: List[str] = []

        if blocker:
            self._debug(
                f"[CONFLICT] Requested {activity.get('title')} overlaps with fixed {blocker.get('title')}"
            )
            next_start = blocker.get("scheduled_end")
            transition = self._transition_minutes(blocker, activity, 0)
            if next_start is not None:
                suggestions.append(f"Move {activity.get('title')} to {format_clock(next_start + transition)}")
            existing_fixed = self._coerce_minutes(activity.get("fixed_start"))
            if existing_fixed is not None:
                suggestions.append(f"Keep {activity.get('title')} at {format_clock(existing_fixed)}")
            suggestions.append(f"Change {blocker.get('title')} if that commitment can move")

        conflict_payload = {
            "status": "conflict",
            "conflict_target": activity.get("title"),
            "conflict_reason": reason,
            "reason_codes": [self._reason_code_from_text(reason)],
            "suggestions": suggestions,
        }
        envelope = deepcopy(original_envelope)
        envelope["status"] = "conflict"
        envelope["planning_mode"] = self._planning_mode(allow_clash)
        envelope["allow_clash"] = allow_clash
        envelope["conflict"] = conflict_payload
        envelope["conflicts"] = [conflict_payload]
        envelope["unmet_items"] = envelope.get("unmet_items") or []
        envelope["validation_issues"] = envelope.get("validation_issues") or []
        envelope["version"] = current_version
        envelope.setdefault("explanations", [])
        envelope["explanations"] = self._merge_explanations(
            envelope.get("explanations", []),
            [reason],
        )
        envelope["warnings"] = []
        envelope["applied_changes"] = []
        envelope["accepted_with_warnings"] = []
        envelope["rejected_changes"] = [conflict_payload]
        return {
            "status": "conflict",
            "applied": False,
            "envelope": envelope,
            "version": current_version,
            "activities": envelope.get("activities", []),
            "updatedActivities": [],
            "deletedItemIds": [],
            "conflict": conflict_payload,
            "applied_changes": [],
            "accepted_with_warnings": [],
            "rejected_changes": [conflict_payload],
            "warnings": [],
        }

    def apply_operations(
        self, 
        envelope: Dict[str, Any], 
        operations: List[Dict[str, Any]], 
        base_version: int,
        new_date: Optional[str] = None,
        target_date_envelope: Optional[Dict[str, Any]] = None,
        saved_locations: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Apply operations against the canonical activity set, then regenerate derived schedule blocks."""
        current_version = int(envelope.get("version", 1))
        if base_version != current_version:
            raise VersionMismatchError(f"Version mismatch: baseVersion {base_version} != currentVersion {current_version}")

        working_envelope = deepcopy(envelope)
        canonical_activities = self._load_canonical_activities(working_envelope)
        preferences = deepcopy(working_envelope.get("preferences", {}))
        allow_clash = self._resolve_allow_clash(preferences, working_envelope)
        planning_mode = self._planning_mode(allow_clash)
        preferences["allow_clash"] = allow_clash
        preferences["planning_mode"] = planning_mode
        accurate_travel_time = self._resolve_accurate_travel_time(preferences, working_envelope)
        preferences["accurate_travel_time"] = accurate_travel_time
        schedule_date = new_date or working_envelope.get("date") or str(date.today())
        source_turn = current_version + 1

        updated_activities: List[Dict[str, Any]] = []
        deleted_activity_ids: List[str] = []
        mutated_ids: set[str] = set()
        source_plan_date = working_envelope.get("date")
        existing_conflict_identities = self._existing_conflict_identities(working_envelope)
        postconditions: List[Dict[str, Any]] = []
        operations_to_apply = self._prepare_operations_for_apply(
            self._sanitize_operations(operations),
            [item for item in canonical_activities if item.get("status") == "active"],
        )
        explicit_shift_requested = any(
            operation.get("op") == "shift_plan_date"
            for operation in operations_to_apply
        )

        for operation in operations_to_apply:
            op_type = operation.get("op")
            active_pool = [item for item in canonical_activities if item.get("status") == "active"]

            if op_type == "shift_plan_date":
                target_date = operation.get("to_date") or new_date or schedule_date
                if target_date == source_plan_date:
                    self._debug(f"[STATE] Skipping bulk date shift because the plan is already on {source_plan_date}")
                    continue
                if target_date:
                    schedule_date = target_date
                self._debug(f"[STATE] Applying bulk date shift from {source_plan_date} to {schedule_date}")
                mutated_ids.update(
                    item.get("stable_activity_id")
                    for item in active_pool
                    if item.get("stable_activity_id")
                )
                for index, activity in enumerate(canonical_activities):
                    if activity.get("status") != "active":
                        continue
                    shifted = deepcopy(activity)
                    shifted["updated_at"] = self._now_iso()
                    shifted["source_turn"] = source_turn
                    shifted.setdefault("trace", [])
                    shifted["trace"].append(f"Shifted with the whole plan from {source_plan_date} to {schedule_date}.")
                    canonical_activities[index] = shifted
                    updated_activities.append(shifted)
                continue

            if op_type == "add":
                anchor = operation.get("anchor_relation")
                if anchor:
                    resolved_anchor = self._resolve_anchor_relation(anchor, active_pool)
                    if resolved_anchor:
                        operation["anchor_relation"] = resolved_anchor
                        if not operation.get("location"):
                            anchor_activity = self._find_activity_by_stable_id(active_pool, resolved_anchor.get("target_activity_id"))
                            if anchor_activity:
                                operation["location"] = anchor_activity.get("location")
                new_activity = self._canonicalize_activity(
                    operation,
                    source_turn=source_turn,
                    default_source="user_operation",
                )
                canonical_activities.append(new_activity)
                updated_activities.append(new_activity)
                mutated_ids.add(new_activity["stable_activity_id"])
                postcondition = self._operation_postcondition(operation, new_activity, active_pool + [new_activity])
                if postcondition:
                    postconditions.append(postcondition)
                self._debug(f"[STATE] Created new activity '{new_activity['title']}' with activity_id={new_activity['stable_activity_id']}")
                continue

            target_reference = operation.get("target_id") or operation.get("target_title")
            resolution = self._resolve_activity_reference(target_reference, active_pool)
            if resolution.get("status") != "resolved":
                return {
                    "status": "clarification_needed",
                    "applied": False,
                    "envelope": working_envelope,
                    "version": current_version,
                    "activities": working_envelope.get("activities", []),
                    "updatedActivities": [],
                    "deletedItemIds": [],
                    "target_resolution": resolution,
                }

            target = resolution["activity"]
            target_id = target["stable_activity_id"]
            target_index = next(
                index for index, activity in enumerate(canonical_activities)
                if activity.get("stable_activity_id") == target_id
            )

            if op_type == "remove":
                removed = deepcopy(canonical_activities[target_index])
                canonical_activities[target_index]["status"] = "removed"
                canonical_activities[target_index]["updated_at"] = self._now_iso()
                canonical_activities[target_index]["source_turn"] = source_turn
                deleted_activity_ids.append(target_id)
                mutated_ids.add(target_id)
                updated_activities.append(canonical_activities[target_index])
                self._debug(f"[STATE] Marked activity '{removed.get('title')}' as removed")
                continue

            if op_type == "replace":
                canonical_activities[target_index]["status"] = "superseded"
                canonical_activities[target_index]["updated_at"] = self._now_iso()
                replacement = self._canonicalize_activity(
                    operation,
                    source_turn=source_turn,
                    default_source="user_operation",
                )
                canonical_activities.append(replacement)
                updated_activities.append(replacement)
                deleted_activity_ids.append(target_id)
                mutated_ids.add(replacement["stable_activity_id"])
                self._debug(f"[STATE] Superseded '{target.get('title')}' with new activity_id={replacement['stable_activity_id']}")
                continue

            mutated = self._mutate_existing_activity(target, operation, source_turn, active_pool)
            canonical_activities[target_index] = mutated
            updated_activities.append(mutated)
            mutated_ids.add(target_id)
            postcondition = self._operation_postcondition(operation, mutated, active_pool)
            if postcondition:
                postconditions.append(postcondition)
            self._debug(f"[STATE] Updating existing {mutated.get('title')} activity instead of creating new one")

        active_set = [
            deepcopy(item)
            for item in canonical_activities
            if item.get("status") == "active"
        ]

        for item in active_set:
            item["locked_fixed"] = (
                item.get("timing_mode") == TimingMode.FIXED
                and item.get("is_mandatory")
                and item.get("stable_activity_id") not in mutated_ids
            )
            if item["locked_fixed"]:
                self._debug(
                    f"[STATE] Fixed lock preserved for {item.get('title')} at {format_clock(item.get('fixed_start') or item.get('scheduled_start') or 0)}"
                )

        if source_plan_date != schedule_date:
            active_set = self._merge_target_date_context(
                active_set,
                target_date_envelope,
                source_plan_date,
                schedule_date,
                is_explicit_shift=explicit_shift_requested,
            )

        jsection("MODULE_9", f"replanning date={schedule_date}", "REPLAN")
        planned_result = self._plan_schedule(schedule_date, active_set, preferences)
        conflicts = self._build_conflicts(planned_result["activities"], mutated_ids)
        for conflict in conflicts:
            identity = self._conflict_identity(conflict)
            conflict["conflict_lifecycle"] = "existing" if identity in existing_conflict_identities else "new"

        planned_by_id = {
            item.get("stable_activity_id"): item
            for item in planned_result.get("activities", [])
            if item.get("stable_activity_id")
        }
        postcondition_failures = self._validate_postconditions(
            planned_by_id,
            postconditions,
            planned_result.get("activities", []),
        )
        if postcondition_failures and not allow_clash:
            self._debug(f"[CONFLICT] Requested ordering was not satisfied: {postcondition_failures[0].get('reason')}")
            return self._build_postcondition_response(working_envelope, current_version, postcondition_failures, allow_clash=allow_clash)

        blocking_conflicts = [
            conflict for conflict in conflicts
            if any(str(activity_id) in mutated_ids for activity_id in conflict.get("activity_ids") or [])
        ]
        if blocking_conflicts and not allow_clash:
            for mutated_id in mutated_ids:
                planned_activity = planned_by_id.get(mutated_id)
                if planned_activity and planned_activity.get("is_conflict"):
                    if not any(str(mutated_id) in [str(activity_id) for activity_id in conflict.get("activity_ids") or []] for conflict in blocking_conflicts):
                        continue
                    blockers = [
                        planned_by_id.get(blocker_id)
                        for blocker_id in planned_activity.get("conflict_with") or []
                        if planned_by_id.get(blocker_id)
                    ]
                    return self._build_conflict_response(working_envelope, current_version, planned_activity, blockers, allow_clash=allow_clash)

        warnings = self._collect_schedule_warnings(planned_result["activities"], planned_result["schedule_blocks"])
        final_updated_activities = [
            self._format_activity(planned_by_id[activity_id])
            for activity_id in mutated_ids
            if activity_id in planned_by_id
        ]
        formatted_activities = [self._format_activity(item) for item in planned_result["activities"]]
        formatted_unscheduled = [
            self._format_activity(item) for item in planned_result.get("unscheduled_activities", [])
        ]
        envelope_status = "partial" if (conflicts or postcondition_failures) else ("warning" if warnings else "ok")
        result_status = "warning" if envelope_status == "warning" else "success"

        updated_envelope = deepcopy(working_envelope)
        updated_envelope["schema_version"] = 4
        updated_envelope["date"] = schedule_date
        updated_envelope["status"] = envelope_status
        updated_envelope["schedule_status"] = envelope_status
        updated_envelope["planning_mode"] = planning_mode
        updated_envelope["allow_clash"] = allow_clash
        updated_envelope["accurate_travel_time"] = accurate_travel_time
        updated_envelope["preferences"] = preferences
        updated_envelope["activities"] = formatted_activities
        updated_envelope["schedule_blocks"] = planned_result["schedule_blocks"]
        updated_envelope["unscheduled_activities"] = formatted_unscheduled
        updated_envelope["version"] = current_version + 1
        updated_envelope["explanations"] = planned_result.get("explanations", [])
        updated_envelope["conflicts"] = conflicts
        updated_envelope["warnings"] = warnings
        updated_envelope["applied_changes"] = final_updated_activities
        updated_envelope["accepted_with_warnings"] = [
            warning for warning in warnings
            if str(warning.get("activity_id")) in {str(activity_id) for activity_id in mutated_ids}
        ]
        updated_envelope["rejected_changes"] = []
        updated_envelope["unmet_items"] = []
        updated_envelope["validation_issues"] = [failure.get("reason") for failure in postcondition_failures if failure.get("reason")]
        updated_envelope["conflict"] = conflicts[0] if conflicts else ({"type": "postcondition_failed", **postcondition_failures[0]} if postcondition_failures else None)
        updated_envelope["postcondition_results"] = postcondition_failures
        updated_envelope = self._apply_accurate_travel_if_requested(updated_envelope, saved_locations or [])
        envelope_status = updated_envelope.get("status") or envelope_status
        result_status = (
            "warning"
            if envelope_status == "warning"
            else ("conflict" if envelope_status in {"route_conflict", "conflict"} else "success")
        )

        jlog("SUMMARY", "Final schedule", "FINAL")
        summary_lines = []
        for block in updated_envelope.get("schedule_blocks", []):
            st = block.get("start")
            et = block.get("end")
            title = block.get("title")
            b_type = block.get("block_type")
            line = f"  - [{st} - {et}] {title}"
            if b_type == "activity" and block.get("location"):
                line += f" ({block['location']})"
            elif b_type == "transition":
                line += f" ({block.get('duration_minutes')} min)"
            jlog("SUMMARY", line.strip(), "FINAL")
            summary_lines.append(line)
        updated_envelope["final_schedule_summary"] = "\n".join(summary_lines)

        return {
            "status": result_status,
            "applied": True,
            "envelope": updated_envelope,
            "planned_result": planned_result,
            "version": updated_envelope["version"],
            "activities": updated_envelope["activities"],
            "updatedActivities": final_updated_activities,
            "deletedItemIds": deleted_activity_ids,
            "applied_changes": final_updated_activities,
            "accepted_with_warnings": updated_envelope["accepted_with_warnings"],
            "rejected_changes": [],
            "warnings": warnings,
        }

    def _compact_result_summary(
        self,
        result: Dict[str, Any],
        allow_clash: bool,
        parsed: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        envelope = result.get("envelope") or result.get("schedule_data") or {}
        conflict = result.get("conflict") or envelope.get("conflict")
        conflicts = envelope.get("conflicts") or result.get("conflicts") or []
        warnings = envelope.get("accepted_with_warnings") or result.get("accepted_with_warnings") or envelope.get("warnings") or result.get("warnings") or []
        postcondition_results = result.get("postcondition_results") or envelope.get("postcondition_results") or []
        requested_titles = self._requested_titles_from_parsed(parsed or {})
        primary_operation = self._primary_reply_operation(parsed or {})
        shift_operation = self._shift_reply_operation(parsed or {})
        schedule_blocks = list(envelope.get("schedule_blocks") or [])
        changed = result.get("updatedActivities") or envelope.get("applied_changes") or []
        referenced_blocks = self._referenced_activity_blocks(schedule_blocks, requested_titles, changed)
        if not referenced_blocks and envelope.get("activities"):
            referenced_blocks = self._referenced_activity_blocks(schedule_blocks, [], envelope.get("activities", [])[:8])

        status = result.get("status") or envelope.get("status") or ("partial" if conflicts else "success")
        if status == "success" and envelope.get("status") == "warning":
            status = "warning"
        if envelope.get("schedule_status") in {"location_pending", "route_conflict"}:
            status = envelope.get("schedule_status")

        return {
            "status": status,
            "envelope_status": envelope.get("status"),
            "schedule_status": envelope.get("schedule_status"),
            "travel_validation_status": envelope.get("travel_validation_status"),
            "location_resolution_requests": envelope.get("location_resolution_requests") or [],
            "route_conflicts": envelope.get("route_conflicts") or [],
            "applied": bool(result.get("applied", result.get("status") != "conflict")),
            "allow_clash": allow_clash,
            "date": envelope.get("date"),
            "shift_operation": shift_operation,
            "primary_operation": primary_operation,
            "conflict": conflict or (conflicts[0] if conflicts else None),
            "conflicts": conflicts[:3],
            "warnings": warnings[:4],
            "postcondition_results": postcondition_results[:3],
            "requested_titles": requested_titles,
            "referenced_blocks": referenced_blocks[:8],
            "allowed_times": self._allowed_reply_times(referenced_blocks, warnings),
            "allowed_dates": self._allowed_reply_dates(envelope, shift_operation),
            "changed": [
                {
                    "title": block.get("title"),
                    "start": block.get("start"),
                    "end": block.get("end"),
                    "duration_minutes": block.get("duration_minutes"),
                    "is_conflict": bool(block.get("is_conflict") or block.get("isConflict") or block.get("is_conflicting")),
                }
                for block in referenced_blocks[:8]
                if isinstance(block, dict)
            ],
        }

    def _primary_reply_operation(self, parsed: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for operation in parsed.get("operations") or []:
            if not isinstance(operation, dict):
                continue
            op_type = clean_title(operation.get("op") or "")
            if op_type == "remove":
                continue
            anchor = operation.get("anchor_relation") or {}
            return {
                "op": op_type or "update",
                "title": operation.get("title") or operation.get("target_title"),
                "anchor_kind": clean_title(anchor.get("kind") or ""),
                "anchor_title": anchor.get("target_title"),
                "from_date": operation.get("from_date"),
                "to_date": operation.get("to_date"),
            }
        return None

    def _shift_reply_operation(self, parsed: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for operation in parsed.get("operations") or []:
            if not isinstance(operation, dict):
                continue
            if clean_title(operation.get("op") or "") == "shift_plan_date":
                return {
                    "from_date": operation.get("from_date"),
                    "to_date": operation.get("to_date") or parsed.get("date"),
                }
        return None

    def _requested_titles_from_parsed(self, parsed: Dict[str, Any]) -> List[str]:
        titles: List[str] = []
        for item in list(parsed.get("operations") or []) + list(parsed.get("activities") or []):
            if not isinstance(item, dict):
                continue
            title = clean_optional_text(item.get("title") or item.get("target_title"))
            if title and item.get("op") != "remove":
                titles.append(title)
        seen: set[str] = set()
        unique: List[str] = []
        for title in titles:
            key = clean_title(title)
            if key and key not in seen:
                seen.add(key)
                unique.append(title)
        return unique

    def _referenced_activity_blocks(
        self,
        schedule_blocks: List[Dict[str, Any]],
        requested_titles: List[str],
        changed: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        requested_keys = {clean_title(title) for title in requested_titles if title}
        changed_ids = {
            str(item.get("stable_activity_id") or item.get("id"))
            for item in changed
            if isinstance(item, dict) and (item.get("stable_activity_id") or item.get("id"))
        }
        changed_titles = {
            clean_title(item.get("title") or "")
            for item in changed
            if isinstance(item, dict) and item.get("title")
        }
        blocks: List[Dict[str, Any]] = []
        for index, block in enumerate(schedule_blocks):
            if block.get("block_type") != "activity":
                continue
            block_id = str(block.get("stable_activity_id") or block.get("id") or "")
            block_title = clean_title(block.get("title") or "")
            if block_id in changed_ids or block_title in requested_keys or block_title in changed_titles:
                enriched = dict(block)
                previous_activity = next(
                    (
                        schedule_blocks[previous_index]
                        for previous_index in range(index - 1, -1, -1)
                        if schedule_blocks[previous_index].get("block_type") == "activity"
                    ),
                    None,
                )
                next_activity = next(
                    (
                        schedule_blocks[next_index]
                        for next_index in range(index + 1, len(schedule_blocks))
                        if schedule_blocks[next_index].get("block_type") == "activity"
                    ),
                    None,
                )
                if previous_activity:
                    enriched["previous_activity_title"] = previous_activity.get("title")
                if next_activity:
                    enriched["next_activity_title"] = next_activity.get("title")
                if index > 0 and schedule_blocks[index - 1].get("is_tight"):
                    enriched["previous_tight_transition"] = schedule_blocks[index - 1]
                if index + 1 < len(schedule_blocks) and schedule_blocks[index + 1].get("is_tight"):
                    enriched["next_tight_transition"] = schedule_blocks[index + 1]
                blocks.append(enriched)
        return blocks

    def _allowed_reply_times(
        self,
        referenced_blocks: List[Dict[str, Any]],
        warnings: List[Dict[str, Any]],
    ) -> List[str]:
        allowed: set[str] = set()
        for block in referenced_blocks:
            for value in (block.get("start"), block.get("end")):
                parsed = parse_clock(value)
                if parsed is not None:
                    allowed.add(format_clock(parsed))
            for transition_key in ("previous_tight_transition", "next_tight_transition"):
                transition = block.get(transition_key) or {}
                for value in (transition.get("start"), transition.get("end")):
                    parsed = parse_clock(value)
                    if parsed is not None:
                        allowed.add(format_clock(parsed))
        for warning in warnings:
            for value in (warning.get("start"), warning.get("end")):
                parsed = parse_clock(value)
                if parsed is not None:
                    allowed.add(format_clock(parsed))
            transition = warning.get("transition") or {}
            for value in (transition.get("start"), transition.get("end")):
                parsed = parse_clock(value)
                if parsed is not None:
                    allowed.add(format_clock(parsed))
        return sorted(allowed)

    def _allowed_reply_dates(
        self,
        envelope: Dict[str, Any],
        shift_operation: Optional[Dict[str, Any]],
    ) -> List[str]:
        allowed = {value for value in [envelope.get("date")] if value}
        if shift_operation:
            allowed.update(
                value for value in [shift_operation.get("from_date"), shift_operation.get("to_date")]
                if value
            )
        return sorted(allowed)

    def _format_date_for_reply(self, iso_date: Optional[str]) -> str:
        if not iso_date:
            return "the selected date"
        try:
            parsed_date = date.fromisoformat(str(iso_date))
            return f"{parsed_date.strftime('%B')} {parsed_date.day}"
        except Exception:
            return str(iso_date)

    def _success_reply_sentence(self, summary: Dict[str, Any]) -> Optional[str]:
        shift = summary.get("shift_operation") or {}
        if shift.get("to_date"):
            from_text = self._format_date_for_reply(shift.get("from_date"))
            to_text = self._format_date_for_reply(shift.get("to_date"))
            return f"I've moved the whole plan from {from_text} to {to_text}."

        blocks = summary.get("referenced_blocks") or []
        changed = summary.get("changed") or []
        if not blocks and not changed:
            return None

        if len(changed) > 1:
            titles = [item.get("title") for item in changed if item.get("title")]
            if titles:
                date_text = f" for {summary.get('date')}" if summary.get("date") else ""
                return f"I generated your schedule{date_text}, including {', '.join(titles)}."

        block = blocks[0] if blocks else changed[0]
        operation = summary.get("primary_operation") or {}
        op_type = clean_title(operation.get("op") or "")
        verb = {
            "add": "added",
            "move": "moved",
            "update": "updated",
            "replace": "updated",
        }.get(op_type, "updated")
        title = block.get("title") or operation.get("title") or "the activity"
        start = block.get("start")
        end = block.get("end")
        if not start or not end:
            return f"I've {verb} {title}."

        anchor_text = ""
        anchor_kind = clean_title(operation.get("anchor_kind") or "")
        if anchor_kind == "after":
            anchor = block.get("previous_activity_title") or operation.get("anchor_title")
            if anchor:
                anchor_text = f" after {anchor}"
        elif anchor_kind == "before":
            anchor = block.get("next_activity_title") or operation.get("anchor_title")
            if anchor:
                anchor_text = f" before {anchor}"

        return f"I've {verb} {title} from {start} to {end}{anchor_text}."

    def _existing_conflict_sentence(self, summary: Dict[str, Any]) -> Optional[str]:
        existing_conflicts = [
            item for item in (summary.get("conflicts") or [])
            if item.get("conflict_lifecycle") == "existing"
        ]
        if not existing_conflicts:
            return None
        existing = existing_conflicts[0]
        names = "/".join(str(item) for item in (existing.get("activities") or [])[:2])
        if names:
            return f"Your existing {names} clash is still marked."
        return "An existing clash is still marked."

    def _warning_sentence(self, summary: Dict[str, Any]) -> Optional[str]:
        warning = (summary.get("warnings") or [{}])[0]
        if not warning:
            return None
        activity_title = warning.get("activity_title") or "This activity"
        transition = warning.get("transition") or {}
        target = transition.get("to_location") or warning.get("anchor_title") or "the next activity"
        if warning.get("warning_code") == WARNING_TIGHT_TRANSITION:
            return f"{activity_title} still has a tight transition to {target}."
        return warning.get("explanation") or "There is still a warning marked on the schedule."

    def _fallback_result_reply(
        self,
        latest_request: str,
        summary: Dict[str, Any],
    ) -> Dict[str, Any]:
        status = summary.get("status") or "success"
        conflict = summary.get("conflict") or {}
        allow_clash = bool(summary.get("allow_clash"))
        applied = bool(summary.get("applied"))
        reason = (
            conflict.get("conflict_reason")
            or conflict.get("explanation")
            or "The requested change conflicts with the current schedule."
        )
        suggestions = conflict.get("suggestions") or conflict.get("suggested_resolution") or []
        target = conflict.get("conflict_target")
        if not target and conflict.get("activities"):
            target = ", ".join(str(item) for item in conflict.get("activities", [])[:2])

        existing_conflicts = [
            item for item in (summary.get("conflicts") or [])
            if item.get("conflict_lifecycle") == "existing"
        ]

        if status == "location_pending":
            requests = summary.get("location_resolution_requests") or []
            titles = ", ".join(str(req.get("title") or "an activity") for req in requests[:4])
            return {
                "reply": (
                    "I drafted the schedule, but accurate travel time is not complete yet. "
                    f"Please confirm the location for {titles or 'the pending activities'}, then complete travel validation."
                ),
                "reply_status": "location_pending",
                "recommend_allow_clash": False,
                "reply_reason": "Accurate travel time needs confirmed coordinates before final validation.",
            }

        if status == "route_conflict":
            route_conflict = (summary.get("route_conflicts") or [{}])[0]
            reason_text = route_conflict.get("reason") or "An accurate route duration does not fit the current draft."
            return {
                "reply": (
                    f"I drafted the schedule, but accurate route validation found a travel issue: {reason_text} "
                    "I marked the affected transition so it can be adjusted."
                ),
                "reply_status": "conflict",
                "recommend_allow_clash": False,
                "reply_reason": reason_text,
            }

        if status == "conflict" and not applied and not allow_clash:
            suggestion_text = f" A possible option is: {suggestions[0]}." if suggestions else ""
            return {
                "reply": (
                    f"I couldn't apply that change because {reason} "
                    f"Your existing plan was kept unchanged for feasibility.{suggestion_text} "
                    "If you want to force the overlap, turn on Allow Clash and send the request again."
                ),
                "reply_status": "conflict",
                "recommend_allow_clash": True,
                "reply_reason": reason,
            }

        success_sentence = self._success_reply_sentence(summary)
        if applied and success_sentence:
            followups: List[str] = []
            existing_sentence = self._existing_conflict_sentence(summary)
            warning_sentence = self._warning_sentence(summary)
            if existing_sentence:
                followups.append(existing_sentence)
            if warning_sentence:
                followups.append(warning_sentence)
            if summary.get("conflicts") and not existing_sentence:
                followups.append(f"This creates a clash: {reason} I marked it so you can resolve it later.")

            reply_status = "success"
            if summary.get("conflicts") or existing_conflicts:
                reply_status = "partial"
            elif status == "warning" or summary.get("warnings"):
                reply_status = "warning"

            return {
                "reply": " ".join([success_sentence] + followups),
                "reply_status": reply_status,
                "recommend_allow_clash": False,
                "reply_reason": (
                    (existing_conflicts[0].get("explanation") if existing_conflicts else None)
                    or (summary.get("warnings") or [{}])[0].get("explanation")
                ),
            }

        if (status in {"conflict", "partial"} or summary.get("conflicts")) and allow_clash:
            target_text = f" for {target}" if target else ""
            return {
                "reply": (
                    f"I kept your requested change{target_text}, but it creates a clash: {reason} "
                    "I marked it so you can resolve it later."
                ),
                "reply_status": "partial",
                "recommend_allow_clash": False,
                "reply_reason": reason,
            }

        if status == "partial" or summary.get("conflicts"):
            return {
                "reply": "I updated the plan, but part of the result is tight or needs attention. I marked the issue in the schedule.",
                "reply_status": "partial",
                "recommend_allow_clash": False,
                "reply_reason": reason if conflict else None,
            }

        if status == "warning" or summary.get("warnings"):
            first_block = (summary.get("referenced_blocks") or [{}])[0]
            warning = (summary.get("warnings") or [{}])[0]
            transition = warning.get("transition") or first_block.get("next_tight_transition") or {}
            title = first_block.get("title") or warning.get("activity_title") or "the activity"
            start = first_block.get("start") or warning.get("start")
            end = first_block.get("end") or warning.get("end")
            duration = first_block.get("duration_minutes")
            duration_text = f"{duration}-minute " if duration else ""
            anchor_text = f" after {warning.get('anchor_title')}" if warning.get("anchor_title") else ""
            transition_text = ""
            if transition:
                destination = transition.get("to_location") or "the next activity"
                transition_text = (
                    f" The transition to {destination} is tight because travel starts at {transition.get('start')}."
                )
            return {
                "reply": f"I've added your {duration_text}{title} from {start} to {end}{anchor_text}.{transition_text}",
                "reply_status": "warning",
                "recommend_allow_clash": False,
                "reply_reason": warning.get("explanation"),
            }

        changed_titles = [item.get("title") for item in summary.get("changed", []) if item.get("title")]
        if changed_titles:
            if len(changed_titles) > 1:
                date_text = f" for {summary.get('date')}" if summary.get("date") else ""
                return {
                    "reply": f"I generated your schedule{date_text}, including {', '.join(changed_titles)}.",
                    "reply_status": "success",
                    "recommend_allow_clash": False,
                    "reply_reason": None,
                }
            first_change = (summary.get("changed") or [{}])[0]
            if first_change.get("start") and first_change.get("end"):
                return {
                    "reply": f"I updated {first_change.get('title')} from {first_change.get('start')} to {first_change.get('end')}.",
                    "reply_status": "success",
                    "recommend_allow_clash": False,
                    "reply_reason": None,
                }
            return {
                "reply": f"I updated your schedule for: {', '.join(changed_titles)}.",
                "reply_status": "success",
                "recommend_allow_clash": False,
                "reply_reason": None,
            }
        return {
            "reply": "I updated your schedule based on your request.",
            "reply_status": "success",
            "recommend_allow_clash": False,
            "reply_reason": None,
        }

    def _reply_claims_failure(self, reply: str) -> bool:
        clean_reply = clean_title(reply)
        failure_phrases = (
            "not applied",
            "not appropriately slotted",
            "not successfully",
            "could not",
            "couldnt",
            "couldn't",
            "cannot",
            "did not",
            "didnt",
            "didn't",
            "unable",
            "failed",
        )
        return any(phrase in clean_reply for phrase in failure_phrases)

    def _reply_time_mentions(self, reply: str) -> List[str]:
        mentions: List[str] = []
        for match in re.finditer(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\s*(?:AM|PM)?\b", reply, flags=re.IGNORECASE):
            parsed = parse_clock(match.group(0))
            if parsed is not None:
                mentions.append(format_clock(parsed))
        for match in re.finditer(r"\b(?:1[0-2]|0?[1-9])\s*(?:AM|PM)\b", reply, flags=re.IGNORECASE):
            parsed = parse_clock(match.group(0))
            if parsed is not None:
                mentions.append(format_clock(parsed))
        return list(dict.fromkeys(mentions))

    def _reply_uses_only_allowed_times(self, reply: str, summary: Dict[str, Any]) -> bool:
        mentions = self._reply_time_mentions(reply)
        if not mentions:
            return True
        allowed = set(summary.get("allowed_times") or [])
        return all(mention in allowed for mention in mentions)

    def _reply_uses_only_allowed_dates(self, reply: str, summary: Dict[str, Any]) -> bool:
        mentions = self._extract_explicit_absolute_dates(reply, {"date": summary.get("date") or self._local_today_iso()})
        if not mentions:
            return True
        allowed = set(summary.get("allowed_dates") or [])
        return all(mention in allowed for mention in mentions)

    def _reply_mentions_required_range(self, reply: str, summary: Dict[str, Any]) -> bool:
        blocks = summary.get("referenced_blocks") or []
        if len(blocks) != 1:
            return True
        if len(summary.get("requested_titles") or []) != 1:
            return True
        clean_reply = clean_title(reply)
        start = clean_title(blocks[0].get("start") or "")
        end = clean_title(blocks[0].get("end") or "")
        return bool(start and end and start in clean_reply and end in clean_reply)

    def _reply_mentions_requested_titles(self, reply: str, summary: Dict[str, Any]) -> bool:
        requested_titles = summary.get("requested_titles") or []
        if len(requested_titles) <= 1:
            return True
        clean_reply = clean_title(reply)
        return all(clean_title(title) in clean_reply for title in requested_titles)

    def _module_8_reply_class(self, reply_status: str) -> str:
        if reply_status == "warning":
            return "WARNING"
        if reply_status == "location_pending":
            return "WARNING"
        if reply_status == "conflict":
            return "CONFLICT"
        if reply_status in {"error", "failure"}:
            return "FAILURE"
        return "SUCCESS"

    def _log_module_8_final(
        self,
        reply_meta: Dict[str, Any],
        summary: Dict[str, Any],
        source: str,
    ) -> None:
        reply = reply_meta.get("reply") or ""
        reply_status = reply_meta.get("reply_status") or "success"
        referenced = [
            {
                "title": block.get("title"),
                "start": block.get("start"),
                "end": block.get("end"),
            }
            for block in summary.get("referenced_blocks") or []
        ]
        jlog("MODULE_8", f"Reply class={self._module_8_reply_class(reply_status)}", "REPLY")
        jlog("MODULE_8", f"Referenced blocks={json.dumps(referenced, ensure_ascii=True)}", "REPLY")
        jlog("MODULE_8", f"Final reply={json.dumps(reply, ensure_ascii=True)}", "REPLY")
        jlog("MODULE_8", f"Reply source={source}", "REPLY")

    def compose_result_reply(
        self,
        latest_request: str,
        parsed: Dict[str, Any],
        result: Dict[str, Any],
        allow_clash: bool,
    ) -> Dict[str, Any]:
        """Second-pass result-aware reply. The LLM phrases; deterministic checks decide truth."""
        summary = self._compact_result_summary(result, allow_clash, parsed)
        fallback = self._fallback_result_reply(latest_request, summary)

        jsection("MODULE_8", "result-aware reply", "REPLY")

        if fallback.get("reply_status") == "location_pending":
            fallback = {**fallback, "reply_source": "template"}
            self._log_module_8_final(fallback, summary, "template")
            return fallback

        if not getattr(self.client, "models", None):
            fallback = {**fallback, "reply_source": "template"}
            self._log_module_8_final(fallback, summary, "template")
            return fallback

        prompt = f"""
You are JPlan's final response writer. Use ONLY this scheduling result.
Write naturally, like a helpful planning assistant, not a formal system notice.
Keep it short: 1-3 sentences.
If applied=false, clearly say the requested change was not applied as intended, but do not use all-caps.
If RESULT_SUMMARY includes timing details, mention the concrete reason, such as the available window and required duration.
If status is ok/success/warning and applied=true, say the schedule was applied.
If status=warning, say the activity was added/updated and mention the warning naturally.
If allow_clash=false and status=conflict, gently mention Allow Clash only as an option to force the overlap.
Do not claim success unless applied=true.
Do not invent reasons, times, blockers, or suggestions outside RESULT_SUMMARY.
Use exact times from RESULT_SUMMARY.referenced_blocks only. Do not change, round, or guess times.
Use exact dates from RESULT_SUMMARY.allowed_dates only. For shift_plan_date, use RESULT_SUMMARY.shift_operation.to_date.
For a single requested activity, include that activity's exact start and end time.
For a generated schedule, mention every title in RESULT_SUMMARY.requested_titles that appears in referenced_blocks.
Do not mention unslotted or failed tasks unless RESULT_SUMMARY says there is a conflict or unmet item.

USER_REQUEST:
{latest_request}

PARSED_OPERATIONS:
{json.dumps(parsed.get("operations") or parsed.get("activities") or [], ensure_ascii=True)[:1200]}

RESULT_SUMMARY:
{json.dumps(summary, ensure_ascii=True)[:1800]}
"""
        try:
            response = self.client.models.generate_content(
                model="gemini-3.1-flash-lite-preview",
                contents=prompt,
            )
            usage = getattr(response, "usage_metadata", None)
            token_usage = None
            if usage:
                token_usage = {
                    "prompt": int(getattr(usage, "prompt_token_count", 0) or 0),
                    "candidates": int(getattr(usage, "candidates_token_count", 0) or 0),
                    "total": int(getattr(usage, "total_token_count", 0) or 0),
                }
                jlog(
                    "MODULE_8",
                    f"Prompt={token_usage['prompt']} | Candidates={token_usage['candidates']} | Total={token_usage['total']}",
                    "TOKEN",
                )
            reply = (response.text or "").strip()
            if not reply:
                final = {**fallback, "token_usage": token_usage, "reply_source": "fallback-template"}
                self._log_module_8_final(final, summary, "fallback-template")
                return final

            if fallback["reply_status"] == "conflict":
                if not self._reply_claims_failure(reply):
                    final = {**fallback, "token_usage": token_usage, "reply_source": "fallback-template"}
                    self._log_module_8_final(final, summary, "fallback-template")
                    return final
            elif fallback["reply_status"] in {"success", "warning"} and self._reply_claims_failure(reply):
                final = {**fallback, "token_usage": token_usage, "reply_source": "fallback-template"}
                self._log_module_8_final(final, summary, "fallback-template")
                return final
            elif fallback["reply_status"] == "partial" and summary.get("conflicts"):
                clean_reply = clean_title(reply)
                if "clash" not in clean_reply and "conflict" not in clean_reply and "overlap" not in clean_reply:
                    final = {**fallback, "token_usage": token_usage, "reply_source": "fallback-template"}
                    self._log_module_8_final(final, summary, "fallback-template")
                    return final

            if not self._reply_uses_only_allowed_times(reply, summary):
                final = {**fallback, "token_usage": token_usage, "reply_source": "fallback-template"}
                self._log_module_8_final(final, summary, "fallback-template")
                return final

            if not self._reply_uses_only_allowed_dates(reply, summary):
                final = {**fallback, "token_usage": token_usage, "reply_source": "fallback-template"}
                self._log_module_8_final(final, summary, "fallback-template")
                return final

            if not self._reply_mentions_required_range(reply, summary):
                final = {**fallback, "token_usage": token_usage, "reply_source": "fallback-template"}
                self._log_module_8_final(final, summary, "fallback-template")
                return final

            if not self._reply_mentions_requested_titles(reply, summary):
                final = {**fallback, "token_usage": token_usage, "reply_source": "fallback-template"}
                self._log_module_8_final(final, summary, "fallback-template")
                return final

            final = {
                **fallback,
                "reply": reply,
                "token_usage": token_usage,
                "reply_source": "llm",
            }
            self._log_module_8_final(final, summary, "llm")
            return final
        except Exception as exc:
            jlog("MODULE_8", f"Result-aware reply failed: {exc}", "ERROR")
            final = {**fallback, "reply_source": "fallback-template"}
            self._log_module_8_final(final, summary, "fallback-template")
            return final

    def _format_activity(self, item: Dict[str, Any]) -> Dict[str, Any]:
        s_start = item.get("scheduled_start")
        s_end = item.get("scheduled_end")

        if s_start is None and item.get("startTime"):
            s_start = parse_clock(item["startTime"])
        if s_end is None and item.get("endTime"):
            s_end = parse_clock(item["endTime"])

        if s_start is None:
            s_start = item.get("fixed_start")
        if s_end is None:
            s_end = item.get("fixed_end") or (s_start + (item.get("duration_minutes") or 60) if s_start is not None else None)

        s_start_val = s_start if s_start is not None else 0
        s_end_val = s_end if s_end is not None else s_start_val + 60

        conflict_with = item.get("conflict_with") or []
        if isinstance(conflict_with, str):
            conflict_with = [conflict_with]

        stable_activity_id = item.get("stable_activity_id") or item.get("id") or "unknown"
        fixed_start = item.get("fixed_start")
        fixed_end = item.get("fixed_end")
        earliest_start = item.get("earliest_start")
        latest_end = item.get("latest_end")
        preferred_start = item.get("preferred_start")
        location = clean_optional_text(item.get("location"))

        return {
            "id": stable_activity_id,
            "stable_activity_id": stable_activity_id,
            "type": item.get("type", "activity"),
            "entity_type": item.get("entity_type", "activity"),
            "activity_type": item.get("activity_type") or ("mandatory" if item.get("is_mandatory", True) else "optional"),
            "title": item.get("title", "Untitled"),
            "normalized_title": item.get("normalized_title") or clean_title(item.get("title", "Untitled")),
            "startTime": format_clock(s_start_val),
            "endTime": format_clock(s_end_val),
            "location": location,
            "location_label": clean_optional_text(item.get("location_label")) or location,
            "location_category": item.get("location_category"),
            "location_status": item.get("location_status"),
            "location_source": item.get("location_source"),
            "location_confidence": item.get("location_confidence"),
            "location_normalized": item.get("location_normalized") or _normalize_location(location),
            "saved_location_label": item.get("saved_location_label"),
            "resolved_location": deepcopy(item.get("resolved_location")) if isinstance(item.get("resolved_location"), dict) else None,
            "raw_llm_location": item.get("raw_llm_location"),
            "explicit_user_location": bool(item.get("explicit_user_location", False)),
            "location_warning": item.get("location_warning"),
            "duration": item.get("duration") or self._duration_label(item.get("duration_minutes") or (s_end_val - s_start_val)),
            "duration_minutes": int(item.get("duration_minutes") or (s_end_val - s_start_val)),
            "priority": item.get("priority", "medium"),
            "isMandatory": bool(item.get("is_mandatory", True) if "is_mandatory" in item else item.get("isMandatory", True)),
            "timing_mode": item.get("timing_mode") or item.get("timingMode") or TimingMode.UNSPECIFIED,
            "fixed_start": fixed_start,
            "fixed_end": fixed_end,
            "earliest_start": earliest_start,
            "latest_end": latest_end,
            "preferred_start": preferred_start,
            "anchor_relation": deepcopy(item.get("anchor_relation")),
            "sequence_index": item.get("sequence_index"),
            "status": item.get("status", "active"),
            "source_turn": item.get("source_turn"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "notes": item.get("notes"),
            "explanation": item["trace"][-1] if item.get("trace") else item.get("explanation"),
            "trace": item.get("trace", []),
            "source": item.get("source", "planner"),
            "isConflict": bool(item.get("is_conflict") or item.get("isConflict", False)),
            "is_conflicting": bool(item.get("is_conflict") or item.get("isConflict", False)),
            "conflict_ids": list(item.get("conflict_ids") or []),
            "conflictWith": conflict_with,
            "conflictReason": item.get("conflict_reason") or item.get("conflictReason"),
            "conflictPriority": item.get("conflict_priority"),
            "conflictSeverity": item.get("conflict_severity"),
            "reason_codes": list(item.get("reason_codes") or []),
            "accepted_with_warning": bool(item.get("accepted_with_warning")),
            "warning_code": item.get("warning_code"),
            "warnings": list(item.get("warnings") or []),
            "scheduled_start": s_start_val,
            "scheduled_end": s_end_val,
            "prep_buffer": item.get("prep_buffer", DEFAULT_PREP_BUFFER),
            "aliases": list(item.get("aliases") or []),
        }

    def _materialize_schedule(
        self,
        schedule_date: str,
        timeline: List[Dict[str, Any]],
        unscheduled: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        ordered = sorted(timeline, key=lambda item: item["scheduled_start"])
        valid_ids = {item["id"] for item in ordered}
        activities: List[Dict[str, Any]] = []
        explanations: List[str] = []
        activities = [self._format_activity(item) for item in ordered]

        expanded: List[Dict[str, Any]] = []
        for index, activity in enumerate(activities):
            expanded.append(activity)
            if index == len(activities) - 1:
                continue

            current = ordered[index]
            nxt = ordered[index + 1]
            if nxt["scheduled_start"] < current["scheduled_end"]:
                continue

            prep_minutes = max(
                current.get("prep_buffer", DEFAULT_PREP_BUFFER),
                nxt.get("prep_buffer", DEFAULT_PREP_BUFFER),
            )
            travel_minutes = estimate_travel_minutes(current.get("location"), nxt.get("location"))

            block_cursor = current["scheduled_end"]
            # UI Fix: Only show buffers if they are substantial (> 5 minutes) to avoid clutter
            if prep_minutes >= 5:
                expanded.append({
                    "id": f"buffer-{current['id']}-{nxt['id']}",
                    "type": "buffer",
                    "title": "Prep / Buffer",
                    "startTime": format_clock(block_cursor),
                    "endTime": format_clock(block_cursor + prep_minutes),
                    "duration": self._duration_label(prep_minutes),
                })
                block_cursor += prep_minutes

            if travel_minutes >= 5:
                expanded.append({
                    "id": f"travel-{current['id']}-{nxt['id']}",
                    "type": "travel",
                    "title": f"Travel to {nxt['title']}",
                    "startTime": format_clock(block_cursor),
                    "endTime": format_clock(block_cursor + travel_minutes),
                    "duration": self._duration_label(travel_minutes),
                    "location": nxt.get("location"),
                })

        for item in ordered:
            if item.get("trace"):
                explanations.extend(item["trace"][-2:])
            if item.get("is_conflict"):
                explanations.append(
                    f"{item['title']} was kept even though it clashes with existing activities."
                )

        return {
            "date": schedule_date,
            "activities": expanded,
            "explanations": self._merge_explanations(explanations),
            "unscheduled_activities": [
                {
                    "title": item["title"],
                    "reason": item["trace"][-1] if item.get("trace") else "No feasible slot was available.",
                    "priority": item.get("priority", "medium"),
                    "isMandatory": item.get("is_mandatory", True),
                }
                for item in unscheduled
            ],
        }

    def _merge_explanations(self, *groups: List[str]) -> List[str]:
        merged: List[str] = []
        seen = set()
        for group in groups:
            for explanation in group:
                key = (explanation or "").strip()
                if key and key not in seen:
                    seen.add(key)
                    merged.append(key)
        return merged

    def _safe_json_loads(self, raw_text: str) -> Optional[Dict[str, Any]]:
        clean_text = raw_text.replace("```json", "").replace("```", "").strip()
        try:
            return json.loads(clean_text)
        except json.JSONDecodeError:
            start = clean_text.find("{")
            end = clean_text.rfind("}") + 1
            if start == -1 or end <= 0:
                return None
            try:
                return json.loads(clean_text[start:end])
            except:
                return None

    def _duration_label(self, minutes: int) -> str:
        if minutes % 60 == 0:
            hours = minutes // 60
            return f"{hours} hour" if hours == 1 else f"{hours} hours"
        hours = minutes // 60
        mins = minutes % 60
        if hours:
            return f"{hours}h {mins}m"
        return f"{mins} mins"

    def _make_id(self, title: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", clean_title(title)).strip("-") or "activity"
        suffix = abs(hash(title)) % 100000
        return f"{slug}-{suffix}"

    def _local_now(self) -> datetime:
        try:
            return datetime.now(ZoneInfo(DEFAULT_LOCAL_TIMEZONE))
        except Exception:
            return datetime.now(timezone(timedelta(hours=8)))

    def _local_today_iso(self) -> str:
        return self._local_now().date().isoformat()

    def _local_datetime_context(self) -> str:
        local_now = self._local_now()
        return (
            f"Current local datetime: {local_now.strftime('%Y-%m-%d %H:%M')} "
            f"{DEFAULT_LOCAL_TIMEZONE}\n"
            f"Current local date: {local_now.date().isoformat()}\n"
        )

class VersionMismatchError(Exception):
    """Raised when the baseVersion does not match the currentVersion in the backend."""
    pass
