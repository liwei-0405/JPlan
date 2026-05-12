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
9. You are ONLY the parser. Never reject a requested schedule change because of conflict. Always output the requested operation; the backend scheduler decides feasibility and Allow Clash behavior.
10. For edit/add/move requests, operations must not be empty. If the user asks "move lunch to 12pm", output update Lunch fixed_start="12:00" even if it may overlap another event.
11. Do not add extra operations that mutate fixed anchor/blocker events unless the user explicitly asks to move that event by name.
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
