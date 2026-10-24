# Team Emotional Exit: Vision Backend

Real-time **YOLO object detection + motion detection** feeding a **3-scenario state machine**. Each scenario triggers a pre-rendered **Cartesia** voice clip. The voice agent and this backend talk through plain files (no sockets).

```
 webcam / video
      │
      ├──► MotionDetector (MOG2 ∧ frame-diff, every frame, 320px) ──┐
      │                                                             ├─► Observation ─► ScenarioEngine ─► AudioPlayer (pygame)
      └──► YoloDetector (background thread, latest frame only) ─────┘   (per-object                       │
                                                                          motion fused)   VoicePackManager ◄── voice_packs/*.zip
                                                                                               │
                                                        runtime/state.json + events.jsonl ◄────┘──► Cartesia agent
                                                        runtime/control.json ◄──────────────────────── Cartesia agent
```

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

python tools/make_placeholder_pack.py   # beep clips so you can test without Cartesia
python main.py                          # webcam 0, window on, audio on (press q to quit)
```

Useful flags:

| Flag | Effect |
|---|---|
| `--source 1` / `--source clip.mp4` | other webcam or a video file (files play at native fps) |
| `--no-window` | headless |
| `--no-audio` | log state only; let the Cartesia agent do playback |
| `--max-seconds 30` | stop automatically |
| `--config other.yaml` | alternate config |

Run the tests with `pytest`.

## Project layout

```
main.py                     CLI entry point
config.yaml                 all tunables + the 3 scenario definitions
vision_backend/
  config.py                 typed config loader (rejects unknown keys)
  motion.py                 MOG2 / frame-diff / hybrid motion detector
  yolo_detector.py          Ultralytics wrapper + AsyncDetector worker thread
  scenarios.py              rule matching + debounced priority state machine
  voice_packs.py            zip discovery, safe extraction, clip selection, hot reload
  audio_player.py           non-blocking playback (pygame → sounddevice → none)
  state_channel.py          state.json / events.jsonl / control.json
  app.py                    the execution loop + HUD overlay
tools/
  make_placeholder_pack.py  generates a beep pack for testing
  make_cartesia_pack.py     renders a real pack via the Cartesia TTS API
  voice_lines.yaml          the lines that script renders
  agent_listener_example.py reference consumer for the agent side
voice_packs/                drop voice pack zips/folders here
runtime/                    created at runtime (gitignored)
tests/
```

## The 3 scenarios

Defined in [config.yaml](config.yaml). The defaults are **placeholders**, so rename and retune them for your concept:

| Priority | id | Fires when | Hold | Cooldown |
|---|---|---|---|---|
| 1 | `approaching` | a person is visible **and** ≥8% of their box is moving | 0.5 s | 12 s |
| 2 | `lingering` | a person is visible **and** ≤3% of their box is moving | 4 s | 20 s |
| 3 | `unattended_motion` | ≥2% of the frame is moving **and** no person is visible | 1 s | 15 s |

Rule keys: `require_any`, `require_all`, `forbid_any`, `min_count`, `motion_scope` (`global` = whole frame, `target` = inside the boxes of the required classes), `motion_min`, `motion_max`, `hold_seconds`, `cooldown_seconds`, `audio` (folder name in the voice pack; defaults to the id).

**State machine behaviour** ([scenarios.py](vision_backend/scenarios.py)):

- `IDLE` → scenario only after its conditions hold **continuously** for `hold_seconds` (debounce).
- An active scenario stays active until it hasn't matched for `engine.release_seconds`, so a single missed detection doesn't make it flap.
- A higher-priority scenario that becomes eligible **preempts** a lower one immediately.
- A voice clip plays on entry only if that scenario's `cooldown_seconds` has passed. Otherwise the entry is logged with `"playback": "cooldown"`.

**Tuning:** the window HUD shows the global motion % and, for each rule, whether it currently matches and the motion value it's comparing. Watch those numbers while acting out each scenario, then set the thresholds.

## Voice pack contract (for the Cartesia teammate)

Drop a `.zip` (or a plain folder) into `voice_packs/`. Inside, put **one folder per scenario audio key** containing one or more `.wav` / `.mp3` / `.ogg` / `.flac` files:

```
my_pack_v3.zip
├── approaching/
│   ├── approaching_01.wav
│   └── approaching_02.wav
├── lingering/
│   └── lingering_01.mp3
└── unattended_motion/
    └── unattended_motion_01.wav
```

- A single wrapper folder at the top of the zip is fine. Scenario folders are found at any depth.
- Several clips per scenario rotate (`audio.selection`: `round_robin` | `random` | `first`).
- **Hot reload:** the folder is rescanned every `audio.rescan_seconds`. The **newest** pack wins per scenario, and older packs fill any scenario it's missing. No restart needed.
- **Write atomically:** write to `name.zip.tmp`, then rename to `name.zip`. Files ending in `.tmp`/`.part` are ignored, and a half-written zip is skipped and retried.
- Optional `manifest.json` to map arbitrary paths: `{"scenarios": {"lingering": ["clips/x.wav"]}}`.
- Zips are extracted to `voice_packs/.extracted/` (gitignored). Entries that try to escape the folder (zip-slip) are rejected.

`python tools/make_cartesia_pack.py` is a working reference. It reads `CARTESIA_API_KEY` from `.env`, renders the lines in `tools/voice_lines.yaml` with `sonic-2`, and writes `voice_packs/cartesia_pack.zip` atomically. Set `CARTESIA_VOICE_ID` in `.env` to pin a voice.

## State channel contract (backend → agent)

All files live in `runtime/` (configurable via `state.dir`). Every record carries `"v": 1` (schema version).

### `runtime/events.jsonl`: append-only, one JSON object per line

Tail it to react to changes. Event types:

```jsonc
{"v":1,"seq":1,"ts":1792822630.09,"event":"started","scenarios":["approaching","lingering","unattended_motion"],"audio_backend":"pygame","clips":{...}}
{"v":1,"seq":4,"ts":1792822638.30,"event":"scenario_entered","scenario":"approaching",
 "description":"A person is in view and actively moving.","audio_key":"approaching",
 "trigger":true,"labels":["person"],"motion":0.92,"motion_scope":"target",
 "clip":"C:\\...\\voice_packs\\.extracted\\cartesia_pack-…\\approaching\\approaching_01.wav",
 "pack":"cartesia_pack.zip","playback":"played"}
{"v":1,"seq":5,"ts":1792822643.12,"event":"scenario_exited","scenario":"approaching","duration":4.83}
{"v":1,"seq":9,"ts":...,"event":"voice_packs_reloaded","clips":{...}}
{"v":1,"seq":12,"ts":...,"event":"stopped"}
```

`playback` is one of `played`, `skipped_busy` (another clip was still playing), `muted`, `no_backend` (audio disabled / `--no-audio`), `no_clip`, `cooldown`, `error`.

### `runtime/state.json`: snapshot, atomically replaced every `state.heartbeat_seconds`

```jsonc
{
  "v": 1, "seq": 7, "updated_at": 1792822649.9, "running": true, "pid": 1234,
  "scenario": "lingering",                    // or "idle"
  "scenario_description": "...", "active_since": 1792822645.7,
  "rules": {"approaching": {"matching": false, "motion": 0.0}, ...},
  "motion_ratio": 0.0012,
  "detections": [{"label": "person", "confidence": 0.87, "box": [x1,y1,x2,y2], "motion": 0.0}],
  "voice": {"backend": "pygame", "muted": false, "playing": false, "last": {...}},
  "voice_packs": {"approaching": ["cartesia_pack.zip/approaching_01.wav", ...]},
  "fps": 29.8
}
```

If `updated_at` is more than a few seconds old, or `running` is `false`, the backend is down.

### `runtime/control.json`: agent → backend (optional)

The agent can write this file (atomically: tmp + rename) to steer the backend. Supported keys:

| Key | Effect |
|---|---|
| `"muted": true` | backend stops playing clips (entries are still logged with `"playback": "muted"`), e.g. while the live agent is talking |

## Integrating the Cartesia agent

[tools/agent_listener_example.py](tools/agent_listener_example.py) has copy-pasteable helpers:

```python
from pathlib import Path
from agent_listener_example import follow_events, read_state, set_control

for ev in follow_events(Path("runtime/events.jsonl")):
    if ev["event"] == "scenario_entered":
        # e.g. switch the agent's system prompt / persona to ev["scenario"]
        ...
```

There are two ways to split playback:

1. **Backend plays the clips (default).** The agent watches events for context, and writes `{"muted": true}` to `control.json` while it speaks live so the two don't overlap.
2. **Agent plays everything.** Run `python main.py --no-audio`. Each `scenario_entered` event with `"trigger": true` still includes the resolved `clip` path, so the agent can play that file or generate a fresh line with Cartesia on the fly.

The agent only needs read access to `runtime/` (and write access for `control.json`). Both processes can run on the same machine or share the folder over a synced drive.

## Configuration notes

- **Secrets:** put `CARTESIA_API_KEY` in `.env` (gitignored). See `.env.example`. Never commit keys.
- **YOLO:** `yolo.model` accepts any Ultralytics weights (`yolov8n.pt`, `yolo11n.pt`, custom). `yolo.classes` limits detection to the listed COCO names. To plug in your existing detection script, keep the `detect(frame) -> list[Detection]` signature in [yolo_detector.py](vision_backend/yolo_detector.py).
- **Motion:** `hybrid` (default) counts only pixels that are both foreground (MOG2) and changing frame-to-frame (diff). That makes it robust to lighting drift and to background noise that MOG2 alone picks up. Use `mog2` if slow movements are being missed.
- **GPU:** set `yolo.device: cuda:0` after installing a CUDA build of PyTorch.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Could not open video source: 0` | try `--source 1`; close other apps using the camera |
| Scenarios never fire | check the HUD motion values; lower `motion_min` / `hold_seconds` |
| Constant `unattended_motion` from a flickering light or screen | raise `motion.diff_threshold` / `min_blob_area`, or `motion_min` |
| `No audio backend available` | `pip install pygame`; check an output device exists. Or use `--no-audio` |
| `No clips for scenario(s): [...]` | pack folder names must equal the scenarios' `audio` keys |
