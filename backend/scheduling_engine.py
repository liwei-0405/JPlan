import json
import re
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
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

PARSER_PROMPT = """
You are the parsing layer for JPlan. Convert user requests into structured operations.
Return ONLY ONE JSON object with: intent, reply, transcription, date, operations, conflict_analysis.

OPERATIONS SCHEMA:
{ 
  "op": "add|remove|move|update|replace", 
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
        """[PART 7] Final API Response construction."""
        # This replaces the old logic
        date = parsed.get("date") or self._local_today_iso()
        all_activities = self._get_base_activities(current_schedule)
        
        # After any optimization or construction, we materialize the blocks
        blocks = self._materialize_blocks(all_activities, DEFAULT_DAY_START, 0)
        
        return {
            "date": date,
            "status": "ok", # Future: partial/infeasible
            "reply": parsed.get("reply"),
            "transcription": parsed.get("transcription"),
            "intent": parsed.get("intent"),
            "activities": all_activities,
            "schedule_blocks": blocks,
            "unmet_items": [],
            "validation_issues": [],
            "explanations": parsed.get("explanations", []),
            "version": (current_schedule.get("version", 1) if current_schedule else 1) + 1
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
            if usage:
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
                "title": act["title"],
                "start": format_clock(start_min),
                "end": format_clock(end_min),
                "startTime": format_clock(start_min),
                "endTime": format_clock(end_min),
                "duration_minutes": end_min - start_min,
                "location": current_loc,
                "notes": act.get("notes"),
                "is_conflict": act.get("is_conflict", False),
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
                fs = item.get("fixed_start") or item.get("fixedStart")
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
            if not anchor or anchor.get("kind") != "after":
                flexible.append(item)
                continue
            
            target_title = anchor.get("target_title")
            anchor_item = next((a for a in timeline if clean_title(a["title"]) == clean_title(target_title)), None)
            
            if not anchor_item:
                flexible.append(item)
                continue
            
            # [PART C] Implicit Windowing: find the next scheduled activity to bound search
            search_start = anchor_item["scheduled_end"]
            search_end = day_end
            
            # Find the nearest activity in the future to constrain the window
            successors = [a for a in timeline if a["scheduled_start"] >= search_start]
            if successors:
                search_end = min(a["scheduled_start"] for a in successors)
            
            print(f"[JPLAN][MODULE_C] [PLACING_RELATIVE] '{item['title']}' after '{target_title}' (Search: {format_clock(search_start)} -> {format_clock(search_end)})")
            
            inserted, reason = self._insert_best_position(
                item, timeline, search_start, search_end, min_travel,
                prefer_earliest=True # PART B: Penalty for delay
            )
            
            if inserted:
                timeline = inserted
                item["trace"].append(f"Placed near anchor '{target_title}' to respect sequence.")
            else:
                # If it doesn't fit in the window, it's a conflict
                print(f"[JPLAN][MODULE_C] [REJECT_REL] '{item['title']}' - No feasible slot in narrative window.")
                timeline.append(self._create_conflict_item(item, timeline, reason or "No space in narrative window."))
                timeline.sort(key=lambda x: x["scheduled_start"])

        # 3. Place FLEXIBLE items
        flexible.sort(key=self._calculate_activity_base_score, reverse=True)
        for item in flexible:
            print(f"[JPLAN][MODULE_C] [PLACING_FLEX] '{item['title']}' (Score: {self._calculate_activity_base_score(item)})")
            inserted, reason = self._insert_best_position(item, timeline, day_start, day_end, min_travel)
            if inserted:
                timeline = inserted
            else:
                if item.get("is_mandatory"):
                    timeline.append(self._create_conflict_item(item, timeline, reason))
                    timeline.sort(key=lambda x: x["scheduled_start"])
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
            # We need to know if there's a transition required
            transition_after = self._transition_minutes(existing, item, min_travel)
            transition_before = self._transition_minutes(item, existing, min_travel)
            
            # THE FIX: We use strict inequality for the actual content overlap, 
            # but we MUST respect the transition buffer.
            # If transition is 0, then 'end <= existing_start' is perfectly fine.
            if not (
                end + transition_before <= existing["scheduled_start"]
                or start >= existing["scheduled_end"] + transition_after
            ):
                return False, f"Clashes with '{existing['title']}' (requires {transition_after if start >= existing['scheduled_end'] else transition_before}m buffer)"
        
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
        prefer_earliest: bool = False
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

    def apply_operations(
        self, 
        envelope: Dict[str, Any], 
        operations: List[Dict[str, Any]], 
        base_version: int,
        new_date: Optional[str] = None
    ) -> Dict[str, Any]:
        """Apply patch operations to a schedule envelope."""
        current_version = envelope.get("version", 1)
        if base_version != current_version:
            # We raise a specialized exception that main.py can catch
            raise VersionMismatchError(f"Version mismatch: baseVersion {base_version} != currentVersion {current_version}")

        activities_raw = deepcopy(envelope.get("activities") or envelope.get("items") or [])
        activities = []
        for raw in activities_raw:
            # CRITICAL: Filter out previous materializations (buffers/travel)
            if raw.get("type") not in [None, "activity"]:
                continue
            
            if "scheduled_start" not in raw and raw.get("startTime"):
                raw["scheduled_start"] = parse_clock(raw["startTime"])
            if "scheduled_end" not in raw and raw.get("endTime"):
                raw["scheduled_end"] = parse_clock(raw["endTime"])
            activities.append(raw)
        
        print(f"[JPLAN][ENGINE_LOGIC] Pre-filtered activities count: {len(activities)}")
            
        updated_activities = []
        deleted_activity_ids = []
        removed_slots = [] # Track times of removed items to reuse for 'add' in same batch

        for op_data in operations:
            op_type = op_data.get("op")
            target_id = op_data.get("target_id")
            target_title = str(op_data.get("target_title") or "").strip().lower()
            
            if op_type == "add":
                new_item = self._normalize_requested_activity(op_data)
                if new_item:
                    activities.append(new_item)
                    updated_activities.append(new_item)

            elif op_type in ["update", "move", "update_priority", "remove", "replace"]:
                target_found = False
                target_item = None
                target_idx = -1
                raw_target_title = str(target_title or "").strip().lower()

                # Matching Strategy: ID (if exists) -> Exact Title -> Fuzzy Title
                for i, item in enumerate(activities):
                    item_id = item.get("id")
                    item_title = str(item.get("title") or "").strip().lower()
                    
                    if target_id and item_id == target_id:
                        target_item, target_idx, target_found = item, i, True
                        break
                    if not target_found and raw_target_title and item_title == raw_target_title:
                        target_item, target_idx, target_found = item, i, True

                if not target_found and raw_target_title:
                    for i, item in enumerate(activities):
                        item_title = str(item.get("title") or "").strip().lower()
                        if raw_target_title in item_title or item_title in raw_target_title:
                            target_item, target_idx, target_found = item, i, True
                            break

                if target_found:
                    print(f"[JPLAN][ENGINE_LOGIC] Match found for {op_type}: '{target_item.get('title')}' (at {target_item.get('scheduled_start')})")
                    if op_type == "remove":
                        removed = activities.pop(target_idx)
                        deleted_activity_ids.append(removed["id"])
                    elif op_type == "replace":
                        activities.pop(target_idx)
                        new_item = self._normalize_requested_activity(op_data)
                        if new_item:
                            activities.append(new_item)
                            updated_activities.append(new_item)
                    else:
                        updated = self._merge_activity(target_item, op_data)
                        activities[target_idx] = updated
                        updated_activities.append(updated)
                else:
                    self._debug(f"Target not found for {op_type}: '{target_title}'")
                    # Fallback to 'add' for everything except remove
                    if op_type != "remove":
                        new_item = self._normalize_requested_activity(op_data)
                        if new_item:
                            activities.append(new_item)
                            updated_activities.append(new_item)

        # [REPLANNING STRATEGY - Module 9]
        preferences = envelope.get("preferences", {})
        schedule_date = new_date or envelope.get("date") or str(date.today())
        
        print("\n" + "!"*60)
        print(f" [JPLAN] STARTING MODULE 9 - REPLANNING ({schedule_date})")
        print("!"*60)
        
        # This is the authoritative planning step
        planned_result = self._plan_schedule(schedule_date, activities, preferences)
        
        # [PART G] Update envelope with rich scheduling data
        envelope["date"] = schedule_date
        envelope["status"] = "ok"
        envelope["activities"] = [self._format_activity(a) for a in planned_result["activities"]]
        envelope["schedule_blocks"] = planned_result["schedule_blocks"]
        envelope["unscheduled"] = [self._format_activity(a) for a in planned_result.get("unscheduled_activities", [])]
        envelope["version"] = current_version + 1
        envelope["explanations"] = planned_result.get("explanations", [])

        # [DEBUG] Final Schedule Summary
        print(f"\n[JPLAN][ENGINE_LOGIC] --- FINAL SCHEDULE SUMMARY ---")
        summary_lines = []
        for block in envelope.get("schedule_blocks", []):
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
        envelope["final_schedule_summary"] = "\n".join(summary_lines)
        
        return {
            "status": "success",
            "envelope": envelope,
            "planned_result": planned_result,
            "version": envelope["version"],
            "activities": envelope["activities"],
            "updatedActivities": [self._format_activity(a) for a in updated_activities],
            "deletedItemIds": deleted_activity_ids
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
        """Convert internal activity format to external API format."""
        # Priority 1: Use existing integer timestamps if present
        s_start = item.get("scheduled_start")
        s_end = item.get("scheduled_end")
        
        # Priority 2: Parse from startTime/endTime strings if integers are missing
        if s_start is None and item.get("startTime"):
            s_start = parse_clock(item["startTime"])
        if s_end is None and item.get("endTime"):
            s_end = parse_clock(item["endTime"])

        # Priority 3: Fallback to fixed_start/end
        if s_start is None:
            s_start = item.get("fixed_start")
        if s_end is None:
            s_end = item.get("fixed_end") or (s_start + (item.get("duration_minutes") or 60) if s_start is not None else None)

        # Final sanity check: if still None, default to 0
        s_start_val = s_start if s_start is not None else 0
        s_end_val = s_end if s_end is not None else s_start_val + 60

        conflict_with = item.get("conflict_with") or []
        if isinstance(conflict_with, str):
            conflict_with = [conflict_with]

        return {
            "id": item.get("id", "unknown"),
            "type": item.get("type", "activity"),
            "title": item.get("title", "Untitled"),
            "startTime": format_clock(s_start_val),
            "endTime": format_clock(s_end_val),
            "location": item.get("location"),
            "duration": item.get("duration") or self._duration_label(item.get("duration_minutes") or (s_end_val - s_start_val)),
            "priority": item.get("priority", "medium"),
            "isMandatory": bool(item.get("is_mandatory", True) if "is_mandatory" in item else item.get("isMandatory", True)),
            "notes": item.get("notes"),
            "explanation": item["trace"][-1] if item.get("trace") else item.get("explanation"),
            "trace": item.get("trace", []),
            "source": item.get("source", "planner"),
            "isConflict": bool(item.get("is_conflict") or item.get("isConflict", False)),
            "conflictWith": conflict_with,
            "conflictReason": item.get("conflict_reason") or item.get("conflictReason"),
            "conflictPriority": item.get("conflict_priority"),
            "conflictSeverity": item.get("conflict_severity"),
            # Preserve internal fields for next processing cycle
            "scheduled_start": s_start_val,
            "scheduled_end": s_end_val,
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

