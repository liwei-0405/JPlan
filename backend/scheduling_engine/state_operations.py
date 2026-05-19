import json
import re
import time
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4
from zoneinfo import ZoneInfo

from jplan_logging import jjson, jlog, jplan_verbose_enabled, jsection
from travel_service import MissingORSApiKey, TravelService, TravelServiceError, coordinate_from_saved_location
from .module_d_refinement import REFINEMENT_META_KEYS
from .types_utils import *
from .types_utils import _normalize_location

class StateOperationsMixin:
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
        normalized["target_id"] = raw.get("target_id") or raw.get("activity_id") or raw.get("stable_activity_id") or raw.get("id")
        normalized["target_title"] = raw.get("target_title") or raw.get("target") or raw.get("title")
        if raw.get("duration_minutes") is None and raw.get("duration") is not None:
            normalized["duration_minutes"] = parse_duration_minutes(raw.get("duration"))
        if normalized.get("anchor_relation") and not normalized.get("timing_mode"):
            normalized["timing_mode"] = TimingMode.RELATIVE
        if normalized.get("fixed_start") is not None and not normalized.get("timing_mode"):
            normalized["timing_mode"] = TimingMode.FIXED
        return normalized

    def _should_inherit_anchor_location(self, operation: Dict[str, Any]) -> bool:
        """Only inherit anchor location for same-place follow-up blocks.

        A coffee break right after a meeting usually shares the meeting place.
        Dinner after grocery shopping should not silently become "at the store".
        """
        title = clean_title(operation.get("title") or operation.get("target_title") or "")
        category = clean_title(operation.get("location_category") or "")
        if any(keyword in title for keyword in ("coffee", "break", "pause")):
            return True
        if category in {"meal_place", "supermarket", "fitness_center", "home"}:
            return False
        status = clean_title(operation.get("location_status") or "")
        if status in {"needs_resolution", "unresolved"}:
            return False
        return True

    def _copy_anchor_location_to_operation(
        self,
        operation: Dict[str, Any],
        anchor_activity: Optional[Dict[str, Any]],
    ) -> None:
        if not anchor_activity:
            return
        operation["location"] = anchor_activity.get("location")
        operation["location_label"] = anchor_activity.get("location_label") or anchor_activity.get("location")
        operation["location_category"] = anchor_activity.get("location_category") or operation.get("location_category")
        operation["location_normalized"] = anchor_activity.get("location_normalized")
        operation["location_source"] = "inferred_from_anchor"
        operation["same_location_as"] = anchor_activity.get("title")
        operation["inherited_from_activity_id"] = (
            anchor_activity.get("stable_activity_id")
            or anchor_activity.get("id")
            or anchor_activity.get("activity_id")
        )
        if anchor_activity.get("resolved_location"):
            operation["resolved_location"] = deepcopy(anchor_activity.get("resolved_location"))
            operation["location_status"] = "resolved"
        else:
            operation["location_status"] = anchor_activity.get("location_status") or operation.get("location_status")

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
        if not clean_message or not clean_activity:
            return False
        for word in action_words:
            if re.search(rf"\b{word}\s+(?:my\s+|the\s+)?{re.escape(clean_activity)}\b", clean_message):
                return True
        if clean_activity in clean_message and re.search(r"\b(?:to|at)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b", clean_message):
            return True
        return False

    def _resolution_confidence(self, score: int) -> str:
        if score >= 120:
            return "high"
        if score >= 90:
            return "medium"
        return "low"

    def _operation_original_target(self, operation: Dict[str, Any]) -> str:
        return str(
            operation.get("original_user_target")
            or operation.get("target_title")
            or operation.get("title")
            or operation.get("target_id")
            or ""
        ).strip()

    def _operation_targets_requested_activity(
        self,
        operation: Dict[str, Any],
        resolution: Dict[str, Any],
        message: str,
    ) -> bool:
        target = resolution.get("activity") or {}
        original = self._operation_original_target(operation)
        resolved_title = target.get("title") or resolution.get("resolved_target_title") or ""
        confidence = resolution.get("target_resolution_confidence") or "low"
        allowed = False
        if self._message_explicitly_targets_activity(message, resolved_title):
            allowed = True
        elif confidence == "high" and self._message_explicitly_targets_activity(message, original):
            allowed = True
        elif confidence == "high" and clean_title(original) in {"it", "this", "that"} and resolved_title:
            allowed = True
        elif confidence == "high" and operation.get("pronoun_resolved_target_title") == resolved_title:
            allowed = True

        jlog(
            "TARGET_RESOLUTION",
            f"original={original or '?'} resolved={resolved_title or '?'} confidence={confidence} allowed={str(allowed).lower()}",
            None,
        )
        return allowed

    def _relative_order_already_satisfied(
        self,
        operation: Dict[str, Any],
        active_pool: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if operation.get("op") not in {"update", "move"} or not operation.get("anchor_relation"):
            return None
        target_resolution = self._resolve_activity_reference(
            operation.get("target_id") or operation.get("target_title") or operation.get("title"),
            active_pool,
        )
        anchor = self._resolve_anchor_relation(operation.get("anchor_relation"), active_pool)
        if target_resolution.get("status") != "resolved" or not anchor:
            return None
        target = target_resolution["activity"]
        anchor_activity = self._find_activity_by_stable_id(active_pool, anchor.get("target_activity_id"))
        if not anchor_activity:
            return None
        target_start = target.get("scheduled_start")
        target_end = target.get("scheduled_end")
        anchor_start = anchor_activity.get("scheduled_start")
        anchor_end = anchor_activity.get("scheduled_end")
        if None in {target_start, target_end, anchor_start, anchor_end}:
            return None
        kind = clean_title(anchor.get("kind") or "after")
        satisfied = target_start >= anchor_end if kind == "after" else target_end <= anchor_start
        if not satisfied:
            return None
        return {
            "target": target.get("title"),
            "anchor": anchor_activity.get("title"),
            "kind": kind,
        }

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

    def _operation_has_relation_or_edit_intent(self, operation: Dict[str, Any]) -> bool:
        timing_mode = clean_title(operation.get("timing_mode") or "")
        return bool(
            operation.get("anchor_relation")
            or timing_mode == TimingMode.RELATIVE
            or operation.get("preferred_adjustment")
            or operation.get("move_direction")
        )

    def _duplicate_guard_allows_new_activity(self, operation: Dict[str, Any]) -> bool:
        message = clean_title(operation.get("_user_message") or operation.get("_source_text") or "")
        if re.search(r"\b(?:another|new|second|additional)\b", message):
            return True
        return False

    def _message_mentions_duration(self, message: str) -> bool:
        return bool(re.search(
            r"\b(?:for\s+)?\d{1,3}\s*(?:-| )?\s*(?:minutes?|mins?|min|hours?|hrs?|hr)\b|"
            r"\b(?:half[-\s]*hour|half\s+an\s+hour)\b",
            message or "",
            flags=re.IGNORECASE,
        ))

    def _location_field_names(self) -> Tuple[str, ...]:
        return (
            "location",
            "location_label",
            "location_category",
            "location_status",
            "location_source",
            "location_confidence",
            "location_normalized",
            "location_warning",
            "saved_location_label",
            "resolved_location",
            "raw_location_text",
            "location_kind",
            "location_resolution_status",
            "no_location_reason",
            "semantic_confidence",
            "needs_clarification",
            "parse_notes",
            "raw_llm_location",
            "explicit_user_location",
            "area_preference",
            "same_location_as",
            "inherited_from_activity_id",
            "latitude",
            "longitude",
            "lat",
            "lng",
            "travel_required",
        )

    def _operation_has_location_payload(self, operation: Dict[str, Any]) -> bool:
        return any(operation.get(field) is not None for field in self._location_field_names())

    def _payload_has_coordinates(self, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        return (
            payload.get("latitude") is not None
            and payload.get("longitude") is not None
        ) or (
            payload.get("lat") is not None
            and (payload.get("lng") is not None or payload.get("lon") is not None)
        )

    def _message_has_explicit_location_or_modality_change(self, message: str) -> bool:
        clean = clean_title(message or "")
        if not clean:
            return False
        if re.search(r"\b(change|update|set|switch)\s+(the\s+)?(location|place|venue)\s+(to|as)\b", clean):
            return True
        if re.search(r"\b(change|update|set|switch)\s+.*\b(to|at|near)\s+[a-z0-9]", clean) and not re.search(r"\b(to|at)\s+(after|before|earlier|later)\b", clean):
            return True
        if re.search(r"\b(at|near|inside|around)\s+(?!after\b|before\b|later\b|earlier\b)[a-z0-9]", clean):
            return True
        if re.search(r"\b(make|set|change|switch)\s+.*\b(online|virtual|remote|home|at home)\b", clean):
            return True
        return False

    def _operation_has_explicit_location_change(self, operation: Dict[str, Any]) -> bool:
        if operation.get("explicit_user_location") is True:
            return True
        if self._payload_has_coordinates(operation.get("resolved_location")):
            return True
        if operation.get("latitude") is not None and operation.get("longitude") is not None:
            return True
        if operation.get("lat") is not None and (operation.get("lng") is not None or operation.get("lon") is not None):
            return True
        source = clean_title(operation.get("location_source") or "")
        if source in {
            "saved_location",
            "saved_profile",
            "selected_geocode",
            "geocode",
            "geocode_result",
            "map_pin",
            "recent",
            "recent_location",
            "same_as_activity",
            "same_label_reuse",
            "user_selected",
            "event_confirmed",
        }:
            return True
        if operation.get("same_location_as") and source != "inferred_from_anchor":
            return True
        return self._message_has_explicit_location_or_modality_change(operation.get("_user_message") or "")

    def _copy_operation_location_fields(self, target: Dict[str, Any], operation: Dict[str, Any]) -> None:
        for field in self._location_field_names():
            if field in operation:
                target[field] = deepcopy(operation.get(field))

    def _restore_location_fields(self, target: Dict[str, Any], source: Dict[str, Any]) -> None:
        for field in self._location_field_names():
            if field in source:
                target[field] = deepcopy(source.get(field))
            else:
                target.pop(field, None)

    def _parser_location_label_for_log(self, operation: Dict[str, Any]) -> str:
        for field in ("location_label", "location", "location_normalized", "raw_location_text", "location_category", "raw_llm_location"):
            value = operation.get(field)
            if value:
                return str(value)
        return "(none)"

    def _cleanup_block_kind(self, block: Dict[str, Any]) -> str:
        return clean_title(block.get("block_type") or block.get("type") or "")

    def _is_cleanup_support_block(self, block: Dict[str, Any]) -> bool:
        block_kind = self._cleanup_block_kind(block)
        title = clean_title(block.get("title") or "")
        return (
            block_kind in {"transition", "travel", "buffer", "prep"}
            or title.startswith("travel to")
            or "buffer" in title
        )

    def _support_block_related_ids(self, block: Dict[str, Any]) -> set[str]:
        values: List[Any] = [
            block.get("source_activity_id"),
            block.get("destination_activity_id"),
            block.get("from_activity_id"),
            block.get("to_activity_id"),
        ]
        related = block.get("related_activity_ids")
        if isinstance(related, list):
            values.extend(related)
        return {str(value) for value in values if value}

    def _prune_support_blocks_for_deleted_activities(
        self,
        blocks: Optional[List[Dict[str, Any]]],
        deleted_activity_ids: List[str],
    ) -> List[Dict[str, Any]]:
        if not blocks:
            return []
        deleted = {str(activity_id) for activity_id in deleted_activity_ids if activity_id}
        if not deleted:
            return blocks
        pruned: List[Dict[str, Any]] = []
        for block in blocks:
            block_id = str(block.get("stable_activity_id") or block.get("id") or "")
            if block_id in deleted:
                continue
            if self._is_cleanup_support_block(block):
                related_ids = self._support_block_related_ids(block)
                generated_id = str(block.get("id") or "")
                if related_ids.intersection(deleted) or any(activity_id and activity_id in generated_id for activity_id in deleted):
                    continue
            pruned.append(block)
        return pruned

    def _strip_parser_wrapper_from_title(self, title: str) -> str:
        text = clean_title(title or "")
        text = re.sub(
            r"^(?:how about|what about|maybe|what if|i was thinking|i think maybe|"
            r"would it be better if|would it help if|i want|i need|i would like|can we|could we)\s+",
            "",
            text,
        )
        text = re.sub(r"\s+(?:is|should|could|would)\s*$", "", text).strip()
        return text

    def _duplicate_guard_reference_resolution(
        self,
        operation: Dict[str, Any],
        active_pool: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        reference = str(operation.get("title") or operation.get("target_title") or "").strip()
        resolution = self._resolve_activity_reference(reference, active_pool)
        if resolution.get("status") in {"resolved", "ambiguous"}:
            return resolution
        stripped = self._strip_parser_wrapper_from_title(reference)
        if stripped and stripped != clean_title(reference):
            stripped_resolution = self._resolve_activity_reference(stripped, active_pool)
            if stripped_resolution.get("status") in {"resolved", "ambiguous"}:
                stripped_resolution["original_user_target"] = reference
                return stripped_resolution
        return resolution

    def _apply_duplicate_guard_to_add(
        self,
        operation: Dict[str, Any],
        active_pool: List[Dict[str, Any]],
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        if clean_title(operation.get("op") or "add") != "add":
            return operation, None
        if not active_pool or not self._operation_has_relation_or_edit_intent(operation):
            return operation, None
        if self._duplicate_guard_allows_new_activity(operation):
            return operation, None

        resolution = self._duplicate_guard_reference_resolution(operation, active_pool)
        original = str(operation.get("title") or operation.get("target_title") or "").strip()
        if resolution.get("status") == "ambiguous":
            candidates = resolution.get("candidates") or []
            jlog(
                "DUPLICATE_GUARD",
                f"original={original or '?'} candidates={candidates}",
                "CLARIFY",
            )
            return None, {
                "operation": operation,
                "target": original,
                "reason": "duplicate_guard_ambiguous_target",
                "candidates": candidates,
            }
        if resolution.get("status") != "resolved":
            return operation, None

        confidence = resolution.get("target_resolution_confidence") or "low"
        if confidence not in {"medium", "high"}:
            return operation, None

        target = resolution.get("activity") or {}
        target_id = target.get("stable_activity_id") or target.get("id")
        converted = deepcopy(operation)
        converted["op"] = "update"
        converted["title"] = target.get("title") or converted.get("title")
        converted["target_title"] = target.get("title") or converted.get("target_title") or converted.get("title")
        converted["target_id"] = target_id
        converted["activity_id"] = target_id
        converted["preserve_existing_fields"] = True
        converted["_preserve_resolved_title"] = True
        converted["_duplicate_guard_converted_add_to_update"] = True
        converted["original_user_target"] = original
        converted["resolved_target_title"] = target.get("title")
        converted["target_resolution_confidence"] = confidence

        resolved_anchor = self._resolve_anchor_relation(converted.get("anchor_relation"), active_pool)
        if resolved_anchor:
            converted["anchor_relation"] = resolved_anchor

        message = converted.get("_user_message") or ""
        if not self._message_mentions_duration(message):
            converted.pop("duration_minutes", None)
        if not re.search(r"\bpriority\b", clean_title(message)):
            converted.pop("priority", None)
        if not self._operation_has_explicit_location_change(converted):
            for field in self._location_field_names():
                converted.pop(field, None)

        jlog(
            "DUPLICATE_GUARD",
            f"original={original or '?'} resolved={converted.get('title')} confidence={confidence}",
            "CONVERT_UPDATE",
        )
        return converted, None

    def _prepare_operations_for_apply(
        self,
        operations: List[Dict[str, Any]],
        active_pool: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        prepared: List[Dict[str, Any]] = []
        ignored: List[Dict[str, Any]] = []

        for raw_operation in operations:
            operation = self._normalize_operation(raw_operation)
            inferred_anchor = self._infer_anchor_from_user_message(operation, active_pool)
            if inferred_anchor:
                operation["anchor_relation"] = inferred_anchor
                operation["timing_mode"] = TimingMode.RELATIVE
                operation["fixed_start"] = None
                operation["fixed_end"] = None

            guarded_operation, duplicate_guard_ignored = self._apply_duplicate_guard_to_add(operation, active_pool)
            if duplicate_guard_ignored:
                ignored.append(duplicate_guard_ignored)
                continue
            operation = guarded_operation or operation

            target_reference = operation.get("target_id") or operation.get("target_title")
            resolution = self._resolve_activity_reference(target_reference, active_pool) if target_reference else {"status": "missing"}
            target = resolution.get("activity") if resolution.get("status") == "resolved" else None
            if target:
                original_target = self._operation_original_target(operation)
                operation["original_user_target"] = original_target
                operation["resolved_target_title"] = target.get("title")
                operation["target_resolution_confidence"] = resolution.get("target_resolution_confidence")
            has_anchor = bool(operation.get("anchor_relation"))
            explicit_fixed = operation.get("fixed_start") is not None or operation.get("fixed_end") is not None
            message = operation.get("_user_message") or ""
            explicitly_requested_target = (
                self._operation_targets_requested_activity(operation, resolution, message)
                if target and message
                else False
            )

            if (
                target
                and operation.get("op") in {"update", "move", "replace", "update_priority"}
                and explicit_fixed
                and message
                and not explicitly_requested_target
            ):
                ignored_item = {
                    "operation": operation,
                    "target": target.get("title"),
                    "reason": "unrequested_fixed_event_mutation",
                }
                ignored.append(ignored_item)
                jlog(
                    "STATE",
                    f"Ignored unrequested operation target={target.get('title')} reason=unrequested_fixed_event_mutation",
                    "OP_FILTER",
                )
                continue
            elif target and explicit_fixed and message and explicitly_requested_target:
                if (
                    operation.get("target_resolution_confidence") == "high"
                    and clean_title(operation.get("title") or "") != clean_title(target.get("title") or "")
                ):
                    operation["_preserve_resolved_title"] = True
                jlog(
                    "STATE",
                    (
                        "allowed resolved requested target "
                        f"original={operation.get('original_user_target')} resolved={target.get('title')}"
                    ),
                    "OP_FILTER",
                )

            if (
                target
                and has_anchor
                and not explicit_fixed
                and target.get("timing_mode") == TimingMode.FIXED
                and message
                and not explicitly_requested_target
            ):
                ignored_item = {
                    "operation": operation,
                    "target": target.get("title"),
                    "reason": "unrequested_fixed_event_mutation",
                }
                ignored.append(ignored_item)
                jlog(
                    "STATE",
                    f"Ignored unrequested operation target={target.get('title')} reason=unrequested_fixed_event_mutation",
                    "OP_FILTER",
                )
                continue
            elif target and has_anchor and not explicit_fixed and message and explicitly_requested_target:
                if (
                    operation.get("target_resolution_confidence") == "high"
                    and clean_title(operation.get("title") or "") != clean_title(target.get("title") or "")
                ):
                    operation["_preserve_resolved_title"] = True
                jlog(
                    "STATE",
                    (
                        "allowed resolved requested target "
                        f"original={operation.get('original_user_target')} resolved={target.get('title')}"
                    ),
                    "OP_FILTER",
                )

            prepared.append(operation)

        return prepared, ignored

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

    def _anchor_resolution_failure(
        self,
        operation: Dict[str, Any],
        active_pool: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        anchor = operation.get("anchor_relation") or {}
        if not anchor:
            return None
        anchor_reference = (
            anchor.get("target_activity_id")
            or anchor.get("target_id")
            or anchor.get("target_title")
        )
        if not anchor_reference:
            return None
        resolution = self._resolve_activity_reference(anchor_reference, active_pool)
        if resolution.get("status") == "resolved":
            return None
        return {
            "status": "unresolved_anchor",
            "anchor_reference": anchor_reference,
            "anchor_resolution": resolution,
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
            if jplan_verbose_enabled():
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
            if jplan_verbose_enabled():
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

        confidence = self._resolution_confidence(best_score)
        self._debug(f"[STATE] Resolved target '{reference}' -> activity_id={best_activity.get('stable_activity_id')}")
        return {
            "status": "resolved",
            "activity": best_activity,
            "original_user_target": str(reference),
            "resolved_target_title": best_activity.get("title"),
            "target_resolution_confidence": confidence,
            "score": best_score,
        }

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

        priority_update_only = bool(operation.get("priority_update_only")) and operation.get("priority") is not None
        preserve_location_metadata = (
            bool(operation.get("preserve_existing_fields"))
            and not self._operation_has_explicit_location_change(operation)
        )
        preserved_location_fields = {
            field: deepcopy(existing.get(field))
            for field in self._location_field_names()
            if field in existing
        }
        if preserve_location_metadata and self._operation_has_location_payload(operation):
            kept_label = (
                existing.get("location_label")
                or existing.get("location")
                or existing.get("location_normalized")
                or "(none)"
            )
            jlog(
                "STATE",
                (
                    f"title={existing.get('title')} kept={kept_label} "
                    f"ignored_parser_location={self._parser_location_label_for_log(operation)}"
                ),
                "PRESERVE_LOCATION",
            )

        for field in ("title", "priority", "location", "notes", "edit_reason", "sequence_index"):
            if field == "title" and (operation.get("_preserve_resolved_title") or priority_update_only):
                continue
            if field == "location" and preserve_location_metadata:
                continue
            if operation.get(field) is not None:
                if field == "priority":
                    old_priority = updated.get("priority") or "medium"
                    new_priority = str(operation.get(field) or old_priority).lower()
                    jlog(
                        "STATE",
                        f"target={updated.get('title')} old_priority={old_priority} new_priority={new_priority}",
                        "PRIORITY_UPDATE",
                    )
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
        preferred_adjustment = clean_title(operation.get("preferred_adjustment") or operation.get("move_direction") or "")

        if explicit_anchor:
            resolved_anchor = self._resolve_anchor_relation(explicit_anchor, anchor_pool)
            if resolved_anchor:
                updated["anchor_relation"] = resolved_anchor
            updated["timing_mode"] = TimingMode.RELATIVE
            updated["original_timing_mode"] = updated.get("original_timing_mode") or TimingMode.RELATIVE
            updated["is_user_fixed"] = False
            updated["user_fixed_start"] = None
            updated["can_move_for_repair"] = False
            updated["fixed_start"] = None
            updated["fixed_end"] = None
            updated["scheduled_start"] = None
            updated["scheduled_end"] = None
            if updated.get("location") is None and resolved_anchor and self._should_inherit_anchor_location(operation):
                anchor_activity = self._find_activity_by_stable_id(anchor_pool, resolved_anchor.get("target_activity_id"))
                if anchor_activity:
                    self._copy_anchor_location_to_operation(updated, anchor_activity)
        elif explicit_fixed_start is not None or explicit_fixed_end is not None:
            if explicit_fixed_start is not None:
                updated["requested_fixed_start"] = explicit_fixed_start
                updated["user_fixed_start"] = explicit_fixed_start
            updated["timing_mode"] = TimingMode.FIXED
            updated["original_timing_mode"] = TimingMode.FIXED
            updated["is_user_fixed"] = True
            updated["is_system_scheduled"] = False
            updated["can_move_for_repair"] = False
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
        elif preferred_adjustment in {"earlier", "later"}:
            current_start = updated.get("scheduled_start") or updated.get("preferred_start") or updated.get("fixed_start")
            if current_start is not None:
                delta = -30 if preferred_adjustment == "earlier" else 30
                updated["preferred_start"] = max(DEFAULT_DAY_START, min(DEFAULT_DAY_END - int(updated.get("duration_minutes") or DEFAULT_DURATION), current_start + delta))
                updated["timing_mode"] = TimingMode.PREFERRED
                updated["fixed_start"] = None
                updated["fixed_end"] = None
                updated["scheduled_start"] = None
                updated["scheduled_end"] = None
                updated["preserve_scheduled_time"] = False
                updated["is_user_fixed"] = False
                updated["user_fixed_start"] = None
                updated["can_move_for_repair"] = True
                updated["preferred_adjustment"] = preferred_adjustment
                updated["move_direction"] = preferred_adjustment

        if priority_update_only:
            updated["priority_update_only"] = True
            updated["priority_direction"] = operation.get("priority_direction")

        if preserve_location_metadata:
            self._restore_location_fields(updated, preserved_location_fields)
        elif self._operation_has_explicit_location_change(operation):
            self._copy_operation_location_fields(updated, operation)

        if updated.get("location") and not updated.get("location_normalized"):
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
        blocker_names = [str(item.get("title")) for item in blockers if item and item.get("title")]
        reason = activity.get("conflict_reason") or "Requested change conflicts with the current locked plan."
        if blocker_names:
            reason = f"{activity.get('title')} overlaps with {', '.join(blocker_names)}."
        suggestions: List[str] = []

        if blocker:
            self._debug(
                f"[CONFLICT] Requested {activity.get('title')} overlaps with fixed {blocker.get('title')}"
            )
            next_start = max(
                item.get("scheduled_end") or 0
                for item in blockers
                if item and item.get("scheduled_end") is not None
            )
            transition = max(self._transition_minutes(item, activity, 0) for item in blockers if item) if blockers else 0
            if next_start:
                suggestions.append(f"Move {activity.get('title')} to {format_clock(next_start + transition)}")
            existing_fixed = self._coerce_minutes(activity.get("fixed_start"))
            if existing_fixed is not None:
                suggestions.append(f"Keep {activity.get('title')} at {format_clock(existing_fixed)}")
            flexible_blockers = [
                item.get("title")
                for item in blockers
                if item and item.get("timing_mode") != TimingMode.FIXED
            ]
            if flexible_blockers:
                suggestions.append(f"Move {', '.join(flexible_blockers)} later or slightly earlier if feasible")
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

    def _attempt_flexible_edit_repair(
        self,
        active_set: List[Dict[str, Any]],
        mutated_ids: set[str],
        planned_result: Dict[str, Any],
        preferences: Dict[str, Any],
        schedule_date: str,
    ) -> Optional[Dict[str, Any]]:
        if len(mutated_ids) != 1:
            return None
        mutated_id = next(iter(mutated_ids))
        planned_by_id = {
            item.get("stable_activity_id"): item
            for item in planned_result.get("activities", [])
            if item.get("stable_activity_id")
        }
        requested = planned_by_id.get(mutated_id)
        requested_source = next(
            (item for item in active_set if item.get("stable_activity_id") == mutated_id),
            requested,
        )
        requested_time = (
            requested_source.get("requested_fixed_start")
            or requested_source.get("fixed_start")
            or requested_source.get("preferred_start")
            or requested.get("fixed_start")
            or requested.get("preferred_start")
            or requested.get("scheduled_start")
            if requested and requested_source else None
        )
        if not requested or requested_time is None:
            return None
        blockers = [
            planned_by_id.get(blocker_id)
            for blocker_id in requested.get("conflict_with") or []
            if planned_by_id.get(blocker_id)
        ]
        movable_blockers = [
            item for item in blockers
            if item.get("timing_mode") != TimingMode.FIXED and item.get("stable_activity_id") not in mutated_ids
        ]
        if not movable_blockers:
            return None

        jlog(
            "REPAIR",
            (
                f"requested={requested.get('title')} at {format_clock(requested_time)} "
                f"blocker={movable_blockers[0].get('title')} attempt=move_flexible"
            ),
            None,
        )
        repaired_active = []
        for item in deepcopy(active_set):
            if item.get("stable_activity_id") in mutated_ids:
                if item.get("timing_mode") != TimingMode.FIXED:
                    if item.get("requested_fixed_start") is not None:
                        requested_start = item.get("requested_fixed_start")
                        item["timing_mode"] = TimingMode.FIXED
                        item["fixed_start"] = requested_start
                        item["fixed_end"] = requested_start + int(item.get("duration_minutes") or DEFAULT_DURATION)
                        item["earliest_start"] = item["fixed_start"]
                        item["latest_end"] = item["fixed_end"]
                    item.pop("preserve_scheduled_time", None)
                    item.pop("scheduled_start", None)
                    item.pop("scheduled_end", None)
                repaired_active.append(item)
                continue
            if item.get("timing_mode") != TimingMode.FIXED:
                item.pop("preserve_scheduled_time", None)
                item.pop("scheduled_start", None)
                item.pop("scheduled_end", None)
            repaired_active.append(item)

        repaired = self._plan_schedule(schedule_date, repaired_active, preferences)
        repaired_conflicts = self._build_conflicts(repaired.get("activities", []), set())
        blocking = [
            conflict for conflict in repaired_conflicts
            if any(str(activity_id) in mutated_ids for activity_id in conflict.get("activity_ids") or [])
        ]
        if blocking:
            return None
        repaired["repair_applied"] = True
        return repaired

    def _operation_batch_source_text(
        self,
        operations: List[Dict[str, Any]],
        preferences: Dict[str, Any],
        envelope: Dict[str, Any],
    ) -> str:
        for operation in operations or []:
            if not isinstance(operation, dict):
                continue
            for key in ("_latest_request", "_source_text", "_user_message", "_transcription"):
                value = str(operation.get(key) or "").strip()
                if value:
                    return value
        for source in (preferences, (envelope or {}).get("preferences") or {}, envelope or {}):
            if not isinstance(source, dict):
                continue
            for key in ("latest_request", "original_request", "transcription", "request_text"):
                value = str(source.get(key) or "").strip()
                if value:
                    return value
        return ""

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
        removed_activities: List[Dict[str, Any]] = []
        deleted_activity_ids: List[str] = []
        mutated_ids: set[str] = set()
        soft_adjustments: List[Dict[str, Any]] = []
        priority_noops: List[Dict[str, Any]] = []
        source_plan_date = working_envelope.get("date")
        existing_conflict_identities = self._existing_conflict_identities(working_envelope)
        postconditions: List[Dict[str, Any]] = []
        active_pool_for_prepare = [item for item in canonical_activities if item.get("status") == "active"]
        sanitized_operations = self._sanitize_operations(operations)
        latest_operation_message = self._operation_batch_source_text(
            sanitized_operations,
            preferences,
            working_envelope,
        )
        if latest_operation_message:
            normalized_for_apply: List[Dict[str, Any]] = []
            operation_scopes = self._build_activity_location_scopes(
                sanitized_operations,
                latest_operation_message,
            )
            for index, operation in enumerate(sanitized_operations):
                normalized_operation = self._normalize_soft_timing_preferences(
                    operation,
                    latest_operation_message,
                    operation_scopes.get(index),
                )
                normalized_operation = self._apply_semantic_location_payload(
                    normalized_operation,
                    latest_operation_message,
                    operation_scopes.get(index),
                    saved_locations or [],
                )
                if normalized_operation.get("location_status") and "raw_llm_location" in normalized_operation:
                    self._enrich_existing_location_payload(
                        normalized_operation,
                        latest_operation_message,
                        operation_scopes.get(index),
                    )
                normalized_for_apply.append(normalized_operation)
            sanitized_operations = normalized_for_apply
        sanitized_operations = self._apply_implicit_lunch_handling(
            sanitized_operations,
            latest_operation_message,
            active_pool_for_prepare,
            preferences,
        )
        self._log_location_and_timing_summary(sanitized_operations, include_locations=False)
        self._log_normalized_operations(sanitized_operations)
        operations_to_apply, ignored_operations = self._prepare_operations_for_apply(
            sanitized_operations,
            active_pool_for_prepare,
        )
        self._configure_module_d_run_policy(
            preferences,
            working_envelope,
            {"operations": operations_to_apply},
            operations_to_apply,
            str((operations_to_apply[0] if operations_to_apply else {}).get("_user_message") or ""),
            is_apply_operations=True,
        )
        if not operations_to_apply and ignored_operations:
            duplicate_guard_ignored = next(
                (item for item in ignored_operations if item.get("reason") == "duplicate_guard_ambiguous_target"),
                None,
            )
            reply_reason = "All parser operations were ignored because they targeted unrequested fixed events."
            reply_text = None
            if duplicate_guard_ignored:
                candidates = duplicate_guard_ignored.get("candidates") or []
                candidate_text = ", ".join(str(candidate) for candidate in candidates) or "multiple existing activities"
                reply_reason = "duplicate_guard_ambiguous_target"
                reply_text = f"Which activity did you mean: {candidate_text}?"
            return {
                "status": "no_operation",
                "applied": False,
                "envelope": working_envelope,
                "version": current_version,
                "activities": working_envelope.get("activities", []),
                "updatedActivities": [],
                "deletedItemIds": [],
                "ignored_operations": ignored_operations,
                "rejected_changes": [],
                "reply_reason": reply_reason,
                "reply": reply_text,
            }
        if len(operations_to_apply) == 1:
            satisfied = self._relative_order_already_satisfied(
                operations_to_apply[0],
                [item for item in canonical_activities if item.get("status") == "active"],
            )
            if satisfied:
                return {
                    "status": "no_operation",
                    "applied": False,
                    "envelope": working_envelope,
                    "version": current_version,
                    "activities": working_envelope.get("activities", []),
                    "updatedActivities": [],
                    "deletedItemIds": [],
                    "ignored_operations": ignored_operations,
                    "rejected_changes": [],
                    "reply_reason": "already_satisfied",
                    "reply": (
                        f"{satisfied['target']} is already scheduled {satisfied['kind']} "
                        f"{satisfied['anchor']}."
                    ),
                }
        explicit_shift_requested = any(
            operation.get("op") == "shift_plan_date"
            for operation in operations_to_apply
        )
        explicit_optimize_requested = any(
            operation.get("op") == "optimize_schedule"
            for operation in operations_to_apply
        )

        for operation in operations_to_apply:
            op_type = operation.get("op")
            active_pool = [item for item in canonical_activities if item.get("status") == "active"]

            if op_type == "optimize_schedule":
                jlog("MODULE_D", "explicit optimize request accepted for refinement pass", "REQUEST")
                continue

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
                        if not operation.get("location") and self._should_inherit_anchor_location(operation):
                            anchor_activity = self._find_activity_by_stable_id(active_pool, resolved_anchor.get("target_activity_id"))
                            if anchor_activity:
                                self._copy_anchor_location_to_operation(operation, anchor_activity)
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
                if jplan_verbose_enabled():
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

            anchor_failure = self._anchor_resolution_failure(operation, active_pool)
            if anchor_failure:
                return {
                    "status": "clarification_needed",
                    "applied": False,
                    "envelope": working_envelope,
                    "version": current_version,
                    "activities": working_envelope.get("activities", []),
                    "updatedActivities": [],
                    "deletedItemIds": [],
                    "target_resolution": resolution,
                    "anchor_resolution": anchor_failure,
                    "reply": f"Which {anchor_failure['anchor_reference']} do you mean?",
                }

            if operation.get("priority_update_only") and operation.get("priority") is not None:
                old_priority = clean_title(target.get("priority") or "medium")
                new_priority = clean_title(operation.get("priority") or old_priority)
                if old_priority == new_priority:
                    priority_noops.append({
                        "activity_id": target_id,
                        "title": target.get("title"),
                        "priority": new_priority,
                        "direction": operation.get("priority_direction"),
                    })
                    jlog(
                        "STATE",
                        f"target={target.get('title')} old_priority={old_priority} new_priority={new_priority} already_set=true",
                        "PRIORITY_UPDATE",
                    )
                    continue

            if op_type == "remove":
                removed = deepcopy(canonical_activities[target_index])
                canonical_activities[target_index]["status"] = "removed"
                canonical_activities[target_index]["updated_at"] = self._now_iso()
                canonical_activities[target_index]["source_turn"] = source_turn
                deleted_activity_ids.append(target_id)
                mutated_ids.add(target_id)
                updated_activities.append(canonical_activities[target_index])
                removed_activities.append(self._format_activity(canonical_activities[target_index]))
                self._debug(f"[STATE] Marked activity '{removed.get('title')}' as removed")
                continue

            if clean_title(operation.get("preferred_adjustment") or operation.get("move_direction") or "") in {"earlier", "later"}:
                soft_adjustments.append({
                    "activity_id": target_id,
                    "title": target.get("title"),
                    "direction": clean_title(operation.get("preferred_adjustment") or operation.get("move_direction")),
                    "original_start": target.get("scheduled_start"),
                })

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

        if priority_noops and not mutated_ids and not deleted_activity_ids and not updated_activities:
            noop = priority_noops[0]
            reply = f"{noop.get('title') or 'That activity'} is already set to {noop.get('priority') or 'that'} priority."
            return {
                "status": "no_operation",
                "applied": False,
                "envelope": working_envelope,
                "version": current_version,
                "activities": working_envelope.get("activities", []),
                "updatedActivities": [],
                "deletedItemIds": [],
                "ignored_operations": ignored_operations,
                "rejected_changes": [],
                "reply_reason": "priority_already_set",
                "reply": reply,
                "priority_noop": noop,
            }

        active_set = [
            deepcopy(item)
            for item in canonical_activities
            if item.get("status") == "active"
        ]
        priority_only_mutated_ids = {
            str(item.get("stable_activity_id"))
            for item in canonical_activities
            if item.get("stable_activity_id") in mutated_ids and item.get("priority_update_only")
        }

        for item in active_set:
            item["locked_fixed"] = (
                item.get("timing_mode") == TimingMode.FIXED
                and item.get("is_mandatory")
                and item.get("stable_activity_id") not in mutated_ids
            )
            if (
                (item.get("stable_activity_id") not in mutated_ids or str(item.get("stable_activity_id")) in priority_only_mutated_ids)
                and item.get("scheduled_start") is not None
                and item.get("scheduled_end") is not None
                and not item["locked_fixed"]
                and not explicit_optimize_requested
            ):
                item["preserve_scheduled_time"] = True
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
        is_initial_generation_mode = preferences.get("refinement_reason") == "initial_generation"
        if (
            explicit_optimize_requested
            and not planned_result.get("refinement_applied")
            and not mutated_ids
            and not deleted_activity_ids
            and not updated_activities
        ):
            return {
                "status": "no_operation",
                "applied": False,
                "envelope": working_envelope,
                "planned_result": planned_result,
                "version": current_version,
                "activities": working_envelope.get("activities", []),
                "updatedActivities": [],
                "deletedItemIds": [],
                "ignored_operations": ignored_operations,
                "rejected_changes": [],
                "reply_reason": "no_safe_refinement",
                "reply": "I checked your schedule, but did not find a safe optimization to apply.",
            }
        forced_conflict_ids = mutated_ids if (allow_clash and not is_initial_generation_mode) else set()
        conflicts = self._build_conflicts(planned_result["activities"], forced_conflict_ids)
        for conflict in conflicts:
            identity = self._conflict_identity(conflict)
            conflict["conflict_lifecycle"] = "existing" if identity in existing_conflict_identities else "new"

        if conflicts and not allow_clash and not is_initial_generation_mode:
            repaired_result = self._attempt_flexible_edit_repair(
                active_set,
                mutated_ids,
                planned_result,
                preferences,
                schedule_date,
            )
            if repaired_result:
                planned_result = repaired_result
                conflicts = self._build_conflicts(planned_result["activities"], set())
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
        if postcondition_failures and not allow_clash and not is_initial_generation_mode:
            self._debug(f"[CONFLICT] Requested ordering was not satisfied: {postcondition_failures[0].get('reason')}")
            response = self._build_postcondition_response(working_envelope, current_version, postcondition_failures, allow_clash=allow_clash)
            response["ignored_operations"] = ignored_operations
            return response

        blocking_conflicts = [
            conflict for conflict in conflicts
            if any(str(activity_id) in mutated_ids for activity_id in conflict.get("activity_ids") or [])
        ]
        if blocking_conflicts and not allow_clash and not is_initial_generation_mode:
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
                    response = self._build_conflict_response(working_envelope, current_version, planned_activity, blockers, allow_clash=allow_clash)
                    response["ignored_operations"] = ignored_operations
                    return response

        unchanged_soft_adjustments = [
            adjustment for adjustment in soft_adjustments
            if planned_by_id.get(adjustment.get("activity_id"))
            and planned_by_id[adjustment["activity_id"]].get("scheduled_start") == adjustment.get("original_start")
        ]
        if soft_adjustments and len(unchanged_soft_adjustments) == len(soft_adjustments):
            direction = soft_adjustments[0].get("direction") or "earlier"
            title = soft_adjustments[0].get("title") or "that activity"
            response = {
                "status": "no_operation",
                "applied": False,
                "envelope": working_envelope,
                "version": current_version,
                "activities": working_envelope.get("activities", []),
                "updatedActivities": [],
                "deletedItemIds": [],
                "ignored_operations": ignored_operations,
                "rejected_changes": [],
                "reply_reason": "soft_adjustment_no_change",
                "reply": f"I couldn't move {title} {direction} without changing the current constraints.",
            }
            return response

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
        envelope_status = "partial" if (conflicts or postcondition_failures or formatted_unscheduled) else ("warning" if warnings else "ok")
        has_only_nonblocking_existing_conflicts = (
            bool(conflicts)
            and not blocking_conflicts
            and not postcondition_failures
            and not formatted_unscheduled
            and not is_initial_generation_mode
        )
        if allow_clash and conflicts and not is_initial_generation_mode:
            result_status = "success"
        elif has_only_nonblocking_existing_conflicts:
            result_status = "success"
        else:
            result_status = "warning" if envelope_status in {"warning", "partial"} else "success"

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
        updated_envelope["ignored_operations"] = ignored_operations
        updated_envelope["unmet_items"] = formatted_unscheduled
        updated_envelope["unmet_optional"] = formatted_unscheduled
        updated_envelope["validation_issues"] = [failure.get("reason") for failure in postcondition_failures if failure.get("reason")]
        updated_envelope["conflict"] = conflicts[0] if conflicts else ({"type": "postcondition_failed", **postcondition_failures[0]} if postcondition_failures else None)
        updated_envelope["postcondition_results"] = postcondition_failures
        for key in REFINEMENT_META_KEYS:
            updated_envelope[key] = planned_result.get(key)
        updated_envelope = self._apply_accurate_travel_if_requested(updated_envelope, saved_locations or [])
        if deleted_activity_ids:
            updated_envelope["schedule_blocks"] = self._prune_support_blocks_for_deleted_activities(
                updated_envelope.get("schedule_blocks") or [],
                deleted_activity_ids,
            )
        envelope_status = updated_envelope.get("status") or envelope_status
        if allow_clash and conflicts and not is_initial_generation_mode:
            result_status = "success"
        elif has_only_nonblocking_existing_conflicts:
            result_status = "success"
        else:
            result_status = (
                "warning"
                if envelope_status in {"warning", "partial"}
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
            "removedActivities": removed_activities,
            "deletedItemIds": deleted_activity_ids,
            "applied_changes": final_updated_activities,
            "accepted_with_warnings": updated_envelope["accepted_with_warnings"],
            "rejected_changes": [],
            "ignored_operations": ignored_operations,
            "warnings": warnings,
        }

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
        preferred_window_start = item.get("preferred_window_start")
        preferred_window_end = item.get("preferred_window_end")
        location = clean_optional_text(item.get("location"))
        raw_travel_required = item.get("travel_required", True)
        if isinstance(raw_travel_required, str):
            travel_required = raw_travel_required.strip().lower() not in {"0", "false", "no", "off"}
        else:
            travel_required = bool(raw_travel_required)

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
            "raw_location_text": item.get("raw_location_text"),
            "location_kind": item.get("location_kind"),
            "location_resolution_status": item.get("location_resolution_status"),
            "no_location_reason": item.get("no_location_reason"),
            "semantic_confidence": item.get("semantic_confidence"),
            "needs_clarification": bool(item.get("needs_clarification", False)),
            "parse_notes": item.get("parse_notes"),
            "saved_location_label": item.get("saved_location_label"),
            "resolved_location": deepcopy(item.get("resolved_location")) if isinstance(item.get("resolved_location"), dict) else None,
            "raw_llm_location": item.get("raw_llm_location"),
            "explicit_user_location": bool(item.get("explicit_user_location", False)),
            "location_warning": item.get("location_warning"),
            "area_preference": item.get("area_preference"),
            "same_location_as": item.get("same_location_as"),
            "inherited_from_activity_id": item.get("inherited_from_activity_id"),
            "travel_required": travel_required,
            "duration": item.get("duration") or self._duration_label(item.get("duration_minutes") or (s_end_val - s_start_val)),
            "duration_minutes": int(item.get("duration_minutes") or (s_end_val - s_start_val)),
            "priority": item.get("priority", "medium"),
            "isMandatory": bool(item.get("is_mandatory", True) if "is_mandatory" in item else item.get("isMandatory", True)),
            "timing_mode": item.get("timing_mode") or item.get("timingMode") or TimingMode.UNSPECIFIED,
            "original_timing_mode": item.get("original_timing_mode") or item.get("originalTimingMode") or item.get("timing_mode") or TimingMode.UNSPECIFIED,
            "is_user_fixed": bool(item.get("is_user_fixed", False)),
            "is_system_scheduled": bool(item.get("is_system_scheduled", item.get("scheduled_start") is not None and not item.get("is_user_fixed", False))),
            "user_fixed_start": item.get("user_fixed_start"),
            "can_move_for_repair": bool(item.get("can_move_for_repair", not (item.get("is_user_fixed") or item.get("timing_mode") == TimingMode.FIXED or item.get("fixed_start") is not None))),
            "repair_protection": item.get("repair_protection") or item.get("repairProtection") or "flexible",
            "fixed_start": fixed_start,
            "fixed_end": fixed_end,
            "earliest_start": earliest_start,
            "latest_end": latest_end,
            "preferred_start": preferred_start,
            "preferred_time_window": item.get("preferred_time_window"),
            "preferred_window_start": preferred_window_start,
            "preferred_window_end": preferred_window_end,
            "preferred_order": deepcopy(item.get("preferred_order")) if isinstance(item.get("preferred_order"), dict) else None,
            "preferred_orders": deepcopy(item.get("preferred_orders")) if isinstance(item.get("preferred_orders"), list) else [],
            "soft_dependency": bool(item.get("soft_dependency", False)),
            "requested_fixed_start": item.get("requested_fixed_start"),
            "preferred_adjustment": item.get("preferred_adjustment"),
            "move_direction": item.get("move_direction"),
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
            "implicit_activity": bool(item.get("implicit_activity", False)),
            "implicit_reason": item.get("implicit_reason"),
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
                int(current.get("prep_buffer", DEFAULT_PREP_BUFFER) or 0),
                int(nxt.get("prep_buffer", DEFAULT_PREP_BUFFER) or 0),
            )
            travel_minutes = estimate_travel_minutes(current.get("location"), nxt.get("location"))
            source_activity_id = current.get("stable_activity_id") or current.get("id")
            destination_activity_id = nxt.get("stable_activity_id") or nxt.get("id")
            support_links = {
                "source_activity_id": source_activity_id,
                "destination_activity_id": destination_activity_id,
                "related_activity_ids": [
                    activity_id
                    for activity_id in (source_activity_id, destination_activity_id)
                    if activity_id
                ],
            }

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
                    **support_links,
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
                    **support_links,
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

