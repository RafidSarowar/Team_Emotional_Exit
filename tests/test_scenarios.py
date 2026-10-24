from vision_backend.config import ScenarioRule
from vision_backend.datatypes import Detection, Observation
from vision_backend.scenarios import IDLE, ScenarioEngine, rule_matches

PERSON_BOX = (100, 100, 200, 300)


def person(motion: float) -> Detection:
    return Detection("person", 0.9, PERSON_BOX, motion=motion)


def rules():
    return [
        ScenarioRule(id="approaching", priority=1, require_any=["person"], motion_scope="target",
                     motion_min=0.08, hold_seconds=0.5, cooldown_seconds=10),
        ScenarioRule(id="lingering", priority=2, require_any=["person"], motion_scope="target",
                     motion_max=0.03, hold_seconds=3.0, cooldown_seconds=10),
        ScenarioRule(id="unattended_motion", priority=3, forbid_any=["person"],
                     motion_min=0.02, hold_seconds=1.0, cooldown_seconds=10),
    ]


def feed(engine, start, end, step, make_obs):
    events, t = [], start
    while t <= end + 1e-9:
        events += engine.update(make_obs(t))
        t += step
    return events


def test_rule_conditions():
    r = rules()
    moving = Observation(0, [person(0.2)], 0.1)
    still = Observation(0, [person(0.0)], 0.0)
    empty_moving = Observation(0, [], 0.05)
    assert rule_matches(r[0], moving) and not rule_matches(r[0], still)
    assert rule_matches(r[1], still) and not rule_matches(r[1], moving)
    assert rule_matches(r[2], empty_moving) and not rule_matches(r[2], moving)


def test_hold_seconds_debounce():
    engine = ScenarioEngine(rules(), release_seconds=1.0)
    assert feed(engine, 0, 0.4, 0.1, lambda t: Observation(t, [person(0.2)], 0.1)) == []
    events = feed(engine, 0.5, 0.6, 0.1, lambda t: Observation(t, [person(0.2)], 0.1))
    assert [(e.kind, e.scenario, e.trigger) for e in events] == [("entered", "approaching", True)]
    assert engine.active == "approaching"


def test_brief_dropout_does_not_flap():
    engine = ScenarioEngine(rules(), release_seconds=1.0)
    feed(engine, 0, 0.6, 0.1, lambda t: Observation(t, [person(0.2)], 0.1))
    # 0.5s of nothing is shorter than release_seconds, so the scenario stays active.
    assert feed(engine, 0.7, 1.2, 0.1, lambda t: Observation(t, [], 0.0)) == []
    assert engine.active == "approaching"
    events = feed(engine, 1.3, 2.0, 0.1, lambda t: Observation(t, [], 0.0))
    assert [(e.kind, e.scenario) for e in events] == [("exited", "approaching")]
    assert engine.active == IDLE


def test_transition_to_lower_priority_after_release():
    engine = ScenarioEngine(rules(), release_seconds=1.0)
    feed(engine, 0, 0.6, 0.1, lambda t: Observation(t, [person(0.2)], 0.1))
    # Person stops moving: lingering needs 3s of hold, approaching releases after 1s.
    events = feed(engine, 0.7, 4.0, 0.1, lambda t: Observation(t, [person(0.0)], 0.0))
    assert [(e.kind, e.scenario) for e in events] == [("exited", "approaching"), ("entered", "lingering")]


def test_higher_priority_preempts():
    engine = ScenarioEngine(rules(), release_seconds=5.0)
    feed(engine, 0, 3.5, 0.1, lambda t: Observation(t, [person(0.0)], 0.0))
    assert engine.active == "lingering"
    events = feed(engine, 3.6, 4.2, 0.1, lambda t: Observation(t, [person(0.3)], 0.1))
    assert [(e.kind, e.scenario) for e in events] == [("exited", "lingering"), ("entered", "approaching")]


def test_cooldown_suppresses_retrigger():
    engine = ScenarioEngine(rules(), release_seconds=0.5)
    moving = lambda t: Observation(t, [person(0.2)], 0.1)  # noqa: E731
    empty = lambda t: Observation(t, [], 0.0)  # noqa: E731
    first = feed(engine, 0, 0.6, 0.1, moving)
    feed(engine, 0.7, 2.0, 0.1, empty)
    second = feed(engine, 2.1, 3.0, 0.1, moving)  # re-entry within the 10s cooldown
    third_start = 20.0
    feed(engine, 3.1, 5.0, 0.1, empty)
    third = feed(engine, third_start, third_start + 0.6, 0.1, moving)
    assert first[-1].trigger is True
    assert second[-1].kind == "entered" and second[-1].trigger is False
    assert third[-1].kind == "entered" and third[-1].trigger is True
