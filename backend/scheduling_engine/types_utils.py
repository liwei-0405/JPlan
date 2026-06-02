import os
import re
from copy import deepcopy
from typing import Any, Dict, Optional, Tuple

from jplan_logging import jlog

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
MAX_HISTORY_TURNS = 2
MAX_HISTORY_MESSAGE_CHARS = 120

PRIORITY_WEIGHT = {
    "low": 1,
    "medium": 2,
    "high": 3,
}

PLANNING_MODE_FEASIBILITY = "feasibility_first"
PLANNING_MODE_CLASH = "clash_allowed"
WARNING_TIGHT_TRANSITION = "TIGHT_TRANSITION"
PARSER_RETRY_DELAYS_SECONDS = (0.4, 0.8)
DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite"
JPLAN_GEMINI_MODEL = (
    os.getenv("JPLAN_GEMINI_MODEL")
    or os.getenv("JPLAN_LLM_MODEL")
    or DEFAULT_GEMINI_MODEL
).strip() or DEFAULT_GEMINI_MODEL
MODULE_A_LLM_MODEL = os.getenv("MODULE_A_LLM_MODEL", JPLAN_GEMINI_MODEL).strip() or JPLAN_GEMINI_MODEL
MODULE8_LLM_MODEL = os.getenv("MODULE8_LLM_MODEL", JPLAN_GEMINI_MODEL).strip() or JPLAN_GEMINI_MODEL
ADVISORY_LLM_MODEL = os.getenv("ADVISORY_LLM_MODEL", JPLAN_GEMINI_MODEL).strip() or JPLAN_GEMINI_MODEL
MODULE_A_MAX_OUTPUT_TOKENS = int(os.getenv("MODULE_A_MAX_OUTPUT_TOKENS", "1200"))
MODULE_A_LLM_TIMEOUT_SECONDS = float(os.getenv("MODULE_A_LLM_TIMEOUT_SECONDS", "20"))
MODULE_A_LLM_TOTAL_TIMEOUT_SECONDS = float(os.getenv("MODULE_A_LLM_TOTAL_TIMEOUT_SECONDS", "25"))
MODULE_A_LLM_ENABLE_RETRY = os.getenv("MODULE_A_LLM_ENABLE_RETRY", "true").strip().lower() not in {"0", "false", "no", "off"}
MODULE_A_LLM_RETRY_COUNT = max(0, int(os.getenv("MODULE_A_LLM_RETRY_COUNT", "1")))
MODULE_A_LLM_EXECUTOR_WORKERS = max(1, int(os.getenv("MODULE_A_LLM_EXECUTOR_WORKERS", "2")))
MODULE_A_LLM_FALLBACK_MODEL = os.getenv("MODULE_A_LLM_FALLBACK_MODEL", "").strip()
MODULE_A_LLM_FALLBACK_TIMEOUT_SECONDS = float(os.getenv("MODULE_A_LLM_FALLBACK_TIMEOUT_SECONDS", "8"))
MODULE_A_PARSER_BUSY_REPLY = "The AI parser is busy right now. Please try again in a moment, or split the request into smaller parts."
MODULE8_LLM_TIMEOUT_SECONDS = float(os.getenv("MODULE8_LLM_TIMEOUT_SECONDS", "8"))
MODULE8_LLM_TOTAL_TIMEOUT_SECONDS = float(os.getenv("MODULE8_LLM_TOTAL_TIMEOUT_SECONDS", "12"))
ADVISORY_LLM_TIMEOUT_SECONDS = float(os.getenv("ADVISORY_LLM_TIMEOUT_SECONDS", "8"))
ADVISORY_LLM_EXECUTOR_WORKERS = max(1, int(os.getenv("ADVISORY_LLM_EXECUTOR_WORKERS", "2")))
JPLAN_ENABLE_MODULE_D = os.getenv("JPLAN_ENABLE_MODULE_D", "true").strip().lower() not in {"0", "false", "no", "off"}
MODULE_D_MAX_ITERATIONS = max(0, int(os.getenv("MODULE_D_MAX_ITERATIONS", "30")))
MODULE_D_TIME_BUDGET_MS = max(1, int(os.getenv("MODULE_D_TIME_BUDGET_MS", "500")))
MODULE_D_MIN_IMPROVEMENT = float(os.getenv("MODULE_D_MIN_IMPROVEMENT", "0.01"))
MODULE_D_TRACE = "Adjusted by Module D refinement to reduce idle/travel cost."

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

NO_LOCATION_REQUIRED_TITLE_KEYWORDS = {
    "admin",
    "assignment",
    "call",
    "calling",
    "coding",
    "document",
    "documents",
    "fyp",
    "implementation",
    "laptop",
    "online meeting",
    "paperwork",
    "parents",
    "phone",
    "phone call",
    "plan tomorrow",
    "planning",
    "proposal",
    "review",
    "writing",
    "work",
}

PHYSICAL_PLACE_TITLE_KEYWORDS = {
    "bank",
    "cafe",
    "campus",
    "class",
    "coffee",
    "dinner",
    "gym",
    "groceries",
    "grocery",
    "library",
    "lunch",
    "meeting",
    "office",
    "restaurant",
    "seminar",
    "shopping",
    "store",
    "supermarket",
    "workout",
}

PREFERRED_TIME_WINDOWS = {
    "morning": (8 * 60, 12 * 60),
    "afternoon": (12 * 60, 17 * 60),
    "lunch": (12 * 60, 14 * 60),
    "coffee_break": (10 * 60, 16 * 60),
    "business_hours": (9 * 60, 16 * 60),
    "after_lunch": (12 * 60, 17 * 60),
    "evening": (18 * 60, 21 * 60),
    "night": (20 * 60, DEFAULT_DAY_END),
    "not_too_late": (DEFAULT_DAY_START, 20 * 60),
}

PREFERENCE_WEIGHT_MULTIPLIERS = {
    "hard": 100000.0,
    "high": 18.0,
    "medium": 5.0,
    "low": 1.5,
}

PREFERENCE_METADATA_FIELDS = (
    "preferred_time_window",
    "preferred_window_start",
    "preferred_window_end",
    "earliest_preferred_start",
    "latest_preferred_end",
    "ideal_start",
    "ideal_end",
    "ideal_start_range",
    "acceptable_start",
    "acceptable_end",
    "acceptable_start_range",
    "preference_weight",
    "preference_priority",
    "preference_type",
    "is_hard_window",
    "is_soft_window",
    "activity_role",
    "semantic_constraint_type",
)


def copy_preference_metadata(source: Dict[str, Any], target: Dict[str, Any], *, overwrite: bool = False) -> Dict[str, Any]:
    for field in PREFERENCE_METADATA_FIELDS:
        if field not in source:
            continue
        if source.get(field) in {None, ""}:
            continue
        if overwrite or target.get(field) in {None, ""}:
            target[field] = deepcopy(source.get(field)) if isinstance(source.get(field), (dict, list)) else source.get(field)
    return target


def preference_window_info(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    label = clean_title(item.get("preferred_time_window") or item.get("preference_type") or "")
    source = ""
    ideal_start = _coerce_pref_minute(
        item.get("earliest_preferred_start")
        or item.get("ideal_start")
        or item.get("preferred_window_start")
    )
    ideal_end = _coerce_pref_minute(
        item.get("latest_preferred_end")
        or item.get("ideal_end")
        or item.get("preferred_window_end")
    )
    if label:
        source = "preferred_time_window"
        if (ideal_start is None or ideal_end is None) and label in PREFERRED_TIME_WINDOWS:
            default_start, default_end = PREFERRED_TIME_WINDOWS[label]
            ideal_start = ideal_start if ideal_start is not None else default_start
            ideal_end = ideal_end if ideal_end is not None else default_end

    if (ideal_start is None or ideal_end is None) and isinstance(item.get("ideal_start_range"), (list, tuple)):
        values = item.get("ideal_start_range") or []
        if len(values) >= 2:
            ideal_start = _coerce_pref_minute(values[0])
            ideal_end = _coerce_pref_minute(values[1])
            source = source or "ideal_start_range"

    acceptable_start = _coerce_pref_minute(item.get("acceptable_start"))
    acceptable_end = _coerce_pref_minute(item.get("acceptable_end"))
    if isinstance(item.get("acceptable_start_range"), (list, tuple)):
        values = item.get("acceptable_start_range") or []
        if len(values) >= 2:
            acceptable_start = _coerce_pref_minute(values[0])
            acceptable_end = _coerce_pref_minute(values[1])
            source = source or "acceptable_start_range"
    acceptable_start = acceptable_start if acceptable_start is not None else ideal_start
    acceptable_end = acceptable_end if acceptable_end is not None else ideal_end

    if ideal_start is None or ideal_end is None:
        semantic = clean_title(item.get("semantic_constraint_type") or item.get("activity_role") or "")
        if semantic in PREFERRED_TIME_WINDOWS:
            ideal_start, ideal_end = PREFERRED_TIME_WINDOWS[semantic]
            acceptable_start, acceptable_end = ideal_start, ideal_end
            label = label or semantic
            source = "semantic"
    if ideal_start is None or ideal_end is None:
        # Legacy fallback only. Structured metadata above owns the normal path.
        title = clean_title(item.get("title") or "")
        for fallback_label in ("lunch", "coffee_break", "evening", "business_hours"):
            if fallback_label.replace("_", " ") in title:
                ideal_start, ideal_end = PREFERRED_TIME_WINDOWS[fallback_label]
                acceptable_start, acceptable_end = ideal_start, ideal_end
                label = label or fallback_label
                source = "fallback_title"
                break
    if ideal_start is None or ideal_end is None:
        return None

    raw_weight = clean_title(item.get("preference_weight") or item.get("preference_priority") or "")
    if item.get("is_hard_window") or label == "business_hours":
        weight = "hard"
    elif raw_weight in {"hard", "high", "medium", "low"}:
        weight = raw_weight
    elif item.get("is_soft_window"):
        weight = "medium"
    elif label in {"lunch", "evening"}:
        weight = "high"
    elif label == "coffee_break":
        weight = "low"
    else:
        weight = "medium"

    return {
        "label": label or "preferred_window",
        "source": source or "metadata",
        "ideal_start": int(ideal_start),
        "ideal_end": int(ideal_end),
        "acceptable_start": int(acceptable_start if acceptable_start is not None else ideal_start),
        "acceptable_end": int(acceptable_end if acceptable_end is not None else ideal_end),
        "weight": weight,
        "hard": weight == "hard" or bool(item.get("is_hard_window")),
    }


def preference_window_deviation(item: Dict[str, Any], start: Optional[int], end: Optional[int]) -> Dict[str, Any]:
    info = preference_window_info(item)
    if not info or start is None or end is None:
        return {"info": info, "deviation": 0, "penalty": 0.0, "hard_violation": False}
    acceptable_start = int(info["acceptable_start"])
    acceptable_end = int(info["acceptable_end"])
    deviation = 0
    if int(start) < acceptable_start:
        deviation += acceptable_start - int(start)
    if int(end) > acceptable_end:
        deviation += int(end) - acceptable_end
    severity = 1.0
    if deviation >= 60:
        severity = 2.0
    elif deviation >= 30:
        severity = 1.5
    multiplier = PREFERENCE_WEIGHT_MULTIPLIERS.get(str(info["weight"]), 5.0)
    penalty = float(deviation) * multiplier * severity
    return {
        "info": info,
        "deviation": deviation,
        "penalty": penalty,
        "hard_violation": bool(info["hard"] and deviation > 0),
    }


def should_run_preference_rescue(item: Dict[str, Any], start: Optional[int], end: Optional[int]) -> bool:
    result = preference_window_deviation(item, start, end)
    info = result.get("info") or {}
    return bool(result.get("deviation", 0) > 0 and info.get("weight") in {"hard", "high"})


def _coerce_pref_minute(value: Any) -> Optional[int]:
    if value in {None, ""}:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return parse_clock(str(value))

ACCURATE_TRAVEL_REQUEST_PATTERN = re.compile(
    r"\b(?:accurate|actual|real|route(?:-|\s*)aware)\s+(?:travel|route|commute)\s+time\b|"
    r"\b(?:travel|route|commute)\s+time\s+(?:to\s+be\s+)?(?:accurate|actual|real)\b|"
    r"\b(?:make|set)\s+(?:my\s+|the\s+)?(?:travel|route|commute)\s+time\s+(?:accurate|actual|real)\b|"
    r"\b(?:add|include|consider|use|with|calculate)\s+(?:the\s+)?(?:accurate|actual|real)?\s*(?:travel|route|commute)\s+time\b|"
    r"\b(?:recalculate|recompute|validate|check|update)\s+(?:the\s+)?(?:travel|route|commute)\s+time\b|"
    r"\b(?:regenerate|rebuild|refresh)\b.*\b(?:accurate|actual|real)\s+(?:travel|route|commute)\s+time\b",
    re.IGNORECASE
)

TRAVEL_INTENT_PATTERN = re.compile(
    r"\bwith\s+(?:the\s+)?(?:accurate\s+|actual\s+|real\s+)?(?:travel|route|commute)\b|"
    r"\brealistic\s+with\s+(?:the\s+)?(?:accurate\s+|actual\s+|real\s+)?(?:travel|route|commute)\b|"
    r"\baccount\s+for\s+(?:the\s+)?(?:accurate\s+|actual\s+|real\s+)?(?:travel|route|commute)\b|"
    r"\binclude\s+(?:the\s+)?(?:accurate\s+|actual\s+|real\s+)?(?:travel|route|commute)\b|"
    r"\bconsider\s+(?:the\s+)?(?:accurate\s+|actual\s+|real\s+)?(?:travel|route|commute)\b|"
    r"\bmake\s+it\s+realistic\s+with\s+(?:the\s+)?(?:accurate\s+|actual\s+|real\s+)?(?:travel|route|commute)\b|"
    r"\bkeep\s+enough\s+(?:travel|route|commute)(?:\s+and\s+buffer)?\s+time\b|"
    r"\bleave\s+(?:enough\s+)?(?:travel|route|commute|buffer)(?:\s+and\s+(?:travel|route|commute|buffer))*\s+time\b|"
    r"\bmake\s+room\s+for\s+(?:travel|route|commute|buffer)\s+time\b|"
    r"\b(?:drop\s+off|dropoff|pick\s+up|pickup)\b.*\b(?:grocery|shopping|lunch|dinner|birthday|gift|errand)\b|"
    r"\b(?:practical|not\s+too\s+tiring|less\s+tiring|not\s+rushed)\b",
    re.IGNORECASE
)

def detect_travel_intent(text: str) -> bool:
    if not text:
        return False
    return bool(ACCURATE_TRAVEL_REQUEST_PATTERN.search(text) or TRAVEL_INTENT_PATTERN.search(text))

def format_clock_ampm(minutes: int) -> str:
    normalized = minutes % (24 * 60)
    hour = normalized // 60
    minute = normalized % 60
    suffix = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12:02d}:{minute:02d} {suffix}"


PARSER_PROMPT = """
You are Module A, the JSON parser for JPlan. Convert the latest user request into structured scheduling operations.
Return ONLY valid JSON.

Schema:
{
  "intent": "add|edit|move|remove|chat",
  "transcription": "...",
  "date": "YYYY-MM-DD or null",
  "operations": [{
    "op": "add|update|remove|replace|shift_plan_date",
    "activity_id": "existing stable activity id when updating an existing item",
    "title": "Activity title",
    "timing_mode": "fixed|relative|flexible|preferred",
    "fixed_start": "HH:MM",
    "duration_minutes": 60,
    "priority": "low|medium|high",
    "location": "label or null",
    "raw_location_text": "exact user location text or null",
    "location_kind": "exact_named_place|area_only|category_only|home|online|no_location_required|unknown_physical",
    "explicit_user_location": true,
    "location_category": "medical|meal_place|supermarket|pharmacy|fitness_center|home|work|study|no_location|unknown",
    "travel_required": true,
    "location_resolution_status": "needs_coordinates|resolved_coordinates|not_required|ambiguous",
    "no_location_reason": "document_preparation|admin_task|focused_work|online|none",
    "semantic_confidence": 0.0,
    "location_confidence": 0.0,
    "needs_clarification": false,
    "parse_notes": "short parser note if useful",
    "anchor_relation": {"kind":"after|before","target_title":"...","target_activity_id":"existing anchor id when known"},
    "edit_reason": "short rationale clause when user explains why",
    "preserve_existing_fields": true,
    "sequence_index": 1
  }],
  "preferences": {},
  "conflict_analysis": "short non-decisive note"
}

Critical rules:
1. You are only the parser. Never reject a requested change because of conflict.
2. Backend decides feasibility, Allow Clash, travel time, and final reply.
3. If user asks to move/add/update/remove, operations must not be empty.
4. If user says "move lunch to 12pm", output update Lunch fixed_start="12:00" even if it overlaps.
5. Do not add operations that move unrelated fixed events unless user explicitly asks.
6. Do not create operations for generic travel, transit, prep, buffer, free time, or idle blocks.
7. Use fixed only for exact times. Use relative for after/before/followed by/then.
8. Use 24-hour time. Use reasonable durations if missing: lunch/dinner 60, coffee 15, gym 60, shopping 45.
9. Use current activity titles when possible. Keep conflict_analysis short and non-decisive.
10. For every activity, output semantic location fields when possible. Use raw_location_text for the exact place/modality words the user wrote.
11. Do not invent exact places. If the user gives only a category ("grocery shopping", "pharmacy stop", "gym"), use location_kind="category_only", travel_required=true, and location_resolution_status="needs_coordinates".
12. Soft phrases like preferably, if possible, maybe, sometime after, not too late, later in the day, at night, or around are preferences, not hard anchor_relation.
13. Use anchor_relation only for hard wording like right after, immediately after, must be after, only after, before X starts, or cannot happen before.
14. For edits that refer to an existing activity in CURRENT_ACTIVITY_INDEX, output op="update" with activity_id when available; do not output op="add".
15. Preserve existing duration, priority, location label/source, coordinates, travel_required, and title unless the user explicitly changes them. You may set preserve_existing_fields=true for update edits.
16. If the user gives a rationale ("because", "cause", "so that", "to avoid"), put it in edit_reason. Never include rationale text in title or anchor_relation.target_title.
17. Explicit user places must be preserved exactly: "doctor appointment at Sunway Medical" => raw_location_text="Sunway Medical", location_kind="exact_named_place", location_category="medical", travel_required=true, location_resolution_status="needs_coordinates". Never change it to home.
18. Use location_kind="home" only when the user explicitly says home/at home/my house, or an existing selected saved home location is referenced.
19. Work/admin/document-prep tasks do not need coordinates unless the user gives a physical place. "prepare documents" => travel_required=false, location_kind="no_location_required", location_resolution_status="not_required", no_location_reason="document_preparation".
20. Focused/deep work does not need coordinates unless a physical place is explicitly given.
21. Pharmacy stops are physical stops: location_category="pharmacy", travel_required=true, location_resolution_status="needs_coordinates" unless exact confirmed coordinates are already known.
22. If unsure whether a physical location is needed, set needs_clarification=true and use location_resolution_status="ambiguous"; do not silently default to home.
"""


def parse_clock(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
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
