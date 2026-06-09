import os
import sys
from copy import deepcopy

sys.path.append(os.path.dirname(__file__))

from calendar_import import (
    apply_calendar_sync,
    apply_replace_from_calendar,
    cleanDirtySchedule,
    import_selected_calendar_events,
    replace_from_calendar_preview,
)


def google_event(event_id, title, *, description="", metadata=None):
    payload = {
        "id": event_id,
        "summary": title,
        "description": description,
        "start": {"dateTime": "2026-05-26T09:00:00+08:00"},
        "end": {"dateTime": "2026-05-26T10:00:00+08:00"},
    }
    if metadata:
        payload["extendedProperties"] = {"private": metadata}
    return payload


def test_calendar_import_stores_google_events_and_bootstraps_empty_jplan_day():
    schedule = {
        "date": "2026-05-26",
        "activities": [],
        "schedule_blocks": [],
        "committed_schedule_blocks": [],
        "external_calendar_events": [],
        "sync_links": [],
    }
    events = [
        google_event("external-1", "Dentist"),
        google_event("jplan-1", "Lunch", metadata={"source_system": "jplan", "block_id": "lunch"}),
        google_event("support-1", "Travel to Dentist"),
        google_event("buffer-1", "Prep / Buffer"),
    ]

    synced = apply_calendar_sync(schedule, events, date="2026-05-26")

    assert [item["title"] for item in synced["activities"]] == ["Dentist", "Lunch"]
    assert [item["title"] for item in synced["external_calendar_events"]] == ["Dentist", "Lunch"]
    assert all(item["title"] not in {"Travel to Dentist", "Prep / Buffer"} for item in synced["external_calendar_events"])
    assert all(item.get("source") != "google_calendar" for item in synced["activities"])


def test_calendar_import_bootstraps_empty_jplan_day():
    schedule = {
        "date": "2026-05-27",
        "activities": [],
        "schedule_blocks": [],
        "committed_schedule_blocks": [],
        "external_calendar_events": [],
        "sync_links": [],
    }

    synced = apply_calendar_sync(
        schedule,
        [google_event("external-1", "Dentist")],
        date="2026-05-27",
    )

    assert [item["title"] for item in synced["activities"]] == ["Dentist"]
    assert [item["title"] for item in synced["external_calendar_events"]] == ["Dentist"]


def test_jplan_and_google_day_does_not_auto_merge():
    schedule = {
        "date": "2026-05-26",
        "activities": [{"id": "act-work", "stable_activity_id": "act-work", "title": "Focused work"}],
        "schedule_blocks": [],
        "committed_schedule_blocks": [],
        "external_calendar_events": [],
        "sync_links": [],
    }

    synced = apply_calendar_sync(schedule, [google_event("external-1", "Dentist")], date="2026-05-26")

    assert [item["title"] for item in synced["activities"]] == ["Focused work"]
    assert [item["title"] for item in synced["external_calendar_events"]] == ["Dentist"]


def test_calendar_sync_replaces_external_layer_snapshot_for_date():
    schedule = {
        "date": "2026-05-26",
        "activities": [{"id": "act-work", "stable_activity_id": "act-work", "title": "Focused work"}],
        "schedule_blocks": [],
        "committed_schedule_blocks": [],
        "external_calendar_events": [
            {"id": "gcal-external-1", "google_event_id": "external-1", "title": "Old Dentist"},
            {"id": "gcal-stale-1", "google_event_id": "stale-1", "title": "Deleted Google event"},
        ],
        "sync_links": [],
    }

    synced = apply_calendar_sync(
        schedule,
        [google_event("external-1", "Dentist updated")],
        date="2026-05-26",
    )

    assert [item["title"] for item in synced["activities"]] == ["Focused work"]
    assert [item["title"] for item in synced["external_calendar_events"]] == ["Dentist updated"]


def test_linked_jplan_calendar_events_do_not_materialize_orphan_committed_blocks():
    schedule = {
        "date": "2026-06-09",
        "activities": [],
        "schedule_blocks": [],
        "committed_schedule_blocks": [],
        "external_calendar_events": [],
        "sync_links": [
            {
                "calendar_event_id": "aaa56rom1dremffqoilppstv1g",
                "google_event_id": "aaa56rom1dremffqoilppstv1g",
                "source_system": "jplan",
            }
        ],
    }
    events = [
        google_event("aaa56rom1dremffqoilppstv1g", "Focused work", description="Created via JPlan")
    ]

    synced = apply_calendar_sync(schedule, events, date="2026-06-09")

    assert [item["title"] for item in synced["activities"]] == ["Focused work"]
    assert [item["title"] for item in synced["external_calendar_events"]] == ["Focused work"]
    assert synced["committed_schedule_blocks"] == []
    assert synced["sync_links"][0]["calendar_event_id"] == "aaa56rom1dremffqoilppstv1g"


def test_sync_prunes_orphan_jplan_calendar_committed_duplicates():
    schedule = {
        "date": "2026-06-09",
        "activities": [{"id": "act-current", "stable_activity_id": "act-current", "title": "Focused work"}],
        "schedule_blocks": [{"block_id": "act-current", "stable_activity_id": "act-current", "title": "Focused work"}],
        "committed_schedule_blocks": [
            {"block_id": "act-current", "stable_activity_id": "act-current", "title": "Focused work"},
            {
                "block_id": "act-old-export",
                "stable_activity_id": "act-old-export",
                "title": "Focused work",
                "source_system": "jplan",
                "read_only": True,
                "calendar_event_id": "google-old",
                "description": "Created via JPlan\n[JPLAN_META] {}",
            },
        ],
        "external_calendar_events": [],
        "sync_links": [],
    }

    synced = apply_calendar_sync(
        schedule,
        [google_event("google-old", "Focused work", metadata={"source_system": "jplan", "block_id": "act-old-export"})],
        date="2026-06-09",
    )

    assert [block["block_id"] for block in synced["committed_schedule_blocks"]] == ["act-current"]
    assert synced["activities"][0]["title"] == "Focused work"
    assert [item["title"] for item in synced["external_calendar_events"]] == ["Focused work"]


def test_replace_preview_is_non_mutating_and_confirm_applies():
    schedule = {
        "date": "2026-05-26",
        "activities": [{"id": "act-work", "stable_activity_id": "act-work", "title": "Focused work"}],
        "schedule_blocks": [{"id": "act-work", "block_type": "activity", "title": "Focused work"}],
        "committed_schedule_blocks": [{"block_id": "travel", "block_type": "travel", "title": "Travel to Office"}],
        "external_calendar_events": [
            {
                "id": "gcal-dentist",
                "original_google_event_id": "external-1",
                "title": "Dentist",
                "startTime": "09:00 AM",
                "endTime": "10:00 AM",
            }
        ],
        "location_resolution_requests": [{"activity_id": "act-work", "title": "Focused work"}],
        "pending_repair_suggestions": [{"id": "repair-1"}],
        "route_conflicts": [{"reason_code": "old_conflict"}],
        "unfit_activities": [{"title": "Old unfit"}],
        "blocked_activities": [{"title": "Old blocked"}],
        "needs_travel_validation": True,
        "travel_validation_status": "pending_locations",
        "needs_reschedule": True,
        "draft_dirty": True,
        "has_unsaved_draft": True,
        "sync_links": [],
    }
    original = deepcopy(schedule)

    preview = replace_from_calendar_preview(schedule, ["external-1"])

    assert schedule == original
    assert preview["import_count"] == 1
    assert preview["replace_count"] == 1

    replaced = apply_replace_from_calendar(schedule, ["external-1"])

    assert [item["title"] for item in replaced["activities"]] == ["Dentist"]
    assert replaced["activities"][0]["source"] == "imported_google_calendar"
    assert replaced["schedule_blocks"] == []
    assert replaced["committed_schedule_blocks"] == []
    assert replaced["location_resolution_requests"] == []
    assert replaced["pending_repair_suggestions"] == []
    assert replaced["route_conflicts"] == []
    assert replaced["unfit_activities"] == []
    assert replaced["blocked_activities"] == []
    assert replaced["needs_travel_validation"] is False
    assert replaced["travel_validation_status"] == "not_requested"
    assert replaced["needs_reschedule"] is False
    assert replaced["draft_dirty"] is False
    assert replaced["has_unsaved_draft"] is False


def test_import_selected_appends_and_dedupes_by_original_google_event_id():
    schedule = {
        "date": "2026-05-26",
        "activities": [
            {
                "id": "gcal-external-1",
                "stable_activity_id": "gcal-external-1",
                "title": "Dentist",
                "source": "imported_google_calendar",
                "original_google_event_id": "external-1",
            }
        ],
        "external_calendar_events": [
            {
                "id": "gcal-external-1",
                "original_google_event_id": "external-1",
                "title": "Dentist",
                "startTime": "09:00 AM",
                "endTime": "10:00 AM",
            },
            {
                "id": "gcal-external-2",
                "original_google_event_id": "external-2",
                "title": "Dinner",
                "startTime": "07:00 PM",
                "endTime": "08:00 PM",
            },
        ],
    }

    imported = import_selected_calendar_events(schedule, ["external-1", "external-2"])

    assert [item["title"] for item in imported["activities"]] == ["Dentist", "Dinner"]
    assert all(item.get("source") != "google_calendar" for item in imported["activities"])


def test_dirty_legacy_schedule_moves_google_and_support_out_of_activities():
    schedule = {
        "date": "2026-05-26",
        "activities": [
            {"id": "real", "stable_activity_id": "real", "title": "Focused work", "source": "planner"},
            {"id": "gcal", "title": "Dentist", "source": "google_calendar", "startTime": "09:00 AM", "endTime": "10:00 AM"},
            {"id": "travel", "title": "Travel to Office", "type": "travel", "startTime": "10:00 AM", "endTime": "10:20 AM"},
        ],
        "external_calendar_events": [],
        "committed_schedule_blocks": [],
    }

    cleaned = cleanDirtySchedule(schedule)

    assert [item["title"] for item in cleaned["activities"]] == ["Focused work"]
    assert {item["title"] for item in cleaned["external_calendar_events"]} == {"Dentist", "Travel to Office"}
    assert all(item.get("source") != "google_calendar" for item in cleaned["activities"])
