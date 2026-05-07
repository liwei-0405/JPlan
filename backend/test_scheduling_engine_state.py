import os
import sys

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


class FakeTravelService:
    def __init__(self, route_minutes=12, route_error=None, geocode_candidates=None):
        self.route_minutes_value = route_minutes
        self.route_error = route_error
        self.geocode_candidates_value = geocode_candidates or []
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
        return self.route_minutes_value


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

    assert client.models.calls == 3
    assert "[LLM_FALLBACK_PARSE] Used deterministic fallback parser for simple request" in captured
    assert parsed["_reply_source"] == "deterministic_fallback"
    assert parsed["operations"][0]["title"] == "Coffee Break"
    assert _count_title(result["envelope"], "Coffee Break") == 1


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
    assert _activity_by_title(result["envelope"], "FYP Implementation")["scheduled_start"] == parse_clock("14:05")
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
    assert "[JPLAN][LOCATION] Grocery Shopping" in captured
    assert "raw_llm_location=home" in captured
    assert "normalized=store" in captured


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
    assert fyp["location"] == "school"
    assert fyp["location_category"] == "workplace"
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
    assert envelope["travel_validation_status"] == "not_requested"
    assert envelope["location_resolution_requests"] == []
    assert envelope["status"] != "location_pending"


def test_accurate_travel_on_missing_location_returns_location_pending_with_candidates():
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
        "preferences": {"accurate_travel_time": True},
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
    assert request["geocode_candidates"][0]["display_name"] == "Supermarket Cyberjaya"
    assert fake_travel.geocode_calls >= 1


def test_accurate_travel_with_saved_coordinates_uses_route_service():
    fake_travel = FakeTravelService(route_minutes=18)
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    parsed = {
        "intent": "add",
        "reply": "Draft created.",
        "transcription": "Meeting at Main Office followed by Seminar at Library.",
        "date": "2026-05-02",
        "preferences": {"accurate_travel_time": True},
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
    assert fake_travel.route_calls == 1


def test_complete_travel_validation_clears_location_pending_state():
    fake_travel = FakeTravelService(route_minutes=18)
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    pending_envelope = {
        "date": "2026-05-02",
        "status": "location_pending",
        "schedule_status": "location_pending",
        "travel_validation_status": "pending_locations",
        "accurate_travel_time": True,
        "preferences": {"accurate_travel_time": True},
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
        "preferences": {"accurate_travel_time": True},
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
    transition = next(block for block in envelope["schedule_blocks"] if block.get("block_type") == "transition")

    assert envelope["travel_validation_status"] == "validated"
    assert envelope["schedule_status"] == "ok"
    assert envelope["location_resolution_requests"] == []
    assert transition["travel_estimate_source"] == "routing_service"
    assert transition["from_coordinate"] == {"latitude": 2.9, "longitude": 101.6}
    assert transition["to_coordinate"] == {"latitude": 2.91, "longitude": 101.61}
    assert fake_travel.route_calls == 1


def test_accurate_route_duration_retimes_transition_and_creates_idle_slack():
    fake_travel = FakeTravelService(route_minutes=4)
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    pending_envelope = {
        "date": "2026-05-02",
        "status": "location_pending",
        "schedule_status": "location_pending",
        "travel_validation_status": "pending_locations",
        "accurate_travel_time": True,
        "preferences": {"accurate_travel_time": True},
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
    travel = next(block for block in envelope["schedule_blocks"] if block.get("block_type") == "transition")
    new_idle = next(
        block for block in envelope["schedule_blocks"]
        if block.get("block_type") == "idle" and block.get("start") == "10:40 AM"
    )
    seminar = next(block for block in envelope["schedule_blocks"] if block.get("title") == "Seminar")

    assert travel["start"] == "10:56 AM"
    assert travel["end"] == "11:00 AM"
    assert travel["duration_minutes"] == 4
    assert travel["route_duration_minutes"] == 4
    assert parse_clock(travel["end"]) - parse_clock(travel["start"]) == travel["duration_minutes"]
    assert travel["travel_estimate_source"] == "routing_service"
    assert travel["travel_validation_status"] == "validated"
    assert new_idle["end"] == "10:56 AM"
    assert new_idle["duration_minutes"] == 16
    assert seminar["start"] == "11:00 AM"
    assert envelope["travel_validation_status"] == "validated"
    assert envelope["location_resolution_requests"] == []
    assert not any("pending" in explanation.lower() for explanation in envelope["explanations"])
    assert "Accurate travel time has been validated using the routing service." in envelope["explanations"]
    assert all(activity.get("resolved_location") for activity in envelope["activities"])
    assert all(activity.get("location_status") == "resolved" for activity in envelope["activities"])


def test_accurate_travel_with_coordinates_and_ors_failure_uses_fallback_not_location_pending():
    fake_travel = FakeTravelService(route_error=RuntimeError("ORS down"))
    engine = SchedulingEngine(DummyClient(), travel_service=fake_travel)
    parsed = {
        "intent": "add",
        "reply": "Draft created.",
        "transcription": "Meeting at Main Office followed by Seminar at Library.",
        "date": "2026-05-02",
        "preferences": {"accurate_travel_time": True},
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
