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

from jplan_logging import jjson, jlog, jlog_verbose, jsection
from travel_service import MissingORSApiKey, TravelService, TravelServiceError, coordinate_from_saved_location
from .types_utils import *
from .types_utils import _normalize_location

ADVISORY_LLM_EXECUTOR = ThreadPoolExecutor(max_workers=ADVISORY_LLM_EXECUTOR_WORKERS, thread_name_prefix="jplan-advice")
ADVISORY_LLM_SEMAPHORE = BoundedSemaphore(ADVISORY_LLM_EXECUTOR_WORKERS)


class AdvisoryLLMTimeoutError(TimeoutError):
    pass


class AdvisoryLLMExecutorSaturatedError(RuntimeError):
    pass


class Module0RouterMixin:
    _ROUTER_ACTIVITY_PATTERN = (
        r"\b(?:lunch|dinner|breakfast|coffee(?:\s+break)?|meeting|seminar|class|gym|workout|"
        r"shopping|grocery|groceries|fyp|study|project|work|deep\s+work|focused\s+work|plan|schedule)\b"
    )
    _ROUTER_ACTION_PATTERN = (
        r"\b(?:move|update|change|shift|add|schedule|remove|delete|cancel|arrange|rearrange|put|place|set|fit|"
        r"make|reschedule|delay|postpone|swap|switch)\b|"
        r"\b(?:bring|move|push)\s+(?:forward|backward|back)\b|"
        r"\bshift\s+(?:earlier|later)\b|"
        r"\bchange\s+(?:the\s+)?order\b|"
        r"\bswap\s+order\b"
    )
    _ROUTER_ADJUSTMENT_PATTERN = (
        r"\b(?:earlier|early|later|late|sooner|too\s+late|too\s+early|not\s+so\s+late|"
        r"a\s+bit\s+earlier|a\s+little\s+earlier)\b"
    )
    _ROUTER_RELATION_PATTERN = r"\b(?:after|before|right\s+after|right\s+before|around|at)\b"

    def _detect_travel_intent(self, clean: str) -> bool:
        return detect_travel_intent(clean)


    def _has_temporal_schedule_signal(self, clean: str) -> bool:
        has_clock_time = bool(re.search(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", clean or ""))
        has_duration = bool(re.search(r"\b\d+(?:\.\d+)?\s*(?:hours?|hrs?|minutes?|mins?)\b", clean or ""))
        return has_clock_time or has_duration

    def _has_retryable_unfit_items(self, current_schedule: Optional[Dict[str, Any]] = None) -> bool:
        if not current_schedule:
            return False
        return bool(
            (current_schedule.get("unfit_activities") or [])
            or (current_schedule.get("unscheduled_activities") or [])
            or (current_schedule.get("optional_skipped") or [])
        )

    def _is_refit_unfit_request(
        self,
        clean: str,
        current_schedule: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if not self._has_retryable_unfit_items(current_schedule):
            return False
        explicit_retry_language = bool(re.search(
            r"\b(?:try\s+to|help\s+me\s+(?:to\s+)?|please\s+)?(?:re)?fit(?:\s+in)?\b.*"
            r"\b(?:unfit|unscheduled|could\s+not\s+fit|cannot\s+fit|can\s+not\s+fit|"
            r"couldn\s*t\s+fit|activities?|items?)\b",
            clean,
        ))
        focused_retry_language = bool(re.search(
            r"\b(?:try\s+to|help\s+me\s+(?:to\s+)?|please\s+)?(?:re)?fit(?:\s+in)?\b.*"
            r"\b(?:focused\s+(?:deep\s+)?work|deep\s+work)\b",
            clean,
        ))
        return explicit_retry_language or focused_retry_language

    def _natural_edit_wording_skip_reason(self, clean: str, text: str) -> Optional[str]:
        has_relation = bool(re.search(r"\b(?:after|before|right\s+after|right\s+before|earlier|later)\b", clean))
        has_activity = bool(re.search(self._ROUTER_ACTIVITY_PATTERN, clean))
        if not (has_relation and has_activity):
            return None
        activity_mentions = len(re.findall(self._ROUTER_ACTIVITY_PATTERN, clean))
        if activity_mentions >= 3 and len(clean.split()) > 14:
            return None
        if re.search(r"\b(?:because|cause|cuz|since|so\s+that|so\s+i\s+(?:do\s+not|don'?t|dont)|to\s+avoid)\b", clean):
            return "natural_edit_wording"
        if re.match(
            r"^(?:how\s+about|what\s+about|maybe|what\s+if|i\s+was\s+thinking|i\s+think\s+maybe|"
            r"would\s+it\s+be\s+better|would\s+it\s+help|i\s+want|i\s+need|i\s+would\s+like|can\s+we|could\s+we)\b",
            clean,
        ):
            return "natural_edit_wording"
        explicit_clean_command = bool(re.match(
            r"^(?:(?:arrange|rearrange|put|place|make|set)\b|(?:add|schedule)\b|(?:move|shift|change|update)\b|(?:remove|delete|cancel)\b)",
            clean,
        ))
        if len(clean.split()) > 14 and not explicit_clean_command:
            return "natural_edit_wording"
        return None

    def route_chat_request(
        self,
        message: str,
        current_schedule: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        text = re.sub(r"\s+", " ", message or "").strip()
        clean = clean_title(text)
        has_schedule = bool(
            (current_schedule or {}).get("activities")
            or (current_schedule or {}).get("schedule_blocks")
        )

        route = {
            "route": "ambiguous",
            "confidence": 0.45,
            "should_mutate_schedule": False,
            "use_deterministic_parser": False,
            "use_module_a_llm": True,
            "use_advisory_llm": False,
            "travel_intent": self._detect_travel_intent(clean),
            "reason": "schedule_related_but_unclear",
        }

        if not clean:
            route.update({
                "route": "general_chat",
                "confidence": 0.95,
                "use_module_a_llm": False,
                "reason": "empty_or_greeting",
            })
            return self._log_route_decision(route)

        if re.fullmatch(r"(hi|hello|hey|help|what can you do|how does this app work)\??", clean):
            route.update({
                "route": "general_chat",
                "confidence": 0.95,
                "use_module_a_llm": False,
                "reason": "matched_general_chat",
            })
            return self._log_route_decision(route)

        if self._is_accurate_travel_validation_request(clean, current_schedule):
            route.update({
                "route": "accurate_travel_validation",
                "confidence": 0.95,
                "should_mutate_schedule": False,
                "use_deterministic_parser": False,
                "use_module_a_llm": False,
                "use_advisory_llm": False,
                "reason": "matched_accurate_travel_validation",
            })
            return self._log_route_decision(route)

        refit_unfit_request = self._is_refit_unfit_request(clean, current_schedule)
        if re.search(r"\b(?:optimi[sz]e|regenerate|rebuild)\b.*\b(?:schedule|plan|day)\b|\bmake\s+(?:the\s+)?(?:schedule|plan|day)\s+better\b", clean) or refit_unfit_request:
            route.update({
                "route": "simple_schedule_command",
                "confidence": 0.92,
                "should_mutate_schedule": True,
                "use_deterministic_parser": True,
                "use_module_a_llm": False,
                "reason": "matched_refit_unfit_request" if refit_unfit_request else "matched_optimize_request",
            })
            return self._log_route_decision(route)

        has_activity_entity = bool(re.search(self._ROUTER_ACTIVITY_PATTERN, clean))
        has_action_word = bool(re.search(self._ROUTER_ACTION_PATTERN, clean))
        has_adjustment_word = bool(re.search(self._ROUTER_ADJUSTMENT_PATTERN, clean))
        has_relation_word = bool(re.search(self._ROUTER_RELATION_PATTERN, clean))
        has_pronoun = bool(re.search(r"\b(?:it|this|that)\b", clean))
        has_action_question = bool(re.search(r"\b(?:can you|please|could you)\b", clean))
        complaint_action = bool(
            (has_activity_entity or has_pronoun)
            and has_adjustment_word
            and re.search(r"\b(?:can|could)\s+(?:it|this|that|you)\s+(?:be|make|move|shift)\b|\bcan you\b", clean)
        )
        complaint_only = bool(has_activity_entity and has_adjustment_word and not complaint_action and not has_action_word)

        natural_skip_reason = self._natural_edit_wording_skip_reason(clean, text)
        if natural_skip_reason:
            jlog("FAST_PATH", f"reason={natural_skip_reason}", "SKIP_TO_MODULE_A")
            route.update({
                "route": "simple_schedule_command",
                "confidence": 0.84,
                "should_mutate_schedule": True,
                "use_deterministic_parser": False,
                "use_module_a_llm": True,
                "reason": natural_skip_reason,
            })
            return self._log_route_decision(route)

        advice_patterns = (
            r"\bshould i\b",
            r"\bdo you think\b",
            r"\bwhat do you suggest\b",
            r"\bwhich .* should\b",
            r"\bis it better\b",
            r"\bis it okay\b",
            r"\bcan i fit\b",
            r"\bwhy\b",
            r"\btoo packed\b",
        )
        explicit_action_override = (
            "can you move",
            "can you add",
            "can you remove",
            "please move",
            "please add",
            "please remove",
            "i want to",
            "i need to",
            "can you make",
            "can you arrange",
            "can you put",
            "can you place",
            "please arrange",
            "please put",
            "please place",
            "please swap",
            "please switch",
        )
        if any(re.search(pattern, clean) for pattern in advice_patterns) and not any(phrase in clean for phrase in explicit_action_override):
            route.update({
                "route": "planning_advice",
                "confidence": 0.88,
                "use_module_a_llm": False,
                "use_advisory_llm": True,
                "reason": "matched_advice_question",
            })
            return self._log_route_decision(route)

        if complaint_only:
            route.update({
                "route": "planning_advice",
                "confidence": 0.88,
                "use_module_a_llm": False,
                "use_advisory_llm": True,
                "reason": "complaint_only_no_action",
            })
            return self._log_route_decision(route)

        complex_markers = (
            "generate a busy",
            "plan my day",
            "plan a day",
            "schedule my day",
            "i have a",
            "followed by",
            "also",
            "fit in",
            "account for travel",
            "redesign",
            "whole evening",
            "my evening",
        )
        activity_mentions = len(re.findall(r"\b(meeting|seminar|lunch|dinner|gym|shopping|grocery|fyp|class|workout|coffee)\b", clean))
        if any(marker in clean for marker in complex_markers) and (activity_mentions >= 2 or len(clean.split()) > 18):
            route.update({
                "route": "complex_schedule_command",
                "confidence": 0.9,
                "should_mutate_schedule": True,
                "use_module_a_llm": True,
                "reason": "multi_activity_redesign" if "redesign" in clean or "evening" in clean else "multi_activity_generation",
            })
            return self._log_route_decision(route)

        arrange_after = bool(re.search(
            r"\b(?:arrange|rearrange|put|place|make|set)\s+(?:the\s+|my\s+|a\s+|an\s+)?[a-z0-9][a-z0-9\s\-]{1,60}?\s+(?:right\s+after|right\s+before|after|before)\s+(?:the\s+|my\s+)?[a-z][a-z\s]{1,40}\b",
            clean,
        ))
        swap_order = bool(re.search(
            r"\b(?:swap|switch)\s+(?:the\s+|my\s+)?[a-z][a-z\s]{1,30}?\s+(?:and|with)\s+(?:the\s+|my\s+)?[a-z][a-z\s]{1,30}\b|"
            r"\bchange\s+(?:the\s+)?order\s+of\s+(?:the\s+|my\s+)?[a-z][a-z\s]{1,30}?\s+and\s+(?:the\s+|my\s+)?[a-z][a-z\s]{1,30}\b",
            clean,
        ))
        complaint_plus_action = bool(
            complaint_action
            or (has_activity_entity and has_action_question and has_adjustment_word)
            or (has_pronoun and has_action_question and has_adjustment_word and has_schedule)
        )
        entity_relation = bool(has_activity_entity and has_relation_word and re.search(r"\b(?:after|before|right\s+after|right\s+before)\b", clean))
        entity_adjustment = bool(has_activity_entity and has_adjustment_word and has_action_word)
        pronoun_adjustment = bool(has_pronoun and has_adjustment_word and has_schedule and (has_action_question or has_action_word))

        if arrange_after or swap_order or complaint_plus_action or entity_relation or entity_adjustment or pronoun_adjustment:
            reason = "matched_simple_natural_schedule_pattern"
            if arrange_after:
                reason = "matched_arrange_after_pattern"
            elif swap_order:
                reason = "matched_swap_order_pattern"
            elif complaint_plus_action:
                reason = "complaint_plus_action"
            
            # Strict deterministic rule: Only true clean commands can use fast-path.
            is_clean_command = bool((arrange_after or swap_order) and not has_adjustment_word and not complaint_plus_action and not has_action_question)
            use_deterministic = bool(is_clean_command and has_schedule)

            route.update({
                "route": "simple_schedule_command",
                "confidence": 0.9 if has_schedule or has_activity_entity else 0.78,
                "should_mutate_schedule": True,
                "use_deterministic_parser": use_deterministic,
                "use_module_a_llm": not use_deterministic,
                "reason": reason,
            })
            if complaint_plus_action:
                target = self._router_first_activity_entity(clean) or "recent_target"
                route["target"] = target
            return self._log_route_decision(route)

        simple_patterns = (
            r"\b(?:can you\s+)?(?:move|update|change|shift)\s+(?:my\s+|the\s+)?(?:it|this|that|[a-z][a-z\s]{1,40}?)\s+to\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b",
            r"\b(?:add|schedule|put|place)\s+.+\s+(?:right\s+after|right\s+before|after|before)\s+.+",
            r"\b(?:after|before)\s+.+\s+add\s+.+",
            r"\b(?:remove|delete|cancel)\s+.+",
            r"\b(?:lower|raise|set|make)\s+.+\s+priority\b|\bpriority\s+(?:low|medium|high)\b",
            r"\b(?:move|shift)\s+(?:this|the|my)?\s*(?:whole|entire)?\s*(?:plan|schedule|day|everything|all)\s+to\b",
        )
        if any(re.search(pattern, clean) for pattern in simple_patterns):
            confidence = 0.9 if has_schedule or re.search(r"\b(add|remove|delete|cancel)\b", clean) else 0.78
            # Strict deterministic rule: Reject if contains soft adjustments/complaints
            is_clean_command = not has_adjustment_word and not complaint_plus_action
            use_deterministic = bool(confidence >= 0.85 and is_clean_command)
            route.update({
                "route": "simple_schedule_command",
                "confidence": confidence,
                "should_mutate_schedule": True,
                "use_deterministic_parser": use_deterministic,
                "use_module_a_llm": not use_deterministic,
                "reason": "matched_simple_schedule_pattern",
            })
            return self._log_route_decision(route)

        if has_activity_entity and has_action_word:
            route.update({
                "route": "simple_schedule_command",
                "confidence": 0.74,
                "should_mutate_schedule": True,
                "use_deterministic_parser": False,
                "use_module_a_llm": True,
                "reason": "matched_action_activity_pattern",
            })
            return self._log_route_decision(route)

        if re.search(r"\b(generate|plan|schedule|move|shift|change|update|add|remove|delete|reschedule)\b", clean):
            route.update({
                "route": "complex_schedule_command",
                "confidence": 0.62,
                "should_mutate_schedule": True,
                "use_module_a_llm": True,
                "reason": "schedule_related_low_confidence_use_llm",
            })
            return self._log_route_decision(route)

        if has_schedule and self._has_temporal_schedule_signal(clean):
            route.update({
                "route": "simple_schedule_command",
                "confidence": 0.7,
                "should_mutate_schedule": True,
                "use_deterministic_parser": False,
                "use_module_a_llm": True,
                "reason": "temporal_schedule_context_use_llm",
            })
            return self._log_route_decision(route)

        route.update({
            "route": "general_chat",
            "confidence": 0.82,
            "use_module_a_llm": False,
            "reason": "no_schedule_action_detected",
        })
        return self._log_route_decision(route)

    def _is_accurate_travel_validation_request(
        self,
        clean: str,
        current_schedule: Optional[Dict[str, Any]] = None,
    ) -> bool:
        activity_mentions = len(re.findall(
            r"\b(meeting|seminar|lunch|dinner|gym|shopping|grocery|groceries|fyp|class|workout|coffee|appointment|doctor|dentist|work)\b",
            clean,
        ))
        generation_markers = (
            "plan my day",
            "plan a day",
            "schedule my day",
            "generate",
            "create",
            "i have",
            "i still need",
            "fit in",
            "please make it realistic",
        )
        if any(marker in clean for marker in generation_markers) and (activity_mentions >= 2 or len(clean.split()) > 18):
            return False

        if bool(re.search(ACCURATE_TRAVEL_REQUEST_PATTERN, clean)):
            return True
        accurate_enabled = bool(
            (current_schedule or {}).get("accurate_travel_time")
            or ((current_schedule or {}).get("preferences") or {}).get("accurate_travel_time")
        )
        if accurate_enabled and re.search(r"\b(?:travel|route|commute)\b", clean):
            return bool(re.search(r"\b(?:now|want|make|use|check|validate|calculate|recalculate|include|add|actual|accurate|real)\b", clean))
        return False

    def _router_first_activity_entity(self, clean: str) -> Optional[str]:
        match = re.search(self._ROUTER_ACTIVITY_PATTERN, clean)
        return match.group(0).title() if match else None

    def _log_route_decision(self, route: Dict[str, Any]) -> Dict[str, Any]:
        jlog(
            "ROUTER",
            (
                f"route={route.get('route')} confidence={route.get('confidence')} "
                f"use_deterministic_parser={str(bool(route.get('use_deterministic_parser'))).lower()} "
                f"use_module_a_llm={str(bool(route.get('use_module_a_llm'))).lower()} "
                f"should_mutate_schedule={str(bool(route.get('should_mutate_schedule'))).lower()} "
                f"travel_intent={str(bool(route.get('travel_intent'))).lower()} "
                f"reason={route.get('reason')}"
            ),
            None,
        )
        return route

    def compose_general_chat_reply(self, message: str) -> Dict[str, Any]:
        clean = clean_title(message or "")
        if "what can you do" in clean or "help" in clean or "how does this app work" in clean:
            reply = "I can help create schedules, edit events, explain conflicts, and validate travel time when Accurate Travel Time is enabled."
        else:
            reply = "Hi! Tell me what you want to plan or change in your schedule."
        return {"reply": reply, "reply_status": "chat", "reply_source": "template"}

    def compose_advisory_reply(
        self,
        latest_request: str,
        current_schedule: Optional[Dict[str, Any]],
        allow_clash: bool,
        accurate_travel_time: bool,
    ) -> Dict[str, Any]:
        summary = self._compact_schedule_summary_for_advice(current_schedule)
        prompt = f"""
You are JPlan's planning advisor.
Answer using only the schedule summary, conflicts, warnings, and flags below.
Do not change the schedule. Do not say anything was applied.
Keep it concise, 1-3 sentences.
If the user wants action, tell them they can ask you to apply it.

USER_QUESTION:
{latest_request}

ALLOW_CLASH: {bool(allow_clash)}
ACCURATE_TRAVEL_TIME: {bool(accurate_travel_time)}
SCHEDULE_SUMMARY:
{summary}
"""
        if not getattr(self.client, "models", None):
            return self._contextual_advisory_fallback(latest_request, current_schedule, reason="llm_unavailable")
        try:
            response = self._generate_advisory_content_with_timeout(prompt)
            usage = getattr(response, "usage_metadata", None)
            token_usage = None
            if usage:
                token_usage = {
                    "prompt": int(getattr(usage, "prompt_token_count", 0) or 0),
                    "candidates": int(getattr(usage, "candidates_token_count", 0) or 0),
                    "total": int(getattr(usage, "total_token_count", 0) or 0),
                }
                jlog(
                    "ADVICE",
                    f"Prompt={token_usage['prompt']} | Candidates={token_usage['candidates']} | Total={token_usage['total']}",
                    "TOKEN",
                )
            reply = (response.text or "").strip()
            if not reply:
                return self._contextual_advisory_fallback(latest_request, current_schedule, reason="empty_reply")
            jlog("ADVICE", "reply_source=llm", "LLM")
            return {
                "reply": reply,
                "reply_status": "advice",
                "reply_source": "llm",
                "token_usage": token_usage,
            }
        except AdvisoryLLMTimeoutError:
            return self._contextual_advisory_fallback(latest_request, current_schedule, reason="timeout")
        except AdvisoryLLMExecutorSaturatedError:
            return self._contextual_advisory_fallback(latest_request, current_schedule, reason="executor_saturated")
        except Exception:
            jlog("ADVICE", "Advisory reply failed; using contextual fallback.", "ERROR")
            return self._contextual_advisory_fallback(latest_request, current_schedule, reason="llm_unavailable")

    def _call_advisory_llm(self, prompt: str) -> Any:
        return self.client.models.generate_content(
            model=ADVISORY_LLM_MODEL,
            contents=prompt,
            config={"temperature": 0.2, "max_output_tokens": 150},
        )

    def _generate_advisory_content_with_timeout(self, prompt: str) -> Any:
        if not ADVISORY_LLM_SEMAPHORE.acquire(blocking=False):
            raise AdvisoryLLMExecutorSaturatedError("advisory executor saturated")
        timeout_seconds = max(0.1, float(ADVISORY_LLM_TIMEOUT_SECONDS))
        jlog_verbose("ADVICE", f"start timeout={int(timeout_seconds * 1000)}ms", "LLM")
        future = ADVISORY_LLM_EXECUTOR.submit(self._call_advisory_llm, prompt)
        future.add_done_callback(lambda _future: ADVISORY_LLM_SEMAPHORE.release())
        try:
            return future.result(timeout=timeout_seconds)
        except FutureTimeoutError as exc:
            future.cancel()
            raise AdvisoryLLMTimeoutError("advisory llm timeout") from exc

    def _contextual_advisory_fallback(
        self,
        latest_request: str,
        current_schedule: Optional[Dict[str, Any]],
        reason: str,
    ) -> Dict[str, Any]:
        target = self._find_advice_target(latest_request, current_schedule)
        target_title = target.get("title") if target else None
        jlog("ADVICE", f"reason={reason} target={target_title or '(none)'}", "FALLBACK")
        if target:
            start = target.get("start")
            end = target.get("end")
            time_text = f" is currently scheduled from {start} to {end}" if start and end else " is currently on your schedule"
            clean_request = clean_title(latest_request or "")
            if re.search(r"\b(?:tmr|tomorrow|next day)\b", clean_request):
                apply_hint = f'If you want to move it to tomorrow, say "move {target_title} to tomorrow".'
            else:
                apply_hint = f'If you want to apply a change, say "move {target_title} to [new date/time]".'
            return {
                "reply": f"{target_title}{time_text}. I'll keep it unchanged for now. {apply_hint}",
                "reply_status": "advice",
                "reply_source": "template",
                "llm_fallback_reason": reason,
            }
        return {
            "reply": "I couldn't generate detailed advice right now, so I'll keep your current plan unchanged. If you want to apply a change, please mention the activity and new date/time.",
            "reply_status": "advice",
            "reply_source": "template",
            "llm_fallback_reason": reason,
        }

    def _find_advice_target(
        self,
        latest_request: str,
        current_schedule: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        pool = self._advice_activity_pool(current_schedule)
        if not pool:
            return None
        clean_request = clean_title(latest_request or "")
        mentions = self._advice_activity_mentions(clean_request)
        for mention in mentions:
            mention_key = clean_title(mention)
            for item in pool:
                if mention_key in item["aliases"] or mention_key == item["key"] or mention_key in item["key"]:
                    return item
        for item in pool:
            if item["key"] and item["key"] in clean_request:
                return item
            if any(alias and alias in clean_request for alias in item["aliases"]):
                return item
        return None

    def _advice_activity_mentions(self, clean_request: str) -> List[str]:
        candidates = [
            "fyp implementation",
            "project meeting",
            "grocery shopping",
            "lunch with girl",
            "coffee break",
            "gym workout",
            "breakfast",
            "shopping",
            "grocery",
            "meeting",
            "seminar",
            "dinner",
            "lunch",
            "coffee",
            "class",
            "study",
            "gym",
            "fyp",
        ]
        return [candidate for candidate in candidates if re.search(rf"\b{re.escape(candidate)}\b", clean_request)]

    def _advice_activity_pool(self, current_schedule: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not current_schedule:
            return []
        blocks = current_schedule.get("schedule_blocks") or current_schedule.get("activities") or []
        pool: List[Dict[str, Any]] = []
        for item in blocks:
            if not isinstance(item, dict):
                continue
            if item.get("block_type") not in {None, "activity"} and item.get("type") != "activity":
                continue
            title = clean_optional_text(item.get("title"))
            if not title:
                continue
            start = item.get("start") or item.get("startTime")
            end = item.get("end") or item.get("endTime")
            if not start and item.get("scheduled_start") is not None:
                start = format_clock(item.get("scheduled_start"))
            if not end and item.get("scheduled_end") is not None:
                end = format_clock(item.get("scheduled_end"))
            aliases = {clean_title(title), clean_title(item.get("normalized_title") or "")}
            aliases.update(clean_title(alias) for alias in item.get("aliases") or [] if alias)
            title_key = clean_title(title)
            if "fyp" in title_key:
                aliases.update({"fyp", "fyp implementation"})
            if "lunch" in title_key:
                aliases.add("lunch")
            if "grocery" in title_key or "shopping" in title_key:
                aliases.update({"shopping", "grocery", "grocery shopping"})
            if "gym" in title_key or "workout" in title_key:
                aliases.update({"gym", "workout", "gym workout"})
            if "meeting" in title_key:
                aliases.add("meeting")
            pool.append({
                "title": title,
                "key": title_key,
                "aliases": {alias for alias in aliases if alias},
                "start": start,
                "end": end,
            })
        return pool

    def _compact_schedule_summary_for_advice(self, current_schedule: Optional[Dict[str, Any]]) -> str:
        if not current_schedule:
            return "(no current schedule)"
        lines = [f"date={current_schedule.get('date')}"]
        blocks = current_schedule.get("schedule_blocks") or current_schedule.get("activities") or []
        for item in blocks[:12]:
            if not isinstance(item, dict):
                continue
            if item.get("block_type") not in {None, "activity"} and item.get("type") != "activity":
                continue
            title = item.get("title")
            start = item.get("start") or item.get("startTime")
            end = item.get("end") or item.get("endTime")
            if title:
                lines.append(f"- {title}: {start or '?'}-{end or '?'}")
        conflicts = current_schedule.get("conflicts") or []
        warnings = current_schedule.get("warnings") or []
        if conflicts:
            lines.append("conflicts=" + json.dumps(conflicts[:3], ensure_ascii=True))
        if warnings:
            lines.append("warnings=" + json.dumps(warnings[:3], ensure_ascii=True))
        return "\n".join(lines)[:1600]
