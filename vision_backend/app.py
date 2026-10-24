"""Main execution loop: capture -> motion + YOLO -> scenario engine -> voice + state."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import cv2
import numpy as np

from .audio_player import AudioPlayer
from .config import AppConfig
from .datatypes import Observation
from .motion import MotionDetector
from .scenarios import IDLE, ScenarioEngine, ScenarioEvent, rule_motion
from .state_channel import StateChannel
from .voice_packs import VoicePackManager

log = logging.getLogger(__name__)

# Detections older than this are dropped, e.g. if the YOLO thread stalls.
STALE_DETECTIONS_SECONDS = 2.0


class VisionApp:
    """Wires the components together. `step()` processes one frame and is
    independent of the camera, so it can be driven by tests or other sources."""

    def __init__(self, cfg: AppConfig, detector, player: AudioPlayer | None = None):
        self.cfg = cfg
        self.detector = detector  # needs .submit(frame) and .latest() -> (detections, timestamp)
        self.motion = MotionDetector(cfg.motion)
        self.engine = ScenarioEngine(cfg.scenarios, cfg.engine.release_seconds)
        self.packs = VoicePackManager(
            cfg.resolve(cfg.audio.voice_packs_dir),
            [r.audio_key for r in cfg.scenarios],
            cfg.audio.selection,
        )
        backend = cfg.audio.backend if cfg.audio.enabled else "none"
        self.player = player or AudioPlayer(backend)
        self.channel = StateChannel(cfg.resolve(cfg.state.dir))

        self.last_obs = Observation(timestamp=time.time())
        self.last_voice: dict = {}
        self.fps = 0.0
        self._last_heartbeat = 0.0

    # -- per-frame pipeline ------------------------------------------------

    def step(self, frame: np.ndarray, now: float | None = None) -> Observation:
        now = time.time() if now is None else now

        # 1. Motion runs synchronously on every frame (cheap at 320px wide).
        motion_ratio, motion_boxes = self.motion.process(frame)

        # 2. YOLO runs on its own thread; use whatever result is freshest.
        self.detector.submit(frame)
        detections, det_time = self.detector.latest()
        if now - det_time > STALE_DETECTIONS_SECONDS:
            detections = []

        # 3. Fuse: how much of each detected object is actually moving.
        for d in detections:
            d.motion = self.motion.region_ratio(d.box)

        obs = Observation(now, detections, motion_ratio, motion_boxes)
        self.last_obs = obs

        # 4. Scenario state machine -> voice triggers + event log.
        for event in self.engine.update(obs):
            self._handle_event(event)

        # 5. Housekeeping: pick up new voice packs, agent control, heartbeat.
        if self.packs.maybe_rescan(now, self.cfg.audio.rescan_seconds):
            self.channel.emit("voice_packs_reloaded", clips=self.packs.summary())
        self.player.muted = bool(self.channel.poll_control().get("muted", False))
        if now - self._last_heartbeat >= self.cfg.state.heartbeat_seconds:
            self._last_heartbeat = now
            self.write_state(running=True)
        return obs

    def _handle_event(self, event: ScenarioEvent) -> None:
        if event.kind == "exited":
            log.info("Scenario exited: %s (%.1fs)", event.scenario, event.duration)
            self.channel.emit("scenario_exited", scenario=event.scenario, duration=round(event.duration, 2))
            return

        rule = self.engine.rule(event.scenario)
        payload = {
            "scenario": event.scenario,
            "description": rule.description,
            "audio_key": rule.audio_key,
            "trigger": event.trigger,
            **event.details,
        }
        if event.trigger:
            clip = self.packs.pick(rule.audio_key)
            if clip is None:
                payload["playback"] = "no_clip"
            else:
                payload["clip"] = str(clip.path)
                payload["pack"] = clip.pack
                payload["playback"] = self.player.play(clip.path, interrupt=self.cfg.audio.interrupt)
            self.last_voice = {k: payload.get(k) for k in ("scenario", "clip", "pack", "playback")}
            self.last_voice["at"] = round(event.timestamp, 3)
        else:
            payload["playback"] = "cooldown"
        log.info("Scenario entered: %s -> %s", event.scenario, payload["playback"])
        self.channel.emit("scenario_entered", **payload)

    def write_state(self, running: bool) -> None:
        obs, engine = self.last_obs, self.engine
        active = engine.active
        self.channel.write_state({
            "running": running,
            "pid": os.getpid(),
            "scenario": active,
            "scenario_description": engine.rule(active).description if active != IDLE else "",
            "active_since": round(engine.active_since, 3) if active != IDLE else None,
            "rules": {
                r.id: {"matching": engine.last_matches[r.id], "motion": round(rule_motion(r, obs), 4)}
                for r in engine.rules
            },
            "motion_ratio": round(obs.motion_ratio, 4),
            "detections": [d.to_dict() for d in obs.detections],
            "voice": {
                "backend": self.player.backend_name,
                "muted": self.player.muted,
                "playing": self.player.busy(),
                "last": self.last_voice,
            },
            "voice_packs": self.packs.summary(),
            "fps": round(self.fps, 1),
        })

    # -- camera loop ----------------------------------------------------------

    def run(self, max_seconds: float | None = None) -> None:
        source = self.cfg.video.source
        if isinstance(source, str) and source.isdigit():
            source = int(source)
        if isinstance(source, str) and not source.startswith(("rtsp:", "http:", "https:")):
            source = str(self.cfg.resolve(source))
        cap = cv2.VideoCapture(source)
        if isinstance(source, int):
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.video.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.video.height)
        if not cap.isOpened():
            raise RuntimeError(f"Could not open video source: {source!r}")

        self.packs.rescan()
        self.channel.emit(
            "started", source=str(source), scenarios=[r.id for r in self.cfg.scenarios],
            audio_backend=self.player.backend_name, clips=self.packs.summary(),
        )
        log.info("Running on %r. Press q in the window or Ctrl+C to stop.", source)

        # Video files are paced to their native frame rate, because the scenario
        # timings (hold, release, cooldown) are measured in wall-clock seconds.
        is_file = isinstance(source, str) and Path(source).is_file()
        file_fps = cap.get(cv2.CAP_PROP_FPS) if is_file else 0
        frame_interval = 1.0 / file_fps if file_fps and file_fps > 0 else 0.0

        started = time.time()
        last = started
        try:
            while True:
                if frame_interval:
                    wait = last + frame_interval - time.time()
                    if wait > 0:
                        time.sleep(wait)
                ok, frame = cap.read()
                if not ok:
                    if is_file and self.cfg.video.loop_file:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    log.info("Video source ended")
                    break

                now = time.time()
                dt = now - last
                last = now
                if dt > 0:
                    self.fps = 0.9 * self.fps + 0.1 * (1.0 / dt) if self.fps else 1.0 / dt

                obs = self.step(frame, now)

                if self.cfg.video.show_window:
                    cv2.imshow("Emotional Exit - vision backend", draw_overlay(frame, obs, self))
                    if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                        break
                if max_seconds is not None and now - started >= max_seconds:
                    break
        except KeyboardInterrupt:
            pass
        finally:
            cap.release()
            if self.cfg.video.show_window:
                cv2.destroyAllWindows()
            self.shutdown()

    def shutdown(self) -> None:
        self.player.stop()
        if hasattr(self.detector, "stop"):
            self.detector.stop()
        self.channel.emit("stopped")
        self.write_state(running=False)
        self.channel.close()


# -- visualization -------------------------------------------------------------

_SCENARIO_COLORS = [(0, 200, 255), (255, 160, 0), (80, 80, 255), (200, 0, 200)]


def draw_overlay(frame: np.ndarray, obs: Observation, app: VisionApp) -> np.ndarray:
    out = frame.copy()
    for (x1, y1, x2, y2) in obs.motion_boxes:
        cv2.rectangle(out, (x1, y1), (x2, y2), (0, 140, 255), 1)
    for d in obs.detections:
        x1, y1, x2, y2 = d.box
        cv2.rectangle(out, (x1, y1), (x2, y2), (0, 220, 0), 2)
        label = f"{d.label} {d.confidence:.2f} mv {d.motion:.0%}"
        cv2.putText(out, label, (x1, max(15, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 220, 0), 1, cv2.LINE_AA)

    engine = app.engine
    ids = [r.id for r in engine.rules]
    color = (180, 180, 180) if engine.active == IDLE else _SCENARIO_COLORS[ids.index(engine.active) % len(_SCENARIO_COLORS)]
    lines = [
        (f"SCENARIO: {engine.active.upper()}", color),
        (f"motion {obs.motion_ratio:.1%}   fps {app.fps:.0f}   audio {app.player.backend_name}"
         + ("  [MUTED]" if app.player.muted else ""), (230, 230, 230)),
    ]
    for r in engine.rules:
        mark = "x" if engine.last_matches[r.id] else " "
        lines.append((f"[{mark}] {r.id}  mv {rule_motion(r, obs):.1%}", (230, 230, 230)))

    cv2.rectangle(out, (0, 0), (420, 22 + 22 * len(lines)), (0, 0, 0), -1)
    for i, (text, c) in enumerate(lines):
        cv2.putText(out, text, (10, 24 + 22 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.55, c, 1, cv2.LINE_AA)
    return out
