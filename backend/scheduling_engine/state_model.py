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

class StateModelMixin:
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

    def _infer_repair_protection(
        self,
        raw: Dict[str, Any],
        title: str,
        timing_mode: Optional[str],
        fixed_start: Optional[int],
        is_user_fixed: bool,
        is_mandatory: bool,
    ) -> str:
        explicit = clean_title(raw.get("repair_protection") or raw.get("repairProtection") or "")
        if explicit in {"fixed", "protected_social", "flexible", "optional"}:
            return explicit
        if is_user_fixed or timing_mode == TimingMode.FIXED or fixed_start is not None:
            return "fixed"
        activity_type = clean_title(raw.get("activity_type") or "")
        if activity_type == "optional" or not is_mandatory:
            return "optional"
        title_clean = clean_title(title)
        social_markers = {
            "parent", "parents", "family", "friend", "friends", "girl", "girlfriend",
            "boyfriend", "client", "colleague", "team", "someone",
        }
        social_actions = {
            "dinner", "lunch", "brunch", "coffee", "catchup", "catch up",
            "meet", "meeting", "hangout", "hang out",
        }
        words = set(title_clean.split())
        has_social_person = bool(words & social_markers)
        has_social_action = any(action in title_clean for action in social_actions)
        if has_social_action and has_social_person:
            return "protected_social"
        if "client catch" in title_clean or "coffee catch" in title_clean:
            return "protected_social"
        return "flexible"

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
        raw_timing_mode = clean_title(raw.get("timing_mode") or raw.get("timingMode") or "")
        raw_has_fixed_fields = any(
            raw.get(key) is not None
            for key in ("fixed_start", "fixedStart", "fixed_end", "fixedEnd")
        )
        user_fixed_start = self._coerce_minutes(
            raw.get("user_fixed_start"),
            raw.get("userFixedStart"),
            raw.get("requested_fixed_start"),
        )
        raw_is_user_fixed = raw.get("is_user_fixed")
        is_user_fixed = (
            bool(raw_is_user_fixed)
            if raw_is_user_fixed is not None
            else bool(raw_has_fixed_fields or user_fixed_start is not None or raw_timing_mode == TimingMode.FIXED)
        )
        fixed_start = self._coerce_minutes(raw.get("fixed_start"), raw.get("fixedStart"), user_fixed_start)
        fixed_end = self._coerce_minutes(raw.get("fixed_end"), raw.get("fixedEnd"))
        if is_user_fixed and fixed_start is None:
            fixed_start = self._coerce_minutes(raw.get("scheduled_start"), raw.get("startTime"))
        if is_user_fixed and fixed_end is None:
            fixed_end = self._coerce_minutes(raw.get("scheduled_end"), raw.get("endTime"))
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
        original_timing_mode = (
            clean_title(raw.get("original_timing_mode") or raw.get("originalTimingMode") or "")
            or raw_timing_mode
            or timing_mode
            or TimingMode.UNSPECIFIED
        )
        raw_system_scheduled = raw.get("is_system_scheduled")
        is_system_scheduled = (
            bool(raw_system_scheduled)
            if raw_system_scheduled is not None
            else bool(scheduled_start is not None and scheduled_end is not None and not is_user_fixed)
        )
        raw_can_move = raw.get("can_move_for_repair")
        can_move_for_repair = (
            bool(raw_can_move)
            if raw_can_move is not None
            else not bool(is_user_fixed or timing_mode == TimingMode.FIXED or fixed_start is not None)
        )

        location = clean_optional_text(raw.get("location"))
        location_normalized = raw.get("location_normalized") or _normalize_location(location)
        location_label = clean_optional_text(raw.get("location_label")) or location
        location_category = clean_optional_text(raw.get("location_category"))
        location_status = clean_optional_text(raw.get("location_status"))
        location_source = clean_optional_text(raw.get("location_source"))
        location_confidence = raw.get("location_confidence")
        location_kind = clean_optional_text(raw.get("location_kind"))
        location_resolution_status = clean_optional_text(raw.get("location_resolution_status"))
        resolved_location = deepcopy(raw.get("resolved_location")) if isinstance(raw.get("resolved_location"), dict) else None
        location_category_clean = clean_title(location_category or "")
        location_status_clean = clean_title(location_status or "")
        location_kind_clean = clean_title(location_kind or "")
        semantic_status_clean = clean_title(location_resolution_status or "")
        travel_required_raw = raw.get("travel_required")
        if travel_required_raw is None:
            travel_required = not (
                location_category_clean in {"home_or_online", "none", "no_location"}
                or location_status_clean in {"not_required", "no_location_required"}
                or location_kind_clean in {"no_location_required", "online"}
                or semantic_status_clean in {"not_required", "no_location_required"}
            )
        elif isinstance(travel_required_raw, str):
            travel_required = travel_required_raw.strip().lower() not in {"0", "false", "no", "off"}
        else:
            travel_required = bool(travel_required_raw)
        preferred_window_start = self._coerce_minutes(
            raw.get("preferred_window_start"),
            raw.get("preferredWindowStart"),
        )
        preferred_window_end = self._coerce_minutes(
            raw.get("preferred_window_end"),
            raw.get("preferredWindowEnd"),
        )

        is_mandatory = bool(
            raw.get("is_mandatory")
            if raw.get("is_mandatory") is not None
            else raw.get("isMandatory", True)
        )
        repair_protection = self._infer_repair_protection(
            raw,
            title,
            timing_mode,
            fixed_start,
            bool(is_user_fixed),
            is_mandatory,
        )
        if repair_protection == "fixed":
            can_move_for_repair = False

        preferred_time_window = raw.get("preferred_time_window") or raw.get("preferredTimeWindow")
        title_clean_for_window = clean_title(title)
        if not preferred_time_window and "dinner" in title_clean_for_window:
            preferred_time_window = "evening"
            preferred_window_start = preferred_window_start if preferred_window_start is not None else 1080
            preferred_window_end = preferred_window_end if preferred_window_end is not None else 1260

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
            "original_timing_mode": original_timing_mode,
            "is_user_fixed": bool(is_user_fixed),
            "is_system_scheduled": bool(is_system_scheduled),
            "user_fixed_start": user_fixed_start if user_fixed_start is not None else (fixed_start if is_user_fixed else None),
            "can_move_for_repair": bool(can_move_for_repair),
            "repair_protection": repair_protection,
            "fixed_start": fixed_start,
            "fixed_end": fixed_end,
            "earliest_start": earliest_start,
            "latest_end": latest_end,
            "preferred_start": preferred_start,
            "preferred_time_window": preferred_time_window,
            "preferred_window_start": preferred_window_start,
            "preferred_window_end": preferred_window_end,
            "preferred_order": deepcopy(raw.get("preferred_order")) if isinstance(raw.get("preferred_order"), dict) else None,
            "preferred_orders": deepcopy(raw.get("preferred_orders")) if isinstance(raw.get("preferred_orders"), list) else [],
            "soft_dependency": bool(raw.get("soft_dependency", False)),
            "requested_fixed_start": raw.get("requested_fixed_start"),
            "preferred_adjustment": raw.get("preferred_adjustment"),
            "move_direction": raw.get("move_direction"),
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
            "raw_location_text": raw.get("raw_location_text"),
            "location_kind": location_kind,
            "location_resolution_status": location_resolution_status,
            "no_location_reason": raw.get("no_location_reason"),
            "semantic_confidence": raw.get("semantic_confidence"),
            "needs_clarification": bool(raw.get("needs_clarification", False)),
            "parse_notes": raw.get("parse_notes"),
            "saved_location_label": raw.get("saved_location_label"),
            "resolved_location": resolved_location,
            "raw_llm_location": raw.get("raw_llm_location"),
            "explicit_user_location": bool(raw.get("explicit_user_location", False)),
            "location_warning": raw.get("location_warning"),
            "area_preference": raw.get("area_preference"),
            "same_location_as": raw.get("same_location_as"),
            "inherited_from_activity_id": raw.get("inherited_from_activity_id"),
            "travel_required": travel_required,
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
            "implicit_activity": bool(raw.get("implicit_activity", False)),
            "implicit_reason": raw.get("implicit_reason"),
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

