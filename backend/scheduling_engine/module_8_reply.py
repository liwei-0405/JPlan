import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4
from zoneinfo import ZoneInfo

from jplan_logging import jjson, jlog, jlog_verbose, jsection
from travel_service import MissingORSApiKey, TravelService, TravelServiceError, coordinate_from_saved_location
from .types_utils import *
from .types_utils import _normalize_location

MODULE8_LLM_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="jplan-module8")

class Module8ReplyMixin:
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
        priority_operation = self._priority_reply_operation(parsed or {}, result)
        schedule_blocks = list(envelope.get("schedule_blocks") or [])
        changed = result.get("updatedActivities") or envelope.get("applied_changes") or []
        referenced_blocks = self._referenced_activity_blocks(schedule_blocks, requested_titles, changed)
        removed_changes = list(result.get("removedActivities") or [])
        removed_changes.extend([
            item for item in changed
            if isinstance(item, dict) and clean_title(item.get("status") or "") == "removed"
        ])
        result_applied = bool(result.get("applied", result.get("status") not in {"conflict", "no_operation", "clarification_needed"}))
        if not referenced_blocks and result_applied and envelope.get("activities"):
            referenced_blocks = self._referenced_activity_blocks(schedule_blocks, [], envelope.get("activities", [])[:8])

        status = result.get("status") or envelope.get("status") or ("partial" if conflicts else "success")
        if status == "success" and envelope.get("status") == "warning":
            status = "warning"
        if result.get("status") not in {"conflict", "no_operation", "clarification_needed"} and envelope.get("schedule_status") in {"location_pending", "route_conflict"}:
            status = envelope.get("schedule_status")

        allowed_times = set(self._allowed_reply_times(referenced_blocks, warnings))
        for conflict_item in conflicts[:3]:
            for key in ("start", "end"):
                parsed_time = parse_clock(conflict_item.get(key))
                if parsed_time is not None:
                    allowed_times.add(format_clock(parsed_time))
        for failure in postcondition_results[:3]:
            for key in (
                "target_start",
                "target_end",
                "anchor_start",
                "anchor_end",
                "available_window_start",
                "available_window_end",
            ):
                parsed_time = parse_clock(failure.get(key))
                if parsed_time is not None:
                    allowed_times.add(format_clock(parsed_time))

        return {
            "status": status,
            "envelope_status": envelope.get("status"),
            "schedule_status": envelope.get("schedule_status"),
            "travel_validation_status": envelope.get("travel_validation_status"),
            "reply_hint": result.get("reply") or envelope.get("reply"),
            "reply_reason": result.get("reply_reason") or envelope.get("reply_reason"),
            "location_resolution_requests": envelope.get("location_resolution_requests") or [],
            "route_conflicts": envelope.get("route_conflicts") or [],
            "applied": result_applied,
            "allow_clash": allow_clash,
            "date": envelope.get("date"),
            "shift_operation": shift_operation,
            "primary_operation": primary_operation,
            "priority_operation": priority_operation,
            "refinement_applied": bool(envelope.get("refinement_applied") or result.get("refinement_applied")),
            "refinement_accepted_moves": envelope.get("refinement_accepted_moves") or result.get("refinement_accepted_moves") or [],
            "conflict": conflict or (conflicts[0] if conflicts else None),
            "conflicts": conflicts[:3],
            "ignored_operations": result.get("ignored_operations") or envelope.get("ignored_operations") or [],
            "rejected_changes": result.get("rejected_changes") or envelope.get("rejected_changes") or [],
            "warnings": warnings[:4],
            "postcondition_results": postcondition_results[:3],
            "requested_titles": requested_titles,
            "referenced_blocks": referenced_blocks[:8],
            "removed_changes": removed_changes,
            "allowed_times": sorted(allowed_times),
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

    def _priority_reply_operation(self, parsed: Dict[str, Any], result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        priority_noop = result.get("priority_noop")
        if isinstance(priority_noop, dict) and priority_noop.get("title") and priority_noop.get("priority"):
            return {
                "title": priority_noop.get("title"),
                "priority": str(priority_noop.get("priority") or "").lower(),
                "direction": priority_noop.get("direction"),
                "priority_update_only": True,
                "already_set": True,
            }
        for operation in parsed.get("operations") or []:
            if not isinstance(operation, dict) or operation.get("priority") is None:
                continue
            op_type = clean_title(operation.get("op") or "update")
            if op_type not in {"update", "move", "replace", "update_priority"}:
                continue
            title = operation.get("resolved_target_title") or operation.get("title") or operation.get("target_title")
            priority = str(operation.get("priority") or "").lower()
            changed_items = [
                changed for changed in result.get("updatedActivities") or result.get("applied_changes") or []
                if isinstance(changed, dict)
            ]
            if len(changed_items) == 1:
                changed = changed_items[0]
                title = changed.get("title") or title
                priority = str(changed.get("priority") or priority).lower()
            for changed in changed_items:
                if not isinstance(changed, dict):
                    continue
                if clean_title(changed.get("title") or "") == clean_title(title or ""):
                    title = changed.get("title") or title
                    priority = str(changed.get("priority") or priority).lower()
                    break
            return {
                "title": title,
                "priority": priority,
                "direction": operation.get("priority_direction"),
                "priority_update_only": bool(operation.get("priority_update_only")),
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

        operation = summary.get("primary_operation") or {}
        if clean_title(operation.get("op") or "") == "optimize_schedule":
            if summary.get("refinement_applied") and summary.get("refinement_accepted_moves"):
                return "I optimized the schedule by adjusting flexible activities while keeping fixed commitments unchanged."
            return "I checked your schedule, but did not find a safe optimization to apply."

        priority = summary.get("priority_operation") or {}
        if priority.get("title") and priority.get("priority"):
            title = priority.get("title")
            priority_value = priority.get("priority")
            direction = clean_title(priority.get("direction") or "")
            jlog("MODULE_8", f"target={title} priority={priority_value}", "PRIORITY_REPLY")
            if direction == "lowered":
                return f"I've lowered {title} priority."
            if direction == "raised":
                return f"I've raised {title} priority."
            return f"I've updated {title} priority to {priority_value}."

        blocks = summary.get("referenced_blocks") or []
        changed = summary.get("changed") or []
        op_type = clean_title(operation.get("op") or "")
        if op_type == "remove":
            removed = (summary.get("removed_changes") or [{}])[0]
            title = removed.get("title") or operation.get("title") or "that activity"
            jlog("MODULE_8", f"removed={title}", "REMOVE_REPLY")
            return f"I've removed {title} from your schedule."
        if not blocks and not changed:
            return None

        if len(changed) > 1:
            titles = [item.get("title") for item in changed if item.get("title")]
            if titles:
                date_text = f" for {summary.get('date')}" if summary.get("date") else ""
                return f"I generated your schedule{date_text}, including {', '.join(titles)}."

        block = blocks[0] if blocks else changed[0]
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

        if status in {"no_operation", "clarification_needed"} or (
            status not in {"conflict", "partial", "route_conflict", "location_pending"}
            and not applied
            and not summary.get("referenced_blocks")
            and not summary.get("changed")
        ):
            if summary.get("reply_reason") == "priority_already_set":
                priority = summary.get("priority_operation") or {}
                title = priority.get("title") or "That activity"
                priority_value = priority.get("priority") or "that"
                jlog("MODULE_8", f"target={title} priority={priority_value}", "PRIORITY_REPLY")
                return {
                    "reply": summary.get("reply_hint") or f"{title} is already set to {priority_value} priority.",
                    "reply_status": "success",
                    "recommend_allow_clash": False,
                    "reply_reason": "priority_already_set",
                }
            if summary.get("reply_reason") == "already_satisfied" and summary.get("reply_hint"):
                return {
                    "reply": summary.get("reply_hint"),
                    "reply_status": "success",
                    "recommend_allow_clash": False,
                    "reply_reason": "already_satisfied",
                }
            return {
                "reply": summary.get("reply_hint") or "I could not understand or apply the requested change. Please try again with the activity name and time.",
                "reply_status": "clarification_needed",
                "recommend_allow_clash": False,
                "reply_reason": summary.get("reply_reason") or "no_operation",
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

        if status == "location_pending":
            requests = summary.get("location_resolution_requests") or []
            request_titles = [str(req.get("title") or "an activity") for req in requests]
            if len(request_titles) <= 5:
                titles = ", ".join(request_titles)
            else:
                titles = f"{', '.join(request_titles[:4])}, and {len(request_titles) - 4} more"
            return {
                "reply": (
                    "I drafted the schedule, but travel-aware validation still needs locations. "
                    f"Please confirm {titles or 'the pending activities'}, then complete travel validation."
                ),
                "reply_status": "location_pending",
                "recommend_allow_clash": False,
                "reply_reason": "Travel-aware validation needs confirmed coordinates before final validation.",
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
        referenced_rows = []
        for block in summary.get("referenced_blocks") or []:
            title = block.get("title") or "Activity"
            start = block.get("start") or "?"
            end = block.get("end") or "?"
            referenced_rows.append(f"- {title}: {start}-{end}")
        if referenced_rows:
            jlog("MODULE_8", "referenced_blocks:\n" + "\n".join(referenced_rows), "REPLY")
        reply_class = self._module_8_reply_class(reply_status)
        jlog(
            "MODULE_8",
            f"source={source} status={reply_class} text={json.dumps(reply, ensure_ascii=True)}",
            "REPLY",
        )

    def _module_8_fallback(
        self,
        fallback: Dict[str, Any],
        summary: Dict[str, Any],
        reason: str,
        token_usage: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        final = {
            **fallback,
            "token_usage": token_usage,
            "reply_source": "fallback-template",
            "llm_fallback_reason": reason,
        }
        if reason == "module_8_timeout":
            jlog("MODULE_8", "fallback=fallback-template", "TIMEOUT")
            jlog("MODULE_8", "reason=timeout", "FALLBACK")
        else:
            jlog("MODULE_8", f"reason={reason}", "FALLBACK")
        self._log_module_8_final(final, summary, "fallback-template")
        return final

    def _call_module_8_llm(self, prompt: str) -> Any:
        return self.client.models.generate_content(
            model=MODULE8_LLM_MODEL,
            contents=prompt,
            config={"temperature": 0.2, "max_output_tokens": 150},
        )

    def compose_result_reply(
        self,
        latest_request: str,
        parsed: Dict[str, Any],
        result: Dict[str, Any],
        allow_clash: bool,
    ) -> Dict[str, Any]:
        """Second-pass result-aware reply. The LLM phrases; deterministic checks decide truth."""
        summary = self._compact_result_summary(result, allow_clash, parsed)
        jlog(
            "MODULE_8",
            (
                f"applied_operations={len(summary.get('changed') or [])} "
                f"rejected_operations={len(summary.get('rejected_changes') or [])} "
                f"ignored_operations={len(summary.get('ignored_operations') or [])}"
            ),
            "TRUTH",
        )
        fallback = self._fallback_result_reply(latest_request, summary)

        jsection("MODULE_8", "result-aware reply", "REPLY")

        if clean_title((summary.get("primary_operation") or {}).get("op") or "") == "remove":
            fallback = {**fallback, "reply_source": "template"}
            self._log_module_8_final(fallback, summary, "template")
            return fallback

        if fallback.get("reply_status") == "location_pending":
            fallback = {**fallback, "reply_source": "template"}
            self._log_module_8_final(fallback, summary, "template")
            return fallback

        if fallback.get("reply_reason") == "priority_already_set":
            fallback = {**fallback, "reply_source": "template"}
            self._log_module_8_final(fallback, summary, "template")
            return fallback

        if not getattr(self.client, "models", None):
            fallback = {**fallback, "reply_source": "template"}
            self._log_module_8_final(fallback, summary, "template")
            return fallback

        prompt = f"""
You are JPlan's final response writer.
Use ONLY RESULT_SUMMARY. Return 1-2 short sentences.
Do not invent times, dates, locations, blockers, or actions.
Do not claim success unless applied=true.
If applied=false, say the requested change was not applied and explain the reason from RESULT_SUMMARY.
If status=conflict and allow_clash=false, mention Allow Clash as an option.
If priority_operation exists, mention the priority change, not the activity time range.
If status=warning, mention the warning briefly.
If status=location_pending, ask user to confirm the listed locations.
For a single changed activity, include its exact start and end time from referenced_blocks.
For generated schedules, mention all requested_titles that appear in referenced_blocks.

USER_REQUEST:
{latest_request}

PARSED_OPERATIONS:
{json.dumps(parsed.get("operations") or parsed.get("activities") or [], ensure_ascii=True)[:600]}

RESULT_SUMMARY:
{json.dumps(summary, ensure_ascii=True)[:1200]}
"""
        try:
            llm_start = time.perf_counter()
            timeout_seconds = min(MODULE8_LLM_TIMEOUT_SECONDS, MODULE8_LLM_TOTAL_TIMEOUT_SECONDS)
            jlog_verbose("MODULE_8", f"start timeout={int(timeout_seconds * 1000)}ms", "LLM")
            future = MODULE8_LLM_EXECUTOR.submit(self._call_module_8_llm, prompt)
            try:
                response = future.result(timeout=timeout_seconds)
            except TimeoutError:
                future.cancel()
                return self._module_8_fallback(fallback, summary, "module_8_timeout")
            jlog("TIMER", f"module_8_llm_seconds={time.perf_counter() - llm_start:.2f}", None)
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
                return self._module_8_fallback(fallback, summary, "empty_module_8_reply", token_usage)

            if fallback["reply_status"] == "conflict":
                if not self._reply_claims_failure(reply):
                    return self._module_8_fallback(fallback, summary, "truth_guard_conflict", token_usage)
            elif fallback["reply_status"] in {"success", "warning"} and self._reply_claims_failure(reply):
                return self._module_8_fallback(fallback, summary, "truth_guard_false_failure", token_usage)
            elif fallback["reply_status"] == "partial" and summary.get("conflicts"):
                clean_reply = clean_title(reply)
                if "clash" not in clean_reply and "conflict" not in clean_reply and "overlap" not in clean_reply:
                    return self._module_8_fallback(fallback, summary, "truth_guard_missing_conflict", token_usage)

            if not self._reply_uses_only_allowed_times(reply, summary):
                return self._module_8_fallback(fallback, summary, "truth_guard_time", token_usage)

            if not self._reply_uses_only_allowed_dates(reply, summary):
                return self._module_8_fallback(fallback, summary, "truth_guard_date", token_usage)

            if not self._reply_mentions_required_range(reply, summary):
                return self._module_8_fallback(fallback, summary, "truth_guard_required_range", token_usage)

            if not self._reply_mentions_requested_titles(reply, summary):
                return self._module_8_fallback(fallback, summary, "truth_guard_requested_titles", token_usage)

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
            return self._module_8_fallback(fallback, summary, "module_8_unavailable")

