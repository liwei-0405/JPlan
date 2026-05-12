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
        return normalized

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
            if operation.get("location_status") and "raw_llm_location" in operation:
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
            normalized.append(operation)
        return normalized

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
        category = self._infer_location_category(title, evidence)

        if explicit_location:
            label = explicit_location["label"]
            category = explicit_location.get("category") or category
            source = "explicit_user"
            status = "resolved"
            confidence = 0.95
        else:
            saved = self._match_saved_location_for_category(category, saved_locations)
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
                    evidence,
                )

        label = clean_optional_text(label)
        normalized_location = _normalize_location(label)
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
        }
        if status in {"needs_resolution", "fallback_used", "resolved_default"}:
            payload["location_warning"] = (
                f"{title or 'Activity'} location was estimated as {label or category} because no exact place was provided."
            )
        self._log_location_resolution(title, raw_location, payload)
        return payload

    def _infer_location_category(self, title: str, evidence: str) -> str:
        title_text = clean_title(title or "")
        if self._contains_any_keyword(title_text, {"grocery", "groceries", "shopping", "supermarket", "buy food", "buy groceries"}):
            return "supermarket"
        if self._contains_any_keyword(title_text, {"gym", "workout", "exercise"}):
            return "fitness_center"
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
        if self._contains_any_keyword(text, {"lunch", "dinner", "meal", "breakfast", "restaurant", "cafe", "coffee"}):
            return "meal_place"
        if self._contains_any_keyword(text, {"meeting", "seminar", "class", "lecture", "office", "library", "campus"}):
            return "institution"
        if self._contains_any_keyword(text, {"study", "fyp", "implementation", "coding"}):
            return "workplace"
        return "unknown"

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

    def _activity_location_context(self, evidence: str, title: str) -> str:
        text = re.sub(r"\s+", " ", evidence or "").strip()
        clean_text = clean_title(text)
        tokens = [token for token in re.split(r"[^a-z0-9]+", clean_title(title or "")) if token]
        stop_words = {"quick", "the", "my", "a", "an"}
        tokens = [token for token in tokens if token not in stop_words]
        if not tokens:
            return text

        positions = [clean_text.find(token) for token in tokens if clean_text.find(token) >= 0]
        if not positions:
            return text
        start = max(0, min(positions) - 60)
        end = min(len(text), max(positions) + 140)
        return text[start:end]

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
            "home": {"home", "house"},
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
        if category == "supermarket":
            return "store", "needs_resolution", "deterministic_default", 0.65
        if category == "fitness_center":
            label = raw_location if raw_clean in {"gym", "fitness center"} else "gym"
            return label, "resolved_default", "deterministic_default", 0.75
        if category == "meal_place":
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
        jlog(
            "LOCATION",
            f"{title or 'Untitled'} | raw_llm_location={raw_location} | "
            f"explicit_user_location={str(payload.get('explicit_user_location')).lower()} | "
            f"normalized={payload.get('location_label')} | "
            f"category={payload.get('location_category')} | "
            f"source={payload.get('location_source')} | "
            f"status={payload.get('location_status')} | module=MODULE_A",
        )

