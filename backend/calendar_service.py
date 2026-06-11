import os
import requests
import json
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timedelta
from supabase import Client

from calendar_import import (
    JPLAN_META_MARKER,
    extract_jplan_metadata,
    materialize_committed_blocks,
)
from jplan_logging import jlog

CALENDAR_VERBOSE_LOGS = os.getenv("JPLAN_CALENDAR_DEBUG", "").lower() in {"1", "true", "yes", "on"}
JPLAN_EXPORT_MARKER = "Created via JPlan"
EXPORTABLE_TRAVEL_TYPES = {"travel", "transition", "start_route"}
SKIPPED_EXPORT_TYPES = {
    "free",
    "free_time",
    "idle",
    "buffer",
    "prep",
    "support",
    "route_conflict",
    "warning",
}


def _calendar_log(message: str, stage: str = "INFO") -> None:
    jlog("CALENDAR", message, stage)


def _calendar_debug(message: str) -> None:
    if CALENDAR_VERBOSE_LOGS:
        _calendar_log(message, "DEBUG")


def _normalize_export_type(block: Dict[str, Any]) -> str:
    return str(block.get("block_type") or block.get("type") or "").strip().lower()


def _activity_key(item: Dict[str, Any]) -> str:
    for field in ("stable_activity_id", "activity_id", "id", "source_activity_id", "block_id"):
        value = item.get(field)
        if value:
            return str(value)
    title = str(item.get("title") or item.get("activity") or "").strip().lower()
    duration = item.get("duration_minutes") or item.get("duration")
    return f"title:{title}|duration:{duration or ''}"


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in {"none", "null", "undefined"}:
            return text
    return ""


def _best_location_label(item: Dict[str, Any]) -> str:
    resolved = item.get("resolved_location") if isinstance(item.get("resolved_location"), dict) else {}
    return _first_text(
        item.get("location_label"),
        item.get("location"),
        resolved.get("display_name"),
        resolved.get("address"),
        resolved.get("label"),
        item.get("saved_location_label"),
        item.get("to_location"),
    )


def _format_minutes_as_time(minutes: Any) -> Optional[str]:
    try:
        total = int(minutes)
    except (TypeError, ValueError):
        return None
    total %= 24 * 60
    hour = total // 60
    minute = total % 60
    suffix = "AM" if hour < 12 else "PM"
    hour_12 = hour % 12 or 12
    return f"{hour_12:02d}:{minute:02d} {suffix}"


def _item_start_end(item: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    start = item.get("startTime") or item.get("start")
    end = item.get("endTime") or item.get("end")
    if not start:
        start = _format_minutes_as_time(item.get("scheduled_start") or item.get("fixed_start"))
    if not end:
        end = _format_minutes_as_time(item.get("scheduled_end") or item.get("fixed_end"))
    return start, end


def _time_text_to_minutes(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 24 * 60 + 999
    for fmt in ("%I:%M %p", "%I %p", "%H:%M"):
        try:
            parsed = datetime.strptime(text.upper(), fmt)
            return parsed.hour * 60 + parsed.minute
        except ValueError:
            continue
    return 24 * 60 + 999


def _activity_identity_values(item: Dict[str, Any]) -> set:
    values = {
        str(item.get(field) or "").strip()
        for field in ("stable_activity_id", "activity_id", "id", "source_activity_id", "block_id")
        if item.get(field)
    }
    title = str(item.get("title") or item.get("activity") or "").strip().lower()
    start, end = _item_start_end(item)
    if title:
        values.add(f"title:{title}")
        values.add(f"title:{title}|start:{start or ''}|end:{end or ''}")
    return {value for value in values if value}


def _merge_missing_canonical_activities(
    timeline_blocks: List[Dict[str, Any]],
    activities: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not timeline_blocks:
        return activities

    existing_keys = set()
    for block in timeline_blocks:
        if isinstance(block, dict) and _is_exportable_activity(block):
            existing_keys.update(_activity_identity_values(block))

    merged = list(timeline_blocks)
    for activity in activities:
        if not isinstance(activity, dict):
            continue
        if str(activity.get("source") or "").strip().lower() == "google_calendar":
            continue
        if not _is_exportable_activity(activity):
            continue
        activity_keys = _activity_identity_values(activity)
        if activity_keys and existing_keys.intersection(activity_keys):
            continue
        merged.append(activity)
        existing_keys.update(activity_keys)

    return sorted(merged, key=lambda item: _time_text_to_minutes(_item_start_end(item)[0]))


def _is_display_only(block: Dict[str, Any]) -> bool:
    return bool(block.get("display_only") or block.get("is_route_conflict"))


def _is_exportable_travel(block: Dict[str, Any]) -> bool:
    kind = _normalize_export_type(block)
    if _is_display_only(block):
        return False
    if kind in EXPORTABLE_TRAVEL_TYPES:
        return True
    title = str(block.get("title") or "").lower()
    return kind == "activity_support" and "travel" in title


def _looks_like_legacy_activity(block: Dict[str, Any]) -> bool:
    kind = _normalize_export_type(block)
    if kind:
        return False
    title = str(block.get("title") or block.get("activity") or "").strip().lower()
    if not title:
        return False
    if title in {"free time", "prep / buffer"} or title.startswith("travel to") or "route warning" in title:
        return False
    start, end = _item_start_end(block)
    return bool(start and end)


def _is_exportable_activity(block: Dict[str, Any]) -> bool:
    kind = _normalize_export_type(block)
    return not _is_display_only(block) and (kind == "activity" or _looks_like_legacy_activity(block))


def _has_unresolved_start_route_conflict(plan: Dict[str, Any]) -> bool:
    for conflict in plan.get("route_conflicts") or []:
        if not isinstance(conflict, dict):
            continue
        reason_code = str(conflict.get("reason_code") or "")
        has_start_marker = any(
            conflict.get(field)
            for field in ("leave_by", "first_physical_event", "blocker_activity_id", "blocker_activity_title")
        )
        if reason_code == "start_route_blocker" or (reason_code == "fixed_to_fixed_infeasible" and has_start_marker):
            return True
    return False


def _start_route_export_event(plan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if _has_unresolved_start_route_conflict(plan):
        return None
    summary = plan.get("start_route_summary")
    if not isinstance(summary, dict):
        return None

    start_location = _first_text(summary.get("start_location"))
    destination = _first_text(
        summary.get("first_physical_event_location"),
        summary.get("destination_location"),
        summary.get("to_location"),
        summary.get("first_physical_event"),
    )
    leave_by = _first_text(summary.get("leave_by"))
    duration = summary.get("travel_duration_minutes")
    if not start_location or not destination or not leave_by or not duration:
        return None

    end_time = _first_text(summary.get("first_physical_event_start"))
    if not end_time:
        try:
            leave_minutes = datetime.strptime(leave_by, "%I:%M %p").hour * 60 + datetime.strptime(leave_by, "%I:%M %p").minute
            end_time = _format_minutes_as_time(leave_minutes + int(duration)) or ""
        except Exception:
            end_time = ""
    if not end_time:
        return None

    return {
        "summary": f"Travel to {destination}",
        "startTime": leave_by,
        "endTime": end_time,
        "location": destination,
        "description": "\n".join([
            JPLAN_EXPORT_MARKER,
            f"Route: {start_location} -> {destination}",
            f"Travel time: {duration} min",
        ]),
        "jplan_export_type": "travel",
        "block_id": "start-route",
        "block_type": "travel",
        "is_travel_block": True,
    }


def build_google_export_events(plan: Dict[str, Any], date_str: str) -> Dict[str, Any]:
    """Build Google Calendar event payloads from the committed JPlan timeline."""
    activities = plan.get("activities") or []
    activity_by_key = {
        _activity_key(activity): activity
        for activity in activities
        if isinstance(activity, dict)
    }
    blocks = plan.get("committed_schedule_blocks") or plan.get("schedule_blocks") or []
    export_source = _merge_missing_canonical_activities(blocks, activities) if blocks else activities

    events: List[Dict[str, Any]] = []
    skipped_count = 0
    activity_count = 0
    travel_count = 0

    start_route_event = _start_route_export_event(plan)
    if start_route_event:
        metadata = {
            "source_system": "jplan",
            "jplan_schedule_id": plan.get("scheduleId") or plan.get("schedule_id") or "",
            "stable_activity_id": "",
            "block_id": start_route_event.get("block_id") or "start-route",
            "block_type": "travel",
            "is_travel_block": "true",
        }
        start_route_event["jplan_metadata"] = metadata
        start_route_event["description"] = "\n".join([
            start_route_event.get("description") or JPLAN_EXPORT_MARKER,
            f"{JPLAN_META_MARKER} {json.dumps(metadata, separators=(',', ':'))}",
        ])
        events.append(start_route_event)
        travel_count += 1

    for raw in export_source:
        if not isinstance(raw, dict):
            skipped_count += 1
            continue

        kind = _normalize_export_type(raw)
        if _is_display_only(raw):
            skipped_count += 1
            continue

        is_activity = _is_exportable_activity(raw)
        is_travel = _is_exportable_travel(raw)
        if kind in SKIPPED_EXPORT_TYPES and not is_travel:
            skipped_count += 1
            continue
        if not is_activity and not is_travel:
            skipped_count += 1
            continue

        source_activity = activity_by_key.get(_activity_key(raw), {}) if is_activity else {}
        item = {**source_activity, **raw}
        start_time, end_time = _item_start_end(item)
        if not start_time or not end_time:
            skipped_count += 1
            continue

        if is_activity:
            summary = _first_text(item.get("title"), item.get("activity"), "JPlan Activity")
            description = JPLAN_EXPORT_MARKER
            activity_count += 1
        else:
            destination = _first_text(item.get("to_activity"), item.get("to_location"), item.get("location"))
            summary = _first_text(item.get("title"), f"Travel to {destination}" if destination else "Travel")
            duration = item.get("duration_minutes") or item.get("route_duration_minutes")
            from_label = _first_text(item.get("from_activity"), item.get("from_location"))
            to_label = _first_text(item.get("to_activity"), item.get("to_location"))
            details = [JPLAN_EXPORT_MARKER]
            if from_label or to_label:
                details.append(f"Route: {from_label or 'previous stop'} -> {to_label or 'next stop'}")
            if duration:
                details.append(f"Travel time: {duration} min")
            description = "\n".join(details)
            travel_count += 1

        location = _best_location_label(item)
        block_type = "activity" if is_activity else "travel"
        block_id = str(
            item.get("block_id")
            or item.get("stable_activity_id")
            or item.get("id")
            or f"{block_type}-{len(events)}"
        )
        stable_activity_id = item.get("stable_activity_id") if is_activity else item.get("related_activity_id")
        metadata = {
            "source_system": "jplan",
            "jplan_schedule_id": plan.get("scheduleId") or plan.get("schedule_id") or "",
            "stable_activity_id": stable_activity_id or "",
            "block_id": block_id,
            "block_type": block_type,
            "is_travel_block": "true" if is_travel else "false",
        }
        description = "\n".join([
            description,
            f"{JPLAN_META_MARKER} {json.dumps(metadata, separators=(',', ':'))}",
        ])
        event = {
            "summary": summary,
            "startTime": start_time,
            "endTime": end_time,
            "description": description,
            "jplan_export_type": block_type,
            "block_id": block_id,
            "stable_activity_id": stable_activity_id,
            "calendar_event_id": item.get("calendar_event_id"),
            "jplan_metadata": metadata,
        }
        if location:
            event["location"] = location
        events.append(event)

    _calendar_log(
        f"Prepared Google export date={date_str} activities={activity_count} travel={travel_count} skipped={skipped_count}",
        "EXPORT",
    )
    return {
        "events": events,
        "activity_count": activity_count,
        "travel_count": travel_count,
        "skipped_count": skipped_count,
    }


class CalendarService:
    def __init__(self, supabase_client: Client):
        self.supabase = supabase_client
        self.client_id = os.getenv("GOOGLE_CLIENT_ID")
        self.client_secret = os.getenv("GOOGLE_CLIENT_SECRET")

    @staticmethod
    def _empty_export_result() -> Dict[str, int]:
        return {
            "exported_count": 0,
            "activity_count": 0,
            "travel_count": 0,
            "skipped_count": 0,
            "updated_count": 0,
            "created_count": 0,
            "exported_events": [],
        }

    def _get_refresh_token(self, user_id: str) -> Optional[str]:
        res = self.supabase.table("profiles").select("google_refresh_token").eq("id", user_id).execute()
        if not res.data or len(res.data) == 0:
            return None
        return res.data[0].get("google_refresh_token")

    def has_refresh_token(self, user_id: str) -> bool:
        try:
            return bool(self._get_refresh_token(user_id))
        except Exception:
            return False

    def list_calendars(self, access_token: str) -> List[Dict[str, Any]]:
        """List all calendars available to the user"""
        url = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            return response.json().get("items", [])
        except Exception as e:
            _calendar_log(f"Error listing calendars: {e}", "ERROR")
            return []

    def refresh_access_token(self, refresh_token: str) -> Optional[str]:
        """
        Exchange a refresh token for a new access token
        """
        if not self.client_id or not self.client_secret:
            _calendar_log("GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET not set", "ERROR")
            raise Exception("GOOGLE_OAUTH_CONFIG_MISSING")

        url = "https://oauth2.googleapis.com/token"
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }

        try:
            response = requests.post(url, data=data)
            if not response.ok:
                try:
                    error_payload = response.json()
                except Exception:
                    error_payload = {}
                google_error = error_payload.get("error")
                google_description = error_payload.get("error_description") or response.text
                _calendar_log(
                    f"Error refreshing access token: status={response.status_code} error={google_error} description={google_description}",
                    "ERROR",
                )
                if google_error in {"invalid_client", "unauthorized_client"}:
                    raise Exception("GOOGLE_OAUTH_CONFIG_MISMATCH")
                if google_error == "invalid_grant":
                    raise Exception("TOKEN_EXPIRED")
                raise Exception("GOOGLE_TOKEN_REFRESH_FAILED")
            return response.json().get("access_token")
        except Exception as e:
            if str(e) in {
                "GOOGLE_OAUTH_CONFIG_MISSING",
                "GOOGLE_OAUTH_CONFIG_MISMATCH",
                "GOOGLE_TOKEN_REFRESH_FAILED",
                "TOKEN_EXPIRED",
            }:
                raise e
            _calendar_log(f"Error refreshing access token: {e}", "ERROR")
            raise Exception("GOOGLE_TOKEN_REFRESH_FAILED")

    def get_calendar_events(self, access_token: str, time_min: str, time_max: str) -> List[Dict[str, Any]]:
        """
        Fetch events from Google Calendar
        """
        url = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
        headers = {"Authorization": f"Bearer {access_token}"}
        params = {
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": True,
            "orderBy": "startTime",
        }

        try:
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            return response.json().get("items", [])
        except Exception as e:
            _calendar_log(f"Error fetching calendar events: {e}", "ERROR")
            return []

    def sync_user_calendar(self, user_id: str, date_str: str) -> List[Dict[str, Any]]:
        """
        Main entry point to sync a user's calendar for a specific day
        """
        # 1. Get refresh token from DB
        try:
            # Use execute() instead of single() to handle "0 rows" gracefully
            res = self.supabase.table("profiles").select("google_refresh_token").eq("id", user_id).execute()
            
            if not res.data or len(res.data) == 0:
                _calendar_log(f"No profile found for user {user_id}", "ERROR")
                return []
                
            refresh_token = res.data[0].get("google_refresh_token")
            
            if not refresh_token:
                _calendar_debug(f"Profile found but no refresh token for user {user_id}")
                return []

            # 2. Refresh access token
            access_token = self.refresh_access_token(refresh_token)
            _calendar_debug(f"Access token obtained: {'yes' if access_token else 'no'}")
            if not access_token:
                _calendar_debug("Failed to get access token")
                return []

            # 3. Define time range for the date (fetching with a buffer to handle timezones)
            # Fetching from 12 hours before to 12 hours after the date to be safe
            time_min = f"{date_str}T00:00:00Z"
            time_max = f"{date_str}T23:59:59Z"
            
            # IMPROVEMENT: Fetch a 48-hour window surrounding the target date and filter locally
            # This is more robust against timezone shifts
            fetch_min = f"{date_str}T00:00:00Z" # We'll still use this but backend will filter
            
            # Actually, let's just fetch everything for the given "day" at UTC 
            # and let the frontend/backend logic be a bit more flexible.
            # Most users in Asia/Europe will be missed by a strict "T00:00:00Z" if their event is early.
            
            # Dynamic calculation to cover the full local day regardless of timezone (-12 to +14)
            time_min = f"{date_str}T00:00:00Z" # Starting from UTC start
            # To be safe for users in GMT+8 (like user), subtract 12 hours
            from datetime import timedelta
            target_date = datetime.fromisoformat(date_str)
            start_utc = (target_date - timedelta(hours=14)).isoformat() + "Z"
            end_utc = (target_date + timedelta(hours=38)).isoformat() + "Z"
            
            _calendar_debug(f"Fetching events from {start_utc} to {end_utc}")

            # 4. Fetch events
            # DEBUG: List all calendars first to see if primary is correct
            calendars = self.list_calendars(access_token)
            _calendar_debug(f"Available calendars: {[c.get('summary') for c in calendars]}")
            
            events = self.get_calendar_events(access_token, start_utc, end_utc)
            _calendar_debug(f"Raw events fetched from primary: {len(events)}")

            # 5. Filter events that actually fall into the target date (local time)
            formatted_activities = []
            for event in events:
                start_obj = event.get("start", {})
                end_obj = event.get("end", {})
                
                start_raw = start_obj.get("dateTime") or start_obj.get("date")
                end_raw = end_obj.get("dateTime") or end_obj.get("date")
                summary = event.get("summary", "Untitled Event")
                
                _calendar_debug(f"Evaluating event: '{summary}' | start={start_raw}")
                
                event_date = start_raw.split("T")[0] if "T" in start_raw else start_raw
                
                if event_date != date_str:
                    _calendar_debug(f"Skipping event_date={event_date} target={date_str}")
                    continue
                
                _calendar_debug(f"Including event: {summary}")
                
                if "T" in start_raw:
                    try:
                        dt_start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                        start_time = dt_start.strftime("%I:%M %p")
                        
                        dt_end = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
                        end_time = dt_end.strftime("%I:%M %p")
                    except Exception:
                        start_time = start_raw[11:16]
                        end_time = end_raw[11:16]
                else:
                    start_time = "All Day"
                    end_time = "All Day"

                formatted_activities.append({
                    "id": event.get("id"),
                    "original_google_event_id": event.get("id"),
                    "google_event_id": event.get("id"),
                    "startTime": start_time,
                    "endTime": end_time,
                    "activity": summary,
                    "title": summary,
                    "category": "External",
                    "source_system": "google_calendar",
                    "read_only": True,
                })

            return formatted_activities

        except Exception as e:
            _calendar_log(f"Sync failed for user {user_id}: {e}", "ERROR")
            return []
    def sync_upcoming_events(
        self,
        user_id: str,
        days_ahead: int = 60,
        start_date: Optional[str] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Fetch all events from now to X days in the future and group by date
        """
        try:
            # 1. Get refresh token
            res = self.supabase.table("profiles").select("google_refresh_token").eq("id", user_id).execute()
            if not res.data or len(res.data) == 0:
                _calendar_log(f"No profile found for user {user_id}", "ERROR")
                raise Exception("TOKEN_EXPIRED")
            
            refresh_token = res.data[0].get("google_refresh_token")
            if not refresh_token:
                _calendar_log(f"Profile found but no refresh token for user {user_id}", "WARNING")
                raise Exception("TOKEN_EXPIRED")

            # 2. Get access token
            try:
                access_token = self.refresh_access_token(refresh_token)
                if not access_token:
                    raise Exception("TOKEN_EXPIRED")
            except Exception as e:
                _calendar_log(f"Access token refresh failed for user {user_id}: {e}", "ERROR")
                if str(e) in {
                    "GOOGLE_OAUTH_CONFIG_MISSING",
                    "GOOGLE_OAUTH_CONFIG_MISMATCH",
                    "GOOGLE_TOKEN_REFRESH_FAILED",
                    "TOKEN_EXPIRED",
                }:
                    raise e
                raise Exception("TOKEN_EXPIRED")

            # 3. Time range. Start from the beginning of the selected/local day
            # instead of "now" so importing today's calendar does not miss
            # already-started events.
            if start_date:
                range_start = datetime.fromisoformat(start_date) - timedelta(hours=14)
            else:
                range_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=14)
            range_end = range_start + timedelta(days=days_ahead, hours=38)
            time_min = range_start.isoformat() + "Z"
            time_max = range_end.isoformat() + "Z"
            
            _calendar_debug(f"Fetching future events from {time_min} to {time_max}")

            # 4. Fetch
            events = self.get_calendar_events(access_token, time_min, time_max)
            _calendar_debug(f"Total future events fetched: {len(events)}")

            # 5. Group and format
            grouped_data = {}
            for event in events:
                start_obj = event.get("start", {})
                end_obj = event.get("end", {})
                
                start_raw = start_obj.get("dateTime") or start_obj.get("date")
                end_raw = end_obj.get("dateTime") or end_obj.get("date")
                summary = event.get("summary", "Untitled Event")
                
                # Extract date "YYYY-MM-DD"
                event_date = start_raw.split("T")[0] if "T" in start_raw else start_raw
                
                if event_date not in grouped_data:
                    grouped_data[event_date] = []
                
                grouped_data[event_date].append(event)

            return grouped_data

        except Exception as e:
            if str(e) == "TOKEN_EXPIRED":
                raise e
            _calendar_log(f"Bulk sync failed for user {user_id}: {e}", "ERROR")
            return {}

    def export_schedule_to_google(
        self,
        user_id: str,
        date_str: str,
        plan: Dict[str, Any],
        replace_google_day: bool = False,
    ) -> Dict[str, int]:
        """
        Push the committed JPlan timeline to Google Calendar.
        """
        try:
            # 1. Get refresh token
            refresh_token = self._get_refresh_token(user_id)
            if not refresh_token:
                _calendar_log(f"No profile found for user {user_id}", "ERROR")
                raise Exception("TOKEN_EXPIRED")

            # 2. Get access token
            try:
                access_token = self.refresh_access_token(refresh_token)
                if not access_token:
                    raise Exception("TOKEN_EXPIRED")
            except Exception as e:
                if str(e) in {
                    "GOOGLE_OAUTH_CONFIG_MISSING",
                    "GOOGLE_OAUTH_CONFIG_MISMATCH",
                    "GOOGLE_TOKEN_REFRESH_FAILED",
                    "TOKEN_EXPIRED",
                }:
                    raise e
                raise Exception("TOKEN_EXPIRED")

            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            }

            deleted_count = 0
            
            def parse_time_to_dt(t_str, d_str):
                if not t_str or t_str == "All Day":
                    return None
                try:
                    # Input: "09:00 AM", "2026-04-15"
                    return datetime.strptime(f"{d_str} {t_str}", "%Y-%m-%d %I:%M %p")
                except Exception as parse_err:
                    _calendar_log(f"Time parse error for '{t_str}': {parse_err}", "ERROR")
                    return None

            # 3. Fetch existing JPlan-created events for idempotent updates.
            target_date_dt = datetime.fromisoformat(date_str)
            clean_min = (target_date_dt - timedelta(hours=14)).isoformat() + "Z"
            clean_max = (target_date_dt + timedelta(hours=38)).isoformat() + "Z"
            existing_google_events = self.get_calendar_events(access_token, clean_min, clean_max)
            if replace_google_day:
                day_min = f"{date_str}T00:00:00+08:00"
                day_max = f"{(target_date_dt + timedelta(days=1)).strftime('%Y-%m-%d')}T00:00:00+08:00"
                events_to_delete = self.get_calendar_events(access_token, day_min, day_max)
                _calendar_log(
                    f"Replacing Google day date={date_str}; deleting {len(events_to_delete)} existing event(s)",
                    "EXPORT_REPLACE",
                )
                for event in events_to_delete:
                    event_id = event.get("id")
                    if not event_id:
                        continue
                    delete_url = f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{event_id}"
                    delete_resp = requests.delete(delete_url, headers=headers)
                    if delete_resp.status_code in {200, 204, 410, 404}:
                        if delete_resp.status_code in {200, 204}:
                            deleted_count += 1
                        continue
                    if delete_resp.status_code == 403:
                        _calendar_log(f"403 Forbidden deleting calendar event: {delete_resp.text}", "ERROR")
                        raise Exception("insufficientPermissions")
                    delete_resp.raise_for_status()
                    deleted_count += 1
                existing_google_events = []

            existing_by_block_id: Dict[str, str] = {}
            for gev in existing_google_events:
                metadata = extract_jplan_metadata(gev)
                if str(metadata.get("source_system") or "").lower() != "jplan":
                    continue
                block_id = metadata.get("block_id")
                if block_id and gev.get("id"):
                    existing_by_block_id[str(block_id)] = str(gev["id"])

            sync_by_block_id = {
                str(link.get("block_id")): str(link.get("calendar_event_id") or link.get("google_event_id"))
                for link in (plan.get("sync_links") or [])
                if link.get("block_id") and (link.get("calendar_event_id") or link.get("google_event_id"))
            } if not replace_google_day else {}

            # 4. Push each exportable timeline item
            if not plan.get("committed_schedule_blocks"):
                plan = {**plan, "committed_schedule_blocks": materialize_committed_blocks(plan)}
            export_payload = build_google_export_events(plan, date_str)
            export_events = export_payload["events"]
            exported_count = 0
            updated_count = 0
            created_count = 0
            exported_event_links: List[Dict[str, Any]] = []
            _calendar_debug(f"Starting export user={user_id} date={date_str} items={len(export_events)}")
            for event_item in export_events:
                _calendar_debug(f"Processing export item='{event_item.get('summary')}' type={event_item.get('jplan_export_type')}")

                start_dt = parse_time_to_dt(event_item.get("startTime"), date_str)
                end_dt = parse_time_to_dt(event_item.get("endTime"), date_str)
                
                if not start_dt or not end_dt:
                    _calendar_debug(f"Skipping '{event_item.get('summary')}' because time parsing failed")
                    continue

                # FIX: Handle cases where end time is physically before start time (e.g. crossing midnight)
                if end_dt <= start_dt:
                    _calendar_debug(f"End time {end_dt} is before start {start_dt}; assuming next day")
                    end_dt += timedelta(days=1)

                start_payload = {"dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S") + "+08:00"}
                end_payload = {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S") + "+08:00"}
                metadata = event_item.get("jplan_metadata") or {
                    "source_system": "jplan",
                    "jplan_schedule_id": plan.get("scheduleId") or "",
                    "stable_activity_id": event_item.get("stable_activity_id") or "",
                    "block_id": event_item.get("block_id") or "",
                    "block_type": event_item.get("jplan_export_type") or "",
                    "is_travel_block": "true" if event_item.get("jplan_export_type") == "travel" else "false",
                }

                event = {
                    "summary": event_item.get("summary", "JPlan Activity"),
                    "description": event_item.get("description") or JPLAN_EXPORT_MARKER,
                    "start": start_payload,
                    "end": end_payload,
                    "reminders": {"useDefault": True},
                    "extendedProperties": {
                        "private": metadata,
                    },
                }
                if event_item.get("location"):
                    event["location"] = event_item["location"]
                
                _calendar_debug(f"Sending event payload to Google: {json.dumps(event)}")

                block_id = str(event_item.get("block_id") or metadata.get("block_id") or "")
                existing_event_id = (
                    event_item.get("calendar_event_id")
                    or sync_by_block_id.get(block_id)
                    or existing_by_block_id.get(block_id)
                )
                if existing_event_id:
                    url = f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{existing_event_id}"
                    resp = requests.patch(url, headers=headers, json=event)
                    updated_count += 1
                else:
                    url = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
                    resp = requests.post(url, headers=headers, json=event)
                    created_count += 1
                
                if resp.status_code == 403:
                    _calendar_log(f"403 Forbidden - likely missing scopes: {resp.text}", "ERROR")
                    raise Exception("insufficientPermissions")
                
                resp.raise_for_status()
                exported_count += 1
                response_payload = {}
                try:
                    response_payload = resp.json()
                except Exception:
                    response_payload = {}
                calendar_event_id = response_payload.get("id") or existing_event_id
                exported_event_links.append({
                    "calendar_event_id": calendar_event_id,
                    "google_event_id": calendar_event_id,
                    "block_id": block_id,
                    "stable_activity_id": event_item.get("stable_activity_id"),
                    "block_type": event_item.get("jplan_export_type"),
                })

            _calendar_log(
                f"Successfully exported {exported_count} timeline items to Google "
                f"(activities={export_payload['activity_count']} travel={export_payload['travel_count']} "
                f"created={created_count} updated={updated_count})",
                "EXPORT",
            )
            return {
                "exported_count": exported_count,
                "activity_count": export_payload["activity_count"],
                "travel_count": export_payload["travel_count"],
                "skipped_count": export_payload["skipped_count"],
                "deleted_count": deleted_count,
                "updated_count": updated_count,
                "created_count": created_count,
                "exported_events": exported_event_links,
            }

        except Exception as e:
            if "TOKEN_EXPIRED" in str(e) or "insufficientPermissions" in str(e):
                raise e
            _calendar_log(f"Export failed for user {user_id}: {e}", "ERROR")
            return self._empty_export_result()
