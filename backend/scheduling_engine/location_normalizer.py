import json
import re
import time
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4
from zoneinfo import ZoneInfo

from jplan_logging import jjson, jlog, jlog_verbose, jsection
from travel_service import MissingORSApiKey, TravelService, TravelServiceError, coordinate_from_saved_location
from .types_utils import *
from .types_utils import _normalize_location

class LocationNormalizerMixin:
    def _normalize_parsed_locations(
        self,
        parsed: Dict[str, Any],
        latest_request: str,
        saved_locations: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        normalized = deepcopy(parsed)
        transcription = normalized.get("transcription") or latest_request
        request_text = latest_request or transcription or ""
        normalized["operations"] = self._normalize_operation_locations(
            normalized.get("operations") or [],
            request_text,
            transcription,
            saved_locations or [],
        )
        normalized["activities"] = self._normalize_operation_locations(
            normalized.get("activities") or [],
            request_text,
            transcription,
            saved_locations or [],
        )
        self._log_location_and_timing_summary(
            list(normalized.get("operations") or []) + list(normalized.get("activities") or [])
        )
        return normalized

    def _log_location_and_timing_summary(
        self,
        operations: List[Dict[str, Any]],
        *,
        include_locations: bool = True,
    ) -> None:
        location_lines: List[str] = []
        timing_lines: List[str] = []
        for operation in operations or []:
            if not isinstance(operation, dict):
                continue
            title = str(operation.get("title") or operation.get("target_title") or operation.get("op") or "Activity")
            if clean_title(operation.get("op") or "add") in {"remove", "shift_plan_date"}:
                continue
            if include_locations:
                location_lines.append(self._location_summary_line(title, operation))
            timing_line = self._timing_summary_line(title, operation)
            if timing_line:
                timing_lines.append(timing_line)
        if location_lines:
            jlog("LOCATION", "\n".join(location_lines), "SUMMARY")
        if timing_lines:
            jlog("SOFT_PREF", "\n".join(timing_lines), "SUMMARY")

    def _location_summary_line(self, title: str, operation: Dict[str, Any]) -> str:
        semantic_status = clean_title(
            operation.get("location_resolution_status")
            or operation.get("location_status")
            or ""
        )
        if (
            operation.get("travel_required") is False
            or semantic_status == "not_required"
            or clean_title(operation.get("location_category") or "") == "home_or_online"
        ):
            return f"{title} -> no location required"
        label = (
            operation.get("location_label")
            or operation.get("location")
            or operation.get("location_normalized")
            or operation.get("location_category")
            or "exact location"
        )
        if self._operation_has_coordinates(operation):
            return f"{title} -> {label} (coordinates confirmed)"
        if semantic_status == "resolved_coordinates":
            return f"{title} -> {label} (resolved)"
        if semantic_status in {"needs_resolution", "unresolved", "resolved_default", "needs_coordinates", "ambiguous"}:
            return f"{title} -> {label} (needs coordinates)"
        return f"{title} -> {label} (needs coordinates)"

    def _operation_has_coordinates(self, operation: Dict[str, Any]) -> bool:
        resolved = operation.get("resolved_location")
        if isinstance(resolved, dict):
            if resolved.get("latitude") is not None and resolved.get("longitude") is not None:
                return True
            if resolved.get("lat") is not None and resolved.get("lng") is not None:
                return True
        return (
            operation.get("latitude") is not None
            and operation.get("longitude") is not None
        ) or (
            operation.get("lat") is not None
            and operation.get("lng") is not None
        )

    def _operation_has_semantic_location_fields(self, operation: Dict[str, Any]) -> bool:
        semantic_fields = {
            "raw_location_text",
            "location_kind",
            "location_resolution_status",
            "no_location_reason",
            "semantic_confidence",
            "location_confidence",
            "needs_clarification",
            "parse_notes",
        }
        return any(field in operation for field in semantic_fields)

    def _clean_location_kind(self, value: Any) -> str:
        kind = clean_title(value or "").replace(" ", "_").replace("-", "_")
        aliases = {
            "exact_place": "exact_named_place",
            "named_place": "exact_named_place",
            "area": "area_only",
            "category": "category_only",
            "physical_unknown": "unknown_physical",
            "none": "no_location_required",
            "no_location": "no_location_required",
            "not_required": "no_location_required",
            "virtual": "online",
            "remote": "online",
        }
        return aliases.get(kind, kind)

    def _clean_location_resolution_status(self, value: Any) -> str:
        status = clean_title(value or "").replace(" ", "_").replace("-", "_")
        aliases = {
            "needs_resolution": "needs_coordinates",
            "missing_coordinates": "needs_coordinates",
            "unresolved": "needs_coordinates",
            "fallback_used": "needs_coordinates",
            "resolved": "resolved_coordinates",
            "not_required": "not_required",
            "no_location_required": "not_required",
        }
        return aliases.get(status, status)

    def _normalize_semantic_category(self, value: Any) -> str:
        category = clean_title(value or "").replace(" ", "_").replace("-", "_")
        aliases = {
            "hospital": "medical",
            "clinic": "medical",
            "doctor": "medical",
            "restaurant": "meal_place",
            "meal": "meal_place",
            "food": "meal_place",
            "grocery": "supermarket",
            "groceries": "supermarket",
            "store": "supermarket",
            "fitness": "fitness_center",
            "gym": "fitness_center",
            "bank": "bank",
            "banking": "bank",
            "home_or_online": "home_or_online",
            "online": "home_or_online",
            "none": "no_location",
            "no_location_required": "no_location",
        }
        return aliases.get(category, category or "unknown")

    def _is_home_location_text(self, value: Any) -> bool:
        return clean_title(value or "") in {"home", "at home", "my home", "my house", "house"}

    def _is_online_location_text(self, value: Any) -> bool:
        return clean_title(value or "") in {"online", "virtual", "remote", "zoom", "teams", "google meet"}

    def _is_non_home_explicit_text(self, value: Any) -> bool:
        text = clean_title(value or "")
        return bool(text and text not in NULL_TEXT_VALUES and not self._is_home_location_text(text) and not self._is_online_location_text(text))

    def _semantic_travel_required(self, operation: Dict[str, Any], category: str, kind: str) -> bool:
        raw_text = operation.get("raw_location_text") or operation.get("raw_llm_location") or operation.get("location")
        if operation.get("explicit_user_location") and self._is_non_home_explicit_text(raw_text):
            return True
        raw_value = operation.get("travel_required")
        if isinstance(raw_value, str):
            lowered = raw_value.strip().lower()
            if lowered in {"0", "false", "no", "off"}:
                raw_value = False
            elif lowered in {"1", "true", "yes", "on"}:
                raw_value = True
        travel_required = bool(raw_value) if raw_value is not None else True
        if kind in {"no_location_required", "online"} or category in {"home_or_online", "no_location", "none"}:
            return False
        if category in {"medical", "meal_place", "supermarket", "pharmacy", "fitness_center", "bank", "home", "institution", "campus_area", "office", "library"}:
            return True
        return travel_required

    def _semantic_source(self, explicit_user_location: bool, status: str) -> str:
        if explicit_user_location:
            return "explicit_user"
        if status == "not_required":
            return "semantic_parser"
        return "llm_semantic"

    def _semantic_legacy_status(self, status: str, kind: str, has_coordinates: bool) -> str:
        if kind in {"no_location_required", "online"} or status == "not_required":
            return "not_required"
        if has_coordinates or status == "resolved_coordinates":
            return "resolved"
        if status == "ambiguous":
            return "unresolved"
        return "needs_resolution"

    def _generic_explicit_location_from_text(
        self,
        source_text: str,
        title: str,
    ) -> Optional[Dict[str, str]]:
        text = re.sub(r"\s+", " ", source_text or "").strip()
        if not text or not title:
            return None
        if self._is_no_location_required_activity(title, text, None, None):
            return None
        matches: List[Dict[str, str]] = []
        for alias in self._activity_mention_aliases(title):
            if not alias:
                continue
            alias_pattern = r"\b" + r"\s+".join(re.escape(token) for token in alias.split()) + r"\b"
            for title_match in re.finditer(alias_pattern, text, flags=re.IGNORECASE):
                window = text[title_match.end(): title_match.end() + 180]
                location_match = re.search(
                    r"\b(?:at|in|near)\s+([A-Za-z0-9@&.'’/()\- ]{2,80}?)(?=,|;|\.|\band\b|\bthen\b|\bfollowed\b|$)",
                    window,
                    flags=re.IGNORECASE,
                )
                if not location_match:
                    continue
                label = re.sub(r"\s+", " ", location_match.group(1)).strip(" .")
                label = re.sub(
                    r"\s+(?:from|between)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b.*$",
                    "",
                    label,
                    flags=re.IGNORECASE,
                ).strip()
                if not self._generic_location_label_is_plausible(label):
                    continue
                category = "home" if self._is_home_location_text(label) else self._infer_location_category(title, label)
                matches.append({
                    "label": label,
                    "category": category,
                    "kind": "home" if self._is_home_location_text(label) else "exact_named_place",
                })
        if not matches:
            return None
        non_home = [match for match in matches if not self._is_home_location_text(match.get("label"))]
        return non_home[0] if non_home else matches[0]

    def _generic_location_label_is_plausible(self, label: str) -> bool:
        clean = clean_title(label or "")
        if not clean or clean in {"a", "an", "the"}:
            return False
        if re.search(r"\b(?:morning|afternoon|evening|night|day|today|tomorrow|minutes?|minute|hours?|hour)\b", clean):
            return False
        if re.search(r"\b(?:way|rushed|realistic|productive|busy|focused|focus|documents?|assignment|implementation)\b", clean):
            return False
        if re.match(r"^(?:a|an|the)\s+\d", clean):
            return False
        return True

    def _apply_semantic_location_payload(
        self,
        operation: Dict[str, Any],
        source_text: str,
        scoped_evidence: Optional[str],
        saved_locations: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not self._operation_has_semantic_location_fields(operation):
            return operation

        updated = deepcopy(operation)
        title = str(updated.get("title") or updated.get("target_title") or "").strip()
        raw_location_text = clean_optional_text(
            updated.get("raw_location_text")
            or updated.get("raw_llm_location")
            or (updated.get("location") if updated.get("explicit_user_location") else None)
        )
        kind = self._clean_location_kind(updated.get("location_kind"))
        category = self._normalize_semantic_category(updated.get("location_category"))
        status = self._clean_location_resolution_status(updated.get("location_resolution_status") or updated.get("location_status"))
        explicit_user_location = bool(updated.get("explicit_user_location"))
        title_text = clean_title(title)
        no_location_work_default = False

        if re.search(r"\bbank(?:ing)?\b", title_text):
            category = "bank"
            if kind in {"", "unknown", "no_location_required"}:
                kind = "category_only"
            if status in {"", "not_required"}:
                status = "needs_coordinates"

        if self._is_no_location_work_task(title, scoped_evidence or source_text or "", raw_location_text):
            no_location_work_default = True
            updated["can_be_done_at_current_location"] = True
            if self._work_task_needs_quiet_place(title, scoped_evidence or source_text or ""):
                updated["quiet_place_required"] = True
            kind = "no_location_required"
            category = "home_or_online"
            status = "not_required"
            raw_location_text = None
            explicit_user_location = False

        if re.search(r"\b(?:anywhere\s+quiet|quiet\s+(?:place|space|spot))\b", clean_title(f"{title} {scoped_evidence or source_text or ''}")):
            updated["can_be_done_at_current_location"] = True
            updated["quiet_place_required"] = True
            kind = "no_location_required"
            category = "home_or_online"
            status = "not_required"

        generic_explicit = self._generic_explicit_location_from_text(scoped_evidence or source_text or "", title)
        if generic_explicit and not no_location_work_default and (
            not raw_location_text
            or (
                clean_title(raw_location_text) in {"home", "store", "gym", "meal_place", "supermarket", "fitness center"}
                and clean_title(generic_explicit.get("label")) != clean_title(raw_location_text)
            )
        ):
            raw_location_text = generic_explicit.get("label")
            kind = generic_explicit.get("kind") or kind or "exact_named_place"
            if category in {"unknown", "home_or_online", "no_location"}:
                category = self._normalize_semantic_category(generic_explicit.get("category"))
            explicit_user_location = True

        if raw_location_text and self._is_home_location_text(raw_location_text):
            kind = "home"
            category = "home"
            explicit_user_location = True
        elif raw_location_text and self._is_online_location_text(raw_location_text):
            kind = "online"
            category = "home_or_online"
            explicit_user_location = True
        elif self._is_non_home_explicit_text(raw_location_text) and kind in {"home", "online", "no_location_required"}:
            ignored_default = kind
            kind = "exact_named_place"
            if category in {"home", "home_or_online", "no_location"}:
                category = "unknown"
            status = "needs_coordinates"
            explicit_user_location = True
            jlog(
                "LOCATION_NORMALIZER",
                f"title={title} kept=\"{raw_location_text}\" ignored_default=\"{ignored_default}\"",
                "PRESERVE_EXPLICIT",
            )

        travel_required = self._semantic_travel_required(updated, category, kind)
        if explicit_user_location and kind == "no_location_required":
            kind = "exact_named_place"
            travel_required = True
            status = "needs_coordinates"
        if status not in {"resolved_coordinates", "not_required", "ambiguous", "needs_coordinates"}:
            status = "not_required" if not travel_required else "needs_coordinates"
        if not travel_required:
            kind = kind or "no_location_required"
            status = "not_required"
            category = "home_or_online" if kind in {"online", "no_location_required"} else "no_location"

        label: Optional[str]
        if not travel_required:
            label = None
        elif kind == "home":
            label = raw_location_text or updated.get("location") or "home"
        elif kind == "category_only" and not raw_location_text:
            label = None
        else:
            label = raw_location_text or clean_optional_text(updated.get("location"))

        has_coordinates = self._operation_has_coordinates(updated)
        legacy_status = self._semantic_legacy_status(status, kind, has_coordinates)
        if kind == "home":
            legacy_status = "resolved" if has_coordinates else "resolved"
        source = self._semantic_source(explicit_user_location, status)
        if label and kind == "home":
            source = "explicit_user"

        updated["raw_location_text"] = raw_location_text
        updated["raw_llm_location"] = raw_location_text
        updated["location_kind"] = kind or ("no_location_required" if not travel_required else "unknown_physical")
        updated["location_resolution_status"] = status
        updated["location_category"] = category or "unknown"
        updated["location"] = label
        updated["location_label"] = label
        updated["location_normalized"] = _normalize_location(label)
        updated["location_source"] = updated.get("location_source") or source
        updated["location_status"] = legacy_status
        updated["explicit_user_location"] = bool(explicit_user_location)
        updated["travel_required"] = bool(travel_required)
        if status == "ambiguous":
            updated["needs_clarification"] = True
        if legacy_status in {"needs_resolution", "unresolved"} and label:
            updated.pop("location_warning", None)

        jlog(
            "SEMANTIC_PARSE",
            (
                f"title={title} raw=\"{raw_location_text}\" kind={updated.get('location_kind')} "
                f"status={updated.get('location_resolution_status')}"
            ),
            "LOCATION",
        )
        return updated

    def _timing_summary_line(self, title: str, operation: Dict[str, Any]) -> str:
        parts: List[str] = []
        if operation.get("preferred_time_window"):
            parts.append(f"preferred_window={operation.get('preferred_time_window')}")
        preferred_order = operation.get("preferred_order")
        if isinstance(preferred_order, dict):
            kind = preferred_order.get("kind")
            target = preferred_order.get("target_title")
            if kind and target:
                parts.append(f"preferred_order={kind} {target}")
        if not parts:
            return ""
        return f"{title} -> {'; '.join(parts)}"

    def _normalize_operation_locations(
        self,
        operations: List[Dict[str, Any]],
        request_text: str,
        transcription: str,
        saved_locations: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        source_text = transcription or request_text or ""
        location_scopes = self._build_activity_location_scopes(operations, source_text)
        for index, raw in enumerate(operations):
            if not isinstance(raw, dict):
                continue
            operation = deepcopy(raw)
            if clean_title(operation.get("op") or "") in {"remove", "shift_plan_date"}:
                normalized.append(operation)
                continue
            operation = self._normalize_soft_timing_preferences(
                operation,
                source_text,
                location_scopes.get(index),
            )
            operation = self._apply_semantic_location_payload(
                operation,
                source_text,
                location_scopes.get(index),
                saved_locations,
            )
            operation = self._validate_clause_location_ownership(operation, location_scopes.get(index))

            if operation.get("location_status") and "raw_llm_location" in operation:
                self._enrich_existing_location_payload(
                    operation,
                    source_text,
                    location_scopes.get(index),
                )
                normalized.append(operation)
                continue
            location_payload = self._resolve_operation_location(
                operation,
                request_text=request_text,
                transcription=transcription,
                saved_locations=saved_locations,
                explicit_evidence=location_scopes.get(index),
                all_operations=operations,
            )
            operation.update(location_payload)
            operation = self._validate_clause_location_ownership(operation, location_scopes.get(index))
            normalized.append(operation)
        return normalized

    def _validate_clause_location_ownership(
        self,
        operation: Dict[str, Any],
        scoped_evidence: Optional[str],
    ) -> Dict[str, Any]:
        resolved = operation.get("resolved_location")
        has_resolved_coordinates = (
            clean_title(operation.get("location_status") or "") == "resolved"
            and isinstance(resolved, dict)
            and (
                (resolved.get("latitude") is not None and resolved.get("longitude") is not None)
                or (resolved.get("lat") is not None and resolved.get("lng") is not None)
            )
        )
        if has_resolved_coordinates:
            return operation
        if not operation.get("explicit_user_location") and operation.get("location_source") != "explicit_user":
            return operation

        raw_location = str(operation.get("raw_location_text") or operation.get("location_normalized") or operation.get("location") or "")
        if not raw_location:
            return operation

        clause_text = f"{scoped_evidence or ''} {operation.get('title') or ''} {operation.get('notes') or ''}".lower()
        loc_clean = clean_title(raw_location)
        is_home = loc_clean in {"home", "house", "my place", "my house"}

        has_evidence = False
        if is_home:
            has_evidence = any(word in clause_text for word in ["home", "house", "my place"])
        else:
            # For non-home locations, the LLM might have used synonyms (campus -> school) or resolved shared clauses.
            # We trust the parser unless it's the notorious "home" bleed.
            has_evidence = True

        if has_evidence:
            return operation

        jlog("LOCATION_NORMALIZE", f"Rejecting location bleed '{raw_location}' for '{operation.get('title')}' due to lack of clause evidence", "CLAUSE_VALIDATION")
        operation["location_kind"] = "category_only"
        operation["location_category"] = self._infer_location_category(str(operation.get("title") or ""), scoped_evidence or "")
        operation["location_resolution_status"] = "needs_coordinates"
        operation["explicit_user_location"] = False
        operation["location_source"] = "inferred_category"
        operation["travel_required"] = True

        for field in ["location", "location_label", "location_normalized", "raw_location_text", "location_status", "location_confidence"]:
            operation.pop(field, None)

        return operation

    def _enrich_existing_location_payload(
        self,
        operation: Dict[str, Any],
        source_text: str,
        scoped_evidence: Optional[str],
    ) -> None:
        title = str(operation.get("title") or operation.get("target_title") or "").strip()
        evidence = scoped_evidence or source_text or ""
        area_preference = self._infer_area_preference(
            title,
            evidence,
            operation.get("raw_llm_location") or operation.get("location"),
        )
        if not area_preference:
            return
        operation["area_preference"] = area_preference
        if (
            clean_title(operation.get("location_category") or "") == "meal_place"
            and not bool(operation.get("explicit_user_location"))
        ):
            operation["location"] = None
            operation["location_label"] = None
            operation["location_normalized"] = None
            if clean_title(operation.get("location_status") or "") == "resolved":
                operation["location_status"] = "unresolved"
            operation["location_source"] = operation.get("location_source") or "area_preference"

    def _apply_implicit_lunch_handling(
        self,
        operations: List[Dict[str, Any]],
        source_text: str,
        existing_activities: Optional[List[Dict[str, Any]]] = None,
        preferences: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        ops = [deepcopy(op) for op in operations or [] if isinstance(op, dict)]
        lunch_match = self._lunch_reference_match(source_text)
        found_lunch_reference = lunch_match is not None
        if found_lunch_reference:
            jlog(
                "IMPLICIT_LUNCH",
                f"found=true phrase=\"{lunch_match.group(0) if lunch_match else ''}\"",
                "DETECT",
            )
        if not self._should_infer_implicit_lunch(ops, source_text, existing_activities or [], preferences or {}):
            return ops

        relation = self._lunch_reference_relation(source_text)
        if not relation:
            return ops

        existing_lunch_title = self._existing_lunch_title(ops, existing_activities or [])
        ignored_meal_titles = [
            str(item.get("title") or item.get("target_title") or "")
            for item in list(ops or []) + list(existing_activities or [])
            if clean_title(item.get("location_category") or "") == "meal_place"
            and not self._is_lunch_like_title(item.get("title") or item.get("target_title") or "")
        ]
        jlog(
            "IMPLICIT_LUNCH",
            (
                f"existing_lunch={str(bool(existing_lunch_title)).lower()} "
                f"existing_meal_dinner_ignored={str(bool(ignored_meal_titles)).lower()}"
            ),
            "CHECK",
        )
        lunch_title = existing_lunch_title or "Lunch Break"
        if not existing_lunch_title:
            max_sequence = max(
                [int(op.get("sequence_index") or 0) for op in ops if str(op.get("sequence_index") or "").isdigit()] + [0]
            )
            status = "needs_resolution" if bool((preferences or {}).get("accurate_travel_time")) else "unresolved"
            ops.append({
                "op": "add",
                "title": lunch_title,
                "timing_mode": TimingMode.PREFERRED,
                "duration_minutes": 60,
                "priority": "medium",
                "is_mandatory": True,
                "preferred_time_window": "lunch",
                "preferred_window_start": PREFERRED_TIME_WINDOWS["lunch"][0],
                "preferred_window_end": PREFERRED_TIME_WINDOWS["lunch"][1],
                "location": None,
                "location_label": None,
                "location_category": "meal_place",
                "location_status": status,
                "location_source": "implicit_lunch",
                "location_confidence": 0.7,
                "location_normalized": None,
                "raw_llm_location": None,
                "explicit_user_location": False,
                "travel_required": True,
                "implicit_activity": True,
                "implicit_reason": "lunch_reference",
                "sequence_index": max_sequence + 1,
            })
            jlog("IMPLICIT_LUNCH", "title=Lunch Break window=12:00 PM-02:00 PM", "ADD")

        target_index = self._lunch_reference_target_index(ops, source_text, relation)
        if relation == "around_lunch":
            return ops

        if target_index is not None and 0 <= target_index < len(ops):
            target = ops[target_index]
            order = {
                "kind": "after" if relation == "after_lunch" else "before",
                "target_title": lunch_title,
            }
            existing_preferred_order = target.get("preferred_order")
            if isinstance(existing_preferred_order, dict):
                self._append_preferred_order(target, existing_preferred_order)
            self._append_preferred_order(target, order)
            target["preferred_order"] = order
            target["soft_dependency"] = True
            if relation == "after_lunch":
                target["preferred_time_window"] = "after_lunch"
                target["preferred_window_start"] = PREFERRED_TIME_WINDOWS["after_lunch"][0]
                target["preferred_window_end"] = PREFERRED_TIME_WINDOWS["after_lunch"][1]
            if target.get("anchor_relation") and self._anchor_relation_is_soft(
                clean_title(source_text or ""),
                clean_title(source_text or ""),
            ):
                self._append_preferred_order(target, deepcopy(target["anchor_relation"]))
                target.pop("anchor_relation", None)
                if clean_title(target.get("timing_mode") or "") == TimingMode.RELATIVE:
                    target["timing_mode"] = TimingMode.PREFERRED
            jlog(
                "SOFT_PREF",
                f"{target.get('title')} lunch_order={order['kind']} {lunch_title}",
                None,
            )
            jlog(
                "SOFT_PREF",
                (
                    f"{target.get('title')} preferred_window={target.get('preferred_time_window')} "
                    f"preferred_order={order['kind']} {lunch_title}"
                ),
                None,
            )

        return ops

    def _log_normalized_operations(self, operations: List[Dict[str, Any]]) -> None:
        titles = [
            str(op.get("title") or op.get("target_title") or op.get("op") or "")
            for op in operations or []
            if isinstance(op, dict)
        ]
        jlog("NORMALIZED_OPS", f"count={len(titles)} titles={titles}", None)

    def _should_infer_implicit_lunch(
        self,
        operations: List[Dict[str, Any]],
        source_text: str,
        existing_activities: List[Dict[str, Any]],
        preferences: Dict[str, Any],
    ) -> bool:
        if not operations or not self._lunch_reference_relation(source_text):
            return False
        add_ops = [op for op in operations if clean_title(op.get("op") or "add") == "add"]
        if len(add_ops) < 2:
            return False
        route = preferences.get("module_0_route") or next((op.get("_router_route") for op in operations if op.get("_router_route")), None)
        if route == "simple_schedule_command":
            return False
        if route == "complex_schedule_command":
            return True
        request = clean_title(source_text or "")
        return any(marker in request for marker in ("plan", "productive day", "busy workday", "generate", "fit in"))

    def _lunch_reference_relation(self, source_text: str) -> Optional[str]:
        match = self._lunch_reference_match(source_text)
        if not match:
            return None
        phrase = clean_title(match.group(0))
        if phrase in {"after lunch", "after my lunch", "sometime after lunch"}:
            return "after_lunch"
        if phrase in {"before lunch", "before my lunch"}:
            return "before_lunch"
        if phrase in {"lunch time", "around lunch"}:
            return "around_lunch"
        return None

    def _lunch_reference_match(self, source_text: str) -> Optional[re.Match[str]]:
        text = clean_title(source_text or "")
        return re.search(
            r"\b(?:sometime after lunch|after my lunch|after lunch|before my lunch|before lunch|lunch time|around lunch)\b",
            text,
        )

    def _existing_lunch_title(
        self,
        operations: List[Dict[str, Any]],
        existing_activities: List[Dict[str, Any]],
    ) -> Optional[str]:
        for item in list(operations or []) + list(existing_activities or []):
            title = item.get("title") or item.get("target_title") or ""
            if self._is_lunch_like_title(title):
                return item.get("title") or item.get("target_title") or "Lunch"
        return None

    def _is_lunch_like_title(self, title: Any) -> bool:
        return re.search(r"\blunch\b", clean_title(str(title or ""))) is not None

    def _lunch_reference_target_index(
        self,
        operations: List[Dict[str, Any]],
        source_text: str,
        relation: str,
    ) -> Optional[int]:
        text = clean_title(source_text or "")
        if not text:
            return None
        if relation == "after_lunch":
            match = re.search(r"\b(?:after lunch|after my lunch|sometime after lunch)\b", text)
        elif relation == "before_lunch":
            match = re.search(r"\b(?:before lunch|before my lunch)\b", text)
        else:
            match = re.search(r"\b(?:lunch time|around lunch)\b", text)
        if not match:
            return None

        best: Optional[Tuple[int, int]] = None
        for index, operation in enumerate(operations):
            if clean_title(operation.get("op") or "add") != "add":
                continue
            title = clean_title(operation.get("title") or operation.get("target_title") or "")
            if "lunch" in title:
                continue
            for alias in self._activity_mention_aliases(title):
                alias_match = re.search(r"\b" + r"\b.{0,80}?\b".join(re.escape(token) for token in alias.split()) + r"\b", text)
                if not alias_match:
                    continue
                distance = abs(alias_match.start() - match.start())
                if best is None or distance < best[0]:
                    best = (distance, index)
        return best[1] if best else None

    def _append_preferred_order(self, operation: Dict[str, Any], order: Dict[str, Any]) -> None:
        orders = list(operation.get("preferred_orders") or [])
        key = (clean_title(order.get("kind") or ""), clean_title(order.get("target_title") or ""))
        existing = {
            (clean_title(item.get("kind") or ""), clean_title(item.get("target_title") or ""))
            for item in orders
            if isinstance(item, dict)
        }
        if key not in existing:
            orders.append(deepcopy(order))
        operation["preferred_orders"] = orders

    def _resolve_operation_location(
        self,
        operation: Dict[str, Any],
        request_text: str,
        transcription: str,
        saved_locations: List[Dict[str, Any]],
        explicit_evidence: Optional[str] = None,
        all_operations: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        title = str(operation.get("title") or operation.get("target_title") or "").strip()
        raw_location = clean_optional_text(operation.get("location"))
        evidence = " ".join(value for value in [request_text, transcription, operation.get("notes") or ""] if value)
        scoped_evidence = explicit_evidence if explicit_evidence is not None else ""
        explicit_location = self._detect_explicit_location(scoped_evidence, title, raw_location)
        if not explicit_location:
            explicit_location = self._detect_shared_explicit_location(
                transcription or request_text or "",
                title,
                raw_location,
                all_operations or [],
            )
        generic_explicit = self._generic_explicit_location_from_text(transcription or request_text or scoped_evidence or "", title)
        if generic_explicit and (
            (not explicit_location and not self._is_non_home_explicit_text(raw_location))
            or (
                raw_location
                and clean_title(raw_location) in {"home", "store", "gym", "meal_place", "supermarket", "fitness center"}
                and clean_title(generic_explicit.get("label")) != clean_title(raw_location)
            )
        ):
            if raw_location and self._is_non_home_explicit_text(generic_explicit.get("label")) and clean_title(raw_location) != clean_title(generic_explicit.get("label")):
                jlog(
                    "LOCATION_NORMALIZER",
                    f"title={title} kept=\"{generic_explicit.get('label')}\" ignored_default=\"{raw_location}\"",
                    "PRESERVE_EXPLICIT",
                )
            explicit_location = {
                "label": generic_explicit.get("label") or "",
                "category": generic_explicit.get("category") or "unknown",
            }
        category = self._infer_location_category(title, scoped_evidence or operation.get("notes") or "")
        if self._is_no_location_required_activity(title, scoped_evidence, raw_location, explicit_location):
            category = "home_or_online"
        area_preference = self._infer_area_preference(title, scoped_evidence or evidence, raw_location)

        if explicit_location:
            label = explicit_location["label"]
            category = explicit_location.get("category") or category
            source = "explicit_user"
            known_generic = clean_title(label) in {"home", "school", "campus", "office", "library", "gym", "restaurant"}
            status = "resolved" if known_generic else "needs_resolution"
            confidence = 0.95
        else:
            saved = None if category in {"home_or_online", "none"} else self._match_saved_location_for_category(category, saved_locations)
            if saved:
                label = saved["label"]
                source = "saved_profile"
                status = "resolved"
                confidence = 0.9
            else:
                label, status, source, confidence = self._deterministic_location_default(
                    title,
                    raw_location,
                    category,
                    scoped_evidence or evidence,
                )
                if category == "meal_place" and area_preference:
                    label = None
                    status = "unresolved"
                    source = "area_preference"
                    confidence = max(float(confidence or 0), 0.5)

        label = clean_optional_text(label)
        normalized_location = _normalize_location(label)
        travel_required = not (category in {"home_or_online", "none"} or status in {"not_required", "no_location_required"})
        payload = {
            "location": label,
            "location_label": label,
            "location_category": category or "unknown",
            "location_status": status,
            "location_source": source,
            "location_confidence": confidence,
            "location_normalized": normalized_location,
            "raw_llm_location": raw_location,
            "explicit_user_location": bool(explicit_location),
            "travel_required": travel_required,
        }
        if area_preference:
            payload["area_preference"] = area_preference
        if status in {"needs_resolution", "fallback_used", "resolved_default"}:
            payload["location_warning"] = (
                f"{title or 'Activity'} location was estimated as {label or category} because no exact place was provided."
            )
        self._log_location_resolution(title, raw_location, payload)
        return payload

    def _is_no_location_required_activity(
        self,
        title: str,
        scoped_evidence: str,
        raw_location: Optional[str],
        explicit_location: Optional[Dict[str, str]],
    ) -> bool:
        if explicit_location:
            return False
        raw_clean = clean_title(raw_location or "")
        title_text = clean_title(title or "")
        scoped_text = clean_title(scoped_evidence or "")
        combined = f"{title_text} {scoped_text}".strip()

        if raw_clean and raw_clean not in {"none", "null", "online"}:
            if raw_clean in {"home"} and self._contains_any_keyword(title_text, {"dinner", "lunch", "meal", "coffee"}):
                return False
            if (
                raw_clean not in {"home", "online", "school", "campus", "office", "workplace", "library"}
                and not self._contains_any_keyword(title_text, NO_LOCATION_REQUIRED_TITLE_KEYWORDS)
            ):
                return False

        if re.search(r"\b(?:call|phone|online)\b", combined):
            return True
        if re.search(r"\b(?:plan tomorrow|planning tomorrow|planning)\b", title_text):
            return True

        if self._contains_any_keyword(title_text, PHYSICAL_PLACE_TITLE_KEYWORDS):
            if not self._contains_any_keyword(title_text, {"assignment", "review", "fyp", "implementation", "call", "parents", "plan tomorrow", "planning"}):
                return False

        if self._contains_any_keyword(title_text, {"assignment", "review", "fyp", "implementation", "coding", "work", "proposal", "writing", "admin", "laptop", "document", "documents", "paperwork"}):
            return not re.search(r"\b(?:at|in|near)\s+(?:the\s+)?(?:library|campus|office|school|cafe|restaurant|gym|store|supermarket)\b", scoped_text)
        if self._contains_any_keyword(title_text, {"study"}) and not self._contains_any_keyword(combined, {"library", "campus", "school", "office", "cafe"}):
            return True
        return False

    def _is_no_location_work_task(
        self,
        title: str,
        evidence: str,
        raw_location: Optional[str],
    ) -> bool:
        title_text = clean_title(title or "")
        evidence_text = clean_title(evidence or "")
        raw_clean = clean_title(raw_location or "")
        if raw_clean and raw_clean not in NULL_TEXT_VALUES:
            return False
        if not self._contains_any_keyword(
            title_text,
            {"proposal", "writing", "admin", "laptop", "document", "documents", "paperwork", "focused work", "deep work"},
        ):
            return False
        return not re.search(
            r"\b(?:at|in|near)\s+(?:the\s+)?(?:library|campus|office|school|cafe|restaurant|gym|store|supermarket|bank)\b",
            evidence_text,
        )

    def _work_task_needs_quiet_place(self, title: str, evidence: str) -> bool:
        text = clean_title(f"{title or ''} {evidence or ''}")
        return bool(re.search(r"\b(?:proposal|focused|focus|deep work|writing|quiet)\b", text))

    def _infer_area_preference(
        self,
        title: str,
        evidence: str,
        raw_location: Optional[str],
    ) -> Optional[str]:
        title_text = clean_title(title or "")
        text = clean_title(" ".join(value for value in (evidence, raw_location or "") if value))
        if not self._contains_any_keyword(title_text, {"lunch", "dinner", "meal", "breakfast", "coffee"}):
            return None
        if re.search(r"\bnear\s+(?:my\s+|the\s+)?home\b", text):
            return "near_home"
        if re.search(r"\bnear\s+(?:the\s+)?campus\b", text):
            return "near_campus"
        return None

    def _infer_location_category(self, title: str, evidence: str) -> str:
        title_text = clean_title(title or "")
        if self._contains_any_keyword(title_text, {"grocery", "groceries", "shopping", "supermarket", "buy food", "buy groceries"}):
            return "supermarket"
        if self._contains_any_keyword(title_text, {"gym", "workout", "exercise"}):
            return "fitness_center"
        if self._contains_any_keyword(title_text, {"bank", "banking"}):
            return "bank"
        if self._contains_any_keyword(title_text, {"lunch", "dinner", "meal", "breakfast", "restaurant", "cafe", "coffee"}):
            return "meal_place"
        if self._contains_any_keyword(title_text, {"meeting", "seminar", "class", "lecture", "office", "library", "campus"}):
            return "institution"
        if self._contains_any_keyword(title_text, {"study", "fyp", "implementation", "coding"}):
            return "workplace"

        text = clean_title(evidence or "")
        if self._contains_any_keyword(text, {"grocery", "groceries", "shopping", "supermarket", "buy food", "buy groceries"}):
            return "supermarket"
        if self._contains_any_keyword(text, {"gym", "workout", "exercise"}):
            return "fitness_center"
        if self._contains_any_keyword(text, {"bank", "banking"}):
            return "bank"
        if self._contains_any_keyword(text, {"lunch", "dinner", "meal", "breakfast", "restaurant", "cafe", "coffee"}):
            return "meal_place"
        if self._contains_any_keyword(text, {"meeting", "seminar", "class", "lecture", "office", "library", "campus"}):
            return "institution"
        if self._contains_any_keyword(text, {"study", "fyp", "implementation", "coding"}):
            return "workplace"
        return "unknown"

    def _normalize_soft_timing_preferences(
        self,
        operation: Dict[str, Any],
        source_text: str,
        scoped_evidence: Optional[str],
    ) -> Dict[str, Any]:
        updated = deepcopy(operation)
        scoped = clean_title(scoped_evidence or "")
        full = clean_title(source_text or "")
        title = clean_title(updated.get("title") or updated.get("target_title") or "")
        clause_context = self._time_scope_clause_for_title(source_text, title)
        # Avoid applying broad phrases from the whole multi-activity request to
        # unrelated activities. Title/clause scopes carry the useful context.
        context = " ".join(part for part in (scoped, clause_context) if part)

        window = self._preferred_time_window_from_text(context, title)
        exact_time_requested = any(
            updated.get(key) is not None and updated.get(key) != ""
            for key in ("fixed_start", "fixedStart", "startTime")
        )
        if not window and not exact_time_requested:
            window = self._default_preferred_window_for_title(title)
        if not window and not exact_time_requested and self._title_defaults_to_evening(title):
            start, end = PREFERRED_TIME_WINDOWS["evening"]
            window = ("evening", start, end)
        if re.search(r"\bif\s+possible\b", full) and re.search(r"\bcoffee\b", title):
            updated["is_mandatory"] = False
            updated["isMandatory"] = False
            updated["priority"] = "low"
            updated["optional_reason"] = "if_possible"
        if re.search(r"\b(?:anywhere\s+quiet|quiet\s+(?:place|space|spot))\b", f"{title} {context}"):
            updated["can_be_done_at_current_location"] = True
            updated["quiet_place_required"] = True
        if window and not updated.get("preferred_time_window"):
            label, start, end = window
            updated["preferred_time_window"] = label
            updated["preferred_window_start"] = start
            updated["preferred_window_end"] = end
            if label == "lunch" and updated.get("earliest_start") is None:
                updated["earliest_start"] = 11 * 60
            if label == "business_hours":
                if updated.get("earliest_start") is None:
                    updated["earliest_start"] = start
                if updated.get("latest_end") is None:
                    updated["latest_end"] = end
            jlog_verbose("MODULE_C", f"{updated.get('title')} preferred_window={label}", "SOFT_PREF")
            jlog_verbose("SOFT_PREF", f"{updated.get('title')} preferred_window={label}", None)
        elif updated.get("preferred_time_window") and (
            updated.get("preferred_window_start") is None or updated.get("preferred_window_end") is None
        ):
            existing_label = clean_title(updated.get("preferred_time_window") or "")
            if existing_label in PREFERRED_TIME_WINDOWS:
                start, end = PREFERRED_TIME_WINDOWS[existing_label]
                updated["preferred_window_start"] = start
                updated["preferred_window_end"] = end
                if existing_label == "business_hours":
                    if updated.get("earliest_start") is None:
                        updated["earliest_start"] = start
                    if updated.get("latest_end") is None:
                        updated["latest_end"] = end

        anchor = updated.get("anchor_relation")
        if anchor and self._anchor_relation_is_soft(context, full):
            updated["preferred_order"] = deepcopy(anchor)
            updated["soft_dependency"] = True
            updated.pop("anchor_relation", None)
            if clean_title(updated.get("timing_mode") or "") == TimingMode.RELATIVE:
                updated["timing_mode"] = TimingMode.PREFERRED if window else TimingMode.UNSPECIFIED
            jlog_verbose(
                "MODULE_C",
                f"{updated.get('title')} soft_order={anchor.get('kind')} {anchor.get('target_title')}",
                "SOFT_PREF",
            )
        return updated

    def _title_defaults_to_evening(self, title: str) -> bool:
        normalized = clean_title(title or "")
        return bool(re.search(r"\b(?:dinner|supper)\b", normalized))

    def _default_preferred_window_for_title(self, title: str) -> Optional[Tuple[str, int, int]]:
        normalized = clean_title(title or "")
        if re.search(r"\blunch\b", normalized):
            start, end = PREFERRED_TIME_WINDOWS["lunch"]
            return "lunch", start, end
        if re.search(r"\bcoffee\b", normalized):
            start, end = PREFERRED_TIME_WINDOWS["coffee_break"]
            return "coffee_break", start, end
        if re.search(r"\bbank(?:ing)?\b", normalized):
            start, end = PREFERRED_TIME_WINDOWS["business_hours"]
            return "business_hours", start, end
        return None

    def _time_scope_clause_for_title(self, source_text: str, title: str) -> str:
        if not source_text or not title:
            return ""
        for clause in re.split(r"[.;\n]", source_text):
            clean_clause = clean_title(clause)
            if not clean_clause:
                continue
            if not re.search(
                r"\b(?:at night|night|morning|afternoon|after lunch|sometime after lunch|not too late|later in the day)\b",
                clean_clause,
            ):
                continue
            aliases = self._activity_mention_aliases(title)
            if any(self._alias_matches_clause(alias, clean_clause) for alias in aliases):
                return clean_clause
        return ""

    def _alias_matches_clause(self, alias: str, clean_clause: str) -> bool:
        tokens = [token for token in re.split(r"[^a-z0-9]+", clean_title(alias or "")) if token]
        if not tokens:
            return False
        if len(tokens) == 1:
            return re.search(rf"\b{re.escape(tokens[0])}\b", clean_clause) is not None
        pattern = r"\b" + r"\b.{0,80}?\b".join(re.escape(token) for token in tokens) + r"\b"
        return re.search(pattern, clean_clause) is not None

    def _preferred_time_window_from_text(self, context: str, title: str) -> Optional[Tuple[str, int, int]]:
        checks = [
            ("night", r"\b(?:at night|near night|tonight|night)\b"),
            ("not_too_late", r"\b(?:not too late|not so late)\b"),
            ("evening", r"\b(?:evening|later in the day)\b"),
            ("lunch", r"\b(?:lunch time|around lunch)\b"),
            ("after_lunch", r"\b(?:after lunch|sometime after lunch)\b"),
            ("afternoon", r"\bafternoon\b"),
            ("morning", r"\bmorning\b"),
        ]
        for label, pattern in checks:
            if re.search(pattern, context):
                start, end = PREFERRED_TIME_WINDOWS[label]
                return label, start, end
        if "plan tomorrow" in title or title == "planning":
            start, end = PREFERRED_TIME_WINDOWS["night"]
            return "night", start, end
        return None

    def _anchor_relation_is_soft(self, scoped_context: str, full_context: str) -> bool:
        soft_patterns = (
            r"\bpreferably\b",
            r"\bif possible\b",
            r"\bmaybe\b",
            r"\bi'?m thinking of\b",
            r"\bmight want\b",
            r"\bsometime after\b",
            r"\bnot too late\b",
            r"\blater in the day\b",
            r"\bat night\b",
            r"\baround\b",
            r"\bafter that\b",
        )
        hard_patterns = (
            r"\bright after\b",
            r"\bimmediately after\b",
            r"\bmust be after\b",
            r"\bonly after\b",
            r"\bbefore .* starts\b",
            r"\bcannot happen before\b",
        )
        contexts = [value for value in (scoped_context, full_context) if value]
        if any(re.search(pattern, context) for pattern in hard_patterns for context in contexts):
            return False
        if any(re.search(pattern, context) for pattern in soft_patterns for context in contexts):
            return True
        return False

    def _build_activity_location_scopes(
        self,
        operations: List[Dict[str, Any]],
        source_text: str,
    ) -> Dict[int, str]:
        text = re.sub(r"\s+", " ", source_text or "").strip()
        if not text:
            return {}

        spans: Dict[int, Tuple[int, int]] = {}
        cursor = 0
        for index, operation in enumerate(operations or []):
            if not isinstance(operation, dict):
                continue
            if clean_title(operation.get("op") or "") in {"remove", "shift_plan_date"}:
                continue
            title = str(operation.get("title") or operation.get("target_title") or "").strip()
            span = self._find_activity_mention_span(text, title, start_at=cursor)
            if span is None:
                span = self._find_activity_mention_span(text, title, start_at=0)
            if span is None:
                continue
            spans[index] = span
            cursor = max(cursor, span[1])

        if not spans:
            if len([op for op in operations or [] if isinstance(op, dict)]) == 1:
                return {0: text}
            return {}

        ordered = sorted(spans.items(), key=lambda item: item[1][0])
        scopes: Dict[int, str] = {}
        for ordered_index, (operation_index, span) in enumerate(ordered):
            previous_span = ordered[ordered_index - 1][1] if ordered_index > 0 else None
            next_span = ordered[ordered_index + 1][1] if ordered_index + 1 < len(ordered) else None

            if previous_span:
                boundary = self._last_location_scope_boundary(text, previous_span[1], span[0])
                segment_start = boundary if boundary is not None else span[0]
            else:
                boundary = self._last_location_scope_boundary(text, 0, span[0])
                segment_start = boundary if boundary is not None else 0

            if next_span:
                boundary = self._first_location_scope_boundary(text, span[1], next_span[0])
                segment_end = boundary if boundary is not None else next_span[0]
            else:
                boundary = self._first_location_scope_boundary(text, span[1], len(text))
                segment_end = boundary if boundary is not None else len(text)

            segment_start = max(0, min(segment_start, span[0]))
            segment_end = max(span[1], min(segment_end, len(text)))
            scopes[operation_index] = text[segment_start:segment_end].strip()

        for index, operation in enumerate(operations or []):
            if index not in scopes and isinstance(operation, dict):
                scopes[index] = str(operation.get("notes") or "").strip()
        return scopes

    def _find_activity_mention_span(
        self,
        text: str,
        title: str,
        start_at: int = 0,
    ) -> Optional[Tuple[int, int]]:
        search_text = clean_title(text or "")
        aliases = self._activity_mention_aliases(title)
        for alias in aliases:
            pattern = r"\b" + r"\s+".join(re.escape(token) for token in alias.split()) + r"\b"
            match = re.search(pattern, search_text[start_at:], flags=re.IGNORECASE)
            if match:
                return start_at + match.start(), start_at + match.end()

        tokens = self._activity_title_tokens(title)
        if len(tokens) >= 2:
            pattern = r"\b" + r"\b.{0,80}?\b".join(re.escape(token) for token in tokens) + r"\b"
            match = re.search(pattern, search_text[start_at:], flags=re.IGNORECASE)
            if match:
                return start_at + match.start(), start_at + match.end()
        return None

    def _activity_mention_aliases(self, title: str) -> List[str]:
        aliases = set(self._generate_aliases(title))
        normalized = clean_title(title or "")
        if normalized:
            aliases.add(normalized)
        return sorted(
            (alias for alias in aliases if alias),
            key=lambda value: (len(value.split()), len(value)),
            reverse=True,
        )

    def _activity_title_tokens(self, title: str) -> List[str]:
        stop_words = {"quick", "the", "my", "a", "an"}
        return [
            token
            for token in re.split(r"[^a-z0-9]+", clean_title(title or ""))
            if token and token not in stop_words
        ]

    def _last_location_scope_boundary(self, text: str, start: int, end: int) -> Optional[int]:
        boundary: Optional[int] = None
        for match in re.finditer(
            r"[.;]|\b(?:followed by|and then|after that|then|also)\b",
            text[start:end],
            flags=re.IGNORECASE,
        ):
            boundary = start + match.end()
        return boundary

    def _first_location_scope_boundary(self, text: str, start: int, end: int) -> Optional[int]:
        match = re.search(
            r"[.;]|\b(?:followed by|and then|after that|then|also)\b",
            text[start:end],
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        return start + match.start()

    def _detect_explicit_location(
        self,
        evidence: str,
        title: str,
        raw_location: Optional[str],
    ) -> Optional[Dict[str, str]]:
        matches = self._explicit_location_matches(evidence, title, raw_location)
        if not matches:
            return None

        title_span = self._find_activity_mention_span(evidence, title, start_at=0)
        if title_span:
            after_title = [match for match in matches if match["start"] >= title_span[1]]
            if after_title:
                chosen = min(after_title, key=lambda match: match["start"] - title_span[1])
                return {"label": chosen["label"], "category": chosen["category"]}
            chosen = min(matches, key=lambda match: title_span[0] - match["end"])
            return {"label": chosen["label"], "category": chosen["category"]}

        chosen = min(matches, key=lambda match: match["start"])
        return {"label": chosen["label"], "category": chosen["category"]}

    def _explicit_location_matches(
        self,
        evidence: str,
        title: str,
        raw_location: Optional[str],
    ) -> List[Dict[str, Any]]:
        text = clean_title(evidence or "")
        matches: List[Dict[str, Any]] = []
        explicit_patterns = [
            (r"\b(?:at|in|from)\s+(?:my\s+|the\s+)?home\b", "home", "home"),
            (r"\bgo home\b", "home", "home"),
            (r"\bat\s+(?:the\s+)?library\b", "library", "library"),
            (r"\bat\s+(?:the\s+)?gym\b", "gym", "fitness_center"),
            (r"\bnear\s+(?:the\s+)?campus\b", "school", "campus_area"),
            (r"\bat\s+(?:the\s+)?campus\b", "school", "campus_area"),
            (r"\bat\s+(?:the\s+)?school\b", "school", "campus_area"),
            (r"\bat\s+(?:the\s+)?main office\b", "office", "office"),
            (r"\bat\s+(?:a\s+|the\s+)?(?:cafe|restaurant)\b", "restaurant", "meal_place"),
            (r"\bonline\b|\bdelivery\b|\bhome delivery\b", "home", "home"),
        ]
        for pattern, label, category in explicit_patterns:
            for match in re.finditer(pattern, text):
                matches.append({
                    "start": match.start(),
                    "end": match.end(),
                    "label": label,
                    "category": category,
                })

        raw_clean = clean_title(raw_location or "")
        if raw_clean and raw_clean not in {"home", "school", "campus", "gym", "office", "library"}:
            location_pattern = rf"\b(?:at|in|near)\s+(?:the\s+)?{re.escape(raw_clean)}\b"
            for match in re.finditer(location_pattern, text):
                matches.append({
                    "start": match.start(),
                    "end": match.end(),
                    "label": raw_location or raw_clean,
                    "category": self._infer_location_category(title, evidence),
                })
        return sorted(matches, key=lambda match: match["start"])

    def _detect_shared_explicit_location(
        self,
        source_text: str,
        title: str,
        raw_location: Optional[str],
        operations: List[Dict[str, Any]],
    ) -> Optional[Dict[str, str]]:
        text = re.sub(r"\s+", " ", source_text or "").strip()
        if not text:
            return None

        clauses = [clause.strip() for clause in re.split(r"[.;]", text) if clause.strip()]
        for clause in clauses:
            title_span = self._find_activity_mention_span(clause, title, start_at=0)
            if not title_span:
                continue
            matches = self._explicit_location_matches(clause, title, raw_location)
            if not matches:
                continue
            for match in matches:
                if title_span[0] > match["start"]:
                    continue
                other_before_location = False
                for operation in operations or []:
                    if not isinstance(operation, dict):
                        continue
                    other_title = str(operation.get("title") or operation.get("target_title") or "").strip()
                    if clean_title(other_title) == clean_title(title):
                        continue
                    other_span = self._find_activity_mention_span(clause, other_title, start_at=0)
                    if other_span and other_span[0] < match["start"]:
                        other_before_location = True
                        break
                if not other_before_location:
                    continue

                prefix = clean_title(clause[:match["start"]])
                if (
                    re.search(r"\b(?:both|all)\b", prefix)
                    or "same location" in clean_title(clause)
                    or "for all" in clean_title(clause)
                    or re.search(r"\band\b", prefix)
                ):
                    return {"label": match["label"], "category": match["category"]}
        return None

    def _match_saved_location_for_category(
        self,
        category: str,
        saved_locations: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        category_keywords = {
            "supermarket": {"supermarket", "grocery", "groceries", "market", "store"},
            "fitness_center": {"gym", "fitness", "workout"},
            "meal_place": {"restaurant", "cafe", "food", "meal", "lunch", "dinner"},
            "campus_area": {"campus", "school", "university", "mmu"},
            "office": {"office", "work"},
            "library": {"library"},
            "bank": {"bank", "banking"},
            "home": {"home", "house"},
            "home_or_online": {"home", "online", "remote"},
        }
        keywords = category_keywords.get(category, set())
        for saved in saved_locations or []:
            haystack = clean_title(" ".join(str(saved.get(field) or "") for field in ("label", "address", "category", "type")))
            if any(keyword in haystack for keyword in keywords):
                return {"label": saved.get("label") or saved.get("address") or next(iter(keywords))}
        return None

    def _deterministic_location_default(
        self,
        title: str,
        raw_location: Optional[str],
        category: str,
        evidence: str,
    ) -> Tuple[Optional[str], str, str, float]:
        text = clean_title(f"{title} {evidence}")
        raw_clean = clean_title(raw_location or "")
        if category in {"home_or_online", "none"}:
            return None, "not_required", "deterministic_default", 0.9
        if category == "supermarket":
            return "store", "needs_resolution", "deterministic_default", 0.65
        if category == "fitness_center":
            label = raw_location if raw_clean in {"gym", "fitness center"} else "gym"
            return label, "resolved_default", "deterministic_default", 0.75
        if category == "meal_place":
            return None, "needs_resolution", "unresolved", 0.35
        if category == "bank":
            return None, "needs_resolution", "unresolved", 0.35
        if category == "institution" and raw_location:
            return raw_location, "fallback_used", "llm_inferred", 0.55
        if category == "workplace":
            if raw_location:
                return raw_location, "resolved_default", "deterministic_default", 0.6
            return None, "needs_resolution", "unresolved", 0.3
        if raw_location:
            return raw_location, "fallback_used", "llm_inferred", 0.45
        return None, "needs_resolution", "unresolved", 0.2

    def _log_location_resolution(
        self,
        title: str,
        raw_location: Optional[str],
        payload: Dict[str, Any],
    ) -> None:
        jlog_verbose(
            "LOCATION",
            f"{title or 'Untitled'} | raw_llm_location={raw_location} | "
            f"explicit_user_location={str(payload.get('explicit_user_location')).lower()} | "
            f"normalized={payload.get('location_label')} | "
            f"category={payload.get('location_category')} | "
            f"source={payload.get('location_source')} | "
            f"status={payload.get('location_status')} | "
            f"travel_required={str(payload.get('travel_required', True)).lower()} | module=MODULE_A",
        )

