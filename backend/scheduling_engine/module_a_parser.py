import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from threading import BoundedSemaphore
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4
from zoneinfo import ZoneInfo

from jplan_logging import jjson, jjson_verbose, jlog, jlog_verbose, jsection
from travel_service import MissingORSApiKey, TravelService, TravelServiceError, coordinate_from_saved_location
from .types_utils import *
from .types_utils import _normalize_location

MODULE_A_LLM_EXECUTOR = ThreadPoolExecutor(
    max_workers=MODULE_A_LLM_EXECUTOR_WORKERS,
    thread_name_prefix="jplan-modulea",
)
MODULE_A_LLM_SEMAPHORE = BoundedSemaphore(MODULE_A_LLM_EXECUTOR_WORKERS)
MODULE_A_PRIMARY_MODEL = MODULE_A_LLM_MODEL


class ModuleALLMTimeoutError(TimeoutError):
    pass


class ModuleALLMExecutorSaturatedError(RuntimeError):
    pass


class ModuleALLMTotalBudgetExceededError(TimeoutError):
    pass


class ModuleAParserMixin:
    def _parse_request(
        self,
        latest_request: str,
        history: List[Dict[str, Any]],
        current_schedule: Optional[Dict[str, Any]],
        audio_part: Any,
        saved_locations: List[Dict[str, Any]] = [],
        disable_deterministic_fallback: bool = False,
        fallback_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        jsection("MODULE_A", "LLM parsing", "PARSE")
        jlog_verbose("MODULE_A", f"Request={latest_request!r}", "PARSE")
        if fallback_reason:
            active_titles = [
                str(item.get("title"))
                for item in (current_schedule or {}).get("activities", [])
                if isinstance(item, dict) and item.get("title")
            ]
            jlog("MODULE_A", f"active_titles={active_titles}", "EDIT_CONTEXT")
        
        compact_output = self._should_use_compact_module_a_output(latest_request, current_schedule)
        if compact_output:
            jlog("MODULE_A", f"compact=true max_output_tokens={MODULE_A_MAX_OUTPUT_TOKENS}", "OUTPUT_MODE")
        prompt = self._build_parser_prompt(
            latest_request,
            history,
            current_schedule,
            saved_locations,
            compact_output=compact_output,
        )

        contents: Any = prompt if audio_part is None else [prompt, audio_part]
        raw_llm_reply: Optional[str] = None
        raw_response_text: Optional[str] = None
        fallback_disabled_reason = self._deterministic_fallback_disabled_reason(
            latest_request,
            current_schedule,
            explicit_disable=disable_deterministic_fallback,
            fallback_reason=fallback_reason,
        )

        try:
            response = self._generate_parser_content_with_retry(contents)
            raw_response_text = response.text or ""
            
            # Print Token Usage
            usage = getattr(response, "usage_metadata", None)
            token_usage = None
            if usage:
                token_usage = {
                    "prompt": int(getattr(usage, "prompt_token_count", 0) or 0),
                    "candidates": int(getattr(usage, "candidates_token_count", 0) or 0),
                    "total": int(getattr(usage, "total_token_count", 0) or 0),
                }
                jlog(
                    "MODULE_A",
                    f"Prompt={usage.prompt_token_count} | Candidates={usage.candidates_token_count} | Total={usage.total_token_count}",
                    "TOKEN",
                )
            
            parsed = self._safe_json_loads(raw_response_text)
            if isinstance(parsed, dict):
                raw_llm_reply = str(parsed.get("reply") or "").strip() or None
        except json.JSONDecodeError as exc:
            self._debug(f"LLM parse exception | type={type(exc).__name__} | message={str(exc)}")
            fallback = self._safe_deterministic_fallback_parse(
                latest_request,
                current_schedule,
                history,
                saved_locations,
                fallback_disabled_reason,
            )
            if fallback:
                self._debug_json("Deterministic fallback parse result", fallback)
                return fallback
            invalid = self._invalid_llm_parse(
                latest_request=latest_request,
                current_schedule=current_schedule,
                raw_llm_reply=raw_llm_reply,
                failure_type="llm_parse_error",
                failure_message=str(exc),
                raw_response_text=raw_response_text,
            )
            self._debug_json("Invalid LLM parse result", invalid)
            return invalid
        except Exception as exc:
            self._debug(f"LLM call exception | type={type(exc).__name__} | message={str(exc)}")
            fallback = self._safe_deterministic_fallback_parse(
                latest_request,
                current_schedule,
                history,
                saved_locations,
                fallback_disabled_reason,
            )
            if fallback:
                self._debug_json("Deterministic fallback parse result", fallback)
                return fallback
            failure_type = self._module_a_failure_type(exc)
            invalid = self._invalid_llm_parse(
                latest_request=latest_request,
                current_schedule=current_schedule,
                raw_llm_reply=raw_llm_reply,
                failure_type=failure_type,
                failure_message=str(exc),
                raw_response_text=raw_response_text,
            )
            self._debug_json("Invalid LLM parse result", invalid)
            return invalid

        if not isinstance(parsed, dict):
            if compact_output and fallback_disabled_reason and self._looks_like_incomplete_json(raw_response_text):
                invalid = self._invalid_llm_parse(
                    latest_request=latest_request,
                    current_schedule=current_schedule,
                    raw_llm_reply=raw_llm_reply,
                    failure_type="llm_parse_error",
                    failure_message="LLM did not return a complete JSON object.",
                    raw_response_text=raw_response_text,
                )
                invalid["reply"] = "I couldn't safely parse the full-day plan. Please try again, or split it into fixed events and flexible tasks."
                self._debug_json("Invalid LLM parse result", invalid)
                return invalid
            fallback = self._safe_deterministic_fallback_parse(
                latest_request,
                current_schedule,
                history,
                saved_locations,
                fallback_disabled_reason,
            )
            if fallback:
                self._debug_json("Deterministic fallback parse result", fallback)
                return fallback
            invalid = self._invalid_llm_parse(
                latest_request=latest_request,
                current_schedule=current_schedule,
                raw_llm_reply=raw_llm_reply,
                failure_type="llm_parse_error",
                failure_message="LLM did not return a JSON object.",
                raw_response_text=raw_response_text,
            )
            self._debug_json("Invalid LLM parse result", invalid)
            return invalid

        parsed.setdefault("intent", "schedule")
        parsed.setdefault("reply", "I translated your request into a plan draft.")
        parsed.setdefault("transcription", latest_request)
        parsed.setdefault("activities", [])
        parsed.setdefault("operations", [])
        parsed.setdefault("preferences", {})
        parsed["_reply_source"] = "llm"
        parsed["_llm_reply"] = raw_llm_reply
        if token_usage:
            parsed["_token_usage"] = token_usage
        parsed = self._normalize_plan_level_operations(parsed, latest_request, current_schedule)
        parsed = self._normalize_parsed_locations(parsed, latest_request, saved_locations)
        parsed = self._normalize_module_a_duplicate_relative_adds(parsed, current_schedule)
        parse_rejection = self._module_a_parse_rejection_reason(parsed, latest_request)
        if parse_rejection:
            invalid = self._invalid_llm_parse(
                latest_request=latest_request,
                current_schedule=current_schedule,
                raw_llm_reply=raw_llm_reply,
                failure_type=parse_rejection,
                failure_message=parse_rejection,
                raw_response_text=raw_response_text,
            )
            invalid["reply"] = "I couldn't safely understand the full-day plan. Please try again, or split it into fixed events and flexible tasks."
            if parse_rejection in {"expected_multi_activity_got_one", "missing_expected_operations"}:
                jlog("MODULE_A", f"reason={parse_rejection}", "SCHEMA_INVALID")
            else:
                jlog("MODULE_A", "reason=giant_title_or_complex_fallback", "PARSE_REJECTED")
            self._debug_json("Invalid LLM parse result", invalid)
            return invalid
        if self._is_schedule_change_intent(parsed) and not parsed.get("operations") and not parsed.get("activities"):
            jlog(
                "MODULE_A",
                "Empty operations for edit intent. Attempting deterministic fallback parse.",
                "SAFETY",
            )
            fallback = self._safe_deterministic_fallback_parse(
                latest_request,
                current_schedule,
                history,
                saved_locations,
                fallback_disabled_reason,
            )
            if fallback:
                self._debug_json("Deterministic fallback parse result", fallback)
                return fallback
            parsed["intent"] = "no_operation"
            parsed["reply"] = (
                "I couldn't safely understand the full-day plan. Please try again, or split it into fixed events and flexible tasks."
                if fallback_disabled_reason == "complex_or_travel_intent"
                else "I could not understand or apply the requested change. Please try again with the activity name and time."
            )
            parsed["_failure_type"] = "empty_operations"
        jlog(
            "MODULE_A",
            f"parser_rejected_request=false operations_count={len(parsed.get('operations') or [])}",
            "SAFETY",
        )
        self._log_module_a_summary(parsed)
        jjson_verbose("MODULE_A", "LLM parsed request", parsed, "PARSE")
        self._debug(
            f"Parsed request | intent={parsed.get('intent')} | parsed_date={parsed.get('date')} | activities={len(parsed.get('activities', []))} | operations={len(parsed.get('operations', []))}"
        )
        return parsed

    def _log_module_a_summary(self, parsed: Dict[str, Any]) -> None:
        operations = parsed.get("operations") or parsed.get("activities") or []
        jlog(
            "MODULE_A",
            f"intent={parsed.get('intent')} date={parsed.get('date')} operations={len(operations)}",
            "PARSE_SUMMARY",
        )
        if not operations:
            return
        lines = []
        for op in operations:
            title = op.get("title") or op.get("target_title") or "activity"
            timing = op.get("fixed_start") or op.get("preferred_time_window") or op.get("timing_mode") or "flexible"
            duration = op.get("duration_minutes") or op.get("duration") or "?"
            location = op.get("location_label") or op.get("location") or "no location"
            status = op.get("location_status") or ""
            needs = "needs coordinates" if clean_title(status) in {"needs_resolution", "unresolved", "fallback_used"} else status or "ok"
            lines.append(f"- {title} | {timing} | {duration} min | {location} ({needs})")
        jlog("SUMMARY", "Module A output:\n" + "\n".join(lines), "MODULE_A_OUTPUT")

    def _safe_deterministic_fallback_parse(
        self,
        latest_request: str,
        current_schedule: Optional[Dict[str, Any]],
        history: Optional[List[Dict[str, Any]]],
        saved_locations: Optional[List[Dict[str, Any]]],
        disabled_reason: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        if disabled_reason:
            jlog("MODULE_A", f"reason={disabled_reason}", "FALLBACK_DISABLED")
            return None
        return self._deterministic_fallback_parse(
            latest_request,
            current_schedule,
            history,
            saved_locations=saved_locations or [],
        )

    def _should_use_compact_module_a_output(
        self,
        latest_request: str,
        current_schedule: Optional[Dict[str, Any]],
    ) -> bool:
        if self._is_strict_atomic_command(latest_request):
            return False
        return self._request_is_complex_or_travel_intent(latest_request, current_schedule)

    def _deterministic_fallback_disabled_reason(
        self,
        latest_request: str,
        current_schedule: Optional[Dict[str, Any]],
        *,
        explicit_disable: bool = False,
        fallback_reason: Optional[str] = None,
    ) -> Optional[str]:
        if explicit_disable:
            return fallback_reason or "complex_or_travel_intent"
        if self._is_strict_atomic_command(latest_request):
            return None
        if self._request_is_complex_or_travel_intent(latest_request, current_schedule):
            return "complex_or_travel_intent"
        return "not_strict_atomic_command"

    def _request_is_complex_or_travel_intent(
        self,
        latest_request: str,
        current_schedule: Optional[Dict[str, Any]] = None,
    ) -> bool:
        clean = clean_title(latest_request or "")
        if not clean:
            return False
        if self._request_is_single_edit_command(latest_request):
            return False
        preferences = (current_schedule or {}).get("preferences") or {}
        if bool((current_schedule or {}).get("travel_intent") or preferences.get("travel_intent")):
            return True
        if clean.startswith("plan my day") or "plan my day" in clean:
            return True
        if detect_travel_intent(clean):
            return True
        if re.search(r"\bi have\b.*\band\b", clean) and len(clean.split()) > 14:
            return True
        if len(re.findall(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", clean)) >= 2:
            return True
        if len(re.findall(r"[,;]", latest_request or "")) >= 2:
            return True
        if len(clean.split()) > 22:
            return True
        activity_mentions = len(re.findall(
            r"\b(?:appointment|doctor|dentist|meeting|lunch|dinner|gym|workout|shopping|grocery|groceries|pharmacy|work|documents?|coffee|class|seminar)\b",
            clean,
        ))
        return activity_mentions >= 3

    def _is_strict_atomic_command(self, latest_request: str) -> bool:
        clean = clean_title(latest_request or "")
        if not clean or len(clean.split()) > 12:
            return False
        if len(re.findall(r"[,;]", latest_request or "")) > 0:
            return False
        if len(re.findall(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", clean)) > 1:
            return False
        if clean.startswith("plan my day") or " i have " in f" {clean} ":
            return False
        atomic_patterns = (
            r"^(?:move|shift|change|update)\s+.+\s+to\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?$",
            r"^(?:move|shift|change|update)\s+.+\s+(?:right\s+after|right\s+before|after|before)\s+.+$",
            r"^(?:remove|delete|cancel)\s+.+$",
            r"^set\s+.+\s+priority\s+(?:low|medium|high)$",
            r"^(?:put|place|arrange|rearrange)\s+.+\s+(?:right\s+after|right\s+before|after|before)\s+.+$",
            r"^(?:add|schedule)\s+.+\s+(?:right\s+after|right\s+before|after|before)\s+.+$",
            r"^(?:add|schedule)\s+(?:a\s+|an\s+)?\d{1,3}[-\s]*(?:minute|min|hour|hr).+$",
        )
        return any(re.search(pattern, clean) for pattern in atomic_patterns)

    def _module_a_parse_rejection_reason(
        self,
        parsed: Dict[str, Any],
        latest_request: str,
    ) -> Optional[str]:
        if not self._request_is_complex_or_travel_intent(latest_request, None):
            return None
        if self._request_is_single_edit_command(latest_request):
            return None
        operations = [
            item for item in list(parsed.get("operations") or []) + list(parsed.get("activities") or [])
            if isinstance(item, dict) and clean_title(item.get("op") or "add") not in {"remove", "shift_plan_date"}
        ]
        if len(operations) == 1 and self._expected_multi_activity_count(latest_request) >= 3:
            return "expected_multi_activity_got_one"
        if self._missing_expected_operation_concepts(operations, latest_request):
            return "missing_expected_operations"
        for operation in operations:
            title = str(operation.get("title") or operation.get("target_title") or "")
            if self._operation_title_looks_like_giant_prompt(title, latest_request):
                return "giant_title_or_complex_fallback"
        return None

    def _request_is_single_edit_command(self, latest_request: str) -> bool:
        clean = clean_title(latest_request or "")
        if not clean:
            return False
        if re.match(r"^(?:move|shift|change|update|put|place|arrange|rearrange|set)\b", clean):
            return True
        return bool(re.search(r"\b(?:move|shift|change|update)\s+.+\s+(?:after|before|to)\b", clean))

    def _expected_multi_activity_count(self, latest_request: str) -> int:
        clean = clean_title(latest_request or "")
        matches = re.findall(
            r"\b(?:appointment|doctor|dentist|meeting|lunch|dinner|gym|workout|shopping|grocery|groceries|pharmacy|work|documents?|coffee|class|seminar)\b",
            clean,
        )
        return len(matches)

    def _missing_expected_operation_concepts(
        self,
        operations: List[Dict[str, Any]],
        latest_request: str,
    ) -> List[str]:
        expected = self._expected_operation_concepts(latest_request)
        if len(expected) < 3:
            return []
        missing: List[str] = []
        for label, aliases in expected:
            if not any(self._operation_matches_expected_concept(operation, aliases) for operation in operations):
                missing.append(label)
        return missing

    def _expected_operation_concepts(self, latest_request: str) -> List[Tuple[str, Tuple[str, ...]]]:
        clean = clean_title(latest_request or "")
        concepts: List[Tuple[str, Tuple[str, ...]]] = []
        seen: set[str] = set()

        def add(label: str, aliases: Tuple[str, ...]) -> None:
            key = clean_title(label)
            if key not in seen:
                seen.add(key)
                concepts.append((label, tuple(clean_title(alias) for alias in aliases if alias)))

        if re.search(r"\bprepare\b.{0,30}\bdocuments?\b|\bdocuments?\b.{0,30}\bprepare\b", clean):
            add("Prepare documents", ("prepare documents", "document preparation", "prepare", "documents", "document", "paperwork", "doc"))
        if re.search(r"\bdoctor\b|\bmedical\b", clean):
            add("Doctor appointment", ("doctor appointment", "doctor", "medical appointment", "medical", "clinic", "hospital", "dentist", "dental", "physician", "checkup", "check-up", "sunway medical"))
        elif re.search(r"\bappointment\b", clean):
            add("Appointment", ("appointment", "appt", "session"))
        if re.search(r"\blunch\b", clean):
            add("Lunch meeting", ("lunch meeting", "lunch", "meal", "eat", "dining"))
        if re.search(r"\bfocused work\b|\bfocus work\b|\bdeep work\b", clean):
            add("Focused work", ("focused work", "focus work", "deep work", "work", "study", "code", "coding", "writing", "research"))
        if re.search(r"\bgocery\b|\bgrocery\b|\bgroceries\b", clean):
            add("Grocery shopping", ("grocery shopping", "grocery run", "grocery", "groceries", "supermarket", "shopping", "mart", "buy food", "grocery store", "dpulze"))
        if re.search(r"\bpharmacy\b", clean):
            add("Pharmacy stop", ("pharmacy stop", "pharmacy", "medicine", "chemist", "drugstore", "prescriptions", "selcare"))
        if re.search(r"\bgym\b|\bworkout\b", clean):
            add("Gym", ("gym", "workout", "fitness", "exercise", "training", "workout session"))
        if re.search(r"\bdinner\b", clean):
            add("Dinner with family", ("dinner with family", "dinner with parents", "dinner", "family dinner", "eat with family"))
        if re.search(r"\bclient meeting\b", clean):
            add("Client meeting", ("client meeting", "meeting", "discussion", "client"))
        elif re.search(r"\bteam meeting\b", clean):
            add("Team meeting", ("team meeting", "meeting", "sync", "discussion"))
        elif re.search(r"\bmeeting\b", clean) and not re.search(r"\blunch meeting\b", clean):
            add("Meeting", ("meeting", "discussion", "talk"))
        return concepts

    def _operation_matches_expected_concept(
        self,
        operation: Dict[str, Any],
        aliases: Tuple[str, ...],
    ) -> bool:
        title = clean_title(operation.get("title") or operation.get("target_title") or "")
        evidence = clean_title(
            " ".join(
                str(operation.get(field) or "")
                for field in (
                    "title",
                    "target_title",
                    "location_category",
                    "no_location_reason",
                    "raw_location_text",
                )
            )
        )
        for alias in aliases:
            if not alias:
                continue
            if alias in evidence or (title and title in alias):
                return True
        return False

    def _operation_title_looks_like_giant_prompt(self, title: str, latest_request: str) -> bool:
        clean_title_value = clean_title(title or "")
        if len(title or "") > 80:
            return True
        if re.search(r"\b(?:plan my day|i have|i still need|please make it realistic)\b", clean_title_value):
            return True
        if len(re.findall(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", clean_title_value)) >= 2:
            return True
        activity_mentions = len(re.findall(
            r"\b(?:appointment|doctor|dentist|meeting|lunch|dinner|gym|shopping|grocery|pharmacy|documents?)\b",
            clean_title_value,
        ))
        if activity_mentions >= 3:
            return True
        request_clean = clean_title(latest_request or "")
        return bool(clean_title_value and len(clean_title_value) > 40 and clean_title_value in request_clean)

    def _looks_like_incomplete_json(self, raw_response_text: Optional[str]) -> bool:
        text = (raw_response_text or "").strip()
        if not text:
            return False
        clean_text = text.replace("```json", "").replace("```", "").strip()
        if not clean_text.startswith("{"):
            return False
        if not clean_text.endswith("}"):
            return True
        return clean_text.count("{") > clean_text.count("}") or clean_text.count("[") > clean_text.count("]")

    def _generate_parser_content_with_retry(self, contents: Any) -> Any:
        total_started = time.perf_counter()
        deadline = total_started + max(0.1, MODULE_A_LLM_TOTAL_TIMEOUT_SECONDS)
        max_retry_count = MODULE_A_LLM_RETRY_COUNT if MODULE_A_LLM_ENABLE_RETRY else 0
        attempt = 1
        last_error: Optional[Exception] = None

        while attempt <= max_retry_count + 1:
            remaining_seconds = deadline - time.perf_counter()
            if remaining_seconds <= 0:
                jlog("MODULE_A", "reason=timeout_total_budget_exceeded", "FAIL")
                raise ModuleALLMTotalBudgetExceededError("timeout_total_budget_exceeded")

            timeout_seconds = min(MODULE_A_LLM_TIMEOUT_SECONDS, remaining_seconds)
            try:
                response = self._generate_parser_content_once(
                    contents=contents,
                    model=MODULE_A_PRIMARY_MODEL,
                    timeout_seconds=timeout_seconds,
                )
                jlog("TIMER", f"module_a_llm_seconds={time.perf_counter() - total_started:.2f}", None)
                if attempt > 1:
                    jlog("MODULE_A", "success on retry", "LLM_RETRY")
                return response
            except ModuleALLMExecutorSaturatedError:
                jlog("MODULE_A", "reason=executor_saturated", "FAIL")
                raise
            except ModuleALLMTimeoutError as exc:
                last_error = exc
                if attempt <= max_retry_count and self._has_retry_budget(deadline):
                    attempt += 1
                    remaining_ms = int(max(0.0, deadline - time.perf_counter()) * 1000)
                    jlog("MODULE_A", f"attempt={attempt} remaining_budget_ms={remaining_ms}", "RETRY")
                    continue
                break
            except Exception as exc:
                last_error = exc
                if self._is_transient_llm_error(exc) and attempt <= max_retry_count and self._has_retry_budget(deadline):
                    reason = self._transient_error_label(exc)
                    remaining_ms = int(max(0.0, deadline - time.perf_counter()) * 1000)
                    jlog("MODULE_A", f"attempt {attempt}/{max_retry_count + 1} after {reason}", "LLM_RETRY")
                    jlog("MODULE_A", f"attempt={attempt + 1} remaining_budget_ms={remaining_ms} after {reason}", "RETRY")
                    delay = PARSER_RETRY_DELAYS_SECONDS[min(attempt - 1, len(PARSER_RETRY_DELAYS_SECONDS) - 1)]
                    if delay > 0:
                        remaining_before_sleep = max(0.0, deadline - time.perf_counter())
                        reserved_for_next_attempt = max(0.02, min(MODULE_A_LLM_TIMEOUT_SECONDS, remaining_before_sleep))
                        safe_delay = min(delay, max(0.0, remaining_before_sleep - reserved_for_next_attempt))
                        if safe_delay > 0:
                            time.sleep(safe_delay)
                    if not self._has_retry_budget(deadline):
                        break
                    attempt += 1
                    continue
                break

        fallback_model = MODULE_A_LLM_FALLBACK_MODEL
        if fallback_model and self._has_retry_budget(deadline):
            remaining_seconds = deadline - time.perf_counter()
            timeout_seconds = min(MODULE_A_LLM_FALLBACK_TIMEOUT_SECONDS, remaining_seconds)
            jlog("MODULE_A", f"model={fallback_model} timeout={int(timeout_seconds * 1000)}ms", "FALLBACK_MODEL")
            try:
                response = self._generate_parser_content_once(
                    contents=contents,
                    model=fallback_model,
                    timeout_seconds=timeout_seconds,
                )
                jlog("TIMER", f"module_a_llm_seconds={time.perf_counter() - total_started:.2f}", None)
                return response
            except Exception as exc:
                last_error = exc
                jlog("MODULE_A", "reason=fallback_failed", "FAIL")

        if deadline - time.perf_counter() <= 0:
            jlog("MODULE_A", "reason=timeout_total_budget_exceeded", "FAIL")
            raise ModuleALLMTotalBudgetExceededError("timeout_total_budget_exceeded")
        if isinstance(last_error, ModuleALLMTimeoutError):
            jlog("MODULE_A", "fallback=parser_busy", "TIMEOUT")
            raise last_error
        if last_error:
            raise last_error
        jlog("MODULE_A", "reason=timeout_total_budget_exceeded", "FAIL")
        raise ModuleALLMTotalBudgetExceededError("timeout_total_budget_exceeded")

    def _has_retry_budget(self, deadline: float) -> bool:
        return (deadline - time.perf_counter()) > 0.005

    def _generate_parser_content_once(self, contents: Any, model: str, timeout_seconds: float) -> Any:
        if timeout_seconds <= 0:
            jlog("MODULE_A", "reason=timeout_total_budget_exceeded", "FAIL")
            raise ModuleALLMTotalBudgetExceededError("timeout_total_budget_exceeded")

        jlog_verbose("MODULE_A", f"start timeout={int(timeout_seconds * 1000)}ms", "LLM")
        acquired = MODULE_A_LLM_SEMAPHORE.acquire(blocking=False)
        if not acquired:
            raise ModuleALLMExecutorSaturatedError("executor_saturated")

        try:
            future = MODULE_A_LLM_EXECUTOR.submit(self._call_module_a_llm, contents, model)
        except Exception:
            MODULE_A_LLM_SEMAPHORE.release()
            raise

        future.add_done_callback(lambda _future: MODULE_A_LLM_SEMAPHORE.release())
        try:
            return future.result(timeout=timeout_seconds)
        except FutureTimeoutError as exc:
            future.cancel()
            raise ModuleALLMTimeoutError("module_a_timeout") from exc

    def _call_module_a_llm(self, contents: Any, model: str) -> Any:
        return self.client.models.generate_content(
            model=model,
            contents=contents,
            config={
                "response_mime_type": "application/json",
                "temperature": 0,
                "max_output_tokens": MODULE_A_MAX_OUTPUT_TOKENS,
            },
        )

    def _is_transient_llm_error(self, exc: Exception) -> bool:
        message = str(exc).lower()
        transient_markers = (
            "503",
            "unavailable",
            "deadline exceeded",
            "timeout",
            "timed out",
            "temporarily overloaded",
            "temporary",
            "high demand",
        )
        return any(marker in message for marker in transient_markers)

    def _transient_error_label(self, exc: Exception) -> str:
        message = str(exc)
        if "503" in message:
            return "503"
        if "UNAVAILABLE" in message.upper():
            return "UNAVAILABLE"
        if "deadline" in message.lower():
            return "deadline exceeded"
        if "timeout" in message.lower() or "timed out" in message.lower():
            return "timeout"
        return "transient error"

    def _module_a_failure_type(self, exc: Exception) -> str:
        if isinstance(exc, ModuleALLMExecutorSaturatedError):
            return "module_a_executor_saturated"
        if isinstance(exc, (ModuleALLMTimeoutError, ModuleALLMTotalBudgetExceededError)):
            return "module_a_timeout"
        if self._is_transient_llm_error(exc):
            return "module_a_unavailable"
        return "llm_call_error"

    def _build_parser_prompt(
        self,
        latest_request: str,
        history: List[Dict[str, Any]],
        current_schedule: Optional[Dict[str, Any]],
        saved_locations: Optional[List[Dict[str, Any]]] = None,
        compact_output: bool = False,
    ) -> str:
        history_lines = self._summarize_history(history)
        local_context = self._local_datetime_context()
        current_activity_index = self._build_current_activity_index(current_schedule)
        saved_location_index = self._build_saved_location_index(saved_locations or [])
        schedule_date = (current_schedule or {}).get("date") or "(none)"
        compact_rules = self._compact_module_a_output_rules(latest_request) if compact_output else ""
        return (
            f"{PARSER_PROMPT}\n\n"
            f"{compact_rules}"
            f"{local_context}\n"
            f"CURRENT_SCHEDULE_DATE: {schedule_date}\n"
            f"CURRENT_ACTIVITY_INDEX:\n{current_activity_index}\n"
            f"SAVED_LOCATION_INDEX:\n{saved_location_index}\n"
            f"HISTORY:\n" + ("\n".join(history_lines) if history_lines else "(none)") + "\n\n"
            f"LATEST_REQUEST:\n{latest_request}\n"
        )

    def _compact_module_a_output_rules(self, latest_request: str) -> str:
        expected = [label for label, _aliases in self._expected_operation_concepts(latest_request)]
        expected_line = ", ".join(expected) if expected else "(derive from latest request)"
        return (
            "COMPACT_OUTPUT_MODE:\n"
            "- Return compact JSON only. No markdown, no explanation.\n"
            "- Top-level keys: intent, date, operations, and preferences only if needed.\n"
            "- Do not output reply, transcription, conflict_analysis, or the full user message.\n"
            "- For each operation always include only: op, title, timing_mode, duration_minutes.\n"
            "- Include fixed_start/fixed_end only for fixed events; anchor_relation only for relative events; preferred_time_window only when semantically important.\n"
            "- Include location semantic fields only when needed: raw_location_text, location_kind, location_category, travel_required, location_resolution_status, no_location_reason.\n"
            "- Omit null fields, location=null, explicit_user_location=false, needs_clarification=false, sequence_index, location_warning, and high-confidence semantic_confidence/location_confidence.\n"
            "- Omit parse_notes unless confidence is low or clarification is needed; if included, keep it under 12 words.\n"
            f"- Ensure operations include these expected activity concepts when present: {expected_line}.\n\n"
        )

    def _build_current_activity_index(self, current_schedule: Optional[Dict[str, Any]]) -> str:
        lines: List[str] = []
        for item in (current_schedule or {}).get("activities", []):
            if item.get("type") not in [None, "activity"]:
                continue
            title = item.get("title", "Untitled")
            start = item.get("startTime", "??:??")
            end = item.get("endTime", "??:??")
            location = item.get("location_label") or item.get("location") or "?"
            timing_mode = item.get("timing_mode") or "?"
            priority = item.get("priority") or "medium"
            duration = item.get("duration_minutes") or item.get("duration") or "?"
            stable_id = item.get("stable_activity_id") or item.get("id") or "?"
            fixed_status = "user_fixed" if item.get("is_user_fixed") or item.get("fixed_start") is not None else "movable"
            resolved_location = item.get("resolved_location") if isinstance(item.get("resolved_location"), dict) else {}
            latitude = resolved_location.get("latitude") or resolved_location.get("lat") or item.get("latitude") or item.get("lat")
            longitude = resolved_location.get("longitude") or resolved_location.get("lng") or item.get("longitude") or item.get("lng")
            coordinates = f"{latitude},{longitude}" if latitude is not None and longitude is not None else "no_coordinates"
            lines.append(
                f"- activity_id={stable_id} | title={title} | time={start}-{end} | duration={duration} min | "
                f"priority={priority} | location={location} | coordinates={coordinates} | timing={timing_mode} | {fixed_status}"
            )
        return "\n".join(lines) if lines else "(none)"

    def _build_saved_location_index(self, saved_locations: List[Dict[str, Any]]) -> str:
        lines: List[str] = []
        for item in (saved_locations or [])[:12]:
            if not isinstance(item, dict):
                continue
            label = item.get("label") or "?"
            category = item.get("category") or "?"
            display = item.get("display_name") or item.get("address") or "?"
            display = re.sub(r"\s+", " ", str(display)).strip()
            if len(display) > 80:
                display = display[:77].rstrip() + "..."
            lines.append(f"- {label} | {category} | {display}")
        return "\n".join(lines) if lines else "(none)"

    def _summarize_history(self, history: List[Dict[str, Any]]) -> List[str]:
        trimmed = history[-(MAX_HISTORY_TURNS * 2):]
        lines: List[str] = []
        for item in trimmed:
            role = "User" if item.get("role") == "user" else "Assistant"
            message = re.sub(r"\s+", " ", str(item.get("message") or "").strip())
            if not message:
                continue
            if role == "Assistant" and (
                "i created a structured draft from your request" in message.lower()
                or "i couldn't parse that request into a schedule change" in message.lower()
            ):
                continue
            if len(message) > MAX_HISTORY_MESSAGE_CHARS:
                message = message[: MAX_HISTORY_MESSAGE_CHARS - 3].rstrip() + "..."
            lines.append(f"{role}: {message}")
        return lines

    def _invalid_llm_parse(
        self,
        latest_request: str,
        current_schedule: Optional[Dict[str, Any]],
        raw_llm_reply: Optional[str],
        failure_type: str,
        failure_message: str,
        raw_response_text: Optional[str],
    ) -> Dict[str, Any]:
        reply = raw_llm_reply or MODULE_A_PARSER_BUSY_REPLY
        return {
            "intent": "chat",
            "reply": reply,
            "transcription": latest_request,
            "date": (current_schedule or {}).get("date") or self._local_today_iso(),
            "preferences": {},
            "activities": [],
            "operations": [],
            "_reply_source": failure_type,
            "_llm_reply": raw_llm_reply,
            "_failure_type": failure_type,
            "_failure_message": failure_message,
            "_raw_response_text": raw_response_text,
        }

    def _base_year_for_date_parse(self, current_schedule: Optional[Dict[str, Any]]) -> int:
        schedule_date = (current_schedule or {}).get("date")
        if schedule_date:
            try:
                return int(str(schedule_date).split("-", 1)[0])
            except Exception:
                pass
        return self._local_now().year

    def _extract_explicit_absolute_date(
        self,
        request_text: str,
        current_schedule: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        matches = self._extract_explicit_absolute_dates(request_text, current_schedule)
        return matches[-1] if matches else None

    def _extract_explicit_absolute_dates(
        self,
        request_text: str,
        current_schedule: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        text = request_text or ""
        month_pattern = "|".join(sorted(MONTH_NAME_TO_NUMBER.keys(), key=len, reverse=True))
        patterns = [
            re.compile(
                rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<month>{month_pattern})(?:\s*,?\s*(?P<year>\d{{4}}))?\b",
                re.IGNORECASE,
            ),
            re.compile(
                rf"\b(?P<month>{month_pattern})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:\s*,?\s*(?P<year>\d{{4}}))?\b",
                re.IGNORECASE,
            ),
        ]
        matches: List[Tuple[int, str]] = []
        base_year = self._base_year_for_date_parse(current_schedule)
        for pattern in patterns:
            for match in pattern.finditer(text):
                month = MONTH_NAME_TO_NUMBER.get(match.group("month").lower())
                day = int(match.group("day"))
                year = int(match.group("year") or base_year)
                try:
                    parsed_date = date(year, month, day).isoformat()
                except Exception:
                    continue
                matches.append((match.start(), parsed_date))
        if not matches:
            return []
        matches.sort(key=lambda item: item[0])
        return [item[1] for item in matches]

    def _apply_deterministic_shift_date_override(
        self,
        parsed: Dict[str, Any],
        latest_request: str,
        current_schedule: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        target_date = self._extract_explicit_absolute_date(latest_request, current_schedule)
        if not target_date:
            return parsed

        candidate = deepcopy(parsed)
        candidate["date"] = target_date
        if not self._request_implies_whole_plan_shift(latest_request, current_schedule, candidate):
            return parsed

        previous_date = parsed.get("date")
        if previous_date and previous_date != target_date:
            self._debug(f"[DATE_NORMALIZE] Deterministic shift date override: {previous_date} -> {target_date}")
        candidate["date"] = target_date
        for operation in candidate.get("operations") or []:
            if clean_title(operation.get("op") or "") == "shift_plan_date":
                previous_to_date = operation.get("to_date")
                if previous_to_date and previous_to_date != target_date:
                    self._debug(f"[DATE_NORMALIZE] Deterministic shift operation override: {previous_to_date} -> {target_date}")
                operation["to_date"] = target_date
        return candidate

    def _deterministic_fallback_parse(
        self,
        latest_request: str,
        current_schedule: Optional[Dict[str, Any]],
        history: Optional[List[Dict[str, Any]]] = None,
        saved_locations: Optional[List[Dict[str, Any]]] = None,
        reply_source: str = "deterministic_fallback",
        failure_type: str = "llm_fallback_parse",
    ) -> Optional[Dict[str, Any]]:
        request = re.sub(r"\s+", " ", latest_request or "").strip()
        clean_request = clean_title(request)
        schedule_date = (current_schedule or {}).get("date") or self._local_today_iso()
        parsed: Optional[Dict[str, Any]] = None

        target_date = self._extract_explicit_absolute_date(request, current_schedule)
        if not target_date and "tomorrow" in clean_request and self._request_text_implies_whole_plan_shift(clean_request):
            base_date = (current_schedule or {}).get("date") or self._local_today_iso()
            try:
                target_date = (date.fromisoformat(str(base_date)) + timedelta(days=1)).isoformat()
            except Exception:
                target_date = (self._local_now().date() + timedelta(days=1)).isoformat()
        if target_date and self._request_text_implies_whole_plan_shift(clean_request):
            parsed = {
                "intent": "edit",
                "reply": f"I found the target date and will move the whole plan to {target_date}.",
                "transcription": request,
                "date": target_date,
                "operations": [],
                "activities": [],
                "preferences": {},
            }

        if parsed is None:
            parsed = self._fallback_parse_optimize_request(request, schedule_date, current_schedule)

        if parsed is None:
            parsed = self._fallback_parse_arrange_relation(request, schedule_date, current_schedule)

        if parsed is None:
            parsed = self._fallback_parse_swap_order(request, schedule_date, current_schedule)

        if parsed is None:
            parsed = self._fallback_parse_soft_adjustment(request, schedule_date, current_schedule, history or [])

        if parsed is None:
            parsed = self._fallback_parse_relative_add(request, schedule_date, current_schedule)

        if parsed is None:
            parsed = self._fallback_parse_fixed_time_update(request, schedule_date, current_schedule, history or [])

        if parsed is None:
            parsed = self._fallback_parse_remove(request, schedule_date)

        if parsed is None:
            parsed = self._fallback_parse_priority_update(request, schedule_date, current_schedule)

        if parsed is None:
            return None

        parsed["_reply_source"] = reply_source
        parsed["_llm_reply"] = None
        parsed["_used_llm"] = False
        parsed["_failure_type"] = failure_type
        stage = "FAST_PATH" if reply_source == "deterministic_fast_path" else "LLM_FALLBACK_PARSE"
        message = (
            "Used deterministic fast-path parser for simple request"
            if stage == "FAST_PATH"
            else "Used deterministic fallback parser for simple request"
        )
        jlog("MODULE_A", message, stage)
        parsed = self._normalize_plan_level_operations(parsed, latest_request, current_schedule)
        parsed = self._normalize_parsed_locations(parsed, latest_request, saved_locations or [])
        return parsed

    def parse_deterministic_fast_path(
        self,
        latest_request: str,
        current_schedule: Optional[Dict[str, Any]],
        history: Optional[List[Dict[str, Any]]] = None,
        saved_locations: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        started = time.perf_counter()
        self._last_fast_path_fallback_reason = None
        pre_skip_reason = self._fast_path_natural_edit_skip_reason(latest_request)
        if pre_skip_reason:
            schedule_date = (current_schedule or {}).get("date") or self._local_today_iso()
            if not self._fallback_parse_optimize_request(latest_request, schedule_date, current_schedule):
                self._last_fast_path_fallback_reason = pre_skip_reason
                jlog("FAST_PATH", f"reason={pre_skip_reason}", "SKIP_TO_MODULE_A")
                jlog("TIMER", f"module_a_fast_path_seconds={time.perf_counter() - started:.2f}", None)
                return None
        parsed = self._deterministic_fallback_parse(
            latest_request=latest_request,
            current_schedule=current_schedule,
            history=history or [],
            saved_locations=saved_locations or [],
            reply_source="deterministic_fast_path",
            failure_type="deterministic_fast_path",
        )
        fallback_reason = self._fast_path_relative_safety_fallback_reason(latest_request, parsed, current_schedule)
        if fallback_reason:
            self._last_fast_path_fallback_reason = fallback_reason
            jlog("FAST_PATH", f"reason={fallback_reason}", "SKIP_TO_MODULE_A")
            jlog("TIMER", f"module_a_fast_path_seconds={time.perf_counter() - started:.2f}", None)
            return None
        jlog("TIMER", f"module_a_fast_path_seconds={time.perf_counter() - started:.2f}", None)
        if parsed:
            jlog("MODULE_A", "deterministic_fast_path matched simple pattern", "FAST_PATH")
        return parsed

    def _fallback_parse_optimize_request(
        self,
        request: str,
        schedule_date: str,
        current_schedule: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        text = clean_title(request)
        refit_unfit_request = self._is_refit_unfit_request(text, current_schedule)
        if not (
            re.search(r"\b(?:optimi[sz]e|regenerate|rebuild)\b.*\b(?:schedule|plan|day)\b|\bmake\s+(?:the\s+)?(?:schedule|plan|day)\s+better\b", text)
            or refit_unfit_request
        ):
            return None
        return {
            "intent": "edit",
            "reply": "I understood this as trying to fit the current schedule items." if refit_unfit_request else "I understood this as optimizing the current schedule.",
            "transcription": request,
            "date": schedule_date,
            "operations": [{
                "op": "optimize_schedule",
                "scope": "active_schedule",
            }],
            "activities": [],
            "preferences": {
                "refinement_reason": "explicit_optimize",
            },
        }

    def _is_schedule_change_intent(self, parsed: Dict[str, Any]) -> bool:
        intent = clean_title(parsed.get("intent") or "")
        if intent in {"edit", "add", "move", "schedule", "create", "update"}:
            return True
        request = clean_title(parsed.get("transcription") or "")
        return bool(re.search(r"\b(move|shift|change|update|add|remove|delete|reschedule)\b", request))

    def _normalize_module_a_duplicate_relative_adds(
        self,
        parsed: Dict[str, Any],
        current_schedule: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if parsed.get("_reply_source") != "llm":
            return parsed
        pool = self._active_resolution_pool(current_schedule)
        if not pool:
            return parsed

        normalized = deepcopy(parsed)
        request_text = clean_title(normalized.get("transcription") or "")
        if re.search(r"\b(?:another|new|second|additional)\b", request_text):
            return normalized

        for operation in normalized.get("operations") or []:
            if clean_title(operation.get("op") or "add") != "add":
                continue
            has_relation_intent = bool(operation.get("anchor_relation")) or clean_title(operation.get("timing_mode") or "") == TimingMode.RELATIVE
            if not has_relation_intent:
                continue
            title = operation.get("title") or operation.get("target_title")
            resolution = self._resolve_activity_reference(title, pool)
            if resolution.get("status") != "resolved" or resolution.get("target_resolution_confidence") != "high":
                continue

            target = resolution.get("activity") or {}
            target_id = target.get("stable_activity_id") or target.get("id")
            operation["op"] = "update"
            operation["title"] = target.get("title") or title
            operation["target_title"] = target.get("title") or title
            operation["target_id"] = target_id
            operation["activity_id"] = target_id
            operation["preserve_existing_fields"] = True
            operation["_preserve_resolved_title"] = True
            operation["_duplicate_safety_converted_add_to_update"] = True
            if operation.get("raw_llm_location") is None and not operation.get("explicit_user_location"):
                for field in (
                    "location",
                    "location_label",
                    "location_category",
                    "location_status",
                    "location_source",
                    "location_confidence",
                    "location_normalized",
                    "location_warning",
                    "travel_required",
                ):
                    operation.pop(field, None)
            jlog(
                "MODULE_A",
                f"converted add->update title={operation.get('title')} reason=existing_relative_target",
                "DUPLICATE_SAFETY",
            )
        return normalized

    def _active_resolution_pool(self, current_schedule: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        activities = [
            item for item in (current_schedule or {}).get("activities", [])
            if isinstance(item, dict) and item.get("title")
        ]
        return [
            {
                **item,
                "status": item.get("status") or "active",
                "normalized_title": item.get("normalized_title") or clean_title(item.get("title") or ""),
                "aliases": item.get("aliases") or self._generate_aliases(item.get("title") or ""),
            }
            for item in activities
        ]

    def _resolve_fast_path_title(
        self,
        raw_title: str,
        current_schedule: Optional[Dict[str, Any]],
    ) -> Tuple[str, bool]:
        title = self._clean_fallback_activity_title(raw_title)
        pool = self._active_resolution_pool(current_schedule)
        if not title or not pool:
            return title, False
        resolution = self._resolve_activity_reference(title, pool)
        if resolution.get("status") == "resolved" and resolution.get("target_resolution_confidence") == "high":
            return resolution["activity"].get("title") or title, True
        return title, False

    def _fast_path_has_rationale_clause(self, text: str) -> bool:
        return bool(re.search(
            r"\b(?:because|cause|cuz|since|so\s+that|so\s+i\s+(?:do\s+not|don'?t|dont)|to\s+avoid)\b",
            text or "",
            flags=re.IGNORECASE,
        ))

    def _fast_path_has_wrapper_title(self, text: str) -> bool:
        return bool(re.match(
            r"^(?:i want|i need|i would like|can you|please)\b",
            clean_title(text or ""),
        ))

    def _fast_path_natural_edit_skip_reason(self, latest_request: str) -> Optional[str]:
        text = latest_request or ""
        clean = clean_title(text)
        if not clean:
            return None

        # Aggressively reject natural phrasing, soft adjustments, or complaints
        if re.search(r"\b(?:earlier|early|later|late|too\s+late|not\s+so\s+late|a\s+bit\s+earlier|a\s+little\s+earlier|sooner)\b", clean):
            return "natural_edit_wording"
            
        if self._fast_path_has_rationale_clause(text):
            return "natural_edit_wording"
        if re.match(
            r"^(?:how\s+about|what\s+about|maybe|what\s+if|i\s+was\s+thinking|i\s+think\s+maybe|"
            r"would\s+it\s+be\s+better|would\s+it\s+help|i\s+want|i\s+need|i\s+would\s+like|can\s+we|could\s+we)\b",
            clean,
        ):
            return "natural_edit_wording"

        has_relation = bool(re.search(r"\b(?:after|before|right\s+after|right\s+before)\b", clean))
        has_activity = bool(re.search(r"\b(?:lunch|dinner|breakfast|coffee|meeting|seminar|class|gym|workout|shopping|grocery|groceries|fyp|study|project)\b", clean))
        if not (has_relation and has_activity):
            return None

        explicit_clean_command = bool(re.match(
            r"^(?:(?:arrange|rearrange|put|place|make|set)\b|(?:add|schedule)\b|(?:move|shift|change|update)\b|(?:remove|delete|cancel)\b)",
            clean,
        ))
        if len(clean.split()) > 14 and not explicit_clean_command:
            return "natural_edit_wording"
        return None

    def _fast_path_relative_safety_fallback_reason(
        self,
        latest_request: str,
        parsed: Optional[Dict[str, Any]],
        current_schedule: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        if not parsed:
            return None
        relative_ops = [
            operation for operation in (parsed.get("operations") or [])
            if isinstance(operation, dict) and operation.get("anchor_relation")
        ]
        if not relative_ops:
            return None

        if self._fast_path_has_rationale_clause(latest_request):
            return "natural_edit_wording"

        pool = self._active_resolution_pool(current_schedule)
        for operation in relative_ops:
            title = str(operation.get("title") or operation.get("target_title") or "")
            anchor = operation.get("anchor_relation") or {}
            anchor_title = str(anchor.get("target_title") or "")
            if self._fast_path_has_wrapper_title(title) or self._fast_path_has_rationale_clause(anchor_title):
                return "unclean_target_or_anchor"

            if clean_title(operation.get("op") or "") in {"update", "move"} and pool:
                target_resolution = self._resolve_activity_reference(title, pool)
                anchor_resolution = self._resolve_activity_reference(anchor_title, pool)
                target_clean = (
                    target_resolution.get("status") == "resolved"
                    and target_resolution.get("target_resolution_confidence") == "high"
                )
                anchor_clean = (
                    anchor_resolution.get("status") == "resolved"
                    and anchor_resolution.get("target_resolution_confidence") == "high"
                )
                if not (target_clean and anchor_clean):
                    return "unclean_target_or_anchor"
        return None

    def _extract_fast_path_duration(self, value: str) -> Tuple[Optional[int], Optional[str]]:
        text = value or ""
        half_match = re.search(r"\b(?:half[-\s]*hour|half\s+an\s+hour)\b", text, flags=re.IGNORECASE)
        if half_match:
            raw = half_match.group(0)
            jlog("FAST_PATH", f'raw="{raw}" duration_minutes=30', "DURATION")
            return 30, raw
        match = re.search(
            r"\b(?:for\s+)?(?P<amount>\d{1,3})\s*(?:-| )?\s*(?P<unit>minutes?|mins?|min|hours?|hrs?|hr)\b",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            return None, None
        amount = int(match.group("amount"))
        unit = clean_title(match.group("unit"))
        minutes = amount * 60 if unit in {"hour", "hours", "hr", "hrs"} else amount
        raw = match.group(0)
        jlog("FAST_PATH", f'raw="{raw}" duration_minutes={minutes}', "DURATION")
        return minutes, raw

    def _remove_fast_path_duration_phrase(self, value: str) -> str:
        text = value or ""
        text = re.sub(
            r"\b(?:for\s+)?\d{1,3}\s*(?:-| )?\s*(?:minutes?|mins?|min|hours?|hrs?|hr)\b",
            " ",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"\b(?:half[-\s]*hour|half\s+an\s+hour)\b", " ", text, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", text).strip()

    def _clean_fast_path_new_activity_title(self, value: str) -> str:
        title = self._clean_fallback_activity_title(value)
        if not title:
            return ""
        if clean_title(title) == "coffee break":
            return "Coffee Break"
        if " " in title or "-" in title:
            title = title[:1].upper() + title[1:].lower()
        title = re.sub(r"\bfyp\b", "FYP", title, flags=re.IGNORECASE)
        return title

    def _fallback_parse_arrange_relation(
        self,
        request: str,
        schedule_date: str,
        current_schedule: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        pattern = re.compile(
            r"\b(?:arrange|rearrange|put|place|make|set|move|shift|change|update)\s+(?:the\s+|my\s+)?(?P<title>.+?)\s+"
            r"(?P<kind>after|before|right\s+after|right\s+before)\s+(?:the\s+|my\s+)?(?P<anchor>.+?)(?:\.|$)",
            re.IGNORECASE,
        )
        match = pattern.search(request)
        if not match:
            if re.match(r"^(?:add|schedule)\s+", clean_title(request)):
                return None
            bare = re.search(
                r"\b(?P<title>.+?)\s+(?P<kind>after|before)\s+(?P<anchor>.+?)(?:\.|$)",
                request,
                flags=re.IGNORECASE,
            )
            if not bare:
                return None
            clean_text = clean_title(request)
            if not re.search(r"\b(lunch|dinner|coffee|meeting|seminar|class|gym|workout|shopping|grocery|fyp|study)\b", clean_text):
                return None
            match = bare

        raw_title_text = match.group("title")
        duration_minutes, _duration_raw = self._extract_fast_path_duration(raw_title_text)
        title_without_duration = self._remove_fast_path_duration_phrase(raw_title_text)
        before_title = self._clean_fallback_activity_title(raw_title_text)
        raw_title = self._clean_fallback_activity_title(title_without_duration)
        raw_anchor = self._clean_fallback_activity_title(match.group("anchor"))
        if not raw_title or not raw_anchor:
            return None
        explicit_move = bool(re.match(r"^(?:move|shift|change|update)\b", clean_title(request)))
        title, target_exists = self._resolve_fast_path_title(raw_title, current_schedule)
        if explicit_move and not target_exists:
            return None
        if not target_exists:
            title = self._clean_fast_path_new_activity_title(title_without_duration)
        if before_title and title and before_title != title:
            jlog("FAST_PATH", f'before="{before_title}" after="{title}"', "TITLE_CLEAN")
        anchor, anchor_exists = self._resolve_fast_path_title(raw_anchor, current_schedule)
        if not anchor_exists:
            return None
            
        kind = clean_title(match.group("kind")).replace("right ", "")
        op_type = "update" if target_exists else "add"
        jlog("FAST_PATH", f"parsed arrange_{kind} target={title} anchor={anchor}", None)
        operation = {
            "op": op_type,
            "title": title,
            "timing_mode": TimingMode.RELATIVE,
            "anchor_relation": {"kind": kind, "target_title": anchor},
        }
        if duration_minutes:
            operation["duration_minutes"] = duration_minutes
        return {
            "intent": "edit",
            "reply": f"I understood this as arranging {title} {kind} {anchor}.",
            "transcription": request,
            "date": schedule_date,
            "operations": [operation],
            "activities": [],
            "preferences": {},
        }

    def _fallback_parse_swap_order(
        self,
        request: str,
        schedule_date: str,
        current_schedule: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        patterns = [
            re.compile(r"\b(?:swap|switch)\s+(?:the\s+|my\s+)?(?P<first>.+?)\s+(?:and|with)\s+(?:the\s+|my\s+)?(?P<second>.+?)(?:\.|$)", re.IGNORECASE),
            re.compile(r"\bchange\s+(?:the\s+)?order\s+of\s+(?:the\s+|my\s+)?(?P<first>.+?)\s+and\s+(?:the\s+|my\s+)?(?P<second>.+?)(?:\.|$)", re.IGNORECASE),
        ]
        match = next((pattern.search(request) for pattern in patterns if pattern.search(request)), None)
        if not match:
            return None
        first, first_exists = self._resolve_fast_path_title(match.group("first"), current_schedule)
        second, second_exists = self._resolve_fast_path_title(match.group("second"), current_schedule)
        if not first or not second or not (first_exists and second_exists):
            return None
        pool = self._active_resolution_pool(current_schedule)
        first_activity = self._resolve_activity_reference(first, pool).get("activity")
        second_activity = self._resolve_activity_reference(second, pool).get("activity")
        first_start = (first_activity or {}).get("scheduled_start")
        second_start = (second_activity or {}).get("scheduled_start")
        if first_start is not None and second_start is not None and first_start < second_start:
            operations = [{
                "op": "update",
                "title": first,
                "timing_mode": TimingMode.RELATIVE,
                "anchor_relation": {"kind": "after", "target_title": second},
            }]
        else:
            operations = [{
                "op": "update",
                "title": first,
                "timing_mode": TimingMode.RELATIVE,
                "anchor_relation": {"kind": "before", "target_title": second},
            }]
        jlog("FAST_PATH", f"parsed swap_order first={first} second={second}", None)
        return {
            "intent": "edit",
            "reply": f"I understood this as swapping the order of {first} and {second}.",
            "transcription": request,
            "date": schedule_date,
            "operations": operations,
            "activities": [],
            "preferences": {},
        }

    def _fallback_parse_soft_adjustment(
        self,
        request: str,
        schedule_date: str,
        current_schedule: Optional[Dict[str, Any]] = None,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        clean_request = clean_title(request)
        direction = None
        if re.search(r"\b(earlier|early|too late|not so late|a bit earlier|a little earlier|sooner|bring forward|move forward|forward)\b", clean_request):
            direction = "earlier"
        elif re.search(r"\b(later|too early|delay|postpone|push back|move backward|backward)\b", clean_request):
            direction = "later"
        if not direction:
            return None

        title = None
        for match in re.finditer(r"\b(lunch|dinner|breakfast|coffee(?:\s+break)?|meeting|seminar|class|gym|workout|shopping|grocery|fyp|study|project)\b", clean_request):
            candidate = match.group(0)
            if candidate in {"project"}:
                continue
            title = candidate
            break
        if title:
            resolved, _ = self._resolve_fast_path_title(title, current_schedule)
            title = resolved
        elif re.search(r"\b(it|this|that)\b", clean_request):
            title = self._resolve_pronoun_target_from_context(request, current_schedule, history or [])
            if not title:
                jlog("PRONOUN", f"target=it context=missing valid=false action=clarify", None)
                return {
                    "intent": "no_operation",
                    "reply": f"Which activity do you want to move {direction}?",
                    "transcription": request,
                    "date": schedule_date,
                    "operations": [],
                    "activities": [],
                    "preferences": {},
                    "_failure_type": "pronoun_target_clarification",
                }
        if not title:
            return None

        jlog("FAST_PATH", f"parsed move_{direction} target={title}", None)
        return {
            "intent": "edit",
            "reply": f"I understood this as moving {title} {direction}.",
            "transcription": request,
            "date": schedule_date,
            "operations": [{
                "op": "update",
                "title": title,
                "timing_mode": TimingMode.PREFERRED,
                "preferred_adjustment": direction,
                "move_direction": direction,
            }],
            "activities": [],
            "preferences": {},
        }

    def _fallback_parse_relative_add(
        self,
        request: str,
        schedule_date: str,
        current_schedule: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        patterns = [
            re.compile(
                r"\b(?:add|schedule)\s+(?:a\s+|an\s+)?(?:quick\s+)?(?P<title>.+?)\s+(?P<kind>right\s+after|right\s+before|after|before)\s+(?:the\s+|my\s+)?(?P<anchor>.+?)(?:\.|$)",
                re.IGNORECASE,
            ),
            re.compile(
                r"\b(?P<kind>after|before)\s+(?:the\s+|my\s+)?(?P<anchor>.+?)\s+add\s+(?:a\s+|an\s+)?(?P<title>.+?)(?:\.|$)",
                re.IGNORECASE,
            ),
        ]
        match = next((pattern.search(request) for pattern in patterns if pattern.search(request)), None)
        if not match:
            return None

        raw_title_text = match.group("title")
        duration_minutes, _duration_raw = self._extract_fast_path_duration(raw_title_text)
        title_without_duration = self._remove_fast_path_duration_phrase(raw_title_text)
        before_title = self._clean_fallback_activity_title(raw_title_text)
        title = self._clean_fast_path_new_activity_title(title_without_duration)
        if before_title and title and before_title != title:
            jlog("FAST_PATH", f'before="{before_title}" after="{title}"', "TITLE_CLEAN")
        anchor = self._clean_fallback_activity_title(match.group("anchor"))
        if not title or not anchor:
            return None

        kind = clean_title(match.group("kind")).replace("right ", "")
        operation = {
            "op": "add",
            "title": title,
            "timing_mode": TimingMode.RELATIVE,
            "anchor_relation": {"kind": kind, "target_title": anchor},
        }
        if duration_minutes:
            operation["duration_minutes"] = duration_minutes

        return {
            "intent": "edit",
            "reply": f"I understood this as adding {title} {kind} {anchor}.",
            "transcription": request,
            "date": schedule_date,
            "operations": [operation],
            "activities": [],
            "preferences": {},
        }

    def _fallback_parse_fixed_time_update(
        self,
        request: str,
        schedule_date: str,
        current_schedule: Optional[Dict[str, Any]] = None,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        match = re.search(
            r"\b(?:move|update|change|shift)\s+(?:my\s+|the\s+)?(?P<title>.+?)\s+to\s+(?P<time>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
            request,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        fixed_start = parse_clock(match.group("time"))
        if fixed_start is None:
            return None
        raw_title = self._clean_fallback_activity_title(match.group("title"))
        title = raw_title
        original_user_target = raw_title
        if clean_title(raw_title) in {"it", "this", "that"}:
            title = self._resolve_pronoun_target_from_context(request, current_schedule, history or [])
            if not title:
                time_text = format_clock(fixed_start)
                return {
                    "intent": "no_operation",
                    "reply": f"Which activity do you want to move to {time_text}?",
                    "transcription": request,
                    "date": schedule_date,
                    "operations": [],
                    "activities": [],
                    "preferences": {},
                    "_failure_type": "pronoun_target_clarification",
                }
        if not title:
            return None
        operation = {
            "op": "update",
            "title": title,
            "timing_mode": TimingMode.FIXED,
            "fixed_start": format_clock(fixed_start),
        }
        if clean_title(original_user_target) in {"it", "this", "that"}:
            operation["original_user_target"] = original_user_target
            operation["pronoun_resolved_target_title"] = title
        return {
            "intent": "edit",
            "reply": f"I understood this as moving {title} to {format_clock(fixed_start)}.",
            "transcription": request,
            "date": schedule_date,
            "operations": [operation],
            "activities": [],
            "preferences": {},
        }

    def _fallback_parse_remove(self, request: str, schedule_date: str) -> Optional[Dict[str, Any]]:
        match = re.search(
            r"\b(?:remove|delete|cancel)\s+(?:my\s+|the\s+)?(?P<title>.+?)(?:\.|$)",
            request,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        title = self._clean_fallback_activity_title(match.group("title"))
        if not title:
            return None
        return {
            "intent": "edit",
            "reply": f"I understood this as removing {title}.",
            "transcription": request,
            "date": schedule_date,
            "operations": [{"op": "remove", "title": title}],
            "activities": [],
            "preferences": {},
        }

    def _fallback_parse_priority_update(
        self,
        request: str,
        schedule_date: str,
        current_schedule: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        match = re.search(
            r"\b(?:set|make)\s+(?:my\s+|the\s+)?(?P<title>.+?)\s+priority\s+(?P<priority>low|medium|high)\b",
            request,
            flags=re.IGNORECASE,
        )
        if not match:
            match = re.search(
                r"\b(?P<direction>lower|raise)\s+(?:my\s+|the\s+)?(?P<title>.+?)\s+priority\b",
                request,
                flags=re.IGNORECASE,
            )
        if not match:
            return None
        priority = match.groupdict().get("priority")
        if not priority:
            priority = "low" if clean_title(match.group("direction")) == "lower" else "high"
        title = self._clean_fallback_activity_title(match.group("title"))
        if not title:
            return None
        original_title = title
        resolved_title, resolved = self._resolve_fast_path_title(title, current_schedule)
        if resolved:
            title = resolved_title
        direction = clean_title(match.groupdict().get("direction") or "")
        operation = {
            "op": "update",
            "title": title,
            "priority": priority,
            "priority_update_only": True,
            "original_user_target": original_title,
        }
        if direction in {"lower", "raise"}:
            operation["priority_direction"] = "lowered" if direction == "lower" else "raised"
        return {
            "intent": "edit",
            "reply": f"I understood this as setting {title} priority to {priority}.",
            "transcription": request,
            "date": schedule_date,
            "operations": [operation],
            "activities": [],
            "preferences": {},
        }

    def _resolve_pronoun_target_from_context(
        self,
        request: str,
        current_schedule: Optional[Dict[str, Any]],
        history: List[Dict[str, Any]],
    ) -> Optional[str]:
        activities = [
            item for item in (current_schedule or {}).get("activities", [])
            if isinstance(item, dict) and item.get("title")
        ]
        activities_for_resolution = [
            {
                **item,
                "status": item.get("status") or "active",
                "normalized_title": item.get("normalized_title") or clean_title(item.get("title") or ""),
                "aliases": item.get("aliases") or self._generate_aliases(item.get("title") or ""),
            }
            for item in activities
        ]
        titles = [str(item.get("title")) for item in activities]
        if not titles:
            return None

        user_messages = []
        for item in reversed(history or []):
            text = str(item.get("message") or item.get("content") or "")
            if text and text != request:
                if item.get("role") == "user":
                    user_messages.append(text)
        for message in user_messages:
            if self._request_text_implies_whole_plan_shift(clean_title(message)):
                jlog("PRONOUN", "target=it context=whole_plan valid=false action=clarify", None)
                return None
            match = re.search(
                r"\b(?:move|update|change|shift)\s+(?:my\s+|the\s+)?(?P<title>.+?)\s+to\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b",
                message,
                flags=re.IGNORECASE,
            )
            if match:
                resolution = self._resolve_activity_reference(match.group("title"), activities_for_resolution)
                if resolution.get("status") == "resolved" and resolution.get("target_resolution_confidence") == "high":
                    return resolution["activity"].get("title")
            remove_match = re.search(
                r"\b(?:remove|delete|cancel)\s+(?:my\s+|the\s+)?(?P<title>.+?)(?:\.|$)",
                message,
                flags=re.IGNORECASE,
            )
            if remove_match:
                resolution = self._resolve_activity_reference(remove_match.group("title"), activities_for_resolution)
                if resolution.get("status") == "resolved" and resolution.get("target_resolution_confidence") == "high":
                    return resolution["activity"].get("title")
            add_match = re.search(
                r"\badd\s+(?:a\s+|an\s+)?(?:quick\s+)?(?:\d{1,3}[-\s]*minute\s+)?(?P<title>.+?)\s+(?:right\s+after|right\s+before|after|before)\s+.+",
                message,
                flags=re.IGNORECASE,
            )
            if add_match:
                resolution = self._resolve_activity_reference(add_match.group("title"), activities_for_resolution)
                if resolution.get("status") == "resolved" and resolution.get("target_resolution_confidence") == "high":
                    return resolution["activity"].get("title")
        return None

    def _clean_fallback_activity_title(self, value: str) -> str:
        text = re.sub(r"\b(right|quick|my|the|a|an|instantly|immediately|now|please)\b", " ", value or "", flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip(" .")
        return text.title() if text else ""

    def _request_text_implies_whole_plan_shift(self, request_text: str) -> bool:
        whole_plan_patterns = [
            r"\bmove (this|the|these|my)? ?(whole|entire)? ?(plan|schedule|day)\b",
            r"\bshift (this|the|whole|entire|my)? ?(plan|schedule|day)\b",
            r"\bmove everything\b",
            r"\bmove all\b",
            r"\bwhole plan\b",
            r"\bentire plan\b",
            r"\bwrong date\b",
            r"\bi said wrong about the date\b",
            r"\bnot\s+\d{1,2}(?:st|nd|rd|th)?\s+(?:it'?s|its)\s+\d{1,2}(?:st|nd|rd|th)?",
            r"\bnot .* make it\b",
        ]
        return any(re.search(pattern, request_text) for pattern in whole_plan_patterns)

    def _request_implies_whole_plan_shift(
        self,
        latest_request: str,
        current_schedule: Optional[Dict[str, Any]],
        parsed: Dict[str, Any],
    ) -> bool:
        if not current_schedule:
            return False

        request_text = clean_title(latest_request)
        current_date = (current_schedule or {}).get("date")
        parsed_date = parsed.get("date") or self._extract_explicit_absolute_date(latest_request, current_schedule)
        if not current_date or not parsed_date or parsed_date == current_date:
            return False

        if self._request_text_implies_whole_plan_shift(request_text):
            return True

        for operation in parsed.get("operations") or []:
            op = clean_title(operation.get("op") or "")
            title = clean_title(operation.get("title") or operation.get("target_title") or "")
            if op == "shift_plan_date":
                return True
            if op == "move" and title in {"all activities", "whole plan", "entire plan", "whole schedule"}:
                return True

        return False

    def _normalize_plan_level_operations(
        self,
        parsed: Dict[str, Any],
        latest_request: str,
        current_schedule: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        normalized = self._apply_deterministic_shift_date_override(
            deepcopy(parsed),
            latest_request,
            current_schedule,
        )
        if not self._request_implies_whole_plan_shift(latest_request, current_schedule, normalized):
            return normalized

        from_date = (current_schedule or {}).get("date")
        to_date = self._extract_explicit_absolute_date(latest_request, current_schedule) or normalized.get("date")
        if not from_date or not to_date:
            return normalized

        normalized["intent"] = "edit"
        normalized["activities"] = []
        normalized["operations"] = [{
            "op": "shift_plan_date",
            "from_date": from_date,
            "to_date": to_date,
            "scope": "all_active_activities",
            "notes": f"Shift the whole active plan from {from_date} to {to_date}.",
        }]
        self._debug(f"[STATE] Normalized whole-plan shift request from {from_date} to {to_date}")
        return normalized

    def _is_generic_system_activity_payload(self, item: Dict[str, Any]) -> bool:
        title = clean_title(str(item.get("title") or item.get("target_title") or ""))
        item_type = clean_title(str(item.get("type") or item.get("block_type") or item.get("entity_type") or ""))

        if title in GENERIC_SYSTEM_ACTIVITY_TITLES:
            return True
        if item_type in GENERIC_SYSTEM_ACTIVITY_TYPES and title in GENERIC_SYSTEM_ACTIVITY_TITLES:
            return True
        return False

    def _sanitize_operation_payload(self, raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        operation = deepcopy(raw)
        if "location" in operation:
            operation["location"] = clean_optional_text(operation.get("location"))
        if "location_normalized" in operation:
            operation["location_normalized"] = clean_optional_text(operation.get("location_normalized"))

        if self._is_generic_system_activity_payload(operation):
            self._debug(
                f"[STATE] Ignored generic system block from parser/current plan: {operation.get('title') or operation.get('type')}"
            )
            return None
        return operation

    def _sanitize_operations(self, operations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        sanitized: List[Dict[str, Any]] = []
        for raw in operations or []:
            if not isinstance(raw, dict):
                continue
            operation = self._sanitize_operation_payload(raw)
            if operation:
                sanitized.append(operation)
        return sanitized

