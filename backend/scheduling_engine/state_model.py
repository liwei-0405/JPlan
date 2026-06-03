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
        block_type = clean_title(str(item.get("block_type") or ""))
        item_type = clean_title(str(item.get("type") or ""))
        support_types = {
            "buffer",
            "travel",
            "transition",
            "idle",
            "free_time",
            "start_route",
            "route_conflict",
            "prep",
        }
        if item.get("display_only") and block_type != BlockType.ACTIVITY and item_type != BlockType.ACTIVITY:
            return False
        if block_type and block_type != BlockType.ACTIVITY:
            return block_type not in support_types
        if item_type in support_types:
            return False
        title = clean_title(str(item.get("title") or ""))
        if title in {"free_time", "free time", "prep_buffer", "prep buffer"}:
            return False
        return True

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
        if explicit in {"fixed", "protected_social", "flexible", "optional", "derived"}:
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
        anchor_relation = deepcopy(raw.get("anchor_relation"))
        is_derived_time = bool(raw.get("is_derived_time") or raw.get("placement_source") == "system_derived")
        if anchor_relation and is_derived_time and raw_timing_mode == TimingMode.FIXED:
            raw_timing_mode = TimingMode.RELATIVE
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
        if anchor_relation and is_derived_time and raw_is_user_fixed is None:
            is_user_fixed = False
        fixed_start = self._coerce_minutes(raw.get("fixed_start"), raw.get("fixedStart"), user_fixed_start)
        fixed_end = self._coerce_minutes(raw.get("fixed_end"), raw.get("fixedEnd"))
        if anchor_relation and is_derived_time and not is_user_fixed:
            fixed_start = None
            fixed_end = None
            user_fixed_start = None
        semantic_constraint_type = clean_title(raw.get("semantic_constraint_type") or "")
        service_kind = clean_title(raw.get("service_kind") or "")
        if semantic_constraint_type in {"pickup", "exact_anchor"} or service_kind == "pickup":
            is_user_fixed = True
            if fixed_start is None:
                fixed_start = self._coerce_minutes(raw.get("scheduled_start"), raw.get("startTime"))
            if fixed_start is not None and fixed_end is None:
                fixed_end = fixed_start + duration_minutes
        elif semantic_constraint_type in {"arrive_by", "dropoff", "deadline"} or service_kind == "dropoff":
            is_user_fixed = False
            fixed_start = None
            fixed_end = None
            user_fixed_start = None
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
        preferred_end = self._coerce_minutes(raw.get("preferred_end"), raw.get("preferredEnd"))
        timing_mode = self._infer_timing_mode(
            raw,
            fixed_start,
            fixed_end,
            earliest_start,
            latest_end,
            preferred_start,
            anchor_relation,
        )
        if semantic_constraint_type in {"pickup", "exact_anchor"} or service_kind == "pickup":
            timing_mode = TimingMode.FIXED
        elif semantic_constraint_type in {"arrive_by", "dropoff", "deadline"} or service_kind == "dropoff":
            timing_mode = TimingMode.WINDOW

        semantic_fixed_start = fixed_start
        semantic_fixed_end = fixed_end
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
        if semantic_constraint_type in {"pickup", "exact_anchor"} or service_kind == "pickup":
            timing_mode = TimingMode.FIXED
            fixed_start = fixed_start if fixed_start is not None else semantic_fixed_start
            fixed_end = fixed_end if fixed_end is not None else semantic_fixed_end
            if fixed_start is not None and fixed_end is None:
                fixed_end = fixed_start + duration_minutes
            timing_trace = "Semantic timing: fixed because this is an exact anchor."
        elif semantic_constraint_type in {"arrive_by", "dropoff", "deadline"} or service_kind == "dropoff":
            timing_mode = TimingMode.WINDOW
            fixed_start = None
            fixed_end = None
            timing_trace = "Semantic timing: deadline because this service must finish by the requested time."
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
        location_policy = raw.get("location_policy") or raw.get("locationPolicy")
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
        has_resolved_coordinates = False
        if isinstance(resolved_location, dict):
            try:
                lat = float(resolved_location.get("latitude") if resolved_location.get("latitude") is not None else resolved_location.get("lat"))
                lng = float(resolved_location.get("longitude") if resolved_location.get("longitude") is not None else resolved_location.get("lng"))
                has_resolved_coordinates = -90 <= lat <= 90 and -180 <= lng <= 180
            except (TypeError, ValueError):
                has_resolved_coordinates = False
        endpoint_text = clean_title(
            " ".join(
                str(value or "")
                for value in (
                    location,
                    location_label,
                    (resolved_location or {}).get("label") if isinstance(resolved_location, dict) else None,
                    (resolved_location or {}).get("display_name") if isinstance(resolved_location, dict) else None,
                )
            )
        )
        policy_clean = clean_title(location_policy or "").replace(" ", "_").replace("-", "_")
        source_clean = clean_title(location_source or "").replace(" ", "_").replace("-", "_")
        user_selected_physical_endpoint = bool(
            has_resolved_coordinates
            and not re.search(r"\b(current location|current place|starting point|start location|default start)\b", endpoint_text)
            and (
                travel_required
                or policy_clean == "exact_location_required"
                or source_clean in {"event_manual_location", "selected_geocode", "event_confirmed", "manual_edit"}
                or bool(raw.get("explicit_user_location", False))
            )
        )
        if user_selected_physical_endpoint:
            travel_required = True
            location_status = "resolved"
            location_resolution_status = "resolved"
            location_policy = "exact_location_required"
            if location_kind_clean in {"", "no_location_required", "online", "none", "no_location"}:
                location_kind = "exact_named_place"
                location_kind_clean = "exact_named_place"
            if location_category_clean in {"", "home_or_online", "none", "no_location"}:
                location_category = (resolved_location or {}).get("category") or "manual_place"
                location_category_clean = clean_title(location_category or "")
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
        if anchor_relation and is_derived_time and not is_user_fixed:
            repair_protection = "derived"
            can_move_for_repair = True

        preferred_time_window = raw.get("preferred_time_window") or raw.get("preferredTimeWindow")
        title_clean_for_window = clean_title(title)
        if not preferred_time_window and "dinner" in title_clean_for_window:
            preferred_time_window = "evening"
            preferred_window_start = preferred_window_start if preferred_window_start is not None else 1080
            preferred_window_end = preferred_window_end if preferred_window_end is not None else 1260
        if (preferred_time_window and clean_title(preferred_time_window) == "business_hours") or re.search(r"\bbank(?:ing)?\b", title_clean_for_window):
            preferred_time_window = preferred_time_window or "business_hours"
            preferred_window_start = preferred_window_start if preferred_window_start is not None else 9 * 60
            preferred_window_end = preferred_window_end if preferred_window_end is not None else 16 * 60
            earliest_start = earliest_start if earliest_start is not None else 9 * 60
            latest_end = latest_end if latest_end is not None else 16 * 60

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
            "preferred_end": preferred_end,
            "preferred_time_window": preferred_time_window,
            "preferred_window_start": preferred_window_start,
            "preferred_window_end": preferred_window_end,
            "earliest_preferred_start": self._coerce_minutes(raw.get("earliest_preferred_start"), raw.get("earliestPreferredStart")),
            "latest_preferred_end": self._coerce_minutes(raw.get("latest_preferred_end"), raw.get("latestPreferredEnd")),
            "ideal_start": self._coerce_minutes(raw.get("ideal_start"), raw.get("idealStart")),
            "ideal_end": self._coerce_minutes(raw.get("ideal_end"), raw.get("idealEnd")),
            "ideal_start_range": deepcopy(raw.get("ideal_start_range") or raw.get("idealStartRange")) if isinstance(raw.get("ideal_start_range") or raw.get("idealStartRange"), (list, tuple)) else raw.get("ideal_start_range") or raw.get("idealStartRange"),
            "acceptable_start": self._coerce_minutes(raw.get("acceptable_start"), raw.get("acceptableStart")),
            "acceptable_end": self._coerce_minutes(raw.get("acceptable_end"), raw.get("acceptableEnd")),
            "acceptable_start_range": deepcopy(raw.get("acceptable_start_range") or raw.get("acceptableStartRange")) if isinstance(raw.get("acceptable_start_range") or raw.get("acceptableStartRange"), (list, tuple)) else raw.get("acceptable_start_range") or raw.get("acceptableStartRange"),
            "preference_weight": raw.get("preference_weight") or raw.get("preferenceWeight"),
            "preference_priority": raw.get("preference_priority") or raw.get("preferencePriority"),
            "preference_type": raw.get("preference_type") or raw.get("preferenceType"),
            "is_hard_window": bool(raw.get("is_hard_window") or raw.get("isHardWindow", False)),
            "is_soft_window": bool(raw.get("is_soft_window") or raw.get("isSoftWindow", False)),
            "preferred_order": deepcopy(raw.get("preferred_order")) if isinstance(raw.get("preferred_order"), dict) else None,
            "preferred_orders": deepcopy(raw.get("preferred_orders")) if isinstance(raw.get("preferred_orders"), list) else [],
            "soft_dependency": bool(raw.get("soft_dependency", False)),
            "relation_type": raw.get("relation_type") or (anchor_relation or {}).get("kind"),
            "anchor_activity_id": raw.get("anchor_activity_id") or (anchor_relation or {}).get("target_activity_id"),
            "anchor_title": raw.get("anchor_title") or (anchor_relation or {}).get("target_title"),
            "relative_offset_minutes": raw.get("relative_offset_minutes") or raw.get("offset_minutes") or 0,
            "placement_source": raw.get("placement_source") or ("system_derived" if anchor_relation and not is_user_fixed else None),
            "is_derived_time": bool(is_derived_time or (anchor_relation and not is_user_fixed)),
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
            "location_policy": location_policy,
            "no_location_reason": raw.get("no_location_reason"),
            "semantic_confidence": raw.get("semantic_confidence"),
            "needs_clarification": bool(raw.get("needs_clarification", False)),
            "parse_notes": raw.get("parse_notes"),
            "saved_location_label": raw.get("saved_location_label"),
            "resolved_location": resolved_location,
            "raw_llm_location": raw.get("raw_llm_location"),
            "explicit_user_location": bool(raw.get("explicit_user_location", False)),
            "location_warning": raw.get("location_warning"),
            "location_flexible": False if user_selected_physical_endpoint else bool(raw.get("location_flexible", False)),
            "can_be_done_at_current_location": False if user_selected_physical_endpoint else bool(raw.get("can_be_done_at_current_location", False)),
            "quiet_place_required": bool(raw.get("quiet_place_required", False)),
            "activity_role": raw.get("activity_role"),
            "travel_context_required": bool(raw.get("travel_context_required", False)),
            "semantic_constraint_type": raw.get("semantic_constraint_type"),
            "service_kind": raw.get("service_kind"),
            "arrive_by": self._coerce_minutes(raw.get("arrive_by"), raw.get("arriveBy")),
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
