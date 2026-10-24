"""Reference consumer for the Cartesia agent side of the file channel.

Tails runtime/events.jsonl and reacts to scenario changes. Copy the
`follow_events` / `set_control` helpers into the voice agent.

    python tools/agent_listener_example.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent.parent / "runtime"


def follow_events(path: Path, from_start: bool = False):
    """Yield events appended to events.jsonl, like `tail -f`."""
    while not path.exists():
        time.sleep(0.2)
    with open(path, "r", encoding="utf-8") as f:
        if not from_start:
            f.seek(0, 2)  # only new events
        buffer = ""
        while True:
            chunk = f.readline()
            if not chunk:
                time.sleep(0.05)
                continue
            buffer += chunk
            if not buffer.endswith("\n"):
                continue  # partial line; wait for the rest
            line, buffer = buffer.strip(), ""
            if line:
                yield json.loads(line)


def read_state(path: Path = RUNTIME / "state.json") -> dict | None:
    """Latest snapshot, or None if the backend hasn't written one yet."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, PermissionError):
        return None


def set_control(**values) -> None:
    """Steer the backend, e.g. set_control(muted=True) while the agent talks."""
    path = RUNTIME / "control.json"
    current = {}
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    current.update(values)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(current), encoding="utf-8")
    tmp.replace(path)


def main() -> None:
    state = read_state()
    print("Current scenario:", state and state.get("scenario"))
    print(f"Listening on {RUNTIME / 'events.jsonl'} (Ctrl+C to stop)\n")
    try:
        for ev in follow_events(RUNTIME / "events.jsonl", from_start="--all" in sys.argv):
            if ev["event"] == "scenario_entered":
                print(f"[{ev['seq']}] ENTER {ev['scenario']}: {ev.get('description')}")
                print(f"      trigger={ev['trigger']} playback={ev.get('playback')} clip={ev.get('clip')}")
                # Agent hook: e.g. switch the conversation prompt to this scenario,
                # or play ev["clip"] yourself when the backend runs with --no-audio.
            elif ev["event"] == "scenario_exited":
                print(f"[{ev['seq']}] EXIT  {ev['scenario']} after {ev['duration']}s")
            else:
                print(f"[{ev['seq']}] {ev['event']}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
