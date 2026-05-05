import json
import re
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4
from zoneinfo import ZoneInfo

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


# [Module B/C Knowledge] Mock Database for Locations and Travel Times
LOCATION_ALIASES = {
    "home": "Home",
    "house": "Home",
    "office": "Main Office",
    "main office": "Main Office",
    "campus": "MMU",
    "mmu": "MMU",
    "university": "MMU",
    "gym": "Fitness Center",
    "fitness center": "Fitness Center",
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
        "MMU": 30
    }
}

def _normalize_location(raw_name: Optional[str]) -> Optional[str]:
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
        print(f"[JPLAN][MODULE_B] Found Travel Time: '{loc1}' -> '{loc2}' = {travel_time}m")
        return travel_time
    
    # Fallback to default travel time
    return 20


# Utility functions for location and travel


def normalize_optional_text(value: Any) -> str:
    if value is None:
        return ""
    return clean_title(str(value))


class SchedulingEngine:
    def __init__(self, client: Any):
        self.client = client

    def _debug(self, message: str) -> None:
        print(f"[JPLAN][ENGINE_LOGIC] {message}")

    def _debug_json(self, label: str, payload: Any) -> None:
        try:
            serialized = json.dumps(payload, indent=2, ensure_ascii=True)
        except Exception:
            serialized = repr(payload)
        print(f"[JPLAN][ENGINE_LOGIC] {label}:\n{serialized}")


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
    ) -> Dict[str, Any]:
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

        canonical_activities = self._load_canonical_activities(current_schedule)
        existing_active = [item for item in canonical_activities if item.get("status") == "active"]
        requested_operations = list(parsed.get("operations") or [])
        if not requested_operations:
            requested_operations = [{**activity, "op": "add"} for activity in (parsed.get("activities") or [])]

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
        status = "partial" if conflicts else "ok"

        schedule_data = {
            "schema_version": 4,
            "scheduleId": (current_schedule or {}).get("scheduleId"),
            "date": schedule_date,
            "status": status,
            "planning_mode": planning_mode,
            "allow_clash": allow_clash,
            "version": max(1, source_turn),
            "preferences": preferences,
            "activities": [self._format_activity(item) for item in planned_result["activities"]],
            "schedule_blocks": planned_result["schedule_blocks"],
            "unscheduled_activities": [self._format_activity(item) for item in planned_result.get("unscheduled_activities", [])],
            "explanations": self._merge_explanations(parsed.get("explanations", []), planned_result.get("explanations", [])),
            "conflicts": conflicts,
            "unmet_items": [],
            "validation_issues": [],
        }

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
        print("\n" + "*"*60)
        print(" [JPLAN] STARTING MODULE A - LLM PARSING")
        print("*"*60)
        self._debug(
            f"[MODULE_A] Request: {latest_request!r}"
        )
        
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
            response = self.client.models.generate_content(
                model="gemini-3.1-flash-lite-preview",
                contents=contents,
                config={"response_mime_type": "application/json"},
            )
            raw_response_text = response.text or ""
            # print(f"\n[JPLAN][LLM_PARSER] --- RAW RESPONSE FROM LLM ---\n{raw_response_text}\n------------------------------------------\n")
            
            # Print Token Usage
            usage = getattr(response, "usage_metadata", None)
            token_usage = None
            if usage:
                token_usage = {
                    "prompt": int(getattr(usage, "prompt_token_count", 0) or 0),
                    "candidates": int(getattr(usage, "candidates_token_count", 0) or 0),
                    "total": int(getattr(usage, "total_token_count", 0) or 0),
                }
                print(f"\n[JPLAN][API] [TOKEN_USAGE] Prompt: {usage.prompt_token_count} | Candidates: {usage.candidates_token_count} | Total: {usage.total_token_count}")
            
            parsed = self._safe_json_loads(raw_response_text)
            if isinstance(parsed, dict):
                raw_llm_reply = str(parsed.get("reply") or "").strip() or None
        except json.JSONDecodeError as exc:
            self._debug(f"LLM parse exception | type={type(exc).__name__} | message={str(exc)}")
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
        self._debug_json("LLM parsed request", parsed)
        self._debug(
            f"Parsed request | intent={parsed.get('intent')} | parsed_date={parsed.get('date')} | activities={len(parsed.get('activities', []))} | operations={len(parsed.get('operations', []))}"
        )
        return parsed

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

    def _request_implies_whole_plan_shift(
        self,
        latest_request: str,
        current_schedule: Optional[Dict[str, Any]],
        parsed: Dict[str, Any],
    ) -> bool:
        if not current_schedule or not parsed.get("date"):
            return False

        request_text = clean_title(latest_request)
        current_date = (current_schedule or {}).get("date")
        parsed_date = parsed.get("date")
        if not current_date or not parsed_date or parsed_date == current_date:
            return False

        whole_plan_patterns = [
            r"\bmove (this|the|these)? ?(plan|schedule|day)\b",
            r"\bshift (this|the|whole|entire)? ?(plan|schedule|day)\b",
            r"\bmove everything\b",
            r"\bmove all\b",
            r"\bwhole plan\b",
            r"\bentire plan\b",
            r"\bwrong date\b",
            r"\bi said wrong about the date\b",
            r"\bnot .* make it\b",
        ]

        if any(re.search(pattern, request_text) for pattern in whole_plan_patterns):
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
        normalized = deepcopy(parsed)
        if not self._request_implies_whole_plan_shift(latest_request, current_schedule, normalized):
            return normalized

        from_date = (current_schedule or {}).get("date")
        to_date = normalized.get("date")
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
                "notes": act.get("notes"),
                "is_conflict": act.get("is_conflict", False),
                "is_conflicting": act.get("is_conflict", False),
                "conflict_ids": list(act.get("conflict_ids") or []),
                "reason": "Scheduled activity"
            })
            
            last_end = end_min
            last_loc = current_loc

        return blocks

    def _build_response(
        self,
        parsed: Dict[str, Any],
        current_schedule: Optional[Dict[str, Any]],
        latest_request: str,
    ) -> Dict[str, Any]:
        reply = parsed.get("reply") or "I've updated your schedule."
        transcription = parsed.get("transcription") or latest_request
        intent = parsed.get("intent", "edit")

        if intent == "chat" and not parsed.get("operations"):
            return {
                "reply": reply,
                "transcription": transcription,
                "schedule_data": None,
            }

        # For "edit" intent, we continue to generate the materialized schedule
        date = parsed.get("date") or self._local_today_iso()
        all_activities = self._get_base_activities(current_schedule)
        
        # After apply_operations and other logic, we usually get the finalized activities
        # Note: In a real flow, apply_operations is called BEFORE build_schedule_response
        blocks = self._materialize_blocks(all_activities, DEFAULT_DAY_START, 0)
        
        return {
            "date": date,
            "reply": reply,
            "transcription": transcription,
            "intent": intent,
            "activities": all_activities,
            "schedule_blocks": blocks,
            "explanations": parsed.get("explanations", []),
            "version": (current_schedule.get("version", 1) if current_schedule else 1) + 1
        }

        schedule_date = self._resolve_schedule_date(
            parsed=parsed,
            current_schedule=current_schedule,
            latest_request=latest_request,
        )
        self._debug(
            f"Building schedule response | intent={intent} | resolved_date={schedule_date} | merged_activity_count={len(merged_activities)}"
        )
        preferences = parsed.get("preferences") or {}
        planned_schedule = self._plan_schedule(schedule_date, merged_activities, preferences)
        if (current_schedule or {}).get("activities"):
            planned_schedule.setdefault("explanations", [])
            planned_schedule["explanations"].insert(0, f"Merged into existing plan for {schedule_date}.")

        if planned_schedule["unscheduled_activities"]:
            skipped = ", ".join(item["title"] for item in planned_schedule["unscheduled_activities"][:3])
            reply = f"{reply} I kept the plan feasible and could not fit: {skipped}."

        return {
            "reply": reply,
            "transcription": transcription,
            "schedule_data": planned_schedule,
        }

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

    def _merge_with_current_schedule(
        self,
        parsed: Dict[str, Any],
        current_schedule: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        existing_map: Dict[str, Dict[str, Any]] = {}
        title_buckets: Dict[str, List[Dict[str, Any]]] = {}
        for item in (current_schedule or {}).get("activities", []):
            if item.get("type") != "activity":
                continue
            normalized = self._normalized_existing_activity(item)
            existing_map[self._activity_identity(normalized)] = normalized
            title_buckets.setdefault(clean_title(normalized["title"]), []).append(normalized)

        incoming = []
        for raw in self._collect_requested_items(parsed, current_schedule):
            normalized = self._normalize_requested_activity(raw)
            if normalized:
                incoming.append(normalized)
        self._debug(
            f"Merging schedules | existing={len(existing_map)} | incoming={len(incoming)} | intent={parsed.get('intent')}"
        )

        if not existing_map:
            return incoming

        if parsed.get("intent") not in {"edit", "schedule"}:
            return list(existing_map.values()) + incoming if existing_map else incoming

        merged = existing_map
        for item in incoming:
            existing = self._find_existing_match(item, merged, title_buckets)
            if item.get("remove"):
                if existing:
                    self._debug(f"Removing existing activity | title={existing.get('title')} | id={existing.get('id')}")
                    merged.pop(self._activity_identity(existing), None)
                    title_key = clean_title(existing["title"])
                    title_buckets[title_key] = [
                        candidate for candidate in title_buckets.get(title_key, [])
                        if candidate.get("id") != existing.get("id")
                    ]
                continue

            if existing:
                self._debug(
                    f"Merging onto existing activity | incoming={item.get('title')} | existing_id={existing.get('id')}"
                )
                updated = self._merge_activity(existing, item)
                old_key = self._activity_identity(existing)
                new_key = self._activity_identity(updated)
                merged.pop(old_key, None)
                merged[new_key] = updated

                title_key = clean_title(existing["title"])
                title_buckets[title_key] = [
                    updated if candidate.get("id") == existing.get("id") else candidate
                    for candidate in title_buckets.get(title_key, [])
                ]
            else:
                self._debug(f"Adding new activity from request | title={item.get('title')} | id={item.get('id')}")
                merged[self._activity_identity(item)] = item
                title_buckets.setdefault(clean_title(item["title"]), []).append(item)
        return list(merged.values())

    def _collect_requested_items(
        self,
        parsed: Dict[str, Any],
        current_schedule: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        items = list(parsed.get("activities") or [])
        operations = parsed.get("operations") or []
        if not operations:
            return items
        items.extend(self._operations_to_activities(operations, current_schedule))
        return items

    def _operations_to_activities(
        self,
        operations: List[Dict[str, Any]],
        current_schedule: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        translated: List[Dict[str, Any]] = []
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            op = clean_title(operation.get("op") or "")
            if op == "shift_plan_date":
                continue
            target = self._find_existing_schedule_activity(
                current_schedule,
                operation.get("target_id"),
                operation.get("target_title"),
            )
            target_title = operation.get("target_title") or (target or {}).get("title")
            base_title = operation.get("title") or target_title or "Activity"

            if op == "remove":
                translated.append({
                    "target_id": operation.get("target_id") or (target or {}).get("id"),
                    "title": target_title or base_title,
                    "duration_minutes": operation.get("duration_minutes") or parse_duration_minutes((target or {}).get("duration")),
                    "fixed_start": operation.get("fixed_start") or (target or {}).get("startTime"),
                    "fixed_end": operation.get("fixed_end") or (target or {}).get("endTime"),
                    "location": operation.get("location") if operation.get("location") is not None else (target or {}).get("location"),
                    "priority": operation.get("priority") or (target or {}).get("priority") or "medium",
                    "is_mandatory": operation.get("is_mandatory") if operation.get("is_mandatory") is not None else (target or {}).get("isMandatory", True),
                    "remove": True,
                    "notes": operation.get("notes") or "Removed based on the parsed user request.",
                })
                continue

            if op == "replace" and target_title:
                translated.append({
                    "target_id": operation.get("target_id") or (target or {}).get("id"),
                    "title": target_title,
                    "duration_minutes": parse_duration_minutes((target or {}).get("duration")),
                    "fixed_start": (target or {}).get("startTime"),
                    "fixed_end": (target or {}).get("endTime"),
                    "location": (target or {}).get("location"),
                    "priority": (target or {}).get("priority") or "medium",
                    "is_mandatory": (target or {}).get("isMandatory", True),
                    "remove": True,
                    "notes": operation.get("notes") or f"Replaced {target_title} based on the parsed user request.",
                })

            translated.append({
                "target_id": operation.get("target_id") if op in {"update", "move", "update_priority"} else None,
                "title": base_title,
                "duration_minutes": operation.get("duration_minutes") or parse_duration_minutes((target or {}).get("duration")),
                "fixed_start": operation.get("fixed_start"),
                "fixed_end": operation.get("fixed_end"),
                "earliest_start": operation.get("earliest_start"),
                "latest_end": operation.get("latest_end"),
                "location": operation.get("location") if operation.get("location") is not None else (target or {}).get("location"),
                "priority": operation.get("priority") or (target or {}).get("priority") or "medium",
                "is_mandatory": operation.get("is_mandatory") if operation.get("is_mandatory") is not None else (target or {}).get("isMandatory", True),
                "remove": False,
                "notes": operation.get("notes") or f"{op or 'update'} operation translated from parsed user request.",
            })
        self._debug_json("Translated operations", translated)
        return translated

    def _find_existing_schedule_activity(
        self,
        current_schedule: Optional[Dict[str, Any]],
        target_id: Any,
        target_title: Any,
    ) -> Optional[Dict[str, Any]]:
        normalized_target_id = normalize_optional_text(target_id)
        normalized_target_title = normalize_optional_text(target_title)
        for item in (current_schedule or {}).get("activities", []):
            if item.get("type") != "activity":
                continue
            if normalized_target_id and normalize_optional_text(item.get("id")) == normalized_target_id:
                return item
            if normalized_target_title and clean_title(item.get("title", "")) == normalized_target_title:
                return item
        return None

    def _activity_identity(self, activity: Dict[str, Any]) -> str:
        source = normalize_optional_text(activity.get("source"))
        act_id = normalize_optional_text(activity.get("id"))
        if source and act_id:
            return f"id:{source}:{act_id}"

        title = clean_title(activity.get("title", ""))
        location = normalize_optional_text(activity.get("location"))
        start = activity.get("fixed_start")
        if start is None:
            start = activity.get("scheduled_start")
        end = activity.get("fixed_end")
        if end is None:
            end = activity.get("scheduled_end")
        return f"fp:{title}|{start}|{end}|{location}"

    def _find_existing_match(
        self,
        incoming: Dict[str, Any],
        merged: Dict[str, Dict[str, Any]],
        title_buckets: Dict[str, List[Dict[str, Any]]],
    ) -> Optional[Dict[str, Any]]:
        target_id = normalize_optional_text(incoming.get("target_id"))
        if target_id:
            for candidate in merged.values():
                if normalize_optional_text(candidate.get("id")) == target_id:
                    return candidate

        identity = self._activity_identity(incoming)
        if identity in merged:
            return merged[identity]

        candidates = title_buckets.get(clean_title(incoming["title"]), [])
        if not candidates:
            return None

        incoming_start = incoming.get("fixed_start") or incoming.get("scheduled_start")
        incoming_location = normalize_optional_text(incoming.get("location"))

        exact_time_candidates = []
        for candidate in candidates:
            candidate_start = candidate.get("fixed_start") or candidate.get("scheduled_start")
            candidate_location = normalize_optional_text(candidate.get("location"))
            if incoming_start is not None and candidate_start == incoming_start:
                if not incoming_location or incoming_location == candidate_location:
                    exact_time_candidates.append(candidate)

        if len(exact_time_candidates) == 1:
            return exact_time_candidates[0]

        if len(candidates) == 1:
            candidate = candidates[0]
            candidate_location = normalize_optional_text(candidate.get("location"))
            if not incoming_location or incoming_location == candidate_location:
                return candidate

        if incoming.get("remove") and len(candidates) == 1:
            return candidates[0]

        return None

    def _merge_activity(self, existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
        merged = deepcopy(existing)
        for field in (
            "duration_minutes",
            "location",
            "priority",
            "is_mandatory",
            "earliest_start",
            "latest_end",
            "fixed_start",
            "fixed_end",
            "notes",
        ):
            if incoming.get(field) is not None:
                merged[field] = incoming[field]

        merged["source"] = "edited_request"
        merged["trace"] = existing.get("trace", []) + [
            incoming.get("notes") or "Adjusted from the latest user instruction."
        ]
        merged["is_conflict"] = False
        merged["conflict_with"] = []
        merged["conflict_reason"] = None
        merged["conflict_priority"] = None
        merged["conflict_severity"] = None

        if incoming.get("fixed_start") is not None:
            # FORCE CAST to int to prevent 'str + int' errors
            try:
                fs_val = incoming.get("fixed_start")
                fs = parse_clock(fs_val) if isinstance(fs_val, str) else int(fs_val)
                
                fe_val = incoming.get("fixed_end")
                fe = parse_clock(fe_val) if isinstance(fe_val, str) else (int(fe_val) if fe_val is not None else None)
                
                duration = int(merged.get("duration_minutes", 60))
                
                if fs is not None:
                    merged["fixed_start"] = fs
                    merged["scheduled_start"] = fs
                    merged["scheduled_end"] = fe or (fs + duration)
                if fe is not None:
                    merged["fixed_end"] = fe
            except (ValueError, TypeError):
                pass
        else:
            merged.pop("scheduled_start", None)
            merged.pop("scheduled_end", None)
        
        # Ensure duration_minutes is always present in merged
        if "duration_minutes" not in merged:
            merged["duration_minutes"] = (merged.get("scheduled_end") or 0) - (merged.get("scheduled_start") or 0)
            if merged["duration_minutes"] <= 0:
                merged["duration_minutes"] = 60
        return merged

    def _normalized_existing_activity(self, item: Dict[str, Any]) -> Dict[str, Any]:
        start = parse_clock(item.get("startTime"))
        end = parse_clock(item.get("endTime"))
        if start is None:
            start = DEFAULT_DAY_START
        if end is None:
            end = start + parse_duration_minutes(item.get("duration"))
        if end <= start:
            end += 24 * 60

        return {
            "id": item.get("id") or self._make_id(item.get("title", "activity")),
            "title": item.get("title", "Untitled Activity"),
            "duration_minutes": end - start,
            "location": item.get("location"),
            "priority": item.get("priority", "medium"),
            "is_mandatory": bool(item.get("isMandatory", True)),
            "earliest_start": start,
            "latest_end": end,
            "fixed_start": start,
            "fixed_end": end,
            "prep_buffer": DEFAULT_PREP_BUFFER,
            "notes": "Existing activity preserved from the current schedule.",
            "source": "current_schedule",
            "trace": ["Started from the current confirmed schedule."],
            "is_conflict": False,
            "conflict_with": [],
            "conflict_reason": None,
            "conflict_priority": None,
            "conflict_severity": None,
        }

    def _normalize_requested_activity(self, raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        title = str(raw.get("title") or raw.get("target_title") or "").strip()

        fixed_start = parse_clock(raw.get("fixed_start"))
        fixed_end = parse_clock(raw.get("fixed_end"))
        earliest_start = parse_clock(raw.get("earliest_start"))
        latest_end = parse_clock(raw.get("latest_end"))
        duration_minutes = parse_duration_minutes(raw.get("duration_minutes"))

        # Detect block type based on title
        item_type = "activity"
        lower_title = title.lower()
        if "travel" in lower_title:
            item_type = "travel"
        elif "buffer" in lower_title:
            item_type = "buffer"

        if fixed_start is not None and fixed_end is None:
            fixed_end = fixed_start + duration_minutes
        if fixed_end is not None and fixed_start is None:
            fixed_start = fixed_end - duration_minutes
        if fixed_end is not None and fixed_start is not None and fixed_end <= fixed_start:
            fixed_end += 24 * 60

        return {
            "id": self._make_id(title),
            "title": title,
            "notes": raw.get("notes") or "Derived from the latest user request.",
            "remove": bool(raw.get("remove")),
            "source": "llm_parse",
            "trace": [raw.get("notes") or "Added from the parsed user request."],
            "is_conflict": bool(raw.get("isConflict")),
            "conflict_with": list(raw.get("conflictWith") or []),
            "conflict_reason": raw.get("conflictReason"),
            "conflict_priority": raw.get("conflictPriority"),
            "conflict_severity": raw.get("conflictSeverity"),
        }

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

        print(f"[JPLAN][MODULE_C] Pass 1: Categorizing {len(activities)} items...")
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
                    print(f"  - '{item['title']}' -> FIXED ({format_clock(fs)})")
                else:
                    flexible.append(item)
                    print(f"  - '{item['title']}' -> FLEX (Missing fixed_start)")
            elif mode == TimingMode.RELATIVE:
                relative.append(item)
                print(f"  - '{item['title']}' -> RELATIVE")
            else:
                flexible.append(item)
                print(f"  - '{item['title']}' -> FLEX (Mode: {mode})")

        print("\n" + "="*60)
        print(" [JPLAN] STARTING MODULE C - FEASIBILITY-FIRST CONSTRUCTION")
        print("="*60)
        
        timeline: List[Dict[str, Any]] = []

        # 1. Place FIXED items first
        fixed.sort(key=lambda x: x["scheduled_start"])
        for item in fixed:
            feasible, reason = self._validate_locked_item(item, timeline, day_start, day_end, min_travel)
            if feasible:
                print(f"[JPLAN][MODULE_C] [LOCKING] '{item['title']}' -> {format_clock(item['scheduled_start'])}")
                item["trace"].append("Locked as a fixed commitment.")
                timeline.append(item)
                timeline.sort(key=lambda x: x["scheduled_start"])
            else:
                print(f"[JPLAN][MODULE_C] [CLASH] '{item['title']}' !!! reason: {reason}")
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
                print(f"[JPLAN][MODULE_C] [PLACING_RELATIVE] '{item['title']}' before '{target_title}' (Search: {format_clock(search_start)} -> {format_clock(search_end)})")
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

                print(f"[JPLAN][MODULE_C] [PLACING_RELATIVE] '{item['title']}' after '{target_title}' (Search: {format_clock(search_start)} -> {format_clock(search_end)})")
                inserted, reason = self._insert_best_position(
                    item, timeline, search_start, search_end, min_travel,
                    prefer_earliest=True
                )
            
            if inserted:
                timeline = inserted
                item["trace"].append(f"Placed near anchor '{target_title}' to respect sequence.")
            else:
                print(f"[JPLAN][MODULE_C] [REJECT_REL] '{item['title']}' - No feasible slot in narrative window.")
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
            print(f"[JPLAN][MODULE_C] [PLACING_FLEX] '{item['title']}' (Score: {self._calculate_activity_base_score(item)})")
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

    def _refine_timeline(
        self,
        timeline: List[Dict[str, Any]],
        day_start: int,
        day_end: int,
        min_travel: Optional[int],
    ) -> List[Dict[str, Any]]:
        print("\n" + "~"*60)
        print(" [JPLAN] STARTING MODULE D - ANSA REFINEMENT")
        print("~"*60)
        best = deepcopy(sorted(timeline, key=lambda item: item["scheduled_start"]))
        best_score = self._objective_score(best, day_start, day_end, min_travel)
        print(f"[MODULE_D] Initial Obj Score: {best_score:.2f}")

        for _ in range(2):
            changed = False
            for index, item in enumerate(list(best)):
                if item.get("fixed_start") is not None:
                    continue

                reduced = deepcopy(best[:index] + best[index + 1 :])
                candidate_timeline, _ = self._insert_best_position(
                    {k: v for k, v in item.items() if k not in {"scheduled_start", "scheduled_end"}},
                    reduced,
                    day_start,
                    day_end,
                    min_travel,
                )
                if candidate_timeline is None:
                    continue
                candidate_score = self._objective_score(candidate_timeline, day_start, day_end, min_travel)
                if candidate_score + 1e-6 < best_score:
                    relocated = next(entry for entry in candidate_timeline if entry["id"] == item["id"])
                    print(f"[MODULE_D] [IMPROVED] {best_score:.2f} -> {candidate_score:.2f} | Action: Relocate '{item['title']}'")
                    relocated["trace"].append("Relocated during local refinement to reduce idle time or transition cost.")
                    best = candidate_timeline
                    best_score = candidate_score
                    changed = True
            if not changed:
                break

        print(f"[MODULE_D] Refinement Complete | Final Score: {best_score:.2f}")
        print("~"*60 + "\n")
        return best

    def _objective_score(
        self,
        timeline: List[Dict[str, Any]],
        day_start: int,
        day_end: int,
        min_travel: Optional[int],
    ) -> float:
        if not timeline:
            return 0.0

        ordered = sorted(timeline, key=lambda item: item["scheduled_start"])
        total_travel = 0
        total_idle = max(0, ordered[0]["scheduled_start"] - day_start)
        tight_penalty = 0

        for previous, current in zip(ordered, ordered[1:]):
            transition = self._transition_minutes(previous, current, min_travel)
            idle = max(0, current["scheduled_start"] - previous["scheduled_end"] - transition)
            total_travel += transition
            total_idle += idle
            if idle < 10:
                tight_penalty += 3

        total_idle += max(0, day_end - ordered[-1]["scheduled_end"])
        utility = sum(activity_score(item) for item in ordered)
        return total_travel * 1.8 + total_idle * 0.3 + tight_penalty * 5 - utility * 0.4

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

    def _normalize_requested_activity(self, op: Dict[str, Any]) -> Dict[str, Any]:
        title = op.get("title") or op.get("target_title") or "Untitled"
        raw_dur = op.get("duration_minutes") or op.get("duration")
        trace = []

        # [PART C] Default duration policy
        duration = parse_duration_minutes(raw_dur, -1)
        if duration == -1:
            ct = clean_title(title)
            if "lunch" in ct or "dinner" in ct or "gym" in ct or "workout" in ct:
                duration = 60
                trace.append(f"Inferred default 60m duration for '{title}'.")
            else:
                duration = DEFAULT_DURATION
        
        # [PART A] Rich Timing Support
        timing_mode = op.get("timing_mode") or TimingMode.UNSPECIFIED
        fixed_start_str = op.get("fixed_start")
        fixed_start_min = parse_clock(fixed_start_str) if isinstance(fixed_start_str, str) else fixed_start_str
        
        if fixed_start_min is not None:
            timing_mode = TimingMode.FIXED

        # [PART D] Location Normalization & Inference
        loc = op.get("location")
        if loc is not None:
            loc = str(loc).strip()
            if not loc: loc = None
        
        # Inference fallback:
        if loc is None:
            ct = clean_title(title)
            if "lunch" in ct or "home" in ct: loc = "home"
            elif "gym" in ct or "workout" in ct: loc = "gym"
            elif "campus" in ct or "school" in ct: loc = "school"
            if loc: trace.append(f"Inferred location '{loc}' from title.")

        return {
            "id": self._make_id(title),
            "title": title,
            "type": "activity",
            "timing_mode": timing_mode,
            "anchor_relation": op.get("anchor_relation"),
            "sequence_index": op.get("sequence_index"),
            "fixed_start": fixed_start_min,
            "duration_minutes": duration,
            "priority": op.get("priority", "medium").lower(),
            "location": loc,
            "notes": op.get("notes", ""),
            "is_mandatory": True,
            "is_conflict": False,
            "trace": trace + [f"Mode={timing_mode}, Seq={op.get('sequence_index')}"]
        }

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

        location = raw.get("location")
        if location is not None:
            location = str(location).strip() or None
        location_normalized = raw.get("location_normalized") or _normalize_location(location)

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
            "location_normalized": location_normalized,
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
        }

    def _load_canonical_activities(self, envelope: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        canonical: List[Dict[str, Any]] = []
        excluded = 0
        for raw in list((envelope or {}).get("activities") or (envelope or {}).get("items") or []):
            if not self._is_activity_entry(raw):
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

    def _planning_mode(self, allow_clash: bool) -> str:
        return PLANNING_MODE_CLASH if allow_clash else PLANNING_MODE_FEASIBILITY

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
            self._debug(f"[STATE] Applying bulk date shift from {source_date} to {target_label}")
            self._debug(f"[STATE] Shifted {len(shifted_activities)} active activities to target date")
        return merged

    def _normalize_operation(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        op = clean_title(raw.get("op") or "add") or "add"
        normalized = deepcopy(raw)
        normalized["op"] = op
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
        return {
            "status": "conflict",
            "applied": False,
            "envelope": envelope,
            "version": current_version,
            "activities": envelope.get("activities", []),
            "updatedActivities": [],
            "deletedItemIds": [],
            "conflict": conflict_payload,
        }

    def apply_operations(
        self, 
        envelope: Dict[str, Any], 
        operations: List[Dict[str, Any]], 
        base_version: int,
        new_date: Optional[str] = None,
        target_date_envelope: Optional[Dict[str, Any]] = None,
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
        schedule_date = new_date or working_envelope.get("date") or str(date.today())
        source_turn = current_version + 1

        updated_activities: List[Dict[str, Any]] = []
        deleted_activity_ids: List[str] = []
        mutated_ids: set[str] = set()
        source_plan_date = working_envelope.get("date")
        existing_conflict_identities = self._existing_conflict_identities(working_envelope)
        postconditions: List[Dict[str, Any]] = []
        operations_to_apply = self._prepare_operations_for_apply(
            operations,
            [item for item in canonical_activities if item.get("status") == "active"],
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
            active_set = self._merge_target_date_context(active_set, target_date_envelope, source_plan_date, schedule_date)

        print("\n" + "!" * 60)
        print(f" [JPLAN] STARTING MODULE 9 - REPLANNING ({schedule_date})")
        print("!" * 60)
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

        updated_envelope = deepcopy(working_envelope)
        updated_envelope["schema_version"] = 4
        updated_envelope["date"] = schedule_date
        updated_envelope["status"] = "partial" if (conflicts or postcondition_failures) else "ok"
        updated_envelope["planning_mode"] = planning_mode
        updated_envelope["allow_clash"] = allow_clash
        updated_envelope["preferences"] = preferences
        updated_envelope["activities"] = [self._format_activity(item) for item in planned_result["activities"]]
        updated_envelope["schedule_blocks"] = planned_result["schedule_blocks"]
        updated_envelope["unscheduled_activities"] = [
            self._format_activity(item) for item in planned_result.get("unscheduled_activities", [])
        ]
        updated_envelope["version"] = current_version + 1
        updated_envelope["explanations"] = planned_result.get("explanations", [])
        updated_envelope["conflicts"] = conflicts
        updated_envelope["unmet_items"] = []
        updated_envelope["validation_issues"] = [failure.get("reason") for failure in postcondition_failures if failure.get("reason")]
        updated_envelope["conflict"] = conflicts[0] if conflicts else ({"type": "postcondition_failed", **postcondition_failures[0]} if postcondition_failures else None)
        updated_envelope["postcondition_results"] = postcondition_failures

        print(f"\n[JPLAN][ENGINE_LOGIC] --- FINAL SCHEDULE SUMMARY ---")
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
            print(line)
            summary_lines.append(line)
        print(f"[JPLAN][ENGINE_LOGIC] ---------------------------")
        updated_envelope["final_schedule_summary"] = "\n".join(summary_lines)

        return {
            "status": "success",
            "applied": True,
            "envelope": updated_envelope,
            "planned_result": planned_result,
            "version": updated_envelope["version"],
            "activities": updated_envelope["activities"],
            "updatedActivities": [self._format_activity(activity) for activity in updated_activities if activity.get("status") == "active"],
            "deletedItemIds": deleted_activity_ids,
        }
        print(f"[JPLAN][ENGINE_LOGIC] Incoming Ops: {json.dumps(operations, indent=2, ensure_ascii=False)}")
        
        # ... (rest of return logic)
        
        print(f"[JPLAN][ENGINE_LOGIC] Final Schedule Summary:")
        for a in activities:
            print(f"  - [{a.get('scheduled_start')} - {a.get('scheduled_end')}] {a.get('title')} (ID: {a.get('id')})")
        print(f"[JPLAN][ENGINE_LOGIC] ---------------------------\n")

        return {
            "envelope": envelope,
            "updatedActivities": formatted_updated,
            "deletedItemIds": [tid for tid in deleted_activity_ids if tid],
            "version": envelope["version"],
            "advisor_suggestion": advisor_suggestion
        }

    def _materialize_schedule(self, date_str: str, activities: List[Dict[str, Any]], unscheduled: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Full envelope builder that also injects buffers."""
        injected = self._inject_buffer_slots(activities)
        return {
            "date": date_str,
            "version": 1,
            "activities": [self._format_activity(a) for a in injected],
            "unscheduled_activities": unscheduled,
            "explanations": []
        }

    def _inject_buffer_slots(self, activities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Sort activities and fill gaps with buffers (> 10 mins)."""
        if not activities: return []
        
        # Sort by start time
        sorted_acts = sorted(activities, key=lambda x: x.get("scheduled_start") or 0)
        final_list = []
        
        for i, current in enumerate(sorted_acts):
            final_list.append(current)
            
            if i < len(sorted_acts) - 1:
                next_act = sorted_acts[i+1]
                current_end = current.get("scheduled_end") or 0
                next_start = next_act.get("scheduled_start") or 0
                
                gap = next_start - current_end
                
                # Only insert buffer if gap is significant (> 10 mins)
                if gap >= 10:
                    # [FIX] Check if this gap is already covered by ANY other activity (clash)
                    # This prevents ghost buffers between 1pm-3pm when a 12pm-3pm activity exists
                    is_covered = False
                    for other in activities:
                        if other.get("type") == "buffer": continue
                        o_start = other.get("scheduled_start") or 0
                        o_end = other.get("scheduled_end") or 0
                        # If an activity covers at least 50% of the gap, skip the buffer
                        if o_start <= current_end and o_end >= next_start:
                            is_covered = True
                            break
                    
                    if not is_covered:
                        buffer_id = f"buffer-{current.get('id')}-{next_act.get('id')}"
                        final_list.append({
                            "id": buffer_id,
                            "type": "buffer",
                            "title": "Buffer",
                            "scheduled_start": current_end,
                            "scheduled_end": next_start,
                            "duration_minutes": gap,
                            "is_mandatory": False,
                            "priority": "low"
                        })
        
        return final_list

    def _get_conflict_advice(self, conflicted_activities: List[Dict[str, Any]]) -> str:
        """[Module 8] Second-pass LLM call for human-like advice."""
        print("\n" + "#"*60)
        print(" [JPLAN] STARTING MODULE 8 - EXPLAINABILITY (ADVISOR)")
        print("#"*60)
        conflict_details = []
        for act in conflicted_activities:
            conflict_details.append(f"- {act['title']} ({format_clock(act['scheduled_start'])} - {format_clock(act['scheduled_end'])}): {act.get('conflict_reason')}")
        
        prompt = f"""
        You are the Schedule Advisor for JPlan. 
        The user's latest update caused the following schedule conflicts:
        {chr(10).join(conflict_details)}

        Please provide a VERY SHORT, human-like suggestion (max 2 sentences) in the user's language. 
        Suggest how they might fix the overlap (e.g., move one activity or shorten another).
        Be empathetic but professional.
        """
        
        print(f"\n[JPLAN][LLM_ADVISOR] --- FULL PROMPT SENT TO LLM ---\n{prompt}\n------------------------------------------\n")
        try:
            response = self.client.models.generate_content(
                model="gemini-3.1-flash-lite-preview",
                contents=prompt
            )
            adv_text = response.text.strip()
            print(f"\n[JPLAN][LLM_ADVISOR] --- RAW RESPONSE FROM LLM ---\n{adv_text}\n------------------------------------------\n")
            
            # Print Token Usage
            usage = getattr(response, "usage_metadata", None)
            if usage:
                print(f"[JPLAN][LLM_ADVISOR] Token Usage: Prompt={usage.prompt_token_count}, Candidates={usage.candidates_token_count}, Total={usage.total_token_count}")
                
            return adv_text
        except Exception as e:
            print(f"[JPLAN][LLM_ADVISOR] Advisor call failed: {e}")
            return "Note: This change creates some overlaps in your schedule."

    def _compact_result_summary(
        self,
        result: Dict[str, Any],
        allow_clash: bool,
    ) -> Dict[str, Any]:
        envelope = result.get("envelope") or result.get("schedule_data") or {}
        conflict = result.get("conflict") or envelope.get("conflict")
        conflicts = envelope.get("conflicts") or result.get("conflicts") or []
        postcondition_results = result.get("postcondition_results") or envelope.get("postcondition_results") or []
        changed = result.get("updatedActivities") or []
        if not changed and envelope.get("activities"):
            changed = envelope.get("activities", [])[:8]

        return {
            "status": result.get("status") or envelope.get("status") or ("partial" if conflicts else "success"),
            "applied": bool(result.get("applied", result.get("status") != "conflict")),
            "allow_clash": allow_clash,
            "conflict": conflict or (conflicts[0] if conflicts else None),
            "conflicts": conflicts[:3],
            "postcondition_results": postcondition_results[:3],
            "changed": [
                {
                    "title": item.get("title"),
                    "start": item.get("startTime") or item.get("start"),
                    "end": item.get("endTime") or item.get("end"),
                    "is_conflict": bool(item.get("is_conflict") or item.get("isConflict") or item.get("is_conflicting")),
                }
                for item in changed[:8]
                if isinstance(item, dict)
            ],
        }

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
        if applied and existing_conflicts and not allow_clash:
            existing = existing_conflicts[0]
            names = ", ".join(str(item) for item in (existing.get("activities") or [])[:2])
            existing_text = f" Your existing {names} clash is still marked." if names else " An existing clash is still marked."
            return {
                "reply": f"I updated the new request.{existing_text}",
                "reply_status": "partial",
                "recommend_allow_clash": False,
                "reply_reason": existing.get("explanation") or existing.get("conflict_reason"),
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

        changed_titles = [item.get("title") for item in summary.get("changed", []) if item.get("title")]
        if changed_titles:
            return {
                "reply": f"I updated your schedule for: {', '.join(changed_titles[:3])}.",
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

    def compose_result_reply(
        self,
        latest_request: str,
        parsed: Dict[str, Any],
        result: Dict[str, Any],
        allow_clash: bool,
    ) -> Dict[str, Any]:
        """Second-pass result-aware reply. The LLM phrases; deterministic checks decide truth."""
        summary = self._compact_result_summary(result, allow_clash)
        fallback = self._fallback_result_reply(latest_request, summary)

        if not getattr(self.client, "models", None):
            return fallback

        prompt = f"""
You are JPlan's final response writer. Use ONLY this scheduling result.
Write naturally, like a helpful planning assistant, not a formal system notice.
Keep it short: 1-3 sentences.
If applied=false, clearly say the requested change was not applied as intended, but do not use all-caps.
If RESULT_SUMMARY includes timing details, mention the concrete reason, such as the available window and required duration.
If status is ok/success and applied=true, say the schedule was applied.
If allow_clash=false and status=conflict, gently mention Allow Clash only as an option to force the overlap.
Do not claim success unless applied=true.
Do not invent reasons, times, blockers, or suggestions outside RESULT_SUMMARY.
Do not mention unslotted or failed tasks unless RESULT_SUMMARY says there is a conflict or unmet item.

USER_REQUEST:
{latest_request}

PARSED_OPERATIONS:
{json.dumps(parsed.get("operations") or parsed.get("activities") or [], ensure_ascii=True)[:1200]}

RESULT_SUMMARY:
{json.dumps(summary, ensure_ascii=True)[:1800]}
"""
        try:
            print("\n" + "#" * 60)
            print(" [JPLAN] STARTING MODULE 8 - RESULT-AWARE REPLY")
            print("#" * 60)
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
                print(
                    f"[JPLAN][LLM_REPLY] Token Usage: Prompt={token_usage['prompt']} | "
                    f"Candidates={token_usage['candidates']} | Total={token_usage['total']}"
                )
            reply = (response.text or "").strip()
            if not reply:
                return fallback

            if fallback["reply_status"] == "conflict":
                if not self._reply_claims_failure(reply):
                    return {**fallback, "token_usage": token_usage}
            elif fallback["reply_status"] == "success" and self._reply_claims_failure(reply):
                return {**fallback, "token_usage": token_usage}
            elif fallback["reply_status"] == "partial" and summary.get("conflicts"):
                clean_reply = clean_title(reply)
                if "clash" not in clean_reply and "conflict" not in clean_reply and "overlap" not in clean_reply:
                    return {**fallback, "token_usage": token_usage}

            return {
                **fallback,
                "reply": reply,
                "token_usage": token_usage,
            }
        except Exception as exc:
            print(f"[JPLAN][LLM_REPLY] Result-aware reply failed: {exc}")
            return fallback

    def _mark_activity_conflicts(self, activities: List[Dict[str, Any]]):
        """Internal helper to mark conflicts among activities in place."""
        for item in activities:
            item["is_conflict"] = False
            item["conflict_with"] = []
            item["conflict_reason"] = None

        valid_ids = {item["id"] for item in activities}
        for item in activities:
            item["is_conflict"] = False
            item["conflict_with"] = []
            item["conflict_reason"] = None

        for i, item in enumerate(activities):
            if item.get("type") not in [None, "activity"]: continue
            
            start = item.get("scheduled_start")
            end = item.get("scheduled_end")
            if start is None or end is None: continue
            
            clashes = []
            for j, other in enumerate(activities):
                if i == j: continue
                if other.get("type") not in [None, "activity"]: continue
                
                o_start = other.get("scheduled_start")
                o_end = other.get("scheduled_end")
                if o_start is None or o_end is None: continue
                
                if start < o_end and end > o_start:
                    clashes.append(other.get("id"))
            
            # Critical Fix: Only keep IDs that are actually in the current list
            item["conflict_with"] = [cid for cid in clashes if cid in valid_ids]
            if item["conflict_with"]:
                item["is_conflict"] = True
                item["conflict_reason"] = f"Overlaps with {len(item['conflict_with'])} events"

    def get_local_window_activities(self, activities: List[Dict[str, Any]], target_time: Optional[int] = None, window_hours: int = 3) -> List[Dict[str, Any]]:
        """Prune activities to a local time window for context-aware LLM requests."""
        if not target_time:
            # If no target time, return a reasonable midday window or first few activities
            return activities[:10] 
            
        window_mins = window_hours * 60
        start_bound = target_time - window_mins
        end_bound = target_time + window_mins
        
        filtered = []
        for item in activities:
            s = item.get("scheduled_start")
            e = item.get("scheduled_end")
            if s is None or e is None: continue
            if s < end_bound and e > start_bound:
                filtered.append(item)
        return filtered

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
            "location": item.get("location"),
            "location_normalized": item.get("location_normalized") or _normalize_location(item.get("location")),
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
