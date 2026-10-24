"""Scenario state machine: turns per-frame observations into debounced
scenario transitions (IDLE <-> one of the configured scenarios)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import ScenarioRule
from .datatypes import Observation

IDLE = "idle"


@dataclass
class ScenarioEvent:
    kind: str                  # "entered" | "exited"
    scenario: str
    timestamp: float
    trigger: bool = False      # entered + cooldown elapsed, so the voice clip should play
    duration: float = 0.0      # time spent in the scenario (for "exited")
    details: dict = field(default_factory=dict)


def rule_motion(rule: ScenarioRule, obs: Observation) -> float:
    """The motion value a rule's motion_min/motion_max is compared against."""
    if rule.motion_scope == "global":
        return obs.motion_ratio
    wanted = set(rule.require_any) | set(rule.require_all)
    return max((d.motion for d in obs.detections if d.label in wanted), default=0.0)


def rule_matches(rule: ScenarioRule, obs: Observation) -> bool:
    """True if every condition in the rule holds for this single observation."""
    labels = obs.labels()
    if rule.require_any and not labels.intersection(rule.require_any):
        return False
    if rule.require_all and not set(rule.require_all) <= labels:
        return False
    if rule.forbid_any and labels.intersection(rule.forbid_any):
        return False

    wanted = set(rule.require_any) | set(rule.require_all)
    if wanted and sum(d.label in wanted for d in obs.detections) < rule.min_count:
        return False

    motion = rule_motion(rule, obs)
    if rule.motion_min is not None and motion < rule.motion_min:
        return False
    if rule.motion_max is not None and motion > rule.motion_max:
        return False
    return True


class ScenarioEngine:
    """Debounced, priority-ordered state machine.

    - A rule must match continuously for its `hold_seconds` before it can activate.
    - The active scenario stays active until it has not matched for
      `release_seconds` (this avoids flapping on noisy frames), unless a
      higher-priority scenario becomes eligible and preempts it.
    - On entering a scenario, `trigger` is set only if that scenario's
      `cooldown_seconds` has elapsed since its last trigger.
    """

    def __init__(self, rules: list[ScenarioRule], release_seconds: float = 1.5):
        self.rules = sorted(rules, key=lambda r: r.priority)
        self._by_id = {r.id: r for r in self.rules}
        self.release_seconds = release_seconds
        self.active: str = IDLE
        self.active_since: float = 0.0
        self._last_active_match = 0.0
        self._match_since: dict[str, float | None] = {r.id: None for r in self.rules}
        self._last_trigger: dict[str, float] = {}
        self.last_matches: dict[str, bool] = {r.id: False for r in self.rules}

    def update(self, obs: Observation) -> list[ScenarioEvent]:
        now = obs.timestamp
        for rule in self.rules:
            matched = rule_matches(rule, obs)
            self.last_matches[rule.id] = matched
            if not matched:
                self._match_since[rule.id] = None
            elif self._match_since[rule.id] is None:
                self._match_since[rule.id] = now

        eligible = [
            r for r in self.rules
            if self._match_since[r.id] is not None and now - self._match_since[r.id] >= r.hold_seconds
        ]
        best = eligible[0] if eligible else None

        if self.active != IDLE and self.last_matches[self.active]:
            self._last_active_match = now

        if self.active == IDLE:
            target = best.id if best else IDLE
        elif best is not None and best.priority < self._by_id[self.active].priority:
            target = best.id  # preempted by a higher-priority scenario
        elif now - self._last_active_match >= self.release_seconds:
            target = best.id if best else IDLE
        else:
            target = self.active

        if target == self.active:
            return []
        return self._transition(target, now, obs)

    def _transition(self, target: str, now: float, obs: Observation) -> list[ScenarioEvent]:
        events = []
        if self.active != IDLE:
            events.append(ScenarioEvent("exited", self.active, now, duration=now - self.active_since))
        self.active = target
        self.active_since = now
        if target != IDLE:
            rule = self._by_id[target]
            self._last_active_match = now
            last = self._last_trigger.get(target)
            trigger = last is None or now - last >= rule.cooldown_seconds
            if trigger:
                self._last_trigger[target] = now
            events.append(ScenarioEvent(
                "entered", target, now, trigger=trigger,
                details={
                    "labels": sorted(obs.labels()),
                    "motion": round(rule_motion(rule, obs), 4),
                    "motion_scope": rule.motion_scope,
                },
            ))
        return events

    def rule(self, scenario_id: str) -> ScenarioRule:
        return self._by_id[scenario_id]
