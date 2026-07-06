"""Calendar import/export state helpers for separated JPlan state.

The Supabase column is still named ``daily_plans.activities`` for backwards
compatibility, but the JSON envelope stored inside it keeps calendar sources
separate:

* activities: canonical JPlan tasks only
* committed_schedule_blocks: saved/exported JPlan timeline
* external_calendar_events: read-only Google Calendar layer
* sync_links: links from JPlan blocks to Google Calendar events
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import uuid4

from jplan_logging import jlog

JPLAN_META_MARKER = "[JPLAN_META]"
JPLAN_EXPORT_MARKER = "Created via JPlan"

SUPPORT_BLOCK_TYPES = {
    "buffer",
    "free",
    "free_time",
    "idle",
    "prep",
    "prep_buffer",
    "route_conflict",
    "start_route",
    "support",
    "transition",
    "travel",
}

SUPPORT_TITLES = {
    "buffer",
    "free time",
    "prep",
    "prep / buffer",
    "prep buffer",
    "travel",
    "travel time",
}


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _clean_key(value: Any) -> str:
    return _clean_text(value).lower().replace("-", "_")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _event_id(event: Dict[str, Any]) -> Optional[str]:
    return (
        event.get("id")
        or event.get("google_event_id")
        or event.get("calendar_event_id")
        or event.get("original_google_event_id")
    )


def parse_clock_minutes(value: Any) -> Optional[int]:
    text = _clean_text(value)
    if not text or text.lower() == "all day":
        return None
    for fmt in ("%I:%M %p", "%H:%M", "%H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.hour * 60 + dt.minute
        except ValueError:
            continue
    return None


def minutes_to_display(total: int) -> str:
    total %= 24 * 60
    hour = total // 60
    minute = total % 60
    suffix = "AM" if hour < 12 else "PM"
    hour_12 = hour % 12 or 12
    return f"{hour_12:02d}:{minute:02d} {suffix}"


def google_event_times(event: Dict[str, Any]) -> Tuple[str, str, Optional[str]]:
    start_obj = _dict(event.get("start"))
    end_obj = _dict(event.get("end"))
    start_raw = start_obj.get("dateTime") or start_obj.get("date") or event.get("startTime") or event.get("start")
    end_raw = end_obj.get("dateTime") or end_obj.get("date") or event.get("endTime") or event.get("end")

    date_str = None
    if start_raw:
        date_str = str(start_raw).split("T")[0]

    if start_raw and end_raw and "T" in str(start_raw):
        try:
            dt_start = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
            dt_end = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
            return dt_start.strftime("%I:%M %p"), dt_end.strftime("%I:%M %p"), date_str
        except Exception:
            return str(start_raw)[11:16], str(end_raw)[11:16], date_str

    if start_raw and end_raw and "T" not in str(start_raw):
        return "All Day", "All Day", date_str

    return _clean_text(event.get("startTime") or event.get("start")), _clean_text(event.get("endTime") or event.get("end")), date_str


def extract_jplan_metadata(event_or_block: Dict[str, Any]) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    extended = _dict(_dict(event_or_block.get("extendedProperties")).get("private"))
    if extended:
        metadata.update(extended)

    direct_fields = (
        "source_system",
        "jplan_schedule_id",
        "stable_activity_id",
        "block_id",
        "block_type",
        "is_travel_block",
    )
    for field in direct_fields:
        if event_or_block.get(field) is not None:
            metadata[field] = event_or_block.get(field)

    description = str(event_or_block.get("description") or "")
    if JPLAN_META_MARKER in description:
        after_marker = description.split(JPLAN_META_MARKER, 1)[1].strip()
        first_line = after_marker.splitlines()[0] if after_marker else ""
        try:
            parsed = json.loads(first_line)
            if isinstance(parsed, dict):
                metadata.update(parsed)
        except Exception:
            for line in after_marker.splitlines():
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                metadata[key.strip()] = value.strip()
    elif JPLAN_EXPORT_MARKER in description and not metadata.get("source_system"):
        metadata["source_system"] = "jplan"

    return metadata


def is_jplan_created_event(event: Dict[str, Any], sync_links: Optional[List[Dict[str, Any]]] = None) -> bool:
    metadata = extract_jplan_metadata(event)
    if _clean_key(metadata.get("source_system")) == "jplan":
        return True

    event_id = _event_id(event)
    if not event_id:
        return False
    for link in _list(sync_links):
        if str(link.get("calendar_event_id") or link.get("google_event_id") or "") == str(event_id):
            return True
    return False


def is_support_like(item: Dict[str, Any]) -> bool:
    title = _clean_text(item.get("title") or item.get("summary") or item.get("activity"))
    title_key = title.lower()
    block_type = _clean_key(item.get("block_type") or item.get("type") or item.get("jplan_export_type"))
    if title_key.startswith("travel to"):
        return True
    if title_key in SUPPORT_TITLES:
        return True
    if block_type in SUPPORT_BLOCK_TYPES and block_type != "activity":
        return True
    return False


def is_canonical_activity(item: Dict[str, Any]) -> bool:
    if not isinstance(item, dict):
        return False
    if _clean_key(item.get("source")) == "google_calendar":
        return False
    if is_support_like(item):
        return False
    block_type = _clean_key(item.get("block_type") or item.get("type"))
    if block_type and block_type in SUPPORT_BLOCK_TYPES:
        return False
    return True


def _block_identity(item: Dict[str, Any], fallback: str) -> str:
    return str(
        item.get("block_id")
        or item.get("stable_activity_id")
        or item.get("activity_id")
        or item.get("source_activity_id")
        or item.get("id")
        or fallback
    )


def _stable_activity_id(item: Dict[str, Any]) -> str:
    return str(item.get("stable_activity_id") or item.get("activity_id") or item.get("id") or f"act-{uuid4().hex[:12]}")


def _has_jplan_state(schedule: Dict[str, Any]) -> bool:
    return bool(
        _list(schedule.get("activities"))
        or _list(schedule.get("schedule_blocks"))
        or _list(schedule.get("committed_schedule_blocks"))
    )


def _canonical_activity_from_external(event: Dict[str, Any]) -> Dict[str, Any]:
    start_time = event.get("startTime") or event.get("start")
    end_time = event.get("endTime") or event.get("end")
    start_minutes = parse_clock_minutes(start_time)
    end_minutes = parse_clock_minutes(end_time)
    if start_minutes is not None and end_minutes is not None and end_minutes <= start_minutes:
        end_minutes += 24 * 60
    duration = (
        max(1, end_minutes - start_minutes)
        if start_minutes is not None and end_minutes is not None
        else event.get("duration_minutes") or 60
    )
    google_id = event.get("original_google_event_id") or event.get("google_event_id") or event.get("calendar_event_id") or event.get("id")
    stable_id = event.get("stable_activity_id") or f"gcal-{google_id or uuid4().hex[:12]}"

    activity = {
        "id": stable_id,
        "stable_activity_id": stable_id,
        "type": "activity",
        "block_type": "activity",
        "title": event.get("title") or event.get("activity") or "Imported Google event",
        "startTime": start_time,
        "endTime": end_time,
        "start": start_time,
        "end": end_time,
        "duration_minutes": duration,
        "source": "imported_google_calendar",
        "original_google_event_id": google_id,
        "calendar_event_id": event.get("calendar_event_id") or event.get("google_event_id") or google_id,
        "status": "active",
        "timing_mode": "fixed" if start_minutes is not None else event.get("timing_mode") or "unspecified",
        "is_user_fixed": start_minutes is not None,
        "fixed_start": start_minutes,
        "fixed_end": end_minutes,
        "scheduled_start": start_minutes,
        "scheduled_end": end_minutes,
        "location": event.get("location"),
        "notes": event.get("notes") or event.get("description"),
    }
    return {key: value for key, value in activity.items() if value is not None}


def _schedule_block_from_activity(activity: Dict[str, Any]) -> Dict[str, Any]:
    block_id = (
        activity.get("block_id")
        or activity.get("stable_activity_id")
        or activity.get("id")
        or f"block-{uuid4().hex[:12]}"
    )
    start_time = activity.get("startTime") or activity.get("start")
    end_time = activity.get("endTime") or activity.get("end")
    block = {
        **activity,
        "id": block_id,
        "block_id": block_id,
        "activity_id": activity.get("stable_activity_id") or activity.get("id") or block_id,
        "related_activity_id": activity.get("stable_activity_id") or activity.get("id") or block_id,
        "type": "activity",
        "block_type": "activity",
        "startTime": start_time,
        "endTime": end_time,
        "start": start_time,
        "end": end_time,
    }
    return {key: value for key, value in block.items() if value is not None}


def _sync_imported_activities_to_draft_blocks(schedule: Dict[str, Any], imported_activities: List[Dict[str, Any]]) -> None:
    if not imported_activities:
        return

    base_blocks = _list(schedule.get("schedule_blocks")) or _list(schedule.get("committed_schedule_blocks"))
    if not base_blocks:
        base_blocks = [
            _schedule_block_from_activity(activity)
            for activity in _list(schedule.get("activities"))
            if is_canonical_activity(activity)
        ]

    existing_keys = {
        _block_identity(block, str(index))
        for index, block in enumerate(base_blocks)
        if isinstance(block, dict)
    }
    draft_blocks = list(base_blocks)
    for activity in imported_activities:
        block = _schedule_block_from_activity(activity)
        key = _block_identity(block, str(len(draft_blocks)))
        if key in existing_keys:
            continue
        draft_blocks.append(block)
        existing_keys.add(key)

    schedule["schedule_blocks"] = sorted(
        draft_blocks,
        key=lambda block: (
            parse_clock_minutes(block.get("startTime") or block.get("start")) is None,
            parse_clock_minutes(block.get("startTime") or block.get("start")) or 0,
        ),
    )
    schedule["has_unsaved_draft"] = True
    schedule["draft_dirty"] = True
    schedule["needs_reschedule"] = True
    schedule["reschedule_reason"] = "calendar_import_selected"
    schedule["preview_id"] = None
    schedule["preview_status"] = None
    schedule["preview_reason"] = None
    schedule["preview_base_version"] = None
    schedule["preview_schedule"] = None


def google_event_to_external_event(event: Dict[str, Any], *, maybe_support_block: bool = False) -> Dict[str, Any]:
    start_time, end_time, event_date = google_event_times(event)
    google_id = _event_id(event)
    title = event.get("summary") or event.get("title") or event.get("activity") or "Untitled Event"
    external = {
        "id": f"gcal-{google_id or uuid4().hex[:12]}",
        "google_event_id": google_id,
        "calendar_event_id": google_id,
        "original_google_event_id": google_id,
        "type": "activity",
        "block_type": "activity",
        "title": title,
        "startTime": start_time,
        "endTime": end_time,
        "start": start_time,
        "end": end_time,
        "date": event_date,
        "location": event.get("location"),
        "description": event.get("description"),
        "source": "google_calendar",
        "source_system": "google_calendar",
        "category": "External",
        "read_only": True,
        "maybe_support_block": bool(maybe_support_block),
        "jplan_metadata": extract_jplan_metadata(event),
    }
    return {key: value for key, value in external.items() if value not in (None, "")}


def classify_google_event(event: Dict[str, Any], schedule: Dict[str, Any]) -> str:
    if is_jplan_created_event(event, schedule.get("sync_links") or []):
        return "jplan_created"
    if is_support_like(event):
        return "unknown_support_like"
    return "external"


def classify_google_events(events: Iterable[Dict[str, Any]], schedule: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    buckets = {"jplan_created": [], "external": [], "unknown_support_like": []}
    for event in events or []:
        if not isinstance(event, dict):
            continue
        buckets[classify_google_event(event, schedule)].append(event)
    jlog(
        "SYNC",
        (
            f"jplan_created={len(buckets['jplan_created'])} "
            f"external={len(buckets['external'])} unknown_support={len(buckets['unknown_support_like'])}"
        ),
        "CLASSIFY",
    )
    return buckets


def _event_key(event: Dict[str, Any]) -> str:
    return str(
        event.get("original_google_event_id")
        or event.get("google_event_id")
        or event.get("calendar_event_id")
        or event.get("id")
        or ""
    )


def _upsert_external_events(existing: List[Dict[str, Any]], incoming: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_key: Dict[str, Dict[str, Any]] = {}
    ordered_keys: List[str] = []
    for item in existing + incoming:
        key = _event_key(item) or _block_identity(item, f"external-{len(ordered_keys)}")
        if key not in by_key:
            ordered_keys.append(key)
        by_key[key] = item
    return [by_key[key] for key in ordered_keys]


def _block_type_for_commit(item: Dict[str, Any]) -> str:
    block_type = _clean_key(item.get("block_type") or item.get("type"))
    if block_type in {"transition", "travel", "start_route"}:
        return "travel"
    if block_type in {"prep", "prep_buffer", "buffer"} or "prep" in _clean_key(item.get("title")):
        return "prep_buffer"
    if block_type in {"free", "free_time", "idle"}:
        return "free_time"
    return "activity"


def materialize_committed_blocks(schedule: Dict[str, Any]) -> List[Dict[str, Any]]:
    source = _list(schedule.get("schedule_blocks"))
    if not source:
        source = _list(schedule.get("committed_schedule_blocks"))
    if not source:
        source = _list(schedule.get("activities"))

    committed: List[Dict[str, Any]] = []
    for index, raw in enumerate(source):
        if not isinstance(raw, dict):
            continue
        block = deepcopy(raw)
        block_type = _block_type_for_commit(block)
        block["block_type"] = block_type
        block["type"] = block.get("type") or ("travel" if block_type == "travel" else block_type)
        block["block_id"] = _block_identity(block, f"block-{index}")
        block["source_system"] = "jplan"
        if block_type == "activity":
            stable_id = _stable_activity_id(block)
            block["stable_activity_id"] = stable_id
            block["id"] = block.get("id") or stable_id
        else:
            related = (
                block.get("related_activity_id")
                or block.get("source_activity_id")
                or block.get("destination_activity_id")
                or block.get("stable_activity_id")
            )
            if related:
                block["related_activity_id"] = related
        committed.append(block)
    return committed


def sync_activity_times_from_blocks(activities: List[Dict[str, Any]], blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    block_by_id: Dict[str, Dict[str, Any]] = {}
    for block in blocks:
        if _block_type_for_commit(block) != "activity":
            continue
        for key in (block.get("stable_activity_id"), block.get("id")):
            if key:
                block_by_id[str(key)] = block

    updated: List[Dict[str, Any]] = []
    for raw in activities:
        activity = deepcopy(raw)
        match = None
        for key in (activity.get("stable_activity_id"), activity.get("id")):
            if key and str(key) in block_by_id:
                match = block_by_id[str(key)]
                break
        if match:
            for target, *sources in (
                ("startTime", "startTime", "start"),
                ("endTime", "endTime", "end"),
                ("start", "start", "startTime"),
                ("end", "end", "endTime"),
                ("scheduled_start", "scheduled_start"),
                ("scheduled_end", "scheduled_end"),
            ):
                for source in sources:
                    if match.get(source) is not None:
                        activity[target] = match.get(source)
                        break
        updated.append(activity)
    return updated


def cleanDirtySchedule(schedule: Dict[str, Any]) -> Dict[str, Any]:
    cleaned = deepcopy(schedule or {})
    cleaned.setdefault("activities", [])
    cleaned.setdefault("schedule_blocks", [])
    cleaned.setdefault("committed_schedule_blocks", [])
    cleaned.setdefault("external_calendar_events", [])
    cleaned.setdefault("sync_links", [])
    cleaned.setdefault("active_view", "jplan")

    kept_activities: List[Dict[str, Any]] = []
    moved_external: List[Dict[str, Any]] = []
    moved_committed: List[Dict[str, Any]] = []
    seen_activity_ids: set[str] = set()

    for raw in _list(cleaned.get("activities")):
        if not isinstance(raw, dict):
            continue
        item = deepcopy(raw)
        if _clean_key(item.get("source")) == "google_calendar" or is_support_like(item):
            if is_jplan_created_event(item, cleaned.get("sync_links") or []) or item.get("source_system") == "jplan":
                block = deepcopy(item)
                block["block_type"] = _block_type_for_commit(block)
                block["source_system"] = "jplan"
                moved_committed.append(block)
                jlog("STATE", f"support_or_google_to_committed title={item.get('title')}", "WARNING")
            else:
                external = google_event_to_external_event(item, maybe_support_block=is_support_like(item))
                moved_external.append(external)
                if _clean_key(item.get("source")) == "google_calendar":
                    jlog("STATE", f"google_calendar_in_activities title={item.get('title')}", "WARNING")
                if is_support_like(item):
                    jlog("STATE", f"support_block_in_activities title={item.get('title')}", "WARNING")
            continue

        stable_id = _stable_activity_id(item)
        if stable_id in seen_activity_ids:
            stable_id = f"act-{uuid4().hex[:12]}"
            item["id"] = stable_id
            item["stable_activity_id"] = stable_id
        else:
            item["id"] = item.get("id") or stable_id
            item["stable_activity_id"] = stable_id
        seen_activity_ids.add(stable_id)
        kept_activities.append(item)

    cleaned["activities"] = kept_activities
    if moved_committed:
        cleaned["committed_schedule_blocks"] = materialize_committed_blocks({
            **cleaned,
            "schedule_blocks": _list(cleaned.get("committed_schedule_blocks")) + moved_committed,
        })
    if moved_external:
        cleaned["external_calendar_events"] = _upsert_external_events(
            _list(cleaned.get("external_calendar_events")),
            moved_external,
        )
    return cleaned


def validate_state_invariants(schedule: Dict[str, Any], *, context: str = "") -> List[str]:
    warnings: List[str] = []
    for activity in _list(schedule.get("activities")):
        if not isinstance(activity, dict):
            continue
        title = activity.get("title")
        if _clean_key(activity.get("source")) == "google_calendar":
            warning = f"google_calendar_in_activities title={title}"
            warnings.append(warning)
            jlog("STATE", warning, "WARNING")
        if is_support_like(activity):
            warning = f"support_block_in_activities title={title}"
            warnings.append(warning)
            jlog("STATE", warning, "WARNING")

    if warnings:
        return warnings
    suffix = f" {context}=true" if context else ""
    jlog("STATE", f"after_{context}=true" if context else f"invariant_ok=true{suffix}", "INVARIANT_OK")
    return warnings


def prepare_schedule_for_save(schedule: Dict[str, Any]) -> Dict[str, Any]:
    prepared = cleanDirtySchedule(schedule)
    committed = materialize_committed_blocks(prepared)
    prepared["committed_schedule_blocks"] = committed
    prepared["activities"] = sync_activity_times_from_blocks(_list(prepared.get("activities")), committed)
    prepared["draft_dirty"] = False
    prepared["has_unsaved_draft"] = False
    prepared["active_view"] = "jplan"
    validate_state_invariants(prepared, context="save")
    return prepared


def _link_jplan_events(schedule: Dict[str, Any], events: List[Dict[str, Any]]) -> None:
    links = _list(schedule.get("sync_links"))
    committed = _prune_orphan_jplan_calendar_committed_blocks(schedule)
    by_block_id = {str(block.get("block_id")): block for block in committed if block.get("block_id")}

    for event in events:
        metadata = extract_jplan_metadata(event)
        event_id = _event_id(event)
        block_id = metadata.get("block_id")
        stable_id = metadata.get("stable_activity_id")
        if event_id and not block_id:
            block_id = next(
                (
                    link.get("block_id")
                    for link in links
                    if str(link.get("calendar_event_id") or link.get("google_event_id") or "") == str(event_id)
                    and link.get("block_id")
                ),
                None,
            )
        if event_id and block_id and str(block_id) in by_block_id:
            by_block_id[str(block_id)]["calendar_event_id"] = event_id
        link = {
            "calendar_event_id": event_id,
            "google_event_id": event_id,
            "block_id": block_id,
            "stable_activity_id": stable_id,
            "block_type": metadata.get("block_type"),
            "source_system": "jplan",
            "updated_at": _now_iso(),
        }
        if event_id and not any(str(existing.get("calendar_event_id")) == str(event_id) for existing in links):
            links.append({key: value for key, value in link.items() if value})

    schedule["sync_links"] = links
    schedule["committed_schedule_blocks"] = committed


def _prune_orphan_jplan_calendar_committed_blocks(schedule: Dict[str, Any]) -> List[Dict[str, Any]]:
    activities = _list(schedule.get("activities"))
    schedule_blocks = _list(schedule.get("schedule_blocks"))
    referenced_ids = {
        str(value)
        for item in activities + schedule_blocks
        for value in (
            item.get("stable_activity_id"),
            item.get("block_id"),
            item.get("id"),
        )
        if value
    }

    kept: List[Dict[str, Any]] = []
    for block in _list(schedule.get("committed_schedule_blocks")):
        if not isinstance(block, dict):
            continue
        block_id = str(block.get("block_id") or "")
        stable_id = str(block.get("stable_activity_id") or "")
        is_calendar_materialized = bool(
            block.get("calendar_event_id")
            or block.get("google_event_id")
            or "[JPLAN_META]" in str(block.get("description") or "")
        )
        is_jplan_calendar_block = (
            block.get("source_system") == "jplan"
            and block.get("read_only") is True
            and is_calendar_materialized
        )
        still_referenced = bool(
            (block_id and block_id in referenced_ids)
            or (stable_id and stable_id in referenced_ids)
        )
        if is_jplan_calendar_block and not still_referenced:
            jlog("STATE", f"removed_orphan_jplan_calendar_committed title={block.get('title')}", "WARNING")
            continue
        kept.append(block)
    return kept


def _jplan_event_to_committed_block(
    event: Dict[str, Any],
    metadata: Dict[str, Any],
    block_id: str,
) -> Dict[str, Any]:
    start_time, end_time, event_date = google_event_times(event)
    title = event.get("summary") or event.get("title") or event.get("activity") or "JPlan Calendar Event"
    raw_block_type = _clean_key(metadata.get("block_type"))
    title_key = str(title).strip().lower()
    if raw_block_type in {"travel", "transition", "start_route"} or title_key.startswith("travel to"):
        block_type = "travel"
    elif raw_block_type in {"buffer", "prep", "prep_buffer"} or "prep" in title_key or "buffer" in title_key:
        block_type = "prep_buffer"
    elif raw_block_type in {"free", "free_time", "idle"} or title_key == "free time":
        block_type = "free_time"
    elif is_support_like({"title": title, "block_type": raw_block_type}):
        block_type = raw_block_type or "travel"
        if block_type in {"buffer", "prep", "prep_buffer"}:
            block_type = "prep_buffer"
        elif block_type in {"free", "free_time", "idle"}:
            block_type = "free_time"
    else:
        block_type = "activity"

    stable_id = metadata.get("stable_activity_id") or (block_id if block_type == "activity" else None)
    block = {
        "id": stable_id or block_id,
        "block_id": block_id,
        "stable_activity_id": stable_id,
        "block_type": block_type,
        "type": "travel" if block_type == "travel" else block_type,
        "title": title,
        "startTime": start_time,
        "endTime": end_time,
        "start": start_time,
        "end": end_time,
        "date": event_date,
        "location": event.get("location"),
        "description": event.get("description"),
        "calendar_event_id": _event_id(event),
        "google_event_id": _event_id(event),
        "source_system": "jplan",
        "read_only": True,
    }
    return {key: value for key, value in block.items() if value not in (None, "")}


def apply_calendar_sync(
    schedule: Dict[str, Any],
    google_events: List[Dict[str, Any]],
    *,
    date: str,
) -> Dict[str, Any]:
    synced = cleanDirtySchedule(schedule or {"date": date, "activities": []})
    synced["date"] = synced.get("date") or date
    had_jplan_state_before_sync = _has_jplan_state(synced)
    buckets = classify_google_events(google_events, synced)

    _link_jplan_events(synced, buckets["jplan_created"])
    external_events = [
        google_event_to_external_event(event, maybe_support_block=False)
        for event in buckets["external"] + buckets["jplan_created"]
        if not is_support_like(event)
    ]
    synced["external_calendar_events"] = external_events
    if not had_jplan_state_before_sync and external_events:
        synced["activities"] = [_canonical_activity_from_external(event) for event in external_events]
        synced["active_view"] = "jplan"
        jlog(
            "IMPORT",
            f"date={date} imported={len(synced['activities'])} skipped_support={len(buckets['unknown_support_like'])}",
            "AUTO_BOOTSTRAP",
        )

    validate_state_invariants(synced, context="sync")
    return synced


def import_selected_calendar_events(schedule: Dict[str, Any], selected_event_ids: List[str]) -> Dict[str, Any]:
    imported = cleanDirtySchedule(schedule)
    selected = {str(event_id) for event_id in selected_event_ids or []}
    external = _list(imported.get("external_calendar_events"))
    existing_google_ids = {
        str(activity.get("original_google_event_id"))
        for activity in _list(imported.get("activities"))
        if activity.get("original_google_event_id")
    }

    appended = 0
    appended_activities: List[Dict[str, Any]] = []
    for event in external:
        event_id = str(_event_key(event))
        if selected and event_id not in selected:
            continue
        if event.get("maybe_support_block") or is_support_like(event):
            continue
        if event_id and event_id in existing_google_ids:
            continue
        activity = _canonical_activity_from_external(event)
        imported["activities"].append(activity)
        appended_activities.append(activity)
        if event_id:
            existing_google_ids.add(event_id)
        appended += 1

    _sync_imported_activities_to_draft_blocks(imported, appended_activities)
    imported["active_view"] = "jplan"
    jlog("IMPORT", f"imported={appended} selected={len(selected)}", "SELECTED")
    validate_state_invariants(imported, context="import")
    return imported


def replace_from_calendar_preview(schedule: Dict[str, Any], selected_event_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    cleaned = cleanDirtySchedule(schedule)
    selected = {str(event_id) for event_id in selected_event_ids or []}
    importable = [
        event for event in _list(cleaned.get("external_calendar_events"))
        if (not selected or str(_event_key(event)) in selected)
        and not event.get("maybe_support_block")
        and not is_support_like(event)
    ]
    preview = {
        "date": cleaned.get("date"),
        "events_to_import": importable,
        "jplan_activities_to_replace": _list(cleaned.get("activities")),
        "support_blocks_to_clear": _list(cleaned.get("schedule_blocks")) + _list(cleaned.get("committed_schedule_blocks")),
        "import_count": len(importable),
        "replace_count": len(_list(cleaned.get("activities"))),
        "clear_support_count": len(_list(cleaned.get("schedule_blocks"))) + len(_list(cleaned.get("committed_schedule_blocks"))),
        "requires_confirmation": True,
    }
    jlog(
        "IMPORT",
        f"import_count={preview['import_count']} replace_count={preview['replace_count']}",
        "REPLACE_PREVIEW",
    )
    return preview


def apply_replace_from_calendar(schedule: Dict[str, Any], selected_event_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    replaced = cleanDirtySchedule(schedule)
    preview = replace_from_calendar_preview(replaced, selected_event_ids)
    replaced["activities"] = [_canonical_activity_from_external(event) for event in preview["events_to_import"]]
    replaced["schedule_blocks"] = []
    replaced["committed_schedule_blocks"] = []
    replaced["active_view"] = "jplan"
    replaced["location_resolution_requests"] = []
    replaced["pending_repair_suggestions"] = []
    replaced["route_conflicts"] = []
    replaced["unfit_activities"] = []
    replaced["blocked_activities"] = []
    replaced["needs_travel_validation"] = False
    replaced["travel_validation_status"] = "not_requested"
    replaced["needs_reschedule"] = False
    replaced["reschedule_reason"] = None
    replaced["draft_dirty"] = False
    replaced["has_unsaved_draft"] = False
    replaced["version"] = int(replaced.get("version") or 1) + 1
    replaced["replacement_applied_at"] = _now_iso()
    jlog("IMPORT", f"date={replaced.get('date')} confirmed=true", "REPLACE_APPLY")
    validate_state_invariants(replaced, context="replace")
    return replaced


def upsert_sync_links_from_export(schedule: Dict[str, Any], exported_events: List[Dict[str, Any]]) -> Dict[str, Any]:
    updated = deepcopy(schedule)
    committed = _list(updated.get("committed_schedule_blocks"))
    links = _list(updated.get("sync_links"))
    blocks_by_id = {str(block.get("block_id")): block for block in committed if block.get("block_id")}

    for exported in exported_events or []:
        event_id = exported.get("calendar_event_id") or exported.get("google_event_id")
        block_id = exported.get("block_id")
        if not event_id or not block_id:
            continue
        if str(block_id) in blocks_by_id:
            blocks_by_id[str(block_id)]["calendar_event_id"] = event_id
        link = {
            "calendar_event_id": event_id,
            "google_event_id": event_id,
            "block_id": block_id,
            "stable_activity_id": exported.get("stable_activity_id"),
            "block_type": exported.get("block_type"),
            "source_system": "jplan",
            "updated_at": _now_iso(),
        }
        links = [
            existing for existing in links
            if str(existing.get("block_id")) != str(block_id)
            and str(existing.get("calendar_event_id")) != str(event_id)
        ]
        links.append({key: value for key, value in link.items() if value})

    updated["committed_schedule_blocks"] = committed
    updated["sync_links"] = links
    validate_state_invariants(updated, context="export")
    return updated
