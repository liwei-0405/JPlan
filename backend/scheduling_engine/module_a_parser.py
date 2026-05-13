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

from jplan_logging import jjson, jlog, jsection
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
        saved_locations: List[Dict[str, Any]] = []
    ) -> Dict[str, Any]:
        jsection("MODULE_A", "LLM parsing", "PARSE")
        jlog("MODULE_A", f"Request={latest_request!r}", "PARSE")
        
        prompt = self._build_parser_prompt(latest_request, history, current_schedule, saved_locations)

        contents: Any = prompt if audio_part is None else [prompt, audio_part]
        raw_llm_reply: Optional[str] = None
        raw_response_text: Optional[str] = None

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
            fallback = self._deterministic_fallback_parse(latest_request, current_schedule, history)
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
            fallback = self._deterministic_fallback_parse(latest_request, current_schedule, history)
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
            fallback = self._deterministic_fallback_parse(latest_request, current_schedule, history)
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
        if self._is_schedule_change_intent(parsed) and not parsed.get("operations") and not parsed.get("activities"):
            jlog(
                "MODULE_A",
                "Empty operations for edit intent. Attempting deterministic fallback parse.",
                "SAFETY",
            )
            fallback = self._deterministic_fallback_parse(latest_request, current_schedule, history)
            if fallback:
                self._debug_json("Deterministic fallback parse result", fallback)
                return fallback
            parsed["intent"] = "no_operation"
            parsed["reply"] = "I could not understand or apply the requested change. Please try again with the activity name and time."
            parsed["_failure_type"] = "empty_operations"
        jlog(
            "MODULE_A",
            f"parser_rejected_request=false operations_count={len(parsed.get('operations') or [])}",
            "SAFETY",
        )
        self._debug_json("LLM parsed request", parsed)
        self._debug(
            f"Parsed request | intent={parsed.get('intent')} | parsed_date={parsed.get('date')} | activities={len(parsed.get('activities', []))} | operations={len(parsed.get('operations', []))}"
        )
        return parsed

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

        jlog("MODULE_A", f"start timeout={int(timeout_seconds * 1000)}ms", "LLM")
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
    ) -> str:
        history_lines = self._summarize_history(history)
        local_context = self._local_datetime_context()
        current_activity_index = self._build_current_activity_index(current_schedule)
        saved_location_index = self._build_saved_location_index(saved_locations or [])
        schedule_date = (current_schedule or {}).get("date") or "(none)"
        return (
            f"{PARSER_PROMPT}\n\n"
            f"{local_context}\n"
            f"CURRENT_SCHEDULE_DATE: {schedule_date}\n"
            f"CURRENT_ACTIVITY_INDEX:\n{current_activity_index}\n"
            f"SAVED_LOCATION_INDEX:\n{saved_location_index}\n"
            f"HISTORY:\n" + ("\n".join(history_lines) if history_lines else "(none)") + "\n\n"
            f"LATEST_REQUEST:\n{latest_request}\n"
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
            stable_id = item.get("stable_activity_id") or item.get("id") or "?"
            lines.append(f"- {stable_id} | {title} | {start}-{end} | {location} | {timing_mode} | {priority}")
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
            parsed = self._fallback_parse_arrange_relation(request, schedule_date, current_schedule)

        if parsed is None:
            parsed = self._fallback_parse_swap_order(request, schedule_date, current_schedule)

        if parsed is None:
            parsed = self._fallback_parse_soft_adjustment(request, schedule_date, current_schedule, history or [])

        if parsed is None:
            parsed = self._fallback_parse_relative_add(request, schedule_date)

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
        parsed = self._deterministic_fallback_parse(
            latest_request=latest_request,
            current_schedule=current_schedule,
            history=history or [],
            saved_locations=saved_locations or [],
            reply_source="deterministic_fast_path",
            failure_type="deterministic_fast_path",
        )
        jlog("TIMER", f"module_a_fast_path_seconds={time.perf_counter() - started:.2f}", None)
        if parsed:
            jlog("MODULE_A", "deterministic_fast_path matched simple pattern", "FAST_PATH")
        return parsed

    def _is_schedule_change_intent(self, parsed: Dict[str, Any]) -> bool:
        intent = clean_title(parsed.get("intent") or "")
        if intent in {"edit", "add", "move", "schedule", "create", "update"}:
            return True
        request = clean_title(parsed.get("transcription") or "")
        return bool(re.search(r"\b(move|shift|change|update|add|remove|delete|reschedule)\b", request))

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

    def _fallback_parse_arrange_relation(
        self,
        request: str,
        schedule_date: str,
        current_schedule: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        pattern = re.compile(
            r"\b(?:arrange|rearrange|put|place|make|set)\s+(?:the\s+|my\s+)?(?P<title>.+?)\s+"
            r"(?P<kind>after|before|right\s+after|right\s+before)\s+(?:the\s+|my\s+)?(?P<anchor>.+?)(?:\.|$)",
            re.IGNORECASE,
        )
        match = pattern.search(request)
        if not match:
            if clean_title(request).startswith("add "):
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

        raw_title = self._clean_fallback_activity_title(match.group("title"))
        raw_anchor = self._clean_fallback_activity_title(match.group("anchor"))
        if not raw_title or not raw_anchor:
            return None
        title, target_exists = self._resolve_fast_path_title(raw_title, current_schedule)
        anchor, _ = self._resolve_fast_path_title(raw_anchor, current_schedule)
        kind = clean_title(match.group("kind")).replace("right ", "")
        op_type = "update" if target_exists else "add"
        jlog("FAST_PATH", f"parsed arrange_{kind} target={title} anchor={anchor}", None)
        return {
            "intent": "edit",
            "reply": f"I understood this as arranging {title} {kind} {anchor}.",
            "transcription": request,
            "date": schedule_date,
            "operations": [{
                "op": op_type,
                "title": title,
                "timing_mode": TimingMode.RELATIVE,
                "anchor_relation": {"kind": kind, "target_title": anchor},
            }],
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

    def _fallback_parse_relative_add(self, request: str, schedule_date: str) -> Optional[Dict[str, Any]]:
        text = clean_title(request)
        duration_minutes: Optional[int] = None
        duration_match = re.search(r"\b(?P<duration>\d{1,3})[-\s]*minute\b", text)
        if duration_match:
            duration_minutes = int(duration_match.group("duration"))

        patterns = [
            re.compile(
                r"\badd\s+(?:a\s+|an\s+)?(?:quick\s+)?(?:(?P<duration>\d{1,3})[-\s]*minute\s+)?(?P<title>.+?)\s+(?P<kind>right\s+after|right\s+before|after|before)\s+(?:the\s+|my\s+)?(?P<anchor>.+?)(?:\.|$)",
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

        title = self._clean_fallback_activity_title(match.group("title"))
        anchor = self._clean_fallback_activity_title(match.group("anchor"))
        if not title or not anchor:
            return None
        if match.groupdict().get("duration"):
            duration_minutes = int(match.group("duration"))

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
        text = re.sub(r"\b(right|quick|my|the)\b", " ", value or "", flags=re.IGNORECASE)
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

