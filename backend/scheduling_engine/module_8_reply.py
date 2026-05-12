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
        schedule_blocks = list(envelope.get("schedule_blocks") or [])
        changed = result.get("updatedActivities") or envelope.get("applied_changes") or []
        referenced_blocks = self._referenced_activity_blocks(schedule_blocks, requested_titles, changed)
        result_applied = bool(result.get("applied", result.get("status") not in {"conflict", "no_operation", "clarification_needed"}))
        if not referenced_blocks and result_applied and envelope.get("activities"):
            referenced_blocks = self._referenced_activity_blocks(schedule_blocks, [], envelope.get("activities", [])[:8])

        status = result.get("status") or envelope.get("status") or ("partial" if conflicts else "success")
        if status == "success" and envelope.get("status") == "warning":
            status = "warning"
        if envelope.get("schedule_status") in {"location_pending", "route_conflict"}:
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
            "location_resolution_requests": envelope.get("location_resolution_requests") or [],
            "route_conflicts": envelope.get("route_conflicts") or [],
            "applied": result_applied,
            "allow_clash": allow_clash,
            "date": envelope.get("date"),
            "shift_operation": shift_operation,
            "primary_operation": primary_operation,
            "conflict": conflict or (conflicts[0] if conflicts else None),
            "conflicts": conflicts[:3],
            "ignored_operations": result.get("ignored_operations") or envelope.get("ignored_operations") or [],
            "rejected_changes": result.get("rejected_changes") or envelope.get("rejected_changes") or [],
            "warnings": warnings[:4],
            "postcondition_results": postcondition_results[:3],
            "requested_titles": requested_titles,
            "referenced_blocks": referenced_blocks[:8],
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

        if status in {"no_operation", "clarification_needed"} or (
            status not in {"conflict", "partial", "route_conflict", "location_pending"}
            and not applied
            and not summary.get("referenced_blocks")
            and not summary.get("changed")
        ):
            return {
                "reply": "I could not understand or apply the requested change. Please try again with the activity name and time.",
                "reply_status": "clarification_needed",
                "recommend_allow_clash": False,
                "reply_reason": "no_operation",
            }

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

