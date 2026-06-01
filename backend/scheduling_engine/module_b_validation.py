from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from jplan_logging import jlog
from .types_utils import *

class ModuleBValidationMixin:
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
                if user_forced:
                    jlog(
                        "MODULE_C",
                        f"Placing {left.get('title') if left_id in forced_ids else right.get('title')} despite overlap with {right.get('title') if left_id in forced_ids else left.get('title')}",
                        "ALLOW_CLASH",
                    )
                    jlog(
                        "MODULE_C",
                        f"{left.get('title')} overlaps {right.get('title')} {format_clock(overlap_start)}-{format_clock(overlap_end)}",
                        "CONFLICT_ALLOWED",
                    )
                for activity in (left, right):
                    activity["is_conflict"] = True
                    activity["is_conflicting"] = True
                    activity.setdefault("conflict_ids", [])
                    if conflict_id not in activity["conflict_ids"]:
                        activity["conflict_ids"].append(conflict_id)
        return conflicts

