"""File-based channel between this backend and the Cartesia voice agent.

    runtime/state.json     snapshot of the current state, rewritten atomically
                           every heartbeat (the agent polls it)
    runtime/events.jsonl   append-only log, one JSON object per line
                           (the agent tails it)
    runtime/control.json   optional, written BY the agent to steer the backend,
                           e.g. {"muted": true} while the agent is speaking
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class StateChannel:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.events_path = self.dir / "events.jsonl"
        self.control_path = self.dir / "control.json"
        self._events = open(self.events_path, "a", encoding="utf-8")
        self._seq = 0
        self._control_mtime: int | None = None
        self.control: dict = {}

    def emit(self, event: str, **payload) -> dict:
        """Append an event to events.jsonl and return it."""
        self._seq += 1
        record = {"v": SCHEMA_VERSION, "seq": self._seq, "ts": round(time.time(), 3), "event": event, **payload}
        self._events.write(json.dumps(record, default=str) + "\n")
        self._events.flush()
        return record

    def write_state(self, state: dict) -> bool:
        """Atomically replace state.json so readers never see a half-written file."""
        body = {"v": SCHEMA_VERSION, "seq": self._seq, "updated_at": round(time.time(), 3), **state}
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")
        # On Windows os.replace fails if a reader has the file open at that
        # exact moment; retry briefly, then give up until the next heartbeat.
        for _ in range(5):
            try:
                os.replace(tmp, self.state_path)
                return True
            except PermissionError:
                time.sleep(0.01)
        return False

    def poll_control(self) -> dict:
        """Re-read control.json if it changed. Returns the current control dict."""
        try:
            mtime = self.control_path.stat().st_mtime_ns
        except FileNotFoundError:
            self.control = {}
            self._control_mtime = None
            return self.control
        if mtime != self._control_mtime:
            try:
                data = json.loads(self.control_path.read_text(encoding="utf-8"))
                self.control = data if isinstance(data, dict) else {}
                self._control_mtime = mtime
                log.info("control.json updated: %s", self.control)
            except (OSError, ValueError):
                pass  # half-written; try again next poll
        return self.control

    def close(self) -> None:
        self._events.close()
