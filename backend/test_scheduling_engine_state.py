import os
import sys

import pytest

sys.path.append(os.path.dirname(__file__))

from scheduling_engine import SchedulingEngine, TimingMode, parse_clock


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
