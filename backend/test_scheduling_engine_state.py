import os
import json
import sys
import time
from copy import deepcopy

import pytest

sys.path.append(os.path.dirname(__file__))

from scheduling_engine import SchedulingEngine, TimingMode, parse_clock
from travel_service import TravelService


@pytest.fixture(autouse=True)
def disable_persistent_geocode_cache(monkeypatch):
    import travel_service

    monkeypatch.setattr(travel_service.database, "get_geocode_cache", lambda **kwargs: None, raising=False)
    monkeypatch.setattr(travel_service.database, "save_geocode_cache", lambda **kwargs: False, raising=False)


class DummyClient:
    pass


class FakeUsage:
    prompt_token_count = 7
    candidates_token_count = 5
    total_token_count = 12


class FakeResponse:
    text = "The requested schedule could not be successfully applied as requested, and the tasks were not appropriately slotted. The change was NOT applied."
    usage_metadata = FakeUsage()


class BadSuccessReplyClient:
    class models:
        @staticmethod
        def generate_content(*args, **kwargs):
            return FakeResponse()


class NaturalConflictReplyClient:
    class models:
        @staticmethod
        def generate_content(*args, **kwargs):
            response = FakeResponse()
            response.text = (
                "I couldn't fit FYP before the Seminar: there is only a 30-minute gap before 11:00 AM, "
                "but FYP needs 3 hours. I kept the current schedule, and you can enable Allow Clash if you want to force it."
            )
            return response


class BadCoffeeTimeReplyClient:
    class models:
        @staticmethod
        def generate_content(*args, **kwargs):
            response = FakeResponse()
            response.text = "I've added your coffee break. It's now on your schedule from 12:00 AM to 1:00 AM."
            return response


class BadShiftDateReplyClient:
    class models:
        @staticmethod
        def generate_content(*args, **kwargs):
            response = FakeResponse()
            response.text = "I've successfully updated your plan to May 6th."
            return response


class SlowReplyClient:
    class models:
        @staticmethod
        def generate_content(*args, **kwargs):
            time.sleep(0.2)
            response = FakeResponse()
            response.text = "This should not be used because it timed out."
            return response


class UnavailableReplyClient:
    class models:
        @staticmethod
        def generate_content(*args, **kwargs):
            raise RuntimeError("503 UNAVAILABLE")


class ParserJsonResponse:
    def __init__(self, text):
        self.text = text
        self.usage_metadata = FakeUsage()


class TransientOnceParserModels:
    def __init__(self):
        self.calls = 0

    def generate_content(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("503 UNAVAILABLE. This model is currently experiencing high demand.")
        return ParserJsonResponse(
            """
            {
              "intent": "edit",
              "reply": "I've added a coffee break.",
              "transcription": "Add a quick 15-minute coffee break right after the meeting.",
              "date": "2026-05-02",
              "operations": [
                {
                  "op": "add",
                  "title": "Coffee Break",
                  "timing_mode": "relative",
                  "anchor_relation": {"kind": "after", "target_title": "Project Meeting"},
                  "duration_minutes": 15
                }
              ],
              "activities": [],
              "preferences": {},
              "conflict_analysis": "No conflict."
            }
            """
        )


class TransientOnceParserClient:
    def __init__(self):
        self.models = TransientOnceParserModels()


class Always503ParserModels:
    def __init__(self):
        self.calls = 0

    def generate_content(self, *args, **kwargs):
        self.calls += 1
        raise RuntimeError("503 UNAVAILABLE. This model is currently experiencing high demand.")


class Always503ParserClient:
    def __init__(self):
        self.models = Always503ParserModels()


class SlowAlwaysParserModels:
    def __init__(self, sleep_seconds=0.08):
        self.calls = 0
        self.sleep_seconds = sleep_seconds

    def generate_content(self, *args, **kwargs):
        self.calls += 1
        time.sleep(self.sleep_seconds)
        return ParserJsonResponse(
            """
            {
              "intent": "add",
              "transcription": "Generate a busy workday.",
              "date": "2026-05-02",
              "operations": [{"op": "add", "title": "Project Meeting", "timing_mode": "fixed", "fixed_start": "09:00", "duration_minutes": 60}],
              "activities": [],
              "preferences": {}
            }
            """
        )


class SlowAlwaysParserClient:
    def __init__(self, sleep_seconds=0.08):
        self.models = SlowAlwaysParserModels(sleep_seconds=sleep_seconds)


class SlowThenSuccessParserModels:
    def __init__(self):
        self.calls = 0

    def generate_content(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            time.sleep(0.06)
        return ParserJsonResponse(
            """
            {
              "intent": "add",
              "transcription": "Generate a busy workday.",
              "date": "2026-05-02",
              "operations": [
                {"op": "add", "title": "Retry Meeting", "timing_mode": "fixed", "fixed_start": "09:00", "duration_minutes": 60},
                {"op": "add", "title": "Lunch", "timing_mode": "fixed", "fixed_start": "12:30", "duration_minutes": 60},
                {"op": "add", "title": "Gym", "timing_mode": "flexible", "duration_minutes": 45},
                {"op": "add", "title": "Grocery", "timing_mode": "flexible", "duration_minutes": 45}
              ],
              "activities": [],
              "preferences": {}
            }
            """
        )


class SlowThenSuccessParserClient:
    def __init__(self):
        self.models = SlowThenSuccessParserModels()


class FallbackModelParserModels:
    def __init__(self):
        self.calls = 0
        self.models_seen = []

    def generate_content(self, *args, **kwargs):
        self.calls += 1
        model = kwargs.get("model")
        self.models_seen.append(model)
        if model == "fallback-json":
            return ParserJsonResponse(
                """
                {
                  "intent": "add",
                  "transcription": "Generate a busy workday.",
                  "date": "2026-05-02",
                  "operations": [
                    {"op": "add", "title": "Fallback Meeting", "timing_mode": "fixed", "fixed_start": "09:00", "duration_minutes": 60},
                    {"op": "add", "title": "Lunch", "timing_mode": "fixed", "fixed_start": "12:30", "duration_minutes": 60},
                    {"op": "add", "title": "Gym", "timing_mode": "flexible", "duration_minutes": 45},
                    {"op": "add", "title": "Grocery", "timing_mode": "flexible", "duration_minutes": 45}
                  ],
                  "activities": [],
                  "preferences": {}
                }
                """
            )
        time.sleep(0.06)
        return ParserJsonResponse("{}")


class FallbackModelParserClient:
    def __init__(self):
        self.models = FallbackModelParserModels()


class EmptyEditParserModels:
    def generate_content(self, *args, **kwargs):
        return ParserJsonResponse(
            """
            {
              "intent": "edit",
              "reply": "I cannot move lunch because it conflicts with Seminar.",
              "transcription": "move the lunch to 12pm",
              "date": "2026-05-02",
              "operations": [],
              "activities": [],
              "preferences": {},
              "conflict_analysis": "Lunch would overlap Seminar."
            }
            """
        )


class EmptyEditParserClient:
    def __init__(self):
        self.models = EmptyEditParserModels()


class FakeTravelService:
    def __init__(self, route_minutes=12, route_error=None, geocode_candidates=None, route_minutes_by_pair=None):
        self.route_minutes_value = route_minutes
        self.route_error = route_error
        self.geocode_candidates_value = geocode_candidates or []
        self.route_minutes_by_pair = route_minutes_by_pair or {}
        self.route_calls = 0
        self.geocode_calls = 0

    def expand_alias(self, label, category=None):
        if str(label).lower() == "mmu":
            return "Multimedia University Cyberjaya, Selangor, Malaysia"
        return label

    def saved_location_matches(self, label, category, saved_locations):
        key = (str(label or "") + " " + str(category or "")).lower()
        matches = []
        for saved in saved_locations or []:
            haystack = " ".join(str(saved.get(field) or "") for field in ("label", "display_name", "address", "category")).lower()
            if any(part and part in haystack for part in key.split()):
                matches.append(saved)
        return matches

    def confirmed_saved_location(self, label, category, saved_locations):
        for saved in self.saved_location_matches(label, category, saved_locations):
            if saved.get("latitude") is not None and saved.get("longitude") is not None:
                return saved
        return None

    def format_saved_match(self, saved):
        return {
            "label": saved.get("label"),
            "display_name": saved.get("display_name") or saved.get("label"),
            "address": saved.get("address"),
            "latitude": saved.get("latitude"),
            "longitude": saved.get("longitude"),
            "source": saved.get("source") or "saved_profile",
            "confirmed_by_user": bool(saved.get("confirmed_by_user", True)),
        }

    def geocode_candidates(self, query, limit=5):
        self.geocode_calls += 1
        return self.geocode_candidates_value[:limit]

    def route_minutes(self, from_coord, to_coord, transport_mode="driving-car", time_bucket=None):
        self.route_calls += 1
        if self.route_error:
            raise self.route_error
        key = (
            round(float(from_coord[0]), 5),
            round(float(from_coord[1]), 5),
            round(float(to_coord[0]), 5),
            round(float(to_coord[1]), 5),
        )
        if key in self.route_minutes_by_pair:
            return self.route_minutes_by_pair[key]
        return self.route_minutes_value


def _default_start_location():
    return {
        "label": "Home",
        "display_name": "Home",
        "address": "Home",
        "latitude": 2.88,
        "longitude": 101.58,
        "source": "saved_location",
    }


def _accurate_preferences(**extra):
    return {
        "accurate_travel_time": True,
        "default_start_location": _default_start_location(),
        **extra,
    }


def _initial_parsed_request():
    return {
        "intent": "schedule",
        "reply": "Draft created.",
        "transcription": "Generate a busy workday for 2 May.",
        "date": "2026-05-02",
        "preferences": {"allow_clash": False},
        "operations": [
            {
                "op": "add",
                "title": "Project Meeting",
                "timing_mode": TimingMode.FIXED,
                "fixed_start": "09:00",
                "duration_minutes": 90,
                "location": "Main Office",
                "priority": "high",
            },
            {
                "op": "add",
                "title": "Seminar",
                "timing_mode": TimingMode.FIXED,
                "fixed_start": "11:00",
                "duration_minutes": 90,
                "location": "Library",
                "priority": "high",
            },
            {
                "op": "add",
                "title": "Lunch",
                "timing_mode": TimingMode.FIXED,
                "fixed_start": "13:00",
                "duration_minutes": 60,
                "location": "near the campus",
                "priority": "high",
            },
            {
                "op": "add",
                "title": "FYP Implementation",
                "duration_minutes": 180,
                "priority": "high",
            },
            {
                "op": "add",
                "title": "Grocery Shopping",
                "duration_minutes": 45,
                "priority": "medium",
            },
            {
                "op": "add",
                "title": "Gym Workout",
                "duration_minutes": 60,
                "location": "gym",
                "priority": "medium",
            },
        ],
    }


def _initial_envelope():
    engine = SchedulingEngine(DummyClient())
    return engine.build_schedule_response(
        parsed=_initial_parsed_request(),
        current_schedule=None,
        latest_request="Generate a busy workday for me, this is for 2 May.",
    )["schedule_data"]


def _empty_envelope(date_value="2026-05-24"):
    return {
        "schema_version": 4,
        "scheduleId": "empty-test",
        "date": date_value,
        "version": 1,
        "status": "ok",
        "schedule_status": "ok",
        "planning_mode": "feasibility_first",
        "allow_clash": False,
        "accurate_travel_time": False,
        "preferences": {"allow_clash": False, "accurate_travel_time": False},
        "activities": [],
        "schedule_blocks": [],
        "explanations": [],
        "conflicts": [],
        "warnings": [],
    }


def _custom_envelope(operations):
    engine = SchedulingEngine(DummyClient())
    return engine.build_schedule_response(
        parsed={
            "intent": "schedule",
            "reply": "Draft created.",
            "transcription": "custom schedule",
            "date": "2026-05-02",
            "preferences": {"allow_clash": False},
            "operations": operations,
        },
        current_schedule=None,
        latest_request="custom schedule",
    )["schedule_data"]


def _activity_by_title(envelope, title):
    for activity in envelope["activities"]:
        if activity["title"] == title:
            return activity
    raise AssertionError(f"Activity not found: {title}")


def _count_title(envelope, title):
    return sum(1 for activity in envelope["activities"] if activity["title"] == title)


def _with_allow_clash(envelope, allow_clash):
    updated = dict(envelope)
    updated["allow_clash"] = allow_clash
    updated["planning_mode"] = "clash_allowed" if allow_clash else "feasibility_first"
    updated["preferences"] = dict(updated.get("preferences") or {})
    updated["preferences"]["allow_clash"] = allow_clash
    updated["preferences"]["planning_mode"] = updated["planning_mode"]
    return updated


def _evening_schedule_envelope(accurate_travel_time=False):
    return {
        "schema_version": 4,
        "scheduleId": "evening-test",
        "date": "2026-05-24",
        "version": 1,
        "status": "ok",
        "schedule_status": "ok",
        "travel_validation_status": "not_requested",
        "planning_mode": "feasibility_first",
        "allow_clash": False,
        "accurate_travel_time": accurate_travel_time,
        "preferences": {
            "allow_clash": False,
            "accurate_travel_time": accurate_travel_time,
        },
        "activities": [
            {
                "id": "act-fyp",
                "stable_activity_id": "act-fyp",
                "type": "activity",
                "title": "FYP Implementation",
                "timing_mode": TimingMode.PREFERRED,
                "scheduled_start": parse_clock("2:05 PM"),
                "scheduled_end": parse_clock("5:05 PM"),
                "startTime": "02:05 PM",
                "endTime": "05:05 PM",
                "duration_minutes": 180,
                "priority": "high",
            },
            {
                "id": "act-gym",
                "stable_activity_id": "act-gym",
                "type": "activity",
                "title": "Gym Workout",
                "timing_mode": TimingMode.PREFERRED,
                "scheduled_start": parse_clock("5:40 PM"),
                "scheduled_end": parse_clock("6:40 PM"),
                "startTime": "05:40 PM",
                "endTime": "06:40 PM",
                "duration_minutes": 60,
                "location": "gym",
                "location_label": "gym",
                "location_category": "fitness_center",
                "location_status": "resolved_default",
                "priority": "medium",
            },
            {
                "id": "act-grocery",
                "stable_activity_id": "act-grocery",
                "type": "activity",
                "title": "Grocery Shopping",
                "timing_mode": TimingMode.PREFERRED,
                "scheduled_start": parse_clock("7:00 PM"),
                "scheduled_end": parse_clock("7:45 PM"),
                "startTime": "07:00 PM",
                "endTime": "07:45 PM",
                "duration_minutes": 45,
                "location": "store",
                "location_label": "store",
                "location_category": "supermarket",
                "location_status": "needs_resolution",
                "priority": "medium",
            },
        ],
        "schedule_blocks": [],
        "explanations": [],
        "conflicts": [],
        "warnings": [],
        "location_resolution_requests": [],
    }


def _dinner_after_grocery_operation():
    return {
        "op": "add",
        "title": "Dinner",
        "timing_mode": TimingMode.RELATIVE,
        "anchor_relation": {"kind": "after", "target_title": "Grocery Shopping"},
        "duration_minutes": 60,
        "location_category": "meal_place",
        "location_status": "needs_resolution",
        "location_source": "unresolved",
    }


def _saved_route_locations():
    return [
        {
            "label": "gym",
            "display_name": "Saved Gym",
            "address": "Saved Gym",
            "latitude": 2.92,
            "longitude": 101.62,
            "source": "saved_profile",
            "confirmed_by_user": True,
        },
        {
            "label": "store",
            "display_name": "Saved Store",
            "address": "Saved Store",
            "category": "supermarket",
            "latitude": 2.93,
            "longitude": 101.63,
            "source": "saved_profile",
            "confirmed_by_user": True,
        },
    ]


def _build_single_activity_with_location(engine, request_text, title, raw_location=None):
    operation = {
        "op": "add",
        "title": title,
        "duration_minutes": 60,
    }
    if raw_location is not None:
        operation["location"] = raw_location
    result = engine.build_schedule_response(
        parsed={
            "intent": "add",
            "reply": "Draft created.",
            "transcription": request_text,
            "date": "2026-05-02",
            "preferences": {"allow_clash": False},
            "operations": [operation],
        },
        current_schedule=None,
        latest_request=request_text,
    )
    return _activity_by_title(result["schedule_data"], title)


def test_multi_turn_plan_state_persists_canonical_activities():
    engine = SchedulingEngine(DummyClient())

    initial_result = engine.build_schedule_response(
        parsed=_initial_parsed_request(),
        current_schedule=None,
        latest_request="Generate a busy workday for me, this is for 2 May.",
    )
    envelope = initial_result["schedule_data"]

    assert envelope["status"] == "ok"
    assert _count_title(envelope, "Lunch") == 1
    assert _count_title(envelope, "FYP Implementation") == 1
    assert _count_title(envelope, "Grocery Shopping") == 1
    assert _count_title(envelope, "Gym Workout") == 1

    meeting = _activity_by_title(envelope, "Project Meeting")
    seminar = _activity_by_title(envelope, "Seminar")
    lunch = _activity_by_title(envelope, "Lunch")
    fyp = _activity_by_title(envelope, "FYP Implementation")

    assert meeting["timing_mode"] == TimingMode.FIXED
    assert seminar["timing_mode"] == TimingMode.FIXED
    assert lunch["timing_mode"] == TimingMode.FIXED
    assert meeting["fixed_start"] == parse_clock("09:00")
    assert seminar["fixed_start"] == parse_clock("11:00")
    assert lunch["fixed_start"] == parse_clock("13:00")
    assert meeting["stable_activity_id"]
    assert seminar["stable_activity_id"]
    assert lunch["stable_activity_id"]
    assert fyp["stable_activity_id"]

    lunch_move = engine.apply_operations(
        envelope=envelope,
        operations=[
            {
                "op": "move",
                "title": "Lunch",
                "fixed_start": "12:00",
                "notes": "Move lunch to 12 PM.",
            }
        ],
        base_version=envelope["version"],
    )

    assert lunch_move["status"] == "conflict"
    assert lunch_move["conflict"]["conflict_target"] == "Lunch"
    assert "Seminar" in lunch_move["conflict"]["conflict_reason"]
    assert _count_title(lunch_move["envelope"], "Lunch") == 1
    assert _activity_by_title(lunch_move["envelope"], "Lunch")["fixed_start"] == parse_clock("13:00")
    assert _activity_by_title(lunch_move["envelope"], "Project Meeting")["fixed_start"] == parse_clock("09:00")
    assert _activity_by_title(lunch_move["envelope"], "Seminar")["fixed_start"] == parse_clock("11:00")

    fyp_before = engine.apply_operations(
        envelope=lunch_move["envelope"],
        operations=[
            {
                "op": "move",
                "title": "FYP Implementation",
                "anchor_relation": {
                    "kind": "before",
                    "target_title": "Seminar",
                },
                "notes": "Move FYP before Seminar.",
            }
        ],
        base_version=lunch_move["envelope"]["version"],
    )

    assert fyp_before["status"] == "conflict"
    assert _count_title(fyp_before["envelope"], "FYP Implementation") == 1
    assert _activity_by_title(fyp_before["envelope"], "Seminar")["fixed_start"] == parse_clock("11:00")

    simple_schedule = engine.build_schedule_response(
        parsed={
            "intent": "schedule",
            "reply": "Simple draft.",
            "transcription": "Plan a simple day.",
            "date": "2026-05-03",
            "preferences": {"allow_clash": False},
            "operations": [
                {"op": "add", "title": "Project Meeting", "timing_mode": TimingMode.FIXED, "fixed_start": "09:00", "duration_minutes": 60, "location": "Main Office"},
                {"op": "add", "title": "Deep Work", "timing_mode": TimingMode.FIXED, "fixed_start": "11:00", "duration_minutes": 60, "location": "Main Office"},
            ],
        },
        current_schedule=None,
        latest_request="simple",
    )["schedule_data"]

    coffee_after = engine.apply_operations(
        envelope=simple_schedule,
        operations=[
            {
                "op": "add",
                "title": "Coffee Break",
                "timing_mode": TimingMode.RELATIVE,
                "anchor_relation": {
                    "kind": "after",
                    "target_title": "meeting",
                },
                "duration_minutes": 15,
                "notes": "Add a quick 15-minute coffee break right after the meeting.",
            }
        ],
        base_version=simple_schedule["version"],
    )

    assert coffee_after["status"] == "success"
    coffee = _activity_by_title(coffee_after["envelope"], "Coffee Break")
    assert coffee["startTime"] == "10:05 AM"
    assert coffee["anchor_relation"]["target_title"] == "Project Meeting"


@pytest.mark.parametrize(
    ("reference", "expected_title"),
    [
        ("Lunch", "Lunch"),
        ("meeting", "Project Meeting"),
        ("FYP implementation", "FYP Implementation"),
    ],
)
def test_reference_resolution_prefers_active_canonical_matches(reference, expected_title):
    engine = SchedulingEngine(DummyClient())
    envelope = engine.build_schedule_response(
        parsed=_initial_parsed_request(),
        current_schedule=None,
        latest_request="initial",
    )["schedule_data"]

    resolution = engine._resolve_activity_reference(reference, envelope["activities"])

    assert resolution["status"] == "resolved"
    assert resolution["activity"]["title"] == expected_title


def test_impossible_lunch_clash_allowed_commits_with_conflict_metadata():
    engine = SchedulingEngine(DummyClient())
    envelope = engine.build_schedule_response(
        parsed=_initial_parsed_request(),
        current_schedule=None,
        latest_request="initial",
    )["schedule_data"]
    clash_envelope = _with_allow_clash(envelope, True)

    result = engine.apply_operations(
        envelope=clash_envelope,
        operations=[
            {"op": "move", "title": "Lunch", "fixed_start": "12:00", "notes": "Move lunch to 12 PM."}
        ],
        base_version=clash_envelope["version"],
    )

    assert result["status"] == "success"
    assert result["envelope"]["allow_clash"] is True
    assert result["envelope"]["planning_mode"] == "clash_allowed"
    assert _activity_by_title(result["envelope"], "Lunch")["fixed_start"] == parse_clock("12:00")
    assert _activity_by_title(result["envelope"], "Seminar")["fixed_start"] == parse_clock("11:00")
    assert result["envelope"]["conflicts"]
    lunch_conflict = next(conflict for conflict in result["envelope"]["conflicts"] if "Lunch" in conflict["activities"])
    assert lunch_conflict["start"] == "12:00 PM"
    assert lunch_conflict["end"] == "12:30 PM"
    assert lunch_conflict["user_forced"] is True


def test_generic_travel_operation_is_not_saved_as_activity():
    engine = SchedulingEngine(DummyClient())
    result = engine.build_schedule_response(
        parsed={
            "intent": "add",
            "reply": "Draft created.",
            "transcription": "Schedule meeting and seminar with travel time.",
            "date": "2026-05-24",
            "preferences": {"allow_clash": False},
            "operations": [
                {
                    "op": "add",
                    "title": "Project Meeting",
                    "timing_mode": TimingMode.FIXED,
                    "fixed_start": "09:00",
                    "duration_minutes": 90,
                    "location": "office",
                },
                {
                    "op": "add",
                    "title": "Travel",
                    "timing_mode": TimingMode.RELATIVE,
                    "anchor_relation": {"kind": "after", "target_title": "Project Meeting"},
                    "duration_minutes": 30,
                    "location": "null",
                },
                {
                    "op": "add",
                    "title": "Seminar",
                    "timing_mode": TimingMode.FIXED,
                    "fixed_start": "11:00",
                    "duration_minutes": 90,
                    "location": "library",
                },
            ],
        },
        current_schedule=None,
        latest_request="Schedule meeting and seminar with travel time.",
    )["schedule_data"]

    assert _count_title(result, "Travel") == 0
    assert any(block.get("block_type") == "transition" for block in result["schedule_blocks"])


def test_existing_generic_travel_activity_is_dropped_on_reload():
    engine = SchedulingEngine(DummyClient())
    envelope = engine.build_schedule_response(
        parsed=_initial_parsed_request(),
        current_schedule=None,
        latest_request="initial",
    )["schedule_data"]
    envelope["activities"].append(
        {
            "id": "bad-travel",
            "type": "activity",
            "title": "Travel",
            "startTime": "10:30 AM",
            "endTime": "11:00 AM",
            "location": "null",
        }
    )

    result = engine.apply_operations(
        envelope=envelope,
        operations=[
            {"op": "add", "title": "Reading", "fixed_start": "20:00", "duration_minutes": 30, "location": "null"}
        ],
        base_version=envelope["version"],
    )

    assert result["status"] == "success"
    assert _count_title(result["envelope"], "Travel") == 0
    assert _activity_by_title(result["envelope"], "Reading")["location"] is None


def test_real_travel_commitment_is_preserved():
    engine = SchedulingEngine(DummyClient())
    result = engine.build_schedule_response(
        parsed={
            "intent": "add",
            "reply": "Draft created.",
            "transcription": "I have a flight to KL at 8 AM.",
            "date": "2026-05-24",
            "preferences": {"allow_clash": False},
            "operations": [
                {
                    "op": "add",
                    "title": "Flight to KL",
                    "timing_mode": TimingMode.FIXED,
                    "fixed_start": "08:00",
                    "duration_minutes": 90,
                    "location": "airport",
                }
            ],
        },
        current_schedule=None,
        latest_request="I have a flight to KL at 8 AM.",
    )["schedule_data"]

    assert _count_title(result, "Flight to KL") == 1


def test_result_reply_is_truthful_for_rejected_conflict():
    engine = SchedulingEngine(DummyClient())
    envelope = engine.build_schedule_response(
        parsed=_initial_parsed_request(),
        current_schedule=None,
        latest_request="initial",
    )["schedule_data"]

    result = engine.apply_operations(
        envelope=envelope,
        operations=[
            {
                "op": "move",
                "title": "Lunch",
                "fixed_start": "12:00",
                "notes": "Move lunch to 12 PM.",
            }
        ],
        base_version=envelope["version"],
    )
    reply = engine.compose_result_reply(
        latest_request="Actually, move my lunch to 12 PM",
        parsed={"operations": [{"op": "move", "title": "Lunch", "fixed_start": "12:00"}]},
        result=result,
        allow_clash=False,
    )

    assert result["status"] == "conflict"
    assert reply["reply_status"] == "conflict"
    assert reply["recommend_allow_clash"] is True
    assert "couldn't apply" in reply["reply"]
    assert "Allow Clash" in reply["reply"]


def test_result_reply_reports_forced_clash_when_allow_clash_is_enabled():
    engine = SchedulingEngine(DummyClient())
    envelope = engine.build_schedule_response(
        parsed=_initial_parsed_request(),
        current_schedule=None,
        latest_request="initial",
    )["schedule_data"]
    clash_envelope = _with_allow_clash(envelope, True)

    result = engine.apply_operations(
        envelope=clash_envelope,
        operations=[
            {"op": "move", "title": "Lunch", "fixed_start": "12:00", "notes": "Move lunch to 12 PM."}
        ],
        base_version=clash_envelope["version"],
    )
    reply = engine.compose_result_reply(
        latest_request="Actually, move my lunch to 12 PM",
        parsed={"operations": [{"op": "move", "title": "Lunch", "fixed_start": "12:00"}]},
        result=result,
        allow_clash=True,
    )

    assert result["status"] == "success"
    assert result["envelope"]["conflicts"]
    assert reply["reply_status"] == "partial"
    assert reply["recommend_allow_clash"] is False
    assert "creates a clash" in reply["reply"]


def test_empty_edit_operations_uses_fallback_fixed_time_update():
    engine = SchedulingEngine(EmptyEditParserClient())
    current_schedule = {
        "date": "2026-05-02",
        "activities": [
            {"title": "Seminar", "startTime": "11:00 AM", "endTime": "12:30 PM"},
            {"title": "Lunch", "startTime": "01:00 PM", "endTime": "02:00 PM"},
        ],
    }

    parsed = engine.parse_text_request(
        "move the lunch to 12pm",
        current_schedule=current_schedule,
    )

    assert parsed["_reply_source"] == "deterministic_fallback"
    assert parsed["operations"]
    assert parsed["operations"][0]["title"] == "Lunch"
    assert parse_clock(parsed["operations"][0]["fixed_start"]) == parse_clock("12:00 PM")


def test_pronoun_fixed_time_update_resolves_last_target_from_history():
    engine = SchedulingEngine(Always503ParserClient())
    current_schedule = {
        "date": "2026-05-02",
        "activities": [
            {"title": "Seminar", "startTime": "11:00 AM", "endTime": "12:30 PM"},
            {"title": "Lunch", "startTime": "01:00 PM", "endTime": "02:00 PM"},
        ],
    }

    parsed = engine.parse_text_request(
        "move it to 12pm",
        history=[
            {"role": "user", "message": "move lunch to 12pm"},
            {"role": "assistant", "message": "I couldn't move Lunch because it overlaps Seminar."},
        ],
        current_schedule=current_schedule,
    )

    assert parsed["operations"][0]["title"] == "Lunch"
    assert parse_clock(parsed["operations"][0]["fixed_start"]) == parse_clock("12:00 PM")


def test_unrequested_fixed_event_mutation_is_ignored_not_applied():
    engine = SchedulingEngine(DummyClient())
    envelope = engine.build_schedule_response(
        parsed=_initial_parsed_request(),
        current_schedule=None,
        latest_request="initial",
    )["schedule_data"]

    result = engine.apply_operations(
        envelope=envelope,
        operations=[
            {
                "op": "update",
                "title": "Seminar",
                "fixed_start": "12:30",
                "_user_message": "move lunch to 12pm",
            },
            {
                "op": "update",
                "title": "Lunch",
                "timing_mode": TimingMode.FIXED,
                "fixed_start": "12:00",
                "_user_message": "move lunch to 12pm",
            },
        ],
        base_version=envelope["version"],
    )

    assert result["status"] == "conflict"
    assert result["ignored_operations"]
    assert result["ignored_operations"][0]["target"] == "Seminar"
    assert _activity_by_title(result["envelope"], "Seminar")["fixed_start"] == parse_clock("11:00 AM")


def test_resolved_requested_target_lunch_with_girl_is_not_ignored():
    engine = SchedulingEngine(DummyClient())
    envelope = _custom_envelope([
        {"op": "add", "title": "Seminar", "timing_mode": TimingMode.FIXED, "fixed_start": "11:00", "duration_minutes": 90},
        {"op": "add", "title": "Lunch with Girl", "timing_mode": TimingMode.FIXED, "fixed_start": "13:00", "duration_minutes": 60},
    ])

    result = engine.apply_operations(
        envelope=envelope,
        operations=[{"op": "update", "title": "Lunch", "fixed_start": "12:00", "_user_message": "can you move lunch to 12pm"}],
        base_version=envelope["version"],
    )

    assert not result.get("ignored_operations")
    assert result["status"] == "conflict"
    assert result["conflict"]["conflict_target"] == "Lunch with Girl"


def test_allow_clash_moves_resolved_lunch_with_girl_and_marks_clash():
    engine = SchedulingEngine(DummyClient())
    envelope = _with_allow_clash(_custom_envelope([
        {"op": "add", "title": "Seminar", "timing_mode": TimingMode.FIXED, "fixed_start": "11:00", "duration_minutes": 90},
        {"op": "add", "title": "Lunch with Girl", "timing_mode": TimingMode.FIXED, "fixed_start": "13:00", "duration_minutes": 60},
    ]), True)

    result = engine.apply_operations(
        envelope=envelope,
        operations=[{"op": "update", "title": "Lunch", "fixed_start": "12:00", "_user_message": "move lunch to 12pm"}],
        base_version=envelope["version"],
    )

    lunch = _activity_by_title(result["envelope"], "Lunch with Girl")
    assert not result.get("ignored_operations")
    assert lunch["fixed_start"] == parse_clock("12:00 PM")
    assert result["envelope"]["conflicts"]


def test_pronoun_resolved_target_lunch_with_girl_is_allowed():
    engine = SchedulingEngine(DummyClient())
    envelope = _with_allow_clash(_custom_envelope([
        {"op": "add", "title": "Seminar", "timing_mode": TimingMode.FIXED, "fixed_start": "11:00", "duration_minutes": 90},
        {"op": "add", "title": "Lunch with Girl", "timing_mode": TimingMode.FIXED, "fixed_start": "13:00", "duration_minutes": 60},
    ]), True)
    parsed = engine.parse_deterministic_fast_path(
        "move it to 12pm",
        current_schedule=envelope,
        history=[
            {"role": "user", "message": "move lunch to 12pm"},
            {"role": "assistant", "message": "I couldn't move Lunch with Girl because it overlaps Seminar."},
        ],
        saved_locations=[],
    )

    result = engine.apply_operations(
        envelope=envelope,
        operations=[{**parsed["operations"][0], "_user_message": "move it to 12pm"}],
        base_version=envelope["version"],
    )

    assert parsed["operations"][0]["title"] == "Lunch with Girl"
    assert not result.get("ignored_operations")
    assert _activity_by_title(result["envelope"], "Lunch with Girl")["fixed_start"] == parse_clock("12:00 PM")


def test_pronoun_after_whole_plan_shift_asks_clarification():
    engine = SchedulingEngine(DummyClient())
    envelope = _initial_envelope()

    parsed = engine.parse_deterministic_fast_path(
        "move it to 12pm",
        current_schedule=envelope,
        history=[
            {"role": "user", "message": "move the whole plan to 27 May"},
            {"role": "assistant", "message": "I've moved the whole plan to May 27."},
        ],
        saved_locations=[],
    )

    assert parsed["intent"] == "no_operation"
    assert parsed["operations"] == []
    assert "Which activity" in parsed["reply"]


def test_pronoun_without_recent_specific_target_asks_clarification():
    engine = SchedulingEngine(DummyClient())
    envelope = _initial_envelope()

    parsed = engine.parse_deterministic_fast_path(
        "move it to 12pm",
        current_schedule=envelope,
        history=[],
        saved_locations=[],
    )

    assert parsed["intent"] == "no_operation"
    assert parsed["operations"] == []
    assert "Which activity" in parsed["reply"]


def test_no_operation_result_reply_is_not_success():
    engine = SchedulingEngine(DummyClient())
    result = {
        "status": "no_operation",
        "applied": False,
        "envelope": {"date": "2026-05-02", "schedule_blocks": [], "activities": []},
        "updatedActivities": [],
        "ignored_operations": [],
    }

    reply = engine.compose_result_reply(
        latest_request="move lunch to 12pm",
        parsed={"intent": "no_operation", "operations": []},
        result=result,
        allow_clash=False,
    )

    assert reply["reply_status"] == "clarification_needed"
    assert "could not" in reply["reply"].lower()


def test_remove_operation_reply_names_removed_activity():
    engine = SchedulingEngine(DummyClient())
    envelope = _initial_envelope()

    result = engine.apply_operations(
        envelope=envelope,
        operations=[{"op": "remove", "title": "Gym", "_user_message": "remove gym"}],
        base_version=envelope["version"],
    )
    reply = engine.compose_result_reply(
        latest_request="remove gym",
        parsed={"intent": "edit", "operations": [{"op": "remove", "title": "Gym"}]},
        result=result,
        allow_clash=False,
    )

    assert result["applied"] is True
    assert "Gym Workout" in result["deletedItemIds"][0] or result["deletedItemIds"]
    assert reply["reply_status"] == "success"
    assert "removed Gym Workout" in reply["reply"]
    assert "generated your schedule" not in reply["reply"]


def test_priority_update_reply_mentions_priority_not_time_range():
    engine = SchedulingEngine(DummyClient())
    envelope = _custom_envelope([
        {
            "op": "add",
            "title": "FYP Implementation",
            "duration_minutes": 180,
            "priority": "medium",
        },
    ])

    parsed = engine.parse_deterministic_fast_path(
        "set FYP implementation priority high",
        current_schedule=envelope,
        history=[],
        saved_locations=[],
    )
    result = engine.apply_operations(
        envelope=envelope,
        operations=[{**parsed["operations"][0], "_user_message": "set FYP implementation priority high"}],
        base_version=envelope["version"],
    )
    reply = engine.compose_result_reply(
        latest_request="set FYP implementation priority high",
        parsed=parsed,
        result=result,
        allow_clash=False,
    )
    updated = _activity_by_title(result["envelope"], "FYP Implementation")

    assert result["applied"] is True
    assert updated["priority"] == "high"
    assert _count_title(result["envelope"], "FYP Implementation") == 1
    assert _count_title(result["envelope"], "Fyp Implementation") == 0
    assert reply["reply_status"] == "success"
    assert "priority to high" in reply["reply"]
    assert "02:05 PM" not in reply["reply"]
    assert "05:05 PM" not in reply["reply"]


def test_priority_same_value_noop_preserves_version_and_replies_already_set():
    engine = SchedulingEngine(DummyClient())
    envelope = _initial_envelope()

    parsed = engine.parse_deterministic_fast_path(
        "set FYP implementation priority high",
        current_schedule=envelope,
        history=[],
        saved_locations=[],
    )
    result = engine.apply_operations(
        envelope=envelope,
        operations=[{**parsed["operations"][0], "_user_message": "set FYP implementation priority high"}],
        base_version=envelope["version"],
    )
    reply = engine.compose_result_reply(
        latest_request="set FYP implementation priority high",
        parsed=parsed,
        result=result,
        allow_clash=False,
    )

    assert result["applied"] is False
    assert result["version"] == envelope["version"]
    assert result["reply_reason"] == "priority_already_set"
    assert _count_title(result["envelope"], "FYP Implementation") == 1
    assert _count_title(result["envelope"], "Fyp Implementation") == 0
    assert reply["reply_status"] == "success"
    assert "FYP Implementation is already set to high priority" in reply["reply"]
    assert "02:05 PM" not in reply["reply"]
    assert "05:05 PM" not in reply["reply"]


def test_lower_lunch_priority_preserves_canonical_title_and_reply():
    engine = SchedulingEngine(DummyClient())
    envelope = _custom_envelope([
        {"op": "add", "title": "Seminar", "timing_mode": TimingMode.FIXED, "fixed_start": "11:00", "duration_minutes": 90},
        {"op": "add", "title": "Lunch with Girl", "timing_mode": TimingMode.FIXED, "fixed_start": "13:00", "duration_minutes": 60, "priority": "medium"},
    ])

    parsed = engine.parse_deterministic_fast_path(
        "lower lunch priority",
        current_schedule=envelope,
        history=[],
        saved_locations=[],
    )
    result = engine.apply_operations(
        envelope=envelope,
        operations=[{**parsed["operations"][0], "_user_message": "lower lunch priority"}],
        base_version=envelope["version"],
    )
    reply = engine.compose_result_reply(
        latest_request="lower lunch priority",
        parsed=parsed,
        result=result,
        allow_clash=False,
    )
    updated = _activity_by_title(result["envelope"], "Lunch with Girl")

    assert result["applied"] is True
    assert updated["priority"] == "low"
    assert _count_title(result["envelope"], "Lunch with Girl") == 1
    assert _count_title(result["envelope"], "Lunch") == 0
    assert reply["reply_status"] == "success"
    assert "lowered Lunch with Girl priority" in reply["reply"]
    assert "01:00 PM" not in reply["reply"]
    assert "02:00 PM" not in reply["reply"]


def test_lower_lunch_priority_same_value_noop_uses_canonical_title():
    engine = SchedulingEngine(DummyClient())
    envelope = _custom_envelope([
        {"op": "add", "title": "Lunch with Girl", "timing_mode": TimingMode.FIXED, "fixed_start": "13:00", "duration_minutes": 60, "priority": "low"},
    ])

    parsed = engine.parse_deterministic_fast_path(
        "lower lunch priority",
        current_schedule=envelope,
        history=[],
        saved_locations=[],
    )
    result = engine.apply_operations(
        envelope=envelope,
        operations=[{**parsed["operations"][0], "_user_message": "lower lunch priority"}],
        base_version=envelope["version"],
    )
    reply = engine.compose_result_reply(
        latest_request="lower lunch priority",
        parsed=parsed,
        result=result,
        allow_clash=False,
    )

    assert result["applied"] is False
    assert result["version"] == envelope["version"]
    assert _count_title(result["envelope"], "Lunch with Girl") == 1
    assert _count_title(result["envelope"], "Lunch") == 0
    assert reply["reply_status"] == "success"
    assert "Lunch with Girl is already set to low priority" in reply["reply"]
    assert "01:00 PM" not in reply["reply"]
    assert "02:00 PM" not in reply["reply"]


def test_result_reply_rejects_false_failure_claim_for_successful_schedule():
    engine = SchedulingEngine(BadSuccessReplyClient())
    result = engine.build_schedule_response(
        parsed=_initial_parsed_request(),
        current_schedule=None,
        latest_request="Generate my busy workday.",
    )
    reply = engine.compose_result_reply(
        latest_request="Generate my busy workday.",
        parsed={"operations": _initial_parsed_request()["operations"]},
        result=result,
        allow_clash=False,
    )

    assert result["schedule_data"]["status"] == "ok"
    assert reply["reply_status"] == "success"
    assert "not applied" not in reply["reply"].lower()
    assert reply["token_usage"]["total"] == 12


def test_module_8_timeout_returns_fallback_and_preserves_reply_reason(monkeypatch):
    import scheduling_engine.module_8_reply as module_8_reply

    monkeypatch.setattr(module_8_reply, "MODULE8_LLM_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(module_8_reply, "MODULE8_LLM_TOTAL_TIMEOUT_SECONDS", 0.01)
    engine = SchedulingEngine(SlowReplyClient())
    envelope = _initial_envelope()
    result = engine.apply_operations(
        envelope=envelope,
        operations=[{"op": "move", "title": "Lunch", "fixed_start": "12:00", "_user_message": "move lunch to 12pm"}],
        base_version=envelope["version"],
    )
    reply = engine.compose_result_reply(
        latest_request="move lunch to 12pm",
        parsed={"operations": [{"op": "move", "title": "Lunch", "fixed_start": "12:00"}]},
        result=result,
        allow_clash=False,
    )

    assert reply["reply_source"] == "fallback-template"
    assert reply["llm_fallback_reason"] == "module_8_timeout"
    assert reply["reply_reason"] == result["conflict"]["conflict_reason"]


def test_module_8_unavailable_returns_fallback_without_failing_schedule():
    engine = SchedulingEngine(UnavailableReplyClient())
    result = engine.build_schedule_response(
        parsed=_initial_parsed_request(),
        current_schedule=None,
        latest_request="Generate my busy workday.",
    )
    reply = engine.compose_result_reply(
        latest_request="Generate my busy workday.",
        parsed={"operations": _initial_parsed_request()["operations"]},
        result=result,
        allow_clash=False,
    )

    assert result["schedule_data"]["status"] == "ok"
    assert reply["reply_source"] == "fallback-template"
    assert reply["llm_fallback_reason"] == "module_8_unavailable"
    assert reply["reply_status"] == "success"


def test_warning_reply_uses_final_coffee_break_block_times():
    engine = SchedulingEngine(BadCoffeeTimeReplyClient())
    envelope = engine.build_schedule_response(
        parsed=_initial_parsed_request(),
        current_schedule=None,
        latest_request="Generate my busy workday.",
    )["schedule_data"]

    result = engine.apply_operations(
        envelope=envelope,
        operations=[
            {
                "op": "add",
                "title": "Coffee Break",
                "timing_mode": TimingMode.RELATIVE,
                "anchor_relation": {"kind": "after", "target_title": "Project Meeting"},
                "duration_minutes": 15,
                "notes": "Add a quick 15-minute coffee break right after the meeting.",
            }
        ],
        base_version=envelope["version"],
    )
    reply = engine.compose_result_reply(
        latest_request="Add a quick 15-minute coffee break right after the meeting.",
        parsed={
            "operations": [
                {
                    "op": "add",
                    "title": "Coffee Break",
                    "timing_mode": TimingMode.RELATIVE,
                    "anchor_relation": {"kind": "after", "target_title": "Project Meeting"},
                    "duration_minutes": 15,
                }
            ]
        },
        result=result,
        allow_clash=False,
    )

    coffee_block = next(
        block for block in result["envelope"]["schedule_blocks"]
        if block.get("block_type") == "activity" and block.get("title") == "Coffee Break"
    )
    tight_travel = result["envelope"]["schedule_blocks"][
        result["envelope"]["schedule_blocks"].index(coffee_block) + 1
    ]

    assert result["status"] == "warning"
    assert result["envelope"]["status"] == "warning"
    assert coffee_block["start"] == "10:30 AM"
    assert coffee_block["end"] == "10:45 AM"
    assert tight_travel["block_type"] == "transition"
    assert tight_travel["is_tight"] is True
    assert reply["reply_status"] == "warning"
    assert "10:30 AM" in reply["reply"]
    assert "10:45 AM" in reply["reply"]
    assert "12:00 AM" not in reply["reply"]
    assert "1:00 AM" not in reply["reply"]
    assert reply["reply_source"] == "fallback-template"


def test_coffee_break_tight_transition_is_warning_not_rejected(capsys):
    engine = SchedulingEngine(DummyClient())
    envelope = engine.build_schedule_response(
        parsed=_initial_parsed_request(),
        current_schedule=None,
        latest_request="Generate my busy workday.",
    )["schedule_data"]
    capsys.readouterr()

    result = engine.apply_operations(
        envelope=envelope,
        operations=[
            {
                "op": "add",
                "title": "Coffee Break",
                "timing_mode": TimingMode.RELATIVE,
                "anchor_relation": {"kind": "after", "target_title": "Project Meeting"},
                "duration_minutes": 15,
            }
        ],
        base_version=envelope["version"],
    )
    captured = capsys.readouterr().out

    assert "[ACCEPTED_TIGHT_TRANSITION] 'Coffee Break'" in captured
    assert "[REJECT_REL] 'Coffee Break'" not in captured
    assert result["status"] == "warning"
    assert result["envelope"]["accepted_with_warnings"]
    assert result["envelope"]["accepted_with_warnings"][0]["warning_code"] == "TIGHT_TRANSITION"
    assert result["envelope"]["warnings"][0]["warning_code"] == "TIGHT_TRANSITION"
    assert result["envelope"]["rejected_changes"] == []
    assert result["envelope"]["applied_changes"][0]["title"] == "Coffee Break"


def test_transient_503_parse_retries_and_applies_simple_request(capsys):
    client = TransientOnceParserClient()
    engine = SchedulingEngine(client)
    envelope = engine.build_schedule_response(
        parsed=_initial_parsed_request(),
        current_schedule=None,
        latest_request="Generate my busy workday.",
    )["schedule_data"]
    capsys.readouterr()

    parsed = engine.parse_text_request(
        "Add a quick 15-minute coffee break right after the meeting.",
        current_schedule=envelope,
    )
    captured = capsys.readouterr().out
    result = engine.apply_operations(
        envelope=envelope,
        operations=parsed["operations"],
        base_version=envelope["version"],
    )

    assert client.models.calls == 2
    assert "[LLM_RETRY] attempt 1/2 after 503" in captured
    assert "[LLM_RETRY] success on retry" in captured
    assert parsed["_reply_source"] == "llm"
    assert _count_title(result["envelope"], "Coffee Break") == 1


def test_503_parse_fallback_applies_simple_coffee_request(capsys):
    client = Always503ParserClient()
    engine = SchedulingEngine(client)
    envelope = engine.build_schedule_response(
        parsed=_initial_parsed_request(),
        current_schedule=None,
        latest_request="Generate my busy workday.",
    )["schedule_data"]
    capsys.readouterr()

    parsed = engine.parse_text_request(
        "Add a quick 15-minute coffee break right after the meeting.",
        current_schedule=envelope,
    )
    captured = capsys.readouterr().out
    result = engine.apply_operations(
        envelope=envelope,
        operations=parsed["operations"],
        base_version=envelope["version"],
    )

    assert client.models.calls == 2
    assert "[LLM_FALLBACK_PARSE] Used deterministic fallback parser for simple request" in captured
    assert parsed["_reply_source"] == "deterministic_fallback"
    assert parsed["operations"][0]["title"] == "Coffee Break"
    assert _count_title(result["envelope"], "Coffee Break") == 1


def _configure_fast_module_a_timeout(monkeypatch, retry=True, fallback_model=""):
    import scheduling_engine.module_a_parser as module_a_parser

    monkeypatch.setattr(module_a_parser, "MODULE_A_LLM_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(module_a_parser, "MODULE_A_LLM_TOTAL_TIMEOUT_SECONDS", 0.16)
    monkeypatch.setattr(module_a_parser, "MODULE_A_LLM_ENABLE_RETRY", retry)
    monkeypatch.setattr(module_a_parser, "MODULE_A_LLM_RETRY_COUNT", 1 if retry else 0)
    monkeypatch.setattr(module_a_parser, "MODULE_A_LLM_FALLBACK_MODEL", fallback_model)
    monkeypatch.setattr(module_a_parser, "MODULE_A_LLM_FALLBACK_TIMEOUT_SECONDS", 0.04)
    return module_a_parser


def test_module_a_timeout_returns_parser_busy_without_schedule_mutation(monkeypatch):
    _configure_fast_module_a_timeout(monkeypatch, retry=False)
    engine = SchedulingEngine(SlowAlwaysParserClient())
    envelope = _initial_envelope()
    started = time.perf_counter()

    parsed = engine.parse_text_request(
        "Generate a busy workday with meeting, lunch, gym and grocery.",
        current_schedule=envelope,
    )
    elapsed = time.perf_counter() - started
    response = engine.build_schedule_response(
        parsed=parsed,
        current_schedule=envelope,
        latest_request="Generate a busy workday with meeting, lunch, gym and grocery.",
    )
    time.sleep(0.1)

    assert elapsed < 0.8
    assert parsed["intent"] == "chat"
    assert parsed["_failure_type"] == "module_a_timeout"
    assert parsed["operations"] == []
    assert parsed["activities"] == []
    assert "AI parser is busy" in parsed["reply"]
    assert response["schedule_data"] is None
    assert envelope["version"] == _initial_envelope()["version"]


def test_module_a_executor_saturation_returns_parser_busy(monkeypatch):
    module_a_parser = _configure_fast_module_a_timeout(monkeypatch, retry=False)
    acquired = [
        module_a_parser.MODULE_A_LLM_SEMAPHORE.acquire(blocking=False)
        for _ in range(module_a_parser.MODULE_A_LLM_EXECUTOR_WORKERS)
    ]
    try:
        engine = SchedulingEngine(SlowAlwaysParserClient())
        parsed = engine.parse_text_request(
            "Generate a busy workday with meeting, lunch, gym and grocery.",
            current_schedule=None,
        )
    finally:
        for did_acquire in acquired:
            if did_acquire:
                module_a_parser.MODULE_A_LLM_SEMAPHORE.release()

    assert all(acquired)
    assert parsed["intent"] == "chat"
    assert parsed["_failure_type"] == "module_a_executor_saturated"
    assert parsed["operations"] == []
    assert "AI parser is busy" in parsed["reply"]


def test_module_a_timeout_retry_success_uses_remaining_budget(monkeypatch):
    _configure_fast_module_a_timeout(monkeypatch, retry=True)
    client = SlowThenSuccessParserClient()
    engine = SchedulingEngine(client)

    parsed = engine.parse_text_request(
        "Generate a busy workday with meeting, lunch, gym and grocery.",
        current_schedule=None,
    )
    time.sleep(0.08)

    assert client.models.calls >= 2
    assert parsed["_reply_source"] == "llm"
    assert parsed["operations"][0]["title"] == "Retry Meeting"


def test_module_a_timeout_fallback_model_success(monkeypatch):
    _configure_fast_module_a_timeout(monkeypatch, retry=False, fallback_model="fallback-json")
    client = FallbackModelParserClient()
    engine = SchedulingEngine(client)

    parsed = engine.parse_text_request(
        "Generate a busy workday with meeting, lunch, gym and grocery.",
        current_schedule=None,
    )
    time.sleep(0.08)

    assert "fallback-json" in client.models.models_seen
    assert parsed["_reply_source"] == "llm"
    assert parsed["operations"][0]["title"] == "Fallback Meeting"


def test_module_a_all_fail_returns_parser_busy(monkeypatch):
    _configure_fast_module_a_timeout(monkeypatch, retry=True, fallback_model="fallback-json")
    client = Always503ParserClient()
    engine = SchedulingEngine(client)

    parsed = engine.parse_text_request(
        "Generate a busy workday with meeting, lunch, gym and grocery.",
        current_schedule=None,
    )

    assert client.models.calls == 3
    assert parsed["intent"] == "chat"
    assert parsed["_failure_type"] == "module_a_unavailable"
    assert parsed["operations"] == []
    assert "AI parser is busy" in parsed["reply"]


def test_initial_generation_reply_lists_all_scheduled_requested_activities():
    engine = SchedulingEngine(DummyClient())
    result = engine.build_schedule_response(
        parsed=_initial_parsed_request(),
        current_schedule=None,
        latest_request="Generate my busy workday.",
    )
    reply = engine.compose_result_reply(
        latest_request="Generate my busy workday.",
        parsed={"operations": _initial_parsed_request()["operations"]},
        result=result,
        allow_clash=False,
    )

    assert reply["reply_status"] == "success"
    for title in [
        "Project Meeting",
        "Seminar",
        "Lunch",
        "FYP Implementation",
        "Grocery Shopping",
        "Gym Workout",
    ]:
        assert title in reply["reply"]


def test_normal_create_does_not_log_bulk_date_shift(capsys):
    engine = SchedulingEngine(DummyClient())
    engine.build_schedule_response(
        parsed={**_initial_parsed_request(), "date": "2026-05-24"},
        current_schedule=None,
        latest_request="Generate a busy workday for 24th of May.",
    )
    captured = capsys.readouterr().out

    assert "Applying bulk date shift" not in captured


def test_whole_plan_shift_preserves_26th_may_from_user_text():
    engine = SchedulingEngine(DummyClient())
    parsed = engine._normalize_plan_level_operations(
        {
            "intent": "edit",
            "date": "2026-05-06",
            "operations": [{"op": "move", "title": "Whole Plan"}],
            "activities": [],
            "preferences": {},
        },
        "Move this whole plan to 26th May.",
        {"date": "2026-05-24", "activities": [], "version": 1},
    )

    assert parsed["date"] == "2026-05-26"
    assert parsed["operations"][0]["op"] == "shift_plan_date"
    assert parsed["operations"][0]["to_date"] == "2026-05-26"
    assert parsed["operations"][0]["to_date"] != "2026-05-06"


def test_whole_plan_shift_correction_overrides_previous_wrong_day():
    engine = SchedulingEngine(DummyClient())
    parsed = engine._normalize_plan_level_operations(
        {
            "intent": "edit",
            "date": "2026-05-06",
            "operations": [],
            "activities": [],
            "preferences": {},
        },
        "not 6th its 26th may",
        {"date": "2026-05-06", "activities": [], "version": 1},
    )

    assert parsed["date"] == "2026-05-26"
    assert parsed["operations"][0]["op"] == "shift_plan_date"
    assert parsed["operations"][0]["from_date"] == "2026-05-06"
    assert parsed["operations"][0]["to_date"] == "2026-05-26"


def test_explicit_shift_plan_date_still_logs_bulk_date_shift(capsys):
    engine = SchedulingEngine(DummyClient())
    source = engine.build_schedule_response(
        parsed={**_initial_parsed_request(), "date": "2026-05-05"},
        current_schedule=None,
        latest_request="source",
    )["schedule_data"]
    capsys.readouterr()

    engine.apply_operations(
        envelope=source,
        operations=[
            {
                "op": "shift_plan_date",
                "from_date": "2026-05-05",
                "to_date": "2026-05-06",
                "scope": "all_active_activities",
            }
        ],
        base_version=source["version"],
    )
    captured = capsys.readouterr().out

    assert "Applying bulk date shift from 2026-05-05 to 2026-05-06" in captured


def test_shift_reply_rejects_wrong_llm_date():
    engine = SchedulingEngine(BadShiftDateReplyClient())
    source = engine.build_schedule_response(
        parsed={**_initial_parsed_request(), "date": "2026-05-24"},
        current_schedule=None,
        latest_request="source",
    )["schedule_data"]
    operation = {
        "op": "shift_plan_date",
        "from_date": "2026-05-24",
        "to_date": "2026-05-26",
        "scope": "all_active_activities",
    }

    shifted = engine.apply_operations(
        envelope=source,
        operations=[operation],
        base_version=source["version"],
    )
    reply = engine.compose_result_reply(
        latest_request="Move this whole plan to 26th May.",
        parsed={"operations": [operation]},
        result=shifted,
        allow_clash=False,
    )

    assert shifted["status"] == "success"
    assert "May 26" in reply["reply"]
    assert "May 6" not in reply["reply"]
    assert reply["reply_source"] == "fallback-template"


def test_failed_anchor_postcondition_rejects_false_success_reply():
    engine = SchedulingEngine(DummyClient())
    envelope = engine.build_schedule_response(
        parsed=_initial_parsed_request(),
        current_schedule=None,
        latest_request="initial",
    )["schedule_data"]

    result = engine.apply_operations(
        envelope=envelope,
        operations=[
            {
                "op": "move",
                "title": "FYP Implementation",
                "timing_mode": TimingMode.RELATIVE,
                "anchor_relation": {"kind": "after", "target_title": "Project Meeting"},
                "_user_message": "I want to do my FYP implementation before the Seminar.",
            },
            {
                "op": "move",
                "title": "Seminar",
                "timing_mode": TimingMode.RELATIVE,
                "anchor_relation": {"kind": "after", "target_title": "FYP Implementation"},
                "_user_message": "I want to do my FYP implementation before the Seminar.",
            },
        ],
        base_version=envelope["version"],
    )
    reply = engine.compose_result_reply(
        latest_request="I want to do my FYP implementation before the Seminar.",
        parsed={"operations": []},
        result=result,
        allow_clash=False,
    )

    assert result["status"] == "conflict"
    assert result["applied"] is False
    assert result["conflict"]["type"] == "postcondition_failed"
    assert _activity_by_title(result["envelope"], "Seminar")["fixed_start"] == parse_clock("11:00")
    assert _activity_by_title(result["envelope"], "FYP Implementation")["scheduled_start"] == parse_clock("14:00")
    assert reply["reply_status"] == "conflict"
    assert "couldn't apply" in reply["reply"]


def test_module_8_natural_conflict_reply_is_used_when_truthful():
    engine = SchedulingEngine(NaturalConflictReplyClient())
    envelope = engine.build_schedule_response(
        parsed=_initial_parsed_request(),
        current_schedule=None,
        latest_request="initial",
    )["schedule_data"]

    result = engine.apply_operations(
        envelope=envelope,
        operations=[
            {
                "op": "move",
                "title": "FYP Implementation",
                "timing_mode": TimingMode.RELATIVE,
                "anchor_relation": {"kind": "before", "target_title": "Seminar"},
                "_user_message": "I want to do my FYP implementation before the Seminar.",
            }
        ],
        base_version=envelope["version"],
    )
    reply = engine.compose_result_reply(
        latest_request="I want to do my FYP implementation before the Seminar.",
        parsed={"operations": []},
        result=result,
        allow_clash=False,
    )

    assert reply["reply_status"] == "conflict"
    assert "only a 30-minute gap" in reply["reply"]
    assert "couldn't apply that change" not in reply["reply"]


def test_existing_unrelated_clash_does_not_block_unrelated_edit_when_allow_clash_is_off():
    engine = SchedulingEngine(DummyClient())
    envelope = engine.build_schedule_response(
        parsed=_initial_parsed_request(),
        current_schedule=None,
        latest_request="initial",
    )["schedule_data"]
    clashing = engine.apply_operations(
        envelope=_with_allow_clash(envelope, True),
        operations=[
            {"op": "move", "title": "Lunch", "fixed_start": "12:00", "notes": "Move lunch to 12 PM."}
        ],
        base_version=envelope["version"],
    )["envelope"]
    feasibility_first = _with_allow_clash(clashing, False)

    result = engine.apply_operations(
        envelope=feasibility_first,
        operations=[
            {"op": "add", "title": "Reading", "fixed_start": "20:00", "duration_minutes": 30, "location": "home"}
        ],
        base_version=feasibility_first["version"],
    )

    assert result["applied"] is True
    assert result["status"] == "success"
    assert _activity_by_title(result["envelope"], "Reading")["scheduled_start"] == parse_clock("20:00")
    assert result["envelope"]["conflicts"]
    assert all(conflict.get("conflict_lifecycle") == "existing" for conflict in result["envelope"]["conflicts"])

    reply = engine.compose_result_reply(
        latest_request="Add Reading at 8 PM.",
        parsed={"operations": [{"op": "add", "title": "Reading", "fixed_start": "20:00"}]},
        result=result,
        allow_clash=False,
    )
    assert reply["reply_status"] == "partial"
    assert "existing" in reply["reply"]
    assert "clash" in reply["reply"]


def test_success_reply_mentions_added_dinner_before_retained_existing_clash():
    engine = SchedulingEngine(DummyClient())
    envelope = engine.build_schedule_response(
        parsed=_initial_parsed_request(),
        current_schedule=None,
        latest_request="initial",
    )["schedule_data"]
    clashing = engine.apply_operations(
        envelope=_with_allow_clash(envelope, True),
        operations=[
            {"op": "move", "title": "Lunch", "fixed_start": "12:00", "notes": "Move lunch to 12 PM."}
        ],
        base_version=envelope["version"],
    )["envelope"]
    feasibility_first = _with_allow_clash(clashing, False)
    dinner_operation = {
        "op": "add",
        "title": "Dinner",
        "timing_mode": TimingMode.RELATIVE,
        "anchor_relation": {"kind": "after", "target_title": "Gym Workout"},
        "duration_minutes": 60,
        "location": "home",
    }

    result = engine.apply_operations(
        envelope=feasibility_first,
        operations=[dinner_operation],
        base_version=feasibility_first["version"],
    )
    reply = engine.compose_result_reply(
        latest_request="after gym add a dinner",
        parsed={"operations": [dinner_operation]},
        result=result,
        allow_clash=False,
    )

    assert result["applied"] is True
    assert result["status"] == "success"
    assert reply["reply_status"] == "partial"
    assert reply["reply"].startswith("I've added Dinner from ")
    assert "after Gym Workout" in reply["reply"]
    assert "Your existing" in reply["reply"]
    assert "clash is still marked" in reply["reply"]


def test_add_dinner_after_grocery_preserves_anchor_order_and_existing_times():
    engine = SchedulingEngine(DummyClient())
    envelope = _evening_schedule_envelope()
    operation = _dinner_after_grocery_operation()

    result = engine.apply_operations(
        envelope=envelope,
        operations=[operation],
        base_version=envelope["version"],
    )
    updated = result["envelope"]
    fyp = _activity_by_title(updated, "FYP Implementation")
    gym = _activity_by_title(updated, "Gym Workout")
    grocery = _activity_by_title(updated, "Grocery Shopping")
    dinner = _activity_by_title(updated, "Dinner")

    assert result["status"] == "success"
    assert grocery["scheduled_end"] <= dinner["scheduled_start"]
    assert fyp["scheduled_start"] == parse_clock("2:05 PM")
    assert gym["scheduled_start"] == parse_clock("5:40 PM")
    assert grocery["scheduled_start"] == parse_clock("7:00 PM")
    assert not updated.get("postcondition_results")


def test_add_dinner_after_shopping_can_use_later_flexible_window():
    engine = SchedulingEngine(DummyClient())
    envelope = _evening_schedule_envelope()
    operation = _dinner_after_grocery_operation()

    result = engine.apply_operations(
        envelope=envelope,
        operations=[operation],
        base_version=envelope["version"],
    )
    updated = result["envelope"]
    grocery = _activity_by_title(updated, "Grocery Shopping")
    gym = _activity_by_title(updated, "Gym Workout")
    dinner = _activity_by_title(updated, "Dinner")

    assert result["status"] == "success"
    assert grocery["scheduled_end"] <= dinner["scheduled_start"]
    assert gym["scheduled_end"] <= dinner["scheduled_start"]


def test_change_gym_to_5pm_attempts_flexible_repair():
    engine = SchedulingEngine(DummyClient())
    envelope = _evening_schedule_envelope()

    result = engine.apply_operations(
        envelope=envelope,
        operations=[{"op": "update", "title": "Gym", "fixed_start": "17:00", "_user_message": "change gym to 5pm"}],
        base_version=envelope["version"],
    )
    updated = result["envelope"]
    gym = _activity_by_title(updated, "Gym Workout")
    fyp = _activity_by_title(updated, "FYP Implementation")
    grocery = _activity_by_title(updated, "Grocery Shopping")

    assert result["status"] == "success"
    assert gym["scheduled_start"] == parse_clock("5:00 PM")
    assert not updated.get("conflicts")
    assert fyp["scheduled_end"] <= gym["scheduled_start"] or gym["scheduled_end"] <= fyp["scheduled_start"]
    assert grocery["scheduled_start"] >= fyp["scheduled_end"]


def test_relative_dependency_places_anchor_before_dependent_regardless_score():
    engine = SchedulingEngine(DummyClient())
    anchor = {
        "id": "act-anchor",
        "stable_activity_id": "act-anchor",
        "title": "Grocery Shopping",
        "timing_mode": TimingMode.UNSPECIFIED,
        "duration_minutes": 45,
        "priority": "low",
        "is_mandatory": True,
        "location": "store",
        "trace": [],
    }
    dependent = {
        "id": "act-dependent",
        "stable_activity_id": "act-dependent",
        "title": "Dinner",
        "timing_mode": TimingMode.RELATIVE,
        "anchor_relation": {"kind": "after", "target_title": "Grocery Shopping"},
        "duration_minutes": 60,
        "priority": "high",
        "is_mandatory": True,
        "location_category": "meal_place",
        "location_status": "needs_resolution",
        "trace": [],
    }

    planned = engine._plan_schedule(
        "2026-05-24",
        [dependent, anchor],
        {"allow_clash": False, "accurate_travel_time": False},
    )
    grocery = next(item for item in planned["activities"] if item["title"] == "Grocery Shopping")
    dinner = next(item for item in planned["activities"] if item["title"] == "Dinner")

    assert grocery["scheduled_end"] <= dinner["scheduled_start"]


def test_accurate_travel_unresolved_dinner_location_is_pending_not_conflict():
    fake_travel = FakeTravelService(route_minutes=8)
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    envelope = _evening_schedule_envelope(accurate_travel_time=True)

    result = engine.apply_operations(
        envelope=envelope,
        operations=[_dinner_after_grocery_operation()],
        base_version=envelope["version"],
        saved_locations=_saved_route_locations(),
    )
    updated = result["envelope"]
    grocery = _activity_by_title(updated, "Grocery Shopping")
    dinner = _activity_by_title(updated, "Dinner")

    assert result["status"] == "success"
    assert updated["schedule_status"] == "location_pending"
    assert updated["travel_validation_status"] == "pending_locations"
    assert grocery["scheduled_end"] <= dinner["scheduled_start"]
    assert any(request["title"] == "Dinner" for request in updated["location_resolution_requests"])
    assert not updated.get("conflicts")


def test_parser_safety_ignores_unrequested_gym_fixed_update_when_adding_dinner():
    engine = SchedulingEngine(DummyClient())
    envelope = _evening_schedule_envelope()
    message = "y not? i want to have dinner after shopping"
    dinner_operation = _dinner_after_grocery_operation()
    dinner_operation["_user_message"] = message

    result = engine.apply_operations(
        envelope=envelope,
        operations=[
            {
                "op": "update",
                "title": "Gym Workout",
                "fixed_start": "17:00",
                "duration_minutes": 60,
                "_user_message": message,
            },
            dinner_operation,
        ],
        base_version=envelope["version"],
    )
    updated = result["envelope"]
    gym = _activity_by_title(updated, "Gym Workout")
    grocery = _activity_by_title(updated, "Grocery Shopping")
    dinner = _activity_by_title(updated, "Dinner")

    assert result["status"] == "success"
    assert gym["scheduled_start"] == parse_clock("5:40 PM")
    assert grocery["scheduled_end"] <= dinner["scheduled_start"]


def test_existing_clash_blocks_editing_clashing_activity_when_allow_clash_is_off():
    engine = SchedulingEngine(DummyClient())
    envelope = engine.build_schedule_response(
        parsed=_initial_parsed_request(),
        current_schedule=None,
        latest_request="initial",
    )["schedule_data"]
    clashing = engine.apply_operations(
        envelope=_with_allow_clash(envelope, True),
        operations=[
            {"op": "move", "title": "Lunch", "fixed_start": "12:00", "notes": "Move lunch to 12 PM."}
        ],
        base_version=envelope["version"],
    )["envelope"]
    feasibility_first = _with_allow_clash(clashing, False)

    result = engine.apply_operations(
        envelope=feasibility_first,
        operations=[
            {"op": "update", "title": "Lunch", "location": "campus"}
        ],
        base_version=feasibility_first["version"],
    )

    assert result["status"] == "conflict"
    assert result["applied"] is False


def test_smart_timing_classifier_uses_domain_evidence():
    engine = SchedulingEngine(DummyClient())

    meeting = engine._canonicalize_activity(
        {
            "title": "Project Meeting",
            "timing_mode": TimingMode.FIXED,
            "fixed_start": "09:00",
            "duration_minutes": 60,
            "_user_message": "Project Meeting from 9 AM to 10 AM.",
        },
        default_source="initial_request",
    )
    gym = engine._canonicalize_activity(
        {
            "title": "Gym Workout",
            "timing_mode": TimingMode.FIXED,
            "fixed_start": "16:00",
            "duration_minutes": 60,
            "_user_message": "Fit in a gym workout at 4 PM.",
        },
        default_source="initial_request",
    )
    lunch = engine._canonicalize_activity(
        {
            "title": "Lunch",
            "timing_mode": TimingMode.FIXED,
            "fixed_start": "13:00",
            "duration_minutes": 60,
            "_user_message": "Lunch with my Girl at 1 PM near campus.",
        },
        default_source="initial_request",
    )
    fyp = engine._canonicalize_activity(
        {
            "title": "FYP Implementation",
            "earliest_start": "12:00",
            "latest_end": "18:00",
            "duration_minutes": 180,
            "_user_message": "Spend 3 hours on my FYP implementation in the afternoon.",
        },
        default_source="initial_request",
    )
    relative = engine._canonicalize_activity(
        {
            "title": "Coffee Break",
            "fixed_start": "10:30",
            "duration_minutes": 15,
            "anchor_relation": {"kind": "after", "target_title": "Project Meeting"},
            "_user_message": "Add coffee right after the meeting.",
        },
        default_source="initial_request",
    )

    assert meeting["timing_mode"] == TimingMode.FIXED
    assert lunch["timing_mode"] == TimingMode.FIXED
    assert gym["timing_mode"] == TimingMode.PREFERRED
    assert gym["preferred_start"] == parse_clock("16:00")
    assert gym["fixed_start"] is None
    assert fyp["timing_mode"] == TimingMode.WINDOW
    assert relative["timing_mode"] == TimingMode.RELATIVE


def test_whole_plan_shift_is_explicit_operation():
    engine = SchedulingEngine(DummyClient())
    parsed = engine._normalize_plan_level_operations(
        {
            "intent": "edit",
            "date": "2026-05-06",
            "operations": [{"op": "move", "title": "All Activities"}],
            "activities": [],
            "preferences": {},
        },
        "ah move these plan to 6th May, i said wrong about the date just now",
        {"date": "2026-05-05", "activities": [], "version": 1},
    )

    assert parsed["operations"][0]["op"] == "shift_plan_date"
    assert parsed["operations"][0]["from_date"] == "2026-05-05"
    assert parsed["operations"][0]["to_date"] == "2026-05-06"


def test_whole_plan_shift_moves_active_activities_to_target_date():
    engine = SchedulingEngine(DummyClient())
    source = engine.build_schedule_response(
        parsed={
            **_initial_parsed_request(),
            "date": "2026-05-05",
            "transcription": "source",
        },
        current_schedule=None,
        latest_request="source",
    )["schedule_data"]

    shifted = engine.apply_operations(
        envelope=source,
        operations=[
            {
                "op": "shift_plan_date",
                "from_date": "2026-05-05",
                "to_date": "2026-05-06",
                "scope": "all_active_activities",
            }
        ],
        base_version=source["version"],
    )

    assert shifted["status"] == "success"
    assert shifted["envelope"]["date"] == "2026-05-06"
    assert shifted["envelope"]["activities"]
    assert _count_title(shifted["envelope"], "Lunch") == 1


def test_whole_plan_shift_with_target_day_conflict_respects_allow_clash():
    engine = SchedulingEngine(DummyClient())
    source = engine.build_schedule_response(
        parsed={
            "intent": "schedule",
            "reply": "Source draft.",
            "transcription": "source",
            "date": "2026-05-05",
            "preferences": {"allow_clash": False},
            "operations": [
                {"op": "add", "title": "Lunch", "timing_mode": TimingMode.FIXED, "fixed_start": "12:00", "duration_minutes": 60},
            ],
        },
        current_schedule=None,
        latest_request="source",
    )["schedule_data"]
    target = engine.build_schedule_response(
        parsed={
            "intent": "schedule",
            "reply": "Target draft.",
            "transcription": "target",
            "date": "2026-05-06",
            "preferences": {"allow_clash": False},
            "operations": [
                {"op": "add", "title": "Seminar", "timing_mode": TimingMode.FIXED, "fixed_start": "11:00", "duration_minutes": 90},
            ],
        },
        current_schedule=None,
        latest_request="target",
    )["schedule_data"]

    rejected = engine.apply_operations(
        envelope=source,
        operations=[{"op": "shift_plan_date", "from_date": "2026-05-05", "to_date": "2026-05-06", "scope": "all_active_activities"}],
        base_version=source["version"],
        target_date_envelope=target,
    )
    assert rejected["status"] == "conflict"

    accepted = engine.apply_operations(
        envelope=_with_allow_clash(source, True),
        operations=[{"op": "shift_plan_date", "from_date": "2026-05-05", "to_date": "2026-05-06", "scope": "all_active_activities"}],
        base_version=source["version"],
        target_date_envelope=target,
    )
    assert accepted["status"] == "success"
    assert accepted["envelope"]["date"] == "2026-05-06"
    assert accepted["envelope"]["conflicts"]


def test_location_normalizer_overrides_llm_home_for_grocery_without_explicit_home(capsys):
    engine = SchedulingEngine(DummyClient())

    grocery = _build_single_activity_with_location(
        engine,
        "fit in a 45-minute grocery shopping trip",
        "Grocery Shopping",
        raw_location="home",
    )
    captured = capsys.readouterr().out

    assert grocery["location"] != "home"
    assert grocery["location"] in {"store", "supermarket", "market"}
    assert grocery["location_category"] == "supermarket"
    assert grocery["location_source"] == "deterministic_default"
    assert grocery["location_status"] == "needs_resolution"
    assert grocery["raw_llm_location"] == "home"
    assert grocery["explicit_user_location"] is False
    assert "[JPLAN][LOCATION][SUMMARY]" in captured
    assert "Grocery Shopping -> store (needs coordinates)" in captured


def test_verbose_logging_gate_keeps_diagnostics_available(monkeypatch, capsys):
    from jplan_logging import jlog_verbose

    monkeypatch.delenv("JPLAN_VERBOSE_LOGS", raising=False)
    jlog_verbose("TEST", "hidden diagnostic", "DETAIL")
    assert "hidden diagnostic" not in capsys.readouterr().out

    monkeypatch.setenv("JPLAN_VERBOSE_LOGS", "1")
    jlog_verbose("TEST", "shown diagnostic", "DETAIL")
    assert "[JPLAN][TEST][DETAIL] shown diagnostic" in capsys.readouterr().out


def test_location_normalizer_keeps_explicit_grocery_at_home():
    engine = SchedulingEngine(DummyClient())

    grocery = _build_single_activity_with_location(
        engine,
        "fit in a 45-minute grocery shopping trip at home",
        "Grocery Shopping",
        raw_location="home",
    )

    assert grocery["location"] == "home"
    assert grocery["location_category"] == "home"
    assert grocery["location_source"] == "explicit_user"
    assert grocery["location_status"] == "resolved"
    assert grocery["explicit_user_location"] is True


def test_semantic_contradiction_preserves_explicit_non_home_location():
    engine = SchedulingEngine(DummyClient())
    parsed = {
        "intent": "add",
        "reply": "Draft created.",
        "transcription": "doctor appointment at Sunway Medical",
        "date": "2026-05-02",
        "preferences": {"accurate_travel_time": False},
        "operations": [
            {
                "op": "add",
                "title": "Doctor appointment",
                "duration_minutes": 60,
                "raw_location_text": "Sunway Medical",
                "location_kind": "home",
                "location_category": "home",
                "explicit_user_location": True,
                "travel_required": False,
                "location_resolution_status": "resolved_coordinates",
            }
        ],
    }

    envelope = engine.build_schedule_response(
        parsed=parsed,
        current_schedule=None,
        latest_request=parsed["transcription"],
    )["schedule_data"]
    doctor = _activity_by_title(envelope, "Doctor appointment")

    assert doctor["location"] == "Sunway Medical"
    assert doctor["location_kind"] == "exact_named_place"
    assert doctor["location_status"] == "needs_resolution"
    assert doctor["travel_required"] is True


def test_location_normalizer_lunch_near_campus_is_explicit_campus_area():
    engine = SchedulingEngine(DummyClient())

    lunch = _build_single_activity_with_location(
        engine,
        "I need to have lunch near campus",
        "Lunch",
        raw_location="school",
    )

    assert lunch["location"] == "school"
    assert lunch["location_category"] == "campus_area"
    assert lunch["location_source"] == "explicit_user"
    assert lunch["location_status"] == "resolved"
    assert lunch["explicit_user_location"] is True


def test_location_normalizer_go_home_for_lunch_is_explicit_home():
    engine = SchedulingEngine(DummyClient())

    lunch = _build_single_activity_with_location(
        engine,
        "I need to go home for lunch",
        "Lunch",
        raw_location="home",
    )

    assert lunch["location"] == "home"
    assert lunch["location_category"] == "home"
    assert lunch["location_source"] == "explicit_user"
    assert lunch["location_status"] == "resolved"
    assert lunch["explicit_user_location"] is True


def test_location_normalizer_defaults_gym_to_fitness_center():
    engine = SchedulingEngine(DummyClient())

    gym = _build_single_activity_with_location(
        engine,
        "1-hour gym workout",
        "Gym Workout",
    )

    assert gym["location"] == "gym"
    assert gym["location_category"] == "fitness_center"
    assert gym["location_source"] == "deterministic_default"
    assert gym["location_status"] == "resolved_default"


def test_busy_workday_location_mentions_are_scoped_to_nearest_activity():
    engine = SchedulingEngine(DummyClient())
    request_text = (
        "Generate a busy workday for me, this is for 24th of May. At that day I have a "
        "Project Meeting from 9:00 AM to 10:30 AM at the Main Office, followed by a Seminar "
        "from 11:00 AM to 12:30 PM at the Library. I need to have Lunch with my Girl at "
        "1:00 PM near the campus. In the afternoon, I must spend 3 hours on my FYP implementation. "
        "Also, fit in a 45-minute grocery shopping trip and a 1-hour Gym workout."
    )
    parsed = {
        **_initial_parsed_request(),
        "date": "2026-05-24",
        "transcription": request_text,
    }
    for operation in parsed["operations"]:
        if operation["title"] == "Grocery Shopping":
            operation["location"] = "home"
        if operation["title"] == "Project Meeting":
            operation["location"] = "school"
        if operation["title"] == "FYP Implementation":
            operation["location"] = "school"

    envelope = engine.build_schedule_response(
        parsed=parsed,
        current_schedule=None,
        latest_request=request_text,
    )["schedule_data"]
    meeting = _activity_by_title(envelope, "Project Meeting")
    seminar = _activity_by_title(envelope, "Seminar")
    lunch = _activity_by_title(envelope, "Lunch")
    fyp = _activity_by_title(envelope, "FYP Implementation")
    grocery = _activity_by_title(envelope, "Grocery Shopping")
    gym = _activity_by_title(envelope, "Gym Workout")
    grocery_block = next(
        block
        for block in envelope["schedule_blocks"]
        if block.get("block_type") == "activity" and block.get("title") == "Grocery Shopping"
    )

    assert meeting["location"] == "office"
    assert meeting["location_category"] == "office"
    assert meeting["location_source"] == "explicit_user"
    assert meeting["location"] != "library"
    assert seminar["location"] == "library"
    assert seminar["location_source"] == "explicit_user"
    assert lunch["location"] == "school"
    assert lunch["location_category"] == "campus_area"
    assert lunch["location_source"] == "explicit_user"
    assert fyp["location"] is None
    assert fyp["location_category"] == "home_or_online"
    assert fyp["location_status"] == "not_required"
    assert fyp["travel_required"] is False
    assert grocery["location"] != "home"
    assert grocery["location"] != "office"
    assert grocery["location"] != "library"
    assert grocery["location"] in {"store", "supermarket", "market"}
    assert grocery["location_category"] == "supermarket"
    assert gym["location"] == "gym"
    assert gym["location"] != "office"
    assert gym["location"] != "library"
    assert grocery_block["location"] != "home"
    assert grocery_block["location_category"] == "supermarket"


def _productive_day_request_text():
    return (
        "Can you help me plan a productive day for 24 May? I don't really have any fixed appointments that day, "
        "but I have quite a lot of things I want to finish. I should spend around 3 hours on my FYP implementation, "
        "and I would prefer to do that sometime after lunch when I can focus properly. I also want to review my "
        "assignment for about an hour before doing the FYP work if possible. In the morning, I'm thinking of going "
        "to the campus library to study for a while, maybe around 1 to 2 hours. After that, I might want to take a "
        "short coffee break near campus. I also want to go to the gym for about an hour, preferably not too late. "
        "Later in the day, I need to buy some groceries, and I also want to have dinner near home. At night, I "
        "should call my parents and maybe spend a little time planning tomorrow. Try to arrange everything in a "
        "way that does not feel too rushed, avoids unnecessary travel back and forth, and leaves some reasonable gaps."
    )


def _productive_day_parsed_request():
    request_text = _productive_day_request_text()
    return {
        "intent": "add",
        "reply": "Draft created.",
        "transcription": request_text,
        "date": "2026-05-24",
        "preferences": {"allow_clash": False},
        "operations": [
            {"op": "add", "title": "Study at library", "timing_mode": "flexible", "duration_minutes": 120, "priority": "medium", "location": "library", "sequence_index": 1},
            {"op": "add", "title": "Coffee break", "timing_mode": "relative", "duration_minutes": 30, "priority": "low", "anchor_relation": {"kind": "after", "target_title": "Study at library"}, "sequence_index": 2},
            {"op": "add", "title": "Gym", "timing_mode": "flexible", "duration_minutes": 60, "priority": "medium", "sequence_index": 3},
            {"op": "add", "title": "Review assignment", "timing_mode": "flexible", "duration_minutes": 60, "priority": "high", "location": "store", "sequence_index": 4},
            {"op": "add", "title": "FYP implementation", "timing_mode": "relative", "duration_minutes": 180, "priority": "high", "anchor_relation": {"kind": "after", "target_title": "Review assignment"}, "sequence_index": 5},
            {"op": "add", "title": "Buy groceries", "timing_mode": "flexible", "duration_minutes": 45, "priority": "medium", "sequence_index": 6},
            {"op": "add", "title": "Dinner", "timing_mode": "flexible", "duration_minutes": 60, "priority": "medium", "location": "home", "sequence_index": 7},
            {"op": "add", "title": "Call parents", "timing_mode": "flexible", "duration_minutes": 30, "priority": "high", "location": "store", "sequence_index": 8},
            {"op": "add", "title": "Plan tomorrow", "timing_mode": "relative", "duration_minutes": 15, "priority": "low", "location": "store", "anchor_relation": {"kind": "after", "target_title": "Call parents"}, "sequence_index": 9},
        ],
    }


def test_natural_productive_day_location_neutral_tasks_do_not_become_store():
    engine = SchedulingEngine(DummyClient())
    parsed = _productive_day_parsed_request()

    envelope = engine.build_schedule_response(
        parsed=parsed,
        current_schedule=None,
        latest_request=_productive_day_request_text(),
    )["schedule_data"]

    for title in ("Review assignment", "FYP implementation", "Call parents", "Plan tomorrow"):
        activity = _activity_by_title(envelope, title)
        assert activity["location"] is None
        assert activity["location_category"] == "home_or_online"
        assert activity["location_status"] == "not_required"
        assert activity["travel_required"] is False

    groceries = _activity_by_title(envelope, "Buy groceries")
    assert groceries["location"] == "store"
    assert groceries["location_category"] == "supermarket"
    assert groceries["travel_required"] is True

    dinner = _activity_by_title(envelope, "Dinner")
    assert dinner["location_category"] == "meal_place"
    assert dinner["area_preference"] == "near_home"
    assert dinner["location"] is None


def test_natural_productive_day_soft_preferences_and_night_window():
    engine = SchedulingEngine(DummyClient())
    parsed = _productive_day_parsed_request()

    normalized = engine._normalize_parsed_locations(
        parsed,
        _productive_day_request_text(),
        saved_locations=[],
    )
    operations_by_title = {operation["title"]: operation for operation in normalized["operations"]}

    fyp = operations_by_title["FYP implementation"]
    assert fyp.get("anchor_relation") is None
    assert fyp["preferred_order"]["target_title"] == "Review assignment"
    assert fyp["soft_dependency"] is True
    assert fyp["preferred_time_window"] == "after_lunch"

    call = operations_by_title["Call parents"]
    assert call["preferred_time_window"] == "night"
    assert call["preferred_window_start"] == parse_clock("8:00 PM")

    plan = operations_by_title["Plan tomorrow"]
    assert plan.get("anchor_relation") is None
    assert plan["preferred_time_window"] == "night"
    assert plan["preferred_window_start"] == parse_clock("8:00 PM")


def test_natural_productive_day_adds_lunch_and_places_fyp_after_lunch():
    engine = SchedulingEngine(DummyClient())
    parsed = _productive_day_parsed_request()

    envelope = engine.build_schedule_response(
        parsed=parsed,
        current_schedule=None,
        latest_request=_productive_day_request_text(),
    )["schedule_data"]

    lunch = _activity_by_title(envelope, "Lunch Break")
    fyp = _activity_by_title(envelope, "FYP implementation")
    review = _activity_by_title(envelope, "Review assignment")

    assert lunch["implicit_activity"] is True
    assert lunch["preferred_time_window"] == "lunch"
    assert parse_clock("12:00 PM") <= lunch["scheduled_start"] <= parse_clock("2:00 PM")
    assert fyp["scheduled_start"] >= lunch["scheduled_end"]
    assert fyp["scheduled_start"] >= parse_clock("12:00 PM")
    assert review["scheduled_end"] <= fyp["scheduled_start"]
    assert any(order["target_title"] == "Lunch Break" for order in fyp["preferred_orders"])
    assert any(order["target_title"] == "Review assignment" for order in fyp["preferred_orders"])


def test_implicit_lunch_not_duplicated_when_lunch_exists():
    engine = SchedulingEngine(DummyClient())
    request = (
        "Plan 24 May. Lunch with my friend is at 1 PM. "
        "I want to do FYP implementation sometime after lunch."
    )
    parsed = {
        "intent": "add",
        "reply": "Draft created.",
        "transcription": request,
        "date": "2026-05-24",
        "preferences": {"allow_clash": False},
        "operations": [
            {"op": "add", "title": "Lunch with my friend", "timing_mode": "fixed", "fixed_start": "13:00", "duration_minutes": 60, "priority": "medium"},
            {"op": "add", "title": "FYP implementation", "timing_mode": "flexible", "duration_minutes": 180, "priority": "high"},
            {"op": "add", "title": "Assignment review", "timing_mode": "flexible", "duration_minutes": 60, "priority": "medium"},
        ],
    }

    envelope = engine.build_schedule_response(
        parsed=parsed,
        current_schedule=None,
        latest_request=request,
    )["schedule_data"]

    lunch_titles = [activity["title"] for activity in envelope["activities"] if "lunch" in activity["title"].lower()]
    assert lunch_titles == ["Lunch with my friend"]


def test_before_lunch_adds_lunch_and_prefers_activity_before_it():
    engine = SchedulingEngine(DummyClient())
    request = "Plan 24 May. I want to review my assignment before lunch if possible, then go to the gym."
    parsed = {
        "intent": "add",
        "reply": "Draft created.",
        "transcription": request,
        "date": "2026-05-24",
        "preferences": {"allow_clash": False},
        "operations": [
            {"op": "add", "title": "Review assignment", "timing_mode": "flexible", "duration_minutes": 60, "priority": "high"},
            {"op": "add", "title": "Gym", "timing_mode": "flexible", "duration_minutes": 60, "priority": "medium"},
        ],
    }

    envelope = engine.build_schedule_response(
        parsed=parsed,
        current_schedule=None,
        latest_request=request,
    )["schedule_data"]

    lunch = _activity_by_title(envelope, "Lunch Break")
    review = _activity_by_title(envelope, "Review assignment")
    assert review["scheduled_end"] <= lunch["scheduled_start"]
    assert review["preferred_order"]["kind"] == "before"
    assert review["preferred_order"]["target_title"] == "Lunch Break"


def test_simple_edit_after_lunch_does_not_infer_new_lunch():
    engine = SchedulingEngine(DummyClient())
    envelope = _custom_envelope([
        {"op": "add", "title": "Gym", "timing_mode": "flexible", "duration_minutes": 60},
        {"op": "add", "title": "Study", "timing_mode": "flexible", "duration_minutes": 60},
    ])

    result = engine.apply_operations(
        envelope=envelope,
        operations=[{
            "op": "update",
            "title": "Gym",
            "target_title": "Gym",
            "timing_mode": "relative",
            "anchor_relation": {"kind": "after", "target_title": "Lunch"},
            "_user_message": "move gym after lunch",
            "_router_route": "simple_schedule_command",
        }],
        base_version=envelope["version"],
    )

    assert result["status"] == "clarification_needed"
    assert all(activity["title"] != "Lunch Break" for activity in result["activities"])


def test_natural_productive_day_generates_partial_instead_of_full_rejection():
    engine = SchedulingEngine(DummyClient())
    parsed = _productive_day_parsed_request()

    envelope = engine.build_schedule_response(
        parsed=parsed,
        current_schedule=None,
        latest_request=_productive_day_request_text(),
    )["schedule_data"]

    assert envelope["status"] in {"ok", "warning", "partial"}
    assert len(envelope["activities"]) >= 7
    assert "kept unchanged" not in " ".join(envelope.get("explanations") or []).lower()
    fyp = _activity_by_title(envelope, "FYP implementation")
    assert fyp["scheduled_start"] >= parse_clock("12:00 PM")
    call = _activity_by_title(envelope, "Call parents")
    assert call["scheduled_start"] >= parse_clock("8:00 PM")
    plan = _activity_by_title(envelope, "Plan tomorrow")
    assert plan["scheduled_start"] >= parse_clock("8:00 PM")


def test_natural_productive_day_module_d_scans_movable_candidates(capsys):
    engine = SchedulingEngine(DummyClient())
    parsed = _productive_day_parsed_request()
    capsys.readouterr()

    envelope = engine.build_schedule_response(
        parsed=parsed,
        current_schedule=None,
        latest_request=_productive_day_request_text(),
    )["schedule_data"]
    logs = capsys.readouterr().out

    assert envelope["preferences"]["refinement_reason"] == "initial_generation"
    assert "[JPLAN][MODULE_D][START] reason=initial_generation" in logs
    assert "[JPLAN][MODULE_D][CANDIDATE_SCAN]" in logs
    assert "movable=true" in logs
    assert "[JPLAN][MODULE_D][NO_CANDIDATE] reason=all_movable_items_protected" not in logs


def test_apply_operations_scans_original_request_for_natural_preferences(capsys):
    engine = SchedulingEngine(DummyClient())
    request = _productive_day_request_text()
    envelope = _empty_envelope("2026-05-24")
    envelope["preferences"]["module_0_route"] = "complex_schedule_command"
    operations = [
        {"op": "add", "title": "Library Study", "timing_mode": "flexible", "duration_minutes": 90, "priority": "medium", "location": "library", "location_status": "fallback_used", "raw_llm_location": "library", "travel_required": True},
        {"op": "add", "title": "Coffee Break", "timing_mode": "relative", "duration_minutes": 15, "priority": "low", "anchor_relation": {"kind": "after", "target_title": "Library Study"}, "location": None, "location_category": "meal_place", "location_status": "needs_resolution", "raw_llm_location": None, "travel_required": True},
        {"op": "add", "title": "Gym", "timing_mode": "flexible", "duration_minutes": 60, "priority": "medium", "location": "gym", "location_status": "resolved_default", "raw_llm_location": None, "travel_required": True},
        {"op": "add", "title": "Assignment Review", "timing_mode": "flexible", "duration_minutes": 60, "priority": "high", "location": None, "location_category": "home_or_online", "location_status": "not_required", "raw_llm_location": None, "travel_required": False},
        {"op": "add", "title": "FYP Implementation", "timing_mode": "relative", "duration_minutes": 180, "priority": "high", "anchor_relation": {"kind": "after", "target_title": "Assignment Review"}, "location": None, "location_category": "home_or_online", "location_status": "not_required", "raw_llm_location": None, "travel_required": False},
        {"op": "add", "title": "Grocery Shopping", "timing_mode": "flexible", "duration_minutes": 45, "priority": "medium", "location": "store", "location_status": "needs_resolution", "raw_llm_location": None, "travel_required": True},
        {"op": "add", "title": "Dinner", "timing_mode": "flexible", "duration_minutes": 60, "priority": "medium", "location": None, "location_category": "meal_place", "location_status": "needs_resolution", "raw_llm_location": "home", "travel_required": True},
        {"op": "add", "title": "Call Parents", "timing_mode": "flexible", "duration_minutes": 30, "priority": "high", "location": None, "location_category": "home_or_online", "location_status": "not_required", "raw_llm_location": None, "travel_required": False},
        {"op": "add", "title": "Plan Tomorrow", "timing_mode": "flexible", "duration_minutes": 15, "priority": "low", "preferred_time_window": "night", "preferred_window_start": 1200, "preferred_window_end": 1320, "location": None, "location_category": "home_or_online", "location_status": "not_required", "raw_llm_location": None, "travel_required": False},
    ]

    capsys.readouterr()
    result = engine.apply_operations(
        envelope=envelope,
        operations=[
            {
                **operation,
                "_user_message": "Plan a productive day with FYP, dinner, call parents, and planning tomorrow.",
                "_latest_request": request,
                "_router_route": "complex_schedule_command",
                "_router_reason": "multi_activity_generation",
            }
            for operation in operations
        ],
        base_version=envelope["version"],
    )
    logs = capsys.readouterr().out

    updated = result["envelope"]
    lunch = _activity_by_title(updated, "Lunch Break")
    fyp = _activity_by_title(updated, "FYP Implementation")
    dinner = _activity_by_title(updated, "Dinner")
    call = _activity_by_title(updated, "Call Parents")
    plan = _activity_by_title(updated, "Plan Tomorrow")

    assert lunch["implicit_activity"] is True
    assert "[JPLAN][IMPLICIT_LUNCH][DETECT] found=true" in logs
    assert "[JPLAN][IMPLICIT_LUNCH][CHECK] existing_lunch=false existing_meal_dinner_ignored=true" in logs
    assert "[JPLAN][IMPLICIT_LUNCH][ADD] title=Lunch Break window=12:00 PM-02:00 PM" in logs
    assert "[JPLAN][NORMALIZED_OPS] count=10" in logs
    assert "FYP Implementation -> preferred_window=after_lunch; preferred_order=after Lunch Break" in logs
    assert "Call Parents -> preferred_window=night" in logs
    assert fyp["preferred_time_window"] == "after_lunch"
    assert any(order["target_title"] == "Lunch Break" for order in fyp["preferred_orders"])
    assert fyp["scheduled_start"] >= lunch["scheduled_end"]
    assert dinner["preferred_time_window"] == "evening"
    assert dinner["scheduled_start"] >= parse_clock("6:00 PM")
    assert dinner["area_preference"] == "near_home"
    assert call["preferred_time_window"] == "night"
    assert call["scheduled_start"] >= parse_clock("8:00 PM")
    assert plan["preferred_time_window"] == "night"
    assert plan["scheduled_start"] >= parse_clock("8:00 PM")


def test_location_normalizer_does_not_leak_library_to_meeting():
    engine = SchedulingEngine(DummyClient())
    request_text = "Meeting at Main Office, followed by Seminar at Library."
    parsed = {
        "intent": "add",
        "reply": "Draft created.",
        "transcription": request_text,
        "date": "2026-05-02",
        "preferences": {"allow_clash": False},
        "operations": [
            {"op": "add", "title": "Meeting", "duration_minutes": 60, "location": "school"},
            {"op": "add", "title": "Seminar", "duration_minutes": 60, "location": "library"},
        ],
    }

    envelope = engine.build_schedule_response(
        parsed=parsed,
        current_schedule=None,
        latest_request=request_text,
    )["schedule_data"]

    meeting = _activity_by_title(envelope, "Meeting")
    seminar = _activity_by_title(envelope, "Seminar")
    assert meeting["location"] == "office"
    assert meeting["location_source"] == "explicit_user"
    assert seminar["location"] == "library"
    assert meeting["location"] != seminar["location"]


def test_location_normalizer_does_not_leak_library_to_lunch():
    engine = SchedulingEngine(DummyClient())
    request_text = "Seminar at Library. Lunch near campus."
    parsed = {
        "intent": "add",
        "reply": "Draft created.",
        "transcription": request_text,
        "date": "2026-05-02",
        "preferences": {"allow_clash": False},
        "operations": [
            {"op": "add", "title": "Seminar", "duration_minutes": 60, "location": "library"},
            {"op": "add", "title": "Lunch", "duration_minutes": 60, "location": "library"},
        ],
    }

    envelope = engine.build_schedule_response(
        parsed=parsed,
        current_schedule=None,
        latest_request=request_text,
    )["schedule_data"]

    seminar = _activity_by_title(envelope, "Seminar")
    lunch = _activity_by_title(envelope, "Lunch")
    assert seminar["location"] == "library"
    assert lunch["location"] == "school"
    assert lunch["location_category"] == "campus_area"
    assert lunch["location_source"] == "explicit_user"


def test_location_normalizer_allows_explicit_shared_location_wording():
    engine = SchedulingEngine(DummyClient())
    request_text = "Meeting and Seminar both at the Library."
    parsed = {
        "intent": "add",
        "reply": "Draft created.",
        "transcription": request_text,
        "date": "2026-05-02",
        "preferences": {"allow_clash": False},
        "operations": [
            {"op": "add", "title": "Meeting", "duration_minutes": 60},
            {"op": "add", "title": "Seminar", "duration_minutes": 60},
        ],
    }

    envelope = engine.build_schedule_response(
        parsed=parsed,
        current_schedule=None,
        latest_request=request_text,
    )["schedule_data"]

    assert _activity_by_title(envelope, "Meeting")["location"] == "library"
    assert _activity_by_title(envelope, "Seminar")["location"] == "library"


def test_location_normalizer_allows_same_phrase_shared_campus_location():
    engine = SchedulingEngine(DummyClient())
    request_text = "Lunch and Study at campus."
    parsed = {
        "intent": "add",
        "reply": "Draft created.",
        "transcription": request_text,
        "date": "2026-05-02",
        "preferences": {"allow_clash": False},
        "operations": [
            {"op": "add", "title": "Lunch", "duration_minutes": 60},
            {"op": "add", "title": "Study", "duration_minutes": 60},
        ],
    }

    envelope = engine.build_schedule_response(
        parsed=parsed,
        current_schedule=None,
        latest_request=request_text,
    )["schedule_data"]

    assert _activity_by_title(envelope, "Lunch")["location"] == "school"
    assert _activity_by_title(envelope, "Lunch")["location_source"] == "explicit_user"
    assert _activity_by_title(envelope, "Study")["location"] == "school"
    assert _activity_by_title(envelope, "Study")["location_source"] == "explicit_user"


def test_accurate_travel_off_keeps_heuristic_without_location_pending():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    parsed = {
        "intent": "add",
        "reply": "Draft created.",
        "transcription": "Fit in a grocery shopping trip.",
        "date": "2026-05-02",
        "preferences": {"accurate_travel_time": False},
        "operations": [
            {"op": "add", "title": "Grocery Shopping", "duration_minutes": 45, "location": "home"},
        ],
    }

    envelope = engine.build_schedule_response(parsed, None, "Fit in a grocery shopping trip.")["schedule_data"]

    assert envelope["accurate_travel_time"] is False
    assert envelope["preferences"]["accurate_travel_time"] is False
    assert envelope["travel_validation_status"] == "not_requested"
    assert envelope["location_resolution_requests"] == []
    assert envelope["status"] != "location_pending"


def test_travel_intent_with_accurate_off_returns_physical_location_requests():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    parsed = {
        "intent": "add",
        "reply": "Draft created.",
        "transcription": (
            "Plan my day for next Tuesday. I have a doctor appointment from 9:00 AM to 10:00 AM "
            "at Sunway Medical, a lunch meeting at 12:30 PM in SS15, and a dinner with family "
            "at 7:30 PM at home. I still need focused work, grocery shopping, a pharmacy stop, "
            "gym, and prepare documents. Please make it realistic with travel time."
        ),
        "date": "2026-05-26",
        "preferences": {
            "accurate_travel_time": False,
            "travel_intent": True,
            "default_start_location": {
                "label": "home",
                "display_name": "Home",
                "category": "home",
                "latitude": 2.9,
                "longitude": 101.6,
            },
        },
        "operations": [
            {
                "op": "add",
                "title": "Doctor appointment",
                "timing_mode": TimingMode.FIXED,
                "fixed_start": "09:00",
                "duration_minutes": 60,
                "raw_location_text": "Sunway Medical",
                "location_kind": "exact_named_place",
                "location_category": "medical",
                "explicit_user_location": True,
                "travel_required": True,
                "location_resolution_status": "needs_coordinates",
            },
            {
                "op": "add",
                "title": "Lunch meeting",
                "timing_mode": TimingMode.FIXED,
                "fixed_start": "12:30",
                "duration_minutes": 60,
                "raw_location_text": "SS15",
                "location_kind": "area_only",
                "location_category": "meal_place",
                "explicit_user_location": True,
                "travel_required": True,
                "location_resolution_status": "needs_coordinates",
            },
            {
                "op": "add",
                "title": "Dinner with family",
                "timing_mode": TimingMode.FIXED,
                "fixed_start": "19:30",
                "duration_minutes": 60,
                "raw_location_text": "home",
                "location_kind": "home",
                "location_category": "home",
                "explicit_user_location": True,
                "travel_required": True,
                "location_resolution_status": "resolved_coordinates",
            },
            {
                "op": "add",
                "title": "Prepare documents",
                "timing_mode": TimingMode.RELATIVE,
                "anchor_relation": {"kind": "before", "target_title": "Doctor appointment"},
                "duration_minutes": 30,
                "location_kind": "no_location_required",
                "location_category": "no_location",
                "travel_required": False,
                "location_resolution_status": "not_required",
                "no_location_reason": "document_preparation",
            },
            {
                "op": "add",
                "title": "Focused work",
                "duration_minutes": 120,
                "location_kind": "no_location_required",
                "location_category": "no_location",
                "travel_required": False,
                "location_resolution_status": "not_required",
                "no_location_reason": "focused_work",
            },
            {
                "op": "add",
                "title": "Grocery shopping",
                "duration_minutes": 45,
                "location_kind": "category_only",
                "location_category": "supermarket",
                "travel_required": True,
                "location_resolution_status": "needs_coordinates",
            },
            {
                "op": "add",
                "title": "Pharmacy stop",
                "duration_minutes": 15,
                "location_kind": "category_only",
                "location_category": "pharmacy",
                "travel_required": True,
                "location_resolution_status": "needs_coordinates",
            },
            {
                "op": "add",
                "title": "Gym",
                "duration_minutes": 45,
                "location_kind": "category_only",
                "location_category": "fitness_center",
                "travel_required": True,
                "location_resolution_status": "needs_coordinates",
            },
        ],
    }
    saved_locations = [
        {
            "label": "home",
            "display_name": "Home",
            "category": "home",
            "latitude": 2.9,
            "longitude": 101.6,
        }
    ]

    envelope = engine.build_schedule_response(
        parsed,
        None,
        parsed["transcription"],
        saved_locations=saved_locations,
    )["schedule_data"]

    requested = {request["title"] for request in envelope["location_resolution_requests"]}
    assert envelope["accurate_travel_time"] is True
    assert envelope["preferences"]["accurate_travel_time"] is True
    assert envelope["travel_intent"] is True
    assert envelope["travel_validation_status"] == "pending_locations"
    assert requested == {
        "Doctor appointment",
        "Lunch meeting",
        "Grocery shopping",
        "Pharmacy stop",
        "Gym",
    }
    assert _activity_by_title(envelope, "Doctor appointment")["location"] == "Sunway Medical"
    assert _activity_by_title(envelope, "Prepare documents")["travel_required"] is False
    assert _activity_by_title(envelope, "Pharmacy stop")["location_category"] == "pharmacy"


def test_module_8_location_pending_reply_lists_all_short_requests():
    engine = SchedulingEngine(DummyClient())
    requested_titles = [
        "Grocery shopping",
        "Pharmacy stop",
        "Doctor appointment",
        "Lunch meeting",
        "Gym",
    ]

    reply = engine.compose_result_reply(
        latest_request="Plan my day with travel time.",
        parsed={"operations": []},
        result={
            "envelope": {
                "date": "2026-05-26",
                "status": "location_pending",
                "schedule_status": "location_pending",
                "location_resolution_requests": [{"title": title} for title in requested_titles],
                "schedule_blocks": [],
                "activities": [],
            }
        },
        allow_clash=False,
    )

    assert reply["reply_status"] == "location_pending"
    for title in requested_titles:
        assert title in reply["reply"]


def test_router_keep_enough_travel_and_buffer_sets_travel_intent():
    engine = SchedulingEngine(DummyClient())

    route = engine.route_chat_request(
        "Generate a busy workday and keep enough travel and buffer time."
    )

    assert route["travel_intent"] is True


def test_busy_workday_semantic_defaults_and_location_requests():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    request_text = (
        "Generate a busy workday for me on 18 August. I have a team stand-up "
        "from 8:30 AM to 9:00 AM at the office, a client presentation from "
        "11:00 AM to 12:00 PM in Bangsar, and a follow-up call at 4:30 PM "
        "that can be done anywhere quiet. I also need 2.5 hours for proposal "
        "writing, lunch, a bank visit, grocery shopping, and one short coffee "
        "break if possible. Keep enough travel and buffer time."
    )
    parsed = {
        "intent": "add",
        "reply": "Draft created.",
        "transcription": request_text,
        "date": "2026-08-18",
        "preferences": {"accurate_travel_time": False, "travel_intent": True},
        "operations": [
            {"op": "add", "title": "Team stand-up", "timing_mode": TimingMode.FIXED, "fixed_start": "08:30", "duration_minutes": 30, "location": "the office", "raw_location_text": "the office", "location_kind": "exact_named_place", "location_category": "work", "explicit_user_location": True, "travel_required": True, "location_resolution_status": "needs_coordinates"},
            {"op": "add", "title": "Client presentation", "timing_mode": TimingMode.FIXED, "fixed_start": "11:00", "duration_minutes": 60, "location": "Bangsar", "raw_location_text": "Bangsar", "location_kind": "area_only", "location_category": "work", "explicit_user_location": True, "travel_required": True, "location_resolution_status": "needs_coordinates"},
            {"op": "add", "title": "Follow-up call", "timing_mode": TimingMode.FIXED, "fixed_start": "16:30", "duration_minutes": 30, "location_kind": "no_location_required", "location_category": "no_location", "travel_required": False, "location_resolution_status": "not_required"},
            {"op": "add", "title": "Proposal writing", "duration_minutes": 150, "location_kind": "no_location_required", "location_category": "no_location", "travel_required": False, "location_resolution_status": "not_required"},
            {"op": "add", "title": "Lunch", "duration_minutes": 60, "location_kind": "category_only", "location_category": "meal_place", "travel_required": True, "location_resolution_status": "needs_coordinates"},
            {"op": "add", "title": "Bank visit", "duration_minutes": 45, "location_kind": "category_only", "location_category": "unknown", "location_resolution_status": "not_required"},
            {"op": "add", "title": "Grocery shopping", "duration_minutes": 45, "location_kind": "category_only", "location_category": "supermarket", "travel_required": True, "location_resolution_status": "needs_coordinates"},
            {"op": "add", "title": "Coffee break", "duration_minutes": 15, "location_kind": "category_only", "location_category": "meal_place", "travel_required": True, "location_resolution_status": "needs_coordinates"},
        ],
    }

    normalized = engine._normalize_parsed_locations(parsed, request_text)
    operations = normalized["operations"]
    lunch = next(item for item in operations if item["title"] == "Lunch")
    bank = next(item for item in operations if item["title"] == "Bank visit")
    standup = next(item for item in operations if item["title"] == "Team stand-up")
    call = next(item for item in operations if item["title"] == "Follow-up call")
    proposal = next(item for item in operations if item["title"] == "Proposal writing")
    coffee = next(item for item in operations if item["title"] == "Coffee break")

    assert standup["travel_required"] is True
    assert standup["location_resolution_status"] == "needs_coordinates"
    assert standup["location"] == "the office"
    assert lunch["preferred_window_start"] == parse_clock("12:00 PM")
    assert lunch["preferred_window_end"] == parse_clock("02:00 PM")
    assert lunch["earliest_start"] == parse_clock("11:00 AM")
    assert bank["location_category"] == "bank"
    assert bank["location_status"] == "needs_resolution"
    assert bank["travel_required"] is True
    assert bank["earliest_start"] == parse_clock("09:00 AM")
    assert bank["latest_end"] == parse_clock("04:00 PM")
    assert proposal["travel_required"] is False
    assert proposal["location_resolution_status"] == "not_required"
    assert proposal["location_kind"] == "no_location_required"
    assert proposal["location_category"] == "home_or_online"
    assert proposal["can_be_done_at_current_location"] is True
    assert proposal["quiet_place_required"] is True
    assert coffee["is_mandatory"] is False
    assert coffee["priority"] == "low"
    assert call["travel_required"] is False
    assert call["can_be_done_at_current_location"] is True
    assert call["quiet_place_required"] is True

    envelope = engine.build_schedule_response(
        normalized,
        None,
        request_text,
        saved_locations=[],
    )["schedule_data"]
    requested = {request["title"] for request in envelope["location_resolution_requests"]}
    lunch_block = _activity_by_title(envelope, "Lunch")
    bank_block = _activity_by_title(envelope, "Bank visit")

    assert parse_clock(lunch_block["startTime"]) >= parse_clock("11:00 AM")
    assert parse_clock(bank_block["startTime"]) >= parse_clock("09:00 AM")
    assert "Team stand-up" in requested
    assert "Bank visit" in requested
    assert "Grocery shopping" in requested
    assert "Proposal writing" not in requested
    assert "Follow-up call" not in requested


def test_proposal_writing_at_physical_place_requests_location():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    request_text = "Add 2 hours of proposal writing at the library."
    parsed = {
        "intent": "add",
        "reply": "Draft created.",
        "transcription": request_text,
        "date": "2026-08-18",
        "preferences": {"travel_intent": True},
        "operations": [
            {
                "op": "add",
                "title": "Proposal writing",
                "duration_minutes": 120,
                "raw_location_text": "library",
                "location_kind": "exact_named_place",
                "location_category": "library",
                "explicit_user_location": True,
                "travel_required": True,
                "location_resolution_status": "needs_coordinates",
            },
        ],
    }

    normalized = engine._normalize_parsed_locations(parsed, request_text)
    proposal = normalized["operations"][0]

    assert proposal["travel_required"] is True
    assert proposal["location_resolution_status"] == "needs_coordinates"
    assert proposal["location"] == "library"


def test_exact_office_location_overrides_stale_not_required_status():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    request_text = "I have a team stand-up from 8:30 AM to 9:00 AM at the office."
    parsed = {
        "intent": "add",
        "reply": "Draft created.",
        "transcription": request_text,
        "date": "2026-08-18",
        "preferences": {"travel_intent": True},
        "operations": [
            {
                "op": "add",
                "title": "Team stand-up",
                "timing_mode": TimingMode.FIXED,
                "fixed_start": "08:30",
                "duration_minutes": 30,
                "location": "the office",
                "raw_location_text": "the office",
                "location_kind": "exact_named_place",
                "location_category": "work",
                "travel_required": False,
                "location_resolution_status": "not_required",
            },
        ],
    }

    normalized = engine._normalize_parsed_locations(parsed, request_text)
    standup = normalized["operations"][0]

    assert standup["travel_required"] is True
    assert standup["location_resolution_status"] == "needs_coordinates"
    assert standup["location_status"] == "needs_resolution"
    assert standup["location"] == "the office"


def test_semantic_constraint_normalizer_deadline_pickup_lunch_and_return_home(monkeypatch):
    monkeypatch.setenv("SEMANTIC_NORMALIZER_MODE", "overwrite")
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    request_text = (
        "Plan my Saturday. I need to drop my younger brother at tuition by 9:00 AM, "
        "pick him up at 11:00 AM, attend a birthday lunch at 1:00 PM, and be back home by 8:00 PM. "
        "I also want to fit in 1 hour of study, grocery shopping, buying a gift, and a 30-minute rest. "
        "Please make the plan practical and not too tiring."
    )
    parsed = {
        "intent": "add",
        "reply": "Draft created.",
        "transcription": request_text,
        "date": "2026-06-06",
        "preferences": {},
        "operations": [
            {"op": "add", "title": "Drop brother at tuition", "timing_mode": TimingMode.PREFERRED, "fixed_start": "09:00", "duration_minutes": 30, "location_kind": "category_only", "location_category": "study", "travel_required": True, "location_resolution_status": "needs_coordinates"},
            {"op": "add", "title": "Pick up brother from tuition", "timing_mode": TimingMode.PREFERRED, "fixed_start": "11:00", "duration_minutes": 30, "location_kind": "category_only", "location_category": "study", "travel_required": True, "location_resolution_status": "needs_coordinates"},
            {"op": "add", "title": "Birthday lunch", "timing_mode": TimingMode.PREFERRED, "fixed_start": "13:00", "duration_minutes": 60, "location_kind": "category_only", "location_category": "meal_place", "travel_required": True, "location_resolution_status": "needs_coordinates"},
            {"op": "add", "title": "Study", "timing_mode": TimingMode.WINDOW, "earliest_start": "09:15", "latest_end": "11:00", "duration_minutes": 60, "location_kind": "no_location_required", "location_category": "no_location", "travel_required": False, "location_resolution_status": "not_required"},
            {"op": "add", "title": "Grocery shopping", "timing_mode": TimingMode.UNSPECIFIED, "duration_minutes": 45, "location_kind": "category_only", "location_category": "supermarket", "travel_required": True, "location_resolution_status": "needs_coordinates"},
            {"op": "add", "title": "Buy gift", "timing_mode": TimingMode.UNSPECIFIED, "duration_minutes": 30, "location_kind": "category_only", "location_category": "unknown", "travel_required": True, "location_resolution_status": "needs_coordinates"},
            {"op": "add", "title": "Rest", "timing_mode": TimingMode.UNSPECIFIED, "duration_minutes": 30, "location_kind": "home", "location_category": "home", "travel_required": True, "location_resolution_status": "needs_coordinates"},
            {"op": "add", "title": "Return home", "timing_mode": TimingMode.PREFERRED, "fixed_start": "20:00", "location_kind": "home", "location_category": "home", "travel_required": True, "location_resolution_status": "needs_coordinates"},
        ],
    }

    normalized = engine._normalize_parsed_locations(parsed, request_text)
    operations = normalized["operations"]
    dropoff = next(item for item in operations if item["title"] == "Drop brother at tuition")
    pickup = next(item for item in operations if item["title"] == "Pick up brother from tuition")
    lunch = next(item for item in operations if item["title"] == "Birthday lunch")
    study = next(item for item in operations if item["title"] == "Study")
    rest = next(item for item in operations if item["title"] == "Rest")

    assert "Return home" not in {item["title"] for item in operations}
    assert normalized["schedule_constraints"]["return_home_deadline"] == parse_clock("08:00 PM")
    assert normalized["preferences"]["low_fatigue_preference"] is True
    assert normalized["preferences"]["travel_intent"] is True
    assert dropoff["service_kind"] == "dropoff"
    assert dropoff["duration_minutes"] == 15
    assert dropoff["latest_end"] == parse_clock("09:00 AM")
    assert dropoff["timing_mode"] == TimingMode.WINDOW
    assert pickup["service_kind"] == "pickup"
    assert pickup["duration_minutes"] == 15
    assert pickup["timing_mode"] == TimingMode.FIXED
    assert parse_clock(pickup["fixed_start"]) == parse_clock("11:00 AM")
    assert pickup["fixed_end"] == parse_clock("11:15 AM")
    assert lunch["timing_mode"] == TimingMode.FIXED
    assert lunch["fixed_start"] == parse_clock("01:00 PM")
    assert study["location_flexible"] is True
    assert study["activity_role"] == "study"
    assert study["travel_context_required"] is True
    assert study["travel_required"] is False
    assert rest["location_flexible"] is True
    assert rest["activity_role"] == "recovery"
    assert not rest.get("travel_context_required")
    assert rest["travel_required"] is False

    envelope = engine.build_schedule_response(normalized, None, request_text, saved_locations=[])["schedule_data"]
    titles = {item["title"] for item in envelope["activities"]}
    dropoff_block = _activity_by_title(envelope, "Drop brother at tuition")
    pickup_block = _activity_by_title(envelope, "Pick up brother from tuition")
    lunch_block = _activity_by_title(envelope, "Birthday lunch")
    rest_block = _activity_by_title(envelope, "Rest")
    assert "Return home" not in titles
    assert envelope["schedule_constraints"]["return_home_deadline"] == parse_clock("08:00 PM")
    assert dropoff_block["timing_mode"] == TimingMode.WINDOW
    assert dropoff_block["startTime"] == "08:45 AM"
    assert dropoff_block["endTime"] == "09:00 AM"
    assert pickup_block["timing_mode"] == TimingMode.FIXED
    assert pickup_block["startTime"] == "11:00 AM"
    assert pickup_block["endTime"] == "11:15 AM"
    assert lunch_block["timing_mode"] == TimingMode.FIXED
    assert lunch_block["startTime"] == "01:00 PM"
    first_activity = next(block for block in envelope["schedule_blocks"] if block.get("block_type") == "activity")
    assert first_activity["title"] != "Rest"
    assert parse_clock(rest_block["startTime"]) > parse_clock(dropoff_block["endTime"])
    assert len([item for item in envelope["activities"] if item.get("title") != "Return home"]) == 7
    assert len(envelope.get("unfit_activities") or []) != 5


def test_semantic_validate_only_keeps_module_a_owner_but_consumes_dropoff_deadline(monkeypatch):
    monkeypatch.delenv("SEMANTIC_NORMALIZER_MODE", raising=False)
    monkeypatch.delenv("USE_SEMANTIC_NORMALIZER", raising=False)
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    request_text = "Drop my brother at tuition by 9:00 AM, then pick him up at 11:00 AM."
    parsed = {
        "intent": "add",
        "reply": "Draft created.",
        "transcription": request_text,
        "date": "2026-06-06",
        "preferences": {},
        "operations": [
            {
                "op": "add",
                "title": "Drop brother at tuition",
                "source_clause": "Drop my brother at tuition by 9:00 AM",
                "timing_mode": TimingMode.PREFERRED,
                "fixed_start": "09:00",
                "duration_minutes": 30,
                "location_kind": "category_only",
                "location_category": "study",
                "travel_required": True,
                "location_resolution_status": "needs_coordinates",
            },
            {
                "op": "add",
                "title": "Pick up brother",
                "source_clause": "pick him up at 11:00 AM",
                "timing_mode": TimingMode.FIXED,
                "fixed_start": "11:00",
                "duration_minutes": 15,
                "location_kind": "category_only",
                "location_category": "study",
                "travel_required": True,
                "location_resolution_status": "needs_coordinates",
            },
        ],
    }

    normalized = engine._normalize_parsed_locations(parsed, request_text)
    dropoff = next(item for item in normalized["operations"] if item["title"] == "Drop brother at tuition")
    pickup = next(item for item in normalized["operations"] if item["title"] == "Pick up brother")
    envelope = engine.build_schedule_response(normalized, None, request_text)["schedule_data"]
    dropoff_block = _activity_by_title(envelope, "Drop brother at tuition")

    assert dropoff["semantic_constraint_type"] == "dropoff"
    assert dropoff["service_kind"] == "dropoff"
    assert dropoff["latest_end"] == parse_clock("09:00 AM")
    assert dropoff["duration_minutes"] == 15
    assert parse_clock(pickup["fixed_start"]) == parse_clock("11:00 AM")
    assert dropoff_block["startTime"] == "08:45 AM"
    assert dropoff_block["endTime"] == "09:00 AM"


def test_semantic_normalizer_validate_only_keeps_module_a_lunch_time(monkeypatch, capsys):
    monkeypatch.delenv("SEMANTIC_NORMALIZER_MODE", raising=False)
    monkeypatch.delenv("USE_SEMANTIC_NORMALIZER", raising=False)
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    request_text = (
        "Plan my day for next Tuesday. I have a doctor appointment from 9:00 AM to 10:00 AM "
        "at Sunway Medical, a lunch meeting at 12:30 PM in SS15, and a dinner with family "
        "at 7:30 PM at home."
    )
    parsed = {
        "intent": "add",
        "transcription": request_text,
        "date": "2026-06-09",
        "operations": [
            {"op": "add", "title": "Doctor appointment", "timing_mode": TimingMode.FIXED, "fixed_start": "09:00", "fixed_end": "10:00", "duration_minutes": 60, "source_clause": request_text, "location": "Sunway Medical", "location_kind": "exact_named_place", "location_category": "medical", "travel_required": True, "location_resolution_status": "needs_coordinates"},
            {"op": "add", "title": "Lunch meeting", "timing_mode": TimingMode.FIXED, "fixed_start": "12:30", "duration_minutes": 60, "source_clause": request_text, "location": "SS15", "location_kind": "area_only", "location_category": "meal_place", "travel_required": True, "location_resolution_status": "needs_coordinates"},
            {"op": "add", "title": "Dinner with family", "timing_mode": TimingMode.FIXED, "fixed_start": "19:30", "duration_minutes": 60, "source_clause": request_text, "location_kind": "home", "location_category": "home", "travel_required": True, "location_resolution_status": "needs_coordinates"},
        ],
    }

    normalized = engine._normalize_parsed_locations(parsed, request_text)
    lunch = next(item for item in normalized["operations"] if item["title"] == "Lunch meeting")
    captured = capsys.readouterr().out

    assert lunch["timing_mode"] == TimingMode.FIXED
    assert parse_clock(lunch["fixed_start"]) == parse_clock("12:30 PM")
    assert parse_clock(lunch.get("fixed_end")) in {None, parse_clock("01:30 PM")}
    assert lunch.get("semantic_constraint_type") != "exact_anchor"
    assert any("Lunch meeting" in issue and "ambiguous" in issue for issue in normalized.get("validation_issues", []))
    assert "[JPLAN][SEMANTIC_CONSTRAINT][VALIDATE_ONLY]" in captured


def test_semantic_normalizer_overwrite_mode_reproduces_legacy_exact_anchor(monkeypatch):
    monkeypatch.setenv("SEMANTIC_NORMALIZER_MODE", "overwrite")
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    request_text = (
        "Plan my day. I have a doctor appointment from 9:00 AM to 10:00 AM, "
        "a lunch meeting at 12:30 PM, and dinner at 7:30 PM."
    )
    parsed = {
        "intent": "add",
        "transcription": request_text,
        "date": "2026-06-09",
        "operations": [
            {"op": "add", "title": "Lunch meeting", "timing_mode": TimingMode.FIXED, "fixed_start": "12:30", "duration_minutes": 60, "source_clause": "a lunch meeting at 12:30 PM"},
        ],
    }

    normalized = engine._normalize_parsed_locations(parsed, request_text)
    lunch = normalized["operations"][0]

    assert lunch["semantic_constraint_type"] == "exact_anchor"
    assert lunch["timing_mode"] == TimingMode.FIXED
    assert lunch["fixed_start"] == parse_clock("12:30 PM")


def test_use_semantic_normalizer_false_forces_validate_only(monkeypatch):
    monkeypatch.setenv("SEMANTIC_NORMALIZER_MODE", "overwrite")
    monkeypatch.setenv("USE_SEMANTIC_NORMALIZER", "false")
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    parsed = {
        "intent": "add",
        "transcription": "Lunch meeting at 12:30 PM.",
        "date": "2026-06-09",
        "operations": [
            {"op": "add", "title": "Lunch meeting", "timing_mode": TimingMode.UNSPECIFIED, "duration_minutes": 60, "source_clause": "Lunch meeting at 12:30 PM."},
        ],
    }

    normalized = engine._normalize_parsed_locations(parsed, parsed["transcription"])
    lunch = normalized["operations"][0]

    assert lunch["timing_mode"] == TimingMode.UNSPECIFIED
    assert lunch.get("fixed_start") is None
    assert lunch.get("semantic_constraint_type") is None


def test_semantic_constraint_normalizer_requires_evidence_before_rewrite():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    request_text = "Plan time for lunch and pickup research sometime this weekend."
    parsed = {
        "intent": "add",
        "transcription": request_text,
        "date": "2026-06-06",
        "operations": [
            {"op": "add", "title": "Pickup research", "timing_mode": TimingMode.UNSPECIFIED, "duration_minutes": 60},
            {"op": "add", "title": "Lunch", "timing_mode": TimingMode.UNSPECIFIED, "duration_minutes": 60},
        ],
    }

    normalized = engine._normalize_parsed_locations(parsed, request_text)
    pickup = next(item for item in normalized["operations"] if item["title"] == "Pickup research")
    lunch = next(item for item in normalized["operations"] if item["title"] == "Lunch")

    assert pickup.get("service_kind") is None
    assert pickup["timing_mode"] != TimingMode.FIXED
    assert lunch["timing_mode"] != TimingMode.FIXED


def test_location_flexible_tasks_request_exact_location_when_travel_context_matters():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    request_text = (
        "Plan my Saturday. Drop my brother at tuition by 9:00 AM, pick him up at 11:00 AM, "
        "and fit in one hour of study between them."
    )
    parsed = {
        "intent": "add",
        "transcription": request_text,
        "date": "2026-06-06",
        "preferences": {"accurate_travel_time": True, "travel_intent": True},
        "operations": [
            {"op": "add", "title": "Drop brother at tuition", "timing_mode": TimingMode.PREFERRED, "fixed_start": "09:00", "duration_minutes": 30, "location": "Tuition centre", "location_label": "Tuition centre", "location_kind": "exact_named_place", "location_category": "study", "location_status": "resolved", "location_resolution_status": "resolved_coordinates", "explicit_user_location": True, "travel_required": True, "resolved_location": {"latitude": 3.0, "longitude": 101.0}},
            {"op": "add", "title": "Study", "timing_mode": TimingMode.UNSPECIFIED, "duration_minutes": 60, "location_kind": "no_location_required", "location_category": "no_location", "travel_required": False, "location_resolution_status": "not_required"},
            {"op": "add", "title": "Pick up brother from tuition", "timing_mode": TimingMode.PREFERRED, "fixed_start": "11:00", "duration_minutes": 30, "location": "Tuition centre", "location_label": "Tuition centre", "location_kind": "exact_named_place", "location_category": "study", "location_status": "resolved", "location_resolution_status": "resolved_coordinates", "explicit_user_location": True, "travel_required": True, "resolved_location": {"latitude": 3.0, "longitude": 101.0}},
        ],
    }

    envelope = engine.build_schedule_response(parsed, None, request_text, saved_locations=[])["schedule_data"]
    requests = envelope["location_resolution_requests"]
    requested_titles = {request["title"] for request in requests}

    assert "Study" in requested_titles
    study = _activity_by_title(envelope, "Study")
    assert study["location_flexible"] is True
    assert study["can_be_done_at_current_location"] is True
    assert study["travel_context_required"] is True


def test_location_flexible_tasks_do_not_request_exact_location_without_travel_context():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    request_text = "Plan one hour of study sometime Saturday."
    parsed = {
        "intent": "add",
        "transcription": request_text,
        "date": "2026-06-06",
        "preferences": {"accurate_travel_time": False, "travel_intent": False},
        "operations": [
            {"op": "add", "title": "Study", "timing_mode": TimingMode.UNSPECIFIED, "duration_minutes": 60, "location_kind": "no_location_required", "location_category": "no_location", "travel_required": False, "location_resolution_status": "not_required"},
        ],
    }

    envelope = engine.build_schedule_response(parsed, None, request_text, saved_locations=[])["schedule_data"]
    requests = envelope["location_resolution_requests"]
    requested_titles = {request["title"] for request in requests}

    assert "Study" not in requested_titles


def test_location_flexible_selected_home_location_displays_without_route_endpoint():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    activities = [
        {
            "id": "study",
            "stable_activity_id": "study",
            "title": "Study",
            "scheduled_start": parse_clock("09:00 AM"),
            "scheduled_end": parse_clock("10:00 AM"),
            "duration_minutes": 60,
            "location": "Home",
            "location_label": "Home",
            "location_category": "unknown",
            "location_status": "resolved",
            "location_flexible": True,
            "travel_context_required": True,
            "travel_required": False,
            "resolved_location": {
                "display_name": "Home",
                "category": "home",
                "latitude": 2.9,
                "longitude": 101.6,
            },
        }
    ]

    blocks = engine._materialize_blocks(activities, parse_clock("08:00 AM"), None)
    study = next(block for block in blocks if block.get("title") == "Study")

    assert study["location"] == "Home"
    assert study["location_label"] == "Home"
    assert study["travel_required"] is False
    assert not any(block.get("block_type") == "transition" for block in blocks)


def test_location_flexible_selected_physical_location_participates_in_routing():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService(route_minutes=9))
    study = {
        "id": "study",
        "stable_activity_id": "study",
        "title": "Study",
        "scheduled_start": parse_clock("09:00 AM"),
        "scheduled_end": parse_clock("10:00 AM"),
        "duration_minutes": 60,
        "location": "Quiet Library",
        "location_label": "Quiet Library",
        "location_category": "unknown",
        "location_status": "resolved",
        "location_flexible": True,
        "travel_context_required": True,
        "travel_required": False,
        "resolved_location": {
            "display_name": "Quiet Library",
            "category": "library",
            "latitude": 3.0,
            "longitude": 101.7,
        },
    }

    assert engine._activity_requires_travel(study) is True
    context = engine._build_route_context(
        {"date": "2026-06-06", "preferences": _accurate_preferences()},
        [study],
        saved_locations=[],
    )

    assert "id:study" in context["nodes"]
    assert context["nodes"]["id:study"]["location"] == "Quiet Library"


def test_post_repair_backfill_moves_late_errand_into_morning_free_slot():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService(route_minutes=5))
    locations = [
        _default_start_location(),
        {"label": "Campus", "display_name": "Campus", "address": "Campus", "latitude": 3.00, "longitude": 101.60},
        {"label": "Laundry", "display_name": "Laundry", "address": "Laundry", "latitude": 3.01, "longitude": 101.61},
    ]
    repaired = {
        "date": "2026-05-02",
        "preferences": _accurate_preferences(day_start="07:00", day_end="22:00"),
        "activities": [
            {
                "id": "class",
                "stable_activity_id": "class",
                "title": "Morning class",
                "scheduled_start": parse_clock("10:00"),
                "scheduled_end": parse_clock("12:00"),
                "duration_minutes": 120,
                "timing_mode": TimingMode.FIXED,
                "fixed_start": parse_clock("10:00"),
                "fixed_end": parse_clock("12:00"),
                "is_user_fixed": True,
                "can_move_for_repair": False,
                "repair_protection": "fixed",
                "location": "Campus",
                "location_label": "Campus",
                "location_category": "school",
                "location_status": "resolved",
                "resolved_location": locations[1],
                "travel_required": True,
                "status": "active",
            },
            {
                "id": "fyp",
                "stable_activity_id": "fyp",
                "title": "FYP Work",
                "scheduled_start": parse_clock("12:30"),
                "scheduled_end": parse_clock("15:30"),
                "duration_minutes": 180,
                "timing_mode": TimingMode.UNSPECIFIED,
                "can_move_for_repair": True,
                "repair_protection": "flexible",
                "location_status": "not_required",
                "location_category": "home_or_online",
                "travel_required": False,
                "status": "active",
            },
            {
                "id": "laundry",
                "stable_activity_id": "laundry",
                "title": "Laundry",
                "scheduled_start": parse_clock("20:00"),
                "scheduled_end": parse_clock("21:00"),
                "duration_minutes": 60,
                "timing_mode": TimingMode.UNSPECIFIED,
                "can_move_for_repair": True,
                "repair_protection": "flexible",
                "location": "Laundry",
                "location_label": "Laundry",
                "location_category": "laundry",
                "location_status": "resolved",
                "resolved_location": locations[2],
                "travel_required": True,
                "status": "active",
            },
        ],
        "schedule_blocks": [
            {"block_type": "idle", "title": "Free Time", "start": "07:00 AM", "end": "10:00 AM", "duration_minutes": 180},
            {"block_type": "activity", "id": "class", "stable_activity_id": "class", "title": "Morning class", "start": "10:00 AM", "end": "12:00 PM", "startTime": "10:00 AM", "endTime": "12:00 PM", "location": "Campus", "location_label": "Campus", "location_category": "school", "location_status": "resolved", "resolved_location": locations[1], "travel_required": True, "timing_mode": TimingMode.FIXED, "fixed_start": parse_clock("10:00"), "fixed_end": parse_clock("12:00"), "is_user_fixed": True, "can_move_for_repair": False, "repair_protection": "fixed"},
            {"block_type": "activity", "id": "fyp", "stable_activity_id": "fyp", "title": "FYP Work", "start": "12:30 PM", "end": "03:30 PM", "startTime": "12:30 PM", "endTime": "03:30 PM", "duration_minutes": 180, "location_status": "not_required", "location_category": "home_or_online", "travel_required": False, "can_move_for_repair": True, "repair_protection": "flexible"},
            {"block_type": "idle", "title": "Free Time", "start": "03:30 PM", "end": "08:00 PM", "duration_minutes": 270},
            {"block_type": "activity", "id": "laundry", "stable_activity_id": "laundry", "title": "Laundry", "start": "08:00 PM", "end": "09:00 PM", "startTime": "08:00 PM", "endTime": "09:00 PM", "duration_minutes": 60, "location": "Laundry", "location_label": "Laundry", "location_category": "laundry", "location_status": "resolved", "resolved_location": locations[2], "travel_required": True, "can_move_for_repair": True, "repair_protection": "flexible"},
        ],
    }
    route_context = engine._build_route_context(repaired, repaired["activities"], locations)
    validation = engine._final_validate_route_aware_repair(
        original=repaired,
        repaired=repaired,
        route_context=route_context,
    )

    result = engine._post_repair_flexible_backfill(
        original=repaired,
        repaired=repaired,
        validation=validation,
        route_context=route_context,
    )

    assert result is not None
    laundry = _activity_by_title(result["repaired"], "Laundry")
    assert laundry["scheduled_start"] < parse_clock("10:00")
    fixed_class = _activity_by_title(result["repaired"], "Morning class")
    assert fixed_class["scheduled_start"] == parse_clock("10:00")
    assert result["validation"]["travel_validation_status"] in {"validated", "fallback_used"}


def test_post_repair_backfill_rejects_later_move(capsys):
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService(route_minutes=5))
    current = {
        "date": "2026-05-02",
        "preferences": _accurate_preferences(day_start="07:00", day_end="22:00"),
        "activities": [
            {
                "id": "laundry",
                "stable_activity_id": "laundry",
                "title": "Laundry",
                "scheduled_start": parse_clock("08:00 PM"),
                "scheduled_end": parse_clock("09:00 PM"),
                "duration_minutes": 60,
                "timing_mode": TimingMode.UNSPECIFIED,
                "can_move_for_repair": True,
                "repair_protection": "flexible",
                "location_category": "laundry",
                "location_status": "not_required",
                "travel_required": False,
                "status": "active",
            },
        ],
        "schedule_blocks": [
            {"block_type": "activity", "id": "laundry", "stable_activity_id": "laundry", "title": "Laundry", "start": "08:00 PM", "end": "09:00 PM", "startTime": "08:00 PM", "endTime": "09:00 PM", "duration_minutes": 60, "location_category": "laundry", "location_status": "not_required", "travel_required": False},
            {"block_type": "idle", "title": "Free Time", "start": "09:00 PM", "end": "10:00 PM", "duration_minutes": 60},
        ],
    }
    validation = {"schedule_blocks": current["schedule_blocks"], "travel_validation_status": "validated"}

    move = engine._best_post_repair_backfill_move(
        current=current,
        original=current,
        validation=validation,
        route_context={"nodes": {}, "routes": {}, "start_routes": {}},
        current_travel_total=0,
        moved_activity_ids=set(),
    )

    assert move is None
    assert "reason=not_earlier_backfill" in capsys.readouterr().out


def test_preference_rescue_moves_high_weight_window_into_free_slot(capsys):
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService(route_minutes=5))
    locations = [
        {"label": "Home", "display_name": "Home", "latitude": 2.88, "longitude": 101.58},
        {"label": "Office", "display_name": "Office", "latitude": 2.90, "longitude": 101.60},
        {"label": "Bank", "display_name": "Bank", "latitude": 2.91, "longitude": 101.61},
        {"label": "Tamarind", "display_name": "Tamarind", "latitude": 2.92, "longitude": 101.62},
    ]
    repaired = {
        "date": "2026-05-02",
        "preferences": _accurate_preferences(day_start="07:00", day_end="22:00"),
        "activities": [
            {
                "id": "presentation",
                "stable_activity_id": "presentation",
                "title": "Client presentation",
                "scheduled_start": parse_clock("11:00 AM"),
                "scheduled_end": parse_clock("12:00 PM"),
                "duration_minutes": 60,
                "timing_mode": TimingMode.FIXED,
                "fixed_start": parse_clock("11:00 AM"),
                "fixed_end": parse_clock("12:00 PM"),
                "is_user_fixed": True,
                "can_move_for_repair": False,
                "repair_protection": "fixed",
                "location": "Office",
                "location_label": "Office",
                "location_status": "resolved",
                "resolved_location": locations[1],
                "travel_required": True,
                "status": "active",
            },
            {
                "id": "bank",
                "stable_activity_id": "bank",
                "title": "Bank visit",
                "scheduled_start": parse_clock("12:39 PM"),
                "scheduled_end": parse_clock("01:24 PM"),
                "duration_minutes": 45,
                "timing_mode": TimingMode.UNSPECIFIED,
                "can_move_for_repair": True,
                "repair_protection": "flexible",
                "location": "Bank",
                "location_label": "Bank",
                "location_status": "resolved",
                "resolved_location": locations[2],
                "travel_required": True,
                "status": "active",
            },
            {
                "id": "lunch",
                "stable_activity_id": "lunch",
                "title": "Lunch",
                "scheduled_start": parse_clock("02:24 PM"),
                "scheduled_end": parse_clock("03:24 PM"),
                "duration_minutes": 60,
                "timing_mode": TimingMode.UNSPECIFIED,
                "preferred_time_window": "lunch",
                "preferred_window_start": parse_clock("12:00 PM"),
                "preferred_window_end": parse_clock("02:00 PM"),
                "preference_weight": "high",
                "can_move_for_repair": True,
                "repair_protection": "flexible",
                "location": "Tamarind",
                "location_label": "Tamarind",
                "location_status": "resolved",
                "resolved_location": locations[3],
                "travel_required": True,
                "status": "active",
            },
        ],
        "schedule_blocks": [
            {"block_type": "activity", "id": "presentation", "stable_activity_id": "presentation", "title": "Client presentation", "start": "11:00 AM", "end": "12:00 PM", "startTime": "11:00 AM", "endTime": "12:00 PM", "location": "Office", "location_label": "Office", "location_status": "resolved", "resolved_location": locations[1], "travel_required": True, "timing_mode": TimingMode.FIXED, "fixed_start": parse_clock("11:00 AM"), "fixed_end": parse_clock("12:00 PM"), "is_user_fixed": True, "can_move_for_repair": False, "repair_protection": "fixed"},
            {"block_type": "activity", "id": "bank", "stable_activity_id": "bank", "title": "Bank visit", "start": "12:39 PM", "end": "01:24 PM", "startTime": "12:39 PM", "endTime": "01:24 PM", "duration_minutes": 45, "location": "Bank", "location_label": "Bank", "location_status": "resolved", "resolved_location": locations[2], "travel_required": True, "can_move_for_repair": True, "repair_protection": "flexible"},
            {"block_type": "idle", "title": "Free Time", "start": "01:24 PM", "end": "02:14 PM", "duration_minutes": 50},
            {"block_type": "activity", "id": "lunch", "stable_activity_id": "lunch", "title": "Lunch", "start": "02:24 PM", "end": "03:24 PM", "startTime": "02:24 PM", "endTime": "03:24 PM", "duration_minutes": 60, "preferred_time_window": "lunch", "preferred_window_start": parse_clock("12:00 PM"), "preferred_window_end": parse_clock("02:00 PM"), "preference_weight": "high", "location": "Tamarind", "location_label": "Tamarind", "location_status": "resolved", "resolved_location": locations[3], "travel_required": True, "can_move_for_repair": True, "repair_protection": "flexible"},
        ],
    }
    route_context = engine._build_route_context(repaired, repaired["activities"], locations)
    validation = engine._final_validate_route_aware_repair(
        original=repaired,
        repaired=repaired,
        route_context=route_context,
    )

    result = engine._post_repair_preference_rescue(
        original=repaired,
        repaired=repaired,
        validation=validation,
        route_context=route_context,
    )

    assert result is not None
    lunch = _activity_by_title(result["repaired"], "Lunch")
    assert lunch["scheduled_start"] < parse_clock("02:24 PM")
    assert "PREF_RESCUE][FREE_SLOT_SCAN" in capsys.readouterr().out


def test_repair_records_preserve_preference_metadata_from_activities():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService(route_minutes=5))
    envelope = {
        "activities": [
            {
                "id": "lunch",
                "stable_activity_id": "lunch",
                "title": "Lunch",
                "duration_minutes": 60,
                "scheduled_start": parse_clock("12:00 PM"),
                "scheduled_end": parse_clock("01:00 PM"),
                "preferred_time_window": "lunch",
                "preferred_window_start": parse_clock("12:00 PM"),
                "preferred_window_end": parse_clock("02:00 PM"),
                "preference_weight": "high",
                "status": "active",
            }
        ],
        "schedule_blocks": [
            {
                "block_type": "activity",
                "id": "lunch",
                "stable_activity_id": "lunch",
                "title": "Lunch",
                "start": "02:24 PM",
                "end": "03:24 PM",
                "startTime": "02:24 PM",
                "endTime": "03:24 PM",
                "duration_minutes": 60,
            }
        ],
    }

    records = engine._activity_records_for_repair(envelope)

    assert len(records) == 1
    assert records[0]["_repair_start"] == parse_clock("02:24 PM")
    assert records[0]["preferred_time_window"] == "lunch"
    assert records[0]["preference_weight"] == "high"


def test_preference_rescue_reorders_same_location_lower_priority_blocker(capsys):
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService(route_minutes=5))
    locations = [
        {"label": "Home", "display_name": "Home", "latitude": 2.88, "longitude": 101.58},
        {"label": "Bank", "display_name": "Bank", "latitude": 2.91, "longitude": 101.61},
        {"label": "Tamarind", "display_name": "Tamarind", "latitude": 2.92, "longitude": 101.62},
    ]
    repaired = {
        "date": "2026-05-02",
        "preferences": _accurate_preferences(day_start="07:00", day_end="22:00"),
        "activities": [
            {
                "id": "bank",
                "stable_activity_id": "bank",
                "title": "Bank visit",
                "scheduled_start": parse_clock("12:39 PM"),
                "scheduled_end": parse_clock("01:24 PM"),
                "duration_minutes": 45,
                "timing_mode": TimingMode.UNSPECIFIED,
                "can_move_for_repair": True,
                "repair_protection": "flexible",
                "location": "Bank",
                "location_label": "Bank",
                "location_status": "resolved",
                "resolved_location": locations[1],
                "travel_required": True,
                "status": "active",
            },
            {
                "id": "grocery",
                "stable_activity_id": "grocery",
                "title": "Grocery shopping",
                "scheduled_start": parse_clock("01:34 PM"),
                "scheduled_end": parse_clock("02:19 PM"),
                "duration_minutes": 45,
                "timing_mode": TimingMode.UNSPECIFIED,
                "can_move_for_repair": True,
                "repair_protection": "flexible",
                "location": "Tamarind",
                "location_label": "Tamarind",
                "location_status": "resolved",
                "resolved_location": locations[2],
                "travel_required": True,
                "status": "active",
            },
            {
                "id": "lunch",
                "stable_activity_id": "lunch",
                "title": "Lunch",
                "scheduled_start": parse_clock("02:24 PM"),
                "scheduled_end": parse_clock("03:24 PM"),
                "duration_minutes": 60,
                "timing_mode": TimingMode.UNSPECIFIED,
                "preferred_time_window": "lunch",
                "preferred_window_start": parse_clock("12:00 PM"),
                "preferred_window_end": parse_clock("02:00 PM"),
                "preference_weight": "high",
                "can_move_for_repair": True,
                "repair_protection": "flexible",
                "location": "Tamarind",
                "location_label": "Tamarind",
                "location_status": "resolved",
                "resolved_location": locations[2],
                "travel_required": True,
                "status": "active",
            },
        ],
    }
    repaired["schedule_blocks"] = engine._materialize_blocks(repaired["activities"], parse_clock("07:00 AM"), 0)
    route_context = engine._build_route_context(repaired, repaired["activities"], locations)
    validation = engine._final_validate_route_aware_repair(
        original=repaired,
        repaired=repaired,
        route_context=route_context,
    )

    result = engine._post_repair_preference_rescue(
        original=repaired,
        repaired=repaired,
        validation=validation,
        route_context=route_context,
    )

    assert result is not None
    lunch = _activity_by_title(result["repaired"], "Lunch")
    grocery = _activity_by_title(result["repaired"], "Grocery shopping")
    assert lunch["scheduled_start"] == parse_clock("01:34 PM")
    assert grocery["scheduled_start"] == lunch["scheduled_end"]
    logs = capsys.readouterr().out
    assert "PREF_RESCUE][BLOCKER_SCAN" in logs
    assert "PREF_RESCUE][REORDER_CANDIDATE" in logs
    assert "reason=reorder_same_location_blocker" in logs


def test_module_d_score_prefers_high_weight_window_candidate():
    engine = SchedulingEngine(DummyClient())
    early = [
        {
            "id": "lunch",
            "stable_activity_id": "lunch",
            "title": "Lunch",
            "scheduled_start": parse_clock("12:30 PM"),
            "scheduled_end": parse_clock("01:30 PM"),
            "duration_minutes": 60,
            "timing_mode": TimingMode.UNSPECIFIED,
            "preferred_time_window": "lunch",
            "preferred_window_start": parse_clock("12:00 PM"),
            "preferred_window_end": parse_clock("02:00 PM"),
            "preference_weight": "high",
            "can_move_for_repair": True,
            "repair_protection": "flexible",
            "status": "active",
        }
    ]
    late = [dict(early[0], scheduled_start=parse_clock("02:24 PM"), scheduled_end=parse_clock("03:24 PM"))]

    assert engine._module_d_score(early, [], parse_clock("07:00 AM"), parse_clock("10:00 PM"), 0) > engine._module_d_score(
        late,
        [],
        parse_clock("07:00 AM"),
        parse_clock("10:00 PM"),
        0,
    )


def test_route_guard_penalizes_flex_zigzag_between_fixed_anchors():
    engine = SchedulingEngine(DummyClient())
    standup = {
        "id": "standup",
        "stable_activity_id": "standup",
        "title": "Team stand-up",
        "timing_mode": TimingMode.FIXED,
        "fixed_start": parse_clock("08:30 AM"),
        "scheduled_start": parse_clock("08:30 AM"),
        "scheduled_end": parse_clock("09:00 AM"),
        "location": "TRX",
        "travel_required": True,
    }
    bank = {
        "id": "bank",
        "stable_activity_id": "bank",
        "title": "Bank visit",
        "timing_mode": TimingMode.UNSPECIFIED,
        "scheduled_start": parse_clock("09:30 AM"),
        "scheduled_end": parse_clock("10:00 AM"),
        "location": "Cyberjaya",
        "travel_required": True,
    }
    client = {
        "id": "client",
        "stable_activity_id": "client",
        "title": "Client presentation",
        "timing_mode": TimingMode.FIXED,
        "fixed_start": parse_clock("11:00 AM"),
        "scheduled_start": parse_clock("11:00 AM"),
        "scheduled_end": parse_clock("12:00 PM"),
        "location": "Bangsar",
        "travel_required": True,
    }
    engine._current_route_context = {
        "enabled": True,
        "pairs": {
            "standup->client": {"duration_minutes": 18},
            "standup->bank": {"duration_minutes": 37},
            "bank->client": {"duration_minutes": 35},
        },
    }

    assert engine._route_candidate_practicality_adjustment(bank, standup, client) < -400


def test_travel_validation_logs_perf_summary(capsys):
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    envelope = {"preferences": {}, "schedule_blocks": [], "activities": []}

    engine._apply_accurate_travel_if_requested(envelope, [])

    logs = capsys.readouterr().out
    assert "[JPLAN][TIMER] route_matrix_seconds=" in logs
    assert "[JPLAN][TIMER] route_fetch_seconds=" in logs
    assert "[JPLAN][PERF][ROUTE_API_CALLS] count=" in logs
    assert "[JPLAN][PERF][ROUTE_CACHE] hits=" in logs


def test_route_context_uses_near_location_shortcut_without_route_api(capsys):
    service = FakeTravelService(route_minutes=42)
    engine = SchedulingEngine(DummyClient(), travel_service=service)
    standup = {
        "id": "standup",
        "stable_activity_id": "standup",
        "title": "Team stand-up",
        "location": "TRX Project Office",
        "location_label": "TRX Project Office",
        "location_kind": "exact_named_place",
        "location_status": "resolved",
        "travel_required": True,
        "resolved_location": {"display_name": "TRX Project Office", "latitude": 3.142, "longitude": 101.721},
        "status": "active",
    }
    coffee = {
        "id": "coffee",
        "stable_activity_id": "coffee",
        "title": "Coffee break",
        "location": "Tun Razak Exchange",
        "location_label": "Tun Razak Exchange",
        "location_kind": "exact_named_place",
        "location_status": "resolved",
        "travel_required": True,
        "resolved_location": {"display_name": "Tun Razak Exchange", "latitude": 3.1423, "longitude": 101.7212},
        "status": "active",
    }

    context = engine._build_route_context({"preferences": {}}, [standup, coffee], [])

    pair = context["pairs"]["id:standup->id:coffee"]
    assert pair["source"] == "near_location"
    assert pair["duration_minutes"] in {3, 5}
    assert len(context["physical_nodes"]) == 1
    assert service.route_calls == 0
    assert "NEAR_LOCATION_GROUP" in capsys.readouterr().out


def test_route_context_dedupes_physical_nodes_and_expands_activity_pairs():
    service = FakeTravelService(route_minutes=17)
    engine = SchedulingEngine(DummyClient(), travel_service=service)
    lunch = {
        "id": "lunch",
        "stable_activity_id": "lunch",
        "title": "Lunch",
        "location": "Tamarind",
        "location_label": "Tamarind",
        "location_status": "resolved",
        "travel_required": True,
        "resolved_location": {"display_name": "Tamarind", "latitude": 3.1, "longitude": 101.6},
        "status": "active",
    }
    grocery = {
        "id": "grocery",
        "stable_activity_id": "grocery",
        "title": "Grocery shopping",
        "location": "Tamarind",
        "location_label": "Tamarind",
        "location_status": "resolved",
        "travel_required": True,
        "resolved_location": {"display_name": "Tamarind", "latitude": 3.1, "longitude": 101.6},
        "status": "active",
    }
    client = {
        "id": "client",
        "stable_activity_id": "client",
        "title": "Client presentation",
        "location": "Bangsar",
        "location_label": "Bangsar",
        "location_status": "resolved",
        "travel_required": True,
        "resolved_location": {"display_name": "Bangsar", "latitude": 3.13, "longitude": 101.67},
        "status": "active",
    }

    context = engine._build_route_context({"preferences": {}}, [lunch, grocery, client], [])

    assert len(context["physical_nodes"]) == 2
    assert service.route_calls == 2
    assert context["pairs"]["id:lunch->id:grocery"]["source"] == "same_location"
    assert context["pairs"]["id:grocery->id:client"]["duration_minutes"] == 17
    assert context["pairs"]["id:lunch->id:client"]["duration_minutes"] == 17


def test_optional_route_guard_keeps_short_flex_near_anchor_and_penalizes_detour():
    engine = SchedulingEngine(DummyClient())
    standup = {
        "id": "standup",
        "stable_activity_id": "standup",
        "title": "Team stand-up",
        "timing_mode": TimingMode.FIXED,
        "scheduled_start": parse_clock("08:30 AM"),
        "scheduled_end": parse_clock("09:00 AM"),
        "location": "TRX Project Office",
        "travel_required": True,
    }
    coffee = {
        "id": "coffee",
        "stable_activity_id": "coffee",
        "title": "Coffee break",
        "duration_minutes": 15,
        "priority": "low",
        "preference_weight": "low",
        "location": "TRX",
        "travel_required": True,
    }
    client = {
        "id": "client",
        "stable_activity_id": "client",
        "title": "Client presentation",
        "scheduled_start": parse_clock("11:00 AM"),
        "scheduled_end": parse_clock("12:00 PM"),
        "location": "Bangsar",
        "travel_required": True,
    }
    engine._current_route_context = {
        "enabled": True,
        "pairs": {
            "id:standup->id:coffee": {"duration_minutes": 3, "source": "near_location"},
            "id:coffee->id:client": {"duration_minutes": 13, "source": "routing_service"},
            "id:standup->id:client": {"duration_minutes": 13, "source": "routing_service"},
            "id:client->id:coffee": {"duration_minutes": 38, "source": "routing_service"},
        },
    }

    assert engine._optional_route_cost_adjustment(coffee, standup, client) > 0
    engine._current_route_context = {
        "enabled": True,
        "pairs": {
            "id:standup->id:coffee": {"duration_minutes": 6, "source": "route_cache"},
            "id:coffee->id:client": {"duration_minutes": 23, "source": "route_cache"},
            "id:standup->id:client": {"duration_minutes": 18, "source": "route_cache"},
        },
    }
    assert engine._optional_route_cost_adjustment(coffee, standup, client) > 0
    assert engine._optional_route_cost_adjustment(coffee, client, None) < 0


def test_module_d_route_total_guard_rejects_low_weight_total_travel_increase():
    engine = SchedulingEngine(DummyClient())
    coffee = {
        "id": "coffee",
        "stable_activity_id": "coffee",
        "title": "Coffee break",
        "duration_minutes": 15,
        "priority": "low",
        "preference_weight": "low",
        "travel_required": True,
    }
    engine._current_route_context = {"enabled": True, "pairs": {}}

    rejected, reason = engine._module_d_route_total_guard_reject(
        {"type": "relocate", "activity_id": "coffee", "title": "Coffee break"},
        [coffee],
        route_delta=25,
    )

    assert rejected is True
    assert reason == "low_weight_total_route_worse"
    assert engine._module_d_route_total_guard_reject(
        {"type": "relocate", "activity_id": "coffee", "title": "Coffee break"},
        [coffee],
        route_delta=5,
    ) == (False, "")


def test_in_window_lunch_does_not_log_violation_allowed(capsys):
    engine = SchedulingEngine(DummyClient())
    repaired = {
        "activities": [
            {
                "id": "lunch",
                "stable_activity_id": "lunch",
                "title": "Lunch",
                "scheduled_start": parse_clock("01:19 PM"),
                "scheduled_end": parse_clock("02:19 PM"),
                "duration_minutes": 60,
                "preferred_time_window": "lunch",
                "preferred_window_start": parse_clock("12:00 PM"),
                "preferred_window_end": parse_clock("02:00 PM"),
                "preference_weight": "high",
            }
        ],
        "preference_rescue_attempts": {},
    }

    engine._log_preference_window_violations(repaired)

    assert "VIOLATION_ALLOWED" not in capsys.readouterr().out


def test_return_home_deadline_conflict_is_enforced_after_travel_validation():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    envelope = {
        "preferences": {},
        "schedule_constraints": {"return_home_deadline": parse_clock("08:00 PM")},
        "schedule_blocks": [
            {
                "block_type": "activity",
                "title": "Buy gift",
                "start": "07:50 PM",
                "end": "08:00 PM",
                "location": "Far Mall",
                "location_label": "Far Mall",
                "location_category": "shopping",
                "travel_required": True,
            }
        ],
    }

    conflict = engine._return_home_deadline_conflict(envelope)

    assert conflict is not None
    assert conflict["reason_code"] == "return_home_deadline_conflict"


def test_return_home_deadline_constraint_is_not_counted_as_missing_activity():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    original = [
        {"id": "gift", "stable_activity_id": "gift", "title": "Buy gift", "duration_minutes": 30, "status": "active"},
        {
            "id": "return-home",
            "stable_activity_id": "return-home",
            "title": "Return home",
            "semantic_constraint_type": "return_home_deadline",
            "is_schedule_constraint": True,
            "status": "active",
        },
    ]
    repaired = {
        "activities": [
            {"id": "gift", "stable_activity_id": "gift", "title": "Buy gift", "scheduled_start": parse_clock("04:00 PM"), "scheduled_end": parse_clock("04:30 PM")},
        ],
        "schedule_blocks": [],
    }

    accounting = engine._account_for_route_repair_activities(original, repaired, [], [])

    assert accounting["unfit_activities"] == []
    assert accounting["optional_skipped"] == []
    assert accounting["missing_after_accounting"] == []


def test_route_repair_accounting_marks_missing_lunch_unfit_and_optional_coffee_skipped():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    lunch = {
        "id": "lunch",
        "stable_activity_id": "lunch",
        "title": "Lunch",
        "duration_minutes": 60,
        "is_mandatory": True,
        "timing_mode": TimingMode.UNSPECIFIED,
    }
    coffee = {
        "id": "coffee",
        "stable_activity_id": "coffee",
        "title": "Coffee break",
        "duration_minutes": 15,
        "is_mandatory": False,
        "optional_reason": "if_possible",
        "priority": "low",
        "timing_mode": TimingMode.UNSPECIFIED,
    }
    repaired = {
        "activities": [],
        "schedule_blocks": [],
    }

    accounting = engine._account_for_route_repair_activities(
        [lunch, coffee],
        repaired,
        [],
        [],
    )

    assert accounting["unfit_activities"][0]["title"] == "Lunch"
    assert accounting["unfit_activities"][0]["reason_code"] == "missing_after_route_repair"
    assert accounting["optional_skipped"][0]["title"] == "Coffee break"
    assert accounting["missing_after_accounting"] == []


def test_route_repair_accounting_dedupes_preserved_unfit_and_counts_unique_originals(capsys):
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    scheduled = [
        {"id": f"act-{index}", "stable_activity_id": f"act-{index}", "title": f"Scheduled {index}", "status": "active"}
        for index in range(8)
    ]
    focused = {
        "id": "focus",
        "stable_activity_id": "focus",
        "activity_id": "focus",
        "title": "Focused deep work",
        "duration_minutes": 180,
        "reason": "Not enough route-safe time.",
        "reason_code": "not_enough_time_after_travel",
        "status": "active",
    }
    repaired = {
        "activities": scheduled,
        "schedule_blocks": [
            {"block_type": "activity", "id": item["id"], "stable_activity_id": item["stable_activity_id"], "title": item["title"]}
            for item in scheduled
        ],
    }

    accounting = engine._account_for_route_repair_activities(
        [*scheduled, focused],
        repaired,
        [focused, {**focused, "reason": "No feasible slot available."}],
        [],
    )
    logs = capsys.readouterr().out

    assert [item["title"] for item in accounting["unfit_activities"]] == ["Focused deep work"]
    assert accounting["duplicate_after_accounting"] == ["focus"]
    assert "original=9 scheduled=8 unfit=1 optional_skipped=0 explicitly_removed=0 missing=[] duplicates=['focus']" in logs
    assert "[JPLAN][ACCOUNTING][WARNING]" in logs


def test_preserve_existing_unfit_merges_duplicate_from_envelope(capsys):
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    focused = {
        "id": "focus",
        "stable_activity_id": "focus",
        "activity_id": "focus",
        "title": "Focused deep work",
        "duration_minutes": 180,
        "reason": "Not enough route-safe time.",
        "reason_code": "not_enough_time_after_travel",
    }
    merged = engine._preserve_existing_unfit_activities(
        {"unfit_activities": [{**focused, "reason": "Existing reason."}]},
        {"activities": [], "schedule_blocks": []},
        [focused],
    )
    logs = capsys.readouterr().out

    assert len(merged) == 1
    assert merged[0]["title"] == "Focused deep work"
    assert "[JPLAN][UNFIT][MERGE] title=Focused deep work reason=duplicate_existing_unfit" in logs
    assert "[JPLAN][UNFIT][PRESERVE] title=Focused deep work source=envelope deduped=true" in logs


def test_home_activity_with_saved_home_does_not_request_location_card():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    saved_home = _default_start_location()
    envelope = {
        "preferences": _accurate_preferences(),
        "activities": [
            {
                "id": "dinner",
                "stable_activity_id": "dinner",
                "title": "Dinner with family",
                "location": "home",
                "location_label": "home",
                "location_kind": "home",
                "location_category": "home",
                "location_status": "resolved",
                "location_resolution_status": "resolved_coordinates",
                "travel_required": True,
            }
        ],
        "schedule_blocks": [
            {
                "block_type": "activity",
                "id": "dinner",
                "stable_activity_id": "dinner",
                "title": "Dinner with family",
                "start": "07:30 PM",
                "end": "08:30 PM",
                "location": "home",
                "location_label": "home",
                "location_kind": "home",
                "location_category": "home",
                "location_status": "resolved",
                "location_resolution_status": "resolved_coordinates",
                "travel_required": True,
            }
        ],
    }

    requests = engine._location_resolution_requests(envelope, [saved_home], include_start_location=False)

    assert requests == []


def test_module_d_rejects_bank_move_before_business_hours():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    timeline = [
        {
            "id": "bank",
            "stable_activity_id": "bank",
            "title": "Bank visit",
            "scheduled_start": parse_clock("07:55 AM"),
            "scheduled_end": parse_clock("08:25 AM"),
            "earliest_start": parse_clock("09:00 AM"),
            "latest_end": parse_clock("04:00 PM"),
            "duration_minutes": 30,
            "timing_mode": TimingMode.UNSPECIFIED,
            "travel_required": True,
            "location": "Bank",
        }
    ]

    feasible, reason = engine._module_d_is_feasible(
        timeline,
        parse_clock("07:00 AM"),
        parse_clock("10:00 PM"),
        0,
    )

    assert feasible is False
    assert reason == "time_window"


def test_optional_coffee_break_skips_bad_preferred_window_slot():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    request_text = (
        "Plan tomorrow. I have a fixed workshop from 10 AM to 12 PM. "
        "Add one short coffee break if possible."
    )
    parsed = {
        "intent": "add",
        "reply": "Draft created.",
        "transcription": request_text,
        "date": "2026-08-18",
        "preferences": {"day_start_time": "08:00", "day_end_time": "12:00"},
        "operations": [
            {"op": "add", "title": "Workshop", "timing_mode": TimingMode.FIXED, "fixed_start": "10:00", "duration_minutes": 120},
            {"op": "add", "title": "Coffee break", "duration_minutes": 15, "location_kind": "category_only", "location_category": "meal_place", "travel_required": True, "location_resolution_status": "needs_coordinates"},
        ],
    }

    normalized = engine._normalize_parsed_locations(parsed, request_text)
    envelope = engine.build_schedule_response(normalized, None, request_text)["schedule_data"]

    assert _count_title(envelope, "Coffee break") == 0
    assert any(item["title"] == "Coffee break" for item in envelope["unscheduled_activities"])


def test_location_summary_marks_resolved_home_as_resolved(capsys):
    engine = SchedulingEngine(DummyClient())

    engine._log_location_and_timing_summary([
        {
            "op": "add",
            "title": "Dinner with family",
            "location_category": "home",
            "location_kind": "home",
            "travel_required": True,
            "location_resolution_status": "resolved_coordinates",
        }
    ])

    assert "Dinner with family -> home (resolved)" in capsys.readouterr().out


def test_day_boundary_preferences_are_used_by_module_c():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    parsed = {
        "intent": "add",
        "reply": "Draft created.",
        "transcription": "Plan focused work.",
        "date": "2026-05-02",
        "preferences": {"day_start_time": "10:00", "day_end_time": "12:00"},
        "operations": [
            {"op": "add", "title": "Focused Work", "duration_minutes": 60},
        ],
    }

    envelope = engine.build_schedule_response(parsed, None, "Plan focused work.")["schedule_data"]
    work = _activity_by_title(envelope, "Focused Work")

    assert envelope["preferences"]["day_start"] == "10:00"
    assert envelope["preferences"]["day_end"] == "12:00"
    assert work["startTime"] == "10:00 AM"


def test_accurate_travel_missing_default_start_returns_start_location_request_without_ors():
    fake_travel = FakeTravelService(route_minutes=15)
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    envelope = {
        "date": "2026-05-02",
        "status": "ok",
        "schedule_status": "ok",
        "travel_validation_status": "not_requested",
        "accurate_travel_time": True,
        "preferences": {"accurate_travel_time": True},
        "activities": [],
        "schedule_blocks": [
            {
                "block_type": "activity",
                "id": "meeting",
                "stable_activity_id": "meeting",
                "title": "Client Meeting",
                "start": "09:00 AM",
                "end": "10:00 AM",
                "startTime": "09:00 AM",
                "endTime": "10:00 AM",
                "location": "office",
                "location_label": "office",
                "resolved_location": {
                    "display_name": "Office",
                    "latitude": 2.9,
                    "longitude": 101.6,
                    "source": "event_confirmed",
                },
            },
        ],
        "warnings": [],
        "location_resolution_requests": [],
    }

    updated = engine._apply_accurate_travel_if_requested(envelope, saved_locations=[])

    assert updated["travel_validation_status"] == "pending_locations"
    assert updated["schedule_status"] == "location_pending"
    assert updated["location_resolution_requests"][0]["request_type"] == "start_location"
    assert updated["location_resolution_requests"][0]["title"] == "Where are you starting from for this plan?"
    assert fake_travel.route_calls == 0


def test_default_start_location_calculates_route_to_first_physical_event():
    fake_travel = FakeTravelService(route_minutes=17)
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    envelope = {
        "date": "2026-05-02",
        "status": "ok",
        "schedule_status": "ok",
        "travel_validation_status": "not_requested",
        "accurate_travel_time": True,
        "preferences": _accurate_preferences(day_start_time="08:00", day_end_time="22:00"),
        "activities": [],
        "schedule_blocks": [
            {
                "block_type": "idle",
                "type": "idle",
                "title": "Free Time",
                "start": "08:00 AM",
                "end": "09:00 AM",
                "startTime": "08:00 AM",
                "endTime": "09:00 AM",
                "duration_minutes": 60,
            },
            {
                "block_type": "activity",
                "id": "meeting",
                "stable_activity_id": "meeting",
                "title": "Client Meeting",
                "start": "09:00 AM",
                "end": "10:00 AM",
                "startTime": "09:00 AM",
                "endTime": "10:00 AM",
                "location": "office",
                "location_label": "office",
            },
        ],
        "warnings": [],
        "location_resolution_requests": [],
    }
    saved_locations = [
        {"label": "office", "address": "Office", "latitude": 2.9, "longitude": 101.6},
    ]

    updated = engine._apply_accurate_travel_if_requested(envelope, saved_locations=saved_locations)

    assert updated["travel_validation_status"] == "validated"
    assert not any(block.get("block_type") == "transition" for block in updated["schedule_blocks"])
    assert updated["start_route_summary"]["start_location"] == "Home"
    assert updated["start_route_summary"]["first_physical_event"] == "Client Meeting"
    assert updated["start_route_summary"]["first_physical_event_location"] == "office"
    assert updated["start_route_summary"]["leave_by"] == "08:43 AM"
    assert fake_travel.route_calls == 1


def test_route_context_excludes_non_physical_activities_from_matrix():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService(route_minutes=9))
    physical_a = {
        "id": "client",
        "stable_activity_id": "client",
        "title": "Client Meeting",
        "location": "office",
        "location_label": "office",
        "travel_required": True,
    }
    home_task = {
        "id": "deep",
        "stable_activity_id": "deep",
        "title": "Deep Work",
        "location_category": "home_or_online",
        "location_status": "not_required",
        "travel_required": False,
    }
    physical_b = {
        "id": "dinner",
        "stable_activity_id": "dinner",
        "title": "Dinner",
        "location": "Dinner Place",
        "location_label": "Dinner Place",
        "location_category": "meal_place",
        "travel_required": True,
    }
    saved_locations = [
        {"label": "office", "address": "Office", "latitude": 2.9, "longitude": 101.6},
        {"label": "Dinner Place", "address": "Dinner Place", "latitude": 3.1, "longitude": 101.7},
    ]

    context = engine._build_route_context(
        {"date": "2026-05-02", "preferences": _accurate_preferences()},
        [physical_a, home_task, physical_b],
        saved_locations,
    )

    assert set(context["nodes"].keys()) == {"id:client", "id:dinner"}
    assert "id:deep" not in context["nodes"]
    assert set(context["pairs"].keys()) == {"id:client->id:dinner", "id:dinner->id:client"}


def test_route_context_precomputes_non_neighbor_physical_pairs_for_reorder():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService(route_minutes=11))
    activities = [
        {"id": "a", "stable_activity_id": "a", "title": "A", "location": "A", "location_label": "A", "travel_required": True},
        {"id": "b", "stable_activity_id": "b", "title": "B", "location": "B", "location_label": "B", "travel_required": True},
        {"id": "c", "stable_activity_id": "c", "title": "C", "location": "C", "location_label": "C", "travel_required": True},
    ]
    saved_locations = [
        {"label": "A", "address": "A", "latitude": 2.90, "longitude": 101.60},
        {"label": "B", "address": "B", "latitude": 2.91, "longitude": 101.61},
        {"label": "C", "address": "C", "latitude": 2.92, "longitude": 101.62},
    ]

    context = engine._build_route_context(
        {"date": "2026-05-02", "preferences": _accurate_preferences()},
        activities,
        saved_locations,
    )

    assert len(context["pairs"]) == 6
    assert context["pairs"]["id:a->id:c"]["duration_minutes"] == 11
    assert context["pairs"]["id:c->id:a"]["duration_minutes"] == 11


def test_route_context_detects_same_location_groups_and_excludes_non_physical(capsys):
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService(route_minutes=9))
    activities = [
        {"id": "client", "stable_activity_id": "client", "title": "Client Meeting", "location": "Mid Valley", "location_label": "Mid Valley", "travel_required": True},
        {"id": "grocery", "stable_activity_id": "grocery", "title": "Grocery Run", "location": "Mid Valley", "location_label": "Mid Valley", "travel_required": True},
        {"id": "deep", "stable_activity_id": "deep", "title": "Deep Work", "location_category": "home_or_online", "travel_required": False},
    ]
    saved_locations = [
        {"label": "Mid Valley", "address": "Mid Valley", "latitude": 3.118, "longitude": 101.677},
    ]

    context = engine._build_route_context(
        {"date": "2026-05-02", "preferences": _accurate_preferences()},
        activities,
        saved_locations,
    )
    logs = capsys.readouterr().out

    assert set(context["nodes"]) == {"id:client", "id:grocery"}
    assert context["same_location_groups"]
    assert set(context["same_location_groups"][0]["titles"]) == {"Client Meeting", "Grocery Run"}
    assert "[JPLAN][MODULE_C][SAME_LOCATION_GROUP]" in logs


def _same_location_route_context():
    def node(activity_id, title, lat, lng, location):
        return {
            "key": f"id:{activity_id}",
            "activity_id": activity_id,
            "title": title,
            "location": location,
            "location_label": location,
            "coordinate": {"latitude": lat, "longitude": lng},
        }

    nodes = {
        "id:client": node("client", "Client Meeting", 3.118, 101.677, "Mid Valley"),
        "id:grocery": node("grocery", "Grocery Run", 3.118, 101.677, "Mid Valley"),
        "id:team": node("team", "Team Lunch", 3.129, 101.670, "Bangsar"),
    }

    def pair(left, right, minutes):
        return {
            "from_key": f"id:{left}",
            "to_key": f"id:{right}",
            "from_title": nodes[f"id:{left}"]["title"],
            "to_title": nodes[f"id:{right}"]["title"],
            "duration_minutes": minutes,
            "source": "routing_service" if minutes else "same_location",
        }

    return {
        "enabled": True,
        "nodes": nodes,
        "pairs": {
            "id:client->id:grocery": pair("client", "grocery", 0),
            "id:grocery->id:client": pair("grocery", "client", 0),
            "id:client->id:team": pair("client", "team", 10),
            "id:team->id:client": pair("team", "client", 10),
            "id:grocery->id:team": pair("grocery", "team", 10),
            "id:team->id:grocery": pair("team", "grocery", 10),
        },
        "start_routes": {},
        "same_location_groups": [{
            "keys": ["id:client", "id:grocery"],
            "titles": ["Client Meeting", "Grocery Run"],
            "travel_minutes": 0,
        }],
        "missing": [],
    }


def _route_efficiency_activity(activity_id, title, start, end, location, *, fixed=False, priority="medium"):
    return {
        "id": activity_id,
        "stable_activity_id": activity_id,
        "title": title,
        "scheduled_start": start,
        "scheduled_end": end,
        "duration_minutes": end - start,
        "startTime": format_minutes_for_test(start),
        "endTime": format_minutes_for_test(end),
        "timing_mode": TimingMode.FIXED if fixed else TimingMode.UNSPECIFIED,
        "fixed_start": start if fixed else None,
        "fixed_end": end if fixed else None,
        "is_user_fixed": fixed,
        "locked_fixed": fixed,
        "priority": priority,
        "location": location,
        "location_label": location,
        "travel_required": True,
    }


def format_minutes_for_test(minutes):
    hour = (minutes // 60) % 24
    minute = minutes % 60
    suffix = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    return f"{display_hour:02d}:{minute:02d} {suffix}"


def test_module_d_same_location_cluster_moves_only_flexible_and_records_efficiency(capsys):
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    engine._current_route_context = _same_location_route_context()
    timeline = [
        _route_efficiency_activity("client", "Client Meeting", 540, 600, "Mid Valley", fixed=True),
        _route_efficiency_activity("team", "Team Lunch", 750, 810, "Bangsar", fixed=True),
        _route_efficiency_activity("grocery", "Grocery Run", 840, 885, "Mid Valley", priority="low"),
    ]

    repaired, _, meta = engine._apply_module_d_refinement(
        timeline,
        [],
        480,
        1320,
        0,
        {"refinement_reason": "explicit_optimize"},
    )
    logs = capsys.readouterr().out
    engine._current_route_context = None

    client = next(item for item in repaired if item["stable_activity_id"] == "client")
    team = next(item for item in repaired if item["stable_activity_id"] == "team")
    grocery = next(item for item in repaired if item["stable_activity_id"] == "grocery")

    assert client["scheduled_start"] == 540
    assert team["scheduled_start"] == 750
    assert grocery["scheduled_start"] != 840
    gap_after_client = grocery["scheduled_start"] - client["scheduled_end"]
    gap_before_client = client["scheduled_start"] - grocery["scheduled_end"]
    assert (
        0 <= gap_after_client <= 20
        or 0 <= gap_before_client <= 20
    )
    assert meta["refinement_applied"] is True
    assert meta["route_efficiency"]["route_total_after"] < meta["route_efficiency"]["route_total_before"]
    assert meta["route_efficiency"]["same_location_split_penalty_after"] < meta["route_efficiency"]["same_location_split_penalty_before"]
    assert "[JPLAN][MODULE_D][CANDIDATE] type=same_location_cluster target=Grocery Run anchor=Client Meeting" in logs
    assert "[JPLAN][MODULE_D][SCORE_BREAKDOWN]" in logs
    assert "[JPLAN][MODULE_D][SAME_LOCATION_ORDER]" in logs


def test_route_breakdown_penalizes_revisit_more_than_clustered_order():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    engine._current_route_context = _same_location_route_context()
    a = _route_efficiency_activity("client", "Client Meeting", 540, 600, "Mid Valley", fixed=True)
    b = _route_efficiency_activity("team", "Team Lunch", 750, 810, "Bangsar", fixed=True)
    a2 = _route_efficiency_activity("grocery", "Grocery Run", 840, 885, "Mid Valley")
    split = engine._module_d_route_breakdown([a, b, a2], [], 480, 1320, 0)
    clustered_grocery = deepcopy(a2)
    clustered_grocery["scheduled_start"] = 605
    clustered_grocery["scheduled_end"] = 650
    clustered = engine._module_d_route_breakdown([a, clustered_grocery, b], [], 480, 1320, 0)
    engine._current_route_context = None

    assert split["revisit_location_penalty"] > clustered["revisit_location_penalty"]
    assert split["same_location_split_penalty"] > clustered["same_location_split_penalty"]
    assert split["total_travel_minutes"] > clustered["total_travel_minutes"]


def test_same_location_cluster_rejection_logs_reason(capsys):
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    engine._current_route_context = _same_location_route_context()
    timeline = [
        _route_efficiency_activity("client", "Client Meeting", 540, 600, "Mid Valley", fixed=True),
        _route_efficiency_activity("team", "Team Lunch", 620, 680, "Bangsar", fixed=True),
        _route_efficiency_activity("grocery", "Grocery Run", 720, 765, "Mid Valley", priority="low"),
    ]

    engine._apply_module_d_refinement(
        timeline,
        [],
        540,
        780,
        0,
        {"refinement_reason": "explicit_optimize"},
    )
    logs = capsys.readouterr().out
    engine._current_route_context = None

    assert "[JPLAN][MODULE_D][REJECT] type=same_location_cluster reason=" in logs


def test_start_route_constraint_moves_or_unfits_pre_first_event_task(capsys):
    fake_travel = FakeTravelService(route_minutes=31)
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    deep_work = {
        "id": "deep",
        "stable_activity_id": "deep",
        "title": "Deep Work",
        "startTime": "07:00 AM",
        "endTime": "09:00 AM",
        "scheduled_start": 420,
        "scheduled_end": 540,
        "duration_minutes": 120,
        "priority": "high",
        "timing_mode": TimingMode.UNSPECIFIED,
        "original_timing_mode": TimingMode.UNSPECIFIED,
        "is_user_fixed": False,
        "is_system_scheduled": True,
        "can_move_for_repair": True,
        "location_category": "home_or_online",
        "location_status": "not_required",
        "travel_required": False,
    }
    client = {
        "id": "client",
        "stable_activity_id": "client",
        "title": "Client Meeting",
        "startTime": "09:00 AM",
        "endTime": "10:00 AM",
        "scheduled_start": 540,
        "scheduled_end": 600,
        "duration_minutes": 60,
        "timing_mode": TimingMode.FIXED,
        "original_timing_mode": TimingMode.FIXED,
        "fixed_start": 540,
        "fixed_end": 600,
        "is_user_fixed": True,
        "user_fixed_start": 540,
        "can_move_for_repair": False,
        "location": "office",
        "location_label": "office",
    }
    envelope = {
        "date": "2026-05-02",
        "status": "ok",
        "schedule_status": "ok",
        "travel_validation_status": "not_requested",
        "accurate_travel_time": True,
        "preferences": _accurate_preferences(day_start_time="07:00", day_end_time="22:00"),
        "activities": [deep_work, client],
        "schedule_blocks": [
            {"block_type": "activity", "start": "07:00 AM", "end": "09:00 AM", **deep_work},
            {"block_type": "activity", "start": "09:00 AM", "end": "10:00 AM", **client},
        ],
        "warnings": [],
        "location_resolution_requests": [],
    }
    saved_locations = [
        {"label": "office", "address": "Office", "latitude": 2.9, "longitude": 101.6},
    ]

    updated = engine._apply_accurate_travel_if_requested(envelope, saved_locations=saved_locations)
    logs = capsys.readouterr().out

    assert updated["start_route_summary"]["leave_by"] == "08:29 AM"
    assert "[JPLAN][START_ROUTE][CONSTRAINT]" in logs
    assert "[JPLAN][MODULE_D][START_BURDEN]" in logs
    assert updated["travel_validation_status"] in {"validated", "fallback_used", "route_conflict", "repaired_validated"}
    schedule_to_check = updated.get("preview_schedule") or updated
    deep = _activity_by_title(schedule_to_check, "Deep Work")
    if deep:
        deep_start = parse_clock(deep["startTime"])
        deep_end = parse_clock(deep["endTime"])
        assert deep_end <= 509 or deep_start >= 600
    else:
        assert any(item["title"] == "Deep Work" for item in schedule_to_check.get("unfit_activities", []))


def test_attached_relative_pre_first_event_does_not_create_start_route_conflict(capsys):
    fake_travel = FakeTravelService(route_minutes=35)
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    doctor = {
        "id": "doctor",
        "stable_activity_id": "doctor",
        "title": "Doctor appointment",
        "startTime": "09:00 AM",
        "endTime": "10:00 AM",
        "scheduled_start": 540,
        "scheduled_end": 600,
        "duration_minutes": 60,
        "timing_mode": TimingMode.FIXED,
        "fixed_start": 540,
        "fixed_end": 600,
        "is_user_fixed": True,
        "user_fixed_start": 540,
        "can_move_for_repair": False,
        "location": "Sunway Medical",
        "location_label": "Sunway Medical",
        "travel_required": True,
    }
    prepare = {
        "id": "prepare",
        "stable_activity_id": "prepare",
        "title": "Prepare documents",
        "startTime": "08:30 AM",
        "endTime": "09:00 AM",
        "scheduled_start": 510,
        "scheduled_end": 540,
        "duration_minutes": 30,
        "timing_mode": TimingMode.RELATIVE,
        "original_timing_mode": TimingMode.RELATIVE,
        "is_user_fixed": False,
        "can_move_for_repair": False,
        "repair_protection": "derived",
        "anchor_relation": {
            "kind": "before",
            "target_activity_id": "doctor",
            "target_title": "Doctor appointment",
        },
        "anchor_activity_id": "doctor",
        "anchor_title": "Doctor appointment",
        "placement_source": "system_derived",
        "is_derived_time": True,
        "location": "Sunway Medical",
        "location_label": "Sunway Medical",
        "location_status": "not_required",
        "travel_required": False,
    }
    envelope = {
        "date": "2026-05-02",
        "status": "ok",
        "schedule_status": "ok",
        "travel_validation_status": "not_requested",
        "accurate_travel_time": True,
        "preferences": _accurate_preferences(day_start_time="07:00", day_end_time="22:00"),
        "activities": [prepare, doctor],
        "schedule_blocks": [
            {"block_type": "activity", "start": "08:30 AM", "end": "09:00 AM", **prepare},
            {"block_type": "activity", "start": "09:00 AM", "end": "10:00 AM", **doctor},
        ],
        "warnings": [],
        "location_resolution_requests": [],
    }

    updated = engine._apply_accurate_travel_if_requested(
        envelope,
        saved_locations=[{"label": "Sunway Medical", "address": "Sunway Medical", "latitude": 2.9, "longitude": 101.6}],
    )
    logs = capsys.readouterr().out

    assert updated["start_route_summary"]["first_physical_event"] == "Doctor appointment"
    assert updated["start_route_summary"]["leave_by"] == "08:25 AM"
    assert updated["route_conflicts"] == []
    assert updated["travel_validation_status"] in {"validated", "fallback_used"}
    assert "reason=attached_relative_block" in logs


def test_accurate_travel_validation_recognizes_type_only_activity_blocks():
    fake_travel = FakeTravelService(route_minutes=12)
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    client = {
        "id": "client",
        "stable_activity_id": "client",
        "type": "activity",
        "title": "Client Meeting",
        "start": "09:00 AM",
        "end": "10:00 AM",
        "startTime": "09:00 AM",
        "endTime": "10:00 AM",
        "duration_minutes": 60,
        "timing_mode": TimingMode.FIXED,
        "fixed_start": 540,
        "fixed_end": 600,
        "is_user_fixed": True,
        "location": "Mid Valley",
        "location_label": "Mid Valley",
        "travel_required": True,
    }
    dentist = {
        "id": "dentist",
        "stable_activity_id": "dentist",
        "type": "activity",
        "title": "Dentist Appointment",
        "start": "05:00 PM",
        "end": "06:00 PM",
        "startTime": "05:00 PM",
        "endTime": "06:00 PM",
        "duration_minutes": 60,
        "timing_mode": TimingMode.FIXED,
        "fixed_start": 1020,
        "fixed_end": 1080,
        "is_user_fixed": True,
        "location": "Cheras",
        "location_label": "Cheras",
        "travel_required": True,
    }
    envelope = {
        "date": "2026-05-02",
        "status": "ok",
        "schedule_status": "ok",
        "travel_validation_status": "not_requested",
        "accurate_travel_time": True,
        "preferences": _accurate_preferences(day_start_time="08:00", day_end_time="22:00"),
        "activities": [client, dentist],
        "schedule_blocks": [
            client,
            {"type": "travel", "title": "Travel to Cheras", "start": "04:40 PM", "end": "05:00 PM", "duration_minutes": 20},
            dentist,
        ],
        "warnings": [],
        "location_resolution_requests": [],
    }
    saved_locations = [
        {"label": "Mid Valley", "address": "Mid Valley", "latitude": 3.118, "longitude": 101.677},
        {"label": "Cheras", "address": "Cheras", "latitude": 3.08, "longitude": 101.74},
    ]

    updated = engine._apply_accurate_travel_if_requested(envelope, saved_locations)

    assert updated["start_route_summary"]["first_physical_event"] == "Client Meeting"
    assert updated["start_route_summary"]["first_physical_event_location"] == "Mid Valley"
    assert updated["travel_validation_status"] in {"validated", "fallback_used"}
    travel_blocks = [
        block for block in updated["schedule_blocks"]
        if block.get("type") in {"travel", "transition"} or block.get("block_type") == "transition"
    ]
    assert len(travel_blocks) == 1
    assert travel_blocks[0]["duration_minutes"] == 12


def test_start_route_unfit_uses_partial_feasible_status():
    fake_travel = FakeTravelService(route_minutes=31)
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    envelope = {
        "date": "2026-05-02",
        "status": "ok",
        "schedule_status": "ok",
        "travel_validation_status": "not_requested",
        "accurate_travel_time": True,
        "preferences": _accurate_preferences(day_start_time="07:00", day_end_time="11:00"),
        "activities": [
            {
                "id": "deep",
                "stable_activity_id": "deep",
                "title": "Deep Work",
                "startTime": "07:00 AM",
                "endTime": "09:00 AM",
                "scheduled_start": 420,
                "scheduled_end": 540,
                "duration_minutes": 120,
                "priority": "low",
                "timing_mode": TimingMode.UNSPECIFIED,
                "is_user_fixed": False,
                "is_system_scheduled": True,
                "can_move_for_repair": True,
                "location_category": "home_or_online",
                "location_status": "not_required",
                "travel_required": False,
            },
            {
                "id": "client",
                "stable_activity_id": "client",
                "title": "Client Meeting",
                "startTime": "09:00 AM",
                "endTime": "10:00 AM",
                "scheduled_start": 540,
                "scheduled_end": 600,
                "duration_minutes": 60,
                "timing_mode": TimingMode.FIXED,
                "fixed_start": 540,
                "fixed_end": 600,
                "is_user_fixed": True,
                "user_fixed_start": 540,
                "can_move_for_repair": False,
                "location": "office",
                "location_label": "office",
            },
        ],
        "schedule_blocks": [
            {
                "block_type": "activity",
                "id": "deep",
                "stable_activity_id": "deep",
                "title": "Deep Work",
                "start": "07:00 AM",
                "end": "09:00 AM",
                "startTime": "07:00 AM",
                "endTime": "09:00 AM",
                "duration_minutes": 120,
                "location_category": "home_or_online",
                "location_status": "not_required",
                "travel_required": False,
            },
            {
                "block_type": "activity",
                "id": "client",
                "stable_activity_id": "client",
                "title": "Client Meeting",
                "start": "09:00 AM",
                "end": "10:00 AM",
                "startTime": "09:00 AM",
                "endTime": "10:00 AM",
                "duration_minutes": 60,
                "timing_mode": TimingMode.FIXED,
                "fixed_start": 540,
                "fixed_end": 600,
                "is_user_fixed": True,
                "user_fixed_start": 540,
                "can_move_for_repair": False,
                "location": "office",
                "location_label": "office",
            },
        ],
        "warnings": [],
        "location_resolution_requests": [],
    }

    updated = engine._apply_accurate_travel_if_requested(
        envelope,
        saved_locations=[{"label": "office", "address": "Office", "latitude": 2.9, "longitude": 101.6}],
    )

    assert updated["travel_validation_status"] == "partial_feasible_with_unfit"
    assert updated["schedule_status"] == "partial"
    assert any(item["title"] == "Deep Work" for item in updated["unfit_activities"])
    assert updated["preview_status"] == "partial_feasible_with_unfit"
    assert updated["preview_schedule"]["unfit_activities"][0]["title"] == "Deep Work"
    assert updated["schedule_blocks"] == envelope["schedule_blocks"]


def test_fixed_pre_first_event_start_route_blocker_is_hard_conflict():
    fake_travel = FakeTravelService(route_minutes=20)
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    prepare = {
        "id": "prepare",
        "stable_activity_id": "prepare",
        "title": "Prepare documents",
        "startTime": "07:00 AM",
        "endTime": "07:45 AM",
        "scheduled_start": 420,
        "scheduled_end": 465,
        "duration_minutes": 45,
        "timing_mode": TimingMode.FIXED,
        "fixed_start": 420,
        "fixed_end": 465,
        "is_user_fixed": True,
        "user_fixed_start": 420,
        "can_move_for_repair": False,
        "location_category": "home_or_online",
        "location_status": "not_required",
        "travel_required": False,
    }
    client = {
        "id": "client",
        "stable_activity_id": "client",
        "title": "Client Meeting",
        "startTime": "08:00 AM",
        "endTime": "09:00 AM",
        "scheduled_start": 480,
        "scheduled_end": 540,
        "duration_minutes": 60,
        "timing_mode": TimingMode.FIXED,
        "fixed_start": 480,
        "fixed_end": 540,
        "is_user_fixed": True,
        "user_fixed_start": 480,
        "can_move_for_repair": False,
        "location": "office",
        "location_label": "office",
        "travel_required": True,
    }
    envelope = {
        "date": "2026-05-02",
        "status": "ok",
        "schedule_status": "ok",
        "travel_validation_status": "not_requested",
        "accurate_travel_time": True,
        "preferences": _accurate_preferences(day_start_time="07:00", day_end_time="22:00"),
        "activities": [prepare, client],
        "schedule_blocks": [
            {"block_type": "activity", "start": "07:00 AM", "end": "07:45 AM", **prepare},
            {"block_type": "activity", "start": "08:00 AM", "end": "09:00 AM", **client},
        ],
        "warnings": [],
        "location_resolution_requests": [],
    }

    updated = engine._apply_accurate_travel_if_requested(
        envelope,
        saved_locations=[{"label": "office", "address": "Office", "latitude": 2.9, "longitude": 101.6}],
    )

    assert updated["travel_validation_status"] == "route_conflict"
    assert any(conflict["reason_code"] == "fixed_to_fixed_infeasible" for conflict in updated["route_conflicts"])
    assert "preview_schedule" not in updated
    assert _activity_by_title(updated, "Prepare documents")["startTime"] == "07:00 AM"
    assert _activity_by_title(updated, "Client Meeting")["startTime"] == "08:00 AM"
    assert any(
        block.get("reason_code") == "fixed_to_fixed_infeasible"
        and "Route conflict: Prepare documents -> Client Meeting" in block.get("title", "")
        for block in updated["schedule_blocks"]
    )


def test_resolved_user_selected_location_survives_clause_ownership_validation():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    operation = {
        "title": "Pharmacy stop",
        "location": "Sunway Medical Centre",
        "location_label": "Sunway Medical Centre",
        "location_status": "resolved",
        "location_source": "event_confirmed",
        "explicit_user_location": True,
        "resolved_location": {
            "display_name": "Sunway Medical Centre",
            "latitude": 3.067,
            "longitude": 101.603,
            "source": "event_confirmed",
        },
    }

    validated = engine._validate_clause_location_ownership(operation, scoped_evidence="")

    assert validated["location_label"] == "Sunway Medical Centre"
    assert validated["resolved_location"]["display_name"] == "Sunway Medical Centre"
    assert validated["location_status"] == "resolved"


def test_accurate_travel_on_missing_location_returns_location_pending_without_background_geocode():
    fake_travel = FakeTravelService(geocode_candidates=[
        {
            "display_name": "Supermarket Cyberjaya",
            "address": "Cyberjaya, Selangor",
            "latitude": 2.92,
            "longitude": 101.65,
            "source": "ors_geocoded",
        }
    ])
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    parsed = {
        "intent": "add",
        "reply": "Draft created.",
        "transcription": "Fit in a grocery shopping trip.",
        "date": "2026-05-02",
        "preferences": _accurate_preferences(),
        "operations": [
            {"op": "add", "title": "Grocery Shopping", "duration_minutes": 45, "location": "home"},
        ],
    }

    envelope = engine.build_schedule_response(parsed, None, "Fit in a grocery shopping trip.")["schedule_data"]

    assert envelope["schedule_status"] == "location_pending"
    assert envelope["travel_validation_status"] == "pending_locations"
    assert envelope["location_resolution_requests"]
    request = envelope["location_resolution_requests"][0]
    assert request["title"] == "Grocery Shopping"
    assert request["category"] == "supermarket"
    assert request["location_readiness_status"] == "missing_coordinates"
    assert request["display_reason"] == "needs exact map location"
    assert request["geocode_candidates"] == []
    assert fake_travel.geocode_calls == 0
    assert fake_travel.route_calls == 0


def test_accurate_travel_with_saved_coordinates_uses_route_service():
    fake_travel = FakeTravelService(route_minutes=18)
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    parsed = {
        "intent": "add",
        "reply": "Draft created.",
        "transcription": "Meeting at Main Office followed by Seminar at Library.",
        "date": "2026-05-02",
        "preferences": _accurate_preferences(),
        "operations": [
            {"op": "add", "title": "Meeting", "timing_mode": TimingMode.FIXED, "fixed_start": "09:00", "duration_minutes": 60, "location": "office"},
            {"op": "add", "title": "Seminar", "timing_mode": TimingMode.FIXED, "fixed_start": "10:30", "duration_minutes": 60, "location": "library"},
        ],
    }
    saved_locations = [
        {"label": "office", "address": "Main Office", "latitude": 2.9, "longitude": 101.6},
        {"label": "library", "address": "Library", "latitude": 2.91, "longitude": 101.61},
    ]

    envelope = engine.build_schedule_response(
        parsed,
        None,
        "Meeting at Main Office followed by Seminar at Library.",
        saved_locations=saved_locations,
    )["schedule_data"]
    transition = next(block for block in envelope["schedule_blocks"] if block.get("block_type") == "transition")

    assert envelope["travel_validation_status"] == "validated"
    assert transition["travel_estimate_source"] == "routing_service"
    assert transition["route_duration_minutes"] == 18
    assert fake_travel.route_calls == 2


def test_accurate_travel_requires_coordinates_not_location_labels_only():
    fake_travel = FakeTravelService()
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    parsed = {
        "intent": "add",
        "reply": "Draft created.",
        "transcription": "Client meeting at Bangsar followed by dinner at Cheras.",
        "date": "2026-05-02",
        "preferences": _accurate_preferences(),
        "operations": [
            {"op": "add", "title": "Client Meeting", "timing_mode": TimingMode.FIXED, "fixed_start": "09:00", "duration_minutes": 60, "location": "Bangsar"},
            {"op": "add", "title": "Dinner", "timing_mode": TimingMode.FIXED, "fixed_start": "11:00", "duration_minutes": 60, "location": "Cheras"},
        ],
    }
    saved_locations = [
        {"label": "Bangsar", "address": "Bangsar, Kuala Lumpur"},
        {"label": "Cheras", "address": "Cheras, Kuala Lumpur"},
    ]

    envelope = engine.build_schedule_response(
        parsed,
        None,
        "Client meeting at Bangsar followed by dinner at Cheras.",
        saved_locations=saved_locations,
    )["schedule_data"]

    assert envelope["travel_validation_status"] == "pending_locations"
    assert envelope["location_resolution_requests"]
    missing_titles = {request["title"] for request in envelope["location_resolution_requests"]}
    assert "Client Meeting" in missing_titles
    assert "Dinner" in missing_titles
    assert fake_travel.route_calls == 0


def test_accurate_travel_readiness_excludes_location_neutral_tasks_and_keeps_physical_missing():
    fake_travel = FakeTravelService()
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    parsed = {
        "intent": "add",
        "reply": "Draft created.",
        "transcription": (
            "Client meeting at Mid Valley, deep work, team lunch at Bangsar, "
            "dentist near Cheras, grocery run, and dinner with parents."
        ),
        "date": "2026-05-02",
        "preferences": _accurate_preferences(),
        "operations": [
            {"op": "add", "title": "Client Meeting", "timing_mode": TimingMode.FIXED, "fixed_start": "09:00", "duration_minutes": 60, "location": "Mid Valley"},
            {"op": "add", "title": "Deep Work", "timing_mode": TimingMode.FIXED, "fixed_start": "10:30", "duration_minutes": 60},
            {"op": "add", "title": "Team Lunch", "timing_mode": TimingMode.FIXED, "fixed_start": "12:00", "duration_minutes": 60, "location": "Bangsar"},
            {"op": "add", "title": "Dentist Appointment", "timing_mode": TimingMode.FIXED, "fixed_start": "14:00", "duration_minutes": 60, "location": "Cheras"},
            {"op": "add", "title": "Grocery Run", "timing_mode": TimingMode.FIXED, "fixed_start": "16:00", "duration_minutes": 45},
            {"op": "add", "title": "Dinner with Parents", "timing_mode": TimingMode.FIXED, "fixed_start": "18:00", "duration_minutes": 60},
        ],
    }

    envelope = engine.build_schedule_response(
        parsed,
        None,
        parsed["transcription"],
    )["schedule_data"]

    missing_titles = {request["title"] for request in envelope["location_resolution_requests"]}
    assert envelope["travel_validation_status"] == "pending_locations"
    assert "Client Meeting" in missing_titles
    assert "Team Lunch" in missing_titles
    assert "Dentist Appointment" in missing_titles
    assert "Grocery Run" in missing_titles
    assert "Dinner with Parents" in missing_titles
    assert "Deep Work" not in missing_titles
    assert _activity_by_title(envelope, "Deep Work")["travel_required"] is False
    assert fake_travel.geocode_calls == 0
    assert fake_travel.route_calls == 0


def test_accurate_travel_skips_location_neutral_tasks_and_bridges_real_locations():
    fake_travel = FakeTravelService(route_minutes=12)
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    parsed = {
        "intent": "add",
        "reply": "Draft created.",
        "transcription": "Study at Library, then review assignment, then go to gym.",
        "date": "2026-05-02",
        "preferences": _accurate_preferences(),
        "operations": [
            {"op": "add", "title": "Study at library", "timing_mode": TimingMode.FIXED, "fixed_start": "09:00", "duration_minutes": 60, "location": "library"},
            {"op": "add", "title": "Review assignment", "timing_mode": TimingMode.FIXED, "fixed_start": "10:00", "duration_minutes": 30},
            {"op": "add", "title": "Gym", "timing_mode": TimingMode.FIXED, "fixed_start": "11:00", "duration_minutes": 60, "location": "gym"},
        ],
    }
    saved_locations = [
        {"label": "library", "address": "Library", "latitude": 2.91, "longitude": 101.61},
        {"label": "gym", "address": "Gym", "latitude": 2.92, "longitude": 101.62},
    ]

    envelope = engine.build_schedule_response(
        parsed,
        None,
        "Study at Library, then review assignment, then go to gym.",
        saved_locations=saved_locations,
    )["schedule_data"]
    review = _activity_by_title(envelope, "Review assignment")
    transition = next(
        block for block in envelope["schedule_blocks"]
        if block.get("block_type") == "transition" and block.get("to_location") == "gym"
    )

    assert review["location_status"] == "not_required"
    assert review["travel_required"] is False
    assert envelope["location_resolution_requests"] == []
    assert envelope["travel_validation_status"] == "validated"
    assert transition["from_location"] == "library"
    assert transition["to_location"] == "gym"
    assert transition["route_duration_minutes"] == 12
    assert fake_travel.route_calls == 2


def test_complete_travel_validation_clears_location_pending_state():
    fake_travel = FakeTravelService(route_minutes=18)
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    pending_envelope = {
        "date": "2026-05-02",
        "status": "location_pending",
        "schedule_status": "location_pending",
        "travel_validation_status": "pending_locations",
        "accurate_travel_time": True,
        "preferences": _accurate_preferences(),
        "location_resolution_requests": [
            {"activity_id": "meeting", "title": "Meeting"},
        ],
        "validation_issues": [
            "Accurate travel time is pending location confirmation for Meeting.",
        ],
        "schedule_blocks": [
            {
                "block_type": "activity",
                "id": "meeting",
                "stable_activity_id": "meeting",
                "title": "Meeting",
                "start": "09:00 AM",
                "end": "10:00 AM",
                "location": "office",
                "location_label": "office",
            },
            {
                "block_type": "transition",
                "title": "Travel to library",
                "start": "10:00 AM",
                "end": "10:30 AM",
                "duration_minutes": 30,
            },
            {
                "block_type": "activity",
                "id": "seminar",
                "stable_activity_id": "seminar",
                "title": "Seminar",
                "start": "10:30 AM",
                "end": "11:30 AM",
                "location": "library",
                "location_label": "library",
            },
        ],
        "activities": [],
        "warnings": [],
    }
    saved_locations = [
        {"label": "office", "address": "Main Office", "latitude": 2.9, "longitude": 101.6},
        {"label": "library", "address": "Library", "latitude": 2.91, "longitude": 101.61},
    ]

    envelope = engine._apply_accurate_travel_if_requested(pending_envelope, saved_locations)

    assert envelope["travel_validation_status"] == "validated"
    assert envelope["schedule_status"] == "ok"
    assert envelope["status"] == "ok"
    assert envelope["location_resolution_requests"] == []
    assert envelope["validation_issues"] == []


def test_complete_travel_validation_uses_event_level_resolved_locations():
    fake_travel = FakeTravelService(route_minutes=18)
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    pending_envelope = {
        "date": "2026-05-02",
        "status": "location_pending",
        "schedule_status": "location_pending",
        "travel_validation_status": "pending_locations",
        "accurate_travel_time": True,
        "preferences": _accurate_preferences(),
        "location_resolution_requests": [
            {"activity_id": "meeting", "title": "Meeting"},
            {"activity_id": "seminar", "title": "Seminar"},
        ],
        "validation_issues": [
            "Accurate travel time is pending location confirmation for Meeting.",
            "Accurate travel time is pending location confirmation for Seminar.",
        ],
        "schedule_blocks": [
            {
                "block_type": "activity",
                "id": "meeting",
                "stable_activity_id": "meeting",
                "title": "Meeting",
                "start": "09:00 AM",
                "end": "10:00 AM",
                "location": "Main Office map point",
                "location_label": "Main Office map point",
                "resolved_location": {
                    "display_name": "Main Office map point",
                    "latitude": 2.9,
                    "longitude": 101.6,
                    "source": "event_confirmed",
                    "confirmed_by_user": True,
                },
            },
            {
                "block_type": "transition",
                "title": "Travel to Library map point",
                "start": "10:00 AM",
                "end": "10:30 AM",
                "duration_minutes": 30,
            },
            {
                "block_type": "activity",
                "id": "seminar",
                "stable_activity_id": "seminar",
                "title": "Seminar",
                "start": "10:30 AM",
                "end": "11:30 AM",
                "location": "Library map point",
                "location_label": "Library map point",
                "resolved_location": {
                    "display_name": "Library map point",
                    "latitude": 2.91,
                    "longitude": 101.61,
                    "source": "manual_map_pin",
                    "confirmed_by_user": True,
                },
            },
        ],
        "activities": [],
        "warnings": [],
    }

    envelope = engine._apply_accurate_travel_if_requested(pending_envelope, saved_locations=[])
    transition = next(
        block for block in envelope["schedule_blocks"]
        if block.get("block_type") == "transition" and block.get("to_location") == "Library map point"
    )

    assert envelope["travel_validation_status"] == "validated"
    assert envelope["schedule_status"] == "ok"
    assert envelope["location_resolution_requests"] == []
    assert transition["travel_estimate_source"] == "routing_service"
    assert transition["from_coordinate"] == {"latitude": 2.9, "longitude": 101.6}
    assert transition["to_coordinate"] == {"latitude": 2.91, "longitude": 101.61}
    assert fake_travel.route_calls == 2


def test_accurate_route_duration_retimes_transition_and_creates_idle_slack():
    fake_travel = FakeTravelService(route_minutes=4)
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    pending_envelope = {
        "date": "2026-05-02",
        "status": "location_pending",
        "schedule_status": "location_pending",
        "travel_validation_status": "pending_locations",
        "accurate_travel_time": True,
        "preferences": _accurate_preferences(),
        "location_resolution_requests": [],
        "validation_issues": [
            "Accurate travel time is pending location confirmation for Seminar.",
        ],
        "explanations": [
            "Accurate travel time is pending until the requested locations are confirmed.",
        ],
        "activities": [
            {
                "id": "meeting",
                "stable_activity_id": "meeting",
                "title": "Project Meeting",
                "startTime": "09:00 AM",
                "endTime": "10:30 AM",
                "location": "office",
                "location_label": "office",
            },
            {
                "id": "seminar",
                "stable_activity_id": "seminar",
                "title": "Seminar",
                "startTime": "11:00 AM",
                "endTime": "12:30 PM",
                "location": "library",
                "location_label": "library",
            },
        ],
        "schedule_blocks": [
            {
                "block_type": "activity",
                "id": "meeting",
                "stable_activity_id": "meeting",
                "title": "Project Meeting",
                "start": "09:00 AM",
                "end": "10:30 AM",
                "startTime": "09:00 AM",
                "endTime": "10:30 AM",
                "location": "office",
                "location_label": "office",
            },
            {
                "block_type": "idle",
                "title": "Free Time",
                "start": "10:30 AM",
                "end": "10:35 AM",
                "duration_minutes": 5,
            },
            {
                "block_type": "buffer",
                "title": "Prep / Buffer",
                "start": "10:35 AM",
                "end": "10:40 AM",
                "duration_minutes": 5,
            },
            {
                "block_type": "transition",
                "title": "Travel to library",
                "start": "10:40 AM",
                "end": "11:00 AM",
                "duration_minutes": 20,
                "from_location": "office",
                "to_location": "library",
            },
            {
                "block_type": "activity",
                "id": "seminar",
                "stable_activity_id": "seminar",
                "title": "Seminar",
                "start": "11:00 AM",
                "end": "12:30 PM",
                "startTime": "11:00 AM",
                "endTime": "12:30 PM",
                "location": "library",
                "location_label": "library",
            },
        ],
        "warnings": [],
    }
    saved_locations = [
        {"label": "office", "display_name": "Main Office", "address": "Main Office", "latitude": 2.9, "longitude": 101.6, "source": "saved_profile"},
        {"label": "library", "display_name": "Library", "address": "Library", "latitude": 2.91, "longitude": 101.61, "source": "saved_profile"},
    ]

    envelope = engine._apply_accurate_travel_if_requested(pending_envelope, saved_locations)
    travel = next(
        block for block in envelope["schedule_blocks"]
        if block.get("block_type") == "transition" and str(block.get("to_location") or "").strip().lower() == "library"
    )
    new_idle = next(
        block for block in envelope["schedule_blocks"]
        if block.get("block_type") == "idle" and block.get("start") == "10:30 AM"
    )
    seminar = next(block for block in envelope["schedule_blocks"] if block.get("title") == "Seminar")

    assert sum(1 for block in envelope["schedule_blocks"] if block.get("block_type") == "transition") == 1
    assert not any(block.get("block_type") == "buffer" for block in envelope["schedule_blocks"])
    assert travel["start"] == "10:56 AM"
    assert travel["end"] == "11:00 AM"
    assert travel["duration_minutes"] == 4
    assert travel["route_duration_minutes"] == 4
    assert parse_clock(travel["end"]) - parse_clock(travel["start"]) == travel["duration_minutes"]
    assert travel["travel_estimate_source"] == "routing_service"
    assert travel["travel_validation_status"] == "validated"
    assert new_idle["end"] == "10:56 AM"
    assert new_idle["duration_minutes"] == 26
    assert seminar["start"] == "11:00 AM"
    assert envelope["travel_validation_status"] == "validated"
    assert envelope["location_resolution_requests"] == []
    assert not any("pending" in explanation.lower() for explanation in envelope["explanations"])
    assert "Accurate travel time has been validated using the routing service." in envelope["explanations"]
    assert all(activity.get("resolved_location") for activity in envelope["activities"])
    assert all(activity.get("location_status") == "resolved" for activity in envelope["activities"])


def test_manual_scheduler_uses_plan_default_buffer_minutes():
    engine = SchedulingEngine(DummyClient())
    envelope = {
        "date": "2026-06-09",
        "version": 1,
        "preferences": {
            "day_start_time": "08:00",
            "day_end_time": "13:00",
            "default_buffer_minutes": 15,
            "prep_buffer": 15,
        },
        "activities": [
            {
                "id": "office",
                "stable_activity_id": "office",
                "title": "Office work",
                "duration_minutes": 60,
                "timing_mode": "fixed",
                "fixed_start": 9 * 60,
                "fixed_end": 10 * 60,
                "location": "office",
                "location_label": "office",
                "travel_required": True,
            },
            {
                "id": "library",
                "stable_activity_id": "library",
                "title": "Library session",
                "duration_minutes": 60,
                "timing_mode": "fixed",
                "fixed_start": 11 * 60,
                "fixed_end": 12 * 60,
                "location": "library",
                "location_label": "library",
                "travel_required": True,
            },
        ],
    }

    replanned = engine.run_manual_scheduler(envelope, base_version=1)
    buffers = [block for block in replanned["schedule_blocks"] if block.get("block_type") == "buffer"]

    assert replanned["preferences"]["default_buffer_minutes"] == 15
    assert replanned["preferences"]["prep_buffer"] == 15
    assert buffers
    assert buffers[0]["duration_minutes"] == 15


def test_accurate_route_longer_duration_replaces_stale_support_segment():
    fake_travel = FakeTravelService(route_minutes=30)
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    pending_envelope = {
        "date": "2026-05-02",
        "status": "ok",
        "schedule_status": "ok",
        "travel_validation_status": "not_requested",
        "accurate_travel_time": True,
        "preferences": _accurate_preferences(),
        "activities": [],
        "schedule_blocks": [
            {
                "block_type": "activity",
                "id": "lunch",
                "stable_activity_id": "lunch",
                "title": "Team lunch",
                "start": "12:30 PM",
                "end": "01:30 PM",
                "startTime": "12:30 PM",
                "endTime": "01:30 PM",
                "location": "Bangsar",
                "location_label": "Bangsar",
            },
            {
                "block_type": "activity",
                "id": "deep",
                "stable_activity_id": "deep",
                "title": "Deep work",
                "start": "01:30 PM",
                "end": "03:30 PM",
                "startTime": "01:30 PM",
                "endTime": "03:30 PM",
                "location_category": "home_or_online",
                "location_status": "not_required",
                "travel_required": False,
            },
            {
                "block_type": "idle",
                "type": "idle",
                "title": "Free Time",
                "start": "03:30 PM",
                "end": "04:35 PM",
                "startTime": "03:30 PM",
                "endTime": "04:35 PM",
                "duration_minutes": 65,
            },
            {
                "block_type": "buffer",
                "type": "buffer",
                "title": "Prep / Buffer",
                "start": "04:35 PM",
                "end": "04:40 PM",
                "startTime": "04:35 PM",
                "endTime": "04:40 PM",
                "duration_minutes": 5,
            },
            {
                "block_type": "transition",
                "type": "travel",
                "title": "Travel to Cheras",
                "start": "04:40 PM",
                "end": "05:00 PM",
                "startTime": "04:40 PM",
                "endTime": "05:00 PM",
                "duration_minutes": 20,
                "from_location": "Bangsar",
                "to_location": "Cheras",
            },
            {
                "block_type": "activity",
                "id": "dentist",
                "stable_activity_id": "dentist",
                "title": "Dentist appointment",
                "start": "05:00 PM",
                "end": "06:00 PM",
                "startTime": "05:00 PM",
                "endTime": "06:00 PM",
                "location": "Cheras",
                "location_label": "Cheras",
            },
        ],
        "warnings": [],
        "location_resolution_requests": [],
    }
    saved_locations = [
        {"label": "Bangsar", "address": "Bangsar", "latitude": 3.13, "longitude": 101.67},
        {"label": "Cheras", "address": "Cheras", "latitude": 3.08, "longitude": 101.74},
    ]

    envelope = engine._apply_accurate_travel_if_requested(pending_envelope, saved_locations)
    transitions = [
        block for block in envelope["schedule_blocks"]
        if block.get("block_type") == "transition" and block.get("to_location") == "Cheras"
    ]
    buffers = [block for block in envelope["schedule_blocks"] if block.get("block_type") == "buffer"]
    idle = next(
        block for block in envelope["schedule_blocks"]
        if block.get("block_type") == "idle" and block.get("start") == "03:30 PM"
    )

    assert envelope["travel_validation_status"] == "validated"
    assert envelope["route_conflicts"] == []
    assert len(transitions) == 1
    assert transitions[0]["title"] == "Travel to Cheras"
    assert transitions[0]["start"] == "04:30 PM"
    assert transitions[0]["end"] == "05:00 PM"
    assert transitions[0]["duration_minutes"] == 30
    assert transitions[0]["route_duration_minutes"] == 30
    assert buffers == []
    assert idle["start"] == "03:30 PM"
    assert idle["end"] == "04:30 PM"


def _route_repair_dentist_dinner_envelope():
    return {
        "date": "2026-05-02",
        "version": 3,
        "status": "ok",
        "schedule_status": "ok",
        "travel_validation_status": "not_requested",
        "accurate_travel_time": True,
        "preferences": _accurate_preferences(),
        "activities": [
            {
                "id": "dentist",
                "stable_activity_id": "dentist",
                "title": "Dentist Appointment",
                "startTime": "05:00 PM",
                "endTime": "06:00 PM",
                "scheduled_start": 1020,
                "scheduled_end": 1080,
                "duration_minutes": 60,
                "timing_mode": TimingMode.FIXED,
                "original_timing_mode": TimingMode.FIXED,
                "fixed_start": 1020,
                "fixed_end": 1080,
                "is_user_fixed": True,
                "user_fixed_start": 1020,
                "can_move_for_repair": False,
                "location": "Cheras",
                "location_label": "Cheras",
            },
            {
                "id": "dinner",
                "stable_activity_id": "dinner",
                "title": "Dinner with Parents",
                "startTime": "06:00 PM",
                "endTime": "07:30 PM",
                "scheduled_start": 1080,
                "scheduled_end": 1170,
                "duration_minutes": 90,
                "timing_mode": TimingMode.UNSPECIFIED,
                "original_timing_mode": TimingMode.UNSPECIFIED,
                "is_user_fixed": False,
                "is_system_scheduled": True,
                "can_move_for_repair": True,
                "preferred_time_window": "evening",
                "preferred_window_start": 1080,
                "preferred_window_end": 1260,
                "location": "Dinner Place",
                "location_label": "Dinner Place",
                "location_category": "meal_place",
            },
        ],
        "schedule_blocks": [
            {
                "block_type": "activity",
                "id": "dentist",
                "stable_activity_id": "dentist",
                "title": "Dentist Appointment",
                "start": "05:00 PM",
                "end": "06:00 PM",
                "startTime": "05:00 PM",
                "endTime": "06:00 PM",
                "duration_minutes": 60,
                "timing_mode": TimingMode.FIXED,
                "original_timing_mode": TimingMode.FIXED,
                "fixed_start": 1020,
                "fixed_end": 1080,
                "is_user_fixed": True,
                "user_fixed_start": 1020,
                "can_move_for_repair": False,
                "location": "Cheras",
                "location_label": "Cheras",
            },
            {
                "block_type": "activity",
                "id": "dinner",
                "stable_activity_id": "dinner",
                "title": "Dinner with Parents",
                "start": "06:00 PM",
                "end": "07:30 PM",
                "startTime": "06:00 PM",
                "endTime": "07:30 PM",
                "duration_minutes": 90,
                "timing_mode": TimingMode.UNSPECIFIED,
                "original_timing_mode": TimingMode.UNSPECIFIED,
                "is_user_fixed": False,
                "is_system_scheduled": True,
                "can_move_for_repair": True,
                "preferred_time_window": "evening",
                "preferred_window_start": 1080,
                "preferred_window_end": 1260,
                "location": "Dinner Place",
                "location_label": "Dinner Place",
                "location_category": "meal_place",
            },
        ],
        "warnings": [],
        "location_resolution_requests": [],
    }


def _route_repair_locations():
    return [
        {"label": "Cheras", "address": "Cheras", "latitude": 3.08, "longitude": 101.74},
        {"label": "Dinner Place", "address": "Dinner Place", "latitude": 3.16, "longitude": 101.71},
    ]


def _dental_pharmacy_repair_locations():
    return [
        {"label": "Home", "address": "Home", "latitude": 3.01, "longitude": 101.60},
        {"label": "Sunway Medical Centre", "address": "Sunway Medical Centre", "latitude": 3.06, "longitude": 101.61},
        {"label": "Gym", "address": "Gym", "latitude": 3.12, "longitude": 101.68},
    ]


def _dental_pharmacy_repair_envelope():
    activities = [
        {
            "id": "dental",
            "stable_activity_id": "dental",
            "title": "Dental checkup",
            "startTime": "01:30 PM",
            "endTime": "03:00 PM",
            "scheduled_start": 810,
            "scheduled_end": 900,
            "duration_minutes": 90,
            "timing_mode": TimingMode.FIXED,
            "original_timing_mode": TimingMode.FIXED,
            "fixed_start": 810,
            "fixed_end": 900,
            "is_user_fixed": True,
            "user_fixed_start": 810,
            "can_move_for_repair": False,
            "repair_protection": "fixed",
            "location": "Sunway Medical Centre",
            "location_label": "Sunway Medical Centre",
            "travel_required": True,
        },
        {
            "id": "pharmacy",
            "stable_activity_id": "pharmacy",
            "title": "Pharmacy stop",
            "startTime": "03:00 PM",
            "endTime": "03:30 PM",
            "scheduled_start": 900,
            "scheduled_end": 930,
            "duration_minutes": 30,
            "timing_mode": TimingMode.RELATIVE,
            "original_timing_mode": TimingMode.RELATIVE,
            "anchor_relation": {"kind": "after", "target_activity_id": "dental", "target_title": "Dental checkup"},
            "is_user_fixed": False,
            "can_move_for_repair": True,
            "repair_protection": "flexible",
            "location": "Sunway Medical Centre",
            "location_label": "Sunway Medical Centre",
            "travel_required": True,
        },
        {
            "id": "gym",
            "stable_activity_id": "gym",
            "title": "Gym",
            "startTime": "03:15 PM",
            "endTime": "04:15 PM",
            "scheduled_start": 915,
            "scheduled_end": 975,
            "duration_minutes": 60,
            "timing_mode": TimingMode.UNSPECIFIED,
            "original_timing_mode": TimingMode.UNSPECIFIED,
            "is_user_fixed": False,
            "can_move_for_repair": True,
            "repair_protection": "flexible",
            "location": "Gym",
            "location_label": "Gym",
            "travel_required": True,
        },
    ]
    return {
        "date": "2026-05-26",
        "version": 7,
        "status": "route_conflict",
        "schedule_status": "route_conflict",
        "travel_validation_status": "repair_suggestion_pending",
        "accurate_travel_time": True,
        "preferences": {**_accurate_preferences(), "accurate_travel_time": True},
        "activities": deepcopy(activities),
        "schedule_blocks": [
            {
                "block_type": "activity",
                "type": "activity",
                "start": item["startTime"],
                "end": item["endTime"],
                **deepcopy(item),
            }
            for item in activities
        ],
        "unscheduled_activities": [],
        "route_conflicts": [{"reason_code": "not_enough_travel_time", "reason": "Dental needs to move later."}],
        "pending_repair_suggestions": [
            {
                "id": "suggestion_1",
                "type": "move_activity",
                "activity_id": "dental",
                "title": "Dental checkup",
                "from": "01:30 PM",
                "from_end": "03:00 PM",
                "to": "02:34 PM",
                "to_end": "04:04 PM",
                "to_start_minutes": 874,
                "to_end_minutes": 964,
                "duration_minutes": 90,
                "reason": "Move Dental checkup to satisfy accurate travel.",
                "cascade_suggestions": [
                    {
                        "activity_id": "pharmacy",
                        "title": "Pharmacy stop",
                        "from": "03:00 PM",
                        "from_end_label": "03:30 PM",
                        "to": "04:09 PM",
                        "to_end_label": "04:39 PM",
                        "to_start": 969,
                        "to_end": 999,
                        "impact_type": "anchor_dependent_recalculated",
                    }
                ],
                "impact_type": "fixed_target_move",
                "requires_user_confirmation": True,
                "would_change": True,
                "schedule_version": 7,
                "preview_id": "preview_dental",
            }
        ],
        "preview_id": "preview_dental",
        "preview_base_version": 7,
        "preview_status": "repair_suggestion_pending",
        "preview_reason": "Suggested route-aware plan needs confirmation before moving Dental checkup.",
        "preview_schedule": {"schedule_blocks": [], "travel_validation_status": "repair_suggestion_pending"},
        "warnings": [],
        "location_resolution_requests": [],
    }


def _route_repair_fixed_dinner_envelope():
    envelope = _route_repair_dentist_dinner_envelope()
    for collection in (envelope["activities"], envelope["schedule_blocks"]):
        for item in collection:
            if item.get("stable_activity_id") != "dinner":
                continue
            item.update({
                "timing_mode": TimingMode.FIXED,
                "original_timing_mode": TimingMode.FIXED,
                "fixed_start": 1080,
                "fixed_end": 1170,
                "is_user_fixed": True,
                "user_fixed_start": 1080,
                "can_move_for_repair": False,
                "repair_protection": "fixed",
            })
    return envelope


def test_accurate_travel_auto_repairs_flexible_dinner_without_confirmation(capsys):
    fake_travel = FakeTravelService(route_minutes=35)
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)

    envelope = engine._apply_accurate_travel_if_requested(
        _route_repair_dentist_dinner_envelope(),
        _route_repair_locations(),
    )
    logs = capsys.readouterr().out

    dinner = _activity_by_title(envelope["preview_schedule"], "Dinner with Parents")

    assert envelope["travel_validation_status"] == "repaired_validated"
    assert envelope["schedule_status"] == "ok"
    assert envelope["route_conflicts"] == []
    assert envelope["pending_repair_suggestions"] == []
    assert envelope["route_repair_actions"]
    assert envelope["route_repair_actions"][0]["title"] == "Dinner with Parents"
    assert envelope["preview_status"] == "repaired_validated"
    assert dinner["startTime"] == "06:40 PM"
    assert dinner["repair_protection"] == "protected_social"
    assert dinner["can_move_for_repair"] is True
    assert "[JPLAN][ROUTE_REPAIR_ACTIONS]" in logs
    assert "[JPLAN][SUMMARY][REPAIRED_FINAL]" in logs


def test_protected_dinner_repair_candidate_before_evening_is_rejected():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService(route_minutes=35))
    original = _route_repair_dentist_dinner_envelope()
    repaired = deepcopy(original)
    for collection in (repaired["activities"], repaired["schedule_blocks"]):
        for item in collection:
            if item.get("stable_activity_id") == "dinner":
                item["startTime"] = "02:00 PM"
                item["endTime"] = "03:30 PM"
                item["start"] = "02:00 PM"
                item["end"] = "03:30 PM"

    violations = engine._protected_semantic_violations(original, repaired)

    assert violations
    assert violations[0]["reason_code"] == "violates_evening_or_social_commitment"


def test_final_route_validation_rejects_dirty_fixed_event_move():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService(route_minutes=12))
    original = _route_repair_dentist_dinner_envelope()
    repaired = deepcopy(original)
    for collection in (repaired["activities"], repaired["schedule_blocks"]):
        for item in collection:
            if item.get("stable_activity_id") == "dentist":
                item["startTime"] = "05:30 PM"
                item["endTime"] = "06:30 PM"
                item["start"] = "05:30 PM"
                item["end"] = "06:30 PM"
                item["scheduled_start"] = 1050
                item["scheduled_end"] = 1110
    context = engine._build_route_context(repaired, engine._load_canonical_activities(repaired), _route_repair_locations())

    validation = engine._final_validate_route_aware_repair(original, repaired, context)

    assert validation["travel_validation_status"] == "route_conflict"
    assert any(conflict.get("reason_code") == "fixed_event_moved" for conflict in validation["route_conflicts"])


def test_fixed_route_conflict_does_not_create_pending_fixed_move_suggestion():
    fake_travel = FakeTravelService(route_minutes=35)
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    envelope = engine._apply_accurate_travel_if_requested(
        _route_repair_fixed_dinner_envelope(),
        _route_repair_locations(),
    )
    dinner_before_apply = _activity_by_title(envelope, "Dinner with Parents")

    assert dinner_before_apply["startTime"] == "06:00 PM"
    assert envelope["pending_repair_suggestions"] == []
    assert any(conflict.get("reason_code") == "fixed_to_fixed_infeasible" for conflict in envelope["route_conflicts"])
    assert envelope["travel_validation_status"] in {"route_conflict", "partial_feasible_with_fixed_route_conflicts"}


def test_explicit_allow_fixed_confirmation_applies_suggestion_and_reruns_validation():
    fake_travel = FakeTravelService(route_minutes=35)
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    envelope = _dental_pharmacy_repair_envelope()

    result = engine.handle_pending_repair_confirmation("allow moving fixed event", envelope, _dental_pharmacy_repair_locations())
    updated = result["envelope"]
    dental = _activity_by_title(updated, "Dental checkup")

    assert result["reply_status"] == "success"
    assert dental["startTime"] == "02:34 PM"
    assert updated["pending_repair_suggestions"] == []
    assert updated["travel_validation_status"] == "repaired_validated"
    assert updated["route_conflicts"] == []
    assert updated["preferences"]["module_0_route"] == "repair_confirmation"
    assert updated["preferences"]["refinement_reason"] == "explicit_route_repair"
    assert updated["refinement_skipped_reason"] is None


def test_pending_repair_confirmation_recalculates_relative_dependents_and_refits_flex():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService(route_minutes=20))
    envelope = _dental_pharmacy_repair_envelope()

    result = engine.handle_pending_repair_confirmation("allow moving fixed event", envelope, _dental_pharmacy_repair_locations())
    updated = result["envelope"]
    dental = _activity_by_title(updated, "Dental checkup")
    pharmacy = _activity_by_title(updated, "Pharmacy stop")
    gym = _activity_by_title(updated, "Gym")

    assert result["reply_status"] == "success"
    assert updated["travel_validation_status"] == "repaired_validated"
    assert dental["startTime"] == "02:34 PM"
    assert pharmacy["timing_mode"] == TimingMode.RELATIVE
    assert pharmacy["anchor_relation"]["target_activity_id"] == "dental"
    assert pharmacy["startTime"] != "03:00 PM"
    assert parse_clock(pharmacy["startTime"]) >= parse_clock(dental["endTime"])
    assert gym["startTime"] != "03:15 PM"
    assert updated["route_conflicts"] == []
    assert updated["pending_repair_suggestions"] == []


def test_pending_repair_apply_failure_is_atomic_and_consumes_suggestion(monkeypatch):
    fake_travel = FakeTravelService(route_minutes=35)
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    envelope = _dental_pharmacy_repair_envelope()
    committed_blocks = deepcopy(envelope["schedule_blocks"])

    def fail_final_validation(*args, **kwargs):
        return {
            "travel_validation_status": "route_conflict",
            "schedule_blocks": [],
            "route_conflicts": [
                {
                    "reason_code": "not_enough_travel_time",
                    "from_activity": "Pharmacy stop",
                    "to_activity": "Gym",
                    "reason": "Pharmacy stop and Gym now conflict with Dental checkup.",
                }
            ],
            "updated_transition_count": 0,
            "start_route_summary": None,
        }

    monkeypatch.setattr(engine, "_final_validate_route_aware_repair", fail_final_validation)

    result = engine.handle_pending_repair_confirmation("allow moving fixed event", envelope, _dental_pharmacy_repair_locations())
    updated = result["envelope"]

    assert result["reply_reason"] == "pending_repair_apply_failed"
    assert updated["schedule_blocks"] == committed_blocks
    assert updated["pending_repair_suggestions"] == []
    assert updated["preview_status"] == "apply_failed"
    assert updated["failed_repair_attempt"]["reason"] == "Pharmacy stop and Gym now conflict with Dental checkup."


def test_fixed_repair_suggestion_generation_skips_fixed_target_cascade():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService(route_minutes=20))
    envelope = _dental_pharmacy_repair_envelope()
    conflict = {
        "from_activity": "Lunch",
        "to_activity": "Dental checkup",
        "from_end": "02:00 PM",
        "required_route_minutes": 34,
        "required_travel_minutes": 34,
    }

    suggestions = engine._protected_repair_suggestions_from_conflicts(envelope, [conflict])

    assert suggestions == []


def test_fixed_to_fixed_infeasible_returns_warning_without_pending_suggestions():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService(route_minutes=35))
    envelope = _route_repair_fixed_dinner_envelope()
    focus = {
        "id": "focus",
        "stable_activity_id": "focus",
        "title": "Focused deep work",
        "startTime": "03:00 PM",
        "endTime": "04:00 PM",
        "scheduled_start": 900,
        "scheduled_end": 960,
        "duration_minutes": 60,
        "timing_mode": TimingMode.UNSPECIFIED,
        "original_timing_mode": TimingMode.UNSPECIFIED,
        "is_user_fixed": False,
        "can_move_for_repair": True,
        "repair_protection": "flexible",
        "location_category": "home_or_online",
        "location_status": "not_required",
        "travel_required": False,
    }
    envelope["activities"].append(deepcopy(focus))
    envelope["schedule_blocks"].append({"block_type": "activity", "type": "activity", "start": "03:00 PM", "end": "04:00 PM", **focus})

    updated = engine._apply_accurate_travel_if_requested(envelope, _route_repair_locations())

    assert updated["travel_validation_status"] == "partial_feasible_with_fixed_route_conflicts"
    assert updated["schedule_status"] == "partial"
    assert any(conflict.get("reason_code") == "fixed_to_fixed_infeasible" for conflict in updated["route_conflicts"])
    assert updated["pending_repair_suggestions"] == []
    assert updated["preview_status"] == "partial_feasible_with_fixed_route_conflicts"
    assert updated["preview_schedule"]["pending_repair_suggestions"] == []
    assert updated["blocked_activities"] == []
    assert updated["unfit_activities"] == []
    focus_block = _activity_by_title(updated["preview_schedule"], "Focused deep work")
    assert focus_block["startTime"] == "08:00 AM"
    assert any(action["title"] == "Focused deep work" for action in updated["route_repair_actions"])
    warning_rows = [
        block for block in updated["preview_schedule"]["schedule_blocks"]
        if block.get("reason_code") == "fixed_to_fixed_infeasible"
    ]
    assert warning_rows
    assert all(
        block.get("display_only") is True
        and block.get("is_route_conflict") is True
        and block.get("block_type") == "route_conflict"
        for block in warning_rows
    )


def test_fixed_route_partial_status_requires_renderable_repaired_chronology(monkeypatch):
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService(route_minutes=30))
    current_blocks = [
        {"id": "a", "block_type": "activity", "title": "Fixed A", "start": "09:00 AM", "end": "10:00 AM", "startTime": "09:00 AM", "endTime": "10:00 AM"}
    ]
    envelope = {
        "date": "2026-05-26",
        "status": "ok",
        "schedule_status": "ok",
        "accurate_travel_time": True,
        "preferences": {"accurate_travel_time": True},
        "activities": current_blocks,
        "schedule_blocks": current_blocks,
        "unscheduled_activities": [],
        "explanations": [],
    }
    fixed_conflict = {
        "reason_code": "fixed_to_fixed_infeasible",
        "from_activity": "Fixed A",
        "to_activity": "Fixed B",
        "from_end": "10:00 AM",
        "to_start": "10:00 AM",
        "required_travel_minutes": 30,
        "available_gap_minutes": 0,
        "reason": "Fixed A cannot reach Fixed B on time.",
    }

    monkeypatch.setattr(engine, "_location_resolution_requests", lambda *args, **kwargs: [])
    monkeypatch.setattr(engine, "_validate_routes_with_service", lambda *args, **kwargs: {
        "travel_validation_status": "route_conflict",
        "schedule_blocks": current_blocks,
        "route_conflicts": [fixed_conflict],
        "updated_transition_count": 0,
    })
    monkeypatch.setattr(engine, "_attempt_route_repair", lambda *args, **kwargs: {
        "route_repair_attempted": True,
        "pending_repair_suggestions": [],
        "unfit_activities": [],
        "blocked_activities": [],
        "travel_validation_status": "partial_feasible_with_fixed_route_conflicts",
        "route_repair_actions": [],
        "route_efficiency": {},
        "repaired_envelope": {"activities": [], "schedule_blocks": []},
        "repaired_validation": {
            "schedule_blocks": [],
            "route_conflicts": [fixed_conflict],
            "updated_transition_count": 0,
        },
    })

    updated = engine._apply_accurate_travel_if_requested(envelope, [])

    assert updated["travel_validation_status"] == "route_conflict"
    assert updated["schedule_blocks"] == current_blocks


def test_relative_derived_materialized_block_stays_recalculable():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())

    canonical = engine._canonicalize_activity({
        "id": "prep",
        "stable_activity_id": "prep",
        "title": "Document preparation",
        "timing_mode": TimingMode.FIXED,
        "fixed_start": 420,
        "fixed_end": 465,
        "startTime": "07:00 AM",
        "endTime": "07:45 AM",
        "duration_minutes": 45,
        "anchor_relation": {"kind": "before", "target_activity_id": "workshop", "target_title": "Morning workshop"},
        "placement_source": "system_derived",
        "is_derived_time": True,
    })

    assert canonical["timing_mode"] == TimingMode.RELATIVE
    assert canonical["repair_protection"] == "derived"
    assert canonical["can_move_for_repair"] is True
    assert canonical["is_user_fixed"] is False
    assert canonical["fixed_start"] is None
    assert canonical["anchor_relation"]["kind"] == "before"
    assert canonical["placement_source"] == "system_derived"


def test_route_repair_preserves_relative_derived_activity_semantics():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())

    prepared = engine._prepare_activity_for_route_repair({
        "id": "gym",
        "stable_activity_id": "gym",
        "title": "Gym",
        "timing_mode": TimingMode.RELATIVE,
        "scheduled_start": parse_clock("01:55 PM"),
        "scheduled_end": parse_clock("02:40 PM"),
        "fixed_start": None,
        "fixed_end": None,
        "duration_minutes": 45,
        "anchor_relation": {"kind": "before", "target_activity_id": "dinner", "target_title": "Dinner with family"},
        "placement_source": "system_derived",
        "is_derived_time": True,
        "is_user_fixed": False,
        "can_move_for_repair": False,
        "repair_protection": "derived",
    })

    assert prepared["timing_mode"] == TimingMode.RELATIVE
    assert prepared["repair_protection"] == "derived"
    assert prepared["locked_fixed"] is False
    assert prepared["fixed_start"] is None
    assert prepared["scheduled_start"] is None
    assert prepared["anchor_relation"]["target_title"] == "Dinner with family"


def test_schedule_envelope_accepts_legacy_unscheduled_activity_without_reason():
    import main as backend_main

    envelope = backend_main.ScheduleEnvelope(**{
        "date": "2026-05-26",
        "activities": [],
        "unscheduled_activities": [
            {
                "id": "act-focus",
                "title": "Focused deep work",
                "duration_minutes": 180,
                "priority": "high",
                "isMandatory": True,
            }
        ],
    })

    assert envelope.unscheduled_activities[0].reason == "Could not fit in the schedule."


def test_noop_pending_repair_suggestion_is_skipped(capsys):
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService(route_minutes=30))
    envelope = _custom_envelope([
        {
            "op": "add",
            "title": "Lunch meeting",
            "timing_mode": TimingMode.FIXED,
            "fixed_start": "12:30",
            "duration_minutes": 60,
            "location": "SS15",
        }
    ])
    conflict = {
        "from_activity": "Previous stop",
        "to_activity": "Lunch meeting",
        "to_start": "12:30 PM",
        "from_end": "12:00 PM",
        "required_route_minutes": 30,
        "required_travel_minutes": 30,
    }

    suggestions = engine._protected_repair_suggestions_from_conflicts(envelope, [conflict])

    assert suggestions == []
    assert "[JPLAN][PENDING_REPAIR][SKIP_NOOP]" in capsys.readouterr().out


def test_pending_repair_keeps_current_chronology(monkeypatch):
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService(route_minutes=30))
    current_blocks = [
        {"id": "current", "block_type": "activity", "title": "Grocery shopping", "start": "07:30 AM", "end": "08:15 AM"}
    ]
    repaired_blocks = [
        {"id": "repaired", "block_type": "activity", "title": "Doctor appointment", "start": "09:00 AM", "end": "10:00 AM"}
    ]
    envelope = {
        "date": "2026-05-26",
        "status": "ok",
        "schedule_status": "ok",
        "accurate_travel_time": True,
        "preferences": {"accurate_travel_time": True},
        "activities": current_blocks,
        "schedule_blocks": current_blocks,
        "unscheduled_activities": [],
        "explanations": [],
    }
    current_start_route = {"first_physical_event": "Grocery shopping", "leave_by": "07:25 AM"}
    repaired_start_route = {"first_physical_event": "Doctor appointment", "leave_by": "08:25 AM"}

    monkeypatch.setattr(engine, "_location_resolution_requests", lambda *args, **kwargs: [])
    monkeypatch.setattr(engine, "_validate_routes_with_service", lambda *args, **kwargs: {
        "travel_validation_status": "route_conflict",
        "schedule_blocks": current_blocks,
        "route_conflicts": [{"reason_code": "not_enough_travel_time", "reason": "Needs travel"}],
        "updated_transition_count": 0,
        "start_route_summary": current_start_route,
    })
    monkeypatch.setattr(engine, "_attempt_route_repair", lambda *args, **kwargs: {
        "route_repair_attempted": True,
        "pending_repair_suggestions": [{"id": "suggestion_1", "title": "Doctor appointment", "would_change": True}],
        "unfit_activities": [],
        "travel_validation_status": "repair_suggestion_pending",
        "route_repair_actions": [{"title": "Gym", "from": "01:30 PM", "to": "04:05 PM"}],
        "route_efficiency": {},
        "repaired_envelope": {"activities": repaired_blocks, "schedule_blocks": repaired_blocks},
        "repaired_validation": {
            "schedule_blocks": repaired_blocks,
            "route_conflicts": [],
            "updated_transition_count": 1,
            "start_route_summary": repaired_start_route,
        },
        "start_route_summary": repaired_start_route,
    })

    updated = engine._apply_accurate_travel_if_requested(envelope, [])

    assert updated["travel_validation_status"] == "route_conflict"
    assert updated["schedule_blocks"] == current_blocks
    assert updated["start_route_summary"] == current_start_route
    assert updated["route_repair_actions"] == []
    assert "committed_schedule_blocks" not in updated
    assert "preview_status" not in updated
    assert "preview_id" not in updated
    assert "preview_schedule" not in updated
    assert updated["pending_repair_suggestions"][0].get("preview_id") is None


def test_valid_route_repair_returns_preview_chronology_without_committing(monkeypatch):
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService(route_minutes=30))
    current_blocks = [
        {"id": "current", "block_type": "activity", "title": "Grocery shopping", "start": "07:30 AM", "end": "08:15 AM"}
    ]
    repaired_blocks = [
        {"id": "doctor", "block_type": "activity", "title": "Doctor appointment", "start": "09:00 AM", "end": "10:00 AM"},
        {"id": "travel", "block_type": "transition", "title": "Travel to Grocery shopping", "start": "10:05 AM", "end": "10:42 AM"},
        {"id": "grocery", "block_type": "activity", "title": "Grocery shopping", "start": "10:42 AM", "end": "11:27 AM"},
    ]
    envelope = {
        "date": "2026-05-26",
        "status": "ok",
        "schedule_status": "ok",
        "accurate_travel_time": True,
        "preferences": {"accurate_travel_time": True},
        "activities": current_blocks,
        "schedule_blocks": current_blocks,
        "unscheduled_activities": [],
        "explanations": [],
    }
    repaired_start_route = {"first_physical_event": "Doctor appointment", "leave_by": "08:25 AM"}
    action = {"title": "Grocery shopping", "from": "07:30 AM", "to": "10:42 AM"}

    monkeypatch.setattr(engine, "_location_resolution_requests", lambda *args, **kwargs: [])
    monkeypatch.setattr(engine, "_validate_routes_with_service", lambda *args, **kwargs: {
        "travel_validation_status": "route_conflict",
        "schedule_blocks": current_blocks,
        "route_conflicts": [{"reason_code": "not_enough_travel_time", "reason": "Needs travel"}],
        "updated_transition_count": 0,
        "start_route_summary": {"first_physical_event": "Grocery shopping", "leave_by": "07:25 AM"},
    })
    monkeypatch.setattr(engine, "_attempt_route_repair", lambda *args, **kwargs: {
        "route_repair_attempted": True,
        "pending_repair_suggestions": [],
        "unfit_activities": [],
        "travel_validation_status": "repaired_validated",
        "route_repair_actions": [action],
        "route_efficiency": {},
        "repaired_envelope": {"activities": repaired_blocks, "schedule_blocks": repaired_blocks},
        "repaired_validation": {
            "schedule_blocks": repaired_blocks,
            "activities": repaired_blocks,
            "unscheduled_activities": [],
            "route_conflicts": [],
            "updated_transition_count": 1,
            "start_route_summary": repaired_start_route,
        },
    })

    updated = engine._apply_accurate_travel_if_requested(envelope, [])

    assert updated["travel_validation_status"] == "repaired_validated"
    assert updated["schedule_blocks"] == current_blocks
    assert updated["preview_schedule"]["schedule_blocks"] == repaired_blocks
    assert updated["preview_schedule"]["start_route_summary"] == repaired_start_route
    assert updated["route_repair_actions"] == [action]
    assert updated["preview_status"] == "repaired_validated"


def test_pending_repair_no_confirmation_keeps_schedule_and_conflict_visible():
    fake_travel = FakeTravelService(route_minutes=35)
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    envelope = engine._apply_accurate_travel_if_requested(
        _route_repair_fixed_dinner_envelope(),
        _route_repair_locations(),
    )

    result = engine.handle_pending_repair_confirmation("no", envelope, _route_repair_locations())

    assert result is None
    assert envelope["pending_repair_suggestions"] == []
    assert envelope["route_conflicts"]
    assert envelope["schedule_status"] == "route_conflict"


def test_pending_repair_rejects_stale_preview_id():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService(route_minutes=35))
    envelope = _route_repair_fixed_dinner_envelope()
    envelope.update({
        "version": 3,
        "preview_id": "preview_current",
        "preview_base_version": 3,
        "pending_repair_suggestions": [
            {
                "id": "suggestion_1",
                "activity_id": "dinner",
                "title": "Dinner with Parents",
                "from": "06:00 PM",
                "from_end": "07:30 PM",
                "to": "06:40 PM",
                "to_end": "08:10 PM",
                "duration_minutes": 90,
                "would_change": True,
                "schedule_version": 3,
                "preview_id": "preview_old",
            }
        ],
    })

    result = engine.handle_pending_repair_confirmation("yes", envelope, _route_repair_locations())

    assert result["reply_reason"] == "pending_repair_version_mismatch"
    assert result["envelope"]["pending_repair_suggestions"]


def test_accurate_travel_unfit_fallback_lists_low_score_flexible_activity():
    fake_travel = FakeTravelService(route_minutes=35)
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    envelope = _route_repair_fixed_dinner_envelope()
    envelope["activities"][1]["title"] = "Fixed Dinner"
    envelope["schedule_blocks"][1]["title"] = "Fixed Dinner"

    # Space out the fixed activities so they don't trigger fixed_to_fixed_infeasible.
    # Dentist: 04:00 PM - 05:00 PM. Gap: 60 mins. Travel: 35 mins. Remaining: 25 mins.
    dentist = next(a for a in envelope["activities"] if a["id"] == "dentist")
    dentist["startTime"] = "04:00 PM"
    dentist["endTime"] = "05:00 PM"
    dentist["scheduled_start"] = 960
    dentist["scheduled_end"] = 1020
    dentist["fixed_start"] = 960
    dentist["fixed_end"] = 1020
    dentist["user_fixed_start"] = 960

    for block in envelope["schedule_blocks"]:
        if block.get("id") == "dentist":
            block["start"] = "04:00 PM"
            block["end"] = "05:00 PM"

    grocery = {
        "id": "grocery",
        "stable_activity_id": "grocery",
        "title": "Grocery Run",
        "startTime": "05:00 PM",
        "endTime": "05:45 PM",
        "scheduled_start": 1020,
        "scheduled_end": 1065,
        "duration_minutes": 600,
        "priority": "low",
        "timing_mode": TimingMode.UNSPECIFIED,
        "is_user_fixed": False,
        "is_system_scheduled": True,
        "can_move_for_repair": True,
        "location": "Store",
        "location_label": "Store",
    }
    envelope["activities"].append(grocery)
    envelope["schedule_blocks"].append(grocery)

    locations = _route_repair_locations() + [{"label": "Store", "address": "Store", "latitude": 3.10, "longitude": 101.72}]

    updated = engine._apply_accurate_travel_if_requested(envelope, locations)

    assert updated["travel_validation_status"] == "partial_feasible_with_unfit"
    assert updated["preview_status"] == "partial_feasible_with_unfit"
    assert updated["preview_schedule"]["unfit_activities"]
    assert not updated.get("pending_repair_suggestions")
    assert updated["unfit_activities"]
    assert updated["unfit_activities"][0]["title"] == "Grocery Run"
    assert updated["unfit_activities"][0]["duration_minutes"] == 600
    assert updated["unfit_activities"][0]["suggested_resolution"]


def test_canonicalize_preserves_system_scheduled_flexible_activity_as_movable():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())

    flexible = engine._canonicalize_activity({
        "id": "dinner",
        "title": "Dinner with Parents",
        "timing_mode": TimingMode.UNSPECIFIED,
        "startTime": "06:00 PM",
        "endTime": "07:30 PM",
        "duration_minutes": 90,
    })
    fixed = engine._canonicalize_activity({
        "id": "dentist",
        "title": "Dentist Appointment",
        "timing_mode": TimingMode.FIXED,
        "startTime": "05:00 PM",
        "endTime": "06:00 PM",
        "duration_minutes": 60,
    })

    assert flexible["fixed_start"] is None
    assert flexible["scheduled_start"] == 1080
    assert flexible["is_user_fixed"] is False
    assert flexible["can_move_for_repair"] is True
    assert fixed["fixed_start"] == 1020
    assert fixed["is_user_fixed"] is True
    assert fixed["can_move_for_repair"] is False


def test_accurate_travel_support_overlap_marks_route_conflict():
    fake_travel = FakeTravelService(route_minutes=12)
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    resolved = {"latitude": 3.1, "longitude": 101.7, "display_name": "Office"}
    pending_envelope = {
        "date": "2026-05-02",
        "status": "ok",
        "schedule_status": "ok",
        "travel_validation_status": "not_requested",
        "accurate_travel_time": True,
        "preferences": _accurate_preferences(),
        "activities": [],
        "schedule_blocks": [
            {
                "block_type": "activity",
                "id": "a",
                "stable_activity_id": "a",
                "title": "Meeting A",
                "start": "09:00 AM",
                "end": "10:00 AM",
                "startTime": "09:00 AM",
                "endTime": "10:00 AM",
                "location": "Office",
                "location_label": "Office",
                "resolved_location": resolved,
            },
            {
                "block_type": "buffer",
                "type": "buffer",
                "title": "Prep / Buffer",
                "start": "10:00 AM",
                "end": "10:15 AM",
                "startTime": "10:00 AM",
                "endTime": "10:15 AM",
                "duration_minutes": 15,
            },
            {
                "block_type": "transition",
                "type": "travel",
                "title": "Travel to Office",
                "start": "10:05 AM",
                "end": "10:20 AM",
                "startTime": "10:05 AM",
                "endTime": "10:20 AM",
                "duration_minutes": 15,
            },
            {
                "block_type": "activity",
                "id": "b",
                "stable_activity_id": "b",
                "title": "Meeting B",
                "start": "10:30 AM",
                "end": "11:30 AM",
                "startTime": "10:30 AM",
                "endTime": "11:30 AM",
                "location": "Office",
                "location_label": "Office",
                "resolved_location": resolved,
            },
        ],
        "warnings": [],
        "location_resolution_requests": [],
    }

    envelope = engine._apply_accurate_travel_if_requested(pending_envelope, saved_locations=[])

    assert envelope["travel_validation_status"] == "route_conflict"
    assert envelope["schedule_status"] == "route_conflict"
    assert envelope["route_conflicts"]
    assert envelope["route_conflicts"][0]["type"] == "support_block_overlap"


def test_accurate_travel_with_coordinates_and_ors_failure_uses_fallback_not_location_pending():
    fake_travel = FakeTravelService(route_error=RuntimeError("ORS down"))
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    parsed = {
        "intent": "add",
        "reply": "Draft created.",
        "transcription": "Meeting at Main Office followed by Seminar at Library.",
        "date": "2026-05-02",
        "preferences": _accurate_preferences(),
        "operations": [
            {"op": "add", "title": "Meeting", "timing_mode": TimingMode.FIXED, "fixed_start": "09:00", "duration_minutes": 60, "location": "office"},
            {"op": "add", "title": "Seminar", "timing_mode": TimingMode.FIXED, "fixed_start": "10:30", "duration_minutes": 60, "location": "library"},
        ],
    }
    saved_locations = [
        {"label": "office", "address": "Main Office", "latitude": 2.9, "longitude": 101.6},
        {"label": "library", "address": "Library", "latitude": 2.91, "longitude": 101.61},
    ]

    envelope = engine.build_schedule_response(
        parsed,
        None,
        "Meeting at Main Office followed by Seminar at Library.",
        saved_locations=saved_locations,
    )["schedule_data"]

    assert envelope["travel_validation_status"] == "fallback_used"
    assert envelope["schedule_status"] != "location_pending"
    assert envelope["warnings"]


def test_travel_service_expands_mmu_alias_before_geocoding():
    service = TravelService(api_key=None)

    assert service.expand_alias("MMU") == "Multimedia University Cyberjaya, Selangor, Malaysia"


def test_travel_service_biases_malaysia_geocoding(monkeypatch):
    calls = []

    class FakeRequestsResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_get(url, params, timeout, headers=None):
        calls.append({"url": url, "params": params, "timeout": timeout})
        if "openrouteservice" in url:
            return FakeRequestsResponse({"features": []})
        return FakeRequestsResponse([])

    import travel_service

    monkeypatch.setattr(travel_service.requests, "get", fake_get)
    service = TravelService(api_key="fake-key")

    service.geocode_candidates("The Arc Cyberjaya Malaysia")

    ors_call = next(call for call in calls if "openrouteservice" in call["url"])
    assert ors_call["params"]["boundary.country"] == "MYS"
    assert ors_call["params"]["focus.point.lat"]
    assert ors_call["params"]["focus.point.lon"]


def test_travel_service_skips_nominatim_when_ors_candidate_is_good(monkeypatch):
    calls = []

    class FakeRequestsResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_get(url, params, timeout, headers=None):
        calls.append({"url": url, "params": params, "headers": headers})
        if "openrouteservice" in url:
            return FakeRequestsResponse({
                "features": [
                    {
                        "geometry": {"coordinates": [101.6373443, 2.9248643]},
                        "properties": {
                            "label": "The Arc @ Cyberjaya, Cyberjaya, Selangor, Malaysia",
                            "country": "Malaysia",
                            "region": "Selangor",
                            "confidence": 1,
                        },
                    }
                ]
            })
        raise AssertionError("Nominatim should not be called when ORS has a good candidate")

    import travel_service

    monkeypatch.setattr(travel_service.requests, "get", fake_get)
    service = TravelService(api_key="fake-key")

    result = service.geocode_candidates_with_metadata("The Arc Cyberjaya Malaysia")

    assert result["geocode_status"] == "ok"
    assert result["providers_used"] == ["ors"]
    assert result["candidates"][0]["source"] == "ors_geocoded"
    assert all("nominatim" not in call["url"] for call in calls)


def test_travel_service_uses_nominatim_when_ors_candidates_are_unrelated(monkeypatch):
    class FakeRequestsResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_get(url, params, timeout, headers=None):
        if "openrouteservice" in url:
            return FakeRequestsResponse({
                "features": [
                    {
                        "geometry": {"coordinates": [-0.22698, 51.97965]},
                        "properties": {
                            "label": "The Arcade, Letchworth Garden City, England, United Kingdom",
                            "country": "United Kingdom",
                            "region": "England",
                            "confidence": 1,
                        },
                    }
                ]
            })
        return FakeRequestsResponse([
            {
                "display_name": "The Arc @ Cyberjaya, Cyberjaya, Sepang, Selangor, Malaysia",
                "lat": "2.9248643",
                "lon": "101.6373443",
                "importance": 0.7,
                "address": {"country": "Malaysia", "state": "Selangor", "city": "Cyberjaya"},
            }
        ])

    import travel_service

    monkeypatch.setattr(travel_service.requests, "get", fake_get)
    service = TravelService(api_key="fake-key")
    service.nominatim_min_interval_seconds = 0

    result = service.geocode_candidates_with_metadata("The Arc Cyberjaya Malaysia")
    candidates = result["candidates"]

    assert candidates[0]["display_name"].startswith("The Arc @ Cyberjaya")
    assert candidates[0]["source"] == "nominatim_geocoded"
    assert result["providers_used"] == ["ors", "nominatim"]


def test_travel_service_nominatim_cache_hit_avoids_http(monkeypatch):
    cached = [
        {
            "display_name": "Cached The Arc",
            "address": "Cyberjaya, Malaysia",
            "latitude": 2.9248643,
            "longitude": 101.6373443,
            "source": "nominatim_geocoded",
        }
    ]

    import travel_service

    monkeypatch.setattr(
        travel_service.database,
        "get_geocode_cache",
        lambda normalized_query, provider, country_hint=None, category_hint=None: cached if provider == "nominatim" else None,
        raising=False,
    )

    def fail_get(*args, **kwargs):
        raise AssertionError("HTTP should not be called for a persistent cache hit")

    monkeypatch.setattr(travel_service.requests, "get", fail_get)
    service = TravelService(api_key="fake-key")

    candidates = service._nominatim_geocode_candidates("The Arc Cyberjaya Malaysia", 5, "my", None)

    assert candidates == cached


def test_travel_service_saves_ors_geocode_cache(monkeypatch):
    saved = []

    class FakeRequestsResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "features": [
                    {
                        "geometry": {"coordinates": [101.6373443, 2.9248643]},
                        "properties": {
                            "label": "The Arc @ Cyberjaya, Malaysia",
                            "country": "Malaysia",
                            "region": "Selangor",
                        },
                    }
                ]
            }

    import travel_service

    monkeypatch.setattr(travel_service.requests, "get", lambda *args, **kwargs: FakeRequestsResponse())
    monkeypatch.setattr(
        travel_service.database,
        "save_geocode_cache",
        lambda **kwargs: saved.append(kwargs) or True,
        raising=False,
    )
    service = TravelService(api_key="fake-key")

    result = service.geocode_candidates_with_metadata("The Arc Cyberjaya Malaysia")

    assert result["candidates"]
    assert saved
    assert saved[0]["provider"] == "ors"
    assert saved[0]["normalized_query"] == "the arc cyberjaya malaysia"


def test_travel_service_nominatim_throttle_serializes_requests(monkeypatch):
    sleeps = []

    class FakeRequestsResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return []

    import travel_service

    travel_service._LAST_NOMINATIM_REQUEST_AT = 0.0
    monkeypatch.setattr(travel_service.requests, "get", lambda *args, **kwargs: FakeRequestsResponse())
    monkeypatch.setattr(travel_service.time, "sleep", lambda seconds: sleeps.append(seconds))
    service = TravelService(api_key="fake-key")
    service.nominatim_min_interval_seconds = 1.0

    service._nominatim_geocode_candidates("first uncached place", 5, None, None)
    service._nominatim_geocode_candidates("second uncached place", 5, None, None)

    assert any(wait >= 0.9 for wait in sleeps)


def test_travel_service_nominatim_failure_returns_ors_candidates_with_warning(monkeypatch):
    class FakeRequestsResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_get(url, params, timeout, headers=None):
        if "openrouteservice" in url:
            return FakeRequestsResponse({
                "features": [
                    {
                        "geometry": {"coordinates": [-0.22698, 51.97965]},
                        "properties": {
                            "label": "The Arcade, Letchworth Garden City, England, United Kingdom",
                            "country": "United Kingdom",
                            "region": "England",
                        },
                    }
                ]
            })
        raise RuntimeError("Nominatim down")

    import travel_service

    monkeypatch.setattr(travel_service.requests, "get", fake_get)
    service = TravelService(api_key="fake-key")
    service.nominatim_min_interval_seconds = 0

    result = service.geocode_candidates_with_metadata("The Arc Cyberjaya Malaysia")

    assert result["geocode_status"] == "partial"
    assert result["providers_used"] == ["ors"]
    assert result["candidates"][0]["source"] == "ors_geocoded"
    assert any("fallback search" in warning for warning in result["warnings"])


def test_travel_service_dedupes_nearby_same_named_candidates():
    service = TravelService(api_key=None)
    first = {
        "display_name": "The Arc @ Cyberjaya",
        "address": "The Arc @ Cyberjaya, Cyber 11, Cyberjaya, Selangor, Malaysia",
        "latitude": 2.9248643,
        "longitude": 101.6373443,
        "source": "nominatim_geocoded",
    }
    second = {
        "display_name": "The Arc Cyberjaya",
        "address": "The Arc Cyberjaya, Persiaran Bestari, Cyber 11, Cyberjaya, Selangor, Malaysia",
        "latitude": 2.9251,
        "longitude": 101.6377,
        "source": "nominatim_geocoded",
    }

    merged = service._merge_geocode_candidates([second], [first], 5)

    assert len(merged) == 1
    assert merged[0]["display_name"] == "The Arc @ Cyberjaya"


def test_scheduling_engine_package_public_api_exports():
    from scheduling_engine import (
        BlockType,
        SchedulingEngine as ExportedSchedulingEngine,
        TimingMode as ExportedTimingMode,
        VersionMismatchError,
        format_clock,
        parse_clock,
        parse_duration_minutes,
    )

    assert ExportedSchedulingEngine is SchedulingEngine
    assert VersionMismatchError.__name__ == "VersionMismatchError"
    assert ExportedTimingMode.FIXED == "fixed"
    assert BlockType.ACTIVITY == "activity"
    assert format_clock(parse_clock("12:00 PM")) == "12:00 PM"
    assert parse_duration_minutes("45 minutes") == 45


def test_chat_api_smoke_returns_schedule_blocks(monkeypatch):
    from fastapi.testclient import TestClient
    import main as backend_main

    class SmokeEngine:
        def route_chat_request(self, message, current_schedule=None):
            return {
                "route": "complex_schedule_command",
                "confidence": 0.9,
                "should_mutate_schedule": True,
                "use_deterministic_parser": False,
                "use_module_a_llm": True,
                "use_advisory_llm": False,
            }

        def parse_text_request(self, message, history=None, current_schedule=None, saved_locations=None, **kwargs):
            return {
                "intent": "add",
                "reply": "Draft created.",
                "transcription": message,
                "date": "2026-05-02",
                "operations": [
                    {
                        "op": "add",
                        "title": "Reading",
                        "timing_mode": "fixed",
                        "fixed_start": "09:00",
                        "duration_minutes": 30,
                    }
                ],
                "activities": [],
                "preferences": {},
            }

        def build_schedule_response(self, parsed, current_schedule=None, latest_request="", saved_locations=None):
            envelope = {
                "date": parsed["date"],
                "version": 1,
                "schema_version": 4,
                "status": "ok",
                "schedule_status": "ok",
                "travel_validation_status": "not_requested",
                "planning_mode": "feasibility_first",
                "allow_clash": False,
                "accurate_travel_time": False,
                "preferences": {},
                "activities": [
                    {
                        "id": "act-reading",
                        "stable_activity_id": "act-reading",
                        "type": "activity",
                        "title": "Reading",
                        "start": "09:00 AM",
                        "end": "09:30 AM",
                        "startTime": "09:00 AM",
                        "endTime": "09:30 AM",
                    }
                ],
                "schedule_blocks": [
                    {
                        "id": "act-reading",
                        "stable_activity_id": "act-reading",
                        "type": "activity",
                        "title": "Reading",
                        "start": "09:00 AM",
                        "end": "09:30 AM",
                        "startTime": "09:00 AM",
                        "endTime": "09:30 AM",
                    }
                ],
                "explanations": [],
                "unscheduled_activities": [],
                "conflicts": [],
                "warnings": [],
            }
            return {"schedule_data": envelope, "transcription": parsed.get("transcription")}

        def compose_result_reply(self, latest_request, parsed, result, allow_clash=False):
            return {
                "reply": "Reading was scheduled from 09:00 AM to 09:30 AM.",
                "reply_status": "success",
                "recommend_allow_clash": False,
                "reply_reason": None,
                "token_usage": {},
            }

    monkeypatch.setattr(backend_main, "scheduling_engine", SmokeEngine())
    monkeypatch.setattr(backend_main.database, "get_user_locations", lambda user_id: [])
    monkeypatch.setattr(backend_main.database, "_parse_schedule_payload", lambda payload, user_id, date: payload)

    response = TestClient(backend_main.app).post(
        "/chat",
        json={"message": "Add reading at 9am", "history": [], "user_id": "smoke-user"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["schedule_data"]["schedule_blocks"]
    assert payload["schedule_data"]["schedule_blocks"][0]["title"] == "Reading"


def test_chat_accurate_travel_request_validates_current_schedule_without_module_a_or_module_d(monkeypatch):
    from fastapi.testclient import TestClient
    import main as backend_main

    class TravelOnlyEngine:
        def __init__(self):
            self.validated = False

        def route_chat_request(self, message, current_schedule=None):
            return {
                "route": "accurate_travel_validation",
                "confidence": 0.95,
                "should_mutate_schedule": False,
                "use_deterministic_parser": False,
                "use_module_a_llm": False,
                "use_advisory_llm": False,
                "reason": "matched_accurate_travel_validation",
            }

        def parse_deterministic_fast_path(self, *args, **kwargs):
            raise AssertionError("travel-only request must not use fast path parser")

        def parse_text_request(self, *args, **kwargs):
            raise AssertionError("travel-only request must not use Module A")

        def apply_operations(self, *args, **kwargs):
            raise AssertionError("travel-only request must not apply operations or run Module D")

        def _apply_accurate_travel_if_requested(self, envelope, saved_locations):
            self.validated = True
            assert envelope["date"] == "2026-05-24"
            assert envelope["accurate_travel_time"] is True
            assert envelope["preferences"]["accurate_travel_time"] is True
            updated = dict(envelope)
            updated["status"] = "location_pending"
            updated["schedule_status"] = "location_pending"
            updated["travel_validation_status"] = "pending_locations"
            updated["location_resolution_requests"] = [
                {"title": "Dinner", "current_guess": "Bangsar", "requires_coordinate": True}
            ]
            return updated

    fake_engine = TravelOnlyEngine()
    monkeypatch.setattr(backend_main, "scheduling_engine", fake_engine)
    monkeypatch.setattr(backend_main.database, "get_user_locations", lambda user_id: [])
    monkeypatch.setattr(backend_main.database, "_parse_schedule_payload", lambda payload, user_id, date: payload)

    response = TestClient(backend_main.app).post(
        "/chat",
        json={
            "message": "now i want the plan with accurate travel time",
            "history": [],
            "user_id": "travel-user",
            "accurate_travel_time": False,
            "current_schedule": _evening_schedule_envelope(),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert fake_engine.validated is True
    assert payload["reply_status"] == "location_pending"
    assert payload["reply"] == "Please confirm the exact locations first so I can calculate accurate travel time."
    assert payload["schedule_data"]["travel_validation_status"] == "pending_locations"
    assert payload["schedule_data"]["date"] == "2026-05-24"


def test_travel_complete_toggle_runs_readiness_without_creating_plan_or_geocoding(monkeypatch):
    from fastapi.testclient import TestClient
    import main as backend_main

    class ToggleTravelEngine:
        def __init__(self):
            self.calls = 0

        def _apply_accurate_travel_if_requested(self, envelope, saved_locations):
            self.calls += 1
            assert envelope["accurate_travel_time"] is True
            assert envelope["preferences"]["accurate_travel_time"] is True
            updated = dict(envelope)
            updated["status"] = "location_pending"
            updated["schedule_status"] = "location_pending"
            updated["travel_validation_status"] = "pending_locations"
            updated["location_resolution_requests"] = [
                {
                    "activity_id": "act-dinner",
                    "title": "Dinner",
                    "current_guess": "Bangsar",
                    "requires_coordinate": True,
                    "location_readiness_status": "missing_coordinates",
                    "geocode_candidates": [],
                }
            ]
            return updated

    fake_engine = ToggleTravelEngine()
    monkeypatch.setattr(backend_main, "scheduling_engine", fake_engine)
    monkeypatch.setattr(backend_main.database, "get_user_locations", lambda user_id: [])

    response = TestClient(backend_main.app).post(
        "/api/travel/complete",
        json={
            "user_id": "travel-user",
            "source": "toggle",
            "schedule": _evening_schedule_envelope(),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert fake_engine.calls == 1
    assert payload["travel_validation_status"] == "pending_locations"
    assert payload["location_resolution_requests"][0]["geocode_candidates"] == []
    assert payload["version"] == _evening_schedule_envelope()["version"]


def test_travel_complete_clears_validation_only_dirty_state(monkeypatch):
    from fastapi.testclient import TestClient
    import main as backend_main

    class ValidatingEngine:
        def _is_activity_entry(self, item):
            return True

        def _is_generic_system_activity_payload(self, item):
            return False

        def _now_iso(self):
            return "2026-06-01T00:00:00+00:00"

        def _apply_accurate_travel_if_requested(self, envelope, saved_locations):
            updated = dict(envelope)
            updated["status"] = "ok"
            updated["schedule_status"] = "ok"
            updated["travel_validation_status"] = "repaired_validated"
            updated["activities"] = envelope["activities"]
            return updated

    monkeypatch.setattr(backend_main, "scheduling_engine", ValidatingEngine())
    monkeypatch.setattr(backend_main.database, "get_user_locations", lambda user_id: [])

    response = TestClient(backend_main.app).post(
        "/api/travel/complete",
        json={
            "user_id": "travel-user",
            "source": "manual",
            "schedule": {
                "date": "2026-08-18",
                "version": 1,
                "activities": [{"id": "lunch", "title": "Lunch", "startTime": "12:00 PM", "endTime": "01:00 PM"}],
                "schedule_blocks": [],
                "explanations": [],
                "unscheduled_activities": [],
                "needs_reschedule": False,
                "needs_travel_validation": True,
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["travel_validation_status"] == "repaired_validated"
    assert payload["needs_reschedule"] is False
    assert payload["needs_travel_validation"] is False
    assert payload["last_rescheduled_at"] == "2026-06-01T00:00:00+00:00"


def test_travel_complete_clears_dirty_flags_after_successful_validation(monkeypatch):
    from fastapi.testclient import TestClient
    import main as backend_main

    class ValidatingEngine:
        def _is_activity_entry(self, item):
            return True

        def _is_generic_system_activity_payload(self, item):
            return False

        def _now_iso(self):
            return "2026-06-01T00:00:00+00:00"

        def _apply_accurate_travel_if_requested(self, envelope, saved_locations):
            updated = dict(envelope)
            updated["status"] = "ok"
            updated["schedule_status"] = "ok"
            updated["travel_validation_status"] = "repaired_validated"
            updated["activities"] = envelope["activities"]
            return updated

    monkeypatch.setattr(backend_main, "scheduling_engine", ValidatingEngine())
    monkeypatch.setattr(backend_main.database, "get_user_locations", lambda user_id: [])

    response = TestClient(backend_main.app).post(
        "/api/travel/complete",
        json={
            "user_id": "travel-user",
            "source": "manual",
            "schedule": {
                "date": "2026-08-18",
                "version": 1,
                "activities": [{"id": "lunch", "title": "Lunch", "startTime": "12:00 PM", "endTime": "01:00 PM"}],
                "schedule_blocks": [],
                "explanations": [],
                "unscheduled_activities": [],
                "needs_reschedule": True,
                "reschedule_reason": "location_changed",
                "needs_travel_validation": True,
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["travel_validation_status"] == "repaired_validated"
    assert payload["needs_reschedule"] is False
    assert payload["reschedule_reason"] is None
    assert payload["needs_travel_validation"] is False


def test_persisted_preferences_fill_missing_schedule_preferences(monkeypatch):
    import main as backend_main

    persisted = {
        "day_start_time": "09:00",
        "day_end_time": "21:00",
        "use_day_boundary_preferences": True,
        "default_start_location": _default_start_location(),
    }
    monkeypatch.setattr(backend_main.database, "get_user_preferences", lambda user_id: persisted)

    envelope = _evening_schedule_envelope()
    merged = backend_main._merge_user_preferences_into_envelope(envelope, "pref-user")

    assert merged["preferences"]["day_start_time"] == "09:00"
    assert merged["preferences"]["day_end_time"] == "21:00"
    assert merged["preferences"]["day_start"] == "09:00"
    assert merged["preferences"]["day_end"] == "21:00"
    assert merged["preferences"]["default_start_location"]["label"] == "Home"


def test_schedule_override_wins_over_persisted_preferences(monkeypatch):
    import main as backend_main

    persisted = {
        "day_start_time": "09:00",
        "day_end_time": "21:00",
        "use_day_boundary_preferences": True,
        "default_start_location": _default_start_location(),
    }
    override = {
        "label": "Campus",
        "display_name": "Campus",
        "address": "Campus",
        "latitude": 2.91,
        "longitude": 101.65,
        "source": "day_override",
    }
    monkeypatch.setattr(backend_main.database, "get_user_preferences", lambda user_id: persisted)

    envelope = _evening_schedule_envelope()
    envelope["preferences"]["day_start_time"] = "08:30"
    envelope["preferences"]["day_start_location_override"] = override
    merged = backend_main._merge_user_preferences_into_envelope(envelope, "pref-user")

    assert merged["preferences"]["day_start_time"] == "08:30"
    assert merged["preferences"]["day_start_location_override"]["label"] == "Campus"
    assert merged["preferences"]["default_start_location"]["label"] == "Home"


def test_module_0_router_classifies_latency_paths():
    engine = SchedulingEngine(DummyClient())
    envelope = _initial_envelope()

    move_lunch_route = engine.route_chat_request("move lunch to 12pm", envelope)
    can_move_lunch_route = engine.route_chat_request("can you move lunch to 12pm", envelope)
    assert move_lunch_route["route"] == "simple_schedule_command"
    assert move_lunch_route["use_deterministic_parser"] is True
    assert move_lunch_route["use_module_a_llm"] is False
    assert can_move_lunch_route["route"] == "simple_schedule_command"
    assert can_move_lunch_route["use_deterministic_parser"] is True
    assert can_move_lunch_route["use_module_a_llm"] is False
    assert engine.route_chat_request("should I move lunch to 12pm", envelope)["route"] == "planning_advice"
    assert engine.route_chat_request("do you think my schedule is too packed", envelope)["route"] == "planning_advice"
    assert engine.route_chat_request("Generate a busy workday for me with meeting, lunch, gym and grocery", envelope)["route"] == "complex_schedule_command"
    empty_accurate_envelope = {"activities": [], "schedule_blocks": [], "accurate_travel_time": True, "preferences": {"accurate_travel_time": True}}
    generation_with_travel = engine.route_chat_request(
        "Plan my day for next Tuesday. I have a doctor appointment from 9:00 AM to 10:00 AM at Sunway Medical, a lunch meeting at 12:30 PM in SS15, and a dinner with family at 7:30 PM at home. Please make it realistic with travel time.",
        empty_accurate_envelope,
    )
    assert generation_with_travel["route"] == "complex_schedule_command"
    assert generation_with_travel["use_module_a_llm"] is True
    assert generation_with_travel["travel_intent"] is True
    new_timed_commitment = engine.route_chat_request(
        "owh i just realise i booked a cinema shows at 5pm and it takes like 2 hours",
        envelope,
    )
    assert new_timed_commitment["route"] == "simple_schedule_command"
    assert new_timed_commitment["use_module_a_llm"] is True
    assert new_timed_commitment["use_deterministic_parser"] is False
    assert new_timed_commitment["reason"] == "temporal_schedule_context_use_llm"
    assert engine.route_chat_request("hello", envelope)["route"] == "general_chat"


def _tuesday_travel_prompt():
    return (
        "Plan my day for next Tuesday. I have a doctor appointment from 9:00 AM to 10:00 AM "
        "at Sunway Medical, a lunch meeting at 12:30 PM in SS15, and a dinner with family "
        "at 7:30 PM at home. I still need to fit in 2 hours of focused work, grocery shopping, "
        "a pharmacy stop, 45 minutes of gym, and 30 minutes to prepare some documents before "
        "the doctor appointment. Please make it realistic with travel time."
    )


def _compact_tuesday_operations(include_pharmacy=True):
    operations = [
        {
            "op": "add",
            "title": "Prepare documents",
            "timing_mode": "relative",
            "duration_minutes": 30,
            "anchor_relation": {"kind": "before", "target_title": "Doctor appointment"},
            "travel_required": False,
            "location_resolution_status": "not_required",
            "no_location_reason": "document_preparation",
        },
        {
            "op": "add",
            "title": "Doctor appointment",
            "timing_mode": "fixed",
            "fixed_start": "09:00",
            "duration_minutes": 60,
            "priority": "high",
            "raw_location_text": "Sunway Medical",
            "location_kind": "exact_named_place",
            "location_category": "medical",
            "travel_required": True,
            "location_resolution_status": "needs_coordinates",
        },
        {
            "op": "add",
            "title": "Lunch meeting",
            "timing_mode": "fixed",
            "fixed_start": "12:30",
            "duration_minutes": 60,
            "raw_location_text": "SS15",
            "location_kind": "area_only",
            "location_category": "meal_place",
            "travel_required": True,
            "location_resolution_status": "needs_coordinates",
        },
        {
            "op": "add",
            "title": "Focused work",
            "timing_mode": "flexible",
            "duration_minutes": 120,
            "travel_required": False,
            "location_resolution_status": "not_required",
            "no_location_reason": "focused_work",
        },
        {
            "op": "add",
            "title": "Grocery shopping",
            "timing_mode": "flexible",
            "duration_minutes": 45,
            "location_kind": "category_only",
            "location_category": "supermarket",
            "travel_required": True,
            "location_resolution_status": "needs_coordinates",
        },
        {
            "op": "add",
            "title": "Gym",
            "timing_mode": "flexible",
            "duration_minutes": 45,
            "location_kind": "category_only",
            "location_category": "fitness_center",
            "travel_required": True,
            "location_resolution_status": "needs_coordinates",
        },
        {
            "op": "add",
            "title": "Dinner with family",
            "timing_mode": "fixed",
            "fixed_start": "19:30",
            "duration_minutes": 60,
            "raw_location_text": "home",
            "location_kind": "home",
            "location_category": "home",
            "travel_required": True,
            "location_resolution_status": "resolved_coordinates",
        },
    ]
    if include_pharmacy:
        operations.insert(5, {
            "op": "add",
            "title": "Pharmacy stop",
            "timing_mode": "flexible",
            "duration_minutes": 15,
            "location_kind": "category_only",
            "location_category": "pharmacy",
            "travel_required": True,
            "location_resolution_status": "needs_coordinates",
        })
    return operations


def test_module_a_complex_travel_failure_does_not_use_deterministic_fallback(capsys):
    engine = SchedulingEngine(UnavailableReplyClient())

    parsed = engine.parse_text_request(
        _tuesday_travel_prompt(),
        current_schedule=None,
        history=[],
        saved_locations=[],
    )
    logs = capsys.readouterr().out

    assert parsed["intent"] == "chat"
    assert parsed["operations"] == []
    assert parsed["_failure_type"] == "module_a_unavailable"
    assert "[JPLAN][MODULE_A][FALLBACK_DISABLED] reason=complex_or_travel_intent" in logs
    assert "[JPLAN][MODULE_A][LLM_FALLBACK_PARSE]" not in logs


def test_module_a_rejects_complex_giant_title_output(capsys):
    class GiantTitleClient:
        class models:
            @staticmethod
            def generate_content(*args, **kwargs):
                prompt = _tuesday_travel_prompt()
                return ParserJsonResponse(
                    json.dumps({
                        "intent": "add",
                        "reply": "I translated your request into a draft.",
                        "transcription": prompt,
                        "date": "2026-05-26",
                        "operations": [{
                            "op": "add",
                            "title": prompt,
                            "timing_mode": "flexible",
                            "duration_minutes": 60,
                        }],
                        "activities": [],
                        "preferences": {},
                    })
                )

    engine = SchedulingEngine(GiantTitleClient())

    parsed = engine.parse_text_request(
        _tuesday_travel_prompt(),
        current_schedule=None,
        history=[],
        saved_locations=[],
    )
    logs = capsys.readouterr().out

    assert parsed["intent"] == "chat"
    assert parsed["operations"] == []
    assert parsed["_failure_type"] in {"expected_multi_activity_got_one", "giant_title_or_complex_fallback"}
    assert (
        "[JPLAN][MODULE_A][SCHEMA_INVALID] reason=expected_multi_activity_got_one" in logs
        or "[JPLAN][MODULE_A][PARSE_REJECTED] reason=giant_title_or_complex_fallback" in logs
    )


def test_module_a_complex_travel_success_keeps_semantic_fields():
    class TuesdaySemanticClient:
        class models:
            @staticmethod
            def generate_content(*args, **kwargs):
                prompt = _tuesday_travel_prompt()
                return ParserJsonResponse(
                    json.dumps({
                        "intent": "add",
                        "reply": "I translated your request into a plan draft.",
                        "transcription": prompt,
                        "date": "2026-05-26",
                        "operations": [
                            {
                                "op": "add",
                                "title": "Prepare documents",
                                "timing_mode": "relative",
                                "anchor_relation": {"kind": "before", "target_title": "Doctor appointment"},
                                "duration_minutes": 30,
                                "location_kind": "no_location_required",
                                "location_category": "no_location",
                                "travel_required": False,
                                "location_resolution_status": "not_required",
                                "no_location_reason": "document_preparation",
                            },
                            {
                                "op": "add",
                                "title": "Doctor appointment",
                                "timing_mode": "fixed",
                                "fixed_start": "09:00",
                                "duration_minutes": 60,
                                "raw_location_text": "Sunway Medical",
                                "location_kind": "exact_named_place",
                                "location_category": "medical",
                                "explicit_user_location": True,
                                "travel_required": True,
                                "location_resolution_status": "needs_coordinates",
                            },
                            {
                                "op": "add",
                                "title": "Lunch meeting",
                                "timing_mode": "fixed",
                                "fixed_start": "12:30",
                                "duration_minutes": 60,
                                "raw_location_text": "SS15",
                                "location_kind": "area_only",
                                "location_category": "meal_place",
                                "explicit_user_location": True,
                                "travel_required": True,
                                "location_resolution_status": "needs_coordinates",
                            },
                            {
                                "op": "add",
                                "title": "Focused work",
                                "duration_minutes": 120,
                                "location_kind": "no_location_required",
                                "location_category": "no_location",
                                "travel_required": False,
                                "location_resolution_status": "not_required",
                            },
                            {
                                "op": "add",
                                "title": "Grocery shopping",
                                "duration_minutes": 45,
                                "location_kind": "category_only",
                                "location_category": "supermarket",
                                "travel_required": True,
                                "location_resolution_status": "needs_coordinates",
                            },
                            {
                                "op": "add",
                                "title": "Pharmacy stop",
                                "duration_minutes": 15,
                                "location_kind": "category_only",
                                "location_category": "pharmacy",
                                "travel_required": True,
                                "location_resolution_status": "needs_coordinates",
                            },
                            {
                                "op": "add",
                                "title": "Gym",
                                "duration_minutes": 45,
                                "location_kind": "category_only",
                                "location_category": "fitness_center",
                                "travel_required": True,
                                "location_resolution_status": "needs_coordinates",
                            },
                            {
                                "op": "add",
                                "title": "Dinner with family",
                                "timing_mode": "fixed",
                                "fixed_start": "19:30",
                                "duration_minutes": 60,
                                "raw_location_text": "home",
                                "location_kind": "home",
                                "location_category": "home",
                                "explicit_user_location": True,
                                "travel_required": True,
                                "location_resolution_status": "resolved_coordinates",
                            },
                        ],
                        "activities": [],
                        "preferences": {},
                    })
                )

    engine = SchedulingEngine(TuesdaySemanticClient(), travel_service=FakeTravelService())
    parsed = engine.parse_text_request(
        _tuesday_travel_prompt(),
        current_schedule=None,
        history=[],
        saved_locations=[],
    )

    assert parsed["_reply_source"] == "llm"
    assert len(parsed["operations"]) == 8
    doctor = next(op for op in parsed["operations"] if op["title"] == "Doctor appointment")
    prepare = next(op for op in parsed["operations"] if op["title"] == "Prepare documents")
    pharmacy = next(op for op in parsed["operations"] if op["title"] == "Pharmacy stop")
    assert doctor["raw_location_text"] == "Sunway Medical"
    assert doctor["location"] == "Sunway Medical"
    assert prepare["travel_required"] is False
    assert pharmacy["travel_required"] is True
    assert pharmacy["location_category"] == "pharmacy"


def test_module_a_complex_travel_compact_output_is_accepted(capsys):
    class CompactTuesdayModels:
        def __init__(self):
            self.kwargs = None
            self.payload = {
                "intent": "add",
                "date": "2026-05-26",
                "operations": _compact_tuesday_operations(),
            }

        def generate_content(self, *args, **kwargs):
            self.kwargs = kwargs
            return ParserJsonResponse(json.dumps(self.payload))

    class CompactTuesdayClient:
        def __init__(self):
            self.models = CompactTuesdayModels()

    client = CompactTuesdayClient()
    engine = SchedulingEngine(client, travel_service=FakeTravelService())

    parsed = engine.parse_text_request(
        _tuesday_travel_prompt(),
        current_schedule=None,
        history=[],
        saved_locations=[],
    )
    logs = capsys.readouterr().out

    assert client.models.kwargs["config"]["max_output_tokens"] == 1200
    assert "COMPACT_OUTPUT_MODE" in client.models.kwargs["contents"]
    assert "[JPLAN][MODULE_A][OUTPUT_MODE] compact=true max_output_tokens=1200" in logs
    assert "reply" not in client.models.payload
    assert "transcription" not in client.models.payload
    assert "conflict_analysis" not in client.models.payload
    assert len(parsed["operations"]) == 8
    assert parsed["transcription"] == _tuesday_travel_prompt()
    assert parsed["_reply_source"] == "llm"
    doctor = next(op for op in parsed["operations"] if op["title"] == "Doctor appointment")
    lunch = next(op for op in parsed["operations"] if op["title"] == "Lunch meeting")
    pharmacy = next(op for op in parsed["operations"] if op["title"] == "Pharmacy stop")
    prepare = next(op for op in parsed["operations"] if op["title"] == "Prepare documents")
    focused = next(op for op in parsed["operations"] if op["title"] == "Focused work")
    assert doctor["raw_location_text"] == "Sunway Medical"
    assert doctor["location"] == "Sunway Medical"
    assert lunch["raw_location_text"] == "SS15"
    assert prepare["travel_required"] is False
    assert focused["travel_required"] is False
    assert pharmacy["travel_required"] is True
    assert pharmacy["location_category"] == "pharmacy"
    assert all("location_warning" not in op for op in client.models.payload["operations"])
    assert all("parse_notes" not in op for op in client.models.payload["operations"])


def test_module_a_complex_travel_rejects_compact_output_missing_expected_operation(capsys):
    class MissingPharmacyClient:
        class models:
            @staticmethod
            def generate_content(*args, **kwargs):
                return ParserJsonResponse(json.dumps({
                    "intent": "add",
                    "date": "2026-05-26",
                    "operations": _compact_tuesday_operations(include_pharmacy=False),
                }))

    engine = SchedulingEngine(MissingPharmacyClient(), travel_service=FakeTravelService())

    parsed = engine.parse_text_request(
        _tuesday_travel_prompt(),
        current_schedule=None,
        history=[],
        saved_locations=[],
    )
    logs = capsys.readouterr().out

    assert parsed["intent"] == "chat"
    assert parsed["operations"] == []
    assert parsed["_failure_type"] == "missing_expected_operations"
    assert "[JPLAN][MODULE_A][SCHEMA_INVALID] reason=missing_expected_operations" in logs
    assert "[JPLAN][MODULE_A][LLM_FALLBACK_PARSE]" not in logs


def test_module_a_complex_travel_truncated_compact_json_fails_safely(capsys):
    class TruncatedCompactClient:
        class models:
            @staticmethod
            def generate_content(*args, **kwargs):
                response = FakeResponse()
                response.text = '{"intent":"add","date":"2026-05-26","operations":[{"op":"add","title":"Pharmacy stop"'
                return response

    engine = SchedulingEngine(TruncatedCompactClient(), travel_service=FakeTravelService())

    parsed = engine.parse_text_request(
        _tuesday_travel_prompt(),
        current_schedule=None,
        history=[],
        saved_locations=[],
    )
    logs = capsys.readouterr().out

    assert parsed["intent"] == "chat"
    assert parsed["operations"] == []
    assert "couldn't safely parse the full-day plan" in parsed["reply"]
    assert parsed["_failure_type"] == "llm_parse_error"
    assert "[JPLAN][MODULE_A][LLM_FALLBACK_PARSE]" not in logs


def test_module_0_router_detects_accurate_travel_validation_before_optimize():
    engine = SchedulingEngine(DummyClient())
    envelope = _initial_envelope()

    route = engine.route_chat_request("now i want the plan with accurate travel time", envelope)
    actual_route = engine.route_chat_request("now i want my travel time to be actual", envelope)
    regenerate_route = engine.route_chat_request("regenerate the whole plan please with accurate travel time", envelope)

    assert route["route"] == "accurate_travel_validation"
    assert route["use_module_a_llm"] is False
    assert route["use_deterministic_parser"] is False
    assert actual_route["route"] == "accurate_travel_validation"
    assert actual_route["use_module_a_llm"] is False
    assert regenerate_route["route"] == "accurate_travel_validation"
    assert regenerate_route["reason"] == "matched_accurate_travel_validation"


def test_module_0_router_classifies_natural_schedule_wording():
    engine = SchedulingEngine(DummyClient())
    envelope = _evening_schedule_envelope()

    assert engine.route_chat_request("arrange the shopping after gym", envelope)["route"] == "simple_schedule_command"
    assert engine.route_chat_request("put shopping after gym", envelope)["route"] == "simple_schedule_command"
    assert engine.route_chat_request("place dinner before gym", envelope)["route"] == "simple_schedule_command"
    assert engine.route_chat_request("it seems like a bit too late for dinner, can it be early a bit?", envelope)["route"] == "simple_schedule_command"
    assert engine.route_chat_request("dinner is too late, can it be earlier?", envelope)["route"] == "simple_schedule_command"
    assert engine.route_chat_request("dinner is too late", envelope)["route"] == "planning_advice"
    assert engine.route_chat_request("should I move dinner earlier?", envelope)["route"] == "planning_advice"
    assert engine.route_chat_request("can you move dinner earlier?", envelope)["route"] == "simple_schedule_command"
    assert engine.route_chat_request("swap shopping and gym", envelope)["route"] == "simple_schedule_command"
    assert engine.route_chat_request("switch gym and shopping", envelope)["route"] == "simple_schedule_command"
    assert engine.route_chat_request("change the order of gym and shopping", envelope)["route"] == "simple_schedule_command"
    assert engine.route_chat_request("that's too late", envelope)["route"] == "general_chat"
    assert engine.route_chat_request(
        "Can you rearrange my evening so dinner is earlier, shopping still happens, gym is not too late, and FYP gets 3 hours?",
        envelope,
    )["route"] == "complex_schedule_command"


def test_deterministic_fast_path_skips_module_a_llm_and_normalizes_output():
    class FailIfCalledModels:
        def generate_content(self, *args, **kwargs):
            raise AssertionError("Module A LLM should not be called for deterministic fast path")

    class FailIfCalledClient:
        def __init__(self):
            self.models = FailIfCalledModels()

    engine = SchedulingEngine(FailIfCalledClient())
    envelope = _initial_envelope()

    parsed = engine.parse_deterministic_fast_path(
        "move lunch to 12pm",
        current_schedule=envelope,
        history=[],
        saved_locations=[],
    )

    assert parsed["_reply_source"] == "deterministic_fast_path"
    assert parsed["_used_llm"] is False
    assert parsed["operations"][0]["title"] == "Lunch"
    assert parse_clock(parsed["operations"][0]["fixed_start"]) == parse_clock("12:00 PM")


def test_deterministic_fast_path_relative_add_preserves_anchor_request():
    engine = SchedulingEngine(DummyClient())
    parsed = engine.parse_deterministic_fast_path(
        "add dinner after shopping",
        current_schedule=_initial_envelope(),
        history=[],
        saved_locations=[],
    )

    assert parsed["_reply_source"] == "deterministic_fast_path"
    assert parsed["operations"][0]["title"] == "Dinner"
    assert parsed["operations"][0]["anchor_relation"]["kind"] == "after"
    assert parsed["operations"][0]["anchor_relation"]["target_title"] == "Shopping"


def test_deterministic_fast_path_arrange_patterns_create_relative_update():
    engine = SchedulingEngine(DummyClient())
    parsed = engine.parse_deterministic_fast_path(
        "arrange the shopping after gym",
        current_schedule=_evening_schedule_envelope(),
        history=[],
        saved_locations=[],
    )

    operation = parsed["operations"][0]
    assert operation["op"] == "update"
    assert operation["title"] == "Grocery Shopping"
    assert operation["anchor_relation"]["kind"] == "after"
    assert operation["anchor_relation"]["target_title"] == "Gym Workout"


def test_deterministic_fast_path_natural_rationale_edit_falls_back_to_module_a_and_updates_existing():
    message = "i want the grocery run after the client meeting, cause i dont want to carry so much stuff to meet my client"

    class NaturalRelativeEditClient:
        class models:
            @staticmethod
            def generate_content(*args, **kwargs):
                contents = kwargs.get("contents") or args[1]
                assert "CURRENT_ACTIVITY_INDEX" in contents
                assert "Grocery Run" in contents
                assert "Client Meeting" in contents
                return ParserJsonResponse(
                    """
                    {
                      "intent": "edit",
                      "reply": "I will move Grocery Run after Client Meeting.",
                      "transcription": "i want the grocery run after the client meeting, cause i dont want to carry so much stuff to meet my client",
                      "date": "2026-05-02",
                      "operations": [
                        {
                          "op": "add",
                          "title": "Grocery Run",
                          "timing_mode": "relative",
                          "anchor_relation": {"kind": "after", "target_title": "Client Meeting"},
                          "edit_reason": "does not want to carry so much stuff to meet client"
                        }
                      ],
                      "activities": [],
                      "preferences": {}
                    }
                    """
                )

    envelope = _custom_envelope([
        {
            "op": "add",
            "title": "Client Meeting",
            "timing_mode": TimingMode.FIXED,
            "fixed_start": "09:00",
            "duration_minutes": 60,
            "location": "Mid Valley",
        },
        {
            "op": "add",
            "title": "Grocery Run",
            "timing_mode": "flexible",
            "duration_minutes": 45,
            "location": "store",
        },
    ])
    engine = SchedulingEngine(NaturalRelativeEditClient())

    fast_path = engine.parse_deterministic_fast_path(
        message,
        current_schedule=envelope,
        history=[],
        saved_locations=[],
    )

    assert fast_path is None
    assert engine._last_fast_path_fallback_reason == "natural_edit_wording"

    parsed = engine.parse_text_request(
        message,
        current_schedule=envelope,
        history=[],
        saved_locations=[],
        disable_deterministic_fallback=True,
        fallback_reason=engine._last_fast_path_fallback_reason,
    )

    operation = parsed["operations"][0]
    assert operation["op"] == "update"
    assert operation["title"] == "Grocery Run"
    assert operation["preserve_existing_fields"] is True
    assert operation["edit_reason"] == "does not want to carry so much stuff to meet client"

    result = engine.apply_operations(
        envelope=envelope,
        operations=parsed["operations"],
        base_version=envelope["version"],
    )

    assert result["status"] in {"success", "no_operation"}
    updated = result["envelope"]
    assert _count_title(updated, "Grocery Run") == 1
    grocery = _activity_by_title(updated, "Grocery Run")
    assert grocery["duration_minutes"] == 45
    assert grocery["location"] == "store"


def test_natural_question_relative_edit_skips_fast_path_to_module_a():
    engine = SchedulingEngine(DummyClient())
    envelope = _custom_envelope([
        {
            "op": "add",
            "title": "Client Meeting",
            "timing_mode": TimingMode.FIXED,
            "fixed_start": "09:00",
            "duration_minutes": 60,
        },
        {
            "op": "add",
            "title": "Grocery Run",
            "timing_mode": "flexible",
            "duration_minutes": 45,
        },
    ])

    route = engine.route_chat_request("how about grocery run after client meeting?", envelope)
    parsed = engine.parse_deterministic_fast_path(
        "how about grocery run after client meeting?",
        current_schedule=envelope,
        history=[],
        saved_locations=[],
    )

    assert route["use_deterministic_parser"] is False
    assert route["use_module_a_llm"] is True
    assert route["reason"] == "natural_edit_wording"
    assert parsed is None
    assert engine._last_fast_path_fallback_reason == "natural_edit_wording"


def test_tentative_relative_edit_skips_fast_path_to_module_a():
    engine = SchedulingEngine(DummyClient())
    envelope = _custom_envelope([
        {
            "op": "add",
            "title": "Client Meeting",
            "timing_mode": TimingMode.FIXED,
            "fixed_start": "09:00",
            "duration_minutes": 60,
        },
        {
            "op": "add",
            "title": "Grocery Run",
            "timing_mode": "flexible",
            "duration_minutes": 45,
        },
    ])

    parsed = engine.parse_deterministic_fast_path(
        "maybe grocery run after client meeting",
        current_schedule=envelope,
        history=[],
        saved_locations=[],
    )

    assert parsed is None
    assert engine._last_fast_path_fallback_reason == "natural_edit_wording"


def test_deterministic_fast_path_keeps_clean_relative_edit_deterministic():
    engine = SchedulingEngine(DummyClient())
    envelope = _custom_envelope([
        {
            "op": "add",
            "title": "Client Meeting",
            "timing_mode": TimingMode.FIXED,
            "fixed_start": "09:00",
            "duration_minutes": 60,
        },
        {
            "op": "add",
            "title": "Grocery Run",
            "timing_mode": "flexible",
            "duration_minutes": 45,
        },
    ])

    parsed = engine.parse_deterministic_fast_path(
        "put grocery run after client meeting",
        current_schedule=envelope,
        history=[],
        saved_locations=[],
    )

    assert parsed["_reply_source"] == "deterministic_fast_path"
    operation = parsed["operations"][0]
    assert operation["op"] == "update"
    assert operation["title"] == "Grocery Run"
    assert operation["anchor_relation"]["target_title"] == "Client Meeting"


def test_module_a_failure_after_unsafe_fast_path_does_not_use_duplicate_prone_fallback():
    message = "i want the grocery run after the client meeting, cause i dont want to carry so much stuff to meet my client"
    envelope = _custom_envelope([
        {
            "op": "add",
            "title": "Client Meeting",
            "timing_mode": TimingMode.FIXED,
            "fixed_start": "09:00",
            "duration_minutes": 60,
        },
        {
            "op": "add",
            "title": "Grocery Run",
            "timing_mode": "flexible",
            "duration_minutes": 45,
        },
    ])
    engine = SchedulingEngine(UnavailableReplyClient())

    fast_path = engine.parse_deterministic_fast_path(
        message,
        current_schedule=envelope,
        history=[],
        saved_locations=[],
    )
    assert fast_path is None

    parsed = engine.parse_text_request(
        message,
        current_schedule=envelope,
        history=[],
        saved_locations=[],
        disable_deterministic_fallback=True,
        fallback_reason=engine._last_fast_path_fallback_reason,
    )

    assert parsed["intent"] == "chat"
    assert parsed["operations"] == []
    assert parsed["_failure_type"] == "module_a_unavailable"


def test_duplicate_guard_converts_add_with_wrapper_title_to_existing_update():
    engine = SchedulingEngine(DummyClient())
    envelope = _custom_envelope([
        {
            "op": "add",
            "title": "Client Meeting",
            "timing_mode": TimingMode.FIXED,
            "fixed_start": "09:00",
            "duration_minutes": 60,
        },
        {
            "op": "add",
            "title": "Grocery Run",
            "timing_mode": "flexible",
            "duration_minutes": 45,
            "priority": "low",
            "location": "KB01 Mid Valley",
        },
    ])
    grocery = _activity_by_title(envelope, "Grocery Run")
    grocery["location"] = "KB01 Mid Valley"
    grocery["location_label"] = "KB01 Mid Valley"
    grocery["location_source"] = "event_confirmed"
    grocery["resolved_location"] = {
        "display_name": "KB01 Mid Valley",
        "latitude": 3.1184,
        "longitude": 101.6778,
        "source": "event_confirmed",
    }
    grocery["location_status"] = "resolved"

    result = engine.apply_operations(
        envelope=envelope,
        operations=[{
            "op": "add",
            "title": "How about grocery run",
            "timing_mode": TimingMode.RELATIVE,
            "duration_minutes": 60,
            "location": "store",
            "anchor_relation": {"kind": "after", "target_title": "Client Meeting"},
            "_user_message": "how about grocery run after client meeting?",
        }],
        base_version=envelope["version"],
    )

    updated = result["envelope"]
    updated_grocery = _activity_by_title(updated, "Grocery Run")
    assert result["status"] in {"success", "no_operation"}
    assert _count_title(updated, "Grocery Run") == 1
    assert _count_title(updated, "How about grocery run") == 0
    assert updated_grocery["duration_minutes"] == 45
    assert updated_grocery["priority"] == "low"
    assert updated_grocery["location"] == "KB01 Mid Valley"
    assert updated_grocery["resolved_location"]["latitude"] == 3.1184


def test_update_preserve_existing_fields_keeps_confirmed_location_over_parser_default(capsys):
    engine = SchedulingEngine(DummyClient())
    envelope = _custom_envelope([
        {
            "op": "add",
            "title": "Client Meeting",
            "timing_mode": TimingMode.FIXED,
            "fixed_start": "09:00",
            "duration_minutes": 60,
            "location": "KB01 Mid Valley",
        },
        {
            "op": "add",
            "title": "Grocery Run",
            "timing_mode": TimingMode.FIXED,
            "fixed_start": "07:00",
            "duration_minutes": 45,
            "priority": "low",
            "location": "KB01 Mid Valley",
        },
    ])
    grocery = _activity_by_title(envelope, "Grocery Run")
    grocery["location"] = "KB01 Mid Valley"
    grocery["location_label"] = "KB01 Mid Valley"
    grocery["location_normalized"] = "KB01 Mid Valley"
    grocery["location_category"] = "supermarket"
    grocery["location_source"] = "event_confirmed"
    grocery["location_status"] = "resolved"
    grocery["travel_required"] = True
    grocery["scheduled_start"] = 420
    grocery["scheduled_end"] = 465
    grocery["startTime"] = "07:00 AM"
    grocery["endTime"] = "07:45 AM"
    grocery["fixed_start"] = 420
    grocery["fixed_end"] = 465
    grocery["resolved_location"] = {
        "display_name": "KB01 Mid Valley",
        "latitude": 3.1184,
        "longitude": 101.6778,
        "source": "event_confirmed",
    }

    result = engine.apply_operations(
        envelope=envelope,
        operations=[{
            "op": "update",
            "title": "Grocery Run",
            "target_title": "Grocery Run",
            "timing_mode": TimingMode.RELATIVE,
            "anchor_relation": {"kind": "after", "target_title": "Client Meeting"},
            "preserve_existing_fields": True,
            "location": "store",
            "location_label": "store",
            "location_category": "supermarket",
            "location_status": "needs_resolution",
            "location_source": "deterministic_default",
            "travel_required": True,
            "_user_message": "how about move grocery run to after client meeting?",
        }],
        base_version=envelope["version"],
    )

    updated = result["envelope"]
    updated_grocery = _activity_by_title(updated, "Grocery Run")
    assert result["status"] in {"success", "warning"}
    assert updated_grocery["duration_minutes"] == 45
    assert updated_grocery["location"] == "KB01 Mid Valley"
    assert updated_grocery["location_label"] == "KB01 Mid Valley"
    assert updated_grocery["location_status"] == "resolved"
    assert updated_grocery["resolved_location"]["latitude"] == 3.1184
    assert "PRESERVE_LOCATION" in capsys.readouterr().out


def test_update_preserve_existing_fields_allows_explicit_location_change():
    engine = SchedulingEngine(DummyClient())
    envelope = _custom_envelope([
        {
            "op": "add",
            "title": "Grocery Run",
            "timing_mode": "flexible",
            "duration_minutes": 45,
            "location": "KB01 Mid Valley",
        },
    ])
    grocery = _activity_by_title(envelope, "Grocery Run")
    grocery["location"] = "KB01 Mid Valley"
    grocery["location_label"] = "KB01 Mid Valley"
    grocery["location_status"] = "resolved"
    grocery["location_source"] = "event_confirmed"
    grocery["resolved_location"] = {
        "display_name": "KB01 Mid Valley",
        "latitude": 3.1184,
        "longitude": 101.6778,
        "source": "event_confirmed",
    }

    result = engine.apply_operations(
        envelope=envelope,
        operations=[{
            "op": "update",
            "title": "Grocery Run",
            "target_title": "Grocery Run",
            "preserve_existing_fields": True,
            "location": "Lotus Cheras",
            "location_label": "Lotus Cheras",
            "location_status": "resolved",
            "location_source": "selected_geocode",
            "explicit_user_location": True,
            "resolved_location": {
                "display_name": "Lotus Cheras",
                "latitude": 3.09,
                "longitude": 101.74,
                "source": "selected_geocode",
            },
            "_user_message": "change grocery run location to Lotus Cheras",
        }],
        base_version=envelope["version"],
    )

    updated_grocery = _activity_by_title(result["envelope"], "Grocery Run")
    assert updated_grocery["location"] == "Lotus Cheras"
    assert updated_grocery["location_label"] == "Lotus Cheras"
    assert updated_grocery["location_source"] == "selected_geocode"
    assert updated_grocery["resolved_location"]["latitude"] == 3.09


def test_backend_prunes_support_blocks_linked_to_deleted_activity():
    engine = SchedulingEngine(DummyClient())
    blocks = [
        {"id": "dentist", "stable_activity_id": "dentist", "block_type": "activity", "title": "Dentist appointment"},
        {
            "id": "buffer-dentist-dinner",
            "block_type": "buffer",
            "title": "Prep / Buffer",
            "source_activity_id": "dentist",
            "destination_activity_id": "dinner",
            "related_activity_ids": ["dentist", "dinner"],
        },
        {
            "id": "travel-dentist-dinner",
            "block_type": "transition",
            "type": "travel",
            "title": "Travel to Dinner",
            "source_activity_id": "dentist",
            "destination_activity_id": "dinner",
            "related_activity_ids": ["dentist", "dinner"],
        },
        {"id": "dinner", "stable_activity_id": "dinner", "block_type": "activity", "title": "Dinner"},
        {"id": "__start_route__", "type": "start_route", "is_start_route": True, "display_only": True, "title": "Leave Home"},
    ]

    pruned = engine._prune_support_blocks_for_deleted_activities(blocks, ["dinner"])

    assert [block["id"] for block in pruned] == ["dentist", "__start_route__"]


def test_route_transition_minutes_handles_none_prep_buffer():
    engine = SchedulingEngine(DummyClient())
    minutes = engine._transition_minutes(
        {
            "title": "Client Meeting",
            "location": "Mid Valley",
            "location_status": "resolved",
            "travel_required": True,
            "prep_buffer": None,
        },
        {
            "title": "Grocery Run",
            "location": "KB01 Mid Valley",
            "location_status": "resolved",
            "travel_required": True,
            "prep_buffer": None,
        },
        0,
    )

    assert isinstance(minutes, int)
    assert minutes >= 0


def test_start_route_summary_dropped_when_first_event_is_unfit():
    engine = SchedulingEngine(DummyClient())
    summary = {
        "start_location": "Home",
        "first_physical_event": "Gym",
        "first_physical_event_location": "Gym map point",
        "leave_by": "08:55 AM",
        "travel_duration_minutes": 5,
    }

    assert engine._valid_start_route_summary(summary, [{"title": "Gym"}], []) is None
    assert engine._valid_start_route_summary(summary, [{"title": "Lunch"}], []) == summary


def test_duplicate_guard_ambiguous_existing_match_clarifies_without_adding():
    engine = SchedulingEngine(DummyClient())
    envelope = _custom_envelope([
        {
            "op": "add",
            "title": "Client Meeting",
            "timing_mode": TimingMode.FIXED,
            "fixed_start": "09:00",
            "duration_minutes": 60,
        },
        {
            "op": "add",
            "title": "Grocery Run",
            "timing_mode": "flexible",
            "duration_minutes": 45,
        },
        {
            "op": "add",
            "title": "Grocery Shopping",
            "timing_mode": "flexible",
            "duration_minutes": 45,
        },
    ])

    result = engine.apply_operations(
        envelope=envelope,
        operations=[{
            "op": "add",
            "title": "grocery",
            "timing_mode": TimingMode.RELATIVE,
            "anchor_relation": {"kind": "after", "target_title": "Client Meeting"},
            "_user_message": "how about grocery after client meeting?",
        }],
        base_version=envelope["version"],
    )

    assert result["status"] == "no_operation"
    assert result["reply_reason"] == "duplicate_guard_ambiguous_target"
    assert _count_title(result["envelope"], "grocery") == 0
    assert _count_title(result["envelope"], "Grocery Run") == 1
    assert _count_title(result["envelope"], "Grocery Shopping") == 1


def test_duplicate_guard_explicit_add_with_relative_anchor_adds_new_activity():
    engine = SchedulingEngine(DummyClient())
    envelope = _custom_envelope([
        {
            "op": "add",
            "title": "Dinner with family",
            "timing_mode": TimingMode.FIXED,
            "fixed_start": "19:00",
            "duration_minutes": 60,
        },
        {
            "op": "add",
            "title": "Lunch meeting",
            "timing_mode": TimingMode.FIXED,
            "fixed_start": "12:00",
            "duration_minutes": 60,
        },
    ])

    result = engine.apply_operations(
        envelope=envelope,
        operations=[{
            "op": "add",
            "title": "Meeting with client",
            "timing_mode": TimingMode.RELATIVE,
            "anchor_relation": {"kind": "after", "target_title": "Dinner with family"},
            "duration_minutes": 60,
            "_user_message": "add a meeting with client after dinner",
        }],
        base_version=envelope["version"],
    )

    assert result["status"] == "success"
    assert not result.get("ignored_operations")
    assert _count_title(result["envelope"], "Meeting with client") == 1


def test_deterministic_fast_path_arrange_duration_cleans_title():
    engine = SchedulingEngine(DummyClient())
    envelope = _custom_envelope([
        {
            "op": "add",
            "title": "Client Meeting",
            "timing_mode": TimingMode.FIXED,
            "fixed_start": "09:00",
            "duration_minutes": 60,
        },
    ])

    parsed = engine.parse_deterministic_fast_path(
        "Put a 20-minute coffee catch-up right after my client meeting.",
        current_schedule=envelope,
        history=[],
        saved_locations=[],
    )

    operation = parsed["operations"][0]
    assert operation["op"] == "add"
    assert operation["title"] == "Coffee catch-up"
    assert operation["duration_minutes"] == 20
    assert operation["anchor_relation"]["kind"] == "after"
    assert operation["anchor_relation"]["target_title"] == "Client Meeting"


def test_relative_add_inherits_anchor_location_for_coffee_catch_up():
    engine = SchedulingEngine(DummyClient())
    envelope = _custom_envelope([
        {
            "op": "add",
            "title": "Client Meeting",
            "timing_mode": TimingMode.FIXED,
            "fixed_start": "09:00",
            "duration_minutes": 60,
            "location": "Mid Valley",
        },
    ])
    anchor_location = {
        "display_name": "Mid Valley Megamall",
        "latitude": 3.1184,
        "longitude": 101.6778,
        "source": "event_confirmed",
        "confirmed_by_user": True,
    }
    _activity_by_title(envelope, "Client Meeting")["resolved_location"] = dict(anchor_location)
    _activity_by_title(envelope, "Client Meeting")["location_status"] = "resolved"
    for block in envelope["schedule_blocks"]:
        if block.get("title") == "Client Meeting":
            block["resolved_location"] = dict(anchor_location)
            block["location_status"] = "resolved"

    parsed = engine.parse_deterministic_fast_path(
        "Put a 20-minute coffee catch-up right after my client meeting.",
        current_schedule=envelope,
        history=[],
        saved_locations=[],
    )
    result = engine.apply_operations(
        envelope=envelope,
        operations=parsed["operations"],
        base_version=envelope["version"],
    )

    assert result["status"] == "success"
    coffee = _activity_by_title(result["envelope"], "Coffee catch-up")
    assert coffee["duration_minutes"] == 20
    assert coffee["location"] == "Mid Valley"
    assert coffee["location_label"] == "Mid Valley"
    assert coffee["location_source"] == "inferred_from_anchor"
    assert coffee["same_location_as"] == "Client Meeting"
    assert coffee["resolved_location"] == anchor_location
    assert coffee["location_status"] == "resolved"


def test_deterministic_fast_path_relative_add_duration_variants():
    engine = SchedulingEngine(DummyClient())

    call = engine.parse_deterministic_fast_path(
        "Add a 30 min call after lunch.",
        current_schedule=_initial_envelope(),
        history=[],
        saved_locations=[],
    )["operations"][0]
    review = engine.parse_deterministic_fast_path(
        "Add half-hour review after class.",
        current_schedule=_initial_envelope(),
        history=[],
        saved_locations=[],
    )["operations"][0]
    gym = engine.parse_deterministic_fast_path(
        "Schedule a 1-hour gym session after work.",
        current_schedule=_initial_envelope(),
        history=[],
        saved_locations=[],
    )["operations"][0]

    assert call["title"] == "Call"
    assert call["duration_minutes"] == 30
    assert review["title"] == "Review"
    assert review["duration_minutes"] == 30
    assert gym["title"] == "Gym session"
    assert gym["duration_minutes"] == 60


def test_deterministic_fast_path_place_new_activity_before_anchor():
    engine = SchedulingEngine(DummyClient())
    parsed = engine.parse_deterministic_fast_path(
        "place dinner before gym",
        current_schedule=_evening_schedule_envelope(),
        history=[],
        saved_locations=[],
    )

    operation = parsed["operations"][0]
    assert operation["op"] == "add"
    assert operation["title"] == "Dinner"
    assert operation["anchor_relation"]["kind"] == "before"
    assert operation["anchor_relation"]["target_title"] == "Gym Workout"


def test_deterministic_fast_path_soft_adjustment_targets_named_dinner():
    engine = SchedulingEngine(DummyClient())
    envelope = engine.apply_operations(
        envelope=_evening_schedule_envelope(),
        operations=[_dinner_after_grocery_operation()],
        base_version=_evening_schedule_envelope()["version"],
    )["envelope"]

    parsed = engine.parse_deterministic_fast_path(
        "it seems like a bit too late for dinner, can it be early a bit?",
        current_schedule=envelope,
        history=[],
        saved_locations=[],
    )

    assert parsed is None


def test_deterministic_fast_path_soft_adjustment_targets_recent_dinner():
    engine = SchedulingEngine(DummyClient())
    envelope = engine.apply_operations(
        envelope=_evening_schedule_envelope(),
        operations=[_dinner_after_grocery_operation()],
        base_version=_evening_schedule_envelope()["version"],
    )["envelope"]

    parsed = engine.parse_deterministic_fast_path(
        "can it be earlier?",
        current_schedule=envelope,
        history=[
            {"role": "user", "message": "add dinner after shopping"},
            {"role": "assistant", "message": "I've added Dinner after Grocery Shopping."},
        ],
        saved_locations=[],
    )

    assert parsed is None


def test_soft_adjustment_result_is_not_silent_success_when_unchanged():
    engine = SchedulingEngine(DummyClient())
    envelope = engine.apply_operations(
        envelope=_evening_schedule_envelope(),
        operations=[_dinner_after_grocery_operation()],
        base_version=_evening_schedule_envelope()["version"],
    )["envelope"]
    before = _activity_by_title(envelope, "Dinner")["scheduled_start"]

    result = engine.apply_operations(
        envelope=envelope,
        operations=[{
            "op": "update",
            "title": "Dinner",
            "timing_mode": TimingMode.PREFERRED,
            "preferred_adjustment": "earlier",
            "move_direction": "earlier",
            "_user_message": "can it be earlier?",
        }],
        base_version=envelope["version"],
    )

    if result["status"] == "success":
        assert _activity_by_title(result["envelope"], "Dinner")["scheduled_start"] != before
    else:
        assert result["status"] == "no_operation"
        assert result["applied"] is False


def test_module_d_complex_generation_runs_and_preserves_fixed_events():
    envelope = _initial_envelope()

    assert envelope["preferences"]["refinement_reason"] == "initial_generation"
    assert envelope["refinement_skipped_reason"] is None
    assert envelope["refinement_iterations"] >= 0
    assert {activity["title"] for activity in envelope["activities"]} >= {
        "Project Meeting",
        "Seminar",
        "Lunch",
        "FYP Implementation",
        "Grocery Shopping",
        "Gym Workout",
    }
    assert _activity_by_title(envelope, "Project Meeting")["startTime"] == "09:00 AM"
    assert _activity_by_title(envelope, "Seminar")["startTime"] == "11:00 AM"
    assert _activity_by_title(envelope, "Lunch")["startTime"] == "01:00 PM"


def test_module_d_apply_path_empty_current_complex_generation_runs(capsys):
    engine = SchedulingEngine(DummyClient())
    operations = [
        {
            **operation,
            "_user_message": "Can you help me plan a productive day for 24 May?",
            "_router_route": "complex_schedule_command",
            "_router_reason": "multi_activity_generation",
        }
        for operation in _initial_parsed_request()["operations"]
    ]
    capsys.readouterr()

    result = engine.apply_operations(
        envelope=_empty_envelope("2026-05-24"),
        operations=operations,
        base_version=1,
        new_date="2026-05-24",
    )
    logs = capsys.readouterr().out

    assert result["status"] == "success"
    assert result["envelope"]["preferences"]["refinement_reason"] == "initial_generation"
    assert result["envelope"]["refinement_skipped_reason"] is None
    assert "[JPLAN][MODULE_D][POLICY] route=complex_schedule_command add_ops=6 active_before=0 is_apply_operations=true reason=initial_generation" in logs
    assert "[JPLAN][MODULE_D][START] reason=initial_generation" in logs


def test_module_d_simple_edits_skip_refinement(capsys):
    engine = SchedulingEngine(DummyClient())
    envelope = _initial_envelope()
    capsys.readouterr()

    result = engine.apply_operations(
        envelope=envelope,
        operations=[{"op": "remove", "title": "Gym Workout", "_user_message": "remove gym"}],
        base_version=envelope["version"],
    )
    logs = capsys.readouterr().out

    assert result["status"] == "success"
    assert result["envelope"]["preferences"]["refinement_reason"] == "skipped_simple_edit"
    assert result["envelope"]["refinement_skipped_reason"] == "simple_edit"
    assert "[JPLAN][MODULE_D][SKIP] reason=simple_edit" in logs
    assert "[JPLAN][MODULE_D][START]" not in logs


def test_module_d_fixed_time_move_skips_refinement_even_when_conflicting(capsys):
    engine = SchedulingEngine(DummyClient())
    envelope = _initial_envelope()
    capsys.readouterr()

    result = engine.apply_operations(
        envelope=envelope,
        operations=[{
            "op": "update",
            "title": "Lunch",
            "timing_mode": TimingMode.FIXED,
            "fixed_start": "12:00",
            "_user_message": "move lunch to 12pm",
            "_router_route": "simple_schedule_command",
        }],
        base_version=envelope["version"],
    )
    logs = capsys.readouterr().out

    assert result["status"] in {"conflict", "no_operation"}
    assert "[JPLAN][MODULE_D][POLICY] route=simple_schedule_command add_ops=0 active_before=6 is_apply_operations=true reason=skipped_simple_edit" in logs
    assert "[JPLAN][MODULE_D][SKIP] reason=simple_edit" in logs
    assert "[JPLAN][MODULE_D][START]" not in logs


def test_module_d_relative_add_on_existing_schedule_skips_refinement(capsys):
    engine = SchedulingEngine(DummyClient())
    envelope = _evening_schedule_envelope()
    capsys.readouterr()

    result = engine.apply_operations(
        envelope=envelope,
        operations=[{
            **_dinner_after_grocery_operation(),
            "_user_message": "add dinner after shopping",
            "_router_route": "simple_schedule_command",
        }],
        base_version=envelope["version"],
    )
    logs = capsys.readouterr().out

    assert result["status"] == "success"
    assert result["envelope"]["preferences"]["refinement_reason"] == "skipped_simple_edit"
    assert result["envelope"]["refinement_skipped_reason"] == "simple_edit"
    assert "[JPLAN][MODULE_D][SKIP] reason=simple_edit" in logs
    assert "[JPLAN][MODULE_D][START]" not in logs


def test_module_d_priority_noop_does_not_run_refinement(capsys):
    engine = SchedulingEngine(DummyClient())
    envelope = _initial_envelope()
    capsys.readouterr()

    result = engine.apply_operations(
        envelope=envelope,
        operations=[{
            "op": "update",
            "title": "FYP Implementation",
            "priority": "high",
            "priority_update_only": True,
            "_user_message": "set FYP implementation priority high",
        }],
        base_version=envelope["version"],
    )
    logs = capsys.readouterr().out

    assert result["status"] == "no_operation"
    assert result["reply_reason"] == "priority_already_set"
    assert "[JPLAN][MODULE_D][START]" not in logs


def test_module_d_disabled_by_preference_skips():
    engine = SchedulingEngine(DummyClient())
    parsed = _initial_parsed_request()
    parsed["preferences"] = {"allow_clash": False, "enable_refinement": False}

    envelope = engine.build_schedule_response(
        parsed=parsed,
        current_schedule=None,
        latest_request="Generate a busy workday for me.",
    )["schedule_data"]

    assert envelope["preferences"]["refinement_reason"] == "disabled_by_preference"
    assert envelope["refinement_applied"] is False
    assert envelope["refinement_skipped_reason"] == "disabled_by_preference"


def test_module_d_safe_relocation_accepts_score_improvement():
    engine = SchedulingEngine(DummyClient())
    timeline = [
        {
            "id": "act-meeting",
            "stable_activity_id": "act-meeting",
            "title": "Project Meeting",
            "timing_mode": TimingMode.FIXED,
            "fixed_start": parse_clock("9:00 AM"),
            "scheduled_start": parse_clock("9:00 AM"),
            "scheduled_end": parse_clock("10:00 AM"),
            "duration_minutes": 60,
            "priority": "high",
            "is_mandatory": True,
            "trace": [],
        },
        {
            "id": "act-focus",
            "stable_activity_id": "act-focus",
            "title": "FYP Implementation",
            "timing_mode": TimingMode.PREFERRED,
            "preferred_start": parse_clock("10:00 AM"),
            "scheduled_start": parse_clock("3:00 PM"),
            "scheduled_end": parse_clock("4:00 PM"),
            "duration_minutes": 60,
            "priority": "medium",
            "is_mandatory": True,
            "trace": [],
        },
        {
            "id": "act-seminar",
            "stable_activity_id": "act-seminar",
            "title": "Seminar",
            "timing_mode": TimingMode.FIXED,
            "fixed_start": parse_clock("5:00 PM"),
            "scheduled_start": parse_clock("5:00 PM"),
            "scheduled_end": parse_clock("6:00 PM"),
            "duration_minutes": 60,
            "priority": "high",
            "is_mandatory": True,
            "trace": [],
        },
    ]

    refined, unscheduled, meta = engine._apply_module_d_refinement(
        timeline,
        [],
        parse_clock("8:00 AM"),
        parse_clock("10:00 PM"),
        0,
        {"refinement_reason": "initial_generation"},
    )
    focus = next(item for item in refined if item["title"] == "FYP Implementation")

    assert unscheduled == []
    assert meta["refinement_applied"] is True
    assert meta["refinement_accepted_moves"]
    assert focus["scheduled_start"] < parse_clock("3:00 PM")
    assert "Adjusted by Module D refinement" in " ".join(focus["trace"])


def test_module_d_score_threshold_rejects_candidate(monkeypatch):
    import scheduling_engine.module_d_refinement as module_d_refinement

    monkeypatch.setattr(module_d_refinement, "MODULE_D_MIN_IMPROVEMENT", 99999)
    engine = SchedulingEngine(DummyClient())
    timeline = [
        {
            "id": "act-meeting",
            "stable_activity_id": "act-meeting",
            "title": "Project Meeting",
            "timing_mode": TimingMode.FIXED,
            "fixed_start": parse_clock("9:00 AM"),
            "scheduled_start": parse_clock("9:00 AM"),
            "scheduled_end": parse_clock("10:00 AM"),
            "duration_minutes": 60,
            "priority": "high",
            "is_mandatory": True,
            "trace": [],
        },
        {
            "id": "act-focus",
            "stable_activity_id": "act-focus",
            "title": "FYP Implementation",
            "timing_mode": TimingMode.PREFERRED,
            "preferred_start": parse_clock("10:00 AM"),
            "scheduled_start": parse_clock("3:00 PM"),
            "scheduled_end": parse_clock("4:00 PM"),
            "duration_minutes": 60,
            "priority": "medium",
            "is_mandatory": True,
            "trace": [],
        },
    ]

    refined, _, meta = engine._apply_module_d_refinement(
        timeline,
        [],
        parse_clock("8:00 AM"),
        parse_clock("10:00 PM"),
        0,
        {"refinement_reason": "initial_generation"},
    )
    focus = next(item for item in refined if item["title"] == "FYP Implementation")

    assert meta["refinement_applied"] is False
    assert focus["scheduled_start"] == parse_clock("3:00 PM")


def test_module_d_preserves_dependency_order():
    engine = SchedulingEngine(DummyClient())
    parsed = {
        "intent": "schedule",
        "reply": "Draft created.",
        "transcription": "Generate schedule with dependency order.",
        "date": "2026-05-02",
        "preferences": {"allow_clash": False},
        "operations": [
            {"op": "add", "title": "Lunch", "timing_mode": TimingMode.FIXED, "fixed_start": "13:00", "duration_minutes": 60},
            {"op": "add", "title": "FYP Implementation", "timing_mode": TimingMode.RELATIVE, "anchor_relation": {"kind": "after", "target_title": "Lunch"}, "duration_minutes": 90},
            {"op": "add", "title": "Grocery Shopping", "duration_minutes": 45, "location": "store"},
            {"op": "add", "title": "Dinner", "timing_mode": TimingMode.RELATIVE, "anchor_relation": {"kind": "after", "target_title": "Grocery Shopping"}, "duration_minutes": 60},
        ],
    }

    envelope = engine.build_schedule_response(
        parsed=parsed,
        current_schedule=None,
        latest_request="Generate schedule with dependency order.",
    )["schedule_data"]

    lunch = _activity_by_title(envelope, "Lunch")
    fyp = _activity_by_title(envelope, "FYP Implementation")
    grocery = _activity_by_title(envelope, "Grocery Shopping")
    dinner = _activity_by_title(envelope, "Dinner")
    assert parse_clock(fyp["startTime"]) >= parse_clock(lunch["endTime"])
    assert parse_clock(dinner["startTime"]) >= parse_clock(grocery["endTime"])


def test_module_d_optimize_request_uses_fast_path_and_runs_refinement():
    engine = SchedulingEngine(DummyClient())
    envelope = _initial_envelope()

    parsed = engine.parse_deterministic_fast_path(
        "optimize my schedule",
        current_schedule=envelope,
        history=[],
        saved_locations=[],
    )
    result = engine.apply_operations(
        envelope=envelope,
        operations=[{**parsed["operations"][0], "_user_message": "optimize my schedule"}],
        base_version=envelope["version"],
    )

    assert parsed["operations"][0]["op"] == "optimize_schedule"
    if result["status"] == "success":
        assert result["envelope"]["preferences"]["refinement_reason"] == "explicit_optimize"
        assert result["envelope"]["refinement_skipped_reason"] is None
    else:
        assert result["reply_reason"] == "no_safe_refinement"
        assert result["planned_result"]["refinement_skipped_reason"] is None


def test_module_d_direct_refinement_does_not_call_ors():
    service = FakeTravelService(route_error=AssertionError("Module D must not call ORS"))
    engine = SchedulingEngine(DummyClient(), travel_service=service)
    timeline = [
        {
            "id": "act-focus",
            "stable_activity_id": "act-focus",
            "title": "FYP Implementation",
            "timing_mode": TimingMode.PREFERRED,
            "preferred_start": parse_clock("10:00 AM"),
            "scheduled_start": parse_clock("3:00 PM"),
            "scheduled_end": parse_clock("4:00 PM"),
            "duration_minutes": 60,
            "priority": "medium",
            "is_mandatory": True,
            "trace": [],
        },
    ]

    engine._apply_module_d_refinement(
        timeline,
        [],
        parse_clock("8:00 AM"),
        parse_clock("10:00 PM"),
        0,
        {"refinement_reason": "initial_generation"},
    )

    assert service.route_calls == 0


def test_deterministic_fast_path_whole_plan_pronoun_earlier_clarifies():
    engine = SchedulingEngine(DummyClient())
    parsed = engine.parse_deterministic_fast_path(
        "can it be earlier?",
        current_schedule=_initial_envelope(),
        history=[
            {"role": "user", "message": "move the whole plan to 27 May"},
            {"role": "assistant", "message": "I've moved the whole plan to May 27."},
        ],
        saved_locations=[],
    )

    assert parsed is None


def test_deterministic_fast_path_swap_order_creates_relative_update():
    engine = SchedulingEngine(DummyClient())
    parsed = engine.parse_deterministic_fast_path(
        "swap shopping and gym",
        current_schedule=_evening_schedule_envelope(),
        history=[],
        saved_locations=[],
    )

    operation = parsed["operations"][0]
    assert operation["op"] == "update"
    assert operation["title"] == "Grocery Shopping"
    assert operation["anchor_relation"]["kind"] == "before"
    assert operation["anchor_relation"]["target_title"] == "Gym Workout"


def test_planning_advice_reply_does_not_mutate_schedule():
    class AdviceModels:
        def generate_content(self, *args, **kwargs):
            response = FakeResponse()
            response.text = "Moving lunch to 12:00 PM would overlap with Seminar, so I would keep it after Seminar unless you enable Allow Clash."
            return response

    class AdviceClient:
        def __init__(self):
            self.models = AdviceModels()

    engine = SchedulingEngine(AdviceClient())
    envelope = _initial_envelope()
    before = repr(envelope)

    reply = engine.compose_advisory_reply(
        "Should I move lunch to 12pm?",
        current_schedule=envelope,
        allow_clash=False,
        accurate_travel_time=True,
    )

    assert reply["reply_status"] == "advice"
    assert repr(envelope) == before


def test_planning_advice_503_uses_contextual_fallback_without_raw_error(capsys):
    engine = SchedulingEngine(UnavailableReplyClient())
    envelope = _initial_envelope()
    before = repr(envelope)

    reply = engine.compose_advisory_reply(
        "do you think should i push my FYP implementation to tmr?",
        current_schedule=envelope,
        allow_clash=False,
        accurate_travel_time=False,
    )
    logs = capsys.readouterr().out

    assert reply["reply_status"] == "advice"
    assert reply["reply_source"] == "template"
    assert "503" not in reply["reply"]
    assert "UNAVAILABLE" not in reply["reply"]
    assert "FYP Implementation is currently scheduled from 02:00 PM to 05:00 PM" in reply["reply"]
    assert "move FYP Implementation to tomorrow" in reply["reply"]
    assert repr(envelope) == before
    assert "[JPLAN][ADVICE][FALLBACK] reason=llm_unavailable target=FYP Implementation" in logs


def test_planning_advice_timeout_uses_contextual_fallback(monkeypatch):
    import scheduling_engine.module_0_router as module_0_router

    monkeypatch.setattr(module_0_router, "ADVISORY_LLM_TIMEOUT_SECONDS", 0.01)
    engine = SchedulingEngine(SlowReplyClient())
    envelope = _initial_envelope()
    started = time.perf_counter()

    reply = engine.compose_advisory_reply(
        "do you think should i push my FYP implementation to tmr?",
        current_schedule=envelope,
        allow_clash=False,
        accurate_travel_time=False,
    )

    assert time.perf_counter() - started < 0.2
    assert reply["reply_status"] == "advice"
    assert reply["reply_source"] == "template"
    assert reply["llm_fallback_reason"] == "timeout"
    assert "FYP Implementation is currently scheduled from 02:00 PM to 05:00 PM" in reply["reply"]


def test_planning_advice_fallback_without_target_is_generic_and_safe():
    engine = SchedulingEngine(UnavailableReplyClient())
    envelope = _initial_envelope()

    reply = engine.compose_advisory_reply(
        "do you think should i push yoga to tomorrow?",
        current_schedule=envelope,
        allow_clash=False,
        accurate_travel_time=False,
    )

    assert reply["reply_status"] == "advice"
    assert "503" not in reply["reply"]
    assert "I couldn't generate detailed advice right now" in reply["reply"]
    assert "please mention the activity and new date/time" in reply["reply"]


def test_module_a_generation_config_is_limited_and_prompt_keeps_safety_rules():
    class CaptureModels:
        def __init__(self):
            self.kwargs = None

        def generate_content(self, *args, **kwargs):
            self.kwargs = kwargs
            return ParserJsonResponse(
                """
                {
                  "intent": "edit",
                  "transcription": "move lunch to 12pm",
                  "date": "2026-05-02",
                  "operations": [{"op": "update", "title": "Lunch", "timing_mode": "fixed", "fixed_start": "12:00"}],
                  "activities": [],
                  "preferences": {},
                  "conflict_analysis": "May overlap; backend validates."
                }
                """
            )

    class CaptureClient:
        def __init__(self):
            self.models = CaptureModels()

    client = CaptureClient()
    engine = SchedulingEngine(client)
    engine.parse_text_request("move lunch to 12pm", current_schedule=_initial_envelope())

    prompt = client.models.kwargs["contents"]
    config = client.models.kwargs["config"]
    assert config["max_output_tokens"] == 1200
    assert config["temperature"] == 0
    assert "Never reject a requested change because of conflict" in prompt
    assert "operations must not be empty" in prompt
    assert "unrelated fixed events" in prompt
    assert "generic travel" in prompt
    assert "COMPACT_OUTPUT_MODE" not in prompt


def test_module_8_generation_config_is_limited_and_truth_guards_remain():
    class CaptureModels:
        def __init__(self):
            self.kwargs = None

        def generate_content(self, *args, **kwargs):
            self.kwargs = kwargs
            response = FakeResponse()
            response.text = "Lunch has been moved to 12:00 AM."
            return response

    class CaptureClient:
        def __init__(self):
            self.models = CaptureModels()

    client = CaptureClient()
    engine = SchedulingEngine(client)
    envelope = _initial_envelope()
    result = engine.apply_operations(
        envelope=envelope,
        operations=[{"op": "move", "title": "Lunch", "fixed_start": "12:00", "_user_message": "move lunch to 12pm"}],
        base_version=envelope["version"],
        saved_locations=[],
    )
    reply = engine.compose_result_reply(
        latest_request="move lunch to 12pm",
        parsed={"operations": [{"op": "move", "title": "Lunch", "fixed_start": "12:00"}]},
        result=result,
        allow_clash=False,
    )

    assert client.models.kwargs["config"]["max_output_tokens"] == 150
    assert "RESULT_SUMMARY" in client.models.kwargs["contents"]
    assert reply["reply_source"] == "fallback-template"


def test_run_manual_scheduler_rejects_stale_version():
    from scheduling_engine import VersionMismatchError

    engine = SchedulingEngine(DummyClient())
    envelope = {
        "date": "2026-05-26",
        "version": 3,
        "activities": [],
        "schedule_blocks": [],
        "preferences": {"accurate_travel_time": False},
    }

    with pytest.raises(VersionMismatchError):
        engine.run_manual_scheduler(envelope, base_version=2, source="manual_button")


def test_activity_loader_accepts_block_type_activity_but_rejects_support_rows():
    engine = SchedulingEngine(DummyClient())

    assert engine._is_activity_entry({"id": "lunch", "title": "Lunch", "block_type": "activity", "type": "activity"})
    assert not engine._is_activity_entry({"id": "travel", "title": "Travel to office", "block_type": "transition", "type": "travel"})
    assert not engine._is_activity_entry({"id": "route", "title": "Route conflict", "block_type": "route_conflict", "display_only": True})


def test_run_manual_scheduler_keeps_lunch_with_block_type_activity():
    engine = SchedulingEngine(DummyClient())
    activities = []
    for index, title in enumerate([
        "Team stand-up",
        "Client presentation",
        "Follow-up call",
        "Proposal writing",
        "Lunch",
        "Bank visit",
        "Grocery shopping",
        "Coffee break",
    ]):
        item = {
            "id": f"act-{index}",
            "stable_activity_id": f"act-{index}",
            "title": title,
            "block_type": "activity",
            "type": "activity",
            "duration_minutes": 30 if title != "Proposal writing" else 150,
            "location_resolution_status": "not_required",
            "travel_required": False,
        }
        if title in {"Team stand-up", "Client presentation", "Follow-up call"}:
            fixed_start = [8 * 60 + 30, 11 * 60, 16 * 60 + 30][len([a for a in activities if a.get("timing_mode") == TimingMode.FIXED])]
            item.update({
                "timing_mode": TimingMode.FIXED,
                "fixed_start": fixed_start,
                "fixed_end": fixed_start + item["duration_minutes"],
                "is_user_fixed": True,
            })
        activities.append(item)
    envelope = {
        "date": "2026-08-18",
        "version": 1,
        "activities": activities,
        "schedule_blocks": [],
        "preferences": {"accurate_travel_time": False},
        "needs_reschedule": True,
    }

    updated = engine.run_manual_scheduler(envelope, base_version=1, source="manual_button")

    assert updated["travel_validation_status"] != "replan_input_invalid"
    assert len(updated["activities"]) == 8
    assert any(item["title"] == "Lunch" for item in updated["activities"])


def test_run_manual_scheduler_uses_shared_accurate_travel_pipeline(monkeypatch):
    engine = SchedulingEngine(DummyClient())
    calls = []

    def shared_pipeline(envelope, saved_locations):
        calls.append({
            "titles": [item.get("title") for item in envelope.get("activities", [])],
            "block_count": len(envelope.get("schedule_blocks") or []),
            "accurate": envelope.get("accurate_travel_time"),
        })
        updated = deepcopy(envelope)
        updated["travel_validation_status"] = "validated"
        updated["updated_transition_count"] = 0
        return updated

    monkeypatch.setattr(engine, "_apply_accurate_travel_if_requested", shared_pipeline)
    envelope = {
        "date": "2026-05-26",
        "version": 1,
        "activities": [
            {
                "id": "focus",
                "stable_activity_id": "focus",
                "title": "Focused work",
                "duration_minutes": 60,
                "timing_mode": TimingMode.UNSPECIFIED,
                "travel_required": False,
                "location_resolution_status": "not_required",
            },
        ],
        "schedule_blocks": [],
        "preferences": {"accurate_travel_time": True},
        "accurate_travel_time": True,
        "needs_reschedule": True,
    }

    updated = engine.run_manual_scheduler(envelope, base_version=1, source="manual_button")

    assert updated["travel_validation_status"] == "validated"
    assert len(calls) == 1
    assert calls[0]["accurate"] is True
    assert calls[0]["titles"] == ["Focused work"]
    assert calls[0]["block_count"] >= 1


def test_run_manual_scheduler_stops_when_activity_disappears_before_module_c(monkeypatch):
    engine = SchedulingEngine(DummyClient())
    envelope = {
        "date": "2026-08-18",
        "version": 1,
        "activities": [
            {"id": "standup", "title": "Team stand-up", "duration_minutes": 30, "location_resolution_status": "not_required", "travel_required": False},
            {"id": "lunch", "title": "Lunch", "duration_minutes": 60, "location_resolution_status": "not_required", "travel_required": False},
        ],
        "schedule_blocks": [],
        "preferences": {"accurate_travel_time": False},
        "needs_reschedule": True,
    }

    original_loader = engine._load_canonical_activities

    def drop_lunch(payload):
        return [item for item in original_loader(payload) if item.get("title") != "Lunch"]

    monkeypatch.setattr(engine, "_load_canonical_activities", drop_lunch)
    updated = engine.run_manual_scheduler(envelope, base_version=1, source="manual_button")

    assert updated["travel_validation_status"] == "replan_input_invalid"
    assert updated["schedule_status"] == "replan_input_invalid"
    assert updated["needs_reschedule"] is True
    assert any("Lunch" in issue for issue in updated["validation_issues"])


def test_run_manual_scheduler_preserves_fixed_anchor_time():
    engine = SchedulingEngine(DummyClient())
    envelope = {
        "date": "2026-05-26",
        "version": 1,
        "activities": [
            {
                "id": "board",
                "title": "Board meeting",
                "duration_minutes": 120,
                "timing_mode": TimingMode.FIXED,
                "fixed_start": 11 * 60 + 30,
                "fixed_end": 13 * 60 + 30,
                "scheduled_start": 11 * 60 + 30,
                "scheduled_end": 13 * 60 + 30,
                "is_user_fixed": True,
                "repair_protection": "fixed",
                "location": "KLCC",
                "location_status": "resolved",
                "resolved_location": {"latitude": 3.1578, "longitude": 101.7123, "display_name": "KLCC"},
            },
            {
                "id": "gym",
                "title": "Gym workout",
                "duration_minutes": 90,
                "timing_mode": TimingMode.UNSPECIFIED,
                "scheduled_start": 15 * 60,
                "scheduled_end": 16 * 60 + 30,
                "location_resolution_status": "not_required",
                "travel_required": False,
            },
        ],
        "schedule_blocks": [],
        "preferences": {"accurate_travel_time": False},
        "needs_reschedule": True,
    }

    updated = engine.run_manual_scheduler(envelope, base_version=1, source="manual_button")
    board = next(item for item in updated["activities"] if item["title"] == "Board meeting")

    assert board["startTime"] == "11:30 AM"
    assert board["endTime"] == "01:30 PM"
    assert updated["needs_reschedule"] is False
    assert updated["last_rescheduled_at"]


def test_run_manual_scheduler_preserves_manual_flexible_location():
    engine = SchedulingEngine(DummyClient())
    damansara = {
        "label": "Damansara grocery",
        "display_name": "Damansara grocery",
        "address": "Damansara",
        "latitude": 3.138,
        "longitude": 101.615,
        "source": "manual_map_pin",
        "confirmed_by_user": True,
    }
    envelope = {
        "date": "2026-05-26",
        "version": 1,
        "activities": [
            {
                "id": "workshop",
                "title": "Workshop",
                "duration_minutes": 60,
                "timing_mode": TimingMode.FIXED,
                "fixed_start": 9 * 60,
                "fixed_end": 10 * 60,
                "is_user_fixed": True,
                "location_resolution_status": "not_required",
                "travel_required": False,
            },
            {
                "id": "grocery",
                "title": "Grocery shopping",
                "duration_minutes": 45,
                "timing_mode": TimingMode.UNSPECIFIED,
                "location": "Damansara grocery",
                "location_label": "Damansara grocery",
                "location_status": "resolved",
                "location_source": "manual_map_pin",
                "resolved_location": damansara,
            },
        ],
        "schedule_blocks": [],
        "preferences": {"accurate_travel_time": False},
        "needs_reschedule": True,
    }

    updated = engine.run_manual_scheduler(envelope, base_version=1, source="manual_button")
    grocery = next(item for item in updated["activities"] if item["title"] == "Grocery shopping")

    assert grocery["location"] == "Damansara grocery"
    assert grocery["location_source"] == "manual_map_pin"
    assert grocery["resolved_location"]["display_name"] == "Damansara grocery"
    assert grocery["can_move_for_repair"] is True


def test_run_manual_scheduler_promotes_manual_location_for_no_location_flex(capsys):
    engine = SchedulingEngine(DummyClient())
    quiet_place = {
        "label": "Quiet workspace",
        "display_name": "Quiet workspace",
        "address": "Quiet workspace",
        "latitude": 3.141,
        "longitude": 101.707,
        "source": "event_manual_location",
        "confirmed_by_user": True,
    }
    envelope = {
        "date": "2026-05-26",
        "version": 1,
        "activities": [
            {
                "id": "focus",
                "stable_activity_id": "focus",
                "title": "Focused work",
                "duration_minutes": 120,
                "timing_mode": TimingMode.UNSPECIFIED,
                "location": "Quiet workspace",
                "location_label": "Quiet workspace",
                "location_status": "resolved",
                "location_resolution_status": "not_required",
                "location_policy": "no_location_required",
                "location_source": "event_manual_location",
                "resolved_location": quiet_place,
                "travel_required": False,
            },
        ],
        "schedule_blocks": [],
        "preferences": {"accurate_travel_time": False},
        "needs_reschedule": True,
        "reschedule_reason": "location_changed",
    }

    updated = engine.run_manual_scheduler(envelope, base_version=1, source="manual_button")
    focus = next(item for item in updated["activities"] if item["title"] == "Focused work")

    assert focus["location"] == "Quiet workspace"
    assert focus["location_status"] == "resolved"
    assert focus["location_resolution_status"] == "resolved"
    assert focus["location_policy"] == "exact_location_required"
    assert focus["location_kind"] == "exact_named_place"
    assert focus["travel_required"] is True
    assert focus["can_move_for_repair"] is True
    route_context = engine._build_route_context(updated, updated["activities"], [])
    route_titles = {node.get("title") for node in (route_context.get("nodes") or {}).values()}
    assert "Focused work" in route_titles
    assert "[JPLAN][RUN_SCHEDULER][INPUT_LOCATION] title=Focused work travel_required=true location_status=resolved" in capsys.readouterr().out


def test_activity_location_sync_promotes_stale_visual_block_for_route_context():
    engine = SchedulingEngine(DummyClient())
    focused_location = {
        "label": "The Arc @ Cyberjaya",
        "display_name": "The Arc @ Cyberjaya",
        "address": "The Arc @ Cyberjaya",
        "latitude": 2.9221,
        "longitude": 101.6508,
        "source": "event_manual_location",
        "confirmed_by_user": True,
    }
    envelope = {
        "date": "2026-05-26",
        "activities": [
            {
                "id": "focus",
                "stable_activity_id": "focus",
                "title": "Focused work",
                "duration_minutes": 120,
                "location": "The Arc @ Cyberjaya",
                "location_label": "The Arc @ Cyberjaya",
                "location_status": "resolved",
                "location_resolution_status": "resolved",
                "location_policy": "exact_location_required",
                "location_source": "event_manual_location",
                "resolved_location": focused_location,
                "travel_required": True,
            }
        ],
        "schedule_blocks": [
            {
                "block_type": "activity",
                "id": "focus",
                "stable_activity_id": "focus",
                "title": "Focused work",
                "start": "02:51 PM",
                "end": "04:51 PM",
                "location": "The Arc @ Cyberjaya",
                "location_label": "The Arc @ Cyberjaya",
                "location_kind": "no_location_required",
                "location_category": "home_or_online",
                "location_resolution_status": "not_required",
                "travel_required": False,
            }
        ],
    }

    engine._sync_activity_locations_to_schedule_blocks(envelope)
    block = envelope["schedule_blocks"][0]

    assert block["location_policy"] == "exact_location_required"
    assert block["location_kind"] == "exact_named_place"
    assert block["travel_required"] is True
    assert engine._block_requires_travel_coordinate(block) is True


def test_manual_home_location_still_counts_as_route_endpoint():
    engine = SchedulingEngine(DummyClient())
    home_location = {
        "label": "home",
        "display_name": "home",
        "address": "home",
        "latitude": 2.9221,
        "longitude": 101.6508,
        "source": "event_manual_location",
        "confirmed_by_user": True,
    }
    envelope = {
        "date": "2026-05-26",
        "activities": [
            {
                "id": "focus",
                "stable_activity_id": "focus",
                "title": "Focused work",
                "duration_minutes": 120,
                "location": "home",
                "location_label": "home",
                "location_status": "resolved",
                "location_resolution_status": "resolved",
                "location_policy": "exact_location_required",
                "location_source": "event_manual_location",
                "resolved_location": home_location,
                "travel_required": True,
            }
        ],
        "schedule_blocks": [
            {
                "block_type": "activity",
                "id": "focus",
                "stable_activity_id": "focus",
                "title": "Focused work",
                "start": "01:30 PM",
                "end": "03:30 PM",
                "location": "home",
                "location_label": "home",
                "location_kind": "no_location_required",
                "location_category": "home_or_online",
                "location_resolution_status": "not_required",
                "travel_required": False,
            }
        ],
    }

    engine._sync_activity_locations_to_schedule_blocks(envelope)
    block = envelope["schedule_blocks"][0]

    assert block["location_policy"] == "exact_location_required"
    assert block["travel_required"] is True
    assert engine._block_requires_travel_coordinate(block) is True


def test_run_manual_scheduler_with_missing_accurate_location_returns_pending():
    engine = SchedulingEngine(DummyClient(), travel_service=FakeTravelService())
    envelope = {
        "date": "2026-05-26",
        "version": 1,
        "activities": [
            {
                "id": "bank",
                "title": "Bank visit",
                "duration_minutes": 30,
                "timing_mode": TimingMode.UNSPECIFIED,
                "location": "Bank",
                "location_category": "bank",
                "location_status": "needs_coordinates",
                "location_resolution_status": "needs_coordinates",
                "travel_required": True,
            },
        ],
        "schedule_blocks": [],
        "preferences": {
            "accurate_travel_time": True,
            "default_start_location": _default_start_location(),
        },
        "accurate_travel_time": True,
        "needs_reschedule": True,
    }

    updated = engine.run_manual_scheduler(envelope, base_version=1, source="manual_button")

    assert updated["travel_validation_status"] == "pending_locations"
    assert updated["needs_travel_validation"] is True
    assert updated["needs_reschedule"] is False
    assert any(req.get("title") == "Bank visit" for req in updated["location_resolution_requests"])
