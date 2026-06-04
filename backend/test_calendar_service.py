import os
import sys

sys.path.append(os.path.dirname(__file__))

import calendar_service
from calendar_service import CalendarService, build_google_export_events


class FakeSupabaseQuery:
    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def execute(self):
        return type("Result", (), {"data": [{"google_refresh_token": "refresh-token"}]})()


class FakeSupabase:
    def table(self, _name):
        return FakeSupabaseQuery()


class FakeSupabaseNoRefreshToken:
    def table(self, _name):
        return self

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def execute(self):
        return type("Result", (), {"data": [{"google_refresh_token": None}]})()


class FakeGoogleResponse:
    status_code = 200
    text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return {"id": "created-event"}


def test_google_export_uses_schedule_blocks_and_best_activity_location():
    plan = {
        "activities": [
            {
                "id": "act-lunch",
                "type": "activity",
                "title": "Lunch",
                "startTime": "12:00 PM",
                "endTime": "01:00 PM",
                "location": "Stale lunch place",
                "resolved_location": {
                    "display_name": "Tamarind Square, Cyberjaya",
                },
            }
        ],
        "schedule_blocks": [
            {
                "id": "act-lunch",
                "block_type": "activity",
                "title": "Lunch",
                "startTime": "12:30 PM",
                "endTime": "01:30 PM",
                "location_label": "Selected Tamarind Square",
            }
        ],
    }

    result = build_google_export_events(plan, "2026-05-26")

    assert result["activity_count"] == 1
    assert result["travel_count"] == 0
    event = result["events"][0]
    assert event["summary"] == "Lunch"
    assert event["startTime"] == "12:30 PM"
    assert event["endTime"] == "01:30 PM"
    assert event["location"] == "Selected Tamarind Square"


def test_google_export_prefers_committed_schedule_blocks_over_draft_blocks():
    plan = {
        "activities": [
            {
                "id": "act-lunch",
                "stable_activity_id": "act-lunch",
                "type": "activity",
                "title": "Lunch",
                "startTime": "12:00 PM",
                "endTime": "01:00 PM",
            }
        ],
        "schedule_blocks": [
            {
                "id": "act-lunch",
                "block_type": "activity",
                "title": "Draft lunch",
                "startTime": "12:30 PM",
                "endTime": "01:30 PM",
            }
        ],
        "committed_schedule_blocks": [
            {
                "id": "act-lunch",
                "stable_activity_id": "act-lunch",
                "block_id": "act-lunch",
                "block_type": "activity",
                "title": "Committed lunch",
                "startTime": "01:00 PM",
                "endTime": "02:00 PM",
            }
        ],
    }

    result = build_google_export_events(plan, "2026-05-26")

    assert result["events"][0]["summary"] == "Committed lunch"
    assert result["events"][0]["startTime"] == "01:00 PM"
    assert result["events"][0]["jplan_metadata"]["source_system"] == "jplan"


def test_google_export_includes_real_travel_blocks_and_skips_display_rows():
    plan = {
        "activities": [],
        "schedule_blocks": [
            {
                "block_type": "transition",
                "type": "travel",
                "title": "31 min travel to Tamarind Square",
                "startTime": "01:35 PM",
                "endTime": "02:06 PM",
                "from_activity": "Focused work",
                "to_activity": "Grocery shopping",
                "to_location": "Tamarind Square",
                "duration_minutes": 31,
            },
            {
                "block_type": "route_conflict",
                "title": "Route warning",
                "startTime": "11:15 AM",
                "endTime": "11:30 AM",
                "display_only": True,
            },
            {
                "block_type": "free",
                "title": "Free Time",
                "startTime": "03:00 PM",
                "endTime": "04:00 PM",
            },
        ],
    }

    result = build_google_export_events(plan, "2026-05-26")

    assert result["activity_count"] == 0
    assert result["travel_count"] == 1
    assert result["skipped_count"] == 2
    event = result["events"][0]
    assert event["summary"] == "31 min travel to Tamarind Square"
    assert event["location"] == "Tamarind Square"
    assert "Route: Focused work -> Grocery shopping" in event["description"]
    assert "Travel time: 31 min" in event["description"]


def test_google_export_falls_back_to_activities_when_schedule_blocks_missing():
    plan = {
        "activities": [
            {
                "type": "activity",
                "title": "Dental checkup",
                "startTime": "01:30 PM",
                "endTime": "03:00 PM",
                "resolved_location": {
                    "display_name": "Sunway Medical Centre, Kuala Lumpur",
                },
            },
            {
                "type": "buffer",
                "title": "Prep / Buffer",
                "startTime": "03:00 PM",
                "endTime": "03:05 PM",
            },
        ],
    }

    result = build_google_export_events(plan, "2026-05-26")

    assert result["activity_count"] == 1
    assert result["travel_count"] == 0
    assert result["skipped_count"] == 1
    assert result["events"][0]["location"] == "Sunway Medical Centre, Kuala Lumpur"


def test_google_export_accepts_legacy_activity_without_type():
    plan = {
        "activities": [
            {
                "title": "Legacy meeting",
                "startTime": "10:00 AM",
                "endTime": "11:00 AM",
                "location_label": "KLCC",
            }
        ],
    }

    result = build_google_export_events(plan, "2026-05-26")

    assert result["activity_count"] == 1
    assert result["events"][0]["summary"] == "Legacy meeting"
    assert result["events"][0]["location"] == "KLCC"


def test_google_export_adds_start_route_from_summary_but_skips_route_warnings():
    plan = {
        "activities": [],
        "start_route_summary": {
            "start_location": "The Arc @ Cyberjaya",
            "first_physical_event": "Morning workshop",
            "first_physical_event_location": "MMU Stadium",
            "first_physical_event_start": "09:00 AM",
            "travel_duration_minutes": 3,
            "leave_by": "08:57 AM",
        },
        "route_conflicts": [
            {
                "reason_code": "fixed_to_fixed_infeasible",
                "from_activity": "Morning workshop",
                "to_activity": "Board meeting",
            }
        ],
        "schedule_blocks": [
            {
                "block_type": "route_conflict",
                "title": "Morning workshop -> Board meeting needs 40 min, only 0 min.",
                "display_only": True,
                "startTime": "11:15 AM",
                "endTime": "11:30 AM",
            }
        ],
    }

    result = build_google_export_events(plan, "2026-05-26")

    assert result["activity_count"] == 0
    assert result["travel_count"] == 1
    assert result["skipped_count"] == 1
    assert result["events"][0]["summary"] == "Travel to MMU Stadium"
    assert result["events"][0]["startTime"] == "08:57 AM"
    assert result["events"][0]["endTime"] == "09:00 AM"


def test_google_export_posts_activity_and_travel_payloads(monkeypatch):
    service = CalendarService(FakeSupabase())
    monkeypatch.setattr(service, "refresh_access_token", lambda _token: "access-token")
    monkeypatch.setattr(service, "get_calendar_events", lambda *_args, **_kwargs: [])

    posted_events = []

    def fake_post(_url, headers=None, json=None, **_kwargs):
        posted_events.append(json)
        return FakeGoogleResponse()

    monkeypatch.setattr(calendar_service.requests, "post", fake_post)

    result = service.export_schedule_to_google(
        "user-1",
        "2026-05-26",
        {
            "activities": [
                {
                    "id": "act-grocery",
                    "type": "activity",
                    "title": "Grocery shopping",
                    "resolved_location": {"display_name": "Tamarind Square"},
                }
            ],
            "schedule_blocks": [
                {
                    "block_type": "transition",
                    "type": "travel",
                    "title": "31 min travel to Tamarind Square",
                    "startTime": "01:35 PM",
                    "endTime": "02:06 PM",
                    "to_location": "Tamarind Square",
                    "duration_minutes": 31,
                },
                {
                    "id": "act-grocery",
                    "block_type": "activity",
                    "title": "Grocery shopping",
                    "startTime": "02:06 PM",
                    "endTime": "02:51 PM",
                },
            ],
        },
    )

    assert result["exported_count"] == 2
    assert result["activity_count"] == 1
    assert result["travel_count"] == 1
    assert [event["summary"] for event in posted_events] == [
        "31 min travel to Tamarind Square",
        "Grocery shopping",
    ]
    assert posted_events[0]["location"] == "Tamarind Square"
    assert posted_events[1]["location"] == "Tamarind Square"
    assert posted_events[0]["extendedProperties"]["private"]["source_system"] == "jplan"


def test_google_export_updates_linked_event_instead_of_posting_duplicate(monkeypatch):
    service = CalendarService(FakeSupabase())
    monkeypatch.setattr(service, "refresh_access_token", lambda _token: "access-token")
    monkeypatch.setattr(service, "get_calendar_events", lambda *_args, **_kwargs: [])

    posted_events = []
    patched_events = []

    def fake_post(_url, headers=None, json=None, **_kwargs):
        posted_events.append(json)
        return FakeGoogleResponse()

    def fake_patch(url, headers=None, json=None, **_kwargs):
        patched_events.append((url, json))
        return FakeGoogleResponse()

    monkeypatch.setattr(calendar_service.requests, "post", fake_post)
    monkeypatch.setattr(calendar_service.requests, "patch", fake_patch)

    result = service.export_schedule_to_google(
        "user-1",
        "2026-05-26",
        {
            "scheduleId": "schedule-1",
            "activities": [],
            "committed_schedule_blocks": [
                {
                    "id": "act-grocery",
                    "stable_activity_id": "act-grocery",
                    "block_id": "block-grocery",
                    "calendar_event_id": "google-existing",
                    "block_type": "activity",
                    "title": "Grocery shopping",
                    "startTime": "02:00 PM",
                    "endTime": "03:00 PM",
                }
            ],
            "sync_links": [
                {"block_id": "block-grocery", "calendar_event_id": "google-existing"}
            ],
        },
    )

    assert result["exported_count"] == 1
    assert result["updated_count"] == 1
    assert posted_events == []
    assert patched_events[0][0].endswith("/google-existing")
    assert patched_events[0][1]["extendedProperties"]["private"]["block_id"] == "block-grocery"


def test_google_export_missing_refresh_token_raises_token_expired():
    service = CalendarService(FakeSupabaseNoRefreshToken())

    try:
        service.export_schedule_to_google("user-1", "2026-05-26", {"activities": []})
    except Exception as exc:
        assert str(exc) == "TOKEN_EXPIRED"
    else:
        raise AssertionError("Expected TOKEN_EXPIRED")
